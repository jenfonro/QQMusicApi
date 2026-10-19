# SPDX-License-Identifier: GPL-3.0-or-later
"""iWebPlus 离线测试的网络隔离."""

import sys
from contextvars import ContextVar

import pytest

_offline = ContextVar("iwebplus_tests_offline", default=False)


def forbid_network(event, args):
    """离线测试执行期间禁止 DNS 解析和外部套接字请求."""
    if _offline.get() and event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise AssertionError("iWebPlus regressions must not access the network")


sys.addaudithook(forbid_network)


@pytest.fixture(autouse=True)
def offline_network():
    """仅对本目录用例启用网络禁令, 不影响其它测试."""
    token = _offline.set(True)
    try:
        yield
    finally:
        _offline.reset(token)
