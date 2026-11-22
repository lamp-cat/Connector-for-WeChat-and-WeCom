"""Settings API for agent-tool (MCP) integration. Token-gated (mounted behind require_auth).

- GET  /api/agents            which local agent tools are detected / registered
- POST /api/agents            {action: "register"|"unregister", tools: [...]} — backup first, non-destructive
- GET  /api/mcp-info          how to connect: stdio command, and the equivalent read-only HTTP endpoint
- POST /api/mcp-info/token    {action: "reveal"} → reader token, for the in-app copy button only (no-store)
Receive-only: the MCP server exposes read tools only; nothing here can send a WeChat message.
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Response
from pydantic import BaseModel, Field

from . import agent_register as ar
from .config_api import READER_BIN, READER_CFG_DIR
from .settings import settings
from .sources.registry import selected, config
from .wecom_agent_register import implementation as wecom_ar
from . import mcp_api

router = APIRouter()


def _bin() -> str:
    return str(READER_BIN)


def _cfg() -> str:
    return str(READER_CFG_DIR)


class AgentAction(BaseModel):
    action: str = Field(pattern="^(register|unregister)$")
    tools: Optional[List[str]] = None


@router.get("/api/agents")
async def agents_status():
    impl = wecom_ar if selected() == "wecom" else ar
    st = await asyncio.to_thread(impl.status, _bin(), _cfg())
    st["reader_bin_exists"] = True if selected() == "wecom" else READER_BIN.is_file()
    st["source"] = selected()
    st["mcp_enabled"] = config()["mcp"][selected()]
    return st


@router.post("/api/agents")
async def agents_action(body: AgentAction):
    source = selected()
    if source == "wecom" and body.action == "register" and not config()["mcp"]["wecom"]:
        raise HTTPException(409, "请先单独开启企业微信 MCP 授权")
    valid = {t["id"] for t in ar.tool_specs()}
    tools = [t for t in (body.tools or []) if t in valid] or None
    if body.tools and not tools:
        raise HTTPException(400, "unknown tool id")
    impl = wecom_ar if source == "wecom" else ar
    fn = impl.register if body.action == "register" else impl.unregister
    results = await asyncio.to_thread(fn, _bin(), _cfg(), tools)
    st = await asyncio.to_thread(impl.status, _bin(), _cfg())
    st["reader_bin_exists"] = True if source == "wecom" else READER_BIN.is_file()
    return {"ok": all(r.get("result") != "error" for r in results), "results": results, "status": st}


@router.get("/api/mcp-info")
async def mcp_info():
    if selected() == "wecom":
        entry = wecom_ar.server_entry(_bin(), _cfg())
        return {"source": "wecom", "server_name": "wecom-reader", "enabled": config()["mcp"]["wecom"], "receive_only": True,
                "stdio": {**entry, "note": "固定读取企业微信；凭据仅从本机 0600 文件读取。"},
                "http": {"url": f"http://127.0.0.1:{settings.bind_port}/mcp/wecom",
                         "auth": "Authorization: Bearer <企业微信 MCP 专用令牌>",
                         "token_file": str(mcp_api.token_file())},
                "tools": list(mcp_api.ROUTES)}
    return {
        "source": "personal", "server_name": "wechat-reader",
        "receive_only": True,
        "stdio": {"command": _bin(), "args": ["mcp"], "env": {"WECHAT_READER_CONFIG": _cfg()},
                  "note": "本机 agent 直接启动该命令即可，命令自己读取 0600 密钥文件，配置里不含任何令牌。"},
        "http": {"url": f"{settings.reader_base}/mcp", "auth": "Authorization: Bearer <读取器令牌>",
                 "token_file": str(settings.reader_token_file),
                 "note": "只监听 127.0.0.1。令牌 = 完整读权限，请勿外传。"},
        "tools": ["list_sessions", "search_contacts", "get_messages", "search_messages", "list_chat_members",
                  "get_new_messages", "get_image", "get_video", "get_voice", "sync_status"],
    }


class Reveal(BaseModel):
    action: str = Field(pattern="^reveal$")


@router.post("/api/mcp-info/token")
async def mcp_token(body: Reveal, response: Response):
    tok = mcp_api.credential() if selected() == "wecom" else settings.reader_token
    if not tok:
        raise HTTPException(404, "reader token not created yet")
    response.headers["Cache-Control"] = "no-store"
    return {"token": tok}


@router.put("/api/mcp/access")
async def mcp_access(body: mcp_api.Access):
    if selected() != "wecom":
        raise HTTPException(400, "个人微信保留已有读取器授权；此开关仅用于企业微信")
    return await mcp_api.set_access(body)
