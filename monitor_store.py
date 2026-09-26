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
    return {"monitors": [], "auth": {}, "dingtalk": {}, "smartstrm": {}, "seq": 0}


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
    data.setdefault("auth", {})
    data.setdefault("dingtalk", {})
    data.setdefault("smartstrm", {})
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
