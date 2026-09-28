"""Emby 集成 —— 按 TMDB ID 在 Emby 媒体库里找到对应的剧，读出它已入库的集数。

订阅追更拿这份"Emby 已入库集数"跟自己的转存记录取并集：如果某一集你是靠别的
方式（其它下载器、手动传、旧的资源）已经导进 Emby 了，gygo 就不会再重复转存
这一集——不管它自己有没有转存记录。

匹配方式：Emby 的剧集条目（Series）如果刮削了 TheMovieDb 元数据插件，会在
ProviderIds 里带一个 "Tmdb": "<tmdb_id>"（这是剧集级 ID，注意不是单集级的
Tmdb ID，两者不是一回事）。这里拉库里所有 Series 条目、按 ProviderIds.Tmdb
匹配，找到对应的 Series Item Id 后，再用 Emby 的 /Shows/{Id}/Episodes 接口
读出已有哪些集（可选按季筛选）。
"""

import json
import time
import urllib.error
import urllib.parse
import urllib.request

import monitor_store

TIMEOUT = 12

_library_cache = {"ts": 0, "map": {}}  # tmdb_id(str) -> Emby series Item Id
_LIBRARY_TTL = 600  # 库里所有剧的 TMDB ID 映射缓存 10 分钟，供推荐页"已入库"角标/详情页用


def get_config():
    cfg = monitor_store.load_emby()
    return {"host": cfg.get("host") or "", "api_key": cfg.get("api_key") or "",
            "enabled": bool(cfg.get("enabled", False))}


def save_config(host, api_key, enabled):
    host = (host or "").strip().rstrip("/")
    api_key = (api_key or "").strip()
    monitor_store.save_emby(host=host, api_key=api_key, enabled=bool(enabled))
    return get_config()


def _get(host, api_key, path, params=None):
    q = dict(params or {})
    q["api_key"] = api_key
    url = host.rstrip("/") + path + "?" + urllib.parse.urlencode(q)
    req = urllib.request.Request(url, headers={"X-Emby-Token": api_key,
                                                "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        return None, "HTTP %s：%s" % (e.code, (detail or e.reason)[:200])
    except urllib.error.URLError as e:
        return None, "网络请求失败：%s（服务器地址填对了吗，容器能连到这个地址吗）" % e
    except Exception as e:
        return None, "请求异常：%s" % e
    if not raw:
        return {}, None
    try:
        return json.loads(raw), None
    except Exception:
        return None, "Emby 返回了非预期内容"


def test(host, api_key):
    host = (host or "").strip()
    api_key = (api_key or "").strip()
    if not host or not api_key:
        return False, "请先填写服务器地址和 API Key"
    data, err = _get(host, api_key, "/System/Info")
    if err:
        return False, err
    name = data.get("ServerName") or "Emby"
    ver = data.get("Version") or "?"
    return True, "连通成功：%s（版本 %s）" % (name, ver)


def find_series_id(host, api_key, tmdb_id):
    """在整个库里按 ProviderIds.Tmdb 找剧，返回 (series_id, series_name, err)。"""
    data, err = _get(host, api_key, "/Items", {
        "IncludeItemTypes": "Series", "Recursive": "true", "Fields": "ProviderIds"})
    if err:
        return None, None, err
    tmdb_id = str(tmdb_id)
    for it in (data.get("Items") or []):
        pids = it.get("ProviderIds") or {}
        for k, v in pids.items():
            if k.lower() == "tmdb" and str(v) == tmdb_id:
                return it.get("Id"), it.get("Name"), None
    return None, None, ("Emby 库里没找到 TMDB ID=%s 对应的剧"
                        "（还没入库，或者刮削用的元数据源不是 TMDB）" % tmdb_id)


def get_library_map(host, api_key, force=False):
    """返回 (tmdb_id字符串 -> Emby Series Item Id 的字典, err)，带缓存（10分钟）。
    推荐页"已入库"角标、详情页"每季入库集数"都基于这份映射，只用一次全库查询
    就够，不会随要判断的剧数量增加而多打 Emby。查询失败时沿用上一次的缓存
    （数据暂时不是最新的，好过页面直接报错）。
    """
    now = time.time()
    if not force and _library_cache["map"] and (now - _library_cache["ts"]) < _LIBRARY_TTL:
        return _library_cache["map"], None
    data, err = _get(host, api_key, "/Items", {
        "IncludeItemTypes": "Series", "Recursive": "true", "Fields": "ProviderIds"})
    if err:
        return _library_cache["map"], err
    m = {}
    for it in (data.get("Items") or []):
        pids = it.get("ProviderIds") or {}
        for k, v in pids.items():
            if k.lower() == "tmdb" and v:
                m[str(v)] = it.get("Id")
    _library_cache["ts"] = now
    _library_cache["map"] = m
    return m, None


def get_library_tmdb_ids(host, api_key, force=False):
    """返回 (tmdb_id集合, err)——get_library_map 的简化版，只要有没有，不要 Id。"""
    m, err = get_library_map(host, api_key, force=force)
    return set(m.keys()), err


def fetch_episodes_grouped_by_season(host, api_key, series_id):
    """返回 (季号 -> 已入库集数集合 的字典, err)。给详情页"每季入库了多少集"用——
    一次性把整部剧的分集都拉下来按季分组，比每季单独查一次快。
    """
    params = {"Fields": "IndexNumber,ParentIndexNumber", "Recursive": "true"}
    data, err = _get(host, api_key, "/Shows/%s/Episodes" % series_id, params)
    if err:
        return None, err
    grouped = {}
    for it in (data.get("Items") or []):
        s, e = it.get("ParentIndexNumber"), it.get("IndexNumber")
        if s is None or e is None:
            continue
        try:
            s, e = int(s), int(e)
        except (TypeError, ValueError):
            continue
        grouped.setdefault(s, set()).add(e)
    return grouped, None


def fetch_have_episodes(host, api_key, tmdb_id, season=None, series_id=None):
    """返回 (episode_number集合, series_id, err)。传了 series_id 就跳过按 tmdb 查找那步。"""
    if not series_id:
        series_id, _name, err = find_series_id(host, api_key, tmdb_id)
        if err:
            return None, None, err
    params = {"Fields": "IndexNumber,ParentIndexNumber", "Recursive": "true"}
    if season not in (None, "", 0):
        params["Season"] = str(season)
    data, err = _get(host, api_key, "/Shows/%s/Episodes" % series_id, params)
    if err:
        return None, series_id, err
    eps = set()
    for it in (data.get("Items") or []):
        idx = it.get("IndexNumber")
        if idx is None:
            continue
        try:
            eps.add(int(idx))
        except (TypeError, ValueError):
            continue
    return eps, series_id, None


def resolve_have_episodes(sub, cfg):
    """给订阅扫描用的封装：优先用缓存的 series_id，查失败了（比如 Emby 那边重新
    刮削换了条目 Id）就清缓存重新按 tmdb_id 找一次再试一遍。
    返回 (episodes集合_or_None, series_id_or_None, err_or_None)。
    """
    tmdb_id = sub.get("tmdb_id")
    sid = sub.get("emby_series_id")
    if sid:
        eps, _sid, err = fetch_have_episodes(cfg["host"], cfg["api_key"], tmdb_id,
                                             season=sub.get("season"), series_id=sid)
        if not err:
            return eps, sid, None
    eps, sid2, err = fetch_have_episodes(cfg["host"], cfg["api_key"], tmdb_id,
                                         season=sub.get("season"))
    return eps, sid2, err
