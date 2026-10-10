"""The whole path, over real HTTP, with no Slack and no tokens spent.

This is the test that answers "can Ricardi be checked end to end without a
human at the keyboard". Everything here is the production code path except the
two ends: the transport is HTTP instead of Slack, and the agent is a fake
instead of a Claude session. Both ends are covered on their own elsewhere --
the Slack parse in test_slack.py, the real agent in test_live_sdk.py.
"""

import asyncio
import http.client
import json

import pytest

from conftest import open_policy

from ricardi.app import Ricardi
from ricardi.backends.fake import FakeBackend
from ricardi.transports.local import LocalTransport


def _request(port, method, path, body=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        payload = json.dumps(body).encode() if body is not None else None
        headers = {"Content-Type": "application/json"} if payload else {}
        conn.request(method, path, body=payload, headers=headers)
        response = conn.getresponse()
        return response.status, json.loads(response.read() or b"{}")
    finally:
        conn.close()


async def call(port, method, path, body=None):
    return await asyncio.to_thread(_request, port, method, path, body)


@pytest.fixture
async def running():
    transport = LocalTransport(port=0)
    backend = FakeBackend()
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0, policy=open_policy())
    await app.start()
    try:
        yield app, transport, backend
    finally:
        await app.close()


async def replies_after(transport, port, n, timeout=5.0):
    """Wait until Ricardi has said at least n things."""
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        status, body = await call(port, "GET", "/replies")
        assert status == 200
        if body["total"] >= n:
            return [r["text"] for r in body["replies"]]
        await asyncio.sleep(0.02)
    raise AssertionError(f"only {body['total']} replies, wanted {n}")


async def test_the_server_is_up(running):
    _, transport, _ = running
    status, body = await call(transport.port, "GET", "/health")
    assert status == 200 and body["ok"] is True


async def test_a_message_in_becomes_an_answer_out(running):
    _, transport, backend = running
    status, body = await call(
        transport.port, "POST", "/say", {"text": "list agents", "thread": "T1"}
    )
    assert status == 202
    assert body["accepted"] is True

    texts = await replies_after(transport, transport.port, 1)
    assert "scout" in texts[0]


async def test_a_full_conversation_over_http(running):
    _, transport, backend = running
    port = transport.port

    await call(port, "POST", "/say", {"text": "tell scout to read auth.py", "thread": "T2"})
    await replies_after(transport, port, 1)
    # The thread is bound now, so a bare message goes to the same agent.
    await call(port, "POST", "/say", {"text": "and the tests?", "thread": "T2"})
    texts = await replies_after(transport, port, 2)

    assert backend.asked == [("scout", "read auth.py"), ("scout", "and the tests?")]
    assert "scout" in texts[1]
    assert "and the tests?" in texts[1]


async def test_two_threads_are_two_conversations(running):
    _, transport, backend = running
    port = transport.port

    await call(port, "POST", "/say", {"text": "tell scout hello", "thread": "A"})
    await call(port, "POST", "/say", {"text": "tell deployer hello", "thread": "B"})
    await replies_after(transport, port, 2)
    await call(port, "POST", "/say", {"text": "still you?", "thread": "A"})
    await replies_after(transport, port, 3)

    assert ("scout", "still you?") in backend.asked
    assert ("deployer", "still you?") not in backend.asked


async def test_replies_can_be_read_incrementally(running):
    _, transport, _ = running
    port = transport.port
    await call(port, "POST", "/say", {"text": "list agents", "thread": "T3"})
    await replies_after(transport, port, 1)
    await call(port, "POST", "/say", {"text": "help", "thread": "T3"})
    await replies_after(transport, port, 2)

    status, body = await call(port, "GET", "/replies?since=1")
    assert status == 200
    assert len(body["replies"]) == 1
    assert body["total"] == 2


async def test_a_slow_agent_does_not_hold_the_request_open():
    transport = LocalTransport(port=0)
    backend = FakeBackend(delay=0.5)
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0, policy=open_policy())
    await app.start()
    try:
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, _ = await call(
            transport.port, "POST", "/say", {"text": "tell scout wait", "thread": "S"}
        )
        # Accepted long before the agent is done, the way Slack needs.
        assert status == 202
        assert loop.time() - started < 0.4
        await replies_after(transport, transport.port, 1, timeout=5.0)
    finally:
        await app.close()


async def test_rubbish_is_refused_not_crashed(running):
    _, transport, _ = running
    conn = http.client.HTTPConnection("127.0.0.1", transport.port, timeout=5)

    def send_bad():
        conn.request("POST", "/say", body=b"{not json")
        r = conn.getresponse()
        return r.status, r.read()

    try:
        status, _ = await asyncio.to_thread(send_bad)
        assert status == 400
    finally:
        conn.close()
    # Still serving afterwards.
    status, _ = await call(transport.port, "GET", "/health")
    assert status == 200


async def test_an_unknown_path_is_a_404(running):
    _, transport, _ = running
    status, _ = await call(transport.port, "GET", "/nope")
    assert status == 404


async def test_an_oversized_body_is_refused(running):
    from ricardi.transports.local import MAX_BODY

    _, transport, backend = running

    def send_big():
        conn = http.client.HTTPConnection("127.0.0.1", transport.port, timeout=5)
        try:
            conn.request("POST", "/say", body=b"x" * (MAX_BODY + 1))
            return conn.getresponse().status
        finally:
            conn.close()

    assert await asyncio.to_thread(send_big) == 413
    assert backend.asked == []
    # Still serving, and nothing was read into memory.
    status, _ = await call(transport.port, "GET", "/health")
    assert status == 200


async def test_a_lying_content_length_does_not_hang_the_connection():
    # Regression: readexactly only gives up at EOF, so a client that declares
    # 500 bytes, sends 2 and waits used to hold the connection open forever.
    transport = LocalTransport(port=0, read_timeout=0.2)
    backend = FakeBackend()
    app = Ricardi(transport=transport, backend=backend, ack_after=30.0, policy=open_policy())
    await app.start()

    def send_short():
        conn = http.client.HTTPConnection("127.0.0.1", transport.port, timeout=5)
        try:
            conn.putrequest("POST", "/say")
            conn.putheader("Content-Length", "500")
            conn.endheaders()
            conn.send(b"{}")
            return conn.getresponse().status
        finally:
            conn.close()

    try:
        assert await asyncio.to_thread(send_short) == 400
        assert backend.asked == []
    finally:
        await app.close()


async def test_the_transport_binds_where_it_is_told():
    """Regression: the default is loopback, which a container port mapping
    cannot reach. The container start-up looked perfectly healthy and nothing
    outside it could connect."""
    transport = LocalTransport(host="127.0.0.1", port=0)
    app = Ricardi(transport=transport, backend=FakeBackend(), ack_after=30.0,
                  policy=open_policy())
    await app.start()
    try:
        assert transport.host == "127.0.0.1"
        status, _ = await call(transport.port, "GET", "/health")
        assert status == 200
    finally:
        await app.close()
