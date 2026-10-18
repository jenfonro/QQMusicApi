# ruff: noqa: D101, D102, D103, D107, E402, I001, PT009, RET503
#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
"""Offline Web route regressions through the real router, executor and SDK engine."""

import sys

sys.dont_write_bytecode = True

import asyncio
import json
import logging
import os
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
for key in list(os.environ):
    if key.startswith("QQMUSIC_"):
        del os.environ[key]
logging.disable(logging.CRITICAL)


def forbid_network(event, args):
    if event in ("socket.connect", "socket.getaddrinfo", "socket.sendto"):
        raise AssertionError("Route regressions must not access the network")


sys.addaudithook(forbid_network)

from niquests.models import Response
from qqmusic_api.core.engine import RequestEngine
from web.src.app import create_app
from web.src.core.config import CredentialConfig
from web.src.routes import ROUTES
from web.src.routing.route_types import AuthPolicy, HttpMethod

RADAR = "/recommend/get_radar_recommend"


def response(data):
    result = Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    result._content_consumed = True
    return result


class OfflineTransport:
    """Capture physical CGI requests; all accounts and responses are synthetic."""

    def __init__(self):
        self.requests = []
        self.radar = []
        self.audio = []

    async def request(self, request):
        self.requests.append(request)
        if request.url == "https://api.tencentmusic.com/tme/trpc/proxy":
            return response({"data": json.dumps({"data": {"q16": "0" * 16, "q36": "0" * 36}})})
        assert request.method == "POST"
        assert request.url == "https://u.y.qq.com/cgi-bin/musicu.fcg"
        payload = request.kwargs["json"]
        item = payload["req_0"]
        target = (item["module"], item["method"])
        if target == ("music.getSession.session", "GetSession"):
            data = {"session": {"uid": "synthetic-uid", "sid": "synthetic-sid"}}
        elif target == ("music.recommend.TrackRelationServer", "GetRadarSong"):
            self.radar.append(payload)
            account, page = int(payload["comm"]["qq"]), item["param"]["Page"]
            track = {
                "id": account,
                "mid": "synthetic",
                "name": "offline",
                "type": 1,
                "singer": [],
                "album": {},
                "mv": {},
                "file": {},
                "pay": {},
                "interval": 180,
                "isonly": 0,
                "language": 0,
                "genre": 0,
                "index_cd": 0,
                "index_album": 0,
                "status": 0,
                "label": "",
                "bpm": 0,
                "ov": 0,
                "sa": 0,
                "es": "",
                "vs": [],
                "vi": [],
                "vf": [],
            }
            data = {
                "VecSongs": [{"Track": track}],
                "RecommendSongIds": [account, page, len(self.radar)],
                "BaseSongIds": [],
                "HasMore": page == 1,
                "TimeStamp": 1700000000,
                "VideoCards": {},
            }
            await asyncio.sleep(0)  # Keep concurrent account requests in flight together.
        elif target == ("music.vkey.GetVkey", "UrlGetVkey"):
            self.audio.append(payload)
            data = {
                "expiration": 60,
                "midurlinfo": [
                    {
                        "songmid": item["param"]["songmid"][0],
                        "filename": item["param"]["filename"][0],
                        "purl": "",
                        "vkey": "",
                        "ekey": "",
                        "result": 104003,
                    }
                ],
            }
        else:
            raise AssertionError("Unexpected upstream method")
        return response({"code": 0, "req_0": {"code": 0, "data": data}})

    async def close(self):
        pass


class NoSharedResults:
    """Fail if radar touches public cache, negative cache or request coalescing."""

    async def get(self, *args):
        raise AssertionError("Private route must not read shared cache")

    async def set(self, *args):
        raise AssertionError("Private route must not write shared cache")

    def enter(self, *args):
        raise AssertionError("Private route must not coalesce requests")


async def request(app, path=RADAR, query="", cookie=""):
    """Drive the actual ASGI route without an HTTP client dependency or sockets."""
    messages = []
    received = False

    async def receive():
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.Future()

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode(),
        "query_string": query.encode(),
        "root_path": "",
        "headers": [(b"host", b"127.0.0.1"), (b"cookie", cookie.encode())],
        "client": ("127.0.0.1", 12345),
        "server": ("127.0.0.1", 80),
    }
    await asyncio.wait_for(app(scope, receive, send), timeout=5)
    start = next(item for item in messages if item["type"] == "http.response.start")
    body = b"".join(item.get("body", b"") for item in messages if item["type"] == "http.response.body")
    return start["status"], dict(start["headers"]), json.loads(body)


def cookie(account):
    return f"musicid={account}; musickey=synthetic-key-{account}; str_musicid={account}"


class RouteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.transport = OfflineTransport()
        self.engine = RequestEngine.create(transport=self.transport, device_path=None)
        self.app = create_app()
        services = self.app.state.services
        services.engine = self.engine
        services.credential_config = CredentialConfig(enabled=False)
        services.cache = services.coalescer = NoSharedResults()
        # Deliberately do not enter upstream lifespan: no account store or seeds.
        self.assertIsNone(services.credential_pool)

    async def asyncTearDown(self):
        await self.engine.close()

    async def test_route_contract(self):
        """雷达需要凭据且无缓存, 其它推荐路由和 JSON 契约保持不变."""
        routes = {route.path: route for route in ROUTES}
        self.assertIs(routes[RADAR].auth, AuthPolicy.COOKIE_OR_DEFAULT)
        self.assertIsNone(routes[RADAR].cache)
        guess = routes["/recommend/get_guess_recommend"]
        self.assertIs(guess.auth, AuthPolicy.OPTIONAL)
        self.assertIsNone(guess.cache)
        for name in ("get_home_feed", "get_recommend_newsong", "get_recommend_songlist"):
            route = routes["/recommend/" + name]
            self.assertIs(route.auth, AuthPolicy.NONE)
            self.assertEqual(route.cache.ttl, 60)
        operation = self.app.openapi()["paths"][RADAR]["get"]
        self.assertEqual(operation["security"], [{"MusicId": [], "MusicKey": []}])
        (page,) = operation["parameters"]
        self.assertEqual((page["name"], page["in"], page["schema"]["default"]), ("page", "query", 1))
        self.assertEqual(
            operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"],
            "#/components/schemas/ApiResponse_RadarRecommendResponse_",
        )
        report = routes["/report/play_duration"]
        self.assertIs(report.auth, AuthPolicy.COOKIE_OR_DEFAULT)
        self.assertIsNone(report.cache)
        self.assertEqual(report.methods, (HttpMethod.POST,))
        operation = self.app.openapi()["paths"]["/report/play_duration"]["post"]
        self.assertEqual(operation["security"], [{"MusicId": [], "MusicKey": []}])
        self.assertEqual({item["name"] for item in operation["parameters"]}, {"songmid", "listened_seconds"})

    async def test_guest_and_invalid_parameters_never_reach_engine(self):
        """游客与无效参数在本地拒绝, 不执行任何上游请求."""
        for value, query, expected in (
            ("", "", 401),
            ("musicid=101", "", 422),
            (cookie(0), "", 401),
            (cookie(101), "page=invalid", 422),
        ):
            with self.subTest(status=expected, query=query):
                status, _, data = await request(self.app, query=query, cookie=value)
                self.assertEqual(status, expected)
                self.assertEqual(data["code"], -1)
        self.assertEqual(self.transport.requests, [])

    def assert_radar_result(self, result, account, page, sequence):
        status, headers, data = result
        self.assertEqual(status, 200)
        self.assertNotIn(b"etag", headers)
        self.assertNotIn(b"cache-control", headers)  # The outer launcher adds no-store.
        self.assertEqual(set(data), {"code", "msg", "data"})
        self.assertEqual((data["code"], data["msg"]), (0, "ok"))
        self.assertEqual(
            set(data["data"]),
            {"songs", "recommend_song_ids", "base_song_ids", "has_more", "toast", "timestamp", "video_cards"},
        )
        self.assertEqual(data["data"]["songs"][0]["id"], account)
        self.assertEqual(data["data"]["recommend_song_ids"], [account, page, sequence])
        self.assertIs(data["data"]["has_more"], page == 1)
        self.assertNotIn("synthetic-key", json.dumps(data))

    def assert_radar_request(self, payload, account, page):
        self.assertEqual(payload["comm"]["qq"], str(account))
        self.assertEqual(payload["comm"]["authst"], f"synthetic-key-{account}")
        self.assertEqual(payload["comm"]["tmeLoginType"], "2")
        self.assertEqual(payload["req_0"]["param"], {"Page": page, "ReqType": 0, "FavSongs": [], "EntranceSongs": []})

    async def test_accounts_pages_and_repeat_are_not_cached(self):
        """每次雷达调用携带请求账号和页码, 相同账号重复请求也不复用结果."""
        for sequence, (account, page) in enumerate(((101, 1), (202, 1), (101, 2), (101, 1)), 1):
            query = "" if sequence == 1 else f"page={page}"
            result = await request(self.app, query=query, cookie=cookie(account))
            self.assert_radar_result(result, account, page, sequence)
            self.assert_radar_request(self.transport.radar[-1], account, page)
        self.assertEqual(len(self.transport.radar), 4)

    async def test_concurrent_accounts_do_not_coalesce(self):
        """并发同页的两个账号及重复调用分别执行, 不合并或串用凭据."""
        accounts = (101, 202, 101)
        results = await asyncio.gather(
            *(request(self.app, query="page=2", cookie=cookie(account)) for account in accounts)
        )
        self.assertEqual(len(self.transport.radar), len(accounts))
        sequences = []
        for account, result in zip(accounts, results, strict=True):
            sequence = result[2]["data"]["recommend_song_ids"][2]
            sequences.append(sequence)
            self.assert_radar_result(result, account, 2, sequence)
            self.assert_radar_request(self.transport.radar[sequence - 1], account, 2)
        self.assertEqual(sorted(sequences), [1, 2, 3])

    async def test_audio_parameters_and_denial_are_preserved(self):
        """音源路由透传 M500 参数和请求账号, 不将无权限结果伪装成功."""
        for query, song_type, filename in (
            ("", 0, "M500synthetic-midsynthetic-mid.mp3"),
            ("&song_type=1", 1, "M500synthetic-midsynthetic-mid.mp3"),
            ("&song_type=1&media_mid=synthetic-media", 1, "M500synthetic-media.mp3"),
        ):
            status, _, data = await request(self.app, "/song/synthetic-mid/url", "file_type=13" + query, cookie(101))
            self.assertEqual(status, 200)
            payload = self.transport.audio[-1]
            self.assertEqual(payload["comm"]["qq"], "101")
            self.assertEqual(payload["comm"]["authst"], "synthetic-key-101")
            self.assertEqual(payload["req_0"]["param"]["uin"], "101")
            self.assertEqual(payload["req_0"]["param"]["songmid"], ["synthetic-mid"])
            self.assertEqual(payload["req_0"]["param"]["songtype"], [song_type])
            self.assertEqual(payload["req_0"]["param"]["filename"], [filename])
            (row,) = data["data"]["data"]
            self.assertEqual((row["mid"], row["result"], row["purl"]), ("synthetic-mid", 104003, ""))


if __name__ == "__main__":
    unittest.main()
