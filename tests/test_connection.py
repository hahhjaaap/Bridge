"""Accept only supported tunnel identifiers and official MCP connection URLs."""

import json
from types import SimpleNamespace

import pytest

from udb import desktop
from udb.connection import is_tunnel_id, normalize_tunnel_id

LEGACY = "tunnel_" + "a" * 32
NAMESPACED = "tunnel_eu01_" + "z" * 32


@pytest.mark.parametrize("tunnel", [LEGACY, NAMESPACED])
@pytest.mark.parametrize("form", ["id", "url", "url443"])
def test_id_and_official_url_preserve_the_complete_opaque_id(tunnel, form):
    value = {
        "id": " " + tunnel + " ",
        "url": "https://api.openai.com/v1/mcp/" + tunnel,
        "url443": "https://api.openai.com:443/v1/mcp/" + tunnel + "/",
    }[form]
    assert normalize_tunnel_id(value) == tunnel
    assert is_tunnel_id(tunnel)


@pytest.mark.parametrize(
    "value",
    [
        None,
        "",
        "tunnel_a",
        "tunnel_EU01_" + "a" * 32,
        "tunnel_eu1_" + "a" * 32,
        "tunnel_eu001_" + "a" * 32,
        "tunnel_" + "A" * 32,
        LEGACY + "\nextra",
        "http://api.openai.com/v1/mcp/" + LEGACY,
        "https://api.openai.com.evil.example/v1/mcp/" + LEGACY,
        "https://other.example/v1/mcp/" + LEGACY,
        "https://api.openai.com:8443/v1/mcp/" + LEGACY,
        "https://user@api.openai.com/v1/mcp/" + LEGACY,
        "https://api.openai.com/v1/mcp/" + LEGACY + "?key=PRIVATE_VALUE",
        "https://api.openai.com/v1/mcp/" + LEGACY + "#PRIVATE_VALUE",
        "https://api.openai.com/v1/mcp/%74unnel_" + "a" * 32,
        "PRIVATE_VALUE_" * 100,
    ],
)
def test_invalid_input_is_refused_without_echoing_pasted_values(value):
    with pytest.raises(ValueError) as error:
        normalize_tunnel_id(value)
    assert "PRIVATE_VALUE" not in str(error.value)
    assert not is_tunnel_id(value)


def test_desktop_configuration_stores_the_normalized_namespace_alias(
    tmp_path, monkeypatch
):
    saved = []
    monkeypatch.setattr(desktop, "save_key", lambda key: saved.append(key))
    controller = SimpleNamespace(agent=SimpleNamespace(state=tmp_path))
    fake_key = "SYNTHETIC_RUNTIME_KEY"
    desktop.DesktopController.configure(
        controller, "https://api.openai.com/v1/mcp/" + NAMESPACED, fake_key
    )
    assert json.loads((tmp_path / "connection.json").read_text()) == {
        "tunnel_id": NAMESPACED
    }
    assert saved == [fake_key]
