"""Regression tests for core service extraction and bounded policy operations."""

import json

from udb.core import Agent, PolicyError, canonical, digest


def owner(tmp_path):
    agent = Agent(tmp_path / "bridge")
    root = tmp_path / "source"
    root.mkdir()
    agent.register_local("demo", str(root), True)
    return agent, root


def test_owner_grants_still_use_original_agent_api(tmp_path):
    agent, root = owner(tmp_path)
    agent.set_sandbox_autonomy_local("demo", True)
    token = agent.sandbox_authorization("demo", str(root))
    assert isinstance(token, str) and len(token) == 32
    queued = agent.propose("job", {"project": "demo", "root": str(root)})
    assert queued["status"] == "queued_sandbox"
    with agent.connect() as db:
        row = db.execute(
            "SELECT result FROM requests WHERE id=?", (queued["request_id"],)
        ).fetchone()
    assert json.loads(row["result"])["authorization"] == token
    agent.set_sandbox_autonomy_local("demo", False)
    assert not agent.sandbox_authorization("demo", str(root))
    assert Agent.__module__ == "udb.core"
    assert issubclass(PolicyError, ValueError)
    assert digest(canonical({"a": 1}).encode()) == digest(b'{"a":1}')


def test_queue_indexes_exist_on_existing_schema(tmp_path):
    agent, _ = owner(tmp_path)
    with agent.connect() as db:
        indexes = {
            row["name"]
            for row in db.execute("SELECT name FROM sqlite_master WHERE type='index'")
        }
        assert "idx_requests_status_created" in indexes
        assert "idx_jobs_status" in indexes
        plan = " ".join(
            str(row["detail"])
            for row in db.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM requests "
                "WHERE status='queued_sandbox' ORDER BY created LIMIT 1"
            )
        )
        assert "idx_requests_status_created" in plan
    # Creating another Agent against an existing database is a safe migration.
    assert Agent(agent.home).projects()[0]["id"] == "demo"


def test_approval_diff_is_complete_for_small_edits(tmp_path):
    agent, root = owner(tmp_path)
    (root / "invoice.py").write_text("old=1\n", encoding="utf-8")
    previous = agent.read("demo", "invoice.py")["sha256"]
    response = agent.propose_write("demo", "invoice.py", "old=2\n", previous)
    assert "-old=1" in response["diff"]
    assert "+old=2" in response["diff"]
    assert "truncated" not in response["diff"]
    assert (root / "invoice.py").read_text(encoding="utf-8") == "old=1\n"


def test_approval_diff_omitted_for_many_lines_without_losing_payload(tmp_path):
    agent, root = owner(tmp_path)
    previous = "old=1\n" * 5000
    next_text = "old=2\n" * 5000
    (root / "invoice.py").write_text(previous, encoding="utf-8")
    response = agent.propose_write(
        "demo", "invoice.py", next_text, digest((root / "invoice.py").read_bytes())
    )
    assert "Diff preview omitted" in response["diff"]
    assert "Review the complete" in response["diff"]
    assert len(response["diff"]) < 1000
    with agent.connect() as db:
        payload = json.loads(
            db.execute(
                "SELECT payload FROM requests WHERE id=?",
                (response["request_id"],),
            ).fetchone()[0]
        )
    assert payload["text"] == next_text
    assert (root / "invoice.py").read_text(encoding="utf-8") == previous


def test_approval_diff_truncation_is_explicit(tmp_path):
    agent, root = owner(tmp_path)
    previous = "".join(f"before-{i:04d} abcdefghijklmnop\n" for i in range(900))
    new = "".join(f"after-{i:04d} abcdefghijklmnop\n" for i in range(900))
    (root / "invoice.py").write_text(previous, encoding="utf-8")
    response = agent.propose_write(
        "demo", "invoice.py", new, digest((root / "invoice.py").read_bytes())
    )
    assert "Diff preview truncated" in response["diff"]
    assert len(response["diff"]) <= 16000


def test_listing_filters_unsupported_files_without_policy_checks(tmp_path, monkeypatch):
    agent, root = owner(tmp_path)
    for index in range(120):
        (root / f"raw_{index:03d}.bin").write_bytes(b"do not expose")
    (root / "report.py").write_text("print(1)\n", encoding="utf-8")
    original = agent.path
    checked = []

    def guarded_path(project, relative=".", write=False):
        checked.append(relative)
        return original(project, relative, write)

    monkeypatch.setattr(agent, "path", guarded_path)
    listed = agent.listing("demo")
    assert listed["items"] == [{"path": "report.py", "directory": False}]
    assert checked == [".", "report.py"]
