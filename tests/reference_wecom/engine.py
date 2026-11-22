"""Frozen Python reference for differential migration tests; never bundled or launched."""
from __future__ import annotations

import hashlib
import json
import os
import plistlib
import re
import shutil
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import quote

from Crypto.Cipher import AES
AIError = ValueError  # AI remains in the shared gateway; the oracle tests only reading.

DB_NAMES = ("message.db", "session.db", "user.db")
MAGIC = b"SQLite format 3\0"
HOME = Path.home()
APP = Path("/Applications/企业微信.app")
CONTAINER = HOME / "Library/Containers/com.tencent.WeWorkMac/Data"
ROOT = Path(os.environ.get("WF_WECOM_ROOT", str(HOME / "Library/Application Support/WeComCompanion")))
WRITE_LOCK = threading.Lock()


def emit(kind, **payload):
    with WRITE_LOCK:
        print(json.dumps({"event": kind, **payload}, ensure_ascii=False), flush=True)


def run(args, seconds=15, **kwargs):
    return subprocess.run(args, capture_output=True, timeout=seconds, check=True, **kwargs)


def page_length(header):
    if len(header) < 24:
        raise ValueError("数据库头不完整")
    n = int.from_bytes(header[16:18], "big")
    n = 65536 if n == 1 else n
    if n not in (512, 1024, 2048, 4096, 8192, 16384, 32768, 65536):
        raise ValueError("当前数据库格式尚未支持")
    return n


def aes_for(key, page):
    if len(key) != 16:
        raise ValueError("密钥未通过验证")
    derived = hashlib.md5(key + page.to_bytes(4, "little") + b"sAlT").digest()
    seed = page + 1
    values = []
    for _ in range(4):
        seed = 40692 * (seed % 52774) - 3791 * (seed // 52774)
        seed = seed if seed >= 0 else seed + 2147483399
        values.append(seed)
    iv = hashlib.md5(struct.pack("<IIII", *values)).digest()
    return AES.new(derived, AES.MODE_CBC, iv)


def decode_page(block, key, number):
    if not block or len(block) % 16:
        raise ValueError("数据库页不完整")
    if number != 1:
        return aes_for(key, number).decrypt(block)
    if len(block) < 32 or block[21:24] != bytes((64, 32, 32)):
        raise ValueError("当前数据库加密格式尚未支持")
    decoded = aes_for(key, 1).decrypt(block[8:16] + block[24:])
    if decoded[:8] != block[16:24]:
        raise ValueError("密钥未通过数据库校验")
    return MAGIC + decoded


def rolling_checksum(blob, previous=(0, 0), endian="<"):
    if len(blob) % 8:
        raise ValueError("WAL 校验长度错误")
    words = struct.unpack(endian + "I" * (len(blob) // 4), blob)
    left, right = previous
    for x, y in zip(words[::2], words[1::2]):
        left = (left + x + right) % 2**32
        right = (right + y + left) % 2**32
    return left, right


def apply_wal(base, journal, key, size, encrypted):
    if not journal:
        return base
    if len(journal) < 32:
        raise ValueError("WAL 正在写入，稍后重试")
    tag, version, pagesize = struct.unpack(">III", journal[:12])
    if tag not in (0x377F0682, 0x377F0683) or version != 3007000 or pagesize != size:
        raise ValueError("WAL 格式尚未支持")
    order = "<" if tag == 0x377F0682 else ">"
    state = rolling_checksum(journal[:24], endian=order)
    if state != struct.unpack(">II", journal[24:32]):
        raise ValueError("WAL 头部校验失败")
    pending, updates, total = {}, {}, None
    step = 24 + size
    for pos in range(32, len(journal) - step + 1, step):
        frame = journal[pos:pos + 24]
        body = journal[pos + 24:pos + step]
        page, count = struct.unpack(">II", frame[:8])
        # SQLite may restart a WAL without truncating its previous-generation tail.
        # Different salts mark the end of this generation, not a corrupt snapshot.
        if frame[8:16] != journal[16:24]:
            break
        if page == 0:
            raise ValueError("WAL 已轮转，稍后重试")
        state = rolling_checksum(frame[:8] + body, state, order)
        if state != struct.unpack(">II", frame[16:24]):
            raise ValueError("WAL 帧校验失败")
        pending[page] = body
        if count:
            # A truncation transaction invalidates older pages beyond its boundary.
            updates.update(pending)
            updates = {n: b for n, b in updates.items() if n <= count}
            pending = {}
            total = count
    if total is None:
        return base
    if total > 1_000_000:
        raise ValueError("数据库规模超出当前读取上限")
    output = bytearray(total * size)
    output[:min(len(base), len(output))] = base[:len(output)]
    for n, b in updates.items():
        output[(n - 1) * size:n * size] = decode_page(b, key, n) if encrypted else b
    return bytes(output)


def fingerprint(path):
    try:
        st = path.stat()
        return st.st_ino, st.st_size, st.st_mtime_ns
    except FileNotFoundError:
        return None


def readonly(path):
    conn = sqlite3.connect("file:" + quote(str(path)) + "?mode=ro&immutable=1", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def snapshot(source, keys, destination):
    paths = [source / n for n in DB_NAMES] + [source / (n + "-wal") for n in DB_NAMES]
    for _ in range(3):
        before = tuple(fingerprint(p) for p in paths)
        if any(x is None for x in before[:3]):
            raise ValueError("当前账号数据库尚未就绪")
        data = {}
        try:
            for p, sig in zip(paths, before):
                if sig and sig[1] > 2 * 1024**3:
                    raise ValueError("单库超过 2 GB，当前版本暂不支持")
                data[p.name] = p.read_bytes() if sig else b""
        except FileNotFoundError:
            continue
        if before == tuple(fingerprint(p) for p in paths):
            break
    else:
        raise ValueError("企业微信正在更新数据库，稍后重试")
    destination.mkdir(mode=0o700, parents=True)
    try:
        for name in DB_NAMES:
            raw = data[name]
            size = page_length(raw)
            if len(raw) % size:
                raise ValueError("数据库快照不完整")
            encrypted = not raw.startswith(MAGIC)
            key = bytes.fromhex(keys.get(name, "")) if encrypted else b""
            plain = b"".join(decode_page(raw[i:i + size], key, i // size + 1)
                             for i in range(0, len(raw), size)) if encrypted else raw
            plain = apply_wal(plain, data[name + "-wal"], key, size, encrypted)
            output = destination / name
            fd = os.open(output, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "wb") as stream:
                stream.write(plain)
            with readonly(output) as db:
                result = db.execute("PRAGMA quick_check").fetchall()
                if len(result) != 1 or result[0][0] != "ok":
                    raise ValueError("解密后数据库完整性校验失败")
        return before
    except BaseException:
        shutil.rmtree(destination, ignore_errors=True)
        raise


def client_processes():
    try:
        result = run(["/usr/bin/pgrep", "-x", "企业微信"], 4)
        return [int(v) for v in result.stdout.split() if v.isdigit()]
    except (subprocess.SubprocessError, OSError):
        return []


def open_paths(pid):
    try:
        result = run(["/usr/sbin/lsof", "-p", str(pid), "-Fn"], 8)
        return [line[1:] for line in result.stdout.decode(errors="replace").splitlines()
                if line.startswith("n")]
    except (subprocess.SubprocessError, OSError):
        return []


def bounded_datasets(root):
    """Known data root only; never traverse media, uploads or all of Library."""
    found = []
    if not root.is_dir():
        return found
    for folder, directories, files in os.walk(root):
        depth = len(Path(folder).relative_to(root).parts)
        directories[:] = [d for d in directories
                          if depth < 4 and d not in {"Avator", "CacheMapping", "Emotion",
                                                     "File", "Video", "Image", "Log", "Cef"}]
        if set(DB_NAMES).issubset(files):
            found.append(Path(folder))
            directories[:] = []
    return found


def discover(pids):
    paths = {pid: open_paths(pid) for pid in pids}
    account_ids = set()
    direct = set()
    for files in paths.values():
        for item in files:
            if Path(item).name in DB_NAMES:
                direct.add(Path(item).parent)
            match = re.search(r"/WXWork/Data/([^/]+)/", item)
            if match:
                account_ids.add(match[1])
    data = CONTAINER / "Library/Application Support/WXWork/Data"
    if len(direct) == 1:
        # An open core database is stronger evidence than a directory walk.
        return next(iter(direct)), len(account_ids)
    if len(direct) > 1:
        raise ValueError("检测到多个正在使用的数据目录，请在企业微信中保留一个登录账号后重试")
    if not account_ids:
        # No evidence of login: do not request access to historical private data.
        return None, 0
    active = list(direct)
    for account in sorted(account_ids):
        active.extend(bounded_datasets(data / account))
    active = sorted(set(active))
    if len(active) == 1:
        return active[0], len(account_ids)
    if len(active) > 1:
        raise ValueError("检测到多个正在使用的数据目录，请在企业微信中保留一个登录账号后重试")
    return None, 0


def columns(db, table):
    return {str(row[1]) for row in db.execute('PRAGMA table_info("' + table + '")')}


def protobuf_fields(blob):
    if len(blob) > 2 * 1024**2:
        raise ValueError("消息结构过大")
    offset, result = 0, []
    def varint():
        nonlocal offset
        value = 0
        for shift in range(0, 70, 7):
            if offset >= len(blob):
                raise ValueError("消息结构不完整")
            byte = blob[offset]
            offset += 1
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise ValueError("消息结构无效")
    while offset < len(blob):
        tag = varint()
        number, wire = tag >> 3, tag & 7
        if not number or len(result) >= 2048:
            raise ValueError("消息结构无效")
        if wire == 0:
            value = varint()
        elif wire in (1, 2, 5):
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            if size > len(blob) - offset:
                raise ValueError("消息结构不完整")
            value = blob[offset:offset + size]
            offset += size
        else:
            raise ValueError("消息结构尚未支持")
        result.append((number, wire, value))
    return result


def decode_wecom_text(blob):
    """Decode observed text runs without treating arbitrary protobuf leaves as chat."""
    runs = protobuf_fields(blob)
    if not runs or any(n != 1 or w != 2 for n, w, _ in runs):
        raise ValueError("消息结构尚未支持")
    text = []
    for _, _, run in runs:
        items = protobuf_fields(run)
        kind = next((v for n, w, v in items if n == 1 and w == 0), None)
        payload = next((v for n, w, v in items if n == 2 and w == 2), None)
        if kind in (0, 10) and payload is not None:
            field = 1 if kind == 0 else 2
            nested = protobuf_fields(payload)
            raw = next((v for n, w, v in nested if n == field and w == 2), None)
            if raw is None:
                raise ValueError("文本片段尚未支持")
            part = raw.decode("utf8")
            if any(ord(c) < 32 and c not in "\t\r\n" for c in part):
                raise ValueError("文本片段无效")
            text.append(part)
        else:
            text.append("[暂未解析的消息片段]")
    return "".join(text)


def readable_content(value, content_type=None):
    if value is None:
        return ""
    if isinstance(value, bytes):
        if content_type in (0, 2):
            try:
                return decode_wecom_text(value)
            except (ValueError, UnicodeDecodeError):
                return "[暂未解析的结构化消息]"
        try:
            value = value.decode("utf-8")
        except UnicodeDecodeError:
            return "[暂未解析的二进制消息]"
    text = str(value)
    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            for k in ("text", "content", "title", "msg"):
                if isinstance(parsed.get(k), str):
                    text = parsed[k]
                    break
    except (ValueError, TypeError):
        pass
    if any(ord(c) < 32 and c not in "\t\r\n" for c in text):
        return "[暂未解析的结构化消息]"
    return text


def message_segments(db):
    segments = []
    for table in ("message_table", "message_small_table", "kf_message_tableV1"):
        cols = columns(db, table)
        if not {"conversation_id", "message_id", "send_time", "content"} <= cols:
            continue
        sender = 'CAST(sender_id AS TEXT)' if "sender_id" in cols else "''"
        kind = "content_type" if "content_type" in cols else "NULL"
        segments.append(f'''SELECT CAST(conversation_id AS TEXT) cid,
            CAST(message_id AS TEXT) mid, {sender} sender,
            CASE WHEN send_time > 100000000000 THEN send_time/1000.0 ELSE send_time END ts,
            content, {kind} kind, '{table}' src FROM "{table}"''')
    if not segments:
        raise ValueError("此版本企业微信的消息结构尚未支持")
    return " UNION ALL ".join(segments)


def history_messages(folder, chosen, scope="n:100", names=None):
    if scope not in ("n:50", "n:100", "n:200", "d:7", "d:30"):
        raise ValueError("请选择有效的历史范围。")
    if names is None:
        names = {}
        with readonly(folder / "user.db") as db:
            if {"id", "name"} <= columns(db, "user_table"):
                for row in db.execute("SELECT * FROM user_table"):
                    r = dict(row)
                    names[str(r["id"])] = r.get("real_name") or r.get("name") or str(r["id"])
    with readonly(folder / "message.db") as db:
        segments = message_segments(db)
        days = int(scope[2:]) if scope.startswith("d:") else 0
        limit = int(scope[2:]) if not days else 5000
        condition = " AND ts>=?" if days else ""
        args = (chosen, time.time() - days * 86400, limit + 1) if days else (chosen, limit + 1)
        rows = list(db.execute("SELECT * FROM (" + segments + ") WHERE cid=?" + condition +
                               " ORDER BY ts DESC,mid DESC,src DESC LIMIT ?", args))
    messages = [{"sender": names.get(str(r["sender"]), str(r["sender"])),
                 "text": readable_content(r["content"], r["kind"]), "ts": r["ts"]}
                for r in reversed(rows[:limit])]
    return messages, bool(days and len(rows) > limit)


def build_context(folder, chosen=None):
    names = {}
    with readonly(folder / "user.db") as db:
        if {"id", "name"} <= columns(db, "user_table"):
            for row in db.execute("SELECT * FROM user_table"):
                r = dict(row)
                names[str(r["id"])] = r.get("real_name") or r.get("name") or str(r["id"])
    sessions = []
    with readonly(folder / "session.db") as db:
        if "id" not in columns(db, "conversation_table"):
            raise ValueError("此版本企业微信的会话结构尚未支持")
        for row in db.execute("SELECT * FROM conversation_table"):
            r = dict(row)
            stamp = float(r.get("last_message_time") or 0)
            sessions.append({"id": str(r["id"]),
                             "name": r.get("roomname_remark") or r.get("name") or str(r["id"]),
                             "ts": stamp / 1000 if stamp > 10**11 else stamp})
    if not sessions:
        return {"sessions": [], "selected": None, "messages": [], "summary": "当前账号暂无会话。"}
    with readonly(folder / "message.db") as db:
        segments = message_segments(db)
        latest = {str(row["cid"]): float(row["ts"] or 0) for row in
                  db.execute("SELECT cid,MAX(ts) ts FROM (" + segments + ") GROUP BY cid")}
    for session in sessions:
        session["ts"] = max(session["ts"], latest.get(session["id"], 0))
    sessions.sort(key=lambda s: (s["ts"], s["id"]), reverse=True)
    selected = next((s for s in sessions if s["id"] == chosen), None)
    if selected is None:
        selected = sessions[0] if sessions else None
    if selected is None:
        return {"sessions": [], "selected": None, "messages": [], "summary": "当前账号暂无会话。"}
    with readonly(folder / "message.db") as db:
        sql = "SELECT * FROM (" + segments + ") WHERE cid=? ORDER BY ts DESC,mid DESC,src DESC LIMIT 60"
        rows = list(db.execute(sql, (selected["id"],)))
    messages = [{"sender": names.get(str(r["sender"]), str(r["sender"])),
                 "text": readable_content(r["content"], r["kind"]), "ts": r["ts"]}
                for r in reversed(rows)]
    text_messages = [m for m in messages if m["text"] and not m["text"].startswith("[")]
    # The built-in output is deliberately extractive: no invented facts or AI claim.
    last = text_messages[-5:]
    summary = "\n\n".join(f'{m["sender"]}：{m["text"][:420]}' for m in last) or "最近消息暂未解析为文本。"
    todos = []
    for m in text_messages[-30:]:
        if re.search(r"请|需要|麻烦|确认|截止|明天|今天|安排|待办|提交|跟进", m["text"]):
            todos.append(f'{m["sender"]}：{m["text"][:280]}')
    return {"sessions": sessions, "selected": selected, "messages": messages,
            "summary": summary, "todos": todos[-6:],
            "context_mode": "selected" if chosen == selected["id"] else "recent"}


HOOK = """
'use strict';
const seen = new Set();
const hooked = new Set();
const contexts = new Map();
const calls = {};
let installed = 0;
function count(name, length) {
  const id = name + ':' + length;
  if (Object.keys(calls).length < 80 || calls[id]) calls[id] = (calls[id] || 0) + 1;
}
function candidate(bytes) {
  if (bytes.length !== 24 || bytes[20] !== 115 || bytes[21] !== 65 || bytes[22] !== 108 || bytes[23] !== 84) return;
  let raw = '';
  for (let i=0; i<16; i++) raw += bytes[i].toString(16).padStart(2, '0');
  if (!seen.has(raw)) { seen.add(raw); send({type:'candidate', value:raw}); }
}
function read(data, length) {
  return Array.from(new Uint8Array(data.readByteArray(length)));
}
function hook(address, callbacks) {
  if (!address || hooked.has(address.toString())) return;
  hooked.add(address.toString());
  Interceptor.attach(address, callbacks);
  installed++;
}
function stream(prefix, init, update, final, piece=false, finalContext=1) {
  hook(init, {onEnter(args) {
    if (contexts.size > 4096) contexts.clear();
    contexts.set(prefix+args[0], []);
  }});
  hook(update, {
    onEnter(args) {
      try {
        const id = prefix+args[0];
        const data = piece ? args[1].readPointer() : args[1];
        const length = piece ? args[1].add(Process.pointerSize).readU64().toNumber() : args[2].toUInt32();
        count(prefix, length);
        if (!contexts.has(id)) return;
        const bytes = contexts.get(id);
        if (length > 24 || bytes.length + length > 24) { contexts.delete(id); return; }
        bytes.push(...read(data, length));
      } catch (_) {}
    }
  });
  hook(final, {onEnter(args) {
    const id = prefix+args[finalContext];
    const bytes = contexts.get(id);
    if (bytes) candidate(bytes);
    contexts.delete(id);
  }});
}
function resolve(name) { return Module.findGlobalExportByName(name); }
for (const name of ['CC_MD5', 'MD5']) {
  hook(resolve(name), {
    onEnter(args) { try {
      const length = args[1].toUInt32();
      count(name, length);
      if (length === 24) candidate(read(args[0], 24));
    } catch (_) {} }
  });
}
stream('cc:', resolve('CC_MD5_Init'), resolve('CC_MD5_Update'), resolve('CC_MD5_Final'));
stream('ssl:', resolve('MD5_Init'), resolve('MD5_Update'), resolve('MD5_Final'));
for (const m of Process.enumerateModules()) {
  if (!m.path.includes('企业微信.app') && m.name !== 'capture-fixture') continue;
  const exports = m.enumerateExports();
  const find = pattern => (exports.find(x=>pattern.test(x.name)) || {}).address;
  const exact = name => (exports.find(x=>x.name === name) || {}).address;
  stream(m.name+':native:', exact('md5_init'), exact('md5_process'), exact('md5_finish'), false, 0);
  stream(m.name+':openssl:', exact('MD5_Init'), exact('MD5_Update'), exact('MD5_Final'));
  stream(m.name+':', find(/^_ZN4base7MD5Init/), find(/^_ZN4base9MD5Update/), find(/^_ZN4base8MD5Final/), true);
  hook(find(/^_ZN4base6MD5Sum/), {
    onEnter(args) { try {
      if (args[1].toUInt32() === 24) candidate(read(args[0], 24));
    } catch (_) {} }
  });
}
send({type:'installed', count:installed});
setInterval(() => { send({type:'activity', calls:calls}); }, 10000);
"""


class Engine:
    def __init__(self):
        os.umask(0o077)
        ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.cache = Path(tempfile.mkdtemp(prefix="snapshot-", dir=ROOT))
        self.quit = threading.Event()
        self.guard = threading.RLock()
        self.busy = threading.Lock()
        self.source = None
        self.keys_by_account = {}
        self.current = None
        self.signature = None
        self.chosen = None
        self.frida_session = None
        self.frida_script = None
        self.attached_pid = None
        self.attempted_pid = None
        self.managed_pid = None
        self.client_checked = None
        self.capture_since = None
        self.candidates = []
        self.pending_key_account = None
        self.last_context = None
        self.permission = False
        self.runtime = ROOT / "Runtime/企业微信.app"
        self.runtime_version = None
        self.updated = None
        self.epoch = 0
        # Unified app uses the gateway's shared AI configuration and service.
        self.ai = None
        self.ai_slots = threading.BoundedSemaphore(2)
        self.state("starting", "正在发现企业微信", "无需输入目录、进程号或密钥。")

    def state(self, code, title, detail, **extra):
        emit("status", code=code, title=title, detail=detail,
             updated=self.updated, managed=bool(self.managed_pid), **extra)

    @property
    def account(self):
        return hashlib.sha256(str(self.source).encode()).hexdigest() if self.source else None

    def clear_context(self):
        with self.guard:
            self.current = None
            self.signature = None
            self.last_context = None
            self.updated = None
            self.chosen = None
            self.epoch += 1
        emit("context", sessions=[], selected=None, messages=[], summary="", todos=[])

    def try_candidates(self):
        if not self.source:
            return {}
        result = {}
        for name in DB_NAMES:
            with (self.source / name).open("rb") as f:
                head = f.read(24)
                f.seek(0)
                first = f.read(page_length(head))
            if first.startswith(MAGIC):
                result[name] = ""
                continue
            existing = self.keys_by_account.get(self.account, {}).get(name)
            candidates = ([existing] if existing else []) + list(self.candidates)
            for key in candidates:
                try:
                    decode_page(first, bytes.fromhex(key), 1)
                    result[name] = key
                    break
                except ValueError:
                    continue
        if len(result) == 3:
            changed = result != self.keys_by_account.get(self.account)
            self.keys_by_account[self.account] = result
            if changed:
                emit("keys", account=self.account, keys=result)
        return result

    def attach(self, pid, spawned=False):
        import frida
        if self.frida_session:
            self.frida_session.detach()
        self.frida_session = frida.get_local_device().attach(pid)
        self.attached_pid = pid
        self.frida_script = self.frida_session.create_script(HOOK)

        def receive(message, data):
            if message.get("type") != "send":
                return
            payload = message.get("payload") or {}
            if payload.get("type") == "candidate":
                value = payload.get("value", "")
                if re.fullmatch("[0-9a-f]{32}", value):
                    with self.guard:
                        if value not in self.candidates:
                            self.candidates.append(value)
                            self.candidates = self.candidates[-128:]
                            emit("capture_state", candidate_count=len(self.candidates))
            elif payload.get("type") == "installed":
                emit("capture_state", hooks=payload.get("count", 0))
                if not payload.get("count"):
                    self.state("unsupported", "取钥入口尚未识别", "此版本客户端没有预期的加密入口，无法承诺自动读取。")
            elif payload.get("type") == "activity":
                emit("capture_state", calls=payload.get("calls", {}))

        self.frida_script.on("message", receive)
        self.frida_script.load()
        self.capture_since = time.monotonic()
        if spawned:
            frida.get_local_device().resume(pid)

    def sync(self):
        pids = client_processes()
        if not pids:
            self.clear_context()
            self.source = None
            self.attempted_pid = None
            self.managed_pid = None
            self.client_checked = None
            emit("managed", pid=None)
            self.state("client_missing", "等待企业微信登录", "打开并登录本机企业微信后，伴侣会自动继续连接。")
            return
        if len(pids) != 1:
            self.clear_context()
            self.state("ambiguous", "检测到多个企业微信进程", "请保留一个客户端后重试，避免读取错误账号。")
            return
        pid = pids[0]
        if self.client_checked != pid:
            self.client_checked = pid
            try:
                executable = run(["/bin/ps", "-p", str(pid), "-o", "comm="], 4).stdout.decode().strip()
                is_runtime = executable == str(self.runtime / "Contents/MacOS/企业微信")
                self.managed_pid = pid if is_runtime else None
                emit("managed", pid=self.managed_pid)
            except (subprocess.SubprocessError, OSError):
                pass
        self.state("discovering", "正在识别当前账号", "按正在运行的客户端识别数据；历史账号不会自动选中。")
        try:
            source, _ = discover(pids)
        except ValueError as exc:
            self.clear_context()
            self.source = None
            self.state("account_wait", "等待确认当前账号", str(exc))
            return
        if source is None:
            self.clear_context()
            self.source = None
            self.state("account_wait", "等待数据库就绪", "请在企业微信登录并打开一个会话。")
            return
        if source != self.source:
            self.clear_context()
            self.source = source
            self.pending_key_account = self.account
            emit("account", account=self.account)
        keys = self.try_candidates()
        if len(keys) < 3:
            self.clear_context()
            if self.pending_key_account == self.account:
                self.state("restoring", "正在恢复已保存的连接", "正在读取钥匙串；若系统提示，请允许本应用访问已保存的密钥。")
                return
            if self.attempted_pid != pid:
                self.attempted_pid = pid
                try:
                    self.state("attaching", "正在连接取钥组件", "系统保护可能要求连接授权；不会自动重启企业微信。")
                    self.attach(pid)
                except Exception:
                    self.state("approval", "需要一次客户端连接授权",
                               "当前客户端的系统保护阻止自动取钥。可在授权后尝试临时运行副本；原安装和 SIP 保持不变。")
                    return
            if self.attached_pid != pid:
                self.state("approval", "需要一次客户端连接授权",
                           "没有取得可用密钥。首次连接按钮会说明临时副本和客户端重启步骤。")
            else:
                elapsed = time.monotonic() - (self.capture_since or time.monotonic())
                self.state("capturing" if elapsed < 60 else "capture_wait",
                           "正在自动验证密钥" if elapsed < 60 else "尚未捕获有效密钥",
                           f"已验证 {len(keys)}/3 个数据库。请在企业微信打开或切换会话；未验证时不会展示历史缓存。")
            return
        signatures = tuple(fingerprint(source / n) for n in
                           list(DB_NAMES) + [n + "-wal" for n in DB_NAMES])
        with self.guard:
            if signatures != self.signature or self.current is None:
                fresh = self.cache / str(time.time_ns())
                new_signature = snapshot(source, keys, fresh)
                context = build_context(fresh, self.chosen)
                old = self.current
                self.current = fresh
                self.signature = new_signature
                self.updated = time.time()
                self.last_context = context
                if old and old != fresh:
                    shutil.rmtree(old, ignore_errors=True)
                emit("context", **context, updated=self.updated)
            self.state("connected", "已连接 · 自动同步",
                       "正在读取当前账号的本地快照。上下文为所选会话或最近活跃会话，不代表当前前台窗口。")

    def loop(self):
        while not self.quit.is_set():
            if self.busy.acquire(blocking=False):
                try:
                    self.sync()
                except PermissionError:
                    self.clear_context()
                    self.state("permission", "需要允许读取本地数据", "macOS 阻止访问企业微信数据。请通过“允许读取目录”授予访问后重试。")
                except Exception as exc:
                    self.clear_context()
                    detail = str(exc) if isinstance(exc, ValueError) else "读取暂未完成；请检查登录状态与 macOS 文件访问权限。"
                    self.state("read_error", "正在等待可用快照", detail)
                finally:
                    self.busy.release()
            self.quit.wait(5)

    def prepare_runtime(self):
        if not self.busy.acquire(blocking=False):
            emit("notice", text="正在连接，请稍后重试首次连接。")
            return
        try:
            self.state("preparing", "正在准备临时运行副本", "仅复制并签名独立副本；此步骤不会退出或启动企业微信。")
            info = plistlib.loads((APP / "Contents/Info.plist").read_bytes())
            version = str(info.get("CFBundleVersion")) + "-" + str(info.get("CFBundleShortVersionString"))
            marker = self.runtime.parent / "version.txt"
            if self.runtime.exists() and marker.exists() and marker.read_text() == version:
                run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(self.runtime)], 90)
                emit("runtime_ready", detail="临时副本已准备。继续连接会正常退出当前企业微信，并在取钥监听启动后运行副本。")
                return
            staging = self.runtime.parent / ("staging-" + str(time.time_ns()) + ".app")
            staging.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            try:
                run(["/usr/bin/ditto", str(APP), str(staging)], 300)
                entitlements = {
                    "com.apple.security.get-task-allow": True,
                    "com.apple.security.cs.allow-jit": True,
                    "com.apple.security.cs.allow-unsigned-executable-memory": True,
                    "com.apple.security.cs.disable-library-validation": True,
                }
                ent_path = self.runtime.parent / "runtime-entitlements.plist"
                ent_path.write_bytes(plistlib.dumps(entitlements))
                run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-",
                     "--entitlements", str(ent_path), str(staging)], 240)
                run(["/usr/bin/codesign", "--verify", "--deep", "--strict", str(staging)], 90)
                if self.runtime.exists():
                    shutil.rmtree(self.runtime)
                staging.rename(self.runtime)
                marker.write_text(version)
            finally:
                if staging.exists():
                    shutil.rmtree(staging, ignore_errors=True)
            emit("runtime_ready", detail="临时副本已准备。继续连接会正常退出当前企业微信，并在取钥监听启动后运行副本。此兼容性仍需实机验证。")
        except Exception:
            self.state("prepare_error", "临时副本未能准备完成", "原客户端未改动。可重试；当前版本尚不能直接完成自动取钥。")
        finally:
            self.busy.release()

    def start_runtime(self):
        # This command is sent only AFTER native UI confirmation and graceful quit.
        if not self.busy.acquire(blocking=False):
            emit("notice", text="正在读取，请稍后重试连接。")
            return
        pid = None
        try:
            if client_processes():
                raise ValueError("企业微信尚未正常退出，已取消副本启动；没有强制结束客户端。")
            if not self.runtime.exists():
                raise ValueError("临时运行副本尚未准备")
            import frida
            executable = self.runtime / "Contents/MacOS/企业微信"
            env = dict(os.environ)
            # Keep Foundation's user directory aligned with the official sandbox's data.
            env["CFFIXED_USER_HOME"] = str(CONTAINER)
            pid = frida.get_local_device().spawn([str(executable)], env=env, cwd=str(CONTAINER))
            self.managed_pid = pid
            emit("managed", pid=pid)
            self.attach(pid, spawned=True)
            self.attempted_pid = pid
            self.state("capturing", "正在连接临时运行副本", "若需要请在企业微信登录。伴侣会自动捕获并逐库验证密钥。")
        except Exception as exc:
            if pid:
                try:
                    import frida
                    frida.get_local_device().resume(pid)
                except Exception:
                    pass
            self.state("runtime_error", "临时副本连接未完成",
                       str(exc) if isinstance(exc, ValueError) else "此客户端版本或系统可能不兼容临时副本。请使用“恢复官方客户端”。")
        finally:
            self.busy.release()

    def action(self, command):
        op = command.get("op")
        if op == "keys":
            account = command.get("account")
            keys = command.get("keys")
            if isinstance(account, str) and isinstance(keys, dict):
                valid = {n: k for n, k in keys.items()
                         if n in DB_NAMES and isinstance(k, str)
                         and (not k or re.fullmatch("[0-9a-f]{32}", k))}
                if valid or account not in self.keys_by_account:
                    self.keys_by_account[account] = valid
                if self.pending_key_account == account:
                    self.pending_key_account = None
        elif op == "select":
            with self.guard:
                requested = command.get("id")
                if requested is not None and not any(s["id"] == requested for s in (self.last_context or {}).get("sessions", [])):
                    raise ValueError("会话不可用，请重新选择。")
                self.chosen = requested
                self.epoch += 1
                if self.current:
                    self.last_context = build_context(self.current, self.chosen)
                    emit("context", **self.last_context, updated=self.updated)
        elif op == "draft":
            with self.guard:
                context = self.last_context
                if not context or not self.current:
                    emit("notice", text="当前没有已验证的会话上下文，请先完成连接。")
                    return
                intent = str(command.get("intent", "")).strip()
                messages = [m for m in context["messages"] if m["text"] and not m["text"].startswith("[")]
                if not messages:
                    emit("notice", text="最近会话没有可解析的文本。")
                    return
                if intent:
                    draft = intent
                    note = "按你输入的回复要点保留原文，请确认后复制。"
                else:
                    draft = "收到，我先确认一下相关信息，稍后回复你。"
                    note = "本地确认回复模板；没有调用 AI 模型。请结合原消息修改后使用。"
                emit("draft", text=draft, note=note)
        elif op == "prepare":
            threading.Thread(target=self.prepare_runtime, daemon=True).start()
        elif op == "start_runtime":
            threading.Thread(target=self.start_runtime, daemon=True).start()
        elif op == "restored":
            self.managed_pid = None
            self.attached_pid = None
            self.attempted_pid = None
            self.frida_session = None
            self.clear_context()
        elif op == "quit":
            self.quit.set()
        elif op == "request":
            route = command.get("route")
            data = command.get("data") or {}
            request_id = command.get("request_id")
            if not isinstance(request_id, str) or len(request_id) > 100:
                return
            if route in ("ai/models", "ai/test", "ai/summary", "ai/suggestions"):
                if not self.ai_slots.acquire(blocking=False):
                    emit("response", request_id=request_id, ok=False, error="已有两个 AI 请求正在处理，请稍后重试。")
                    return
                threading.Thread(target=self.respond, args=(request_id, route, data, True), daemon=True).start()
            else:
                self.respond(request_id, route, data)

    def respond(self, request_id, route, data, release=False):
        try:
            if route == "settings/get":
                result = self.ai.public()
            elif route == "settings/save":
                result = self.ai.save(data)
                emit("ai_settings", **result)
            elif route == "ai/models":
                result = self.ai.models(data)
            elif route == "ai/test":
                result = self.ai.test(data)
            elif route in ("ai/summary", "ai/suggestions"):
                with self.guard:
                    context = self.last_context or {}
                    selected = context.get("selected")
                    if not selected or not self.current or data.get("chat_id") != selected["id"]:
                        raise AIError("当前会话已变化或尚未验证，请重新选择后生成。")
                    epoch, account, selected_id = self.epoch, self.account, selected["id"]
                    scope = data.get("scope", "n:100")
                    messages, capped = history_messages(self.current, selected_id, scope)
                    chat_name = selected["name"]
                with self.ai.lock:
                    revision = self.ai.revision
                    config = dict(self.ai.config)
                result = self.ai.analyze(route.split("/")[1], messages, chat_name, scope,
                                         str(data.get("intent", "")), config=config)
                result.update(chat_id=selected_id, history_capped=capped)
                with self.guard:
                    if (epoch != self.epoch or account != self.account or not self.current
                            or selected_id != (self.last_context or {}).get("selected", {}).get("id")):
                        raise AIError("分析期间会话或账号已变化，旧结果已丢弃。")
                with self.ai.lock:
                    if revision != self.ai.revision:
                        raise AIError("分析期间 AI 设置已变化，请重新生成。")
            elif route == "state/get":
                result = {"context": self.last_context or {}, "settings": self.ai.public()}
            else:
                raise AIError("此操作尚未支持。")
            emit("response", request_id=request_id, ok=True, data=result)
        except Exception as exc:
            emit("response", request_id=request_id, ok=False,
                 error=str(exc) if isinstance(exc, (AIError, ValueError)) else "操作未完成，请检查配置后重试。")
        finally:
            if release:
                self.ai_slots.release()

    def close(self):
        if self.frida_session:
            try:
                self.frida_session.detach()
            except Exception:
                pass
        shutil.rmtree(self.cache, ignore_errors=True)


def main():
    global ROOT
    demo = "--demo" in sys.argv
    if demo:
        ROOT = Path(tempfile.mkdtemp(prefix="wecom-companion-demo-"))
    engine = Engine()
    if demo:
        from .demo_fixture import prepare
        engine.current = prepare(ROOT)
        engine.last_context = build_context(engine.current)
        engine.updated = time.time()
        emit("context", **engine.last_context, updated=engine.updated, demo=True)
        engine.state("connected", "演示数据 · 界面验收", "当前仅使用虚构会话，不读取真实企业微信账号。")
    if "--start-copy" in sys.argv:
        engine.start_runtime()
    worker = threading.Thread(target=(lambda: engine.quit.wait()) if demo else engine.loop, daemon=True)
    worker.start()
    try:
        for line in sys.stdin:
            if len(line) > 16384:
                continue
            try:
                command = json.loads(line)
                engine.action(command)
            except Exception:
                emit("notice", text="操作未完成，请重试。")
            if engine.quit.is_set():
                break
    finally:
        engine.quit.set()
        worker.join(timeout=2)
        engine.close()


def inspect_companion_status(pid, fix_wal=False, fix_reader=False):
    """Observe status events from our own bundled engine, without keys or messages."""
    import frida
    actual = run(["/bin/ps", "-p", str(pid), "-o", "comm="], 4).stdout.decode().strip()
    base = Path("/tmp/wecom-companion-rebuild").resolve()
    if not Path(actual).resolve().is_relative_to(base) or not actual.endswith("/Contents/Resources/Engine/companion-engine"):
        raise ValueError("状态诊断仅允许独立构建的本地伴侣进程")
    session = frida.get_local_device().attach(pid)
    try:
        patch = ""
        if fix_wal or fix_reader:
            import ast
            source = (base / "engine.py").read_text()
            names = {"apply_wal"}
            if fix_reader:
                names.update({"protobuf_fields", "decode_wecom_text", "readable_content", "message_segments", "build_context"})
            snippets = [ast.get_source_segment(source, n) for n in ast.parse(source).body
                        if isinstance(n, ast.FunctionDef) and n.name in names]
            code = "import __main__\nexec(" + repr("\n\n".join(snippets)) + ", __main__.__dict__)\n"
            if fix_reader:
                code += "import gc\nfor obj in gc.get_objects():\n    if isinstance(obj, __main__.Engine):\n        obj.signature = None\n"
            patch = """
            const ensure = new NativeFunction(Module.getGlobalExportByName('PyGILState_Ensure'), 'int', []);
            const release = new NativeFunction(Module.getGlobalExportByName('PyGILState_Release'), 'void', ['int']);
            const run = new NativeFunction(Module.getGlobalExportByName('PyRun_SimpleString'), 'int', ['pointer']);
            const gil = ensure();
            const result = run(Memory.allocUtf8String(%s));
            release(gil);
            send({code:'reader_patch', applied:result === 0});
            """ % json.dumps(code)
        script = session.create_script("""
        let buffered = '';
        const seen = new Set();
        for (const name of ['write', 'write$NOCANCEL']) {
          const address = Module.findGlobalExportByName(name);
          if (!address || seen.has(address.toString())) continue;
          seen.add(address.toString());
          Interceptor.attach(address, {onEnter(args) {
            if (args[0].toInt32() !== 1) return;
            const length = args[2].toUInt32();
            if (length > 1048576) return;
            try {
              buffered += args[1].readUtf8String(length);
              const lines = buffered.split('\\n'); buffered = lines.pop();
              for (const line of lines) {
                const event = JSON.parse(line);
                if (event.event === 'status') send({code:event.code, detail:event.detail});
                if (event.event === 'context') {
                  const messages = event.messages || [];
                  send({code:'context', sessions:(event.sessions || []).length, messages:messages.length,
                        text_messages:messages.filter(x=>x.text && !x.text.startsWith('[')).length});
                }
              }
              if (buffered.length > 1048576) buffered = '';
            } catch (_) { buffered = ''; }
          }});
        }
        send({code:'probe_ready'});
        """ + patch)
        script.on("message", lambda message, data: emit("companion_diagnostic", **message["payload"])
                  if message.get("type") == "send" else None)
        script.load()
        threading.Event().wait(12)
    finally:
        session.detach()


def inspect_runtime(pid):
    """Inspect only the authorized runtime copy; never print database content."""
    import frida
    expected = ROOT / "Runtime/企业微信.app/Contents/MacOS/企业微信"
    actual = run(["/bin/ps", "-p", str(pid), "-o", "comm="], 4).stdout.decode().strip()
    if actual != str(expected):
        raise ValueError("诊断仅允许已授权的临时企业微信副本")
    session = frida.get_local_device().attach(pid)
    try:
        script = session.create_script("""
        for (const m of Process.enumerateModules()) {
          if (!m.path.includes('企业微信.app') && !/crypto|sqlite|commoncrypto/i.test(m.name)) continue;
          const match = x => /md5|sqlite3_key|codec|wxsqlite/i.test(x.name);
          send({type:'module', name:m.name, size:m.size,
                exports:m.enumerateExports().filter(match).map(x=>({name:x.name,offset:x.address.sub(m.base).toString()})),
                symbols:m.enumerateSymbols().filter(match).slice(0,160).map(x=>({name:x.name,offset:x.address.sub(m.base).toString()}))});
        }
        send({type:'done'});
        """)
        done = threading.Event()
        def receive(message, data):
            payload = message.get("payload") or {}
            if payload.get("type") == "done":
                done.set()
            elif message.get("type") == "send":
                emit("diagnostic", **payload)
            else:
                emit("diagnostic_error", description=message.get("description", "unknown"))
        script.on("message", receive)
        script.load()
        if not done.wait(20):
            raise ValueError("诊断未按时完成")
    finally:
        session.detach()


def capture_fixture_test(path):
    """Validate our signed connector on synthetic data; no client databases."""
    import frida
    import signal
    fixture = Path(path).resolve()
    if fixture.name != "capture-fixture":
        raise ValueError("测试模式仅允许指定的独立测试程序")
    device = frida.get_local_device()
    started = time.monotonic()
    pid = device.spawn([str(fixture)])
    session = None
    finished = threading.Event()
    received = []
    try:
        emit("fixture_test", stage="spawned")
        session = device.attach(pid)
        emit("fixture_test", stage="attached")
        script = session.create_script(HOOK)
        script.on("message", lambda message, data: received.append(message))
        session.on("detached", lambda *args: finished.set())
        script.load()
        device.resume(pid)
        if not finished.wait(12):
            raise ValueError("独立测试进程未按时退出")
        payloads = [m.get("payload") or {} for m in received if m.get("type") == "send"]
        installed = next((p.get("count", 0) for p in payloads if p.get("type") == "installed"), 0)
        candidates = [p for p in payloads if p.get("type") == "candidate"]
        matched = {p.get("value") for p in candidates} == {
            "102030405060708090a0b0c0d0e0f001", "222030405060708090a0b0c0d0e0f001",
            "442030405060708090a0b0c0d0e0f001"
        } and len(candidates) == 3
        emit("fixture_test", stage="finished", hooks=installed, synthetic_key_matched=matched,
             elapsed=round(time.monotonic() - started, 2))
        if not installed or not matched:
            raise ValueError("独立取钥监听测试未通过")
    finally:
        try:
            os.kill(pid, signal.SIGTERM)
            os.kill(pid, signal.SIGCONT)
        except ProcessLookupError:
            pass
        if session:
            try:
                session.detach()
            except Exception:
                pass


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        import frida
        sample = aes_for(b"\0" * 16, 1)
        encrypted = sample.encrypt(b"\0" * 16)
        emit("selftest", python=sys.version.split()[0], frida=frida.__version__,
             aes_roundtrip=aes_for(b"\0" * 16, 1).decrypt(encrypted) == b"\0" * 16,
             transport="inherited-pipes", network_listener=False)
    elif "--inspect-companion-status" in sys.argv:
        inspect_companion_status(int(sys.argv[sys.argv.index("--inspect-companion-status") + 1]),
                                 "--fix-wal" in sys.argv, "--fix-reader" in sys.argv)
    elif "--inspect-runtime" in sys.argv:
        inspect_runtime(int(sys.argv[sys.argv.index("--inspect-runtime") + 1]))
    elif "--capture-fixture-test" in sys.argv:
        capture_fixture_test(sys.argv[sys.argv.index("--capture-fixture-test") + 1])
    else:
        main()
