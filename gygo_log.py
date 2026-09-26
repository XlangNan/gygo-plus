"""gygo 运行日志 —— 内存环形缓冲 + 落盘文件。

不引第三方依赖，标准库实现：
  - 内存里保留最近 N 条，供网页「运行日志」直接查看
  - 同时写 /data/gygo.log，按大小轮转，方便 docker logs 或直接开文件看
"""

import os
import sys
import threading
import time
from collections import deque

DATA_DIR = os.environ.get("GYGO_DATA_DIR") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "data")
LOG_FILE = os.path.join(DATA_DIR, "gygo.log")

MAX_KEEP = 500        # 内存里保留的条数
MAX_BYTES = 1024 * 1024   # 单文件超过 1MB 就轮转
BACKUP_COUNT = 2

_lock = threading.RLock()
_buf = deque(maxlen=MAX_KEEP)
_seq = [0]

LEVELS = {"DEBUG": 10, "INFO": 20, "WARN": 30, "ERROR": 40}
_MIN_LEVEL = LEVELS.get(
    (os.environ.get("GYGO_LOG_LEVEL") or "INFO").upper(), 20)


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _rotate_if_needed():
    try:
        if os.path.getsize(LOG_FILE) < MAX_BYTES:
            return
    except OSError:
        return
    for i in range(BACKUP_COUNT, 0, -1):
        src = LOG_FILE if i == 1 else "%s.%d" % (LOG_FILE, i - 1)
        dst = "%s.%d" % (LOG_FILE, i)
        try:
            if os.path.exists(src):
                if os.path.exists(dst):
                    os.remove(dst)
                os.rename(src, dst)
        except OSError:
            pass


def log(level, msg, **kw):
    """写一条日志。kw 里的附加字段会拼到消息后面。"""
    lv = LEVELS.get(str(level).upper(), 20)
    if lv < _MIN_LEVEL:
        return
    text = str(msg)
    if kw:
        extra = " ".join("%s=%s" % (k, v) for k, v in kw.items())
        text = "%s | %s" % (text, extra)
    entry = {"seq": _seq[0], "ts": _ts(), "level": str(level).upper(), "msg": text}
    _seq[0] += 1
    with _lock:
        _buf.append(entry)
    line = "[%s] %-5s %s\n" % (entry["ts"], entry["level"], text)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        _rotate_if_needed()
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line)
    except OSError:
        pass
    if lv >= LEVELS["WARN"]:
        try:
            sys.stderr.write(line)
            sys.stderr.flush()
        except Exception:
            pass
    return entry


def _fmt(msg, args):
    if not args:
        return msg
    try:
        return str(msg) % args
    except Exception:
        try:
            return "%s %s" % (msg, " ".join(str(a) for a in args))
        except Exception:
            return str(msg)


def info(msg, *args, **kw):
    return log("INFO", _fmt(msg, args), **kw)


def warn(msg, *args, **kw):
    return log("WARN", _fmt(msg, args), **kw)


def error(msg, *args, **kw):
    return log("ERROR", _fmt(msg, args), **kw)


def debug(msg, *args, **kw):
    return log("DEBUG", _fmt(msg, args), **kw)


def tail(n=200, level=None):
    """取最近 n 条，可按级别过滤。"""
    lv = LEVELS.get(str(level).upper(), 0) if level else 0
    with _lock:
        items = list(_buf)
    if lv:
        items = [x for x in items if LEVELS.get(x["level"], 0) >= lv]
    return items[-int(n):]


def clear():
    with _lock:
        _buf.clear()
