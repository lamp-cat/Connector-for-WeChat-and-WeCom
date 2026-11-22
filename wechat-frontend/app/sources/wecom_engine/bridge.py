"""Inherited-pipe RPC. Key events stay on this local pipe, never reach web pages."""
from __future__ import annotations

import json
import sys
import threading

from .engine import Engine, emit
from .query import query


def main():
    engine = Engine()
    worker = threading.Thread(target=engine.loop, daemon=True)
    worker.start()
    try:
        for line in sys.stdin:
            if len(line) > 65536:
                continue
            command = json.loads(line)
            if command.get("op") == "query":
                try:
                    data = query(engine, command["path"], command.get("params") or {})
                    emit("response", request_id=command["request_id"], ok=True, data=data)
                except Exception as exc:
                    code = 501 if isinstance(exc, NotImplementedError) else 409 if "账号" in str(exc) else 503
                    emit("response", request_id=command["request_id"], ok=False, status=code,
                         error=str(exc) if isinstance(exc, (ValueError, NotImplementedError)) else "快照查询未完成")
            elif command.get("op") in ("keys", "prepare", "start_runtime", "restored", "quit"):
                engine.action(command)
            if engine.quit.is_set():
                break
    finally:
        engine.quit.set()
        worker.join(timeout=6)
        engine.close()


if __name__ == "__main__":
    main()
