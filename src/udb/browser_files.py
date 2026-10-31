"""Explicit same-origin browser transfers using separately granted local file access."""

import base64
import hashlib
import json
import uuid
from urllib.parse import urlsplit

from .core import PolicyError
from .host_browser import Browser, validate_url

MAX_BYTES = 8 * 1024 * 1024


def origin(url):
    validate_url(url)
    parsed = urlsplit(url)
    if not parsed.hostname:
        raise PolicyError("A public HTTP(S) origin is required")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    return (parsed.scheme, parsed.hostname.casefold(), port)


def check_origin(browser, tab, expected):
    actual = browser.evaluate(tab, "location.origin", timeout=2)
    if not expected or origin(actual) != origin(expected):
        raise PolicyError(
            "Browser origin changed; observe the page before transferring files"
        )
    return actual


def upload(browser, tab, selector, source, data, expected_origin):
    check_origin(browser, tab, expected_origin)
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_BYTES:
        raise PolicyError("Upload exceeds the 8 MiB browser transfer limit")
    if not isinstance(selector, str) or not selector or len(selector) > 500:
        raise PolicyError("Upload requires one CSS file-input selector")
    state = browser.evaluate(
        tab,
        Browser._locator_expression(
            selector,
            "",
            "",
            True,
            "return {ok:e.tagName==='INPUT'&&e.type==='file'&&!e.matches(':disabled')&&e.getAttribute('aria-disabled')!=='true'&&!isInert(e)};",
            visible_only=False,
        ),
    )
    if not isinstance(state, dict) or not state.get("ok"):
        raise PolicyError("Upload selector is not one enabled file input")
    session = browser.sid(tab)
    browser.call("DOM.getDocument", {"depth": 0, "pierce": True}, session)
    expression = Browser._locator_expression(
        selector,
        "",
        "",
        True,
        "return e&&e.tagName==='INPUT'&&e.type==='file'&&!e.matches(':disabled')&&e.getAttribute('aria-disabled')!=='true'&&!isInert(e)?e:null;",
        visible_only=False,
    )
    remote = browser.call(
        "Runtime.evaluate",
        {"expression": expression, "returnByValue": False},
        session,
    ).get("result", {})
    object_id = remote.get("objectId")
    if remote.get("subtype") != "node" or not isinstance(object_id, str):
        raise PolicyError("Upload selector must identify exactly one enabled file input")
    try:
        node_id = browser.call(
            "DOM.requestNode", {"objectId": object_id}, session
        ).get("nodeId")
        if not isinstance(node_id, int) or node_id <= 0:
            raise PolicyError("Upload selector is not a live file input")
        node = browser.call(
            "DOM.describeNode", {"nodeId": node_id}, session
        )["node"]
        attributes = node.get("attributes", [])
        attrs = dict(zip(attributes[::2], attributes[1::2], strict=True))
        if (
            node.get("nodeName") != "INPUT"
            or attrs.get("type", "").lower() != "file"
            or "disabled" in attrs
        ):
            raise PolicyError("Upload selector is not an enabled file input")
    except BaseException:
        try:
            browser.call("Runtime.releaseObject", {"objectId": object_id}, session)
        except Exception:
            pass
        raise
    if len(list(browser.folder.glob("udb-upload-*"))) >= 8:
        raise PolicyError(
            "Maximum 8 staged uploads per browser session; close the session to clear them"
        )
    folder = browser.folder / ("udb-upload-" + uuid.uuid4().hex)
    folder.mkdir()
    staged = folder / source.name
    try:
        staged.write_bytes(data)
        check_origin(browser, tab, expected_origin)
        browser.call(
            "DOM.setFileInputFiles",
            {"files": [str(staged)], "nodeId": node_id},
            session,
        )
    except BaseException:
        staged.unlink(missing_ok=True)
        folder.rmdir()
        raise
    finally:
        try:
            browser.call("Runtime.releaseObject", {"objectId": object_id}, session)
        except Exception:
            pass
    return dict(
        selected=True,
        submitted=False,
        bytes=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    )


def download(browser, tab, url, expected_origin):
    actual = check_origin(browser, tab, expected_origin)
    if origin(url) != origin(actual):
        raise PolicyError("Download URL must be on the currently observed origin")
    # Browser cookies stay inside the owned browser. Redirects are rejected;
    # nothing is automatically navigated, downloaded by Chrome, or executed.
    script = r"""(async () => {
      const abort = new AbortController();
      const timer = setTimeout(() => abort.abort(), 10000);
      try {
        const response = await fetch(__URL__, {credentials:'same-origin', mode:'same-origin', redirect:'error', signal:abort.signal});
        if (!response.ok || !response.body) return {error:'http_error',status:response.status};
        const reader = response.body.getReader(), chunks = [];
        let size = 0;
        while (true) {
          const {done,value} = await reader.read(); if (done) break;
          size += value.length;
          if (size > __LIMIT__) {await reader.cancel(); return {error:'file_too_large'};}
          chunks.push(value);
        }
        let binary = '';
        for (const chunk of chunks) for (let i=0;i<chunk.length;i+=8192)
          binary += String.fromCharCode(...chunk.subarray(i,i+8192));
        return {data:btoa(binary),bytes:size,content_type:response.headers.get('content-type')||''};
      } catch (_) { return {error:'download_failed_or_redirected'}; }
      finally {clearTimeout(timer);}
    })()""".replace("__LIMIT__", str(MAX_BYTES)).replace("__URL__", json.dumps(url))
    result = browser.call(
        "Runtime.evaluate",
        dict(expression=script, returnByValue=True, awaitPromise=True),
        browser.sid(tab),
        timeout=12,
    )
    value = result.get("result", {}).get("value", {})
    if (
        result.get("exceptionDetails")
        or not isinstance(value, dict)
        or value.get("error")
        or not isinstance(value.get("data"), str)
    ):
        raise PolicyError(
            "Download failed, redirected or exceeds 8 MiB; inspect before retrying"
        )
    try:
        raw = base64.b64decode(value["data"], validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise PolicyError("Browser returned an invalid download payload") from exc
    if len(raw) > MAX_BYTES:
        raise PolicyError("Browser download exceeds 8 MiB")
    return raw, str(value.get("content_type", ""))[:200]
