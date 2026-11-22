"""Enterprise MCP over local HTTP; a dedicated credential grants only this source."""
from __future__ import annotations

import json
import secrets
import hmac
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel

from .sources import registry
from .reader_client import ReaderError
from .settings import settings

router = APIRouter()
sessions = {}
ROUTES = {"list_sessions": "/api/sessions", "search_contacts": "/api/contacts",
          "get_messages": "/api/messages", "search_messages": "/api/search",
          "get_new_messages": "/api/messages", "sync_status": "/api/status"}


def token_file():
    return settings.config_path.parent / "mcp" / "wecom-token"


def credential():
    try:
        return token_file().read_text().strip()
    except OSError:
        return ""


class Access(BaseModel):
    enabled: bool
    confirm: bool = False


async def set_access(body):
    if body.enabled and not body.confirm:
        raise HTTPException(400, "开启企业微信 MCP 需要单独确认")
    cfg = registry.config()
    cfg["mcp"]["wecom"] = body.enabled
    registry.save(cfg)
    sessions.clear()
    path = token_file()
    if body.enabled:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        from .archive import _atomic_write
        _atomic_write(path, secrets.token_urlsafe(32).encode())
    else:
        path.unlink(missing_ok=True)
    return {"enabled": body.enabled, "source": "wecom"}


def tools():
    common = {
        "chat": {"type": "string"}, "keyword": {"type": "string"}, "sender": {"type": "string"},
        "limit": {"type": "integer", "minimum": 1, "maximum": 1000},
        "before": {"type": "string", "description": "Opaque next_before cursor from this account and chat"},
        "after": {"type": "string", "description": "Opaque next_after cursor from this account and chat"},
        "since": {"type": "string"}, "until": {"type": "string"},
    }
    fields = {
        "list_sessions": ["keyword", "limit"], "search_contacts": ["keyword", "limit"],
        "get_messages": list(common), "search_messages": list(common),
        "get_new_messages": ["chat", "after", "before", "limit"], "sync_status": [],
    }
    descriptions = {
        "list_sessions": "列出企业微信快照会话", "search_contacts": "查询已解析联系人",
        "get_messages": "读取选定会话消息并分页", "search_messages": "搜索快照中的可读消息",
        "get_new_messages": "读取游标之后的消息；has_more 时保留原 after，用 next_before 补页，完成后再推进 after",
        "sync_status": "查看账号、快照状态和能力限制",
    }
    return [{"name": name, "description": descriptions[name],
             "inputSchema": {"type": "object", "properties": {k: common[k] for k in fields[name]},
                             "required": ["chat"] if name in ("get_messages", "get_new_messages") else [],
                             "additionalProperties": False},
             "annotations": {"readOnlyHint": True, "destructiveHint": False}}
            for name in ROUTES]


async def dispatch(payload, session_id=None):
    method, rpc_id = payload.get("method"), payload.get("id")
    result = None
    if method == "initialize":
        if len(sessions) >= 500:
            sessions.clear()
        session_id = secrets.token_urlsafe(24)
        sessions[session_id] = None
        result = {"protocolVersion": (payload.get("params") or {}).get("protocolVersion", "2024-11-05"),
                  "capabilities": {"tools": {"listChanged": False}},
                  "serverInfo": {"name": "wecom-reader", "version": "2.0.0"}}
    elif method and method.startswith("notifications/"):
        return None, session_id
    elif method == "ping":
        result = {}
    elif method in ("tools/list", "tools/call"):
        if session_id not in sessions:
            raise HTTPException(404, "MCP session expired; initialize again")
        if method == "tools/list":
            result = {"tools": tools()}
        else:
            params = payload.get("params") or {}
            name = params.get("name")
            arguments = params.get("arguments") or {}
            if name not in ROUTES:
                return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32602, "message": "Unknown read tool"}}, session_id
            try:
                if sessions[session_id] is None:
                    status = await registry.fixed("wecom").get_json("/api/status")
                    account = registry.account(status)
                    if not account:
                        raise ReaderError(503, "企业微信当前账号未就绪")
                    sessions[session_id] = account
                if name == "get_messages" and not arguments.get("chat"):
                    raise ReaderError(400, "chat is required")
                if name == "get_new_messages" and not arguments.get("after"):
                    raise ReaderError(400, "请先用 get_messages 取得 next_after 游标")
                if name == "search_messages" and not (arguments.get("keyword") or arguments.get("sender")):
                    raise ReaderError(400, "keyword or sender is required")
                data = await registry.fixed("wecom", sessions[session_id]).get_json(ROUTES[name], arguments)
                result = {"content": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}], "isError": False}
            except ReaderError as exc:
                result = {"content": [{"type": "text", "text": exc.detail}], "isError": True}
    else:
        return {"jsonrpc": "2.0", "id": rpc_id, "error": {"code": -32601, "message": "Method not found"}}, session_id
    return {"jsonrpc": "2.0", "id": rpc_id, "result": result}, session_id


@router.api_route("/mcp/wecom", methods=["POST", "GET", "DELETE"])
async def mcp(request: Request):
    expected = credential()
    provided = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if not registry.config()["mcp"]["wecom"] or not expected or not hmac.compare_digest(expected, provided):
        raise HTTPException(401, "企业微信 MCP 未授权")
    session_id = request.headers.get("mcp-session-id")
    if request.method == "DELETE":
        sessions.pop(session_id, None)
        return Response(status_code=204)
    if request.method == "GET":
        return Response(status_code=405, headers={"Allow": "POST, DELETE"})
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            raise ValueError()
    except Exception:
        raise HTTPException(400, "Expected JSON-RPC object")
    result, session_id = await dispatch(payload, session_id)
    headers = {"Mcp-Session-Id": session_id} if session_id else {}
    if result is None:
        return Response(status_code=202, headers=headers)
    return JSONResponse(result, headers=headers)
