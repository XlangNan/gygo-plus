"""监控项持久化 —— 单个 JSON 文件，线程安全。

存两份东西：
  monitors  : 分享链接监控项
  auth      : 光鸭登录态（access_token / refresh_token / device_id）
"""

import json
import os
import threading
import time

DATA_DIR = os.environ.get("GYGO_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
DB_FILE = os.path.join(DATA_DIR, "monitors.json")

MIN_INTERVAL = 60  # 扫描间隔下限（分钟），基于风控考虑，不建议调更低

_lock = threading.RLock()


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _empty():
    return {"monitors": [], "subscriptions": [], "auth": {}, "dingtalk": {},
            "smartstrm": {}, "tmdb": {}, "emby": {}, "seq": 0}


def _load():
    if not os.path.exists(DB_FILE):
        return _empty()
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return _empty()
    if not isinstance(data, dict):
        return _empty()
    data.setdefault("monitors", [])
    data.setdefault("subscriptions", [])
    data.setdefault("auth", {})
    data.setdefault("dingtalk", {})
    data.setdefault("smartstrm", {})
    data.setdefault("tmdb", {})
    data.setdefault("emby", {})
    data.setdefault("seq", 0)
    return data


def _save(data):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = DB_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DB_FILE)


# ------------------------------------------------------------------ 监控项

def list_all():
    with _lock:
        return list(_load()["monitors"])


def get(mid):
    with _lock:
        for m in _load()["monitors"]:
            if m.get("id") == mid:
                return m
    return None


def add(fields):
    with _lock:
        data = _load()
        data["seq"] += 1
        item = {
            "id": data["seq"],
            "share_url": "",
            "share_id": "",
            "passcode": "",
            "pdir_fid": "",
            "link_name": "",
            "target_path": "",
            "interval_min": MIN_INTERVAL,
            "enabled": True,
            "status": "pending",       # pending / ok / error / invalid / paused
            "last_files": [],          # 上一次见到的 fid 集合，用于做差集
            "last_scan": "",
            "last_result": "",
            "added_at": _now(),
        }
        item.update(fields or {})
        item["interval_min"] = max(MIN_INTERVAL, int(item.get("interval_min") or MIN_INTERVAL))
        data["monitors"].append(item)
        _save(data)
        return dict(item)


def update(mid, **fields):
    with _lock:
        data = _load()
        for m in data["monitors"]:
            if m.get("id") == mid:
                m.update(fields)
                _save(data)
                return dict(m)
    return None


def remove(mid):
    with _lock:
        data = _load()
        before = len(data["monitors"])
        data["monitors"] = [m for m in data["monitors"] if m.get("id") != mid]
        _save(data)
        return len(data["monitors"]) < before


# ------------------------------------------------------------------ 登录态

def load_auth():
    with _lock:
        return dict(_load()["auth"])


def save_auth(**fields):
    with _lock:
        data = _load()
        data["auth"].update(fields)
        data["auth"]["updated_at"] = _now()
        _save(data)


# ------------------------------------------------------------------ 钉钉机器人通知配置

def load_dingtalk():
    with _lock:
        return dict(_load()["dingtalk"])


def save_dingtalk(**fields):
    with _lock:
        data = _load()
        data["dingtalk"].update(fields)
        data["dingtalk"]["updated_at"] = _now()
        _save(data)
        return dict(data["dingtalk"])


# ------------------------------------------------------------------ 联动 SmartStrm 配置

def load_smartstrm():
    with _lock:
        return dict(_load()["smartstrm"])


def save_smartstrm(**fields):
    with _lock:
        data = _load()
        data["smartstrm"].update(fields)
        data["smartstrm"]["updated_at"] = _now()
        _save(data)
        return dict(data["smartstrm"])


# ------------------------------------------------------------------ TMDB 配置

def load_tmdb():
    with _lock:
        return dict(_load()["tmdb"])


def save_tmdb(**fields):
    with _lock:
        data = _load()
        data["tmdb"].update(fields)
        data["tmdb"]["updated_at"] = _now()
        _save(data)
        return dict(data["tmdb"])


# ------------------------------------------------------------------ Emby 配置

def load_emby():
    with _lock:
        return dict(_load()["emby"])


def save_emby(**fields):
    with _lock:
        data = _load()
        data["emby"].update(fields)
        data["emby"]["updated_at"] = _now()
        _save(data)
        return dict(data["emby"])


# ------------------------------------------------------------------ 订阅追更
# 一个订阅 = 一部剧（可选绑定 TMDB ID 查总集数）+ 多个分享链接。
# 以「集数」为基线（have: {"3": {...}}），不是以单条分享的 fid 集合为基线，
# 所以同一集不管从哪条链接来的都只会转一次。

def list_subscriptions():
    with _lock:
        return list(_load()["subscriptions"])


def get_subscription(sid):
    with _lock:
        for s in _load()["subscriptions"]:
            if s.get("id") == sid:
                return s
    return None


def add_subscription(fields):
    with _lock:
        data = _load()
        data["seq"] += 1
        item = {
            "id": data["seq"],
            "name": "",
            "tmdb_id": "",
            "season": None,           # None = 不分季，按整部剧的总集数
            "total_episodes": None,   # None = 未知，不判断"缺了哪几集"，来什么转什么
            "poster_url": "",         # TMDB 海报图 URL（新建/刷新时自动查）
            "overview": "",           # TMDB 简介
            "target_path": "",
            "keep_tree": True,
            "interval_min": MIN_INTERVAL,
            "enabled": True,
            "status": "pending",      # pending/ok/error/paused/complete
            "links": [],              # [{id, share_url, share_id, passcode, pdir_fid,
                                       #   link_name, note, status, last_result}]
            "link_seq": 0,
            "have": {},                # {"集数": {"fid","name","link_id"}}
            "dir_fids": {},
            "emby_series_id": None,    # 缓存：这部剧在 Emby 里对应的 Item Id
            "emby_have": [],           # 缓存：Emby 库里已经有的集数（上次查询结果）
            "last_scan": "",
            "last_result": "",
            "added_at": _now(),
        }
        item.update(fields or {})
        item["interval_min"] = max(MIN_INTERVAL, int(item.get("interval_min") or MIN_INTERVAL))
        data["subscriptions"].append(item)
        _save(data)
        return dict(item)


def update_subscription(sid, **fields):
    with _lock:
        data = _load()
        for s in data["subscriptions"]:
            if s.get("id") == sid:
                s.update(fields)
                _save(data)
                return dict(s)
    return None


def remove_subscription(sid):
    with _lock:
        data = _load()
        before = len(data["subscriptions"])
        data["subscriptions"] = [s for s in data["subscriptions"] if s.get("id") != sid]
        _save(data)
        return len(data["subscriptions"]) < before


def add_sub_link(sid, share_url, share_id, passcode, pdir_fid, note=""):
    with _lock:
        data = _load()
        for s in data["subscriptions"]:
            if s.get("id") == sid:
                s["link_seq"] = s.get("link_seq", 0) + 1
                link = {"id": s["link_seq"], "share_url": share_url, "share_id": share_id,
                        "passcode": passcode, "pdir_fid": pdir_fid, "link_name": "",
                        "note": note, "status": "pending", "last_result": "",
                        "added_at": _now()}
                s.setdefault("links", []).append(link)
                _save(data)
                return dict(s)
    return None


def remove_sub_link(sid, lid):
    with _lock:
        data = _load()
        for s in data["subscriptions"]:
            if s.get("id") == sid:
                before = len(s.get("links") or [])
                s["links"] = [l for l in (s.get("links") or []) if l.get("id") != lid]
                _save(data)
                return dict(s) if len(s["links"]) < before else None
    return None
