"""Synthetic SQLite fixture for repeatable UI acceptance; no live account data."""
import sqlite3
import time


def prepare(root):
    folder = root / "demo-data"
    folder.mkdir(exist_ok=True)
    with sqlite3.connect(folder / "user.db") as db:
        db.execute("CREATE TABLE user_table(id TEXT,name TEXT)")
        db.executemany("INSERT INTO user_table VALUES (?,?)", [("u1", "陈雨"), ("u2", "林远")])
    with sqlite3.connect(folder / "session.db") as db:
        db.execute("CREATE TABLE conversation_table(id TEXT,name TEXT,last_message_time REAL)")
        db.executemany("INSERT INTO conversation_table VALUES (?,?,?)",
                       [("demo-project", "项目协作群 · 演示", time.time()),
                        ("demo-other", "林远 · 演示", time.time()-1000)])
    with sqlite3.connect(folder / "message.db") as db:
        db.execute("CREATE TABLE message_table(conversation_id TEXT,message_id TEXT,sender_id TEXT,send_time REAL,content TEXT)")
        texts = ["新版方案已整理好，大家可以先看一下。",
                 "收到，我会在今天下午确认交互细节。",
                 "请同步接口联调进度，明天上午一起验收。",
                 "API 配置页支持模型选择和连接测试，回复内容只生成草稿。"]
        db.executemany("INSERT INTO message_table VALUES (?,?,?,?,?)",
                       [("demo-project", str(i), "u1" if i % 2 else "u2",
                         time.time()-(219-i)*60, texts[i % 4]) for i in range(220)] +
                       [("demo-other", "300", "u2", time.time()-1000, "稍后一起核对下周安排。")])
    return folder
