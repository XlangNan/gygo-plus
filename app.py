"""gygo —— 光鸭云盘分享链接追更监控。

纯标准库 Web 服务，浏览器里用鼠标操作，不需要命令行。
默认端口 5099（避开 casgen 的 5000 和 139cas 的 5244）。
"""

import json
import os
import signal
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import dingtalk
import gygo_log
import monitor
import monitor_store
import smartstrm
import subscription
import tmdb
from guangya import ApiError, GuangyaClient, TokenExpired, ROOT_FID
from share_gy import ShareError, parse_share_input

PORT = int(os.environ.get("PORT") or os.environ.get("GYGO_PORT") or 5099)
HERE = os.path.dirname(os.path.abspath(__file__))
VERSION = "1.4.0"

# 全局状态
CLIENT = None            # GuangyaClient 实例
AUTH_EXPIRED = False     # 登录态是否失效
AUTH_STATE = {}          # 短信登录过程的中间态（captcha_token / verification_id）
_lock = threading.Lock()


def _set_expired(val):
    global AUTH_EXPIRED
    AUTH_EXPIRED = bool(val)


def _client_from_auth():
    """启动时尝试用已保存的登录态恢复会话。"""
    auth = monitor_store.load_auth()
    if not auth.get("access_token"):
        return None
    c = GuangyaClient(access_token=auth.get("access_token") or "",
                      refresh_token=auth.get("refresh_token") or "",
                      device_id=auth.get("device_id") or "",
                      phone=auth.get("phone") or "")
    try:
        c.ensure_login()
        return c
    except Exception:
        # 探活失败就试着刷新一次
        if c.do_refresh():
            try:
                c.ensure_login()
                return c
            except Exception:
                pass
    return None


def _persist(c):
    monitor_store.save_auth(access_token=c.access_token,
                            refresh_token=c.refresh_token,
                            device_id=c.device_id,
                            phone=c.phone)


# ------------------------------------------------------------------ 业务动作

def act_sms_send(phone):
    """发送短信验证码，返回提示语。"""
    global CLIENT
    if not phone:
        raise ApiError("请填写手机号")
    c = CLIENT or GuangyaClient()
    captcha_token = c.sms_init(phone)
    vid = c.sms_send(phone, captcha_token)
    with _lock:
        AUTH_STATE["phone"] = phone
        AUTH_STATE["captcha_token"] = captcha_token
        AUTH_STATE["verification_id"] = vid
        if not CLIENT:
            AUTH_STATE["device_id"] = c.device_id
    return "验证码已发送到 %s" % phone


def act_sms_login(phone, code):
    """用短信验证码完成登录。"""
    global CLIENT, AUTH_EXPIRED
    st = dict(AUTH_STATE)
    if not st.get("verification_id") or not st.get("captcha_token"):
        raise ApiError("请先点击「发送验证码」")
    if st.get("phone") and phone and st["phone"] != phone:
        raise ApiError("手机号与发送验证码时不一致，请重新发送")

    c = CLIENT or GuangyaClient(device_id=st.get("device_id") or "")
    vtoken = c.sms_verify(st["verification_id"], code)
    c.sms_signin(st.get("phone") or phone, code, vtoken, st["captcha_token"])

    CLIENT = c
    AUTH_EXPIRED = False
    _persist(c)
    monitor.bind(sys.modules[__name__], _set_expired)
    monitor.start_scheduler()
    subscription.start_scheduler()
    # 登录成功后，把之前因登录失效暂停的项恢复
    for m in monitor_store.list_all():
        if m.get("status") == "paused":
            monitor_store.update(m["id"], status="ok")
    for s in monitor_store.list_subscriptions():
        if s.get("status") == "paused":
            monitor_store.update_subscription(s["id"], status="ok")
    # 立即扫描待处理的监控项（pending / 还没建基线的），不用等下一个周期
    for m in monitor_store.list_all():
        if not m.get("enabled") or m.get("status") == "invalid":
            continue
        if m.get("status") == "pending" or not m.get("last_files"):
            monitor.scan_now(m["id"])
    for s in monitor_store.list_subscriptions():
        if not s.get("enabled") or s.get("status") in ("invalid", "complete"):
            continue
        if s.get("links"):
            subscription.scan_now(s["id"])
    return {"ok": True, "phone": c.phone}


def act_logout():
    global CLIENT, AUTH_EXPIRED
    CLIENT = None
    AUTH_EXPIRED = False
    with _lock:
        AUTH_STATE.clear()
    monitor_store.save_auth(access_token="", refresh_token="", phone="")
    return {"ok": True}


def _need_client():
    if CLIENT is None:
        raise ApiError("请先登录光鸭云盘")
    if AUTH_EXPIRED:
        raise ApiError("登录已失效，请重新登录")
    return CLIENT


def act_add_monitor(share_url, target_path, interval_min, **opts):
    if not share_url:
        raise ApiError("请填写分享链接")
    # 未登录也能添加（先攒链接，登录后首次扫描会自动转存）。transfer_existing
    # 默认 True：添加即把当前已有视频也转存进来；设为 False 则只追新不重转。
    transfer_existing = bool(opts.pop("transfer_existing", True))

    # 重复链接防护：同一个分享链接只允许存在一个监控项。
    # 以前不查重：同一链接加几次就建几个监控项，而每个监控项在
    # 「添加即转存」或「登录后首次回填」时都会各自全量转存一遍 —— 实测
    # 遮天 178 集被建成 5 个监控项，盘里就成了每集 5 个一模一样的副本。
    try:
        _sid, _pc, _pdir = parse_share_input(share_url)
    except Exception:
        _sid = ""
    if _sid:
        for _m in monitor_store.list_all():
            if _m.get("share_id") == _sid and _m.get("status") != "invalid":
                gygo_log.info("重复添加已拦截", monitor=_m.get("id"),
                              name=_m.get("link_name"))
                return {"ok": True, "duplicate": True,
                        "msg": "该链接已在监控中（#%s %s），不会重复添加、也不会重复转存"
                               % (_m.get("id"), _m.get("link_name") or ""),
                        "monitor": _m}
    mon = monitor.add_and_baseline(
        share_url, target_path, interval_min,
        transfer_existing=transfer_existing, client=CLIENT, **opts)
    # 已登录就立刻在后台扫一次，不用等下一个周期（已转存的判为空差集，不重复）
    if CLIENT is not None and not AUTH_EXPIRED:
        monitor.scan_now(mon["id"])
    return {"ok": True, "monitor": mon}


EDITABLE = ("target_path", "interval_min", "keep_tree", "include_kw",
            "exclude_kw", "min_size_mb", "enabled")


def act_update_monitor(mid, patch):
    """改监控项配置。改目标路径不会重转已转存的文件（基线还在）。"""
    m = monitor_store.get(mid)
    if not m:
        raise ApiError("监控项不存在")
    clean = {}
    for k in EDITABLE:
        if k in patch and patch[k] is not None:
            clean[k] = patch[k]
    if "interval_min" in clean:
        try:
            clean["interval_min"] = max(monitor_store.MIN_INTERVAL,
                                        int(clean["interval_min"]))
        except (TypeError, ValueError):
            clean.pop("interval_min")
    if "min_size_mb" in clean:
        try:
            clean["min_size_mb"] = max(0, int(clean["min_size_mb"]))
        except (TypeError, ValueError):
            clean.pop("min_size_mb")
    if "keep_tree" in clean:
        clean["keep_tree"] = bool(clean["keep_tree"])
    if "enabled" in clean:
        clean["enabled"] = bool(clean["enabled"])
    monitor_store.update(mid, **clean)
    gygo_log.info("修改监控项", id=mid, fields=",".join(clean.keys()))
    return {"ok": True, "monitor": monitor_store.get(mid)}


def act_reset_baseline(mid):
    """清空基线。下一轮扫描会把分享里所有文件当成"新出的"重新转存。

    配合 MAX_PER_SCAN 分批，不会一次性灌爆。
    """
    m = monitor_store.get(mid)
    if not m:
        raise ApiError("监控项不存在")
    monitor_store.update(mid, last_files=[], status="ok",
                         last_result="基线已清空，下一轮将重新转存全部文件")
    gygo_log.warn("基线已重置，下一轮将全量重转", id=mid,
                  name=m.get("link_name") or "")
    return {"ok": True, "monitor": monitor_store.get(mid)}


def act_browse_dirs(fid):
    """浏览自己网盘的目录，用来选转存目标。"""
    c = _need_client()
    items = c.list_dir(fid if fid not in (None, "") else ROOT_FID)
    return {"ok": True, "fid": fid or ROOT_FID,
            "items": [{"fid": it.get("fid"), "name": it.get("name"),
                       "dir": bool(it.get("dir")), "size": it.get("size") or 0}
                      for it in items if it.get("dir")]}


def act_dingtalk_get():
    return {"ok": True, "config": dingtalk.get_config()}


def act_dingtalk_save(webhook, secret, enabled):
    cfg = dingtalk.save_config(webhook, secret, enabled)
    return {"ok": True, "config": cfg, "msg": "已保存"}


def act_dingtalk_test(webhook, secret):
    ok, msg = dingtalk.test((webhook or "").strip(), (secret or "").strip())
    return {"ok": ok, "msg": msg}


def act_smartstrm_get():
    return {"ok": True, "config": smartstrm.get_config()}


def act_smartstrm_save(webhook, tasks, storage_path, delay, enabled):
    cfg = smartstrm.save_config(webhook, tasks, storage_path, delay, enabled)
    return {"ok": True, "config": cfg, "msg": "已保存"}


def act_smartstrm_test(webhook, tasks, storage_path):
    ok, msg = smartstrm.test((webhook or "").strip(), (tasks or "").strip(),
                             (storage_path or "").strip())
    return {"ok": ok, "msg": msg}


def act_tmdb_get():
    return {"ok": True, "config": tmdb.get_config()}


def act_tmdb_save(api_key):
    cfg = tmdb.save_config(api_key)
    return {"ok": True, "config": cfg, "msg": "已保存"}


def act_tmdb_test(api_key):
    ok, msg = tmdb.test(api_key)
    return {"ok": ok, "msg": msg}


def act_add_subscription(name, tmdb_id, season, target_path, interval_min,
                         keep_tree, total_episodes):
    if not name and not tmdb_id:
        raise ApiError("请至少填写订阅名称或 TMDB ID")
    sub, err = subscription.add_subscription(
        name, tmdb_id, season or None, target_path, interval_min,
        keep_tree=keep_tree, total_episodes_override=total_episodes or None)
    r = {"ok": True, "subscription": sub}
    if err:
        r["warn"] = "TMDB 查询总集数失败（%s），可以在编辑里手动填总集数" % err
    return r


def act_add_sub_link(sid, share_text, note):
    if not share_text:
        raise ApiError("请填写分享链接")
    sub, dup = subscription.add_link(sid, share_text, note)
    if not sub:
        raise ApiError("订阅不存在")
    if not dup and CLIENT is not None and not AUTH_EXPIRED:
        subscription.scan_now(sid)
    return {"ok": True, "subscription": sub, "duplicate": dup}


def act_remove_sub_link(sid, lid):
    sub = monitor_store.remove_sub_link(sid, lid)
    if not sub:
        raise ApiError("订阅或链接不存在")
    return {"ok": True, "subscription": sub}


def act_scan_subscription(sid):
    _need_client()
    s = monitor_store.get_subscription(sid)
    if not s:
        raise ApiError("订阅不存在")
    sch = subscription._scheduler
    if sch is None or not sch.is_alive():
        sch = subscription.start_scheduler()
    if sid in sch._busy:
        return {"ok": False, "msg": "该订阅正在扫描中，请稍候再点"}
    sch._busy.add(sid)
    try:
        phases = []
        result = subscription.scan_subscription(s, on_phase=lambda ph, kw: phases.append(ph))
    finally:
        sch._busy.discard(sid)
    return {"ok": result.get("status") in ("ok", "complete"), "result": result,
            "phases": phases, "subscription": monitor_store.get_subscription(sid)}


SUB_EDITABLE = ("name", "target_path", "interval_min", "keep_tree", "enabled",
               "total_episodes", "season")


def act_update_subscription(sid, patch):
    s = monitor_store.get_subscription(sid)
    if not s:
        raise ApiError("订阅不存在")
    clean = {}
    for k in SUB_EDITABLE:
        if k in patch and patch[k] is not None:
            clean[k] = patch[k]
    if "interval_min" in clean:
        try:
            clean["interval_min"] = max(monitor_store.MIN_INTERVAL, int(clean["interval_min"]))
        except (TypeError, ValueError):
            clean.pop("interval_min")
    if "total_episodes" in clean:
        try:
            clean["total_episodes"] = int(clean["total_episodes"]) or None
        except (TypeError, ValueError):
            clean.pop("total_episodes")
    if "season" in clean:
        try:
            clean["season"] = int(clean["season"]) if str(clean["season"]).strip() else None
        except (TypeError, ValueError):
            clean.pop("season")
    if "keep_tree" in clean:
        clean["keep_tree"] = bool(clean["keep_tree"])
    if "enabled" in clean:
        clean["enabled"] = bool(clean["enabled"])
    monitor_store.update_subscription(sid, **clean)
    gygo_log.info("修改订阅", id=sid, fields=",".join(clean.keys()))
    # 手动改了总集数、之前判过"已完结"的，重新判一下要不要恢复追更
    s2 = monitor_store.get_subscription(sid)
    if s2.get("status") == "complete" and s2.get("total_episodes"):
        if len(s2.get("have") or {}) < int(s2["total_episodes"]):
            monitor_store.update_subscription(sid, status="ok")
    return {"ok": True, "subscription": monitor_store.get_subscription(sid)}


def act_scan(mid):
    _need_client()
    m = monitor_store.get(mid)
    if not m:
        raise ApiError("监控项不存在")
    # 与定时扫描 / 即时扫描互斥：同一链接只允许一个扫描在跑，
    # 否则两个扫描都读到旧基线、都判"新增"，会把整批文件重复转存（20→40 副本）。
    sch = monitor._scheduler
    if sch is None or not sch.is_alive():
        sch = monitor.start_scheduler()
    if mid in sch._busy:
        return {"ok": False, "msg": "该链接正在扫描中，请稍候再点"}
    sch._busy.add(mid)
    try:
        phases = []
        result = monitor.scan_one(m, on_phase=lambda ph, kw: phases.append(ph))
    finally:
        sch._busy.discard(mid)
    return {"ok": result.get("status") in ("ok",), "result": result,
            "phases": phases, "monitor": monitor_store.get(mid)}


def act_cleanup_duplicates():
    """清理重复的监控项：同一个 share_id 只保留最早（id 最小）的那个。

    场景：同一个分享链接被加了多次，于是建了多个监控项，而每个监控项在
    「添加即转存」或「登录后首次回填」时都会各自全量转存一遍 —— 实测
    遮天 178 集被建成 5 个监控项，盘里就成了每集 5 个一模一样的副本。

    注意：只删 gygo 的监控配置，**不会删你盘里已经转存的文件**。
    """
    groups = {}
    for m in monitor_store.list_all():
        sid = m.get("share_id")
        if not sid:
            continue
        groups.setdefault(sid, []).append(m)
    removed = []
    for _sid, group in groups.items():
        if len(group) <= 1:
            continue
        group.sort(key=lambda x: x.get("id") or 0)
        for extra in group[1:]:
            monitor_store.remove(extra["id"])
            removed.append({"id": extra["id"],
                            "name": extra.get("link_name") or ""})
    if removed:
        gygo_log.info("清理重复监控项", removed=len(removed))
    return {"ok": True, "removed": len(removed), "removed_items": removed,
            "msg": ("已清理 %d 个重复监控项（盘里已转存的文件不受影响）" % len(removed))
                   if removed else "没有发现重复的监控项"}


def _walk_my_dir(client, fid, prefix, out, depth=0):
    """递归列举个人盘某个目录下的所有文件，结果追加进 out。"""
    if depth > 10:
        return
    for it in client.list_dir(fid):
        path = (prefix + "/" + it["name"]) if prefix else it["name"]
        if it.get("dir"):
            _walk_my_dir(client, it["fid"], path, out, depth + 1)
        else:
            out.append({"fid": it["fid"], "name": it["name"],
                        "dir_path": prefix, "path": path,
                        "size": it.get("size") or 0})


def act_dedupe(mid, data=None):
    """清理某个监控项目标目录下「同名重复」的文件副本。

    场景：同一链接被建了多个监控项，每个都全量转存一遍，于是盘里每集
    都有好几个一模一样的副本（实测遮天每集 5 个）。

    规则（保守，只删确定是副本的）：
      - 只统计**文件**，不动目录
      - 按「所在目录 + 文件名」分组，同组超过 1 个才算重复
      - 每组保留列表里的第一份，其余列入删除（副本内容一样，留哪份都一样）

    data 里 dry_run=1（默认）只出报告不删除；确认无误后传 dry_run=0 才真删。
    """
    m = monitor_store.get(mid)
    if not m:
        raise ApiError("监控项不存在")
    client = _need_client()
    dry = bool((data or {}).get("dry_run", 1))

    target_fid = monitor.resolve_target_fid(m, client)
    files = []
    _walk_my_dir(client, target_fid, m.get("target_path") or "", files)

    groups = {}
    for f in files:
        groups.setdefault((f["dir_path"], f["name"]), []).append(f)
    dup = {k: v for k, v in groups.items() if len(v) > 1}
    to_delete = []
    for _k, group in dup.items():
        to_delete.extend(group[1:])

    if not to_delete:
        return {"ok": True, "dry_run": dry, "scanned": len(files),
                "dup_groups": 0, "to_delete": 0, "deleted": 0,
                "msg": "目标目录下没有发现同名重复文件"}

    if dry:
        return {"ok": True, "dry_run": True, "scanned": len(files),
                "dup_groups": len(dup), "to_delete": len(to_delete), "deleted": 0,
                "samples": [f["path"] for f in to_delete[:20]],
                "msg": "预演：发现 %d 组同名重复、共 %d 个副本待删（本次未删除，"
                       "确认后传 dry_run=0 执行）" % (len(dup), len(to_delete))}

    fids = [f["fid"] for f in to_delete]
    client.delete_files(fids)
    gygo_log.info("清理同名副本", monitor=mid, deleted=len(fids))
    return {"ok": True, "dry_run": False, "scanned": len(files),
            "dup_groups": len(dup), "to_delete": len(to_delete),
            "deleted": len(fids),
            "msg": "已删除 %d 个同名副本，每组保留 1 份" % len(fids)}


# ------------------------------------------------------------------ HTTP

class Handler(BaseHTTPRequestHandler):
    server_version = "gygo/1.0"

    def log_message(self, fmt, *args):
        if os.environ.get("GYGO_DEBUG"):
            sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    # -- helpers

    def _send(self, code, body, ctype="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            self.wfile.write(body)
        except Exception:
            pass

    def _body(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        raw = self.rfile.read(length).decode("utf-8", "replace")
        try:
            return json.loads(raw)
        except Exception:
            return {}

    # -- routes

    def do_GET(self):
        path = self.path.split("?")[0]
        if path in ("/", "/index.html"):
            try:
                with open(os.path.join(HERE, "index.html"), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except Exception:
                self._send(500, {"ok": False, "msg": "index.html 读取失败"})
            return

        if path == "/api/status":
            return self._send(200, {
                "ok": True,
                "logged": CLIENT is not None and not AUTH_EXPIRED,
                "expired": AUTH_EXPIRED,
                "phone": (getattr(CLIENT, "phone", "") or ""),
                "min_interval": monitor_store.MIN_INTERVAL,
            })

        if path == "/api/monitors":
            items = monitor_store.list_all()
            return self._send(200, {"ok": True, "monitors": items})

        if path == "/api/logs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            n = int((q.get("n") or ["200"])[0])
            lv = (q.get("level") or [None])[0]
            return self._send(200, {"ok": True, "logs": gygo_log.tail(n, lv)})

        if path == "/api/health":
            return self._send(200, {
                "ok": True,
                "version": VERSION,
                "logged": CLIENT is not None and not AUTH_EXPIRED,
                "expired": AUTH_EXPIRED,
                "monitors": len(monitor_store.list_all()),
                "running": sum(1 for m in monitor_store.list_all()
                               if m.get("enabled") and m.get("status") != "invalid"),
            })

        if path == "/api/dingtalk":
            return self._send(200, act_dingtalk_get())

        if path == "/api/smartstrm":
            return self._send(200, act_smartstrm_get())

        if path == "/api/tmdb":
            return self._send(200, act_tmdb_get())

        if path == "/api/subscriptions":
            return self._send(200, {"ok": True,
                                    "subscriptions": monitor_store.list_subscriptions()})

        if path == "/api/dirs":
            q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            fid = (q.get("fid") or [""])[0]
            try:
                return self._send(200, act_browse_dirs(fid))
            except ApiError as e:
                return self._send(200, {"ok": False, "msg": str(e)})

        return self._send(404, {"ok": False, "msg": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        data = self._body()
        try:
            if path == "/api/sms/send":
                msg = act_sms_send((data.get("phone") or "").strip())
                return self._send(200, {"ok": True, "msg": msg})

            if path == "/api/sms/login":
                r = act_sms_login((data.get("phone") or "").strip(),
                                  (data.get("code") or "").strip())
                return self._send(200, {"ok": True, "msg": "登录成功", **r})

            if path == "/api/logout":
                return self._send(200, {**act_logout(), "msg": "已退出"})

            if path == "/api/dingtalk":
                r = act_dingtalk_save(data.get("webhook"), data.get("secret"),
                                      data.get("enabled"))
                return self._send(200, r)

            if path == "/api/dingtalk/test":
                r = act_dingtalk_test(data.get("webhook"), data.get("secret"))
                return self._send(200, r)

            if path == "/api/smartstrm":
                r = act_smartstrm_save(data.get("webhook"), data.get("tasks"),
                                       data.get("storage_path"), data.get("delay"),
                                       data.get("enabled"))
                return self._send(200, r)

            if path == "/api/smartstrm/test":
                r = act_smartstrm_test(data.get("webhook"), data.get("tasks"),
                                       data.get("storage_path"))
                return self._send(200, r)

            if path == "/api/tmdb":
                r = act_tmdb_save(data.get("api_key"))
                return self._send(200, r)

            if path == "/api/tmdb/test":
                r = act_tmdb_test(data.get("api_key"))
                return self._send(200, r)

            if path == "/api/subscriptions":
                r = act_add_subscription(
                    (data.get("name") or "").strip(),
                    (data.get("tmdb_id") or "").strip(),
                    data.get("season"),
                    (data.get("target_path") or "").strip(),
                    int(data.get("interval_min") or monitor_store.MIN_INTERVAL),
                    data.get("keep_tree", True),
                    data.get("total_episodes"),
                )
                return self._send(200, r)

            if path.startswith("/api/subscriptions/") and path.endswith("/scan"):
                sid = int(path.split("/")[3])
                return self._send(200, act_scan_subscription(sid))

            if path.startswith("/api/subscriptions/") and path.endswith("/toggle"):
                sid = int(path.split("/")[3])
                s = monitor_store.get_subscription(sid)
                if not s:
                    return self._send(404, {"ok": False, "msg": "订阅不存在"})
                monitor_store.update_subscription(sid, enabled=not s.get("enabled", True))
                return self._send(200, {"ok": True,
                                        "subscription": monitor_store.get_subscription(sid)})

            if path.startswith("/api/subscriptions/") and path.endswith("/links"):
                sid = int(path.split("/")[3])
                r = act_add_sub_link(sid, (data.get("share_url") or "").strip(),
                                     (data.get("note") or "").strip())
                return self._send(200, r)

            # 编辑订阅：/api/subscriptions/{id}（只认三段，多的当未知路由）
            if path.startswith("/api/subscriptions/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    return self._send(404, {"ok": False, "msg": "not found"})
                try:
                    sid = int(parts[2])
                except ValueError:
                    return self._send(400, {"ok": False, "msg": "参数错误"})
                return self._send(200, act_update_subscription(sid, data))

            if path == "/api/monitors":
                r = act_add_monitor(
                    (data.get("share_url") or "").strip(),
                    (data.get("target_path") or "").strip(),
                    int(data.get("interval_min") or monitor_store.MIN_INTERVAL),
                    keep_tree=data.get("keep_tree", True),
                    include_kw=(data.get("include_kw") or "").strip(),
                    exclude_kw=(data.get("exclude_kw") or "").strip(),
                    min_size_mb=int(data.get("min_size_mb") or 0),
                    transfer_existing=data.get("transfer_existing", True),
                )
                return self._send(200, r)

            # 清理重复监控项（同一链接只留一个，不删盘文件）
            if path == "/api/monitors/cleanup":
                return self._send(200, act_cleanup_duplicates())

            if path.startswith("/api/monitors/") and path.endswith("/scan"):
                mid = int(path.split("/")[3])
                return self._send(200, act_scan(mid))

            if path.startswith("/api/monitors/") and path.endswith("/reset"):
                mid = int(path.split("/")[3])
                return self._send(200, act_reset_baseline(mid))

            if path.startswith("/api/monitors/") and path.endswith("/toggle"):
                mid = int(path.split("/")[3])
                m = monitor_store.get(mid)
                if not m:
                    return self._send(404, {"ok": False, "msg": "监控项不存在"})
                monitor_store.update(mid, enabled=not m.get("enabled", True))
                return self._send(200, {"ok": True, "monitor": monitor_store.get(mid)})

            # 清理目标目录下的同名副本：/api/monitors/{id}/dedupe
            if path.startswith("/api/monitors/") and path.endswith("/dedupe"):
                mid = int(path.split("/")[3])
                return self._send(200, act_dedupe(mid, data))

            # 编辑监控项：/api/monitors/{id}（只认三段，多的当未知路由）
            if path.startswith("/api/monitors/"):
                parts = path.strip("/").split("/")
                if len(parts) != 3:
                    return self._send(404, {"ok": False, "msg": "not found"})
                try:
                    mid = int(parts[2])
                except ValueError:
                    return self._send(400, {"ok": False, "msg": "参数错误"})
                return self._send(200, act_update_monitor(mid, data))
        except ShareError as e:
            return self._send(200, {"ok": False, "msg": e.message})
        except TokenExpired:
            global AUTH_EXPIRED
            AUTH_EXPIRED = True
            for m in monitor_store.list_all():
                if m.get("status") == "ok":
                    monitor_store.update(m["id"], status="paused")
            return self._send(200, {"ok": False, "msg": "登录已失效，请重新登录"})
        except ApiError as e:
            return self._send(200, {"ok": False, "msg": str(e)})
        except Exception as e:
            return self._send(200, {"ok": False, "msg": "出错了：%s" % e})

        return self._send(404, {"ok": False, "msg": "not found"})

    def do_DELETE(self):
        path = self.path.split("?")[0]
        if path.startswith("/api/monitors/"):
            try:
                mid = int(path.split("/")[3])
            except (IndexError, ValueError):
                return self._send(400, {"ok": False, "msg": "参数错误"})
            ok = monitor_store.remove(mid)
            return self._send(200, {"ok": ok})
        if path.startswith("/api/subscriptions/"):
            parts = path.strip("/").split("/")
            try:
                if len(parts) == 5 and parts[3] == "links":
                    sid, lid = int(parts[2]), int(parts[4])
                    return self._send(200, act_remove_sub_link(sid, lid))
                if len(parts) == 3:
                    sid = int(parts[2])
                    ok = monitor_store.remove_subscription(sid)
                    return self._send(200, {"ok": ok})
            except (IndexError, ValueError):
                return self._send(400, {"ok": False, "msg": "参数错误"})
            except ApiError as e:
                return self._send(200, {"ok": False, "msg": str(e)})
        return self._send(404, {"ok": False, "msg": "not found"})


def main():
    global CLIENT, AUTH_EXPIRED
    CLIENT = _client_from_auth()
    if CLIENT:
        AUTH_EXPIRED = False
    monitor.bind(sys.modules[__name__], _set_expired)
    monitor.start_scheduler()
    subscription.start_scheduler()

    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)

    def _shutdown(signum, _frame):
        gygo_log.info("收到退出信号 %s，正在停止", signum)
        try:
            srv.shutdown()
        except Exception:
            pass

    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, OSError):
            pass

    gygo_log.info("gygo v%s 已启动 -> http://0.0.0.0:%d", VERSION, PORT)
    print("gygo v%s 已启动 -> http://0.0.0.0:%d" % (VERSION, PORT))
    if CLIENT:
        gygo_log.info("已恢复上次登录态（%s）", CLIENT.phone or "已登录")
        print("已恢复上次登录态（%s），监控运行中" % (CLIENT.phone or "已登录"))
    else:
        print("尚未登录，请打开网页用手机号短信登录")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        print("\n已停止")
        gygo_log.info("已停止")
        srv.server_close()


if __name__ == "__main__":
    main()
