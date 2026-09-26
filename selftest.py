#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""gygo 自检工具 —— 不登录也能跑，用于判断"是接口变了还是我配错了"。

用法：
    python3 selftest.py                       # 跑内置示例链接
    python3 selftest.py "https://..."         # 跑你自己的分享链接

它会依次检查：
  1. 分享链接能不能解析出 share_id
  2. get_share_summary    分享是否有效
  3. get_share_access_token   能不能换到分享 token
  4. 递归列举    能不能列出视频文件（重点，最容易出问题）
  5. 登录接口路径是否还活着（不登录，只探 404）
"""

import json
import sys
import urllib.error
import urllib.request

from guangya import API_ACCOUNT, CLIENT_ID, GuangyaClient
from share_gy import (_extract_list, _is_fail, _msg, _public_post,
                      list_share_files, parse_share_input)

SAMPLE = "https://www.guangyapan.com/s/1939697587621466165_ad0FsY8EdLN_2BKm"

PASS, FAIL, WARN = "  [OK]  ", "  [!!]  ", "  [??]  "


def hr(title):
    print("\n" + "=" * 62)
    print(title)
    print("=" * 62)


def probe_account(path, body, headers):
    """探 account 接口路径是否还活着。404=路径没了，其它=路径还在。"""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(API_ACCOUNT + path, data=data, method="POST")
    for k, v in headers.items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return -1, "%s: %s" % (type(e).__name__, e)


def main():
    url = sys.argv[1] if len(sys.argv) > 1 else SAMPLE
    problems = []

    hr("1. 解析分享链接")
    print("  " + url)
    try:
        share_id, pwd, pdir = parse_share_input(url)
        print(PASS + "share_id = %s" % share_id)
        print("       提取码 = %s   子目录 = %s" % (pwd or "(无)", pdir or "(根)"))
    except Exception as e:
        print(FAIL + "解析失败：%s" % e)
        return 1

    hr("2. 分享是否有效")
    code = 0
    try:
        p = _public_post("get_share_summary", {"shareId": share_id})
        d = p.get("data") if isinstance(p.get("data"), dict) else {}
        print(PASS + "标题：%s" % (d.get("title") or "(未返回)"))
        print("       分享状态 shareStatus=%s  分享者：%s"
              % (d.get("shareStatus"), d.get("nickName")))
    except Exception as e:
        print(FAIL + "get_share_summary 失败：%s" % e)
        problems.append("get_share_summary 调不通")
        code = 1

    hr("3. 换取分享 token")
    if not code:
        try:
            p = _public_post("get_share_access_token",
                             {"shareId": share_id, "code": pwd or ""})
            tok = (p.get("data") or {}).get("accessToken") or (p.get("data") or {}).get("access_token")
            if tok:
                print(PASS + "拿到 token（%d 字符，%s...）" % (len(tok), tok[:24]))
            else:
                print(FAIL + "没拿到 token，返回：%s" % json.dumps(p, ensure_ascii=False)[:200])
                problems.append("get_share_access_token 无 token")
                code = 1
        except Exception as e:
            print(FAIL + "失败：%s" % e)
            problems.append("get_share_access_token 调不通")
            code = 1

    hr("4. 递归列举视频（最关键的一步）")
    if not code:
        try:
            files, name = list_share_files(share_id, pwd, pdir, only_video=True)
            if files:
                print(PASS + "列出 %d 个视频文件" % len(files))
                for f in files[:8]:
                    print("       - %s  (%.1f MB)" % (f["path"], f["size"] / 1048576.0))
                if len(files) > 8:
                    print("       ... 还有 %d 个" % (len(files) - 8))
            else:
                print(FAIL + "返回 0 个视频 —— 分享里可能真的没视频，"
                             "也可能是目录类型字段又变了")
                problems.append("递归列举返回 0 个文件")
        except Exception as e:
            print(FAIL + "列举失败：%s" % e)
            problems.append("递归列举异常")

    hr("5. 登录接口路径探活（不登录）")
    c = GuangyaClient()
    h = c._account_headers()
    for label, path, body, extra in [
        ("captcha/init", "/shield/captcha/init",
         {"client_id": CLIENT_ID, "action": "POST:/v1/auth/verification",
          "device_id": c.device_id, "meta": {"phone_number": "+8613800138000"}}, {}),
        ("auth/verification", "/auth/verification",
         {"phone_number": "+8613800138000", "target": "ANY", "client_id": CLIENT_ID},
         {"x-captcha-token": "probe"}),
        ("auth/token", "/auth/token",
         {"client_id": CLIENT_ID, "grant_type": "refresh_token",
          "refresh_token": "probe"}, {"x-action": "401"}),
    ]:
        headers = dict(h)
        headers.update(extra)
        st, body_raw = probe_account(path, body, headers)
        if st == 404:
            print(FAIL + "%-20s 路径已失效(404)，接口变了" % label)
            problems.append("%s 路径 404" % label)
        elif st == -1:
            print(WARN + "%-20s 连不上：%s" % (label, body_raw[:80]))
        elif st >= 500:
            print(WARN + "%-20s 服务器 %d（偶发或限流，不代表接口变了）" % (label, st))
        elif st in (200, 201):
            print(PASS + "%-20s 正常(HTTP %d)" % (label, st))
        else:
            print(PASS + "%-20s 路径在(HTTP %d，业务码属正常)" % (label, st))

    hr("结论")
    if not problems:
        print("  全部检查通过，接口没变。")
        print("  如果 gygo 仍然不工作，问题多半在登录态或目标目录，不在接口。")
        return 0
    print("  发现 %d 个问题：" % len(problems))
    for p in problems:
        print("    - " + p)
    print("\n  光鸭接口是逆向整理的，官方一改就得跟着改。")
    print("  把上面的输出贴到 https://github.com/tianjian518/gygo/issues")
    return 1


if __name__ == "__main__":
    sys.exit(main())
