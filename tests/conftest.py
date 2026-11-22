import json
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wechat-frontend"))


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    from app.settings import settings
    from app import archive, mcp_api
    from app.sources import registry
    from app.sources.api import selections
    config = tmp_path / ".config/wechat-frontend"
    config.mkdir(parents=True)
    monkeypatch.setattr(settings, "config_path", config / "config.json")
    for name in ("gateway_token_file", "reader_token_file", "ai_config_file", "ui_config_file"):
        monkeypatch.setattr(settings, name, config / name)
    settings.gateway_token_file.write_text("test-gateway-credential")
    settings.reader_token_file.write_text("test-reader-credential")
    monkeypatch.setattr(archive, "HOME", tmp_path)
    monkeypatch.setattr(archive, "CONFIG_FILE", config / "archive-config.json")
    monkeypatch.setattr(archive, "DEFAULT_DIR", tmp_path / "MessageArchive")
    monkeypatch.setitem(archive.DEFAULTS, "archive_dir", str(tmp_path / "MessageArchive"))
    monkeypatch.setattr(registry, "revision", 0)
    registry.current_source.set(None)
    registry.save({"active": "personal", "mcp": {"personal": True, "wecom": False}})
    selections.clear()
    mcp_api.sessions.clear()
    return SimpleNamespace(home=tmp_path, config=config, settings=settings)


class MemoryReader:
    def __init__(self, source):
        self.source = source
        self.account_id = source + "-account-A"
        self.calls = []

    async def start(self):
        pass

    async def stop(self):
        pass

    async def get_json(self, path, params=None):
        from app.reader_client import ReaderError
        params = params or {}
        self.calls.append((path, dict(params)))
        if params.get("_account") and params["_account"] != self.account_id:
            raise ReaderError(409, "账号已变化")
        if path == "/api/status":
            return {"self": self.account_id, "ready": True, "account_id": self.account_id}
        if path == "/api/sessions":
            return {"sessions": [{"username": "shared-chat", "name": self.source + "会话", "kind": "friend"}]}
        if path == "/api/messages":
            return {"messages": [{"local_id": 1, "seq": 1, "sender": "user",
                                  "text": self.source + "原文", "time": "2026-11-22T10:00:00+08:00"}],
                    "has_more": False, "next_after": "cursor"}
        return {"contacts": []}


@pytest.fixture
def readers(isolated, monkeypatch):
    from app.reader_client import personal_reader
    from app.sources.wecom import wecom
    personal, enterprise = MemoryReader("personal"), MemoryReader("wecom")
    monkeypatch.setattr(personal_reader, "get_json", personal.get_json)
    monkeypatch.setattr(wecom, "get_json", enterprise.get_json)
    monkeypatch.setattr(wecom, "start", enterprise.start)
    monkeypatch.setattr(wecom, "stop", enterprise.stop)
    return personal, enterprise


@pytest.fixture
def snapshot_engine(tmp_path):
    from reference_wecom.demo_fixture import prepare
    folder = prepare(tmp_path)
    return SimpleNamespace(account="account-A", current=folder, chosen=None, updated=1,
                           signature="fixture-1", guard=threading.RLock())
