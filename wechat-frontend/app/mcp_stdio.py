"""Stdlib stdio transport for the fixed enterprise MCP endpoint. No secret in agent config."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import urllib.request
import urllib.error
from urllib.parse import urlparse


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, choices=["wecom"])
    parser.parse_args()
    config = Path(os.environ.get("WF_CONFIG", Path.home() / ".config/wechat-frontend/config.json"))
    cfg = json.loads(config.read_text()) if config.is_file() else {}
    host = cfg.get("bind_host", "127.0.0.1")
    if host not in ("127.0.0.1", "::1", "localhost"):
        raise SystemExit("MCP only supports local loopback")
    endpoint = f"http://{'[::1]' if host == '::1' else host}:{int(cfg.get('bind_port', 8088))}/mcp/wecom"
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    session = None
    for line in sys.stdin:
        payload = None
        try:
            payload = json.loads(line)
            token = (config.parent / "mcp/wecom-token").read_text().strip()
            headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json",
                       "Accept": "application/json, text/event-stream"}
            if session:
                headers["Mcp-Session-Id"] = session
            request = urllib.request.Request(endpoint, json.dumps(payload).encode(), headers=headers)
            with opener.open(request, timeout=50) as response:
                session = response.headers.get("Mcp-Session-Id") or session
                body = response.read()
            if body:
                print(body.decode(), flush=True)
        except Exception as exc:
            if isinstance(locals().get("payload"), dict) and "id" in payload:
                print(json.dumps({"jsonrpc": "2.0", "id": payload["id"], "error": {
                    "code": -32000, "message": "企业微信 MCP 未就绪或授权已撤销；请检查统一版设置"}}), flush=True)


if __name__ == "__main__":
    main()
