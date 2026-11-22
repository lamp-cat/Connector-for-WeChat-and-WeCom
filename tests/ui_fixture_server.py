"""Isolated UI acceptance service. Synthetic chats only; no client/key/launchd access."""
import asyncio
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "wechat-frontend"))
HOME = Path(os.environ.get("WF_TEST_HOME") or tempfile.mkdtemp(prefix="chat-companion-ui-"))
os.environ["WF_CONFIG"] = str(HOME / ".config/config.json")
os.environ["WD_AGENT_HOME"] = str(HOME)
from app import archive, main, sidebar, suggest, config_api
from app.settings import settings
from app.reader_client import personal_reader
from app.sources.wecom import wecom
from app.sources.wecom_engine.demo_fixture import prepare
from app.sources.wecom_engine.query import query

settings.config_path.parent.mkdir(parents=True, exist_ok=True)
for name in ("gateway_token_file", "reader_token_file", "ai_config_file", "ui_config_file"):
    setattr(settings, name, HOME / name)
settings.gateway_token_file.write_text("ui-fixture-login")
settings.reader_token_file.write_text("ui-fixture-reader")
if not settings.ai_config_file.exists():
    settings.ai_config_file.write_text(json.dumps({"enabled": True, "api_key": "fixture-only-key", "model": "fixture-model",
                                                  "base_url": "http://127.0.0.1:1/v1"}))
for path in (settings.gateway_token_file, settings.reader_token_file, settings.ai_config_file):
    path.chmod(0o600)
settings.config_path.write_text(json.dumps({
    "bind_host": "127.0.0.1", "bind_port": 18088,
    **{name: str(getattr(settings, name)) for name in (
        "gateway_token_file", "reader_token_file", "ai_config_file", "ui_config_file")}}))
archive.HOME = HOME
archive.CONFIG_FILE = settings.config_path.parent / "archive-config.json"
archive.DEFAULTS["archive_dir"] = str(HOME / "MessageArchive")
config_api.READER_CFG_DIR = HOME / "personal-config"
config_api.READER_CFG_DIR.mkdir(exist_ok=True)
config_api.READER_BIN = HOME / "fixture-reader-not-executable"
config_api.READER_ROOT = HOME
config_api.WECHAT_APP = HOME / "no-client"

folder = HOME / "demo-data"
if not folder.exists():
    folder = prepare(HOME)
enterprise = SimpleNamespace(account="synthetic-enterprise-A", current=folder, updated=1,
                             signature="fixture", chosen=None, guard=threading.RLock())
personal = SimpleNamespace(account="synthetic-personal-A", current=folder, updated=1,
                           signature="fixture", chosen=None, guard=threading.RLock())

async def noop(*args, **kwargs):
    pass

async def personal_query(path, params=None):
    data = query(personal, path, params or {})
    if path == "/api/status":
        data["self"] = personal.account
    if "sessions" in data:
        for s in data["sessions"]:
            s["name"] = "个人微信 · " + s["name"]
            s["kind"] = "friend"
    for m in data.get("messages", []):
        m["text"] = "个人微信原文：" + m["text"]
    return data

async def enterprise_query(path, params=None):
    return query(enterprise, path, params or {})

async def personal_active(bound, top):
    data = query(personal, "/api/active-chat", {"top": top})
    if data["active"]:
        data["active"]["name"] = "个人微信 · " + data["active"]["name"]
    return data

async def completion(ai, messages, **kwargs):
    await asyncio.sleep(0.3)
    if "overview" in messages[0]["content"]:
        content = json.dumps({"overview": "合成会话总结：确认接口联调与明日验收。",
                           "topics": ["接口联调"], "open_threads": ["确认验收时间"],
                           "commitments": [], "tone": "协作", "reply_tips": ["说明下一步安排"]})
    else:
        content = json.dumps({"suggestions": ["收到，我会确认联调进度后同步。"]})
    return {"content": content, "model": ai.get("model", "fixture-model")}

async def media_request(*args, **kwargs):
    return 404, b"Fixture has no media", {"content-type": "text/plain"}

personal_reader.get_json = personal_query
personal_reader.start = noop
personal_reader.stop = noop
personal_reader.request = media_request
wecom.get_json = enterprise_query
wecom.start = noop
wecom.stop = noop
wecom.account_id = enterprise.account
wecom.state = {"code": "connected", "title": "合成企业微信已连接"}
main.active_chat = personal_active
sidebar.chat_completion = completion
suggest.chat_completion = completion

if __name__ == "__main__":
    import uvicorn
    print("FIXTURE_HOME=" + str(HOME), flush=True)
    uvicorn.run(main.app, host="127.0.0.1", port=18088, log_level="warning")
