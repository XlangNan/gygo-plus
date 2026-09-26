"""TMDB（themoviedb.org）集成 —— 按 tmdb_id 查一部剧的总集数 / 分季集数。

用的是 v3 API，key 在 https://www.themoviedb.org/settings/api 免费申请
（选 "API Key (v3 auth)" 那个，不是需要审核的 v4 那个）。
"""

import json
import urllib.error
import urllib.parse
import urllib.request

import monitor_store

BASE = "https://api.themoviedb.org/3"
TIMEOUT = 10


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
    """返回 (info, err)。info = {"name", "total_episodes", "seasons":[...]}"""
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
    return {"name": name, "total_episodes": data.get("number_of_episodes") or 0,
            "seasons": seasons}, None


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
