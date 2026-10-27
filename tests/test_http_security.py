"""Regression coverage for the local MCP HTTP security boundary."""

import asyncio

from udb.server import AuthenticatedASGI


def invoke(headers=(), events=None, client=("127.0.0.1", 40000), kind="http"):
    observed = []
    reached = []

    async def app(scope, receive, send):
        reached.append(scope["type"])
        if scope["type"] == "lifespan":
            return
        request = await receive()
        reached.append(request.get("body"))
        await send({
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"cache-control", b"public, max-age=100")],
        })
        await send({"type": "http.response.body", "body": b"ok"})

    async def run():
        queue = list(events if events is not None else [
            {"type": "http.request", "body": b"{}", "more_body": False}
        ])

        async def receive():
            return queue.pop(0) if queue else {"type": "http.disconnect"}

        async def send(message):
            observed.append(message)

        scope = {"type": kind, "headers": list(headers), "client": client}
        await AuthenticatedASGI(app, "test-token")(scope, receive, send)

    asyncio.run(run())
    return observed, reached


AUTH = (b"authorization", b"Bearer test-token")


def test_valid_local_bearer_request_is_forwarded_with_secure_headers():
    events, reached = invoke([AUTH])
    assert reached == ["http", b"{}"]
    assert events[0]["status"] == 200
    headers = dict(events[0]["headers"])
    assert headers[b"cache-control"] == b"no-store"
    assert headers[b"x-content-type-options"] == b"nosniff"
    assert headers[b"x-frame-options"] == b"DENY"


def test_missing_or_duplicate_authorization_is_rejected():
    for headers in ([], [AUTH, AUTH], [(b"authorization", b"Bearer wrong")]):
        events, reached = invoke(headers)
        assert events[0]["status"] == 401
        assert reached == []


def test_non_loopback_client_cannot_reach_mcp():
    events, reached = invoke([AUTH], client=("203.0.113.5", 4444))
    assert events[0]["status"] == 403
    assert reached == []


def test_oversized_content_length_is_rejected_before_body_read():
    events, reached = invoke([AUTH, (b"content-length", b"16777217")])
    assert events[0]["status"] == 413
    assert reached == []


def test_chunked_body_limit_applies_without_content_length():
    events, reached = invoke(
        [AUTH],
        events=[
            {"type": "http.request", "body": b"x" * (16 * 1024 * 1024), "more_body": True},
            {"type": "http.request", "body": b"x", "more_body": False},
        ],
    )
    assert events[0]["status"] == 413
    assert reached == []


def test_websocket_cannot_bypass_http_auth():
    events, reached = invoke(kind="websocket")
    assert events == [{"type": "websocket.close", "code": 1008}]
    assert reached == []


def test_lifespan_is_forwarded_without_http_auth():
    events, reached = invoke(kind="lifespan")
    assert reached == ["lifespan"]
    assert events == []


def test_duplicate_content_length_is_rejected():
    events, reached = invoke([
        AUTH,
        (b"content-length", b"2"),
        (b"content-length", b"2"),
    ])
    assert events[0]["status"] == 400
    assert reached == []
