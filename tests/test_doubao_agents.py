import json

import httpx
import pytest

from app import agent_register as personal, main
from app.wecom_agent_register import implementation as enterprise


@pytest.mark.parametrize("implementation", [personal, enterprise])
def test_doubao_is_detected_without_claiming_registered(isolated, monkeypatch, implementation):
    monkeypatch.setenv("WD_AGENT_HOME", str(isolated.home))
    support = isolated.home / "Library/Application Support/DoubaoWork"
    support.mkdir(parents=True)
    row = next(t for t in implementation.status("/fixture/reader")["tools"] if t["id"] == "doubao-work")
    assert row["label"] == "豆包工作"
    assert row["detected"] is True and row["manual"] is True
    assert row["registered"] is False and row["managed"] is False


@pytest.mark.parametrize("implementation", [personal, enterprise])
def test_doubao_register_never_creates_an_unverified_config(isolated, monkeypatch, implementation):
    monkeypatch.setenv("WD_AGENT_HOME", str(isolated.home))
    support = isolated.home / "Library/Application Support/DoubaoWork"
    support.mkdir(parents=True)
    existing = support / "public_config.json"
    existing.write_text('{"existing":"keep"}')
    before = existing.read_bytes()
    result = implementation.register("/fixture/reader", tools=["doubao-work"])
    assert result[0]["result"] == "manual"
    assert list(support.iterdir()) == [existing] and existing.read_bytes() == before
    assert implementation.load_state()["tools"].get("doubao-work") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("source,server", [("personal", "wechat-reader"), ("wecom", "wecom-reader")])
async def test_doubao_connection_info_keeps_source_identity(readers, source, server):
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test",
                                headers={"Authorization": "Bearer test-gateway-credential"}) as client:
        status = (await client.get("/api/agents?source=" + source)).json()
        row = next(t for t in status["tools"] if t["id"] == "doubao-work")
        assert row["manual"] is True and row["registered"] is False
        info = (await client.get("/api/mcp-info?source=" + source)).json()
        assert info["source"] == source and info["server_name"] == server
        assert "token" not in info.get("stdio", {}).get("env", {})
