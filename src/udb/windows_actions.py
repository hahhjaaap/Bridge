"""Shared Windows action names; permission checks remain in their backends."""

UIA_ACTIONS = (
    "snapshot",
    "set_value",
    "invoke",
    "uia_focus",
    "wait_for",
    "read_text",
    "set_checked",
    "select",
    "expand",
    "collapse",
    "scroll_into_view",
)
DESKTOP_ACTIONS = (
    "list",
    "control_status",
    "focus",
    "screenshot",
    "type",
    "key",
    "click",
    "scroll",
    "mouse_move",
    "drag",
    "move_window",
    "resize_window",
    "window_state",
    "close_window",
    "batch",
    "displays",
    "open_chrome_tab",
    *UIA_ACTIONS,
)
UTILITY_ACTIONS = ("clipboard_get", "clipboard_set", "notify", "apps", "launch_app")
READ_ACTIONS = (
    "list",
    "snapshot",
    "displays",
    "wait_for",
    "read_text",
    "control_status",
)
