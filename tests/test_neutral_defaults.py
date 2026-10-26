"""Portable defaults, account-neutral source policy and multi-folder file access."""

from __future__ import annotations

from pathlib import Path

import pytest

from udb import config
from udb.companion.files import ScopedFiles
from udb.computer_files import Files
from udb.core import Agent, PolicyError
from udb.path_policy import is_data_container


def portable_tree(tmp_path):
    home = tmp_path / "renamed-portable-installation"
    package = home / "engines" / "package"
    package.mkdir(parents=True)
    for name, content in (
        ("udb.toml", "[bridge]\n"),
        ("plugin.json", "{}"),
        ("mcp.json", "{}"),
    ):
        (home / name).write_text(content, encoding="utf-8")
    return home, package


def test_explicit_environment_home_wins_over_discovery(tmp_path, monkeypatch):
    selected = tmp_path / "chosen-home"
    monkeypatch.setenv("UDB_HOME", str(selected))

    def no_discovery(_):
        raise AssertionError("Explicit home must not inspect portable installations")

    monkeypatch.setattr(config, "_portable_home", no_discovery)
    assert config.default_home() == selected.absolute()
    assert not selected.exists()


def test_renamed_portable_installation_is_discovered_from_package(
    tmp_path, monkeypatch
):
    home, package = portable_tree(tmp_path)
    monkeypatch.delenv("UDB_HOME", raising=False)
    monkeypatch.setattr(config, "__file__", str(package / "config.py"))
    monkeypatch.setattr(config.sys, "frozen", False, raising=False)
    monkeypatch.chdir(tmp_path)
    assert config.default_home() == home


def test_frozen_installation_is_discovered_from_executable(tmp_path, monkeypatch):
    home, _ = portable_tree(tmp_path)
    release = home / "releases" / "current"
    release.mkdir(parents=True)
    monkeypatch.delenv("UDB_HOME", raising=False)
    monkeypatch.setattr(config.sys, "frozen", True, raising=False)
    monkeypatch.setattr(config.sys, "executable", str(release / "bridge.exe"))
    assert config.default_home() == home


def test_incomplete_markers_do_not_select_an_unrelated_parent(tmp_path):
    (tmp_path / "udb.toml").write_text("[bridge]\n")
    assert config._portable_home(tmp_path) is None


@pytest.mark.parametrize(
    "platform,variable,leaf",
    [
        ("win32", "LOCALAPPDATA", "UniversalDesktopBridge"),
        ("linux", "XDG_DATA_HOME", "universal-desktop-bridge"),
    ],
)
def test_standalone_home_uses_the_current_account_data_location(
    tmp_path, monkeypatch, platform, variable, leaf
):
    base = tmp_path / "account-data"
    monkeypatch.delenv("UDB_HOME", raising=False)
    monkeypatch.setenv(variable, str(base))
    monkeypatch.setattr(config.sys, "platform", platform)
    monkeypatch.setattr(config, "_portable_home", lambda _: None)
    assert config.default_home() == base / leaf
    assert not base.exists()


@pytest.mark.parametrize(
    "platform,parts",
    [
        ("win32", ("AppData", "Local", "UniversalDesktopBridge")),
        ("linux", (".local", "share", "universal-desktop-bridge")),
    ],
)
def test_missing_account_environment_has_a_per_user_fallback(
    tmp_path, monkeypatch, platform, parts
):
    monkeypatch.delenv("UDB_HOME", raising=False)
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(config.sys, "platform", platform)
    monkeypatch.setattr(config, "_portable_home", lambda _: None)
    assert config.default_home() == tmp_path.joinpath(*parts)


def test_clean_scoped_workspace_has_a_generic_default(tmp_path):
    home = tmp_path / "fresh-installation"
    settings = config.load(home=home)
    assert settings.project_root == home / "projects" / "workspace"
    assert not home.exists()


def test_explicit_workspace_is_preserved(tmp_path):
    home = tmp_path / "owner-home"
    home.mkdir()
    chosen = tmp_path / "selected-project"
    (home / "udb.toml").write_text('[bridge]\nproject_root="../selected-project"\n')
    assert config.load(home=home).project_root == chosen


def test_new_agent_has_no_personal_roots_projects_or_grants(tmp_path):
    agent = Agent(tmp_path / "fresh-home")
    with agent.connect() as connection:
        assert list(connection.execute("SELECT root FROM protected")) == []
        assert list(connection.execute("SELECT * FROM projects")) == []
        assert list(connection.execute("SELECT * FROM source_grants")) == []
        assert list(connection.execute("SELECT * FROM sandbox_grants")) == []
    assert not (agent.state / "computer-access.dpapi").exists()
    assert not (agent.state / "source-access").exists()


def test_existing_owner_protection_survives_initialization(tmp_path):
    home = tmp_path / "owner-home"
    private = tmp_path / "owner-selected-private-folder"
    private.mkdir()
    (private / "example.py").write_text("private source")
    Agent(home).protect_local(str(private))
    reopened = Agent(home)
    with reopened.connect() as connection:
        assert [row[0] for row in connection.execute("SELECT root FROM protected")] == [
            str(private.resolve())
        ]
    with pytest.raises(PolicyError, match="Protected root"):
        Files(reopened).dispatch("read", str(private / "example.py"))


def test_general_file_api_is_independent_of_the_scoped_workspace(tmp_path):
    agent = Agent(tmp_path / "home")
    first = tmp_path / "application-one"
    second = tmp_path / "different-location" / "application-two"
    first.mkdir()
    second.mkdir(parents=True)
    previous = config.activate(config.Settings(agent.home, first))
    try:
        files = Files(agent)
        for folder in (first, second):
            path = folder / "example.txt"
            files.dispatch(
                "write", str(path), content="roundtrip", expected_sha256="NEW"
            )
            assert files.dispatch("read", str(path))["content"] == "roundtrip"
        with pytest.raises(PermissionError):
            ScopedFiles(str(first)).read(str(second / "example.txt"))
    finally:
        config.activate(previous)


@pytest.mark.parametrize("name", ["image-editor", "accounting-tool", "research-app"])
def test_project_identity_does_not_choose_access_policy(tmp_path, name):
    agent = Agent(tmp_path / "home")
    root = tmp_path / "source"
    root.mkdir()
    nested = root / name
    nested.mkdir()
    path = nested / "module.py"
    path.write_bytes(b"VALUE = 1\n")
    agent.register_local("project", str(root), writable=True)
    assert agent.read("project", f"{name}/module.py")["text"] == "VALUE = 1\n"
    assert ScopedFiles(str(root)).read(f"{name}/module.py")["content"] == "VALUE = 1\n"


@pytest.mark.parametrize("name", ["alpha_datasets", "beta-backups", "gamma_quarantine"])
def test_data_container_rules_are_generic_across_projects(tmp_path, name):
    agent = Agent(tmp_path / "home")
    root = tmp_path / "source"
    root.mkdir()
    nested = root / name
    nested.mkdir()
    (nested / "example.py").write_text("protected fixture")
    agent.register_local("project", str(root), writable=True)
    assert is_data_container(name.upper())
    with pytest.raises(PolicyError):
        agent.read("project", f"{name}/example.py")
    with pytest.raises(PermissionError):
        ScopedFiles(str(root)).read(f"{name}/example.py")
