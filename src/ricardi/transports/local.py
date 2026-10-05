"""A transport with no Slack behind it, driven over plain HTTP.

Ricardi's one human-gated step is creating a Slack app, so the rest of the
system is built to be exercised without it. POST a message in, poll the replies
out, and every other part of Ricardi runs exactly as it does in production.

    curl -s -XPOST localhost:8808/say -d '{"text":"list agents"}'
    curl -s localhost:8808/replies

Only the stdlib is involved, so this works before anything is installed.
"""

from __future__ import annotations

import asyncio
import json
from http import HTTPStatus
from typing import Any

from ricardi.transports.base import Inbound, OnMessage

# Generous for a sentence, small enough that nothing can exhaust memory.
MAX_BODY = 64 * 1024
# A body that never finishes arriving must not hold a connection open forever.
READ_TIMEOUT = 5.0
# When refusing an oversized body, read a little of it first so the client can
# finish writing and then read the refusal, instead of seeing a reset.
DRAIN = 256 * 1024


class LocalTransport:
    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 8808,
        read_timeout: float = READ_TIMEOUT,
    ):
        self.host = host
        self.port = port
        self._read_timeout = read_timeout
        # Everything Ricardi has said, oldest first. Tests poll this.
        self.replies: list[dict[str, str]] = []
        # Refusals, which in Slack only the asker sees.
        self.private: list[dict[str, str]] = []
        self._on_message: OnMessage | None = None
        self._server: asyncio.base_events.Server | None = None
        self._turns: set[asyncio.Task[None]] = set()

    # -- Transport ---------------------------------------------------------
    async def start(self, on_message: OnMessage) -> None:
        self._on_message = on_message
        self._server = await asyncio.start_server(self._serve, self.host, self.port)
        # port 0 asks the OS for a free one; report back what it gave us.
        self.port = self._server.sockets[0].getsockname()[1]

    async def send(self, channel: str, thread: str, text: str) -> None:
        self.replies.append({"channel": channel, "thread": thread, "text": text})

    async def send_private(self, channel: str, thread: str, user: str, text: str) -> None:
        self.private.append(
            {"channel": channel, "thread": thread, "user": user, "text": text}
        )

    async def close(self) -> None:
        for task in list(self._turns):
            task.cancel()
        if self._turns:
            await asyncio.gather(*list(self._turns), return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    # -- the smallest HTTP server that can do this -------------------------
    async def _serve(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        try:
            method, target, _ = lines[0].split(" ", 2)
        except ValueError:
            await self._respond(writer, 400, {"error": "bad request line"})
            return
        length = 0
        for line in lines[1:]:
            name, _, value = line.partition(":")
            if name.strip().lower() == "content-length":
                try:
                    length = int(value.strip() or 0)
                except ValueError:
                    await self._respond(writer, 400, {"error": "bad content-length"})
                    return
        # A message to an agent is a sentence, not a payload. Refusing an
        # oversized one keeps a typo in a Content-Length from reading gigabytes
        # into memory.
        if length > MAX_BODY:
            await self._drain(reader, min(length, DRAIN))
            await self._respond(writer, 413, {"error": f"body over {MAX_BODY} bytes"})
            return
        body = b""
        if length:
            try:
                # Timed, not just bounded: readexactly only gives up at EOF, and
                # a client that declares 500 bytes then sends 2 and waits for a
                # response never reaches one.
                body = await asyncio.wait_for(
                    reader.readexactly(length), timeout=self._read_timeout
                )
            except asyncio.IncompleteReadError:
                await self._respond(writer, 400, {"error": "body shorter than declared"})
                return
            except (asyncio.TimeoutError, ConnectionError):
                await self._respond(writer, 400, {"error": "body never finished arriving"})
                return

        path, _, query = target.partition("?")
        try:
            status, payload = await self._route(method, path, query, body)
        except Exception as exc:  # a stub's failure should still be readable
            status, payload = 500, {"error": repr(exc)}
        await self._respond(writer, status, payload)

    async def _route(
        self, method: str, path: str, query: str, body: bytes
    ) -> tuple[int, Any]:
        if method == "GET" and path == "/health":
            return 200, {
                "ok": True,
                "replies": len(self.replies),
                "private": len(self.private),
            }

        if method == "GET" and path == "/replies":
            since = 0
            for part in query.split("&"):
                key, _, value = part.partition("=")
                if key == "since" and value.isdigit():
                    since = int(value)
            return 200, {
                "replies": self.replies[since:],
                "total": len(self.replies),
                "private": self.private,
            }

        if method == "POST" and path == "/say":
            if self._on_message is None:
                return 503, {"error": "not started"}
            try:
                data = json.loads(body or b"{}")
            except ValueError:
                return 400, {"error": "body must be JSON"}
            inbound = Inbound(
                text=str(data.get("text", "")),
                thread=str(data.get("thread") or "local"),
                channel=str(data.get("channel") or "local"),
                user=str(data.get("user") or "tester"),
            )
            # Answered off to the side so a slow agent doesn't hold the request
            # open -- the same shape as Slack, which wants a fast ack.
            task = asyncio.create_task(self._on_message(inbound))
            self._turns.add(task)
            task.add_done_callback(self._turns.discard)
            return 202, {"accepted": True, "thread": inbound.thread}

        return 404, {"error": "no such path"}

    async def _drain(self, reader: asyncio.StreamReader, upto: int) -> None:
        """Swallow at most `upto` bytes of a body we are about to refuse."""
        left = upto
        while left > 0:
            try:
                chunk = await asyncio.wait_for(
                    reader.read(min(left, 65536)), timeout=self._read_timeout
                )
            except (asyncio.TimeoutError, ConnectionError):
                return
            if not chunk:
                return
            left -= len(chunk)

    async def _respond(self, writer: asyncio.StreamWriter, status: int, payload: Any) -> None:
        raw = json.dumps(payload).encode("utf-8")
        try:
            reason = HTTPStatus(status).phrase
        except ValueError:
            reason = "Status"
        head = (
            f"HTTP/1.1 {status} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(raw)}\r\n"
            "Connection: close\r\n\r\n"
        ).encode("latin-1")
        try:
            writer.write(head + raw)
            await writer.drain()
        except ConnectionError:
            pass
        finally:
            writer.close()
