"""Fixed, account-bound playback report used by iWebPlus.

This is a controlled adaptation of the pinned upstream example
``examples/sound_power.py``. It reuses the existing engine, credential and
Android session transport. Fields whose real values are not available from the
verified request/credential are omitted rather than fabricated.
"""

import gzip
import hashlib
import json
import time
import zlib
from typing import TYPE_CHECKING, Any, cast

from fastapi import HTTPException

from qqmusic_api import Platform
from qqmusic_api.modules.song import SongApi, SongFileInfo, SongFileType, SongQueryInfo

from ..routing.adapter_registry import adapter
from ..routing.route_types import AuthPolicy, HttpMethod, RouteContext
from ._helpers import Q, R

if TYPE_CHECKING:
    from qqmusic_api.core.transport import NiquestsTransport

REPORT_URL = "https://stat6.y.qq.com/android/fcgi-bin/imusic_tj"

ROUTES = (
    R(
        "song",
        "play_duration",
        "/report/play_duration",
        bool,
        methods=(HttpMethod.POST,),
        params=(
            Q("songmid", str, description="Verified native song MID."),
            Q("listened_seconds", int, description="Effective wall-clock seconds."),
        ),
        auth=AuthPolicy.COOKIE_OR_DEFAULT,
        summary="Submit one verified playback snapshot",
        description="iWebPlus fixed action; no arbitrary URL, cookie or header is accepted.",
    ),
)


@adapter("song", "play_duration")
async def play_duration_adapter(context: RouteContext) -> bool:
    """Send exactly one report item for the authenticated account."""
    songmid = context.params["songmid"]
    listened = context.params["listened_seconds"]
    if not isinstance(songmid, str) or not songmid.isalnum() or len(songmid) > 64:
        raise HTTPException(status_code=422, detail="invalid songmid")
    if type(listened) is not int or listened < 1 or listened > 86400:
        raise HTTPException(status_code=422, detail="invalid listened_seconds")
    credential = context.credential
    if credential is None or not credential.is_valid():
        raise HTTPException(status_code=401, detail="credential required")

    tracks = (
        await context.execute_module(
            SongApi,
            SongApi.query_song,
            song_info=[SongQueryInfo(mid=songmid)],
        )
    ).tracks
    if len(tracks) != 1 or tracks[0].mid != songmid or tracks[0].id <= 0 or tracks[0].interval <= 0:
        raise HTTPException(status_code=422, detail="song identity not confirmed")
    song = tracks[0]
    urls = await context.execute_module(
        SongApi,
        SongApi.get_song_urls,
        file_info=[SongFileInfo(mid=songmid)],
        file_type=SongFileType.MP3_128,
    )
    row = next((item for item in urls.data if item.mid == songmid and item.result == 0 and item.vkey), None)
    if row is None:
        raise HTTPException(status_code=403, detail="no vkey for this account")

    cgi_exec = cast("Any", context.engine)._cgi
    device = await cgi_exec._device_store.get_device()
    session = await cgi_exec._android_session.ensure()
    comm = context.engine.version_policy.build_comm(
        platform=Platform.ANDROID,
        credential=credential,
        device=device,
        qimei=await cgi_exec._qimei_manager.get_cached(),
        guid=device.open_udid,
        session=session,
    )
    optime = int(time.time())
    account = str(credential.musicid)
    timekey = hashlib.md5(f"{optime}{listened}{account}gk2$Lh-&l4#!4iow".encode()).hexdigest().upper()
    singer = song.singer[0] if song.singer else None
    # Only observed values are included. The upstream example's fixed VIP,
    # sound-quality, network and page-time fields describe facts we cannot
    # verify, so they are deliberately absent.
    item = (
        f'<item cmd="1" optime="{optime}" QQ="{account}" uid="{session.uid}" '
        f'songtype="1" playtype="5" time="{listened}" play_duration_mi="{listened * 1000}" '
        f'audiotime="{song.interval * 1000}" songid="{song.id}" '
        f'singerid="{singer.id if singer else 0}" vkey="{row.vkey}" timekey="{timekey}"/>'
    )
    xml = (
        '<?xml version="1.0" encoding="UTF-8"?>\r\n<root>\r\n'
        + "".join(f"    <{key}>{value}</{key}>\r\n" for key, value in comm.items())
        + f"    <cid>228</cid>\r\n    {item}\r\n</root>"
    )
    client = context.engine
    # Reuse the engine's AsyncSession (proxy trust disabled at startup) and
    # assemble the same account cookies its HTTP executor would attach.
    cookies = {
        "uin": credential.str_musicid or str(credential.musicid),
        "qqmusic_uin": credential.str_musicid or str(credential.musicid),
    }
    if credential.musickey:
        cookies["qm_keyst"] = credential.musickey
        cookies["qqmusic_key"] = credential.musickey
    transport = cast("NiquestsTransport", client.transport)
    response = await transport._client.post(
        REPORT_URL,
        data=gzip.compress(xml.encode()),
        cookies=cookies,
        allow_redirects=False,
        timeout=20,
        headers={
            "User-Agent": client.version_policy.get_user_agent(Platform.ANDROID, device),
            "Content-Encoding": "gzip",
            "Host": "stat6.y.qq.com",
            "Accept": "*/*",
        },
    )
    # niquests may return a lazy/multiplexed response; gather it before any
    # status or body access, matching the upstream transport's own behavior.
    if getattr(response, "lazy", False):
        await transport._client.gather(response)
    content = response.content or b""
    if response.status_code != 200 or len(content) <= 5 or content[:5] != b"\x00" * 5:
        return False
    try:
        payload = json.loads(zlib.decompress(content[5:]).decode("utf-8", "replace"))
    except (ValueError, zlib.error):
        return False
    # Strict type check: the upstream example accepts `False == 0` by accident.
    return type(payload.get("code")) is int and payload["code"] == 0
