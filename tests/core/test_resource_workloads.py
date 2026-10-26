"""Regressions for light file operations during unrelated machine pressure."""

import time

import pytest

from udb.core import PolicyError
from udb.resources import Governor, require_capacity, require_light_capacity


def sample(**overrides):
    row = dict(
        at=time.time(),
        cpu_percent=8,
        ram_percent=59,
        available_mib=6600,
        commit_percent=82,
        commit_available_mib=4000,
    )
    row.update(overrides)
    return row


def test_cpu_spike_recovers_without_demanding_unrelated_commit_drop():
    gov = Governor()
    assert gov.update(sample(cpu_percent=92))["pressure_causes"] == ["cpu"]
    assert not gov.update(sample())["admission_ready"]
    assert not gov.update(sample())["admission_ready"]
    assert gov.update(sample())["admission_ready"]


def test_real_commit_pressure_keeps_its_own_recovery_threshold():
    gov = Governor()
    assert gov.update(sample(commit_percent=94))["paused"]
    for _ in range(8):
        assert not gov.update(sample())["admission_ready"]
    for _ in range(2):
        assert not gov.update(sample(commit_percent=78))["admission_ready"]
    assert gov.update(sample(commit_percent=78))["admission_ready"]


def test_new_pressure_is_added_during_recovery():
    gov = Governor()
    gov.update(sample(cpu_percent=92))
    gov.update(sample(ram_percent=91))
    for _ in range(5):
        assert not gov.update(sample(ram_percent=79))["admission_ready"]
    for _ in range(2):
        assert not gov.update(sample())["admission_ready"]
    assert gov.update(sample())["admission_ready"]


def test_light_file_operation_allowed_but_heavy_launch_refused_during_cpu_pressure():
    status = Governor().update(sample(cpu_percent=96))
    require_light_capacity(status)
    assert status["light_operations_ready"]
    with pytest.raises(PolicyError):
        require_capacity(status)


@pytest.mark.parametrize(
    "changes",
    [
        {"at": time.time() - 7},
        {"at": time.time() + 3600},
        {"available_mib": 300},
        {"commit_available_mib": 180},
        {"commit_available_mib": None},
        {"commit_available_mib": float("nan")},
        {"available_mib": float("inf")},
    ],
)
def test_light_operations_still_refuse_stale_unknown_or_critical_memory(changes):
    with pytest.raises(PolicyError):
        require_light_capacity(sample(**changes))


def test_heavy_launch_checks_absolute_commit_budget_even_at_low_percentage():
    status = Governor().update(sample(commit_percent=40, commit_available_mib=600))
    with pytest.raises(PolicyError, match="commit"):
        require_capacity(status, 256)


def test_computer_file_dispatch_uses_light_gate_while_execute_uses_heavy_gate(
    tmp_path, monkeypatch
):
    from udb import computer_access
    from udb.computer import Computer

    monkeypatch.setattr(computer_access, "require", lambda _: "isolated-test-token")

    class NoJobs:
        def __init__(self, *_):
            pass

        def start(self, **_):
            pytest.fail("Busy machine must not start a program")

        def close(self):
            pass

    from udb.core import Agent

    agent = Agent(tmp_path / "agent")
    computer = Computer(agent, jobs_factory=NoJobs)
    target = tmp_path / "readback.txt"
    target.write_text("resource-regression")
    status = Governor().update(sample(cpu_percent=95))
    assert (
        computer.dispatch(
            "files", {"action": "read", "path": str(target), "limit": 100}, status
        )["content"]
        == "resource-regression"
    )
    with pytest.raises(PolicyError):
        computer.dispatch("execute", {}, status)
    assert not (agent.state / "computer-access.dpapi").exists()
    computer.close()
