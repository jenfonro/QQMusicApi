# SPDX-License-Identifier: GPL-3.0-or-later
"""验证真实私有网关的异常诊断脱敏与鉴权边界."""

import json
from pathlib import Path

import pytest

from iwebplus.launcher import PrivateApp

pytestmark = pytest.mark.core


async def failing(scope, receive, send):
    """用下游应用桩抛出包含合成敏感数据的异常."""
    raise ValueError("private-cookie private-qr private-account private-token")


def scope(path):
    """构造包含合成 URL, Cookie 和令牌的请求."""
    return {
        "type": "http",
        "method": "GET",
        "path": path,
        "query_string": b"identifier=private-qr",
        "headers": [
            (b"host", b"127.0.0.1:12345"),
            (b"authorization", b"Bearer private-token"),
            (b"cookie", b"musickey=private-cookie"),
        ],
    }


@pytest.mark.parametrize(
    ("path", "stage"),
    [
        ("/login/qrcode/qq", "qr-create"),
        ("/login/qrcode/qq/status", "qr-status"),
        ("/user/get_vip_info", "account-check"),
        ("/user/private-account/homepage", "business"),
    ],
)
async def test_error_diagnostics_keep_locations_not_secrets(tmp_path, path, stage):
    """异常继续抛出, 私有诊断仅保存阶段和代码位置, 不泄露请求及异常敏感信息."""
    app = PrivateApp(failing, "private-token", {"port": 12345}, tmp_path)
    messages = []

    async def send(message):
        """收集网关实际发送的响应."""
        messages.append(message)

    with pytest.raises(ValueError, match="private-cookie"):
        await app(scope(path), None, send)
    file = tmp_path / "last-error.json"
    text = file.read_text()
    assert "private-" not in text
    data = json.loads(text)
    assert set(data) == {"time", "stage", "type", "frames"}
    assert type(data["time"]) is int
    assert data["type"] == "ValueError"
    assert data["stage"] == stage
    assert 1 <= len(data["frames"]) <= 8
    assert data["frames"][-1]["function"] == "failing"
    for frame in data["frames"]:
        assert set(frame) == {"file", "function", "line"}
        assert Path(frame["file"]).name == frame["file"]
        assert frame["line"] > 0
    assert file.stat().st_mode & 0o777 == 0o600
    assert messages == []

    with pytest.raises(ValueError, match="private-cookie"):
        await app(scope("/user/private-account/homepage"), None, send)
    assert json.loads(file.read_text())["stage"] == "business"
    assert "private-" not in file.read_text()
    assert file.stat().st_mode & 0o777 == 0o600


async def test_unauthorized_request_never_creates_error_diagnostics(tmp_path):
    """鉴权失败在下游执行前返回 401, 不记录请求信息或创建异常诊断."""
    app = PrivateApp(failing, "private-token", {"port": 12345}, tmp_path)
    incoming = scope("/login/qrcode/qq/status")
    incoming["headers"] = [(b"authorization", b"Bearer wrong")]
    messages = []

    async def send(message):
        """收集网关实际发送的鉴权响应."""
        messages.append(message)

    await app(incoming, None, send)
    assert messages[0]["status"] == 401
    assert (b"cache-control", b"no-store") in messages[0]["headers"]
    assert json.loads(messages[1]["body"]) == {"error": "unauthorized"}
    assert not (tmp_path / "last-error.json").exists()
