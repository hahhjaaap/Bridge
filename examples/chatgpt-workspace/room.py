#!/usr/bin/env python3
"""Shared UDB discussion room. Standard library only; no API key required."""

import argparse
import datetime
import json
import sqlite3
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB = ROOT / "workspace.sqlite3"
KINDS = ("message", "proposal", "question", "review", "decision", "summary")
TASK_STATES = ("todo", "doing", "blocked", "done")


def configure_output():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def connect(path):
    db = sqlite3.connect(str(path), timeout=20)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys=ON")
    if db.execute("PRAGMA user_version").fetchone()[0] >= 3:
        return db
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript("""
    CREATE TABLE IF NOT EXISTS participants(
      name TEXT PRIMARY KEY, role TEXT NOT NULL, last_seen TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS topics(
      name TEXT PRIMARY KEY, goal TEXT NOT NULL, created_at TEXT NOT NULL);
    CREATE TABLE IF NOT EXISTS messages(
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      request_id TEXT UNIQUE NOT NULL,
      topic TEXT NOT NULL REFERENCES topics(name),
      author TEXT NOT NULL REFERENCES participants(name),
      kind TEXT NOT NULL CHECK(kind IN ('message','proposal','question','review','decision','summary')),
      body TEXT NOT NULL, reply_to INTEGER REFERENCES messages(id),
      created_at TEXT NOT NULL);
    CREATE INDEX IF NOT EXISTS messages_topic_id ON messages(topic,id);
    """)
    with db:
        db.execute(
            "INSERT OR IGNORE INTO topics VALUES(?,?,?)",
            ("general", "Phong thao luan chung cua cac chat ChatGPT qua UDB.", now()),
        )
    db.execute("""CREATE TABLE IF NOT EXISTS tasks(
        id INTEGER PRIMARY KEY AUTOINCREMENT, request_id TEXT UNIQUE NOT NULL,
        topic TEXT NOT NULL REFERENCES topics(name), title TEXT NOT NULL,
        assignee TEXT REFERENCES participants(name), initial_assignee TEXT, state TEXT NOT NULL DEFAULT 'todo'
          CHECK(state IN ('todo','doing','blocked','done')),
        revision INTEGER NOT NULL DEFAULT 1, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS tasks_topic_state ON tasks(topic,state,id)")
    if "initial_assignee" not in {
        r["name"] for r in db.execute("PRAGMA table_info(tasks)")
    }:
        db.execute("ALTER TABLE tasks ADD COLUMN initial_assignee TEXT")
        db.execute("UPDATE tasks SET initial_assignee=assignee")
        db.commit()
    db.execute("PRAGMA user_version=3")
    return db


def check_text(value, maximum, label):
    if not value.strip() or len(value) > maximum:
        raise ValueError(
            label + " must be nonempty and <= " + str(maximum) + " characters"
        )
    return value.strip()


def join(db, name, role):
    name = check_text(name, 80, "name")
    role = check_text(role, 500, "role")
    with db:
        db.execute(
            "INSERT INTO participants VALUES(?,?,?) ON CONFLICT(name) DO UPDATE SET role=excluded.role,last_seen=excluded.last_seen",
            (name, role, now()),
        )
    return {"name": name, "role": role}


def topic(db, name, goal):
    name = check_text(name, 80, "topic")
    goal = check_text(goal, 4000, "goal")
    with db:
        old = db.execute("SELECT goal FROM topics WHERE name=?", (name,)).fetchone()
        if old and old["goal"] != goal:
            raise ValueError("Existing topic has a different goal")
        db.execute("INSERT OR IGNORE INTO topics VALUES(?,?,?)", (name, goal, now()))
    return {"topic": name, "goal": goal}


def post(db, args):
    body = check_text(args.body, 32000, "body")
    request_id = args.request_id or str(uuid.uuid4())
    with db:
        previous = db.execute(
            "SELECT * FROM messages WHERE request_id=?", (request_id,)
        ).fetchone()
        if previous:
            if (
                previous["topic"],
                previous["author"],
                previous["kind"],
                previous["body"],
                previous["reply_to"],
            ) != (args.topic, args.author, args.kind, body, args.reply_to):
                raise ValueError("request-id already used for different content")
            return dict(previous)
        if not db.execute(
            "SELECT 1 FROM participants WHERE name=?", (args.author,)
        ).fetchone():
            raise ValueError("Unknown author; run join first")
        if not db.execute(
            "SELECT 1 FROM topics WHERE name=?", (args.topic,)
        ).fetchone():
            raise ValueError("Unknown topic; run topic first")
        if args.reply_to is not None:
            parent = db.execute(
                "SELECT topic FROM messages WHERE id=?", (args.reply_to,)
            ).fetchone()
            if not parent or parent["topic"] != args.topic:
                raise ValueError(
                    "reply-to must reference an existing message in the same topic"
                )
        cur = db.execute(
            "INSERT INTO messages(request_id,topic,author,kind,body,reply_to,created_at) VALUES(?,?,?,?,?,?,?)",
            (
                request_id,
                args.topic,
                args.author,
                args.kind,
                body,
                args.reply_to,
                now(),
            ),
        )
        db.execute(
            "UPDATE participants SET last_seen=? WHERE name=?", (now(), args.author)
        )
        row = db.execute(
            "SELECT * FROM messages WHERE id=?", (cur.lastrowid,)
        ).fetchone()
    return dict(row)


def read(db, topic, after, limit):
    rows = db.execute(
        "SELECT * FROM messages WHERE topic=? AND id>? ORDER BY id LIMIT ?",
        (topic, after, limit),
    ).fetchall()
    return {
        "topic": topic,
        "messages": [dict(r) for r in rows],
        "next_after": rows[-1]["id"] if rows else after,
        "more": bool(
            rows
            and db.execute(
                "SELECT 1 FROM messages WHERE topic=? AND id>? LIMIT 1",
                (topic, rows[-1]["id"]),
            ).fetchone()
        ),
    }


def status(db):
    return {
        "workspace": "ChatGPT Common Room",
        "database": str(DB),
        "participants": [
            dict(r) for r in db.execute("SELECT * FROM participants ORDER BY name")
        ],
        "topics": [
            dict(r)
            for r in db.execute(
                "SELECT t.*,COUNT(m.id) AS messages FROM topics t LEFT JOIN messages m ON m.topic=t.name GROUP BY t.name ORDER BY t.name"
            )
        ],
        "note": "last_seen is last join/post, not a live-presence signal. Chats must explicitly read and post.",
    }


def task_create(db, title, topic="general", assignee=None, request_id=None):
    title = check_text(title, 1000, "title")
    request_id = request_id or str(uuid.uuid4())
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute(
            "SELECT * FROM tasks WHERE request_id=?", (request_id,)
        ).fetchone()
        if old:
            if (old["title"], old["topic"], old["initial_assignee"]) != (
                title,
                topic,
                assignee,
            ):
                raise ValueError("Task request-id already used for different content")
            return dict(old)
        stamp = now()
        row = db.execute(
            "INSERT INTO tasks(request_id,topic,title,assignee,initial_assignee,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
            (request_id, topic, title, assignee, assignee, stamp, stamp),
        )
        return dict(
            db.execute("SELECT * FROM tasks WHERE id=?", (row.lastrowid,)).fetchone()
        )


def task_list(db, topic=None, state=None, limit=200):
    if state is not None and state not in TASK_STATES:
        raise ValueError("Invalid task state")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ValueError("Task limit must be 1..500")
    conditions, params = [], []
    for field, value in (("topic", topic), ("state", state)):
        if value is not None:
            conditions.append(field + "=?")
            params.append(value)
    where = " WHERE " + " AND ".join(conditions) if conditions else ""
    rows = db.execute(
        "SELECT * FROM tasks" + where + " ORDER BY id DESC LIMIT ?",
        params + [limit + 1],
    ).fetchall()
    return {"tasks": [dict(r) for r in rows[:limit]], "more": len(rows) > limit}


def task_update(
    db, task_id, state, expected_revision, assignee=None, change_assignee=False
):
    if state not in TASK_STATES or type(task_id) is not int or task_id < 1:
        raise ValueError("Invalid task id/state")
    if type(expected_revision) is not int or expected_revision < 1:
        raise ValueError("expected_revision must be a positive integer")
    with db:
        db.execute("BEGIN IMMEDIATE")
        old = db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not old:
            raise ValueError("Task not found")
        target_assignee = assignee if change_assignee else old["assignee"]
        if (
            old["revision"] >= expected_revision
            and old["state"] == state
            and old["assignee"] == target_assignee
        ):
            return dict(old)
        if old["revision"] != expected_revision:
            raise ValueError("Task changed; read its latest revision before updating")
        db.execute(
            "UPDATE tasks SET state=?,assignee=?,revision=revision+1,updated_at=? WHERE id=?",
            (state, target_assignee, now(), task_id),
        )
        return dict(db.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())


def selftest():
    import tempfile
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.sqlite3"
        db = connect(path)
        join(db, "test", "tester")
        a = argparse.Namespace(
            topic="general",
            author="test",
            kind="proposal",
            body="one",
            reply_to=None,
            request_id="stable",
        )
        first = post(db, a)
        assert post(db, a)["id"] == first["id"]
        a.body = "conflict"
        try:
            post(db, a)
            raise AssertionError("duplicate conflict accepted")
        except ValueError:
            pass
        a.request_id = "reply"
        a.reply_to = first["id"]
        a.body = "reply"
        assert post(db, a)["reply_to"] == first["id"]
        a.request_id = "invalid"
        a.reply_to = 999999
        try:
            post(db, a)
            raise AssertionError("invalid reply accepted")
        except ValueError:
            pass
        db.close()

        def worker(i):
            d = connect(path)
            try:
                return post(
                    d,
                    argparse.Namespace(
                        topic="general",
                        author="test",
                        kind="message",
                        body=str(i),
                        reply_to=None,
                        request_id="parallel-" + str(i),
                    ),
                )["id"]
            finally:
                d.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            ids = list(pool.map(worker, range(12)))
        assert len(set(ids)) == 12
        db = connect(path)
        page = read(db, "general", 0, 3)
        assert len(page["messages"]) == 3 and page["more"]
        page2 = read(db, "general", page["next_after"], 100)
        assert len(page2["messages"]) == 11
        assert db.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        created = task_create(
            db, "Review architecture", assignee="test", request_id="task-one"
        )
        assert (
            task_create(
                db, "Review architecture", assignee="test", request_id="task-one"
            )["id"]
            == created["id"]
        )
        updated = task_update(db, created["id"], "doing", 1)
        assert updated["revision"] == 2
        assert task_update(db, created["id"], "doing", 1)["revision"] == 2
        try:
            task_update(db, created["id"], "done", 1)
            raise AssertionError("Stale task update accepted")
        except ValueError:
            pass
        assert task_list(db)["tasks"][0]["state"] == "doing"
        db.close()
    return {
        "ok": True,
        "checks": [
            "idempotent retry",
            "conflict rejection",
            "reply validation",
            "12 concurrent writes",
            "cursor pagination",
            "restart persistence",
            "SQLite integrity",
            "task retries",
            "task revision conflicts",
        ],
        "production_data_touched": False,
    }


def main():
    configure_output()
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("status")
    sub.add_parser("selftest")
    p = sub.add_parser("join")
    p.add_argument("--name", required=True)
    p.add_argument("--role", default="Contributor")
    p = sub.add_parser("topic")
    p.add_argument("--name", required=True)
    p.add_argument("--goal", required=True)
    p = sub.add_parser("post")
    p.add_argument("--author", required=True)
    p.add_argument("--topic", default="general")
    p.add_argument("--kind", choices=KINDS, default="message")
    p.add_argument("--body", required=True)
    p.add_argument("--reply-to", type=int)
    p.add_argument(
        "--request-id", help="Reuse the same ID when retrying an uncertain write"
    )
    p = sub.add_parser("read")
    p.add_argument("--topic", default="general")
    p.add_argument("--after", type=int, default=0)
    p.add_argument("--limit", type=int, default=50)
    p = sub.add_parser("export")
    p.add_argument("--topic", default="general")
    args = parser.parse_args()
    try:
        if args.command == "selftest":
            result = selftest()
        else:
            db = connect(DB)
            try:
                if args.command in ("init", "status"):
                    result = status(db)
                elif args.command == "join":
                    result = join(db, args.name, args.role)
                elif args.command == "topic":
                    result = topic(db, args.name, args.goal)
                elif args.command == "post":
                    result = post(db, args)
                elif args.command == "read":
                    if args.after < 0 or not 1 <= args.limit <= 200:
                        raise ValueError(
                            "after >= 0 and limit between 1 and 200 required"
                        )
                    result = read(db, args.topic, args.after, args.limit)
                else:
                    rows = db.execute(
                        "SELECT * FROM messages WHERE topic=? ORDER BY id",
                        (args.topic,),
                    ).fetchall()
                    print("# " + args.topic)
                    for row in rows:
                        print(
                            "\n## #"
                            + str(row["id"])
                            + " | "
                            + row["author"]
                            + " | "
                            + row["kind"]
                            + " | "
                            + row["created_at"]
                        )
                        if row["reply_to"]:
                            print("Reply to #" + str(row["reply_to"]))
                        print("\n" + row["body"])
                    return
            finally:
                db.close()
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except (ValueError, sqlite3.Error, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
