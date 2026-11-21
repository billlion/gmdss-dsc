import base64
import unittest

from gmdss_dsc.web_access import AccessGuard, access_password


class AccessTests(unittest.IsolatedAsyncioTestCase):
    async def request(self, *, kind="http", password="secret", credentials="dsc:secret",
                      origin="http://boat:8080", path="/"):
        messages, passed = [], []
        async def downstream(scope, receive, send):
            passed.append(True)
        async def send(message):
            messages.append(message)
        headers = [(b"host", b"boat:8080")]
        if origin is not None:
            headers.append((b"origin", origin.encode()))
        if credentials is not None:
            headers.append((b"authorization", b"Basic " + base64.b64encode(credentials.encode())))
        await AccessGuard(downstream, password)(
            dict(type=kind, headers=headers, scheme="ws" if kind == "websocket" else "http", path=path),
            None, send)
        return passed, messages

    async def test_http_polling_and_websocket_require_credentials(self):
        for path in ("/", "/_nicegui/3.19.0/static/nicegui.js", "/_nicegui_ws/socket.io/"):
            for credentials in (None, "dsc:wrong", "other:secret"):
                passed, messages = await self.request(path=path, credentials=credentials, origin=None)
                self.assertFalse(passed)
                self.assertEqual(messages[0]["status"], 401)
            passed, _ = await self.request(path=path)
            self.assertTrue(passed)
        passed, messages = await self.request(kind="websocket", credentials=None)
        self.assertFalse(passed)
        self.assertEqual(messages[0]["code"], 1008)
        passed, _ = await self.request(kind="websocket")
        self.assertTrue(passed)

    async def test_foreign_origin_rejected_even_on_passwordless_loopback(self):
        for kind in ("http", "websocket"):
            for origin in ("https://boat:8080", "http://evil.example", "null", "http://boat:8080/evil"):
                passed, _ = await self.request(kind=kind, password=None, credentials=None, origin=origin)
                self.assertFalse(passed)

    async def test_passwordless_mode_rejects_foreign_host(self):
        passed, messages = await self.request(password=None, credentials=None)
        self.assertFalse(passed)
        self.assertEqual(messages[0]["status"], 403)

    def test_explicit_lan_binding_gets_random_password(self):
        for host in ("127.0.0.1", "::1", "localhost"):
            self.assertEqual(access_password(host), (None, False))
        first, generated = access_password("0.0.0.0")
        self.assertTrue(generated)
        self.assertGreaterEqual(len(first), 20)
        self.assertNotEqual(first, access_password("192.168.1.20")[0])
        self.assertEqual(access_password("0.0.0.0", "chosen"), ("chosen", False))
