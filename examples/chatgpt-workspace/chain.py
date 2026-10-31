"""Execute a bounded room command chain in one Python process or warm JSON-lines worker."""

import argparse
import json
import re
import sqlite3
import sys
import time
import room

ALLOWED = {
    "join": {"op", "name", "role"},
    "topic": {"op", "name", "goal"},
    "post": {"op", "author", "topic", "kind", "body", "reply_to", "request_id"},
    "read": {"op", "topic", "after", "limit"},
    "status": {"op"},
    "task": {"op", "title", "topic", "assignee", "request_id"},
    "tasks": {"op", "topic", "state", "limit"},
    "task_update": {"op", "task_id", "state", "expected_revision", "assignee"},
}


def resolve(value, results):
    if isinstance(value, dict):
        if set(value) != {"ref"} or not isinstance(value["ref"], str):
            raise ValueError("References must be {ref: step_index.field}")
        parts = value["ref"].split(".")
        if len(parts) != 2 or not parts[0].isdigit():
            raise ValueError("Reference syntax: 0.id")
        index = int(parts[0])
        if index >= len(results):
            raise ValueError("Reference must point to a completed earlier step")
        return results[index]["result"][parts[1]]
    return value


def validate(req):
    if not isinstance(req, dict) or set(req) - {"id", "steps"}:
        raise ValueError("Envelope requires id and steps")
    rid = req.get("id")
    if not isinstance(rid, str) or not re.fullmatch(r"[a-zA-Z0-9_.:-]{1,100}", rid):
        raise ValueError("id must be a stable 1..100 character identifier")
    steps = req.get("steps")
    if not isinstance(steps, list) or not 1 <= len(steps) <= 24:
        raise ValueError("steps must contain 1..24 operations")
    required = {
        "join": {"name"},
        "topic": {"name", "goal"},
        "post": {"author", "body"},
        "read": set(),
        "status": set(),
        "task": {"title"},
        "tasks": set(),
        "task_update": {"task_id", "state", "expected_revision"},
    }
    for i, step in enumerate(steps):
        if not isinstance(step, dict) or step.get("op") not in ALLOWED:
            raise ValueError("Unsupported operation at step " + str(i))
        op = step["op"]
        if set(step) - ALLOWED[op] or not required[op].issubset(step):
            raise ValueError("Unknown or missing fields at step " + str(i))
        # References are only accepted in reply_to, so text cannot become an instruction.
        for k, v in step.items():
            if isinstance(v, (dict, list)) and k not in {
                "reply_to",
                "task_id",
                "expected_revision",
            }:
                raise ValueError(
                    "References are allowed only for reply_to, task_id and expected_revision"
                )
        if op == "read":
            if type(step.get("after", 0)) is not int or step.get("after", 0) < 0:
                raise ValueError("after must be a nonnegative integer")
            if (
                type(step.get("limit", 20)) is not int
                or not 1 <= step.get("limit", 20) <= 200
            ):
                raise ValueError("limit must be 1..200")
        if op == "post":
            if step.get("kind", "message") not in room.KINDS:
                raise ValueError("Invalid message kind")
            room.check_text(step["body"], 32000, "body")
        if op == "task":
            room.check_text(step["title"], 1000, "title")
        if (
            op in ("tasks", "task_update")
            and step.get("state") is not None
            and step["state"] not in room.TASK_STATES
        ):
            raise ValueError("Invalid task state")
        if "assignee" in step and step["assignee"] is not None:
            room.check_text(step["assignee"], 80, "assignee")
        if op == "tasks" and (
            type(step.get("limit", 200)) is not int
            or not 1 <= step.get("limit", 200) <= 500
        ):
            raise ValueError("Task limit must be 1..500")
        for field in ("name", "author", "topic"):
            if field in step:
                room.check_text(step[field], 80, field)
        for field, maximum in (("role", 500), ("goal", 4000)):
            if field in step:
                room.check_text(step[field], maximum, field)
    return rid, steps


def run(db, req):
    started = time.perf_counter()
    results = []
    try:
        rid, steps = validate(req)
    except (ValueError, TypeError, AttributeError) as exc:
        return {
            "id": req.get("id") if isinstance(req, dict) else None,
            "ok": False,
            "results": [],
            "error": str(exc),
            "executed": 0,
        }
    for index, step in enumerate(steps):
        t = time.perf_counter()
        try:
            op = step["op"]
            if op == "join":
                value = room.join(db, step["name"], step.get("role", "Contributor"))
            elif op == "topic":
                value = room.topic(db, step["name"], step["goal"])
            elif op == "post":
                # Serialize idempotency check and insert across competing writers.
                db.execute("BEGIN IMMEDIATE")
                value = room.post(
                    db,
                    argparse.Namespace(
                        author=step["author"],
                        topic=step.get("topic", "general"),
                        kind=step.get("kind", "message"),
                        body=step["body"],
                        reply_to=resolve(step.get("reply_to"), results),
                        request_id=step.get("request_id") or rid + ":" + str(index),
                    ),
                )
            elif op == "read":
                value = room.read(
                    db,
                    step.get("topic", "general"),
                    step.get("after", 0),
                    step.get("limit", 20),
                )
            elif op == "task":
                value = room.task_create(
                    db,
                    step["title"],
                    step.get("topic", "general"),
                    step.get("assignee"),
                    step.get("request_id") or rid + ":" + str(index),
                )
            elif op == "tasks":
                value = room.task_list(
                    db, step.get("topic"), step.get("state"), step.get("limit", 200)
                )
            elif op == "task_update":
                value = room.task_update(
                    db,
                    resolve(step["task_id"], results),
                    step["state"],
                    resolve(step["expected_revision"], results),
                    step.get("assignee"),
                    "assignee" in step,
                )
            else:
                value = room.status(db)
            results.append(
                {
                    "index": index,
                    "op": op,
                    "ok": True,
                    "result": value,
                    "ms": round((time.perf_counter() - t) * 1000, 3),
                }
            )
        except (
            ValueError,
            TypeError,
            AttributeError,
            KeyError,
            IndexError,
            sqlite3.Error,
        ) as exc:
            db.rollback()
            results.append(
                {"index": index, "op": step["op"], "ok": False, "error": str(exc)}
            )
            return {
                "id": rid,
                "ok": False,
                "results": results,
                "executed": index,
                "failed_index": index,
                "partial_commits": index > 0,
                "ms": round((time.perf_counter() - started) * 1000, 3),
            }
    return {
        "id": rid,
        "ok": True,
        "results": results,
        "executed": len(results),
        "ms": round((time.perf_counter() - started) * 1000, 3),
    }


def emit(db, line):
    try:
        if len(line.encode("utf-8")) > 512000:
            raise ValueError("Request exceeds 512000 bytes")
        req = json.loads(line)
        out = run(db, req)
    except (ValueError, TypeError) as exc:
        out = {"id": None, "ok": False, "error": str(exc), "executed": 0}
    print(json.dumps(out, ensure_ascii=False, separators=(",", ":")), flush=True)
    print("UDB_DONE:" + str(out.get("id")), flush=True)


def selftest():
    import tempfile
    from pathlib import Path
    from concurrent.futures import ThreadPoolExecutor

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "test.sqlite3"
        db = room.connect(path)
        request = {
            "id": "test-chain",
            "steps": [
                {"op": "join", "name": "test"},
                {"op": "post", "author": "test", "body": "first"},
                {
                    "op": "post",
                    "author": "test",
                    "body": "reply",
                    "reply_to": {"ref": "1.id"},
                },
                {"op": "read", "after": 0},
                {"op": "status"},
            ],
        }
        a = run(db, request)
        assert (
            a["ok"]
            and a["results"][2]["result"]["reply_to"] == a["results"][1]["result"]["id"]
        )
        b = run(db, request)
        assert (
            b["ok"] and db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        )
        invalid = run(
            db,
            {
                "id": "bad",
                "steps": [
                    {"op": "join", "name": "must-not-exist"},
                    {"op": "shell", "command": "no"},
                ],
            },
        )
        assert (
            not invalid["ok"]
            and not db.execute(
                "SELECT 1 FROM participants WHERE name='must-not-exist'"
            ).fetchone()
        )
        failure = run(
            db,
            {
                "id": "partial",
                "steps": [
                    {"op": "join", "name": "partial"},
                    {"op": "post", "author": "missing", "body": "invalid"},
                    {"op": "post", "author": "test", "body": "must-not-post"},
                ],
            },
        )
        assert not failure["ok"] and failure["failed_index"] == 1
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 2
        db.close()

        def worker(i):
            d = room.connect(path)
            try:
                return run(
                    d,
                    {
                        "id": "same-retry",
                        "steps": [{"op": "post", "author": "test", "body": "once"}],
                    },
                )
            finally:
                d.close()

        with ThreadPoolExecutor(max_workers=4) as pool:
            outcomes = list(pool.map(worker, range(8)))
        assert all(o["ok"] for o in outcomes)
        db = room.connect(path)
        assert db.execute("SELECT count(*) FROM messages").fetchone()[0] == 3
        task_req = {
            "id": "new-task-chain",
            "steps": [
                {"op": "task", "title": "Review", "assignee": "test"},
                {
                    "op": "task_update",
                    "task_id": {"ref": "0.id"},
                    "state": "doing",
                    "expected_revision": {"ref": "0.revision"},
                },
                {"op": "tasks"},
            ],
        }
        assert run(db, task_req)["ok"]
        assert run(db, task_req)["ok"]
        assert room.task_list(db)["tasks"][0]["state"] == "doing"
        db.close()
    return {
        "ok": True,
        "checks": [
            "5-step chain",
            "earlier-result references",
            "safe post retries",
            "prevalidate before writes",
            "stop on failure",
            "8 concurrent identical requests",
        ],
        "production_data_touched": False,
    }


def main():
    room.configure_output()
    p = argparse.ArgumentParser(description=__doc__)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--json")
    g.add_argument("--worker", action="store_true")
    g.add_argument("--selftest", action="store_true")
    a = p.parse_args()
    if a.selftest:
        print(json.dumps(selftest()), flush=True)
        print("UDB_DONE:selftest", flush=True)
        return
    db = room.connect(room.DB)
    try:
        if a.worker:
            print("UDB_READY", flush=True)
            for line in sys.stdin:
                if line.strip():
                    emit(db, line)
        else:
            emit(db, a.json)
    finally:
        db.close()


if __name__ == "__main__":
    main()
