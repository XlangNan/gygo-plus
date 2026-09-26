"""分享链接定时监控 —— 调度与差异检测。

单个监控项的一轮扫描：
  1. 免登录列举分享里的当前文件
  2. 与上次记录的 fid 集合取差集 → 只拿到新增的剧集
  3. 转存新增文件到目标目录
  4. 回写新的 fid 集合作为基线

失效处理：
  - 分享链接失效（过期/被取消）→ status=invalid，不再扫描，不影响其它项
  - 登录态失效 → status=paused，全部暂停，重新登录后自动恢复
  - 网络等临时错误 → status=error，下个周期自动重试
"""

import re
import threading
import time

import dingtalk
import gygo_log
import monitor_store
import smartstrm
from monitor_store import MIN_INTERVAL
from guangya import ApiError, TokenExpired
from share_gy import (ShareError, list_share_files, parse_share_input,
                      transfer_share_files)

# 添加即转存 / 全量回填时，单次接口批量大小（安全值，防一次性请求过大）。
MAX_PER_SCAN = 20
# 追更模式：单次扫描最多转存多少个。分享方一次性补更几十集时，分批慢慢转，
# 一口气全塞进去容易触发光鸭风控，也容易把免费号的每日额度打满。
# 注意：仅"追更"（基线已有内容）时生效；首次全量回填（基线为空）不设上限，
# 一次性把整部剧/整部动漫全部转存完，避免一两百集按小时一轮轮补。
MAX_PER_ROUND = 20

# 与 app.py 解耦：启动时由 app 把自己的模块绑进来，避免循环 import
_CLIENT_GETTER = None
_EXPIRED_GETTER = None
_EXPIRE_SETTER = None


def bind(app_module, expired_setter=None):
    global _CLIENT_GETTER, _EXPIRED_GETTER, _EXPIRE_SETTER
    _CLIENT_GETTER = lambda: getattr(app_module, "CLIENT", None)
    _EXPIRED_GETTER = lambda: getattr(app_module, "AUTH_EXPIRED", False)
    _EXPIRE_SETTER = expired_setter


def _client():
    return _CLIENT_GETTER() if _CLIENT_GETTER else None


def _expired():
    return bool(_EXPIRED_GETTER()) if _EXPIRED_GETTER else False


def _mark_expired():
    if _EXPIRE_SETTER:
        try:
            _EXPIRE_SETTER(True)
        except Exception:
            pass


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def resolve_target_fid(m, client):
    """解析监控项的目标目录 fid（改名安全）。

    以前每轮都按"名字"解析：resolve_path 是按名字找目录、找不到就自动新建。
    于是你在光鸭里把文件夹改名后，gygo 找不到原名，就会**新建一个原来名字
    的目录**，新集数进新目录、旧集留在改名后的目录 —— 一部剧被拆成两份。

    光鸭的目录 fid 在改名后是不变的，所以这里记住 fid：
      1. 缓存里有 fid、且它是为当前 target_path 解析的、且目录还在 → 直接用
      2. 否则按路径解析（找不到会自动建目录），并把 fid 记进缓存
    目录被删导致 fid 失效时，自动回退成按名字重建，不会卡死。
    """
    mid = m.get("id")
    path = m.get("target_path") or ""
    cached = m.get("target_fid")
    if cached and m.get("target_fid_path") == path and client.dir_exists(cached):
        return cached
    fid = client.resolve_path(path)
    if mid and fid != cached:
        monitor_store.update(mid, target_fid=fid, target_fid_path=path)
    return fid


def scan_one(monitor, on_phase=None):
    """扫描单个监控项一次。"""
    def _phase(ph, **kw):
        if callable(on_phase):
            try:
                on_phase(ph, kw)
            except Exception:
                pass

    mid = monitor["id"]
    client = _client()
    if client is None:
        return {"status": "error", "msg": "尚未登录"}
    if _expired():
        monitor_store.update(mid, status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}

    # 1) 解析目标目录（没有就沿途创建；改名后靠缓存的 fid 找回同一个目录）
    _phase("解析目标目录")
    try:
        target_fid = resolve_target_fid(monitor, client)
    except TokenExpired:
        _mark_expired()
        monitor_store.update(mid, status="paused")
        return {"status": "paused", "msg": "登录已失效，暂停监控"}
    except ApiError as e:
        return {"status": "error", "msg": "目录解析失败：%s" % e}

    # 2) 列举分享当前文件（免登录接口，登录过期也能扫）
    _phase("列举分享文件")
    try:
        files, _name = list_share_files(
            monitor["share_id"], monitor.get("passcode") or "",
            monitor.get("pdir_fid") or "", only_video=True)
    except ShareError as e:
        if e.fatal:
            monitor_store.update(mid, status="invalid",
                                 last_result="链接失效：" + e.message, last_scan=_now())
            dingtalk.notify_invalid(monitor.get("link_name") or monitor.get("share_url"),
                                    e.message)
            return {"status": "invalid", "msg": e.message}
        monitor_store.update(mid, status="error",
                             last_result="列举失败：" + e.message, last_scan=_now())
        return {"status": "error", "msg": e.message}

    # 3) 差异检测
    _phase("差异检测")
    cur_ids = [f["fid"] for f in files if f.get("fid")]
    prev = set(monitor.get("last_files") or [])
    new_files = [f for f in files if f.get("fid") and f["fid"] not in prev]

    # 3.5) 过滤规则：关键词 / 扩展名 / 最小体积
    new_files, filtered = _apply_filters(new_files, monitor)
    if filtered:
        gygo_log.info("过滤掉 %d 个文件", len(filtered),
                      monitor=monitor.get("link_name"))

    # 3.6) 限流：仅"追更"模式（基线已有内容）下，一轮最多转 MAX_PER_ROUND 个，
    #      超出的留到下一轮慢慢补，避免一次灌爆风控 / 打满每日额度。
    #      若是"首次全量回填"（基线为空 prev 为空集），则不设上限，
    #      一次性把整部剧 / 整部动漫的全部集数转存完，不再按小时一轮轮补。
    pending = 0
    is_backfill = len(prev) == 0
    if not is_backfill and len(new_files) > MAX_PER_ROUND:
        pending = len(new_files) - MAX_PER_ROUND
        gygo_log.warn("单次新增 %d 个，超过追更上限 %d，本轮只转前 %d 个（剩余 %d 下轮补）",
                      len(new_files), MAX_PER_ROUND, MAX_PER_ROUND, pending,
                      monitor=monitor.get("link_name"))
        new_files = new_files[:MAX_PER_ROUND]

    added = 0
    transfer = {"submitted": 0, "ok": 0, "fail": 0, "timeout": 0, "detail": []}
    if new_files:
        _phase("转存新增", count=len(new_files))
        if _expired():
            monitor_store.update(mid, status="paused")
            return {"status": "paused", "msg": "登录已失效，暂停监控"}
        try:
            _dir_cache = dict(monitor.get("dir_fids") or {})
            transfer = transfer_share_files(
                monitor["share_id"], monitor.get("passcode") or "",
                new_files, target_fid, client,
                keep_tree=monitor.get("keep_tree", True),
                share_name=monitor.get("link_name") or "",
                dir_cache=_dir_cache)
            added = transfer.get("submitted", 0)
            # 记住子目录 fid（改名安全）：下次仍转进同一个目录，不新建原名目录
            if _dir_cache != (monitor.get("dir_fids") or {}):
                monitor_store.update(mid, dir_fids=_dir_cache)
        except ShareError as e:
            if e.fatal:
                monitor_store.update(mid, status="invalid",
                                     last_result="转存失败(链接失效)：" + e.message,
                                     last_scan=_now())
                dingtalk.notify_invalid(monitor.get("link_name") or monitor.get("share_url"),
                                        e.message)
                return {"status": "invalid", "msg": e.message}
            monitor_store.update(mid, status="error",
                                 last_result="转存失败：" + e.message, last_scan=_now())
            return {"status": "error", "msg": e.message}
        except TokenExpired:
            _mark_expired()
            monitor_store.update(mid, status="paused")
            return {"status": "paused", "msg": "登录已失效，暂停监控"}
        except ApiError as e:
            monitor_store.update(mid, status="error",
                                 last_result="转存失败：%s" % e, last_scan=_now())
            return {"status": "error", "msg": str(e)}

    # 基线推进：只有确认成功（或超时但不算失败）的才写进基线，
    # 明确失败的留着下轮重试；被过滤掉的也写进去，免得每轮都当新增。
    kept_ids = set(f["fid"] for f in filtered if f.get("fid"))
    if transfer.get("fail"):
        failed_note = "；".join(transfer.get("detail") or [])
        summary = "%s 扫描：转存失败，下轮重试（%s）" % (_now(), failed_note[:180])
        gygo_log.error("转存失败", monitor=monitor.get("link_name"),
                       detail=failed_note[:200])
        monitor_store.update(mid, last_files=sorted(set(prev) | kept_ids),
                             last_scan=_now(), status="error",
                             last_result=summary)
        _phase("完成", added=0)
        return {"status": "error", "added": 0, "total": len(files),
                "msg": summary}

    done_ids = set(f["fid"] for f in new_files if f.get("fid"))
    if transfer.get("timeout"):
        gygo_log.warn("转存任务超时未确认，按成功处理",
                      monitor=monitor.get("link_name"),
                      n=transfer.get("timeout"))

    tail = ""
    if pending:
        tail = "，还有 %d 个排队等下一轮" % pending
    if filtered:
        tail += "，过滤掉 %d 个" % len(filtered)
    summary = ("%s 扫描：新增 %d 个，已转存（共 %d）%s"
               % (_now(), added, len(files), tail)) if added else (
               "%s 扫描：无新增（共 %d）%s" % (_now(), len(files), tail))

    monitor_store.update(mid, last_files=sorted(set(prev) | kept_ids | done_ids),
                         last_scan=_now(), status="ok", last_result=summary)
    if added:
        names = [f.get("name") or f.get("path") or "" for f in new_files]
        dingtalk.notify_transferred(monitor.get("link_name"), names,
                                    monitor.get("target_path"))
        smartstrm.trigger(monitor.get("target_path"))
    _phase("完成", added=added)
    return {"status": "ok", "added": added, "total": len(files),
            "pending": pending, "filtered": len(filtered),
            "transfer": {k: v for k, v in transfer.items() if k != "detail"}}


def _match_keywords(name, raw):
    """raw 是用户填的规则串，逗号/空格/换行分隔，支持 * 通配。"""
    parts = [p.strip() for p in re.split(r"[,，\s]+", str(raw or "")) if p.strip()]
    if not parts:
        return True
    low = str(name or "").lower()
    for p in parts:
        pl = p.lower()
        if "*" in pl:
            pat = "^" + re.escape(pl).replace(r"\*", ".*") + "$"
            if re.match(pat, low):
                return True
        elif pl in low:
            return True
    return False


def _apply_filters(files, monitor):
    """按监控项上的过滤规则筛文件。返回 (留下的, 被过滤掉的)。"""
    inc = monitor.get("include_kw") or ""
    exc = monitor.get("exclude_kw") or ""
    try:
        min_mb = int(monitor.get("min_size_mb") or 0)
    except (TypeError, ValueError):
        min_mb = 0

    if not inc and not exc and min_mb <= 0:
        return files, []

    keep, drop = [], []
    for f in files:
        name = f.get("name") or f.get("path") or ""
        if min_mb > 0 and int(f.get("size") or 0) < min_mb * 1024 * 1024:
            drop.append(f)
            continue
        if inc and not _match_keywords(name, inc):
            drop.append(f)
            continue
        if exc and _match_keywords(name, exc):
            drop.append(f)
            continue
        keep.append(f)
    return keep, drop


def add_and_baseline(share_url, target_path, interval_min, pdir_fid="",
                     keep_tree=True, include_kw="", exclude_kw="",
                     min_size_mb=0, transfer_existing=True, client=None):
    """新增监控项，并立刻拉一次链接。

    transfer_existing=True（默认）且已登录时，会把分享里**当前已有的视频
    也立刻转存**到目标目录（分批限流，避免一次灌爆），然后建立基线。
    之后每次扫描只转"新出的"，这就是追剧逻辑。

    其它情况退化为"只建基线"：
      - 未登录（client=None）：基线留空，登录后首次扫描会自动转存全部
      - 用户明确不要（transfer_existing=False）：建完整基线，只追新不重转
    """
    share_id, passcode, parsed_pdir = parse_share_input(share_url)
    if not pdir_fid:
        pdir_fid = parsed_pdir

    mon = monitor_store.add({
        "share_url": share_url,
        "share_id": share_id,
        "passcode": passcode,
        "pdir_fid": pdir_fid or "",
        "target_path": target_path or "",
        "interval_min": interval_min,
        "keep_tree": bool(keep_tree),
        "include_kw": include_kw or "",
        "exclude_kw": exclude_kw or "",
        "min_size_mb": int(min_size_mb or 0),
    })
    mid = mon["id"]

    # 列一次分享，建基线（列举是免登录接口）
    link_name = ""
    try:
        files, link_name = list_share_files(share_id, passcode, pdir_fid or "", only_video=True)
        kept, dropped = _apply_filters(files, mon)
        note = "（过滤掉 %d 个）" % len(dropped) if dropped else ""
    except ShareError as e:
        monitor_store.update(mid, status="error" if not e.fatal else "invalid",
                             last_result="建立基线失败：" + e.message)
        if e.fatal:
            dingtalk.notify_invalid(share_url, e.message)
        return monitor_store.get(mid)
    except Exception as e:
        monitor_store.update(mid, status="error",
                             last_result="建立基线失败：%s" % e)
        return monitor_store.get(mid)

    # 链接有效、拿到片名了：通知"开始监控"
    dingtalk.notify_monitoring(link_name, target_path)

    if transfer_existing and client is not None and not _expired():
        # 立刻把当前已有的视频转存进来（分批 + 批间降速）
        target_fid = None
        try:
            target_fid = resolve_target_fid(mon, client)
        except (TokenExpired, ApiError) as e:
            monitor_store.update(mid, status="error",
                                 last_result="目标目录解析失败，仅建立基线（未转存）：%s" % e)
            gygo_log.warn("添加时目标目录解析失败，仅建基线", id=mid)
            return monitor_store.get(mid)

        # 被过滤掉的文件永不转存，先把它们的 fid 记进基线，
        # 否则以后每一轮扫描都会把它们当"新增"重新判定。
        baseline_ids = set(f["fid"] for f in dropped if f.get("fid"))
        # 先建一份"最小基线"再开始转存。这样即便有并发扫描抢跑，
        # 它读到的是这份基线，不会把还没转的文件误判成新增重复转存。
        monitor_store.update(mid, last_files=sorted(baseline_ids),
                             link_name=link_name, last_scan=_now(), status="ok",
                             last_result="已添加，正在转存已有视频%s" % note)

        # 占住 _busy：转存期间不让 scan_now / 自动扫描 / 手动扫描抢跑，
        # 否则会趁基线还没记全、把整批已转文件当"新增"又转一遍 —— 这就是
        # 20 集变成 40 集（每集一个副本）的元凶。
        sch = _scheduler
        if sch is None or not sch.is_alive():
            sch = start_scheduler()
        hold = mid not in sch._busy
        if hold:
            sch._busy.add(mid)

        transferred, note2, fatal = set(), "", False
        transferred_names = []
        dir_cache = {}
        try:
            batch = list(kept)
            while batch:
                chunk = batch[:MAX_PER_SCAN]
                batch = batch[MAX_PER_SCAN:]
                try:
                    transfer_share_files(share_id, passcode, chunk, target_fid, client,
                                         keep_tree=keep_tree, share_name=link_name,
                                         dir_cache=dir_cache)
                    # 成功一批就增量写回基线 —— 即便中断也只记真正成功的部分，
                    # 没转成功的下轮扫描会自动补，不漏也不重。
                    baseline_ids |= set(f["fid"] for f in chunk if f.get("fid"))
                    monitor_store.update(mid, last_files=sorted(baseline_ids))
                    transferred |= set(f["fid"] for f in chunk if f.get("fid"))
                    transferred_names.extend(
                        f.get("name") or f.get("path") or "" for f in chunk)
                except ShareError as e:
                    note2, fatal = e.message, e.fatal
                    break
                except TokenExpired:
                    _mark_expired()
                    note2 = "登录已失效"
                    break
                except ApiError as e:
                    note2, fatal = str(e), False
                    break
                if batch:
                    time.sleep(2)
        finally:
            if hold:
                sch._busy.discard(mid)

        # 记住子目录 fid：在光鸭里给这些文件夹改名后，下次仍能转进同一个目录，
        # 不会因为"按名字找不到"而新建一个原名目录、把一部剧拆成两份。
        if dir_cache:
            monitor_store.update(mid, dir_fids=dir_cache)

        if transferred_names:
            dingtalk.notify_transferred(link_name, transferred_names, target_path)
            smartstrm.trigger(target_path)

        if note2:
            st = "invalid" if fatal else "error"
            monitor_store.update(mid, link_name=link_name, last_scan=_now(), status=st,
                                 last_result="已添加，转存中断（%s），已转 %d 个，下轮补"
                                             % (note2, len(transferred)))
            gygo_log.error("添加时转存中断", id=mid, err=note2,
                           transferred=len(transferred))
        else:
            monitor_store.update(mid, link_name=link_name, last_scan=_now(), status="ok",
                                 last_result="已添加并转存 %d 个视频%s"
                                             % (len(transferred), note))
            gygo_log.info("新增监控并转存：%s", link_name or share_url,
                          added=len(transferred), dropped=len(dropped),
                          target=target_path or "(根目录)")
    elif client is None:
        # 未登录添加：基线留空，登录后首次扫描会当成"新出的"全部转存
        monitor_store.update(mid, link_name=link_name, last_scan=_now(),
                             status="pending",
                             last_result="已添加，尚未登录，登录后首次扫描会自动转存全部视频%s"
                                         % note)
        gygo_log.info("新增监控（未登录，空基线）：%s", link_name or share_url)
    else:
        # 已登录但用户选了"只追新"：建完整基线，不转已有
        monitor_store.update(mid,
                             last_files=[f["fid"] for f in files if f.get("fid")],
                             link_name=link_name, last_scan=_now(), status="ok",
                             last_result="已添加，基线 %d 个视频（按设置不转存已有，仅追更）%s"
                                         % (len(files), note))
        gygo_log.info("新增监控（仅基线/追新）：%s", link_name or share_url,
                      baseline=len(files))
    return monitor_store.get(mid)


class MonitorScheduler(threading.Thread):
    def __init__(self):
        threading.Thread.__init__(self)
        self.daemon = True
        self._stop = threading.Event()
        self._busy = set()          # 正在扫描的监控项 id，防止重叠

    def run(self):
        while not self._stop.is_set():
            time.sleep(60)
            if _expired():
                continue
            if _client() is None:
                continue            # 还没登录就先不扫，免得反复报"尚未登录"
            now = time.time()
            for m in monitor_store.list_all():
                if not m.get("enabled"):
                    continue
                if m.get("status") == "invalid":
                    continue
                mid = m["id"]
                if mid in self._busy:
                    continue
                iv = max(MIN_INTERVAL, int(m.get("interval_min") or MIN_INTERVAL)) * 60
                last = m.get("last_scan")
                if not last:
                    due = True
                else:
                    try:
                        lt = time.mktime(time.strptime(last, "%Y-%m-%d %H:%M:%S"))
                        due = (now - lt) >= iv
                    except Exception:
                        due = True
                if due:
                    self._busy.add(mid)
                    try:
                        r = scan_one(m)
                        if r.get("status") == "ok" and r.get("added"):
                            gygo_log.info("自动扫描完成：%s 新增 %d 个",
                                          m.get("link_name") or m.get("share_url"),
                                          r.get("added"))
                        elif r.get("status") == "error":
                            gygo_log.warn("自动扫描出错：%s %s",
                                          m.get("link_name") or m.get("share_url"),
                                          r.get("msg") or "")
                    except Exception as e:
                        monitor_store.update(mid, status="error",
                                             last_result="扫描异常：%s" % e)
                        gygo_log.error("扫描异常", monitor=mid, err=str(e))
                    finally:
                        self._busy.discard(mid)

    def stop(self):
        self._stop.set()


_scheduler = None


def start_scheduler():
    global _scheduler
    if _scheduler is None or not _scheduler.is_alive():
        _scheduler = MonitorScheduler()
        _scheduler.start()
    return _scheduler


def scan_now(mid):
    """立即在后台扫描某个监控项（不阻塞当前 HTTP 请求）。

    用于：添加链接后、登录成功后，让用户立刻看到效果，而不是干等一个
    扫描周期。重复触发会被调度器的 _busy 去重，不会和定时扫描重叠扫同一个。
    """
    global _scheduler
    sch = _scheduler
    if sch is None or not sch.is_alive():
        sch = start_scheduler()
    if mid in sch._busy:
        return False

    def _run():
        try:
            m = monitor_store.get(mid)
            if not m or not m.get("enabled"):
                return
            if _client() is None:
                return
            if _expired():
                monitor_store.update(mid, status="paused")
                return
            scan_one(m)
        except Exception as e:
            gygo_log.error("即时扫描异常", monitor=mid, err=str(e))
        finally:
            sch._busy.discard(mid)

    sch._busy.add(mid)
    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return True

