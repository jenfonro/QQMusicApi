# SPDX-License-Identifier: GPL-3.0-or-later
"""通过真实引擎与传输桩验证扫码登录, 不访问网络或账号存储."""

import json

import pytest
from niquests.models import Response

from qqmusic_api import Client, Credential, Platform
from qqmusic_api.models.login import QR, QRCodeLoginEvents, QRLoginType
from qqmusic_api.utils import hash33
from web.src.modules.login import _serialize_qrcode_status

pytestmark = pytest.mark.core

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
    """构造包含真实 Cookie 容器的离线响应."""
    result = Response()
    result.status_code = status
    result._content = (json.dumps(data, ensure_ascii=False) if data is not None else text).encode("utf-8")
    result._content_consumed = True
    if headers:
        result.headers.update(headers)
    # Leave encoding unset: snapshot_payload must also handle non-ASCII text.
    return result


class LoginTransport:
    """返回真实 niquests 响应与跨域 Cookie, 不打开套接字."""

    def __init__(self, event=0, duplicate_at=()):
        """设置扫码状态和同名 Cookie 所在的授权阶段."""
        self.event = event
        self.duplicate_at = duplicate_at
        self.requests = []

    def cookies(self, result, stage, name):
        """为响应添加单域或跨域同名 Cookie."""
        result.cookies.set(name, "synthetic-first", domain=".qq.com", path="/")
        if stage in self.duplicate_at:
            result.cookies.set(name, "synthetic-last", domain=".graph.qq.com", path="/")
        return result

    async def request(self, request):
        """记录真实引擎的请求并返回对应协议的合成响应."""
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
                response(status=302, headers={"Location": "https://graph.qq.com/oauth2.0/login_jump"}),
                "check_sig",
                "p_skey",
            )
        if request.url == AUTHORIZE_URL:
            return response(
                status=302,
                headers={"Location": "https://y.qq.com/portal/wx_redirect.html?code=synthetic-code&state=synthetic"},
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
        """关闭无外部资源的传输桩."""


async def check_status(event=0, duplicate_at=()):
    """执行真实扫码轮询并验证请求标识和状态序列化."""
    transport = LoginTransport(event, duplicate_at)
    qrcode = QR(b"", QRLoginType.QQ, "image/png", "synthetic-qr-only")
    async with Client(transport=transport, platform=Platform.ANDROID, device_path=None) as client:
        result = await client.login.check_qrcode(qrcode)
    status = _serialize_qrcode_status(result, qrcode)
    assert status.identifier == qrcode.identifier
    assert status.login_type == "qq"
    poll = transport.requests[0]
    assert (poll.method, poll.url) == ("GET", POLL_URL)
    assert poll.kwargs["cookies"] == {"qrsig": qrcode.identifier}
    assert poll.kwargs["params"]["ptqrtoken"] == str(hash33(qrcode.identifier))
    return result, status, transport.requests


@pytest.mark.parametrize(
    "duplicate_at",
    [(), ("ptqrlogin",), ("check_sig",), ("ptqrlogin", "check_sig")],
    ids=["single-cookie", "poll-duplicates", "check-sig-duplicates", "both-duplicates"],
)
async def test_confirmed_login(duplicate_at):
    """单域和跨域同名 Cookie 均完成授权, 返回真实 Credential 及稳定 JSON 字段."""
    result, status, requests = await check_status(duplicate_at=duplicate_at)
    assert result.event is QRCodeLoginEvents.DONE
    assert result.done
    assert status.event == 0
    assert status.done
    assert isinstance(result.credential, Credential)
    assert result.credential.is_valid()
    serialized = status.model_dump(mode="json", by_alias=False)["credential"]
    assert serialized["musicid"] == CREDENTIAL["musicid"]
    assert serialized["musickey"] == CREDENTIAL["musickey"]
    assert serialized["refresh_token"] == CREDENTIAL["refresh_token"]
    assert serialized["encrypt_uin"] == CREDENTIAL["encryptUin"]
    assert serialized["musickey_create_time"] == CREDENTIAL["musickeyCreateTime"]
    assert serialized["key_expires_in"] == CREDENTIAL["keyExpiresIn"]
    assert serialized["login_type"] == 2
    assert "encryptUin" not in serialized
    assert [(item.method, item.url) for item in requests] == [
        ("GET", POLL_URL),
        ("GET", CHECK_URL),
        ("POST", AUTHORIZE_URL),
        ("POST", QIMEI_URL),
        ("POST", CGI_URL),
        ("POST", CGI_URL),
    ]
    check, authorize = requests[1:3]
    assert not check.kwargs["allow_redirects"]
    assert check.kwargs["params"]["uin"] == "1"
    assert check.kwargs["params"]["ptsigx"] == "synthetic-sig"
    assert "cookies" not in check.kwargs
    assert not authorize.kwargs["allow_redirects"]
    selected_cookie = "synthetic-last" if "check_sig" in duplicate_at else "synthetic-first"
    assert authorize.kwargs["cookies"] == {"p_skey": selected_cookie}
    assert authorize.kwargs["data"]["g_tk"] == hash33(selected_cookie, 5381)
    assert authorize.kwargs["data"]["response_type"] == "code"
    session = requests[4].kwargs["json"]["req_0"]
    assert (session["module"], session["method"]) == ("music.getSession.session", "GetSession")
    assert session["param"]["caller"] == 2
    login = requests[5].kwargs["json"]
    assert login["req_0"] == {
        "module": "QQConnectLogin.LoginServer",
        "method": "QQLogin",
        "param": {"code": "synthetic-code"},
    }
    assert login["comm"]["tmeLoginType"] == "2"


@pytest.mark.parametrize(
    ("raw", "event", "code"),
    [(66, QRCodeLoginEvents.SCAN, 1), (67, QRCodeLoginEvents.CONF, 2)],
)
async def test_waiting_and_scanned(raw, event, code):
    """等待扫描及等待确认仅轮询一次且不产生凭据."""
    result, status, requests = await check_status(raw)
    assert result.event is event
    assert not result.done
    assert status.event == code
    assert not status.done
    assert status.credential is None
    assert len(requests) == 1
