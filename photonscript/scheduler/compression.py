"""Gzip for the dashboard's text responses (2026-10-24).

GET /api/runs/2026-10-23 (673 subs) is 2.2 MB of JSON. Over the tailnet
the scope PC sends about 200 KB/s, so the Imaging Runs page waited ~10 s
for the body after a 0.7 s server answer; gzipped it is 136 KB (16x).

JsonGzipMiddleware compresses JSON, HTML, CSS and JavaScript responses of
at least MIN_SIZE bytes for a client that accepts gzip. Everything else
(thumbnail PNG / JPEG, FITS crops, zip bundles, websockets) passes through
untouched and unbuffered, whatever the installed Starlette's own
GZipMiddleware would exclude. A body larger than THREAD_MIN is compressed
in a worker thread so the event loop never stalls on it.
"""
from __future__ import annotations

import gzip

import anyio.to_thread

MIN_SIZE = 1024
THREAD_MIN = 256 * 1024
LEVEL = 5            # the scope PC is CPU-tight: 2 MB in ~20 ms, still ~16x
COMPRESSIBLE = ("application/json", "text/html", "text/css", "text/plain",
                "application/javascript", "text/javascript")


def compressible(content_type: str) -> bool:
    ct = (content_type or "").split(";")[0].strip().lower()
    return ct in COMPRESSIBLE


def _accepts_gzip(scope) -> bool:
    for k, v in scope.get("headers") or []:
        if k.lower() == b"accept-encoding" and b"gzip" in v.lower():
            return True
    return False


def _header(headers: list, name: bytes) -> bytes | None:
    for k, v in headers:
        if k.lower() == name:
            return v
    return None


class JsonGzipMiddleware:
    def __init__(self, app, minimum_size: int = MIN_SIZE, level: int = LEVEL):
        self.app = app
        self.minimum_size = minimum_size
        self.level = level

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http" or not _accepts_gzip(scope):
            await self.app(scope, receive, send)
            return
        start: dict | None = None
        chunks: list[bytes] = []
        passthrough = False

        async def wrapped(message):
            nonlocal start, passthrough
            kind = message.get("type")
            if kind == "http.response.start":
                headers = list(message.get("headers") or [])
                ct = (_header(headers, b"content-type") or b"").decode("latin-1")
                if _header(headers, b"content-encoding") or not compressible(ct):
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if kind != "http.response.body" or passthrough or start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            headers = [(k, v) for k, v in start.get("headers") or []]
            if len(body) >= self.minimum_size:
                if len(body) >= THREAD_MIN:
                    body = await anyio.to_thread.run_sync(
                        gzip.compress, body, self.level)
                else:
                    body = gzip.compress(body, self.level)
                headers = [(k, v) for k, v in headers
                           if k.lower() not in (b"content-length", b"vary")]
                vary = _header(start.get("headers") or [], b"vary")
                headers += [(b"content-encoding", b"gzip"),
                            (b"content-length", str(len(body)).encode()),
                            (b"vary", (vary + b", Accept-Encoding") if vary
                             else b"Accept-Encoding")]
            await send({**start, "headers": headers})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped)
