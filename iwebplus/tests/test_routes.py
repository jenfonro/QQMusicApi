# SPDX-License-Identifier: GPL-3.0-or-later
"""通过真实路由和 SDK 引擎验证账号隔离与播放行为, 仅替换外部传输."""

import asyncio
import gzip
import hashlib
import json
import time
import xml.etree.ElementTree as ET
import zlib
from pathlib import Path

import pytest
from niquests.models import Response

from qqmusic_api.core.engine import RequestEngine
from web.src.app import create_app
from web.src.core.config import CredentialConfig
from web.src.routes import ROUTES
from web.src.routing.route_types import AuthPolicy, HttpMethod

pytestmark = pytest.mark.core
RADAR = "/recommend/get_radar_recommend"
REPORT = "/report/play_duration"


def response(data):
    """构造真实响应对象, 由引擎负责解析合成 JSON."""
    result = Response()
    result.status_code = 200
    result._content = json.dumps(data).encode()
    result._content_consumed = True
    return result


def track(account=101):
    """返回符合真实模型约束的合成歌曲元数据."""
    return {
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


class ReportResponse:
    """上报传输响应桩, 延迟响应在 gather 之前不可读取."""

    def __init__(self):
        """默认返回协议要求的压缩成功回执."""
        self.lazy = False
        self.gathered = False
        self.status = 200
        self.body = b"\0" * 5 + zlib.compress(b'{"code":0}')

    @property
    def status_code(self):
        """返回已完成响应的状态码."""
        assert not self.lazy or self.gathered
        return self.status

    @property
    def content(self):
        """返回已完成响应的原始二进制内容."""
        assert not self.lazy or self.gathered
        return self.body


class ReportSession:
    """仅记录外部 HTTP 上报, 不替换路由或引擎行为."""

    def __init__(self):
        """创建独立的请求记录和可控协议回执."""
        self.requests = []
        self.response = ReportResponse()

    async def post(self, url, **kwargs):
        """记录真实路由生成的 URL, Cookie 和压缩载荷."""
        self.requests.append((url, kwargs))
        return self.response

    async def gather(self, result):
        """完成当前延迟响应."""
        assert result is self.response
        result.gathered = True


class OfflineTransport:
    """记录真实 CGI 请求, 所有账号和响应均为合成数据."""

    def __init__(self):
        """初始化雷达, 播放授权及上报传输桩."""
        self.requests = []
        self.radar = []
        self.audio = []
        self.tracks = [track()]
        self.audio_result = 104003
        self.audio_vkey = ""
        self.audio_mid = None
        self._client = ReportSession()

    async def request(self, request):
        """按真实请求的协议目标返回合成响应."""
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
            account, page = (int(payload["comm"]["qq"]), item["param"]["Page"])
            data = {
                "VecSongs": [{"Track": track(account)}],
                "RecommendSongIds": [account, page, len(self.radar)],
                "BaseSongIds": [],
                "HasMore": page == 1,
                "TimeStamp": 1700000000,
                "VideoCards": {},
            }
            await asyncio.sleep(0)  # Keep concurrent account requests in flight together.
        elif target == ("music.trackInfo.UniformRuleCtrl", "CgiGetTrackInfo"):
            data = {"tracks": self.tracks}
        elif target == ("music.vkey.GetVkey", "UrlGetVkey"):
            self.audio.append(payload)
            data = {
                "expiration": 60,
                "midurlinfo": [
                    {
                        "songmid": self.audio_mid or item["param"]["songmid"][0],
                        "filename": item["param"]["filename"][0],
                        "purl": "",
                        "vkey": self.audio_vkey,
                        "ekey": "",
                        "result": self.audio_result,
                    }
                ],
            }
        else:
            raise AssertionError("Unexpected upstream method")
        return response({"code": 0, "req_0": {"code": 0, "data": data}})

    async def close(self):
        """关闭无外部资源的传输桩."""


class NoSharedResults:
    """禁止私有路由读取公共缓存, 负缓存或合并请求."""

    async def get(self, *args):
        """公共缓存读取一旦发生即失败."""
        raise AssertionError("Private route must not read shared cache")

    async def set(self, *args):
        """公共缓存写入一旦发生即失败."""
        raise AssertionError("Private route must not write shared cache")

    def enter(self, *args):
        """私有请求一旦合并即失败."""
        raise AssertionError("Private route must not coalesce requests")


async def request(app, path=RADAR, query="", cookie="", *, method="GET"):
    """直接驱动真实 ASGI 路由, 不依赖 HTTP 客户端或套接字."""
    messages = []
    received = False

    async def receive():
        """提供一个空请求体并等待响应完成."""
        nonlocal received
        if not received:
            received = True
            return {"type": "http.request", "body": b"", "more_body": False}
        return await asyncio.Future()

    async def send(message):
        """记录实际 ASGI 响应消息."""
        messages.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": method,
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
    """为指定合成账号生成请求 Cookie."""
    return f"musicid={account}; musickey=synthetic-key-{account}; str_musicid={account}"


async def test_route_contract(route_app):
    """雷达需要凭据且无缓存, 其它推荐路由和 JSON 契约保持不变."""
    routes = {route.path: route for route in ROUTES}
    assert routes[RADAR].auth is AuthPolicy.COOKIE_OR_DEFAULT
    assert routes[RADAR].cache is None
    guess = routes["/recommend/get_guess_recommend"]
    assert guess.auth is AuthPolicy.OPTIONAL
    assert guess.cache is None
    for name in ("get_home_feed", "get_recommend_newsong", "get_recommend_songlist"):
        route = routes["/recommend/" + name]
        assert route.auth is AuthPolicy.NONE
        assert route.cache is not None
        assert route.cache.ttl == 60
    operation = route_app.openapi()["paths"][RADAR]["get"]
    assert operation["security"] == [{"MusicId": [], "MusicKey": []}]
    (page,) = operation["parameters"]
    assert (page["name"], page["in"], page["schema"]["default"]) == ("page", "query", 1)
    assert (
        operation["responses"]["200"]["content"]["application/json"]["schema"]["$ref"]
        == "#/components/schemas/ApiResponse_RadarRecommendResponse_"
    )
    report = routes["/report/play_duration"]
    assert report.auth is AuthPolicy.COOKIE_OR_DEFAULT
    assert report.cache is None
    assert report.methods == (HttpMethod.POST,)
    operation = route_app.openapi()["paths"]["/report/play_duration"]["post"]
    assert operation["security"] == [{"MusicId": [], "MusicKey": []}]
    assert {item["name"] for item in operation["parameters"]} == {"songmid", "listened_seconds"}


@pytest.mark.parametrize(
    ("value", "query", "expected"),
    [
        ("", "", 401),
        ("musicid=101", "", 422),
        (cookie(0), "", 401),
        (cookie(101), "page=invalid", 422),
    ],
)
async def test_guest_and_invalid_parameters_never_reach_engine(route_app, transport, value, query, expected):
    """游客与无效参数在本地拒绝, 不执行任何上游请求."""
    status, _, data = await request(route_app, query=query, cookie=value)
    assert status == expected
    assert data["code"] == -1
    assert transport.requests == []


def assert_radar_result(result, account, page, sequence):
    """验证账号相关响应字段和禁止缓存的响应头."""
    status, headers, data = result
    assert status == 200
    assert b"etag" not in headers
    assert b"cache-control" not in headers  # The outer launcher adds no-store.
    assert set(data) == {"code", "msg", "data"}
    assert (data["code"], data["msg"]) == (0, "ok")
    assert set(data["data"]) == {
        "songs",
        "recommend_song_ids",
        "base_song_ids",
        "has_more",
        "toast",
        "timestamp",
        "video_cards",
    }
    assert data["data"]["songs"][0]["id"] == account
    assert data["data"]["recommend_song_ids"] == [account, page, sequence]
    assert data["data"]["has_more"] is (page == 1)
    assert "synthetic-key" not in json.dumps(data)


def assert_radar_request(payload, account, page):
    """验证 CGI 参数只携带当前请求账号和页码."""
    assert payload["comm"]["qq"] == str(account)
    assert payload["comm"]["authst"] == f"synthetic-key-{account}"
    assert payload["comm"]["tmeLoginType"] == "2"
    assert payload["req_0"]["param"] == {"Page": page, "ReqType": 0, "FavSongs": [], "EntranceSongs": []}


async def test_accounts_pages_and_repeat_are_not_cached(route_app, transport):
    """每次雷达调用携带请求账号和页码, 相同账号重复请求也不复用结果."""
    for sequence, (account, page) in enumerate(((101, 1), (202, 1), (101, 2), (101, 1)), 1):
        query = "" if sequence == 1 else f"page={page}"
        result = await request(route_app, query=query, cookie=cookie(account))
        assert_radar_result(result, account, page, sequence)
        assert_radar_request(transport.radar[-1], account, page)
    assert len(transport.radar) == 4


async def test_concurrent_accounts_do_not_coalesce(route_app, transport):
    """并发同页的两个账号及重复调用分别执行, 不合并或串用凭据."""
    accounts = (101, 202, 101)
    results = await asyncio.gather(
        *(request(route_app, query="page=2", cookie=cookie(account)) for account in accounts)
    )
    assert len(transport.radar) == len(accounts)
    sequences = []
    for account, result in zip(accounts, results, strict=True):
        sequence = result[2]["data"]["recommend_song_ids"][2]
        sequences.append(sequence)
        assert_radar_result(result, account, 2, sequence)
        assert_radar_request(transport.radar[sequence - 1], account, 2)
    assert sorted(sequences) == [1, 2, 3]


@pytest.mark.parametrize(
    ("query", "song_type", "filename"),
    [
        ("", 0, "M500synthetic-midsynthetic-mid.mp3"),
        ("&song_type=1", 1, "M500synthetic-midsynthetic-mid.mp3"),
        ("&song_type=1&media_mid=synthetic-media", 1, "M500synthetic-media.mp3"),
    ],
)
async def test_audio_parameters_and_denial_are_preserved(route_app, transport, query, song_type, filename):
    """音源路由透传 M500 参数和请求账号, 不将无权限结果伪装成功."""
    status, _, data = await request(route_app, "/song/synthetic-mid/url", "file_type=13" + query, cookie(101))
    assert status == 200
    payload = transport.audio[-1]
    assert payload["comm"]["qq"] == "101"
    assert payload["comm"]["authst"] == "synthetic-key-101"
    assert payload["req_0"]["param"]["uin"] == "101"
    assert payload["req_0"]["param"]["songmid"] == ["synthetic-mid"]
    assert payload["req_0"]["param"]["songtype"] == [song_type]
    assert payload["req_0"]["param"]["filename"] == [filename]
    (row,) = data["data"]["data"]
    assert (row["mid"], row["result"], row["purl"]) == ("synthetic-mid", 104003, "")


def test_report_source_platform_boundary():
    """唯一 report 源码只使用 QQ 元数据, 不拼接其它平台或无法验证的字段."""
    source = (Path(__file__).resolve().parents[2] / "web/src/routes/report.py").read_text()
    for token in (
        "songId",
        "songmid: input",
        "sourceId",
        "sourcetype",
        "alg",
        "is_heart",
        "playend",
        "startplay",
        "code: 200",
        "vip_level",
        "supersound",
        "nettype",
        "playpage_time",
        "play_speed",
    ):
        assert token not in source, token
    for token in ("song.interval", "song.id", "row.vkey", "play_duration_mi", "songtype="):
        assert token in source, token
    assert 'type(payload.get("code")) is int' in source


@pytest.mark.parametrize(("account", "listened"), [(101, 1), (202, 9), (101, 86400)])
@pytest.mark.parametrize("lazy", [False, True])
async def test_report_uses_verified_qq_metadata(route_app, transport, account, listened, lazy):
    """上报只携带已验证的 QQ 元数据和当前账号, 墙钟秒数与音频时长保持独立."""
    transport.tracks = [
        {**track(), "id": 4321, "interval": 240, "singer": [{"id": 77, "mid": "SingerMid", "name": "合成歌手"}]},
    ]
    transport.audio_result, transport.audio_vkey = 0, "synthetic-vkey"
    transport._client.response.lazy = lazy
    before = int(time.time())
    status, headers, data = await request(
        route_app, REPORT, f"songmid=synthetic&listened_seconds={listened}", cookie(account), method="POST"
    )
    after = int(time.time())
    assert status == 200
    assert data == {"code": 0, "msg": "ok", "data": None}
    assert b"etag" not in headers
    assert b"cache-control" not in headers
    assert transport._client.response.gathered is lazy
    ((url, sent),) = transport._client.requests
    assert url == "https://stat6.y.qq.com/android/fcgi-bin/imusic_tj"
    assert not sent["allow_redirects"]
    assert sent["timeout"] == 20
    assert sent["headers"]["Content-Encoding"] == "gzip"
    assert sent["headers"]["Host"] == "stat6.y.qq.com"
    assert sent["cookies"] == {
        "uin": str(account),
        "qqmusic_uin": str(account),
        "qm_keyst": f"synthetic-key-{account}",
        "qqmusic_key": f"synthetic-key-{account}",
    }
    xml = ET.fromstring(gzip.decompress(sent["data"]))
    (item,) = xml.findall("item")
    optime = item.attrib["optime"]
    assert before <= int(optime) <= after
    assert item.attrib == {
        "cmd": "1",
        "optime": optime,
        "QQ": str(account),
        "uid": "synthetic-uid",
        "songtype": "1",
        "playtype": "5",
        "time": str(listened),
        "play_duration_mi": str(listened * 1000),
        "audiotime": "240000",
        "songid": "4321",
        "singerid": "77",
        "vkey": "synthetic-vkey",
        "timekey": hashlib.md5(f"{optime}{listened}{account}gk2$Lh-&l4#!4iow".encode()).hexdigest().upper(),
    }
    assert xml.findtext("qq") == str(account)
    assert xml.findtext("authst") == f"synthetic-key-{account}"
    assert xml.findtext("cid") == "228"
    (audio,) = transport.audio
    assert audio["comm"]["qq"] == str(account)
    assert audio["comm"]["authst"] == f"synthetic-key-{account}"
    assert audio["req_0"]["param"]["uin"] == str(account)
    assert audio["req_0"]["param"]["songmid"] == ["synthetic"]
    assert audio["req_0"]["param"]["filename"] == ["M500syntheticsynthetic.mp3"]


@pytest.mark.parametrize(
    ("value", "query", "expected"),
    [
        ("", "songmid=synthetic&listened_seconds=9", 401),
        ("musicid=101", "songmid=synthetic&listened_seconds=9", 422),
        (cookie(0), "songmid=synthetic&listened_seconds=9", 401),
        (cookie(101), "listened_seconds=9", 422),
        (cookie(101), "songmid=synthetic", 422),
        (cookie(101), "songmid=bad-mid&listened_seconds=9", 422),
        (cookie(101), "songmid=" + "x" * 65 + "&listened_seconds=9", 422),
        (cookie(101), "songmid=synthetic&listened_seconds=0", 422),
        (cookie(101), "songmid=synthetic&listened_seconds=86401", 422),
        (cookie(101), "songmid=synthetic&listened_seconds=1.5", 422),
        (cookie(101), "songmid=synthetic&listened_seconds=true", 422),
    ],
)
async def test_report_invalid_input_never_reaches_engine(route_app, transport, value, query, expected):
    """未认证账号及无效歌曲标识或时长在本地拒绝, 不查询元数据或发送上报."""
    status, _, data = await request(route_app, REPORT, query, value, method="POST")
    assert status == expected
    assert data["code"] == -1
    assert transport.requests == []
    assert transport._client.requests == []


@pytest.mark.parametrize(
    "tracks",
    [[], [track(), track()], [{**track(), "mid": "wrong"}], [{**track(), "id": 0}], [{**track(), "interval": 0}]],
    ids=["missing", "ambiguous", "wrong-mid", "invalid-id", "invalid-duration"],
)
async def test_report_requires_verified_identity(route_app, transport, tracks):
    """元数据无法唯一确认歌曲身份或时长时拒绝上报, 不申请播放授权."""
    transport.tracks = tracks
    status, _, data = await request(
        route_app, REPORT, "songmid=synthetic&listened_seconds=9", cookie(101), method="POST"
    )
    assert status == 422
    assert data["code"] == -1
    assert transport.audio == []
    assert transport._client.requests == []


@pytest.mark.parametrize(
    ("result", "vkey", "mid"),
    [(104003, "synthetic-vkey", "synthetic"), (0, "", "synthetic"), (0, "synthetic-vkey", "wrong")],
)
async def test_report_requires_account_vkey(route_app, transport, result, vkey, mid):
    """上报要求当前歌曲授权成功且拥有非空 vkey, 不接受其它歌曲的授权."""
    transport.audio_result, transport.audio_vkey, transport.audio_mid = result, vkey, mid
    status, _, data = await request(
        route_app, REPORT, "songmid=synthetic&listened_seconds=9", cookie(101), method="POST"
    )
    assert status == 403
    assert data["code"] == -1
    assert len(transport.audio) == 1
    assert transport._client.requests == []


@pytest.mark.parametrize(
    ("status_code", "body", "accepted"),
    [
        (200, b"\0" * 5 + zlib.compress(b'{"code":0}'), True),
        (500, b"\0" * 5 + zlib.compress(b'{"code":0}'), False),
        (200, b"", False),
        (200, b"\0" * 5, False),
        (200, b"wrong" + zlib.compress(b'{"code":0}'), False),
        (200, b"\0" * 5 + b"not-zlib", False),
        (200, b"\0" * 5 + zlib.compress(b"not-json"), False),
        *[
            (200, b"\0" * 5 + zlib.compress(json.dumps({"code": code}).encode()), False)
            for code in (False, True, "0", 0.0, None, -1, 200)
        ],
        (200, b"\0" * 5 + zlib.compress(b"{}"), False),
    ],
)
async def test_report_acknowledgement_is_strict(route_app, transport, status_code, body, accepted):
    """仅合法二进制回执中的整数零表示成功, 布尔值和其它平台成功码不算确认."""
    transport.audio_result, transport.audio_vkey = 0, "synthetic-vkey"
    transport._client.response.status = status_code
    transport._client.response.body = body
    status, _, data = await request(
        route_app, REPORT, "songmid=synthetic&listened_seconds=9", cookie(101), method="POST"
    )
    assert status == 200
    assert data == {
        "code": 0 if accepted else -1,
        "msg": "ok" if accepted else "操作失败",
        "data": None,
    }
    assert len(transport._client.requests) == 1


@pytest.fixture
def transport():
    """为每个用例提供独立的离线传输桩."""
    return OfflineTransport()


@pytest.fixture
async def route_app(transport):
    """使用真实路由和引擎, 不启动账号存储或加载账号种子."""
    engine = RequestEngine.create(transport=transport, device_path=None)
    app = create_app()
    services = app.state.services
    services.engine = engine
    services.credential_config = CredentialConfig(enabled=False)
    services.cache = services.coalescer = NoSharedResults()
    assert services.credential_pool is None
    try:
        yield app
    finally:
        await engine.close()
