"""TMDB（themoviedb.org）集成 —— 按 tmdb_id 查一部剧的总集数 / 分季集数。

用的是 v3 API，key 在 https://www.themoviedb.org/settings/api 免费申请
（选 "API Key (v3 auth)" 那个，不是需要审核的 v4 那个）。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import monitor_store

BASE = "https://api.themoviedb.org/3"
IMG_BASE = "https://image.tmdb.org/t/p/w500"
IMG_POSTER = "https://image.tmdb.org/t/p/w342"
IMG_BACKDROP = "https://image.tmdb.org/t/p/w780"
IMG_PROFILE = "https://image.tmdb.org/t/p/w185"
TIMEOUT = 10

# 推荐页三个大类：按"剧集来源国家"分组，pipe(|) 在 TMDB discover 接口里表示"或"。
# 国家代码分组照抄自用户自己另一个 Emby 插件项目（EMBY剧集完结标志插件）里用的分类表。
REGION_PRESETS = {
    "kr_jp": {"label": "日韩剧", "countries": "JP|KP|KR|TH|IN|SG"},
    "us_eu": {"label": "欧美剧", "countries": "US|FR|GB|DE|ES|IT|NL|PT|RU"},
    "cn": {"label": "国产剧", "countries": "CN|TW|HK"},
}
SORT_PRESETS = {
    "popularity": "popularity.desc",
    "rating": "vote_average.desc",
    "latest": "first_air_date.desc",
}

_cache = {}         # {key: (存入时间, 结果)}
_CACHE_TTL = 600     # 首页推荐/列表缓存 10 分钟，不用每次切页面都打一遍 TMDB
_DETAIL_CACHE_TTL = 1800   # 详情页缓存 30 分钟，剧集信息不常变


def _cached(key, ttl, fetch_fn):
    now = time.time()
    hit = _cache.get(key)
    if hit and (now - hit[0]) < ttl:
        return hit[1]
    result = fetch_fn()
    _cache[key] = (now, result)
    return result


def get_config():
    cfg = monitor_store.load_tmdb()
    return {"api_key": cfg.get("api_key") or ""}


def save_config(api_key):
    api_key = (api_key or "").strip()
    monitor_store.save_tmdb(api_key=api_key)
    return get_config()


def _get(path, api_key, params=None):
    q = dict(params or {})
    q["api_key"] = api_key
    q.setdefault("language", "zh-CN")
    url = BASE + path + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            j = json.loads(detail)
            detail = j.get("status_message") or detail
        except Exception:
            pass
        return None, "HTTP %s：%s" % (e.code, (detail or e.reason)[:200])
    except urllib.error.URLError as e:
        return None, "网络请求失败：%s" % e
    except Exception as e:
        return None, "请求异常：%s" % e
    try:
        return json.loads(raw), None
    except Exception:
        return None, "TMDB 返回了非预期内容"


def fetch_tv_info(tmdb_id, api_key):
    """返回 (info, err)。info = {"name", "total_episodes", "seasons":[...],
    "poster_url", "overview"}"""
    data, err = _get("/tv/%s" % tmdb_id, api_key)
    if err:
        return None, err
    if data.get("success") is False:
        return None, data.get("status_message") or "TMDB 查询失败"
    name = data.get("name") or data.get("original_name") or ""
    seasons = []
    for s in (data.get("seasons") or []):
        if s.get("season_number") is None:
            continue
        seasons.append({"season_number": s.get("season_number"),
                        "episode_count": s.get("episode_count") or 0,
                        "name": s.get("name") or ""})
    poster_path = data.get("poster_path") or ""
    return {"name": name, "total_episodes": data.get("number_of_episodes") or 0,
            "seasons": seasons, "poster_url": (IMG_BASE + poster_path) if poster_path else "",
            "overview": data.get("overview") or ""}, None


def fetch_season_episode_count(tmdb_id, season, api_key):
    data, err = _get("/tv/%s/season/%s" % (tmdb_id, season), api_key)
    if err:
        return None, err
    if data.get("success") is False:
        return None, data.get("status_message") or "TMDB 查询失败（可能这一季不存在）"
    eps = data.get("episodes") or []
    return {"episode_count": len(eps), "name": data.get("name") or ""}, None


def resolve_total_episodes(tmdb_id, season, api_key):
    """按 tmdb_id (+ 可选季号) 算出总集数和剧名。返回 (total_episodes, name, err)。"""
    info, err = fetch_tv_info(tmdb_id, api_key)
    if err:
        return None, None, err
    name = info["name"]
    if season in (None, "", 0):
        return info["total_episodes"], name, None
    s, err2 = fetch_season_episode_count(tmdb_id, season, api_key)
    if err2:
        return None, name, err2
    season_name = s.get("name") or ("第%s季" % season)
    return s["episode_count"], "%s %s" % (name, season_name), None


def resolve_subscription_meta(tmdb_id, season, api_key):
    """新建订阅 / 刷新订阅信息时用：一次查出总集数、剧名、海报、简介。
    返回 (meta_or_None, err)。meta = {"name","total_episodes","poster_url","overview"}
    即使总集数那一步（分季）查询失败，只要剧集主信息查到了，也会把 name/poster/overview
    带回去（err 会非空提示总集数没查到，调用方自己决定要不要紧着这个失败）。
    """
    info, err = fetch_tv_info(tmdb_id, api_key)
    if err:
        return None, err
    name = info["name"]
    total = info["total_episodes"]
    meta = {"name": name, "total_episodes": total,
            "poster_url": info["poster_url"], "overview": info["overview"]}
    if season in (None, "", 0):
        return meta, None
    s, err2 = fetch_season_episode_count(tmdb_id, season, api_key)
    if err2:
        meta["total_episodes"] = None
        return meta, err2
    season_name = s.get("name") or ("第%s季" % season)
    meta["name"] = "%s %s" % (name, season_name)
    meta["total_episodes"] = s["episode_count"]
    return meta, None


def test(api_key):
    api_key = (api_key or "").strip()
    if not api_key:
        return False, "请先填写 TMDB API Key"
    data, err = _get("/tv/1399", api_key)  # 拿权游当探活样本，公开剧集不会出隐私问题
    if err:
        return False, err
    if data.get("success") is False:
        return False, data.get("status_message") or "Key 无效"
    return True, "连通成功（示例：%s，共 %s 集）" % (
        data.get("name") or "TMDB", data.get("number_of_episodes") or "?")


# ------------------------------------------------------------------ 剧集推荐 / 浏览

def _img(path, base):
    return (base + path) if path else ""


def _brief(it):
    return {
        "id": it.get("id"),
        "name": it.get("name") or it.get("original_name") or "",
        "poster_url": _img(it.get("poster_path"), IMG_POSTER),
        "overview": it.get("overview") or "",
        "vote_average": round(it.get("vote_average") or 0, 1),
        "popularity": it.get("popularity") or 0,
        "first_air_date": it.get("first_air_date") or "",
    }


def discover_tv(api_key, category="all", sort="popularity", page=1, per_page=20):
    """浏览/推荐用：category 是 REGION_PRESETS 的 key 或 "all"；sort 是
    SORT_PRESETS 的 key。返回 (result, err)，result = {items, page, total_pages,
    total_results}。有缓存，同样的参数 10 分钟内不会重复打 TMDB。
    """
    region = REGION_PRESETS.get(category)
    sort_by = SORT_PRESETS.get(sort, "popularity.desc")
    key = ("discover", category, sort, page, per_page)

    def _fetch():
        params = {"sort_by": sort_by, "page": page, "include_adult": "false",
                  "vote_count.gte": 20}  # 过滤掉只有几个人投票、评分不靠谱的冷门条目
        if region:
            params["with_origin_country"] = region["countries"]
        data, err = _get("/discover/tv", api_key, params)
        if err:
            return None, err
        items = [_brief(it) for it in (data.get("results") or [])[:per_page]]
        return {"items": items, "page": data.get("page") or 1,
                "total_pages": min(data.get("total_pages") or 1, 500),
                "total_results": data.get("total_results") or 0}, None

    return _cached(key, _CACHE_TTL, _fetch)


def fetch_home_rows(api_key):
    """推荐首页：日韩剧/欧美剧/国产剧三行，每行按热度取 20 部。"""
    rows = []
    for cat in ("kr_jp", "us_eu", "cn"):
        result, err = discover_tv(api_key, category=cat, sort="popularity", page=1, per_page=20)
        rows.append({"category": cat, "label": REGION_PRESETS[cat]["label"],
                     "items": (result or {}).get("items", []), "err": err})
    return rows


def search_tv(api_key, query, page=1):
    """按名称搜剧，TMDB 自己的 /search/tv 接口本身就支持模糊/部分匹配，
    不用在这边再另外做模糊逻辑。"""
    query = (query or "").strip()
    if not query:
        return {"items": [], "page": 1, "total_pages": 1, "total_results": 0}, None
    key = ("search", query, page)

    def _fetch():
        params = {"query": query, "page": page, "include_adult": "false", "language": "zh-CN"}
        data, err = _get("/search/tv", api_key, params)
        if err:
            return None, err
        items = [_brief(it) for it in (data.get("results") or [])]
        return {"items": items, "page": data.get("page") or 1,
                "total_pages": min(data.get("total_pages") or 1, 500),
                "total_results": data.get("total_results") or 0}, None

    return _cached(key, _CACHE_TTL, _fetch)


def fetch_tv_detail(tmdb_id, api_key):
    """详情页：简介、评分、季数/集数、类型、演员表等。"""
    key = ("detail", str(tmdb_id))

    def _fetch():
        data, err = _get("/tv/%s" % tmdb_id, api_key, {"append_to_response": "credits"})
        if err:
            return None, err
        if data.get("success") is False:
            return None, data.get("status_message") or "TMDB 查询失败"
        cast = []
        for c in ((data.get("credits") or {}).get("cast") or [])[:16]:
            cast.append({"name": c.get("name") or "", "character": c.get("character") or "",
                        "profile_url": _img(c.get("profile_path"), IMG_PROFILE)})
        genres = [g.get("name") for g in (data.get("genres") or []) if g.get("name")]
        seasons = []
        for s in (data.get("seasons") or []):
            if s.get("season_number") is None:
                continue
            seasons.append({"season_number": s.get("season_number"),
                            "name": s.get("name") or "",
                            "episode_count": s.get("episode_count") or 0,
                            "air_date": s.get("air_date") or "",
                            "poster_url": _img(s.get("poster_path"), IMG_POSTER)})
        detail = {
            "id": data.get("id"),
            "name": data.get("name") or data.get("original_name") or "",
            "overview": data.get("overview") or "",
            "poster_url": _img(data.get("poster_path"), IMG_POSTER),
            "backdrop_url": _img(data.get("backdrop_path"), IMG_BACKDROP),
            "first_air_date": data.get("first_air_date") or "",
            "vote_average": round(data.get("vote_average") or 0, 1),
            "vote_count": data.get("vote_count") or 0,
            "number_of_seasons": data.get("number_of_seasons") or 0,
            "number_of_episodes": data.get("number_of_episodes") or 0,
            "status": data.get("status") or "",
            "genres": genres,
            "seasons": seasons,
            "cast": cast,
        }
        return detail, None

    return _cached(key, _DETAIL_CACHE_TTL, _fetch)
