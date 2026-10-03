"""qoder2api.models —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import re
import time

from . import paths, runtime
from . import catalog as qoder_catalog
from . import accounts as qoder_accounts
from . import net as qoder_net
from . import sign as qoder_sign
from . import settings as qoder_settings
from .logbus import log
import urllib.request

from .body import MODELS_PATH
from .security import _lock, validate_public_http_url
from .accounts import CLIENT_UA, gateway_candidates, get_realm_config
from .sign import SESSIONS, qoder_encode




_models_cache = {"intl": {"at": 0.0, "data": None}, "cn": {"at": 0.0, "data": None}}


# ---------------------------------------------------------------------------
# 模型目录（动态 COSY model/list + 静态兜底）
# ---------------------------------------------------------------------------
NON_CHAT_MODELS = {"lite"}
NON_CHAT_PREFIXES = ("codewise-", "completion-")
NON_CHAT_SUFFIXES = ("-image-alpha", "-image-alpha-edit", "-taco-completion")


def is_chat_model(mid):
    if not mid:
        return False
    if mid in NON_CHAT_MODELS:
        return False
    if mid.startswith(NON_CHAT_PREFIXES):
        return False
    if mid.endswith(NON_CHAT_SUFFIXES):
        return False
    return True


CN_UI_ORDER = [m["key"] for m in qoder_catalog.STATIC_CN_MODELS]
INTL_UI_ORDER = [m["key"] for m in qoder_catalog.STATIC_INTL_MODELS]


def merge_catalog(primary, realm=None):
    """静态目录打底（补元数据），按官方 UI 顺序输出。

    **清单以 primary（动态接口或本机官方目录）为准**：桌面版此刻显示什么
    这里就显示什么（如动态返回 15 条就不额外塞静态独有的 2 条）；
    primary 为空时回退静态快照全量。enable/strategies 等状态不做任何裁剪。
    """
    r = realm or runtime.CURRENT_REALM
    merged = {}
    source_static = getattr(qoder_catalog, "STATIC_CN_MODELS" if r == "cn"
                            else "STATIC_INTL_MODELS", qoder_catalog.STATIC_MODELS)
    for item in source_static:
        mid = item.get("key") or item.get("id")
        if mid and is_chat_model(mid):
            merged[mid] = dict(item)
    primary = list(primary or [])
    primary_keys = None
    for mid, meta in primary:
        if not is_chat_model(mid):
            continue
        if meta:
            base = merged.get(mid) or {}
            # None 值不覆盖：动态接口对部分条目返回 null（如 context_config/
            # thinking_config/description），静默 null 会把静态快照里的真实
            # 值抹掉——只用有值字段做增量覆盖。
            base.update({k: v for k, v in meta.items() if v is not None})
            merged[mid] = base
        elif mid not in merged:
            merged[mid] = {}
    if primary:
        primary_keys = {mid for mid, _ in primary if is_chat_model(mid)}
    order = CN_UI_ORDER if r == "cn" else INTL_UI_ORDER
    out = []
    seen = set()

    def wanted(mid):
        if not is_chat_model(mid):
            return False
        if primary_keys is not None and mid not in primary_keys:
            return False      # 静态独有、官方此刻未列出的条目不外塞
        return True

    for mid in order:
        if mid in merged and wanted(mid):
            out.append((mid, merged[mid]))
            seen.add(mid)
    for mid, meta in merged.items():
        if mid not in seen and wanted(mid):
            out.append((mid, meta))
    return out


def read_local_models(realm=None):
    """读取本机官方客户端的模型目录缓存（QMC 解密），无网络也能跟官方对齐。

    路径：~/.qoder/.models/<uid>/catalog-v6（intl）/ ~/.qoder-cn/...（cn）
    返回 [(key, meta)]；任何失败返回 []（回退静态快照）。
    """
    r = realm or runtime.CURRENT_REALM
    home = os.path.join(os.path.expanduser("~"),
                        get_realm_config(r)["home_dir"], ".models")
    try:
        default_path = os.path.join(home, "default")
        uid = ""
        with open(default_path, encoding="utf-8") as fh:
            uid = str(json.load(fh).get("uid") or "")
        subdirs = [uid] if uid and os.path.isdir(os.path.join(home, uid)) else \
            [d for d in os.listdir(home) if os.path.isdir(os.path.join(home, d))]
        for sub in subdirs:
            cat = os.path.join(home, sub, "catalog-v6")
            if not os.path.isfile(cat):
                continue
            with open(cat, "rb") as fh:
                blob = fh.read().decode("ascii", "replace")
            from qoder2api.sign import qmc_decrypt
            plain = json.loads(qmc_decrypt(blob, sub).decode("utf-8"))
            chat = plain.get("chat") or []
            out = []
            for m in chat:
                if not isinstance(m, dict) or not m.get("key"):
                    continue
                # 官方本地目录条目全字段原样
                row = dict(m)
                row["id"] = m["key"]
                row.setdefault("name", m.get("display_name") or m["key"])
                row.setdefault("display_name", m.get("display_name") or m["key"])
                out.append((m["key"], row))
            if out:
                log("model catalog read from local client cache: %d models (%s)"
                    % (len(out), r))
                return out
    except Exception as exc:
        log("local model catalog read skipped: %s" % exc)
    return []


def union_model_entries(parts):
    """合并两区模型清单：按上游 key 去重，标注每条可用的出口。

    parts = [(realm, [(mid, meta), ...]), ...]，先出现的列表优先（首选区在前）。
    共享模型 `realm="both"` 且 `realms` 列出两区；区域独占模型只带自身区域。
    命中的 meta 一律复制，不污染 fetch_models 的按区缓存。
    """
    merged = {}
    order = []
    for realm, entries in parts:
        for mid, meta in entries or []:
            if mid not in merged:
                item = dict(meta or {})
                item["realm"] = realm
                item["realms"] = [realm]
                merged[mid] = item
                order.append(mid)
            else:
                item = merged[mid]
                if realm not in item["realms"]:
                    item["realms"].append(realm)
                item["realm"] = "both"
    return [(mid, merged[mid]) for mid in order]


def _fetch_models_both():
    """双区出口模式下的并集清单：首选区在前，其次另一区。"""
    first = runtime.REALM_PREFERRED if runtime.REALM_PREFERRED in ("intl", "cn") else "cn"
    second = "cn" if first == "intl" else "intl"
    return union_model_entries([(first, fetch_models(realm=first)),
                                (second, fetch_models(realm=second))])


def fetch_models(realm=None):
    r = realm or runtime.CURRENT_REALM
    with _lock:
        c = _models_cache.get(r) or {"at": 0.0, "data": None}
        if c["data"] and time.time() - c["at"] < 300:
            return c["data"]
    if r == "both":
        entries = _fetch_models_both()
    else:
        # 1) 动态接口（需要账号） 2) 本机官方目录缓存 3) 内嵌静态快照
        live = read_dynamic_models(realm=r)
        if not live:
            live = read_local_models(realm=r)
        entries = merge_catalog(live, realm=r)
    with _lock:
        _models_cache[r] = {"at": time.time(), "data": entries}
    return entries


def read_dynamic_models(realm=None):
    """COSY 签名拉取 /algo/api/v2/model/list（chat scene）。失败返回 []。

    注意：签名的 body 是 qoder_encode("{}")，请求必须**带同款 body**发出
    （服务端校验签名与 body 一致，裸 GET 会 403）。

    主机按官方的候选顺序尝试（国际版 api1 → api2 → api3）：切换主机不影响
    签名（签名只覆盖 path），单个域名故障不再导致模型清单整体拉取失败。
    """
    r = realm or runtime.CURRENT_REALM
    account = runtime.POOL.pick(realm=r) if runtime.POOL else None
    if account is None:
        return []
    # 签名以 qoder_encode("{}") 作为 body 参与 MD5；请求同样携带该 body。
    sign_body = qoder_encode(b"{}")
    payload = None
    for host in gateway_candidates(r):
        raw_url = host + MODELS_PATH
        try:
            sess = SESSIONS.get(account)
            headers = sess.headers(sign_body, raw_url, model_key="", sse=False,
                                   accept="application/json")
            headers["User-Agent"] = CLIENT_UA
            url = validate_public_http_url(raw_url)
            req = urllib.request.Request(url, data=sign_body.encode("utf-8"),
                                         method="GET", headers=headers)
            with qoder_net.urlopen(req, timeout=15) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            break
        except Exception as exc:
            log("model discovery failed on %s: %s" % (host, exc))
    if payload is None:
        return []
    chat = payload.get("chat") or []
    out = []
    for m in chat:
        if not isinstance(m, dict):
            continue
        mid = m.get("key")
        if not mid:
            continue
        # 官方原始条目**全字段原样**透传（enable/strategies/is_editable/
        # minimal_version/... 一律不裁剪），仅补 id/name 兼容键。
        row = dict(m)
        row["id"] = mid
        row.setdefault("name", m.get("display_name") or mid)
        row.setdefault("display_name", m.get("display_name") or mid)
        out.append((mid, row))
    if out:
        log("model discovery ok: %d chat models from %s" % (len(out), r))
    return out
