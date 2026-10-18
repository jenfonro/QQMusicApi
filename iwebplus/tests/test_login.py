# ruff: noqa: D101, D102, D103, D107, E402, I001, PT009
#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline confirmed-login regression against the pinned upstream source."""

import sys

sys.dont_write_bytecode = True

import json
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def forbid_network(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise AssertionError("Login regression tests must not access the network")


sys.addaudithook(forbid_network)

from niquests.models import Response
from qqmusic_api import Client, Credential, Platform
from qqmusic_api.models.login import QR, QRCodeLoginEvents, QRLoginType
from qqmusic_api.utils import hash33
from web.src.modules.login import _serialize_qrcode_status

# All account, cookie and QR values below are synthetic, never read from storage.
CREDENTIAL = {
    "musicid": 1,
    "musickey": "synthetic-music-key",
    "encryptUin": "synthetic-euin",
    "refresh_token": "synthetic-refresh",
    "musickeyCreateTime": 1700000000,
    "keyExpiresIn": 7200,
}
POLL_URL = "https://ssl.ptlogin2.qq.com/ptqrlogin"
CHECK_URL = "https://ssl.ptlogin2.graph.qq.com/check_sig"
AUTHORIZE_URL = "https://graph.qq.com/oauth2.0/authorize"
QIMEI_URL = "https://api.tencentmusic.com/tme/trpc/proxy"
CGI_URL = "https://u.y.qq.com/cgi-bin/musicu.fcg"


def response(*, text="", data=None, status=200, headers=None):
    result = Response()
    result.status_code = status
    result._content = (json.dumps(data, ensure_ascii=False) if data is not None else text).encode("utf-8")
    result._content_consumed = True
    if headers:
        result.headers.update(headers)
    # Leave encoding unset: snapshot_payload must also handle non-ASCII text.
    return result


class LoginTransport:
    """Return real niquests responses/cookie jars without opening any sockets."""

    def __init__(self, event=0, duplicate_at=()):
        self.event = event
        self.duplicate_at = duplicate_at
        self.requests = []

    def cookies(self, result, stage, name):
        result.cookies.set(name, "synthetic-first", domain=".qq.com", path="/")
        if stage in self.duplicate_at:
            result.cookies.set(name, "synthetic-last", domain=".graph.qq.com", path="/")
        return result

    async def request(self, request):
        self.requests.append(request)
        if request.url == POLL_URL:
            redirect = CHECK_URL + "?uin=1&service=ptqrlogin&ptsigx=synthetic-sig&s_url=https://y.qq.com/"
            return self.cookies(
                response(text=(f"ptuiCB('{self.event}','0','{redirect}','0','扫码状态','合成用户');")),
                "ptqrlogin",
                "pt_login",
            )
        if request.url == CHECK_URL:
            return self.cookies(
                response(
                    status=302,
                    headers={
                        "Location": "https://graph.qq.com/oauth2.0/login_jump",
                    },
                ),
                "check_sig",
                "p_skey",
            )
        if request.url == AUTHORIZE_URL:
            return response(
                status=302,
                headers={
                    "Location": "https://y.qq.com/portal/wx_redirect.html?code=synthetic-code&state=synthetic",
                },
            )
        if request.url == QIMEI_URL:
            return response(data={"data": json.dumps({"data": {"q16": "0" * 16, "q36": "0" * 36}})})
        if request.url == CGI_URL:
            module = request.kwargs["json"]["req_0"]["module"]
            if module == "music.getSession.session":
                data = {"session": {"uid": "synthetic-uid", "sid": "synthetic-sid"}}
            elif module == "QQConnectLogin.LoginServer":
                data = CREDENTIAL
            else:
                raise AssertionError("Unexpected CGI module in login flow")
            return response(data={"code": 0, "req_0": {"code": 0, "data": data}})
        raise AssertionError("Unexpected request in login flow")

    async def close(self):
        pass


class LoginTests(unittest.IsolatedAsyncioTestCase):
    async def check_status(self, event=0, duplicate_at=()):
        transport = LoginTransport(event, duplicate_at)
        qrcode = QR(b"", QRLoginType.QQ, "image/png", "synthetic-qr-only")
        async with Client(transport=transport, platform=Platform.ANDROID, device_path=None) as client:
            result = await client.login.check_qrcode(qrcode)
        status = _serialize_qrcode_status(result, qrcode)
        self.assertEqual(status.identifier, qrcode.identifier)
        self.assertEqual(status.login_type, "qq")
        poll = transport.requests[0]
        self.assertEqual((poll.method, poll.url), ("GET", POLL_URL))
        self.assertEqual(poll.kwargs["cookies"], {"qrsig": qrcode.identifier})
        self.assertEqual(poll.kwargs["params"]["ptqrtoken"], str(hash33(qrcode.identifier)))
        return result, status, transport.requests

    async def confirmed(self, duplicate_at=()):
        result, status, requests = await self.check_status(duplicate_at=duplicate_at)
        self.assertIs(result.event, QRCodeLoginEvents.DONE)
        self.assertTrue(result.done)
        self.assertEqual(status.event, 0)
        self.assertTrue(status.done)
        self.assertIsInstance(result.credential, Credential)
        self.assertTrue(result.credential.is_valid())
        serialized = status.model_dump(mode="json", by_alias=False)["credential"]
        self.assertEqual(serialized["musicid"], CREDENTIAL["musicid"])
        self.assertEqual(serialized["musickey"], CREDENTIAL["musickey"])
        self.assertEqual(serialized["refresh_token"], CREDENTIAL["refresh_token"])
        self.assertEqual(serialized["encrypt_uin"], CREDENTIAL["encryptUin"])
        self.assertEqual(serialized["musickey_create_time"], CREDENTIAL["musickeyCreateTime"])
        self.assertEqual(serialized["key_expires_in"], CREDENTIAL["keyExpiresIn"])
        self.assertEqual(serialized["login_type"], 2)
        self.assertNotIn("encryptUin", serialized)
        self.assertEqual(
            [(item.method, item.url) for item in requests],
            [
                ("GET", POLL_URL),
                ("GET", CHECK_URL),
                ("POST", AUTHORIZE_URL),
                ("POST", QIMEI_URL),
                ("POST", CGI_URL),
                ("POST", CGI_URL),
            ],
        )
        check, authorize = requests[1:3]
        self.assertFalse(check.kwargs["allow_redirects"])
        self.assertEqual(check.kwargs["params"]["uin"], "1")
        self.assertEqual(check.kwargs["params"]["ptsigx"], "synthetic-sig")
        self.assertNotIn("cookies", check.kwargs)
        self.assertFalse(authorize.kwargs["allow_redirects"])
        selected_cookie = "synthetic-last" if "check_sig" in duplicate_at else "synthetic-first"
        self.assertEqual(authorize.kwargs["cookies"], {"p_skey": selected_cookie})
        self.assertEqual(authorize.kwargs["data"]["g_tk"], hash33(selected_cookie, 5381))
        self.assertEqual(authorize.kwargs["data"]["response_type"], "code")
        session = requests[4].kwargs["json"]["req_0"]
        self.assertEqual((session["module"], session["method"]), ("music.getSession.session", "GetSession"))
        self.assertEqual(session["param"]["caller"], 2)
        login = requests[5].kwargs["json"]
        self.assertEqual(
            login["req_0"],
            {
                "module": "QQConnectLogin.LoginServer",
                "method": "QQLogin",
                "param": {"code": "synthetic-code"},
            },
        )
        self.assertEqual(login["comm"]["tmeLoginType"], "2")

    async def test_confirmed_single_cookie(self):
        """单 Cookie 确认流程完成授权并返回真实 Credential."""
        await self.confirmed()

    async def test_confirmed_duplicate_domains(self):
        """轮询及 check_sig 的同名跨域 Cookie 不阻断确认流程."""
        for stages in (("ptqrlogin",), ("check_sig",), ("ptqrlogin", "check_sig")):
            with self.subTest(stages=stages):
                await self.confirmed(stages)

    async def test_waiting_and_scanned(self):
        """等待扫描及等待确认仅轮询一次且不产生凭据."""
        for raw, event, code in ((66, QRCodeLoginEvents.SCAN, 1), (67, QRCodeLoginEvents.CONF, 2)):
            with self.subTest(event=event.name):
                result, status, requests = await self.check_status(raw)
                self.assertIs(result.event, event)
                self.assertFalse(result.done)
                self.assertEqual(status.event, code)
                self.assertFalse(status.done)
                self.assertIsNone(status.credential)
                self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
