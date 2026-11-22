import asyncio
import base64
import json
import os
import signal
import sqlite3
import subprocess
import sys
import time

import pytest

from app.sources.wecom import WeComReader
from go_wecom import BINARY, query
from reference_wecom.query import query as reference_query


def normalize(value):
    if isinstance(value, dict):
        return {key: normalize(item) for key, item in value.items() if key != "snapshot_revision"}
    if isinstance(value, list):
        return [normalize(item) for item in value]
    return value


@pytest.mark.parametrize("path,params", [
    ("/api/sessions", {"keyword": "演示"}),
    ("/api/contacts", {"keyword": "陈"}),
    ("/api/active-chat", {"top": 2}),
    ("/api/messages", {"chat": "demo-project", "limit": 13}),
    ("/api/search", {"keyword": "明天", "limit": 9}),
    ("/api/messages", {"chat": "demo-project", "sender": "u1", "until": "2030-01-01", "limit": 7}),
])
def test_go_matches_former_python_contract(snapshot_engine, path, params):
    old = reference_query(snapshot_engine, path, params)
    new = query(snapshot_engine, path, params)
    # Base64 JSON permits different formatting of an identical numeric timestamp.
    for item in (old, new):
        for message in item.get("messages", []):
            message["cursor"] = json.loads(base64.urlsafe_b64decode(message["cursor"] + "==="))
        for key in ("next_before", "next_after"):
            if item.get(key):
                item[key] = json.loads(base64.urlsafe_b64decode(item[key] + "==="))
    assert normalize(new) == normalize(old)


def test_go_accepts_previous_cursor_and_rejects_wrong_account(snapshot_engine):
    old = reference_query(snapshot_engine, "/api/messages", {"chat": "demo-project", "limit": 3})
    params = {"chat": "demo-project", "before": old["next_before"], "limit": 3}
    assert [m["message_id"] for m in query(snapshot_engine, "/api/messages", params)["messages"]] == [
        m["message_id"] for m in reference_query(snapshot_engine, "/api/messages", params)["messages"]]
    with pytest.raises(ValueError, match="账号"):
        query(snapshot_engine, "/api/messages", dict(params, _account="another-account"))


def test_go_merges_independent_sqlite_wal_without_source_changes(snapshot_engine):
    from hashlib import sha256
    folder = snapshot_engine.current
    db = sqlite3.connect(folder / "message.db")
    try:
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.execute("INSERT INTO message_table VALUES('demo-project','wal-python','u1',2000000000,'独立 SQLite WAL 原文')")
        db.commit()
        before = {p.name: (p.stat().st_mtime_ns, sha256(p.read_bytes()).hexdigest()) for p in folder.iterdir()}
        page = query(snapshot_engine, "/api/search", {"keyword": "独立 SQLite WAL 原文"})
        assert page["messages"][0]["server_id"] == "wal-python"
        after = {p.name: (p.stat().st_mtime_ns, sha256(p.read_bytes()).hexdigest()) for p in folder.iterdir()}
        assert before == after
    finally:
        db.close()

@pytest.mark.skipif(sys.platform != "darwin", reason="macOS native spawn primitive")
def test_native_copy_starts_suspended_with_correct_home(tmp_path):
    executable = tmp_path / "Runtime/企业微信.app/Contents/MacOS/企业微信"
    executable.parent.mkdir(parents=True)
    container = tmp_path / "container"
    container.mkdir()
    output = tmp_path / "home.txt"
    source = tmp_path / "fixture.c"
    source.write_text(
        '#include <stdio.h>\n#include <stdlib.h>\n'
        'int main(void) { FILE *f=fopen(getenv("WECOM_TEST_OUTPUT"),"w"); '
        'if(!f) return 1; fputs(getenv("CFFIXED_USER_HOME"),f); fclose(f); return 0; }\n')
    subprocess.run(["/usr/bin/cc", str(source), "-o", str(executable)],
                   capture_output=True, check=True, timeout=20)
    helper = BINARY.parents[2] / "setup/wecom-runtime"
    result = subprocess.run([str(helper)],
        input=json.dumps({"executable": str(executable), "container": str(container)}),
        env=dict(os.environ, WECOM_TEST_OUTPUT=str(output)),
        capture_output=True, text=True, check=True, timeout=5)
    pid = json.loads(result.stdout)["pid"]
    try:
        state = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="],
                               capture_output=True, text=True, check=True, timeout=5).stdout
        assert "T" in state and not output.exists()
        os.kill(pid, signal.SIGCONT)
        deadline = time.monotonic() + 3
        while not output.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert output.read_text() == str(container)
    finally:
        try:
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass


@pytest.mark.asyncio
async def test_python_adapter_launches_go_and_restarts_with_large_text(snapshot_engine, monkeypatch, tmp_path):
    monkeypatch.setenv("WF_WECOM_ROOT", str(tmp_path / "state"))
    with sqlite3.connect(snapshot_engine.current / "message.db") as db:
        db.execute("INSERT INTO message_table VALUES('large','1','u1',2000000000,?)", ("锁定原文" * 50000,))
    real_create = asyncio.create_subprocess_exec
    launched = []

    async def create(*args, **kwargs):
        # Exercise the production adapter and real Go binary, restricting it to synthetic data.
        assert args[0] == str(BINARY) and "-m" not in args
        process = await real_create(*args, "--snapshot", str(snapshot_engine.current),
                                    "--account", snapshot_engine.account, **kwargs)
        launched.append(process)
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    reader = WeComReader()
    restored = asyncio.Event()

    async def keychain(*args, **kwargs):
        restored.set()
        return {}

    reader._keychain = keychain
    try:
        data = await reader.get_json("/api/messages", {"chat": "large"})
        assert data["messages"][0]["text"] == "锁定原文" * 50000
        await asyncio.wait_for(restored.wait(), 2)
        await reader.stop()
        assert launched[0].returncode == 0 and not reader.pending and not reader.auxiliary
        assert (await reader.get_json("/api/status"))["ready"]
        assert len(launched) == 2
    finally:
        await reader.stop()
