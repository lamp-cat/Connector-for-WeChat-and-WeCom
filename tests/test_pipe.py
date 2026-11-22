import asyncio
import sys

import pytest

from app.sources.wecom import WeComReader


WORKER = """
import json,sys
print(json.dumps({"event":"account","account":"fixture-account"}), flush=True)
for line in sys.stdin:
    command=json.loads(line)
    if command.get("op")=="quit": break
    if command.get("op")=="query":
        data={"account_id":"fixture-account","text":"长原文" * 50000}
        print(json.dumps({"event":"response","request_id":command["request_id"],"ok":True,"data":data}), flush=True)
"""


@pytest.mark.asyncio
async def test_large_pipe_response_and_cancel_keychain_on_stop(monkeypatch):
    real_create = asyncio.create_subprocess_exec
    launched = []
    async def create(*args, **kwargs):
        process = await real_create(sys.executable, "-c", WORKER, **kwargs)
        launched.append(process)
        return process
    monkeypatch.setattr(asyncio, "create_subprocess_exec", create)
    reader = WeComReader()
    keychain_started = asyncio.Event()
    async def blocked_keychain(*args, **kwargs):
        keychain_started.set()
        await asyncio.Event().wait()
    reader._keychain = blocked_keychain
    try:
        data = await reader.get_json("/api/messages")
        assert data["text"] == "长原文" * 50000
        await asyncio.wait_for(keychain_started.wait(), 2)
        await reader.stop()
        assert len(launched) == 1 and launched[0].returncode == 0
        assert not reader.auxiliary and reader.account_id is None
        await reader.start()
        await reader.get_json("/api/status")
        assert len(launched) == 2
    finally:
        await reader.stop()
