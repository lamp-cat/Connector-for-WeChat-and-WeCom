from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import uuid
from pathlib import Path

from ..reader_client import ReaderError


class WeComReader:
    source = "wecom"

    def __init__(self):
        self.process = None
        self.worker = None
        self.pending = {}
        self.lock = asyncio.Lock()
        self.state = {"code": "idle", "title": "等待企业微信连接"}
        self.account_id = None
        self.runtime_ready = False
        self.managed_pid = None
        self.notices = []
        self.auxiliary = set()
        self.stopping = False

    async def start(self):
        async with self.lock:
            if self.stopping:
                raise ReaderError(503, "企业微信读取进程正在停止")
            if self.process and self.process.returncode is None:
                return
            if self.worker:
                await self.worker
            self.account_id = None
            self.runtime_ready = False
            self.managed_pid = None
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.sources.wecom_engine.bridge",
                cwd=str(Path(__file__).resolve().parents[2]),
                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL, limit=64 * 1024 * 1024)
            self.worker = asyncio.create_task(self._events(self.process))

    async def send(self, command):
        await self.start()
        await self._send_current(self.process, command)

    async def _send_current(self, process, command):
        async with self.lock:
            if self.stopping or process is not self.process or process.returncode is not None:
                raise ReaderError(503, "企业微信连接已变化")
            try:
                process.stdin.write((json.dumps(command) + "\n").encode())
                await process.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                raise ReaderError(503, "企业微信读取进程已停止") from None

    def _background(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.auxiliary.add(task)
        def done(t):
            self.auxiliary.discard(t)
            if not t.cancelled() and t.exception():
                self.notices.append("企业微信密钥保存或恢复未完成，可重试连接。")
        task.add_done_callback(done)

    async def _keychain(self, op, account, keys=None):
        # Native helper uses Security.framework; secrets travel only over inherited pipes.
        root = Path(os.environ.get("WF_PLATFORM_ROOT", Path(__file__).resolve().parents[3]))
        helper = root / "setup/wecom-keychain"
        if not helper.is_file():
            self.notices.append("钥匙串组件缺失，请先构建统一版；连接密钥仅保留在当前进程。")
            return {}
        proc = await asyncio.create_subprocess_exec(str(helper), stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        payload = {"op": op, "account": account}
        if keys is not None:
            payload["keys"] = keys
        try:
            out, _ = await proc.communicate(json.dumps(payload).encode())
        except asyncio.CancelledError:
            if proc.returncode is None:
                proc.kill()
                await proc.wait()
            raise
        result = json.loads(out or b"{}")
        if op == "write" and not result.get("ok"):
            self.notices.append("钥匙串保存未成功；当前连接仍可使用，重启后需重新验证。")
        return result.get("keys") or {}

    async def _restore_keys(self, account, process):
        try:
            keys = await self._keychain("read", account)
        except Exception:
            keys = {}
        if not self.stopping and process is self.process and account == self.account_id:
            await self._send_current(process, {"op": "keys", "account": account, "keys": keys})

    async def _events(self, process):
        try:
            async for line in process.stdout:
                event = json.loads(line)
                kind = event.get("event")
                if kind == "response":
                    future = self.pending.pop(event.get("request_id"), None)
                    if future and not future.done():
                        future.set_result(event)
                elif kind == "status":
                    self.state = {k: v for k, v in event.items() if k != "event"}
                    if self.state["code"] in ("client_missing", "account_wait", "ambiguous"):
                        self.account_id = None
                elif kind == "account":
                    self.account_id = event.get("account")
                    self._background(self._restore_keys(self.account_id, process))
                elif kind == "keys":
                    self._background(self._keychain("write", event["account"], event["keys"]))
                elif kind == "runtime_ready":
                    self.runtime_ready = True
                elif kind == "managed":
                    self.managed_pid = event.get("pid")
                elif kind == "notice":
                    self.notices.append(event.get("text", "连接状态已更新"))
                    self.notices = self.notices[-5:]
        except (ValueError, asyncio.LimitOverrunError):
            self.state = {"code": "reader_error", "title": "企业微信读取响应未完成，请重试连接"}
        finally:
            if process is self.process:
                self.account_id = None
                for future in self.pending.values():
                    if not future.done():
                        future.set_exception(ReaderError(503, "企业微信读取进程已停止"))
                self.pending.clear()

    async def get_json(self, path, params=None):
        await self.start()
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            await self.send({"op": "query", "request_id": request_id, "path": path, "params": dict(params or {})})
            event = await asyncio.wait_for(future, timeout=45)
            if not event.get("ok"):
                raise ReaderError(event.get("status", 503), event.get("error", "企业微信查询未完成"))
            return event["data"]
        finally:
            self.pending.pop(request_id, None)

    async def request(self, method, path, *, params=None, **kwargs):
        if method != "GET":
            raise ReaderError(405, "只支持读取")
        data = await self.get_json(path, params)
        return 200, json.dumps(data, ensure_ascii=False).encode(), {"content-type": "application/json"}

    async def stop(self):
        self.stopping = True
        try:
            tasks = list(self.auxiliary)
            for task in tasks:
                task.cancel()
            for task in tasks:
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            async with self.lock:
                process = self.process
                if process and process.returncode is None:
                    try:
                        process.stdin.write(b'{"op":"quit"}\n')
                        await process.stdin.drain()
                    except (BrokenPipeError, ConnectionResetError):
                        pass
                    try:
                        await asyncio.wait_for(process.wait(), 8)
                    except asyncio.TimeoutError:
                        process.kill()
                        await process.wait()
                if self.worker:
                    await self.worker
                self.process = self.worker = None
                self.account_id = None
                self.runtime_ready = False
                self.managed_pid = None
        finally:
            self.stopping = False


wecom = WeComReader()
