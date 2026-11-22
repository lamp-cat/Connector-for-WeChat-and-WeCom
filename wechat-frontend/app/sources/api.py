from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import registry
from .wecom import wecom

router = APIRouter()
selections = {}


@router.get("/api/sources")
async def sources():
    return {**registry.config(), "revision": registry.revision, "labels": registry.SOURCES}


class SourceChoice(BaseModel):
    source: str = Field(pattern="^(personal|wecom)$")


@router.put("/api/sources")
async def choose(body: SourceChoice):
    cfg = registry.select(body.source)
    if body.source == "wecom":
        await wecom.start()
    return {**cfg, "revision": registry.revision, "labels": registry.SOURCES}


class Selection(BaseModel):
    client_id: str = Field(min_length=1, max_length=100)
    source: str = Field(pattern="^(personal|wecom)$")
    chat: str = Field(max_length=300)
    revision: int = Field(ge=0)


@router.put("/api/selection")
async def selection(body: Selection):
    if len(selections) > 1000:
        selections.clear()
    previous = selections.get(body.client_id)
    if previous and body.revision < previous[2]:
        return {"ok": False, "stale": True}
    selections[body.client_id] = (body.source, body.chat, body.revision)
    return {"ok": True}


@router.get("/api/wecom/connection")
async def connection():
    await wecom.start()
    return {"source": "wecom", "account_id": wecom.account_id, "status": wecom.state,
            "runtime_ready": wecom.runtime_ready, "managed_pid": wecom.managed_pid,
            "notices": wecom.notices[-5:]}


class NativeCommand(BaseModel):
    op: str = Field(pattern="^(prepare|start_runtime|restored|retry)$")
    confirmed: bool = False


@router.post("/api/wecom/connection")
async def command(body: NativeCommand):
    if body.op in ("prepare", "start_runtime", "restored") and not body.confirmed:
        raise HTTPException(400, "请通过原生连接引导确认客户端操作")
    if body.op == "retry":
        await wecom.stop()
        await wecom.start()
    else:
        if body.op == "prepare":
            wecom.runtime_ready = False
        if body.op == "start_runtime" and not wecom.runtime_ready:
            raise HTTPException(409, "请先准备临时副本")
        await wecom.send({"op": body.op})
    return {"ok": True}
