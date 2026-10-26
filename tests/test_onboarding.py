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


def test_mcp_url_setup_saves_namespaced_id_and_shows_account_steps(
    tmp_path, monkeypatch, capsys
):
    tunnel = "tunnel_eu01_" + "c" * 32
    calls = local(monkeypatch, ["y", "https://api.openai.com/v1/mcp/" + tunnel, "n"])
    secret = "SYNTHETIC_SETUP_KEY_DO_NOT_LOG"
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
    assert saved == [(tunnel, secret)] and "--offline" not in calls[-1]
    output = capsys.readouterr().out
    assert all(
        url in output
        for url in (onboarding.TUNNELS_URL, onboarding.KEYS_URL, onboarding.CHATGPT_URL)
    )
    assert secret not in output and secret not in repr(calls)


@pytest.mark.parametrize("answers", [["y", ""], ["y", "?", ""]])
def test_empty_tunnel_or_help_then_cancel_starts_only_offline(
    tmp_path, monkeypatch, answers
):
    calls = local(monkeypatch, answers)
    monkeypatch.setattr(
        onboarding.getpass, "getpass", lambda _: pytest.fail("Must not request a key")
    )
    home = tmp_path / "owner"
    assert onboarding.run(Settings(home, home / "work")) == 0
    assert "--offline" in calls[-1] and "--access" not in calls[-1]
    assert not (home / "state/connection.json").exists()


def test_empty_key_does_not_save_partial_connection(tmp_path, monkeypatch):
    calls = local(monkeypatch, ["y", "tunnel_" + "a" * 32])
    monkeypatch.setattr(onboarding.getpass, "getpass", lambda _: "")
    monkeypatch.setattr(
        desktop,
        "DesktopController",
        lambda *a, **k: pytest.fail("Must not create credential controller"),
    )
    home = tmp_path / "owner"
    assert onboarding.run(Settings(home, home / "work")) == 0
    assert "--offline" in calls[-1] and "--access" not in calls[-1]
    assert not (home / "state/connection.json").exists()


def test_existing_namespaced_connection_is_reused_without_requesting_key(
    tmp_path, monkeypatch
):
    calls = local(monkeypatch, ["n"])
    home = tmp_path / "owner"
    (home / "state").mkdir(parents=True)
    connection = home / "state/connection.json"
    data = '{"tunnel_id":"tunnel_us01_' + "b" * 32 + '"}'
    connection.write_text(data)
    monkeypatch.setattr(
        onboarding.getpass,
        "getpass",
        lambda _: pytest.fail("Must not request or read the existing key"),
    )
    assert onboarding.run(Settings(home, home / "work")) == 0
    assert connection.read_text() == data and "--offline" not in calls[-1]
