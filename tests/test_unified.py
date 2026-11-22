import asyncio
import json
import sqlite3
import stat

import httpx
import pytest

from app import archive, main, mcp_api
from app.reader_client import ReaderError
from app.sources import registry
from app.sources.api import selections
from app.sources.wecom_engine.query import query


def insert(engine, rows, table="message_table"):
    with sqlite3.connect(engine.current / "message.db") as db:
        if table != "message_table":
            db.execute(f'CREATE TABLE IF NOT EXISTS "{table}"(conversation_id TEXT,message_id TEXT,sender_id TEXT,send_time REAL,content TEXT)')
        db.executemany(f'INSERT INTO "{table}" VALUES (?,?,?,?,?)', rows)


def test_pagination_same_timestamp_across_three_tables(snapshot_engine):
    e = snapshot_engine
    for table in ("message_table", "message_small_table", "kf_message_tableV1"):
        insert(e, [("ties", str(i), "u1", 1795200000, "相同时间原文") for i in range(9)], table)
    all_ids, before = [], None
    while True:
        page = query(e, "/api/messages", {"chat": "ties", "limit": 4, "before": before})
        all_ids.extend(m["message_id"] for m in page["messages"])
        if not page["has_more"]:
            break
        before = page["next_before"]
    assert len(all_ids) == len(set(all_ids)) == 27
    assert {x.split(":")[0] for x in all_ids} == {"message_table", "message_small_table", "kf_message_tableV1"}
    assert page["messages"][0]["is_self"] is None


def test_cursor_cannot_cross_account_or_chat(snapshot_engine):
    e = snapshot_engine
    cursor = query(e, "/api/messages", {"chat": "demo-project"})["next_before"]
    with pytest.raises(ValueError, match="游标"):
        query(e, "/api/messages", {"chat": "demo-other", "before": cursor})
    e.account = "account-B"
    with pytest.raises(ValueError, match="游标"):
        query(e, "/api/messages", {"chat": "demo-project", "before": cursor})


def test_decoded_blob_search_and_unknown_capabilities(snapshot_engine):
    e = snapshot_engine
    insert(e, [("search", "a", "u1", 1795200000, b'\x0a\x0c\x08\x00\x12\x08\x0a\x06' + "你好".encode())])
    with sqlite3.connect(e.current / "message.db") as db:
        db.execute("ALTER TABLE message_table ADD COLUMN content_type INTEGER")
        db.execute("UPDATE message_table SET content_type=2 WHERE conversation_id='search'")
    page = query(e, "/api/search", {"keyword": "你好"})
    assert page["messages"][0]["text"] == "你好"
    assert query(e, "/api/sessions", {})["sessions"][0]["kind"] == "unknown"
    with pytest.raises(NotImplementedError):
        query(e, "/api/members", {"chat": "search"})


@pytest.mark.asyncio
async def test_archive_backfill_incremental_and_source_isolation(isolated, snapshot_engine, monkeypatch):
    e = snapshot_engine
    monkeypatch.setattr(archive, "PAGE", 17)
    manager = archive.ArchiveManager("wecom", e.account)
    async def read(path, params=None):
        return query(e, path, params or {})
    manager.reader.get_json = read
    cfg = manager.load_config()
    cfg.update(enabled=True, copy_media=False)
    manager.save_config(cfg)
    await manager._cycle(cfg)
    root = manager.root(cfg)
    with sqlite3.connect(root / "archive.db") as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 221
        assert db.execute("SELECT COUNT(*) FROM messages WHERE is_self IS NULL").fetchone()[0] == 221
    insert(e, [("demo-project", f"new-{i}", "u1", 2000000000 + i, "增量锁定原文") for i in range(55)])
    await manager._cycle(cfg)
    await manager._cycle(cfg)
    with sqlite3.connect(root / "archive.db") as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 276
        assert db.execute("SELECT COUNT(DISTINCT message_key) FROM messages").fetchone()[0] == 276
    assert archive.ArchiveManager("personal", e.account).root(cfg) != root
    assert archive.ArchiveManager("wecom", "other-account").root(cfg) != root
    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE((root / "archive.db").stat().st_mode) == 0o600
    assert stat.S_IMODE(manager.config_file.stat().st_mode) == 0o600
    assert list(root.glob("conversations/*/messages.md"))


def test_old_archive_schema_upgrade_preserves_message_and_attachment(isolated):
    root = isolated.home / "old"
    root.mkdir()
    with sqlite3.connect(root / "archive.db") as db:
        old_schema = archive.SCHEMA.replace("  message_key TEXT NOT NULL,\n", "").replace(
            "UNIQUE(conversation_id, message_key)", "UNIQUE(conversation_id,seq,local_id)")
        db.executescript(old_schema)
        db.execute("INSERT INTO messages(conversation_id,local_id,seq,time,timestamp,text) VALUES('c',2,3,'2026',1,'旧原文')")
        db.execute("INSERT INTO attachments(message_id,conversation_id,kind,status) VALUES(1,'c','image','ok')")
    db = archive.open_db(root)
    assert db.execute("SELECT message_key,text FROM messages").fetchone()[:] == ("3:2", "旧原文")
    assert db.execute("SELECT message_id FROM attachments").fetchone()[0] == 1
    db.close()


def test_legacy_archive_is_preserved_and_claimed_once(isolated):
    old = dict(archive.DEFAULTS, enabled=True, archive_dir=str(isolated.home / "Legacy"))
    archive.save_config(old)
    hub = archive.ArchiveHub()
    first = hub.get("personal", "verified-old-account")
    hub.migrate_legacy(first)
    cfg = first.load_config()
    assert first.root(cfg) == isolated.home / "Legacy"
    assert cfg["migration_pending"] is True
    assert cfg["enabled"] is False
    second = hub.get("personal", "other-account")
    hub.migrate_legacy(second)
    assert second.root(second.load_config()) != first.root(cfg)
    assert archive.CONFIG_FILE.exists()


@pytest.mark.asyncio
async def test_gateway_auth_health_and_personal_compatibility(readers):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        assert (await client.get("/api/health")).status_code == 401
        client.headers["Authorization"] = "Bearer test-gateway-credential"
        assert (await client.get("/api/health")).json()["ok"] is True
        assert (await client.get("/api/sessions")).json()["sessions"][0]["name"] == "personal会话"
        enterprise = await client.get("/api/messages?source=wecom&chat=shared-chat")
        assert enterprise.json()["source"] == "wecom"
        assert enterprise.json()["messages"][0]["text"] == "wecom原文"
        assert (await client.get("/api/sessions?source=bad")).status_code == 400
        async def offline(*args, **kwargs):
            raise ReaderError(503, "离线")
        readers[0].get_json = offline
        from app.reader_client import personal_reader
        original = personal_reader.get_json
        personal_reader.get_json = offline
        try:
            assert (await client.get("/api/health")).status_code == 200
            assert (await client.get("/api/status")).status_code == 503
        finally:
            personal_reader.get_json = original


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["source", "account", "selection", "ai"])
async def test_ai_discards_slow_response_when_context_changes(readers, isolated, monkeypatch, change):
    started, release = asyncio.Event(), asyncio.Event()
    async def slow(bound, *args):
        started.set()
        await release.wait()
        return {"summary": "旧结果"}
    monkeypatch.setattr(main, "summarize_chat", slow)
    selections["window-A"] = ("personal", "shared-chat", 7)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                headers={"Authorization": "Bearer test-gateway-credential"}) as client:
        task = asyncio.create_task(client.post("/api/summarize", json={
            "chat": "shared-chat", "client_id": "window-A", "selection_revision": 7}))
        await asyncio.wait_for(started.wait(), 2)
        if change == "source":
            registry.select("wecom")
        elif change == "account":
            readers[0].account_id = "account-B"
        elif change == "selection":
            selections["window-A"] = ("personal", "other-chat", 8)
        else:
            isolated.settings.ai_config_file.write_text('{"model":"changed"}')
        release.set()
        response = await task
        assert response.status_code == 409
        assert "summary" not in response.json()


@pytest.mark.asyncio
async def test_ai_context_has_fixed_enterprise_source(readers, monkeypatch):
    seen = []
    async def analysis(bound, *args):
        seen.append((bound.source, bound.account_id))
        messages = await bound.get_json("/api/messages", {"chat": "shared-chat"})
        return {"summary": messages["messages"][0]["text"]}
    monkeypatch.setattr(main, "summarize_chat", analysis)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                headers={"Authorization": "Bearer test-gateway-credential"}) as client:
        response = await client.post("/api/summarize?source=wecom", json={"chat": "shared-chat"})
        assert response.status_code == 200
        assert response.json()["summary"] == "wecom原文"
        assert seen == [("wecom", "wecom-account-A")]


@pytest.mark.asyncio
async def test_mcp_separate_auth_fixed_account_and_revoke(readers):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test") as client:
        init = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}}
        assert (await client.post("/mcp/wecom", json=init)).status_code == 401
        await mcp_api.set_access(mcp_api.Access(enabled=True, confirm=True))
        assert (await client.post("/mcp/wecom", json=init, headers={
            "Authorization": "Bearer test-gateway-credential"})).status_code == 401
        client.headers["Authorization"] = "Bearer " + mcp_api.credential()
        response = await client.post("/mcp/wecom", json=init)
        client.headers["mcp-session-id"] = response.headers["mcp-session-id"]
        call = {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "get_messages", "arguments": {"chat": "shared-chat"}}}
        assert (await client.post("/mcp/wecom", json=call)).json()["result"]["isError"] is False
        registry.select("personal")
        assert (await client.post("/mcp/wecom", json=call)).json()["result"]["isError"] is False
        readers[1].account_id = "wecom-account-B"
        assert (await client.post("/mcp/wecom", json=call)).json()["result"]["isError"] is True
        assert (await client.get("/api/ai-config")).status_code == 401
        await mcp_api.set_access(mcp_api.Access(enabled=False))
        assert (await client.post("/mcp/wecom", json=call)).status_code == 401


def test_agent_entries_and_backups_do_not_overwrite_other_source(isolated, monkeypatch):
    from app import agent_register as personal
    from app.wecom_agent_register import implementation as enterprise
    monkeypatch.setenv("WD_AGENT_HOME", str(isolated.home))
    path = isolated.home / ".cursor/mcp.json"
    path.parent.mkdir()
    path.write_text('{"mcpServers":{"existing":{"command":"keep"}}}')
    personal.register("/fixture/reader", "/fixture/config", ["cursor"])
    enterprise.register("/fixture/reader", "/fixture/config", ["cursor"])
    servers = json.loads(path.read_text())["mcpServers"]
    assert set(servers) == {"existing", "wechat-reader", "wecom-reader"}
    assert servers["wecom-reader"]["args"][-1] == "wecom"
    assert personal.state_file() != enterprise.state_file()
    enterprise.unregister("/fixture/reader", "/fixture/config", ["cursor"])
    assert set(json.loads(path.read_text())["mcpServers"]) == {"existing", "wechat-reader"}
    assert len(list(personal.backups_root().glob("*/*"))) >= 2


def test_codex_toml_entries_remain_valid(isolated, monkeypatch):
    try:
        import tomllib
    except ImportError:
        import tomli as tomllib
    from app import agent_register as personal
    from app.wecom_agent_register import implementation as enterprise
    monkeypatch.setenv("WD_AGENT_HOME", str(isolated.home))
    path = isolated.home / ".codex/config.toml"
    path.parent.mkdir()
    path.write_text('model="keep"\n')
    personal.register("/fixture/reader", "/fixture/config", ["codex"])
    enterprise.register("/fixture/reader", "/fixture/config", ["codex"])
    assert set(tomllib.loads(path.read_text())["mcp_servers"]) == {"wechat-reader", "wecom-reader"}
    enterprise.unregister("/fixture/reader", "/fixture/config", ["codex"])
    assert set(tomllib.loads(path.read_text())["mcp_servers"]) == {"wechat-reader"}


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["personal", "wecom"])
async def test_real_shared_ai_pipeline_contract(readers, isolated, monkeypatch, source):
    from app import sidebar, suggest
    isolated.settings.ai_config_file.write_text(json.dumps({
        "enabled": True, "api_key": "fixture", "model": "shared-model"}))
    prompts = []
    async def completion(ai, messages, **kwargs):
        prompts.append((ai["model"], messages[-1]["content"]))
        content = '{"overview":"核对交付","topics":[]}' if "overview" in messages[0]["content"] else '{"suggestions":["收到，我会核对。"]}'
        return {"content": content, "model": ai["model"]}
    monkeypatch.setattr(sidebar, "chat_completion", completion)
    monkeypatch.setattr(suggest, "chat_completion", completion)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                headers={"Authorization": "Bearer test-gateway-credential"}) as client:
        summary = await client.post("/api/summarize?source=" + source, json={"chat": "shared-chat"})
        draft = await client.post("/api/suggest?source=" + source, json={"chat": "shared-chat"})
        assert summary.status_code == draft.status_code == 200
        assert summary.json()["summary"]["overview"] == "核对交付"
        assert draft.json()["suggestions"] == ["收到，我会核对。"]
        assert all(model == "shared-model" and source + "原文" in prompt for model, prompt in prompts)
        if source == "wecom":
            assert all("本人身份" in prompt for model, prompt in prompts)


@pytest.mark.asyncio
async def test_archive_task_restore_retains_source_and_account(readers, isolated):
    item = archive.ArchiveManager("wecom", readers[1].account_id)
    cfg = item.load_config()
    cfg.update(enabled=True, copy_media=False)
    item.save_config(cfg)
    registry.select("personal")
    restored = archive.ArchiveHub()
    try:
        await restored.start()
        active = restored.managers[("wecom", readers[1].account_id)]
        assert active._task and not active._task.done()
        assert active.reader.source == "wecom"
        assert active.reader.account_id == readers[1].account_id
        assert registry.config()["active"] == "personal"
    finally:
        await restored.stop()


@pytest.mark.asyncio
async def test_reader_rejects_external_address_before_token_or_network(isolated, monkeypatch):
    from app.reader_client import ReaderClient
    monkeypatch.setattr(isolated.settings, "reader_base", "https://example.com")
    with pytest.raises(ReaderError, match="回环"):
        await ReaderClient().request("GET", "/api/status")


@pytest.mark.asyncio
async def test_late_selection_cannot_overwrite_newer_choice(readers):
    from app.sources.api import Selection, selection
    await selection(Selection(client_id="window", source="wecom", chat="new", revision=9))
    await selection(Selection(client_id="window", source="personal", chat="old", revision=8))
    assert selections["window"] == ("wecom", "new", 9)


@pytest.mark.asyncio
async def test_media_result_cancelled_on_account_change(readers, monkeypatch):
    from app.reader_client import personal_reader
    async def changed(*args, **kwargs):
        readers[0].account_id = "changed-account"
        return 200, b"old-media", {}
    monkeypatch.setattr(personal_reader, "request", changed)
    with pytest.raises(ReaderError, match="账号"):
        await registry.fixed("personal").request("GET", "/api/avatar/user")


@pytest.mark.asyncio
async def test_stdio_transport_against_real_local_mcp_http(readers, isolated):
    import socket
    import sys
    import uvicorn
    from pathlib import Path
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(16)
    port = sock.getsockname()[1]
    isolated.settings.config_path.write_text(json.dumps({"bind_host": "127.0.0.1", "bind_port": port}))
    await mcp_api.set_access(mcp_api.Access(enabled=True, confirm=True))
    server = uvicorn.Server(uvicorn.Config(main.app, host="127.0.0.1", port=port, lifespan="off", log_level="warning"))
    task = asyncio.create_task(server.serve(sockets=[sock]))
    process = None
    try:
        import os
        env = dict(os.environ, WF_CONFIG=str(isolated.settings.config_path))
        script = Path(main.__file__).with_name("mcp_stdio.py")
        process = await asyncio.create_subprocess_exec(sys.executable, str(script), "--source", "wecom",
                    env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        requests = [
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2024-11-05"}},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
             "params": {"name": "get_messages", "arguments": {"chat": "shared-chat"}}},
        ]
        output, errors = await asyncio.wait_for(process.communicate(
            ("\n".join(json.dumps(x) for x in requests) + "\n").encode()), 10)
        assert process.returncode == 0 and not errors
        replies = [json.loads(line) for line in output.splitlines()]
        assert [r["id"] for r in replies] == [1, 2, 3]
        assert len(replies[1]["result"]["tools"]) == 6
        assert replies[2]["result"]["isError"] is False
        assert "wecom原文" in replies[2]["result"]["content"][0]["text"]
    finally:
        if process and process.returncode is None:
            process.kill()
            await process.wait()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        sock.close()
