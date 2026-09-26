"""订阅追更 —— 按"集数"track一部剧，可以挂多个不同人分享的网盘链接。

跟 monitor.py 的「单链接监控」不一样的地方：
  - 单链接监控以"这个分享里出现过的 fid 集合"为基线，一个分享对一个基线。
  - 订阅以"集数"为基线（have = {"3": {...}, "5": {...}}），不管这一集是从
    哪条链接转过来的，转过就不会再转第二次；反过来，同一订阅下挂几条不同
    人分享的链接，缺的集数会依次从每条链接里找，直到集数收齐或链接查完。

总集数来源：
  - 有 TMDB key 的话，添加订阅时会自动按 tmdb_id（+可选季号）查一次；
  - 没查到 / 没填 key，也能手动在编辑里填总集数；
  - 完全不填就是「来什么转什么」模式，不做"缺了哪几集"的判断，只是把
    还没转过的（按文件名解析出集数、且这一集之前没转过）转存过去。

不知道怎么把文件名对应到集数：见 episode_parse.py，纯靠文件名猜，猜不出来
的文件（比如特典、纯字幕包之类）直接跳过，不会被当成某一集误转。
"""

import threading
import time

import dingtalk
import episode_parse
import gygo_log
import monitor
import monitor_store
import smartstrm
import tmdb
from guangya import ApiError, TokenExpired
from share_gy import ShareError, list_share_files, parse_share_input, transfer_share_files

MAX_PER_LINK_PER_SCAN = 20  # 单条链接单轮最多转多少集，避免一次灌太猛


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


# ------------------------------------------------------------------ 增删改

def add_subscription(name, tmdb_id, season, target_path, interval_min,
                     keep_tree=True, total_episodes_override=None):
    """新建订阅。有 TMDB key 且给了 tmdb_id 就自动查总集数；查不到不阻塞创建，
    返回 (subscription, tmdb_err)，tmdb_err 非空时前端可以提示"自动查询失败"。
    """
    total = total_episodes_override
    tmdb_name = ""
    err = None
    cfg = tmdb.get_config()
    if tmdb_id and cfg.get("api_key") and not total:
        total, tmdb_name, err = tmdb.resolve_total_episodes(tmdb_id, season, cfg["api_key"])

    sub = monitor_store.add_subscription({
        "name": (name or tmdb_name or "").strip(),
        "tmdb_id": str(tmdb_id or ""),
        "season": season or None,
        "total_episodes": total or None,
        "target_path": target_path or "",
        "interval_min": interval_min,
        "keep_tree": keep_tree,
    })
    if not sub.get("name"):
        sub = monitor_store.update_subscription(sub["id"], name="订阅#%s" % sub["id"])
    gygo_log.info("新增订阅：%s", sub.get("name"), tmdb_id=tmdb_id,
                  total_episodes=sub.get("total_episodes"))
    return sub, err


def add_link(sid, share_text, note=""):
    """给订阅加一条分享链接。同一订阅下重复加同一个分享会被拦截。

    返回 (subscription_or_None, duplicate_bool)。
    """
    sub = monitor_store.get_subscription(sid)
    if not sub:
        return None, False
    share_id, passcode, pdir_fid = parse_share_input(share_text)
    for l in sub.get("links") or []:
        if l.get("share_id") == share_id:
            return sub, True
    sub = monitor_store.add_sub_link(sid, share_text, share_id, passcode, pdir_fid, note=note)
    gygo_log.info("订阅加链接：%s", sub.get("name"), share_id=share_id)
    return sub, False


# ------------------------------------------------------------------ 扫描

def _missing_set(sub, have):
    total = sub.get("total_episodes")
    if not total:
        return None
    return set(range(1, int(total) + 1)) - set(int(k) for k in have.keys())


def scan_subscription(sub, on_phase=None):
    """扫描一个订阅：按顺序查它下面的每条链接，把缺的集数补上。"""
    def _phase(ph, **kw):
        if callable(on_phase):
            try:
                on_phase(ph, kw)
            except Exception:
                pass

    sid = sub["id"]
    client = monitor._client()
    if client is None:
        return {"status": "error", "msg": "尚未登录"}
    if monitor._expired():
        monitor_store.update_subscription(sid, status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停"}

    have = dict(sub.get("have") or {})
    missing = _missing_set(sub, have)
    if missing is not None and not missing:
        monitor_store.update_subscription(
            sid, status="complete", last_scan=_now(),
            last_result="%s 已集齐全部 %d 集" % (_now(), sub.get("total_episodes")))
        _phase("完成", added=0)
        return {"status": "complete", "added": 0}

    _phase("解析目标目录")
    try:
        target_fid = monitor.resolve_target_fid(sub, client)
    except TokenExpired:
        monitor._mark_expired()
        monitor_store.update_subscription(sid, status="paused")
        return {"status": "paused", "msg": "登录已失效"}
    except ApiError as e:
        return {"status": "error", "msg": "目录解析失败：%s" % e}

    dir_cache = dict(sub.get("dir_fids") or {})
    links = list(sub.get("links") or [])
    link_patch = {}     # link_id -> 要覆盖到这个链接上的字段
    newly_names = []
    added_total = 0
    any_error = False

    for link in links:
        if missing is not None and not missing:
            break  # 已经补齐了，后面的链接不用再查
        lid = link.get("id")
        _phase("检查链接", link=link.get("note") or (link.get("link_name") or "")[:20])
        try:
            files, link_name = list_share_files(
                link.get("share_id") or "", link.get("passcode") or "",
                link.get("pdir_fid") or "", only_video=True)
        except ShareError as e:
            link_patch[lid] = {"status": "invalid" if e.fatal else "error",
                               "last_result": e.message}
            if e.fatal:
                dingtalk.notify_invalid(
                    "%s（%s）" % (sub.get("name"), link.get("note") or link.get("link_name") or ""),
                    e.message)
            continue

        patch = {"status": "ok"}
        if link_name and not link.get("link_name"):
            patch["link_name"] = link_name

        cand = {}  # 集数 -> file，同一链接里同一集出现多个版本只留第一个
        for f in files:
            ep = episode_parse.parse_episode(f.get("name") or f.get("path") or "",
                                             season=sub.get("season"))
            if ep is None or str(ep) in have:
                continue
            if missing is not None and ep not in missing:
                continue
            cand.setdefault(ep, f)

        if not cand:
            link_patch[lid] = patch
            continue

        pairs = sorted(cand.items())
        if len(pairs) > MAX_PER_LINK_PER_SCAN:
            pairs = pairs[:MAX_PER_LINK_PER_SCAN]

        try:
            transfer = transfer_share_files(
                link.get("share_id") or "", link.get("passcode") or "",
                [f for _ep, f in pairs], target_fid, client,
                keep_tree=sub.get("keep_tree", True),
                share_name=link_name or link.get("link_name") or "",
                dir_cache=dir_cache)
        except ShareError as e:
            patch = {"status": "invalid" if e.fatal else "error", "last_result": e.message}
            link_patch[lid] = patch
            any_error = any_error or (not e.fatal)
            if e.fatal:
                dingtalk.notify_invalid(
                    "%s（%s）" % (sub.get("name"), link.get("note") or link.get("link_name") or ""),
                    e.message)
            continue
        except TokenExpired:
            monitor._mark_expired()
            monitor_store.update_subscription(sid, status="paused", dir_fids=dir_cache)
            return {"status": "paused", "msg": "登录已失效"}
        except ApiError as e:
            link_patch[lid] = {"status": "error", "last_result": str(e)}
            any_error = True
            continue

        if transfer.get("fail"):
            patch["last_result"] = "部分转存失败，下轮重试"
            patch["status"] = "error"
            link_patch[lid] = patch
            any_error = True
            continue  # 失败的这批不计入 have，还在 missing 里，下轮重试

        for ep, f in pairs:
            have[str(ep)] = {"fid": f.get("fid"), "name": f.get("name") or f.get("path") or "",
                             "link_id": lid}
            if missing is not None:
                missing.discard(ep)
            newly_names.append(f.get("name") or f.get("path") or "")
            added_total += 1
        patch["last_result"] = "%s 本轮转存 %d 集" % (_now(), len(pairs))
        link_patch[lid] = patch

    new_links = []
    for link in links:
        patch = link_patch.get(link.get("id"))
        if patch:
            link = dict(link)
            link.update(patch)
        new_links.append(link)

    total = sub.get("total_episodes")
    if total:
        done = len(have)
        status = "complete" if done >= total else ("error" if any_error else "ok")
        summary = "%s 扫描：已集齐 %d/%d 集" % (_now(), done, total)
        if status == "complete":
            summary += "（已完结，不再扫描）"
    else:
        status = "error" if any_error else "ok"
        summary = "%s 扫描：已收集 %d 集（未设总集数，来什么转什么）" % (_now(), len(have))
    if added_total:
        summary += "，本轮新增 %d 集" % added_total

    monitor_store.update_subscription(sid, have=have, links=new_links, dir_fids=dir_cache,
                                      last_scan=_now(), status=status, last_result=summary)
    if newly_names:
        dingtalk.notify_transferred(sub.get("name"), newly_names, sub.get("target_path"))
        smartstrm.trigger(sub.get("target_path"))
    _phase("完成", added=added_total)
    return {"status": status, "added": added_total, "have": len(have), "total": total}


# ------------------------------------------------------------------ 调度

class SubScheduler(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self._stop = threading.Event()
        self._busy = set()

    def run(self):
        while not self._stop.is_set():
            time.sleep(60)
            if monitor._expired() or monitor._client() is None:
                continue
            now = time.time()
            for s in monitor_store.list_subscriptions():
                if not s.get("enabled") or s.get("status") == "complete":
                    continue
                sid = s["id"]
                if sid in self._busy:
                    continue
                iv = max(monitor_store.MIN_INTERVAL,
                         int(s.get("interval_min") or monitor_store.MIN_INTERVAL)) * 60
                last = s.get("last_scan")
                if not last:
                    due = True
                else:
                    try:
                        lt = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
                        due = (now - lt) >= iv
                    except Exception:
                        due = True
                if due:
                    self._busy.add(sid)
                    try:
                        r = scan_subscription(s)
                        if r.get("status") in ("ok", "complete") and r.get("added"):
                            gygo_log.info("订阅自动扫描：%s 新增 %d 集",
                                          s.get("name"), r.get("added"))
                    except Exception as e:
                        monitor_store.update_subscription(
                            sid, status="error", last_result="扫描异常：%s" % e)
                        gygo_log.error("订阅扫描异常", sub=sid, err=str(e))
                    finally:
                        self._busy.discard(sid)

    def stop(self):
        self._stop.set()


_scheduler = None


def start_scheduler():
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = SubScheduler()
        _scheduler.start()
    return _scheduler


def scan_now(sid):
    global _scheduler
    sch = _scheduler
    if sch is None or not sch.is_alive():
        sch = start_scheduler()
    if sid in sch._busy:
        return False

    def _run():
        try:
            s = monitor_store.get_subscription(sid)
            if not s or not s.get("enabled"):
                return
            if monitor._client() is None:
                return
            if monitor._expired():
                monitor_store.update_subscription(sid, status="paused")
                return
            scan_subscription(s)
        except Exception as e:
            gygo_log.error("订阅即时扫描异常", sub=sid, err=str(e))
        finally:
            sch._busy.discard(sid)

    sch._busy.add(sid)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True
