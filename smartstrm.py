"""联动 SmartStrm（https://smartstrm.github.io/settings/webhook）。

SmartStrm 的开发者接口很简单：POST 一个 JSON 到它的 Webhook 地址，
body 里 event 固定 "a_task"，task.name 是已存在的任务名（单次请求只认一个），
task.storage_path 可选（只触发任务路径下的某个子路径）。

这里把「Webhook 地址 / 任务名（可逗号分隔多个，会依次各发一次请求）/
可选路径 / 可选延迟秒数」存在本地，gygo 转存成功后自动挨个 POST 过去。
"""

import json
import threading
import time
import urllib.error
import urllib.request

import gygo_log
import monitor_store

TIMEOUT = 10


def get_config():
    cfg = monitor_store.load_smartstrm()
    return {
        "webhook": cfg.get("webhook") or "",
        "tasks": cfg.get("tasks") or "",
        "storage_path": cfg.get("storage_path") or "",
        "delay": cfg.get("delay") or 0,
        "enabled": bool(cfg.get("enabled", False)),
    }


def save_config(webhook, tasks, storage_path, delay, enabled):
    webhook = (webhook or "").strip()
    tasks = (tasks or "").strip()
    storage_path = (storage_path or "").strip()
    try:
        delay = int(delay or 0)
    except (TypeError, ValueError):
        delay = 0
    monitor_store.save_smartstrm(webhook=webhook, tasks=tasks, storage_path=storage_path,
                                 delay=delay, enabled=bool(enabled))
    gygo_log.info("SmartStrm 联动配置已保存", enabled=bool(enabled), tasks=tasks)
    return get_config()


def _task_names(tasks_field):
    return [t.strip() for t in (tasks_field or "").split(",") if t.strip()]


def _post(webhook, task_name, storage_path="", delay=0):
    if not webhook:
        return False, "请先填写 Webhook 地址"
    if not task_name:
        return False, "请先填写要触发的任务名"

    task = {"name": task_name}
    if storage_path:
        task["storage_path"] = storage_path
    payload = {"event": "a_task", "task": task}
    if delay:
        payload["delay"] = delay

    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(webhook, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:200]
        except Exception:
            pass
        return False, "HTTP %s：%s" % (e.code, detail or e.reason)
    except urllib.error.URLError as e:
        return False, "网络请求失败：%s" % e
    except Exception as e:
        return False, "发送异常：%s" % e
    return True, (raw or "触发成功")[:200]


def test(webhook, tasks, storage_path=""):
    """测试按钮：立即发一次（不模拟延迟，延迟现在是 gygo 自己 sleep 实现的，
    跟发出去的这个请求本身无关——测的是连通性和任务名对不对）。"""
    names = _task_names(tasks)
    if not names:
        return False, "请先填写要触发的任务名"
    results = []
    ok_all = True
    for name in names:
        ok, msg = _post(webhook, name, storage_path, 0)
        ok_all = ok_all and ok
        results.append("%s：%s" % (name, msg))
    return ok_all, "；".join(results)


def trigger(monitor_path=None):
    """转存成功后调用。按已保存配置逐个任务名触发；未配置/未启用时静默跳过。

    storage_path 只用配置里手填的那个，不会自动拿本次转存的目标目录去填——
    SmartStrm 要求这个字段"必须是任务本身路径的路径或子路径"，格式是它那边
    存储挂载后的绝对路径，跟 gygo 内部的相对路径体系对不上，瞎填反而会导致
    它静默不执行（收到推送但没有任何后续）。留空更稳，SmartStrm 会按任务自己
    配置的路径全量处理。monitor_path 参数保留只是为了兼容旧调用点，暂不使用。

    延迟改成 gygo 这边自己 sleep 再发请求，发出去的 payload 里不带 delay：
    实测发现有些 SmartStrm 部署收到带 delay 的请求后根本不会真的执行任务，
    干脆不依赖它的延迟机制，等够时间了直接发"立即执行"的请求，更保险。
    sleep 放在后台线程里，不阻塞扫描主流程。
    """
    cfg = get_config()
    if not cfg["enabled"] or not cfg["webhook"]:
        return
    names = _task_names(cfg["tasks"])
    if not names:
        return
    storage_path = cfg["storage_path"]
    delay = cfg["delay"]
    if delay:
        t = threading.Thread(target=_fire_after_delay,
                             args=(cfg["webhook"], names, storage_path, delay),
                             daemon=True)
        t.start()
    else:
        _fire(cfg["webhook"], names, storage_path)


def _fire_after_delay(webhook, names, storage_path, delay):
    time.sleep(delay)
    _fire(webhook, names, storage_path)


def _fire(webhook, names, storage_path):
    for name in names:
        try:
            ok, msg = _post(webhook, name, storage_path, 0)
            if not ok:
                gygo_log.warn("SmartStrm 触发失败", task=name, err=msg)
        except Exception as e:
            gygo_log.warn("SmartStrm 触发异常", task=name, err=str(e))
