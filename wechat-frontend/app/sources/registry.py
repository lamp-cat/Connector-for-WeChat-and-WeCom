from __future__ import annotations

import contextvars
import hashlib
import json
import os
import tempfile
from pathlib import Path

from ..settings import settings

SOURCES = {"personal": "个人微信", "wecom": "企业微信"}
current_source = contextvars.ContextVar("chat_source", default=None)
revision = 0


def config_file():
    return settings.config_path.parent / "sources.json"


def config():
    try:
        data = json.loads(config_file().read_text())
    except (OSError, ValueError):
        data = {}
    return {"active": data.get("active") if data.get("active") in SOURCES else "personal",
            "mcp": {"personal": True, "wecom": bool((data.get("mcp") or {}).get("wecom", False))}}


def save(data):
    path = config_file()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def selected():
    return current_source.get() or config()["active"]


def select(source):
    global revision
    if source not in SOURCES:
        raise ValueError("未知消息来源")
    cfg = config()
    if source != cfg["active"]:
        cfg["active"] = source
        revision += 1
        save(cfg)
    return cfg


def account(status):
    value = status.get("account_id") or status.get("self")
    if isinstance(value, dict):
        value = value.get("wxid") or value.get("username") or value.get("id")
    return str(value) if value else None


def fingerprint():
    return hashlib.sha256(json.dumps(settings.load_ai(), sort_keys=True).encode()).hexdigest()


class FixedReader:
    def __init__(self, source, account_id=None):
        if source not in SOURCES:
            raise ValueError("未知消息来源")
        self.source, self.account_id = source, account_id

    def adapter(self):
        if self.source == "wecom":
            from .wecom import wecom
            return wecom
        from ..reader_client import personal_reader
        return personal_reader

    async def get_json(self, path, params=None):
        from ..reader_client import ReaderError
        params = dict(params or {})
        request_account = self.account_id
        if not request_account and path != "/api/status":
            request_account = account(await self.adapter().get_json("/api/status"))
        if self.account_id and self.source == "wecom":
            params["_account"] = self.account_id
        if self.account_id and self.source == "personal":
            st = await self.adapter().get_json("/api/status")
            if account(st) != self.account_id:
                raise ReaderError(409, "个人微信账号已变化，已取消本次读取")
        if request_account and self.source == "wecom":
            params["_account"] = request_account
        data = await self.adapter().get_json(path, params)
        st = data if path == "/api/status" else await self.adapter().get_json("/api/status")
        final_account = account(st)
        if request_account and final_account != request_account:
            raise ReaderError(409, "消息账号已变化，已取消本次读取")
        if isinstance(data, dict):
            data = {**data, "source": self.source, "account_id": final_account}
        if self.account_id:
            st = data if path == "/api/status" else await self.adapter().get_json("/api/status")
            if account(st) != self.account_id:
                raise ReaderError(409, "消息账号已变化，已取消本次读取")
        return data

    async def request(self, *args, **kwargs):
        if not args or args[0] != "GET":
            from ..reader_client import ReaderError
            raise ReaderError(405, "只支持读取")
        from ..reader_client import ReaderError
        expected = self.account_id or account(await self.adapter().get_json("/api/status"))
        if self.account_id and expected != account(await self.adapter().get_json("/api/status")):
            raise ReaderError(409, "消息账号已变化，已取消本次读取")
        result = await self.adapter().request(*args, **kwargs)
        if expected and expected != account(await self.adapter().get_json("/api/status")):
            raise ReaderError(409, "消息账号已变化，已取消本次读取")
        return result


def fixed(source=None, account_id=None):
    return FixedReader(source or selected(), account_id)
