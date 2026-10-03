"""qoder2api.update —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import time
import urllib.error
import urllib.request

from . import net as qoder_net
from . import VERSION
from .security import validate_public_http_url




# ---------------------------------------------------------------------------
# 项目新版本检测（对比 GitHub 最新 release；结果缓存 6 小时）
# ---------------------------------------------------------------------------
# 默认跟随「本仓库」的 release；QD_UPDATE_REPO=owner/repo 可覆盖。
UPDATE_CHECK_REPO = (os.environ.get("QD_UPDATE_REPO")
                     or "Xinghongia/qoder2api-hub")
UPDATE_CHECK_TTL = 6 * 3600
_update_cache = {"at": 0.0, "data": None}


def version_tuple(value):
    """'v1.2.3' -> (1, 2, 3)；容忍前缀与不足位数。"""
    out = []
    for chunk in str(value or "").lstrip("vV").split("."):
        digits = ""
        for ch in chunk:
            if ch.isdigit():
                digits += ch
            else:
                break
        out.append(int(digits or 0))
    while len(out) < 3:
        out.append(0)
    return tuple(out[:3])


def check_for_update(force=False):
    """检查项目是否有新版本（对比当前 VERSION 与 GitHub 最新 release）。

    返回 {ok, current, latest, has_update, url, published_at, name, checked_at,
          error, no_releases}。网络不可用/被墙时 ok=False + error，看板据此
    显示"检查失败"而不是误报；仓库还没有任何 release 时按"暂无发布记录"
    优雅处理（不当作错误）。
    """
    now = time.time()
    cached = _update_cache.get("data")
    if cached and not force and now - _update_cache.get("at", 0) < UPDATE_CHECK_TTL:
        return cached
    info = {"ok": False, "current": VERSION, "latest": "", "has_update": False,
            "url": "", "published_at": "", "name": "", "checked_at": int(now),
            "error": "", "no_releases": False}
    try:
        url = validate_public_http_url(
            "https://api.github.com/repos/%s/releases/latest" % UPDATE_CHECK_REPO)
        req = urllib.request.Request(url, headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "qoder-proxy-update-check",
        })
        with qoder_net.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        tag = str(data.get("tag_name") or "").strip()
        info.update({
            "ok": True,
            "latest": tag,
            "url": str(data.get("html_url") or ""),
            "published_at": str(data.get("published_at") or ""),
            "name": str(data.get("name") or ""),
            "has_update": bool(tag) and version_tuple(tag) > version_tuple(VERSION),
        })
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            # 仓库还没有发布过 release：不是错误，只是暂无更新源
            info.update({"ok": True, "no_releases": True})
        else:
            info["error"] = "HTTP %d" % exc.code
    except Exception as exc:
        info["error"] = str(exc)[:160]
    _update_cache.update({"at": now, "data": info})
    return info
