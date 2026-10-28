"""Explicit browser action routing; no arbitrary method or JavaScript dispatch."""

from .core import PolicyError

BROWSER_ACTIONS = (
    "open",
    "upload",
    "download",
    "scroll",
    "list",
    "new",
    "navigate",
    "read",
    "snapshot",
    "wait_for",
    "click",
    "fill",
    "press",
    "select_option",
    "check",
    "uncheck",
    "back",
    "forward",
    "reload",
    "close_tab",
)


def dispatch_browser(obj, args):
    action = args.get("action")
    if action == "scroll":
        return obj.scroll(
            args.get("tab_id", ""), args.get("delta_x", 0), args.get("delta_y", 500)
        )
    if action == "list":
        return dict(tabs=obj.listing())
    if action == "new":
        return obj.new(args.get("url", "about:blank"))
    tab = args.get("tab_id", "")
    if action == "navigate":
        return obj.navigate(tab, args.get("url", ""))
    if action == "read":
        return obj.read(tab)
    if action == "snapshot":
        return obj.snapshot(tab)
    if action == "wait_for":
        return obj.wait_for(
            tab,
            args.get("selector", ""),
            args.get("role", ""),
            args.get("name", ""),
            args.get("exact", True),
            args.get("state", "visible"),
            args.get("value", ""),
            args.get("timeout_ms", 5000),
        )
    if action in ("fill", "click", "select_option", "check", "uncheck"):
        return obj.element(
            tab,
            action,
            args.get("selector", ""),
            args.get("value", ""),
            args.get("role", ""),
            args.get("name", ""),
            args.get("exact", True),
        )
    if action == "press":
        return obj.press(
            tab,
            args.get("key", ""),
            args.get("selector", ""),
            args.get("role", ""),
            args.get("name", ""),
            args.get("exact", True),
        )
    if action in ("back", "forward", "reload"):
        return obj.history(tab, action)
    if action == "screenshot":
        return dict(png_base64=obj.screenshot(tab))
    if action == "close_tab":
        return obj.close_tab(tab)
    raise PolicyError("Unsupported browser action")
