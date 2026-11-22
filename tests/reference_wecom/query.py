"""Former Python query contract, retained only as a differential test oracle."""
from __future__ import annotations

import base64
import hashlib
import json
import time
from datetime import datetime

from .engine import build_context, columns, message_segments, readable_content, readonly

CAPABILITIES = {
    "sessions": True, "contacts": True, "messages": True, "search": True,
    "pagination": True, "members": False, "media": False, "is_self": False,
    "conversation_kind": False,
}


def cursor(row, account, chat):
    value = [account, chat, float(row["ts"]), row["mid"], row["src"]]
    return base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).decode().rstrip("=")


def parse_cursor(value, account, chat):
    try:
        parts = json.loads(base64.urlsafe_b64decode(str(value) + "=" * (-len(str(value)) % 4)))
        if (len(parts) != 5 or parts[:2] != [account, chat] or
                not isinstance(parts[2], (int, float)) or not all(isinstance(x, str) for x in parts[3:])):
            raise ValueError()
        return parts[2:]
    except Exception:
        raise ValueError("分页游标无效，或不属于当前账号和会话")


def timestamp(value):
    if value is None or value == "":
        return None
    value = str(value)
    if value.endswith("d") and value[:-1].isdigit():
        return time.time() - int(value[:-1]) * 86400
    try:
        return float(value)
    except ValueError:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def names(folder):
    result = {}
    with readonly(folder / "user.db") as db:
        if {"id", "name"} <= columns(db, "user_table"):
            for row in db.execute("SELECT * FROM user_table"):
                r = dict(row)
                result[str(r["id"])] = r.get("real_name") or r.get("name") or str(r["id"])
    return result


def query(engine, path, params):
    with engine.guard:
        account = engine.account
        if path == "/api/status":
            return {"source": "wecom", "account_id": account, "ready": bool(engine.current),
                    "capabilities": CAPABILITIES, "freshness": {"state": "fresh" if engine.current else "unavailable"},
                    "updated": engine.updated, "receive_only": True}
        if not engine.current or not account:
            raise ValueError("企业微信尚未生成已验证的快照，请先完成连接")
        expected = params.get("_account")
        if expected and expected != account:
            raise ValueError("账号已变化，已取消本次读取")
        folder = engine.current
        limit = max(1, min(int(params.get("limit") or 60), 1000))
        base = {"source": "wecom", "account_id": account, "snapshot_revision": engine.signature,
                "freshness": {"state": "fresh"}}
        if path in ("/api/sessions", "/api/active-chat"):
            context = build_context(folder)
            sessions = [{"username": s["id"], "name": s["name"], "kind": "unknown",
                         "last_time": datetime.fromtimestamp(s["ts"]).astimezone().isoformat(),
                         "unread": None} for s in context["sessions"]]
            keyword = str(params.get("keyword") or "").casefold()
            sessions = [s for s in sessions if keyword in (s["name"] + s["username"]).casefold()]
            if params.get("kind") and params["kind"] != "unknown":
                sessions = []
            if path == "/api/active-chat":
                selected = next((s for s in sessions if s["username"] == engine.chosen), None)
                return {**base, "active": selected or (sessions[0] if sessions else None),
                        "candidates": sessions[:int(params.get("top") or 5)], "estimate": True,
                        "signal": "wecom.selected_or_recent",
                        "note": "企业微信：所选会话或最近活跃会话，不代表当前前台窗口。"}
            return {**base, "sessions": sessions[:limit], "total": len(sessions)}
        user_names = names(folder)
        if path == "/api/contacts":
            keyword = str(params.get("keyword") or "").casefold()
            contacts = [{"username": uid, "name": name, "kind": "unknown"}
                        for uid, name in user_names.items() if keyword in (uid + name).casefold()]
            return {**base, "contacts": contacts[:limit]}
        if path not in ("/api/messages", "/api/search"):
            raise NotImplementedError("企业微信暂不支持此能力（成员或媒体读取）")
        chat = str(params.get("chat") or "")
        if path == "/api/messages" and not chat:
            raise ValueError("chat is required")
        conditions, args = [], []
        if chat:
            conditions.append("cid=?")
            args.append(chat)
        if params.get("sender"):
            conditions.append("sender=?")
            args.append(str(params["sender"]))
        for key, operator in (("before", "<"), ("after", ">")):
            if params.get(key) is not None and params[key] != "":
                bound = parse_cursor(params[key], account, chat)
                conditions.append(f"(ts,mid,src) {operator} (?,?,?)")
                args.extend(bound)
        for key, operator in (("since", ">="), ("until", "<=")):
            if params.get(key):
                conditions.append(f"ts {operator} ?")
                args.append(timestamp(params[key]))
        if params.get("types"):
            raise NotImplementedError("企业微信原始类型码尚未映射，暂不支持 types 筛选")
        keyword = str(params.get("keyword") or "").casefold()
        # Decode before matching: content may be a protobuf blob, not a SQL TEXT value.
        results = []
        with readonly(folder / "message.db") as db:
            sql = "SELECT * FROM (" + message_segments(db) + ")"
            if conditions:
                sql += " WHERE " + " AND ".join(conditions)
            sql += " ORDER BY ts DESC,mid DESC,src DESC"
            for row in db.execute(sql, args):
                text = readable_content(row["content"], row["kind"])
                if keyword and keyword not in text.casefold():
                    continue
                results.append((dict(row), text))
                if len(results) > limit:
                    break
        page = results[:limit]
        messages = []
        for row, text in reversed(page):
            identity = row["src"] + ":" + row["mid"]
            messages.append({
                "source": "wecom", "account_id": account, "chat": row["cid"],
                "message_id": identity, "server_id": row["mid"],
                "local_id": int.from_bytes(hashlib.sha256(identity.encode()).digest()[:8], "big") & (2**63 - 1),
                "seq": int(float(row["ts"]) * 1000000),
                "cursor": cursor(row, account, chat),
                "sender": row["sender"], "sender_name": user_names.get(row["sender"], row["sender"]),
                "is_self": None, "kind": "text" if text and not text.startswith("[") else "other",
                "type": row["kind"], "time": datetime.fromtimestamp(row["ts"]).astimezone().isoformat(),
                "text": text, "extra": {"source_table": row["src"], "is_self_unknown": True},
            })
        return {**base, "messages": messages, "has_more": len(results) > limit,
                "next_before": cursor(page[-1][0], account, chat) if page else None,
                "next_after": cursor(page[0][0], account, chat) if page else params.get("after")}
