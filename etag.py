"""ETags on JSON GET responses, so a browser revalidates instead of downloading
again: a repeat visit to a team page was re-fetching ~330KB of API JSON that
hadn't changed.

Every 200 JSON answer to a GET gets `ETag` (a hash of the body) and
`Cache-Control: private, no-cache`. `no-cache` means the browser asks every
time, so nothing can be served stale; when its `If-None-Match` names the hash
of what the server would send now, the answer is an empty 304 instead.

It saves bandwidth, not server work: the endpoint still runs to produce the
body that is hashed. (`storage.shared_json_response` is what makes the big
ones cheap to run.)

Left alone: anything that already sets `Cache-Control` or `ETag`, anything
that sets a cookie, non-JSON bodies (images, files), and non-200 statuses.
nginx gzips these responses and weakens the ETag to `W/"…"` as it does, which
is why the comparison ignores a `W/` prefix.
"""
import hashlib


def _etag_matches(header: str, etag: str) -> bool:
    for tag in header.split(","):
        tag = tag.strip()
        if tag.startswith("W/"):
            tag = tag[2:]
        if tag == etag or tag == "*":
            return True
    return False


class ETagMiddleware:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        if_none_match = None
        for k, v in scope.get("headers") or []:
            if k == b"if-none-match":
                if_none_match = v.decode("latin-1")

        start = None
        chunks = []
        passthrough = False

        async def wrapped_send(message):
            nonlocal start, passthrough
            if passthrough:
                await send(message)
                return
            if message["type"] == "http.response.start":
                headers = {k.lower(): v for k, v in message.get("headers") or []}
                ctype = headers.get(b"content-type", b"")
                if (message["status"] != 200 or not ctype.startswith(b"application/json")
                        or b"cache-control" in headers or b"etag" in headers
                        or b"set-cookie" in headers):
                    passthrough = True
                    await send(message)
                    return
                start = message
                return
            if message["type"] != "http.response.body":
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body"):
                return
            body = b"".join(chunks)
            etag = '"' + hashlib.blake2b(body, digest_size=16).hexdigest() + '"'
            extra = [(b"etag", etag.encode()), (b"cache-control", b"private, no-cache")]
            if if_none_match and _etag_matches(if_none_match, etag):
                headers = [(k, v) for k, v in start.get("headers") or []
                           if k.lower() not in (b"content-length", b"content-type")]
                await send({"type": "http.response.start", "status": 304, "headers": headers + extra})
                await send({"type": "http.response.body", "body": b""})
                return
            await send({**start, "headers": list(start.get("headers") or []) + extra})
            await send({"type": "http.response.body", "body": body})

        await self.app(scope, receive, wrapped_send)
