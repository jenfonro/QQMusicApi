"""推荐 Web 路由契约."""

from qqmusic_api.modules.recommend import RecommendApi

from ..routing.route_types import PUBLIC_60, AuthPolicy, WebRoute
from ._helpers import R

ROUTES: tuple[WebRoute, ...] = (
    R(
        RecommendApi.get_guess_recommend,
        "/recommend/get_guess_recommend",
        auth=AuthPolicy.OPTIONAL,
    ),
    R(RecommendApi.get_home_feed, "/recommend/get_home_feed", cache=PUBLIC_60),
    # iWebPlus, 2026-10-18: 雷达推荐与账号绑定, 且不能进入公共缓存或请求合并.
    R(
        RecommendApi.get_radar_recommend,
        "/recommend/get_radar_recommend",
        auth=AuthPolicy.COOKIE_OR_DEFAULT,
    ),
    R(
        RecommendApi.get_recommend_newsong,
        "/recommend/get_recommend_newsong",
        cache=PUBLIC_60,
    ),
    R(
        RecommendApi.get_recommend_songlist,
        "/recommend/get_recommend_songlist",
        cache=PUBLIC_60,
    ),
)
