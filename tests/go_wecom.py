"""Test-only adapter exercising the actual Go pipe binary with offline fixtures."""
import json
import subprocess
from pathlib import Path

BINARY = Path(__file__).resolve().parents[1] / "wechat-realtime-reader/bin/wecom-reader"


def query(engine, path, params):
    command = {"op": "query", "request_id": "test", "path": path, "params": params}
    result = subprocess.run(
        [str(BINARY), "--snapshot", str(engine.current), "--account", engine.account],
        input=json.dumps(command) + '\n{"op":"quit"}\n',
        capture_output=True, text=True, timeout=15, check=True,
    )
    events = [json.loads(line) for line in result.stdout.splitlines()]
    response = next(event for event in events if event.get("event") == "response")
    if not response["ok"]:
        exception = NotImplementedError if response["status"] == 501 else ValueError
        raise exception(response["error"])
    return response["data"]
