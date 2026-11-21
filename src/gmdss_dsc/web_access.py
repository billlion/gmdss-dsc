"""Small ASGI access guard, including NiceGUI's WebSocket transport.

SPDX-License-Identifier: GPL-3.0-or-later
"""
import base64
import hmac
import ipaddress
import secrets
from urllib.parse import urlsplit


def access_password(host, configured=None):
    """LAN binding requires a password; loopback can run without one."""
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    if configured:
        return configured, False
    return (None, False) if loopback else (secrets.token_urlsafe(18), True)


class AccessGuard:
    def __init__(self, app, password=None):
        self.app = app
        self.expected = (b"Basic " + base64.b64encode(("dsc:" + password).encode())
                         if password else None)

    async def __call__(self, scope, receive, send):
        if scope["type"] not in ("http", "websocket"):
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        origin = headers.get(b"origin")
        # NiceGUI's Socket.IO accepts '*' origins by default. Restrict both
        # HTTP polling and WebSockets before any application event can run.
        allowed_origin = True
        if origin:
            try:
                parsed = urlsplit(origin.decode("ascii"))
                scheme = "https" if scope.get("scheme") in ("https", "wss") else "http"
                allowed_origin = (parsed.scheme == scheme and
                                  parsed.netloc.lower() == headers.get(b"host", b"").decode("ascii").lower()
                                  and not parsed.path and not parsed.query and not parsed.fragment)
            except (UnicodeError, ValueError):
                allowed_origin = False
        if self.expected is None:
            # Loopback-only mode also restricts Host, to reject DNS rebinding
            # from a foreign browser origin whose name resolves to loopback.
            try:
                host = urlsplit("//" + headers.get(b"host", b"").decode("ascii"))
                name = host.hostname or ""
                local = name.lower() == "localhost"
                if not local:
                    local = ipaddress.ip_address(name).is_loopback
                allowed_origin = allowed_origin and local and not host.path
            except (UnicodeError, ValueError):
                allowed_origin = False
        authenticated = self.expected is None or hmac.compare_digest(
            headers.get(b"authorization", b""), self.expected)
        if allowed_origin and authenticated:
            return await self.app(scope, receive, send)
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        status = 401 if allowed_origin else 403
        response_headers = [(b"content-type", b"text/plain"), (b"cache-control", b"no-store")]
        if status == 401:
            response_headers.append((b"www-authenticate", b'Basic realm="DSC receiver", charset="UTF-8"'))
        await send({"type": "http.response.start", "status": status, "headers": response_headers})
        await send({"type": "http.response.body", "body": b"Sign in as dsc." if status == 401 else b"Origin denied."})
