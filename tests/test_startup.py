"""The single start command reuses choices without prompting automation."""

import io
import json
from types import SimpleNamespace

import pytest

from udb import cli, config, onboarding, owner_command
from udb.config import Settings


class Console(io.StringIO):
    def isatty(self):
        return True


def route(monkeypatch, *, interactive=True):
    monkeypatch.setattr(onboarding, "os", SimpleNamespace(name="nt"))
    stream = Console if interactive else io.StringIO
    monkeypatch.setattr(onboarding.sys, "stdin", stream())
    monkeypatch.setattr(onboarding.sys, "stdout", stream())
    calls, setups = [], []
    monkeypatch.setattr(owner_command, "main", lambda args: calls.append(args) or 0)
    monkeypatch.setattr(
        onboarding,
        "run",
        lambda settings, **options: setups.append((settings, options)) or 0,
    )
    return calls, setups


def test_first_interactive_start_opens_setup_without_creating_home(
    tmp_path, monkeypatch
):
    calls, setups = route(monkeypatch)
    home = tmp_path / "first run"
    previous = config._active
    assert cli.main(["start", "--home", str(home)]) == 0
    assert not calls and len(setups) == 1 and setups[0][0].home == home
    assert config._active is previous and not home.exists()


@pytest.mark.parametrize("flags", [["--json"], ["--detach"], ["--access", "computer"]])
def test_explicit_machine_or_owner_commands_never_enter_automatic_setup(
    tmp_path, monkeypatch, flags
):
    calls, setups = route(monkeypatch)
    home = tmp_path / "no prompts"
    assert cli.main(["start", "--home", str(home), *flags]) == 0
    assert len(calls) == 1 and not setups and not home.exists()


def test_redirected_start_never_enters_setup(tmp_path, monkeypatch):
    calls, setups = route(monkeypatch, interactive=False)
    home = tmp_path / "redirected"
    assert cli.main(["start", "--home", str(home)]) == 0
    assert len(calls) == 1 and not setups and not home.exists()


def test_saved_connection_reuses_normal_start_without_a_setup_prompt(
    tmp_path, monkeypatch
):
    calls, setups = route(monkeypatch)
    home = tmp_path / "existing"
    (home / "state").mkdir(parents=True)
    data = json.dumps({"tunnel_id": "tunnel_" + "a" * 32})
    connection = home / "state/connection.json"
    connection.write_text(data)
    assert cli.main(["start", "--home", str(home)]) == 0
    assert len(calls) == 1 and not setups and "--offline" not in calls[0]
    assert connection.read_text() == data


def test_offline_choice_is_reused_even_by_machine_readable_start(tmp_path, monkeypatch):
    calls, setups = route(monkeypatch, interactive=False)
    home = tmp_path / "local"
    settings = Settings(home, home / "workspace")
    onboarding.save_mode(settings, "offline")
    assert cli.main(["start", "--home", str(home), "--json"]) == 0
    assert len(calls) == 1 and "--offline" in calls[0] and not setups
    assert onboarding.saved_mode(settings) == "offline"
    assert not (home / "state/connection.json").exists()
    assert not (home / "state/computer-access.dpapi").exists()


def test_explicit_offline_start_can_bypass_a_damaged_preference(tmp_path, monkeypatch):
    calls, setups = route(monkeypatch)
    home = tmp_path / "local"
    (home / "state").mkdir(parents=True)
    preference = home / "state/startup-preferences.json"
    preference.write_text("damaged")
    assert cli.main(["start", "--home", str(home), "--offline"]) == 0
    assert "--offline" in calls[0] and not setups
    assert preference.read_text() == "damaged"


def test_invalid_saved_mode_does_not_start_online_or_echo_its_content(
    tmp_path, monkeypatch, capsys
):
    calls, setups = route(monkeypatch)
    home = tmp_path / "invalid"
    (home / "state").mkdir(parents=True)
    data = '{"schema":1,"mode":"SYNTHETIC_PRIVATE_VALUE"}'
    preference = home / "state/startup-preferences.json"
    preference.write_text(data)
    assert cli.main(["start", "--home", str(home)]) == 2
    assert not calls and not setups and preference.read_text() == data
    error = capsys.readouterr().err
    assert "SYNTHETIC_PRIVATE_VALUE" not in error and "udb setup" in error


@pytest.mark.parametrize(
    "flag,options",
    [
        ("--edit", {"edit": True, "local": False}),
        ("--local", {"edit": False, "local": True}),
    ],
)
def test_setup_options_route_only_to_the_attended_wizard(
    tmp_path, monkeypatch, flag, options
):
    calls, setups = route(monkeypatch)
    assert cli.main(["setup", "--home", str(tmp_path), flag]) == 0
    assert not calls and setups[0][1] == options


def test_canceling_first_run_restores_settings_and_creates_nothing(
    tmp_path, monkeypatch, capsys
):
    calls, setups = route(monkeypatch)

    def cancel(settings):
        raise KeyboardInterrupt()

    monkeypatch.setattr(onboarding, "run", cancel)
    home = tmp_path / "cancelled"
    previous = config._active
    assert cli.main(["start", "--home", str(home)]) == 130
    assert not home.exists() and not calls and config._active is previous
    assert "Setup canceled" in capsys.readouterr().err


@pytest.mark.parametrize(
    "active,preference", [("online", "offline"), ("offline", "online")]
)
def test_start_reuses_an_active_agent_without_changing_its_saved_preference(
    tmp_path, monkeypatch, active, preference
):
    calls, setups = route(monkeypatch)
    home = tmp_path / "active"
    settings = Settings(home, home / "workspace")
    onboarding.save_mode(settings, preference)
    monkeypatch.setattr(
        owner_command, "headless_state", lambda _: {"connection_mode": active}
    )
    assert cli.main(["start", "--home", str(home)]) == 0
    assert not setups and ("--offline" in calls[-1]) == (active == "offline")
    assert onboarding.saved_mode(settings) == preference


def test_offline_agent_from_an_older_release_needs_no_new_setup(tmp_path, monkeypatch):
    calls, setups = route(monkeypatch)
    monkeypatch.setattr(
        owner_command, "headless_state", lambda _: {"connection_mode": "offline"}
    )
    home = tmp_path / "older"
    assert cli.main(["start", "--home", str(home)]) == 0
    assert not setups and "--offline" in calls[-1]
    assert not home.exists()
