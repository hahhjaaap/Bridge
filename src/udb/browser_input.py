"""Keyboard and browser-local virtual mouse control for owned Chrome.

All pointer and key events are sent through CDP, never through Windows SendInput.
"""

from .core import PolicyError

_KEY_SPECIAL = {
    "enter": ("Enter", "Enter", 13),
    "tab": ("Tab", "Tab", 9),
    "escape": ("Escape", "Escape", 27),
    "esc": ("Escape", "Escape", 27),
    "space": (" ", "Space", 32),
    "arrowdown": ("ArrowDown", "ArrowDown", 40),
    "arrowup": ("ArrowUp", "ArrowUp", 38),
    "arrowleft": ("ArrowLeft", "ArrowLeft", 37),
    "arrowright": ("ArrowRight", "ArrowRight", 39),
    "home": ("Home", "Home", 36),
    "end": ("End", "End", 35),
    "pageup": ("PageUp", "PageUp", 33),
    "pagedown": ("PageDown", "PageDown", 34),
    "backspace": ("Backspace", "Backspace", 8),
    "delete": ("Delete", "Delete", 46),
}
_KEY_COMBOS = frozenset(
    {
        "CTRL+A",
        "CONTROL+A",
        "CTRL+ENTER",
        "CONTROL+ENTER",
        "SHIFT+TAB",
        "CTRL+BACKSPACE",
        "CONTROL+BACKSPACE",
        "CTRL+ARROWLEFT",
        "CONTROL+ARROWLEFT",
        "CTRL+ARROWRIGHT",
        "CONTROL+ARROWRIGHT",
        "CTRL+HOME",
        "CONTROL+HOME",
        "CTRL+END",
        "CONTROL+END",
    }
)


def parse_key(value):
    if not isinstance(value, str) or not 1 <= len(value) <= 40:
        raise PolicyError("Provide a supported browser key")
    parts = value.split("+")
    aliases = {
        "ctrl": "CTRL",
        "control": "CONTROL",
        "shift": "SHIFT",
        "alt": "ALT",
        "meta": "META",
    }
    modifiers = []
    for part in parts[:-1]:
        token = aliases.get(part.lower())
        if token is None or token in modifiers:
            raise PolicyError("Unsupported browser key modifier")
        modifiers.append(token)
    final = parts[-1]
    info = _KEY_SPECIAL.get(final.lower())
    if info is None and len(final) == 1 and final.isalpha():
        info = (final.lower(), "Key" + final.upper(), ord(final.upper()))
    elif info is None and len(final) == 1 and final.isdigit():
        info = (final, "Digit" + final, ord(final))
    if info is None:
        raise PolicyError("Unsupported browser key")
    if modifiers and "+".join(modifiers + [final.upper()]) not in _KEY_COMBOS:
        raise PolicyError("That browser key combination is not allowed")
    mask = 0
    for modifier in modifiers:
        mask |= {"ALT": 1, "CTRL": 2, "CONTROL": 2, "META": 4, "SHIFT": 8}[modifier]
    key, code, vk = info
    text = (
        key if not modifiers and (len(final) == 1 or final.lower() == "space") else ""
    )
    return dict(key=key, code=code, vk=vk, modifiers=mask, text=text)


class BrowserInputMixin:
    """Input methods shared by the UDB-owned browser; no OS cursor takeover."""

    def _mouse_click(self, tab, x, y):
        self._show_identity(tab, x, y)
        session = self.sid(tab)
        point = dict(x=float(x), y=float(y))
        self.call(
            "Input.dispatchMouseEvent",
            dict(type="mouseMoved", **point),
            session,
            timeout=2,
        )
        press_error = None
        try:
            self.call(
                "Input.dispatchMouseEvent",
                dict(type="mousePressed", button="left", clickCount=1, **point),
                session,
                timeout=2,
            )
        except Exception as exc:
            press_error = exc
        finally:
            try:
                self.call(
                    "Input.dispatchMouseEvent",
                    dict(type="mouseReleased", button="left", clickCount=1, **point),
                    session,
                    timeout=2,
                )
            except Exception:
                if press_error is None:
                    raise
        if press_error is not None:
            raise press_error

    def press(self, tab, key, selector="", role="", name="", exact=True):
        info = parse_key(key)
        body = """if(e.matches(':disabled')||e.getAttribute('aria-disabled')==='true'||isInert(e))return {ok:false,error:'disabled'};
          if(typeof e.focus!=='function')return {ok:false,error:'not_focusable'};e.focus();
          return {ok:document.activeElement===e||e.contains(document.activeElement),error:'not_focusable'};"""
        focused = self.evaluate(
            tab, self._locator_expression(selector, role, name, exact, body)
        )
        if not focused.get("ok"):
            return focused
        session = self.sid(tab)
        params = dict(
            key=info["key"],
            code=info["code"],
            windowsVirtualKeyCode=info["vk"],
            modifiers=info["modifiers"],
        )
        down_params = dict(params)
        if info["text"]:
            down_params["text"] = info["text"]
            down_params["unmodifiedText"] = info["text"]
        down_error = None
        try:
            self.call(
                "Input.dispatchKeyEvent",
                dict(type="keyDown", **down_params),
                session,
                timeout=2,
            )
        except Exception as exc:
            down_error = exc
        finally:
            try:
                self.call(
                    "Input.dispatchKeyEvent",
                    dict(type="keyUp", **params),
                    session,
                    timeout=2,
                )
            except Exception:
                if down_error is None:
                    raise
        if down_error is not None:
            raise down_error
        return dict(ok=True, key=key, focused=True)

    def scroll(self, tab, x=0, y=500):
        if (
            type(x) is not int
            or type(y) is not int
            or not -2000 <= x <= 2000
            or not -2000 <= y <= 2000
        ):
            raise PolicyError("Browser scroll deltas must be integers in -2000..2000")
        session = self.sid(tab)
        metrics = self.call("Page.getLayoutMetrics", session=session, timeout=2)
        viewport = (
            metrics.get("cssVisualViewport")
            or metrics.get("visualViewport")
            or metrics.get("cssLayoutViewport")
            or metrics.get("layoutViewport")
            or {}
        )
        width, height = viewport.get("clientWidth"), viewport.get("clientHeight")
        if (
            isinstance(width, bool)
            or not isinstance(width, (int, float))
            or isinstance(height, bool)
            or not isinstance(height, (int, float))
            or width <= 0
            or height <= 0
        ):
            raise PolicyError("Browser viewport is unavailable; inspect the tab")
        self.call(
            "Input.dispatchMouseEvent",
            dict(
                type="mouseWheel",
                x=max(0, min(int(width) - 1, int(width // 2))),
                y=max(0, min(int(height) - 1, int(height // 2))),
                deltaX=x,
                deltaY=y,
            ),
            session,
            timeout=2,
        )
        return dict(scrolled=True, delta_x=x, delta_y=y)
