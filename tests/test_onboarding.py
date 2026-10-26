"""Only the attended local owner can supply connection details or opt in."""

import io
from types import SimpleNamespace

import pytest

from udb import cli, desktop, onboarding, owner_command
from udb.config import Settings


class Console(io.StringIO):
    def isatty(self):
        return True


def local(monkeypatch, answers):
    values = iter(answers)
    monkeypatch.setattr(onboarding, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(onboarding.sys, "stdin", Console())
    monkeypatch.setattr("builtins.input", lambda _: next(values))
    calls = []
    monkeypatch.setattr(owner_command, "main", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(owner_command, "headless_state", lambda _: None)
    return calls


def test_redirected_setup_cannot_create_state_or_prompt_for_secrets(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(onboarding, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(onboarding.sys, "stdin", io.StringIO("y\n"))
    home = tmp_path / "unused"
    with pytest.raises(ValueError, match="attended"):
        onboarding.run(Settings(home, home / "work"))
    assert not home.exists()


def test_setup_json_is_refused_before_any_state_write(tmp_path):
    home = tmp_path / "unused"
    assert cli.main(["--json", "setup", "--home", str(home)]) == 2
    assert not home.exists()


def test_offline_setup_starts_local_runtime_without_owner_access(tmp_path, monkeypatch):
    calls = local(monkeypatch, ["n"])
    home = tmp_path / "owner"
    assert onboarding.run(Settings(home, home / "work")) == 0
    assert "--offline" in calls[-1] and "--follow" in calls[-1]
    assert "--access" not in calls[-1]
    assert not (home / "state/connection.json").exists()
    assert not (home / "state/computer-access.dpapi").exists()


def test_invalid_tunnel_is_refused_before_reading_key_or_creating_home(
    tmp_path, monkeypatch
):
    calls = local(monkeypatch, ["y", "not-a-tunnel"])
    home = tmp_path / "unused"
    monkeypatch.setattr(
        onboarding.getpass, "getpass", lambda _: pytest.fail("Must not request a key")
    )
    with pytest.raises(ValueError, match="Invalid tunnel"):
        onboarding.run(Settings(home, home / "work"))
    assert not calls and not home.exists()


def test_local_key_is_not_echoed_or_forwarded_in_process_arguments(
    tmp_path, monkeypatch, capsys
):
    tunnel = "tunnel_" + "a" * 32
    calls = local(monkeypatch, ["y", tunnel, "n"])
    secret = "SYNTHETIC_LOCAL_KEY_DO_NOT_LOG"
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda _: secret)
    saved = []

    class Controller:
        def __init__(self, home, monitor):
            assert monitor is False

        def configure(self, tunnel_id, key):
            saved.append((tunnel_id, key))

        def close(self):
            pass

    monkeypatch.setattr(desktop, "DesktopController", Controller)
    home = tmp_path / "owner"
    assert onboarding.run(Settings(home, home / "work")) == 0
    assert saved == [(tunnel, secret)]
    assert secret not in capsys.readouterr().out
    assert all(secret not in arg for call in calls for arg in call)
    assert "--access" not in calls[-1]


def test_existing_configuration_needs_no_key_or_connection_overwrite(
    tmp_path, monkeypatch
):
    calls = local(monkeypatch, ["n"])
    home = tmp_path / "owner"
    (home / "state").mkdir(parents=True)
    connection = home / "state/connection.json"
    data = '{"tunnel_id":"tunnel_' + "b" * 32 + '"}'
    connection.write_text(data)
    monkeypatch.setattr(
        onboarding.getpass,
        "getpass",
        lambda _: pytest.fail("Existing key must not be read or requested"),
    )
    monkeypatch.setattr(
        owner_command, "headless_state", lambda _: {"connection_mode": "offline"}
    )
    assert onboarding.run(Settings(home, home / "work")) == 0
    assert [call[0] for call in calls] == ["stop", "start"]
    assert connection.read_text() == data
    assert "--access" not in calls[-1]
