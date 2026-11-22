import importlib.util
import json
import sqlite3
import struct
from pathlib import Path
from types import SimpleNamespace

import pytest

from . import engine as e
KEY = bytes.fromhex("102030405060708090a0b0c0d0e0f001")


def make_databases(folder):
    folder.mkdir()
    with sqlite3.connect(folder / "message.db") as c:
        c.execute("PRAGMA page_size=4096")
        c.execute("CREATE TABLE message_table(conversation_id TEXT,message_id TEXT,send_time INTEGER,content TEXT,sender_id TEXT)")
        c.execute("INSERT INTO message_table VALUES ('S1','10',1795200000000,'请明天确认交付时间','U1')")
        c.execute("INSERT INTO message_table VALUES ('S2','11',1795199999000,'旧会话消息','U2')")
    with sqlite3.connect(folder / "session.db") as c:
        c.execute("CREATE TABLE conversation_table(id TEXT,name TEXT,last_message_time INTEGER)")
        c.executemany("INSERT INTO conversation_table VALUES (?,?,?)",
                      [("S1", "当前讨论", 1795200000000), ("S2", "旧讨论", 1795199999000)])
    with sqlite3.connect(folder / "user.db") as c:
        c.execute("CREATE TABLE user_table(id TEXT,name TEXT)")
        c.execute("INSERT INTO user_table VALUES ('U1','同事')")


def encode_page(plain, number):
    if number > 1:
        return e.aes_for(KEY, number).encrypt(plain)
    encrypted = e.aes_for(KEY, 1).encrypt(plain[16:])
    return b"fixture!" + encrypted[:8] + plain[16:24] + encrypted[8:]


def make_encrypted(folder):
    for name in e.DB_NAMES:
        p = folder / name
        plain = p.read_bytes()
        p.write_bytes(b"".join(encode_page(plain[i:i + 4096], i // 4096 + 1)
                               for i in range(0, len(plain), 4096)))


def wal(frames, size=4096, salts=b"12345678"):
    header = struct.pack(">IIII", 0x377F0682, 3007000, size, 0) + salts
    state = e.rolling_checksum(header)
    result = header + struct.pack(">II", *state)
    for number, commit, body in frames:
        head = struct.pack(">II", number, commit) + salts
        state = e.rolling_checksum(head[:8] + body, state)
        result += head + struct.pack(">II", *state) + body
    return result


def test_real_sqlite_plain_snapshot_is_readonly_and_selects_recent(tmp_path):
    source, dest = tmp_path / "source", tmp_path / "dest"
    make_databases(source)
    before = {n: (source / n).read_bytes() for n in e.DB_NAMES}
    e.snapshot(source, {}, dest)
    context = e.build_context(dest)
    assert context["selected"]["id"] == "S1"
    assert context["messages"][0]["sender"] == "同事"
    assert "明天" in context["todos"][0]
    assert context["context_mode"] == "recent"
    assert e.build_context(dest, "S2")["selected"]["id"] == "S2"
    assert before == {n: (source / n).read_bytes() for n in e.DB_NAMES}
    with e.readonly(dest / "message.db") as db:
        with pytest.raises(sqlite3.OperationalError):
            db.execute("DELETE FROM message_table")


def test_encryption_requires_correct_key_and_full_sqlite_integrity(tmp_path):
    source = tmp_path / "source"
    make_databases(source)
    make_encrypted(source)
    with pytest.raises(ValueError, match="校验"):
        e.snapshot(source, {n: "ff" * 16 for n in e.DB_NAMES}, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    e.snapshot(source, {n: KEY.hex() for n in e.DB_NAMES}, tmp_path / "ok")
    assert "请明天" in e.build_context(tmp_path / "ok")["summary"]


def test_wal_includes_only_committed_transaction():
    a, b, c = (bytes([v]) * 4096 for v in (1, 2, 3))
    journal = wal([(1, 1, b), (1, 0, c)])
    assert e.apply_wal(a, journal, b"", 4096, False) == b
    assert e.apply_wal(a, wal([(1, 0, c)]), b"", 4096, False) == a


def test_wal_rejects_corruption_and_supports_encrypted_pages(tmp_path):
    source = tmp_path / "source"
    make_databases(source)
    raw = (source / "message.db").read_bytes()[:4096]
    journal = wal([(1, 1, encode_page(raw, 1))])
    assert e.apply_wal(raw, journal, KEY, 4096, True) == raw
    corrupted = bytearray(journal)
    corrupted[-1] ^= 1
    with pytest.raises(ValueError, match="校验"):
        e.apply_wal(raw, corrupted, KEY, 4096, True)
    with pytest.raises(ValueError):
        e.apply_wal(raw, journal, KEY, 1024, True)


def test_wal_truncation_does_not_resurrect_removed_pages():
    block = lambda x: bytes([x]) * 4096
    # Page 2 from the first transaction must not return after truncate then extend.
    journal = wal([(2, 2, block(2)), (1, 1, block(3)), (2, 2, block(4))])
    assert e.apply_wal(block(0) * 2, journal, b"", 4096, False) == block(3) + block(4)


def test_wal_restart_ignores_previous_generation_tail():
    base, current, stale = (bytes([v]) * 4096 for v in (1, 2, 3))
    journal = wal([(1, 1, current)]) + wal([(1, 1, stale)], salts=b"87654321")[32:]
    assert e.apply_wal(base, journal, b"", 4096, False) == current


def test_wal_restart_without_current_commit_keeps_checkpoint():
    base, uncommitted, stale = (bytes([v]) * 4096 for v in (1, 2, 3))
    previous = wal([(1, 1, stale)], salts=b"87654321")[32:]
    assert e.apply_wal(base, wal([]) + previous, b"", 4096, False) == base
    assert e.apply_wal(base, wal([(1, 0, uncommitted)]) + previous, b"", 4096, False) == base


def test_active_account_discovery_ignores_newer_historic_account(tmp_path, monkeypatch):
    container = tmp_path / "Container"
    data = container / "Library/Application Support/WXWork/Data"
    data.mkdir(parents=True)
    active = data / "111"
    old = data / "222"
    make_databases(active)
    make_databases(old)
    monkeypatch.setattr(e, "CONTAINER", container)
    monkeypatch.setattr(e, "open_paths", lambda pid: [str(active / "Avator/a.db")])
    assert e.discover([7])[0] == active
    monkeypatch.setattr(e, "open_paths", lambda pid: [])
    assert e.discover([7]) == (None, 0)


def test_account_switch_and_permission_error_clear_stale_context(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "ROOT", tmp_path / "state")
    events = []
    monkeypatch.setattr(e, "emit", lambda kind, **payload: events.append((kind, payload)))
    reader = e.Engine()
    reader.current = tmp_path / "old"
    reader.last_context = {"summary": "旧账号"}
    monkeypatch.setattr(e, "client_processes", lambda: [])
    reader.sync()
    assert reader.current is None and reader.last_context is None
    assert events[-1][1]["code"] == "client_missing"
    reader.close()


def test_capture_does_not_accept_candidate_for_wrong_account(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "ROOT", tmp_path / "state")
    monkeypatch.setattr(e, "emit", lambda *a, **k: None)
    source = tmp_path / "source"
    make_databases(source)
    make_encrypted(source)
    reader = e.Engine()
    reader.source = source
    reader.candidates = ["ff" * 16]
    assert reader.try_candidates() == {}
    reader.candidates.append(KEY.hex())
    assert reader.try_candidates() == {n: KEY.hex() for n in e.DB_NAMES}
    reader.close()


def test_unrecognized_message_blob_is_not_fabricated_text():
    assert e.readable_content(b"\xff\x01") == "[暂未解析的二进制消息]"
    assert e.readable_content('{"text":"锁定原文"}') == "锁定原文"
    assert e.readable_content("x\x01y") == "[暂未解析的结构化消息]"


def test_observed_text_protobuf_preserves_text_and_rejects_truncation():
    # field 1 run: {field 1 kind=0, field 2 payload: {field 1 text="你好"}}
    body = b"\x0a\x0c\x08\x00\x12\x08\x0a\x06" + "你好".encode()
    assert e.readable_content(body, 2) == "你好"
    assert e.readable_content(body + body, 2) == "你好你好"
    assert e.readable_content(body[:-1], 2) == "[暂未解析的结构化消息]"
    assert e.readable_content(b"\x0a\x04\x08\x03\x12\x00", 2) == "[暂未解析的消息片段]"
    with pytest.raises(ValueError, match="过大"):
        e.protobuf_fields(b"\0" * (2 * 1024**2 + 1))


def test_recent_context_uses_message_activity_if_session_timestamps_are_stale(tmp_path):
    make_databases(tmp_path / "source")
    with sqlite3.connect(tmp_path / "source/session.db") as db:
        db.execute("UPDATE conversation_table SET last_message_time=0")
    assert e.build_context(tmp_path / "source")["selected"]["id"] == "S1"


def test_keychain_restore_finishes_before_attempting_process_attach(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "ROOT", tmp_path / "state")
    events = []
    monkeypatch.setattr(e, "emit", lambda kind, **payload: events.append((kind, payload)))
    source = tmp_path / "source"
    make_databases(source)
    make_encrypted(source)
    monkeypatch.setattr(e, "client_processes", lambda: [123])
    monkeypatch.setattr(e, "discover", lambda pids: (source, 1))
    reader = e.Engine()
    reader.client_checked = 123
    def attach_should_not_run(*args, **kwargs):
        raise AssertionError("cached keys should be tried before attach")
    reader.attach = attach_should_not_run
    try:
        reader.sync()
        assert events[-1][1]["code"] == "restoring"
        assert reader.attempted_pid is None
        reader.action({"op": "keys", "account": reader.account, "keys": {n: KEY.hex() for n in e.DB_NAMES}})
        reader.sync()
        assert events[-1][1]["code"] == "connected"
        assert reader.attempted_pid is None
    finally:
        reader.close()


def test_start_runtime_never_launches_if_official_client_still_running(tmp_path, monkeypatch):
    monkeypatch.setattr(e, "ROOT", tmp_path / "state")
    events = []
    monkeypatch.setattr(e, "emit", lambda kind, **payload: events.append((kind, payload)))
    monkeypatch.setattr(e, "client_processes", lambda: [123])
    reader = e.Engine()
    reader.start_runtime()
    assert reader.managed_pid is None
    assert events[-1][1]["code"] == "runtime_error"
    assert "尚未正常退出" in events[-1][1]["detail"]
    reader.close()


def test_snapshot_merges_real_sqlite_wal_without_changing_any_source_file(tmp_path):
    import hashlib
    source, dest = tmp_path / "source", tmp_path / "copy"
    make_databases(source)
    connection = sqlite3.connect(source / "message.db")
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO message_table VALUES('S1','20',1795200001000,'WAL里的已提交原文','U1')")
        connection.commit()
        paths = list(source.iterdir())
        before = {p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths}
        e.snapshot(source, {}, dest)
        after = {p.name: (p.stat().st_mtime_ns, hashlib.sha256(p.read_bytes()).hexdigest()) for p in paths}
        assert before == after
        with e.readonly(dest / "message.db") as db:
            assert db.execute("SELECT content FROM message_table WHERE message_id='20'").fetchone()[0] == "WAL里的已提交原文"
            assert db.execute("PRAGMA quick_check").fetchone()[0] == "ok"
    finally:
        connection.close()
