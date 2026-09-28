"""钉钉自定义机器人通知。

配置（webhook / secret / 是否启用）存在 monitor_store 的同一个 JSON 文件里，
不需要在 Docker 里填环境变量 —— 网页上填、保存即生效。

钉钉自定义机器人支持「加签」校验（比只填关键词安全），这里默认按加签方式
签名；如果用户的机器人是「自定义关键词」模式、没有 secret，也兼容（不加签
直接发）。

三个通知场景，均为“尽力而为”：网络失败/未配置时只记日志，不影响主流程。
"""

import base64
import hashlib
import hmac
import json
import time
import urllib.error
import urllib.parse
import urllib.request

import episode_parse
import gygo_log
import monitor_store

TIMEOUT = 10
MAX_LIST_SHOW = 20  # 转存文件较多时，通知里最多列这么多个，其余合并成一行


def get_config():
    """返回 {webhook, secret, enabled}，未配置过也给出默认值。"""
    cfg = monitor_store.load_dingtalk()
    return {
        "webhook": cfg.get("webhook") or "",
        "secret": cfg.get("secret") or "",
        "enabled": bool(cfg.get("enabled", False)),
    }


def save_config(webhook, secret, enabled):
    webhook = (webhook or "").strip()
    secret = (secret or "").strip()
    monitor_store.save_dingtalk(webhook=webhook, secret=secret, enabled=bool(enabled))
    gygo_log.info("钉钉通知配置已保存", enabled=bool(enabled),
                  webhook=("已填写" if webhook else "空"))
    return get_config()


def _sign(secret, timestamp):
    s = "%s\n%s" % (timestamp, secret)
    h = hmac.new(secret.encode("utf-8"), s.encode("utf-8"), hashlib.sha256).digest()
    return urllib.parse.quote_plus(base64.b64encode(h))


def _build_url(webhook, secret):
    if not secret:
        return webhook
    ts = str(round(time.time() * 1000))
    sign = _sign(secret, ts)
    sep = "&" if "?" in webhook else "?"
    return "%s%stimestamp=%s&sign=%s" % (webhook, sep, ts, sign)


def _send_raw(webhook, secret, title, text):
    """真正发请求。返回 (ok, msg)。"""
    if not webhook:
        return False, "请先填写 Webhook 地址"
    try:
        url = _build_url(webhook, secret)
    except Exception as e:
        return False, "签名失败：%s" % e

    payload = {"msgtype": "markdown",
               "markdown": {"title": title, "text": "#### %s\n\n%s" % (title, text)}}
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        return False, "网络请求失败：%s" % e
    except Exception as e:
        return False, "发送异常：%s" % e

    try:
        data = json.loads(raw)
    except Exception:
        return False, "钉钉返回了非预期内容：%s" % raw[:200]

    if data.get("errcode") == 0:
        return True, "发送成功"
    return False, "钉钉返回错误（errcode=%s）：%s" % (data.get("errcode"), data.get("errmsg") or raw[:200])


def test(webhook, secret):
    """测试按钮：用传入的（可能还没保存的）webhook/secret 直接发一条测试消息。"""
    return _send_raw(webhook, secret,
                     "🔧 gygo 连通性测试",
                     "这是一条测试消息，能看到说明配置正确 ✅\n\n时间：%s"
                     % time.strftime("%Y-%m-%d %H:%M:%S"))


def send(title, text):
    """按当前已保存的配置发一条通知；未配置/未启用时静默跳过（只记日志）。"""
    cfg = get_config()
    if not cfg["enabled"] or not cfg["webhook"]:
        return False, "未配置或未启用钉钉通知"
    ok, msg = _send_raw(cfg["webhook"], cfg["secret"], title, text)
    if not ok:
        gygo_log.warn("钉钉通知发送失败", title=title, err=msg)
    return ok, msg


# ------------------------------------------------------------------ 事件级便捷方法
# 这几个函数专门给 monitor.py / subscription.py 调用，出错也不会往外抛异常，不影响主流程。
#
# 关于通知标题：钉钉机器人 markdown 消息的 title 就是手机通知栏 / 会话列表里显示的
# 预览文字，所以关键信息（剧名、集数）必须放进 title，不然要点进钉钉才看得到。

def _compress_ranges(nums):
    """[34, 35, 37] -> "34-35,37"。"""
    arr = sorted(set(int(n) for n in nums))
    if not arr:
        return ""
    out, start, prev = [], arr[0], arr[0]
    for n in arr[1:]:
        if n == prev + 1:
            prev = n
            continue
        out.append(str(start) if start == prev else "%d-%d" % (start, prev))
        start = prev = n
    out.append(str(start) if start == prev else "%d-%d" % (start, prev))
    return ",".join(out)


def _episode_numbers(file_names, episodes=None):
    """优先用调用方直接给的集数（订阅扫描已经解析过）；没给就从文件名里解析。
    返回 (集数集合, 有多少个文件没解析出集数)。"""
    if episodes:
        return set(int(e) for e in episodes), 0
    eps, unparsed = set(), 0
    for n in file_names:
        try:
            ep = episode_parse.parse_episode(n)
        except Exception:
            ep = None
        if ep is None:
            unparsed += 1
        else:
            eps.add(ep)
    return eps, unparsed


def _short(text, limit=24):
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit - 1] + "…"


def notify_monitoring(link_name, target_path=""):
    try:
        name = link_name or "未知剧集"
        send("📡 开始监控：%s" % _short(name),
             "**%s**\n\n已添加监控，出新集会自动转存到 `%s`"
             % (name, target_path or "/（根目录）"))
    except Exception as e:
        gygo_log.warn("钉钉通知异常(monitoring)", err=str(e))


def notify_transferred(link_name, file_names, target_path="", episodes=None):
    """转存成功通知。标题形如「✅ 转存了 兰香如故 34-35集」，通知栏直接可见。"""
    file_names = [n for n in (file_names or []) if n]
    if not file_names:
        return
    try:
        name = link_name or "未知剧集"
        eps, unparsed = _episode_numbers(file_names, episodes)
        if eps:
            ep_text = _compress_ranges(eps)
            title = "✅ 转存了 %s %s集" % (_short(name), ep_text)
            if unparsed:
                title += "（另有%d个文件）" % unparsed
            head = "集数：**%s**\n\n" % ep_text
        else:
            title = "✅ 转存了 %s %d个文件" % (_short(name), len(file_names))
            head = ""

        shown = file_names[:MAX_LIST_SHOW]
        lines = "\n".join("- %s" % n for n in shown)
        if len(file_names) > MAX_LIST_SHOW:
            lines += "\n- …等共 %d 个" % len(file_names)
        send(title,
             "**%s**\n\n%s转存到 `%s`，新增 %d 个：\n%s"
             % (name, head, target_path or "/（根目录）", len(file_names), lines))
    except Exception as e:
        gygo_log.warn("钉钉通知异常(transferred)", err=str(e))


def notify_invalid(link_name, reason=""):
    try:
        name = link_name or "未知剧集"
        send("⚠️ 链接失效：%s" % _short(name),
             "**%s**\n\n分享链接已失效，已停止监控。%s"
             % (name, ("原因：" + reason) if reason else ""))
    except Exception as e:
        gygo_log.warn("钉钉通知异常(invalid)", err=str(e))
