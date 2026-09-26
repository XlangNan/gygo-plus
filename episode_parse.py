"""从文件名里尽量猜出"第几集"，给订阅追更用来判断这一集有没有转存过。

分享链接里的文件名太乱了（尤其是动漫/字幕组压制，比如
"[SweetSub][葬送的芙莉莲][01][1080p][简体内嵌].mp4"），自己写正则规则很难
覆盖所有花样，所以这里按"从专到通用"接入两个社区维护的解析库，都失败了
再退回内置正则兜底：

  1. guessit  —— 通用视频文件名解析，覆盖美剧/电影/综艺等主流命名习惯，
     Sonarr/Bazarr 这类工具背后常用的库，见 https://github.com/guessit-io/guessit
  2. anitopy  —— 专门解析动漫字幕组命名（基于 C++ 库 Anitomy 的 Python 移植），
     对 guessit 不擅长的"[字幕组][剧名][集数][画质]"这种格式更准，见
     https://github.com/igorcmoura/anitopy
  3. 内置正则兜底 —— 上面两个库没装上（pip 装不了、网络问题）或者都没猜出来，
     就用这里写的规则硬猜，保证功能不会因为可选依赖缺失而完全瘫痪。

guessit / anitopy 都是纯 Python、没有 C 扩展，Dockerfile 里会在构建时
`pip install` 一次；装不上完全不影响其它功能，只是集数识别准确率会退回
兜底规则的水平。
"""

import re

try:
    import guessit as _guessit_lib
except Exception:
    _guessit_lib = None

try:
    import anitopy as _anitopy_lib
except Exception:
    _anitopy_lib = None


def _first(v):
    """guessit/anitopy 对"多集"文件会返回列表，这里只取第一个。"""
    if isinstance(v, (list, tuple)):
        return v[0] if v else None
    return v


def _as_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def _season_matches(guessed_season, season):
    """season 是调用方指定的期望季号；guessed_season 是解析库猜出来的。
    只要没冲突就算通过——很多动漫命名压根不带季号，这时不能因为"猜不出
    季号"就否掉这一集，只有"猜出来的季号明确对不上"才该跳过。
    """
    if season in (None, "", 0):
        return True
    gs = _as_int(_first(guessed_season))
    if gs is None:
        return True
    return gs == int(season)


def _parse_with_guessit(base, season):
    if _guessit_lib is None:
        return None
    try:
        info = _guessit_lib.guessit(base)
    except Exception:
        return None
    ep = _as_int(_first(info.get("episode")))
    if ep is None:
        return None
    if not _season_matches(info.get("season"), season):
        return None
    return ep


def _parse_with_anitopy(base, season):
    if _anitopy_lib is None:
        return None
    try:
        info = _anitopy_lib.parse(base)
    except Exception:
        return None
    if not isinstance(info, dict):
        return None
    ep = _as_int(_first(info.get("episode_number")))
    if ep is None:
        return None
    if not _season_matches(info.get("anime_season"), season):
        return None
    return ep


# ------------------------------------------------------------------ 内置正则兜底
# 规则按从严到松的顺序尝试，命中一条就不往下走：
#   1. SxxEyy（S01E05 / s1e05 ...）—— 指定了季数就必须对得上
#   2. 第xx集 / 第xx话
#   3. EPxx / Exx（没有 S 前缀的单集号，如 "EP05"）
#   4. 兜底：文件名里孤立的数字，先清掉分辨率/编码/年份这些干扰项，
#      剩下候选取最后一个（常见形态是"剧名 - 05.mp4"）

_SXXEYY = re.compile(r'[Ss](\d{1,2})[Ee](\d{1,4})')
_CN_JI = re.compile(r'第\s*(\d{1,4})\s*[集话話]')
_EPXX = re.compile(r'(?:^|[^A-Za-z0-9])[Ee][Pp]?(\d{1,4})(?:[^0-9]|$)')

_CLEAN_RES = re.compile(r'\d{3,4}[pi]\b', re.I)
_CLEAN_CODEC = re.compile(r'[xXhH]\.?26[45]')
_CLEAN_BIT = re.compile(r'\b(?:8|10)\s*[Bb]it\b')
_CLEAN_YEAR_P = re.compile(r'[\(\[]((?:19|20)\d{2})[\)\]]')
_CLEAN_YEAR = re.compile(r'\b(?:19|20)\d{2}\b')
_FALLBACK_NUM = re.compile(r'(?:^|[^0-9])(\d{1,4})(?:[^0-9]|$)')


def _parse_fallback(base, season):
    m = _SXXEYY.search(base)
    if m:
        s, e = int(m.group(1)), int(m.group(2))
        if season in (None, "", 0) or int(season) == s:
            return e
        return None  # 季号对不上，明确不是这一季的，别再往下猜

    m = _CN_JI.search(base)
    if m:
        return int(m.group(1))

    m = _EPXX.search(base)
    if m:
        return int(m.group(1))

    cleaned = base
    cleaned = _CLEAN_RES.sub(' ', cleaned)
    cleaned = _CLEAN_CODEC.sub(' ', cleaned)
    cleaned = _CLEAN_BIT.sub(' ', cleaned)
    cleaned = _CLEAN_YEAR_P.sub(' ', cleaned)
    cleaned = _CLEAN_YEAR.sub(' ', cleaned)

    candidates = [int(g) for g in _FALLBACK_NUM.findall(cleaned)]
    return candidates[-1] if candidates else None


# ------------------------------------------------------------------ 对外入口

def parse_episode(filename, season=None):
    """返回猜出来的集数（int），猜不出来返回 None。

    依次尝试 guessit → anitopy → 内置正则，前一个猜不出来（或没装上）才
    试下一个，不会互相覆盖已经猜对的结果。
    """
    name = str(filename or "")
    base = name.rsplit("/", 1)[-1]
    base_noext = re.sub(r'\.[A-Za-z0-9]{2,5}$', '', base)

    ep = _parse_with_guessit(base, season)
    if ep is not None:
        return ep

    ep = _parse_with_anitopy(base, season)
    if ep is not None:
        return ep

    return _parse_fallback(base_noext, season)
