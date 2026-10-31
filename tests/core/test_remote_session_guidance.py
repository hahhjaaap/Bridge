"""Remote-device troubleshooting stays honest about what local probes prove."""

import pytest

from udb.owner_command import remote_session_action


@pytest.mark.parametrize(
    ("local_ready", "expected"),
    [
        (False, "local tunnel is not ready"),
        (True, "NOT verified"),
    ],
)
def test_remote_guidance_distinguishes_local_and_remote(local_ready, expected):
    action = remote_session_action({"local_tunnel_ready": local_ready}, offline=False)
    assert expected.casefold() in action.casefold()


def test_ready_tunnel_does_not_claim_connected_chatgpt():
    action = remote_session_action({"local_tunnel_ready": True}, offline=False)
    assert "bridge_status" in action
    assert "Session terminated" in action
    assert "Do not replay" in action
    assert "Connected" in action


def test_offline_mode_does_not_recommend_reconnecting_a_remote_session():
    action = remote_session_action({"local_tunnel_ready": False}, offline=True)
    assert "offline" in action
    assert "udb setup" in action
    assert "bridge_status" not in action


def test_missing_tunnel_field_fails_closed_with_local_diagnostics():
    action = remote_session_action({}, offline=False)
    assert "udb doctor --json" in action
    assert "bridge_status" not in action
