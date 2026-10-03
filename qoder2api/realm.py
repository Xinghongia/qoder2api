"""qoder2api.realm —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import time

from . import catalog as qoder_catalog
from . import runtime
from pathlib import Path

from .logbus import log




def detect_model_realm(model_id):
    """模型 -> 出口区域。区域独占模型强制路由到其归属出口，其余跟随全局开关。"""
    owner = exclusive_realm(model_id)
    if owner:
        return owner
    if not model_id:
        return runtime.CURRENT_REALM
    return runtime.CURRENT_REALM


# 目前双区模型清单不同（源自官方 catalog 快照差集），独占表给区域特供模型。
INTL_EXCLUSIVE_PREFIXES = getattr(qoder_catalog, "INTL_EXCLUSIVE_PREFIXES", ())
CN_EXCLUSIVE_PREFIXES = getattr(qoder_catalog, "CN_EXCLUSIVE_PREFIXES", ())
INTL_EXCLUSIVE = getattr(qoder_catalog, "INTL_EXCLUSIVE", set())
CN_EXCLUSIVE = getattr(qoder_catalog, "CN_EXCLUSIVE", set())


def exclusive_realm(model_id):
    """"intl"/"cn" when only that exit serves the model, else "".

    先把人类可读别名解析成上游 key 再判独占（glm-5.2 -> gm51model 是国内独占）。
    """
    if not model_id:
        return ""
    m = str(model_id).lower()
    resolved = qoder_catalog.resolve_upstream_key(model_id).lower()
    for cand in (resolved, m):
        if cand in INTL_EXCLUSIVE or cand.startswith(INTL_EXCLUSIVE_PREFIXES):
            return "intl"
        if cand in CN_EXCLUSIVE or cand.startswith(CN_EXCLUSIVE_PREFIXES):
            return "cn"
    return ""


def _normalize_realm_state(mode, preferred=None):
    """把 (mode, preferred) 归一化为合法组合；非法输入回退到当前状态。"""
    mode = str(mode or "").strip().lower()
    preferred = str(preferred or "").strip().lower()
    if mode not in ("intl", "cn", "both"):
        mode = runtime.REALM_MODE
    if mode == "both":
        if preferred not in ("intl", "cn"):
            preferred = runtime.REALM_PREFERRED if runtime.REALM_PREFERRED in ("intl", "cn") \
                else "cn"
    else:
        preferred = mode
    return mode, preferred


def load_persisted_realm():
    
    if os.path.isfile(runtime.REALM_STATE_FILE):
        try:
            with open(runtime.REALM_STATE_FILE, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            # 兼容旧格式 {"realm": "cn"}（v1.1.4 之前只有单区开关）
            mode, preferred = _normalize_realm_state(d.get("mode") or d.get("realm"),
                                                     d.get("preferred"))
            runtime.REALM_MODE, runtime.REALM_PREFERRED = mode, preferred
            runtime.CURRENT_REALM = preferred
        except Exception as e:
            log("could not load active realm: %s" % e)
    return runtime.CURRENT_REALM


def save_persisted_realm(mode, preferred=None):
    """持久化出口模式：intl / cn / both(+preferred)。"""
    
    mode, preferred = _normalize_realm_state(mode, preferred)
    runtime.REALM_MODE, runtime.REALM_PREFERRED = mode, preferred
    runtime.CURRENT_REALM = preferred
    try:
        state_dir = Path(runtime.ACCOUNTS_DIR).resolve()
        state_dir.mkdir(parents=True, exist_ok=True)
        state_file = state_dir / os.path.basename(runtime.REALM_STATE_FILE)
        if not state_file.is_relative_to(state_dir):
            raise ValueError("realm state path escapes base directory")
        state_file.write_text(
            json.dumps({"mode": mode, "preferred": preferred,
                        "realm": preferred,   # 旧版字段，向后兼容
                        "updated_at": time.time(),
                        "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S")},
                       indent=2),
            encoding="utf-8")
        log("persisted realm mode '%s' (preferred=%s) to disk"
            % (mode, preferred))
    except Exception as exc:
        log("failed to persist active realm: %s" % exc)
    return runtime.CURRENT_REALM


def realm_candidates(model=None, explicit=None):
    """一次请求可用的出口列表（按优先级）。

    显式指定（Key 绑定 / X-Realm / ?realm=）只用该出口；区域独占模型固定
    在归属出口；否则按面板模式：单区=只有该区；双区(both)=优先首选出口，
    首选无可用账号时自动换另一个（见 pick_serving_realm）。
    """
    if explicit in ("intl", "cn"):
        return [explicit]
    owner = exclusive_realm(model) if model else ""
    if owner:
        return [owner]
    if runtime.REALM_MODE == "both":
        other = "cn" if runtime.REALM_PREFERRED == "intl" else "intl"
        return [runtime.REALM_PREFERRED, other]
    return [runtime.REALM_MODE]


def pick_serving_realm(candidates, model=None):
    """选实际服务本次请求的出口：首选有可用账号就用首选；没有就顺延到
    下一个有账号的出口；全都没有时仍返回第一个（让错误信息更直观）。"""
    if runtime.POOL:
        for r in candidates:
            if runtime.POOL.count_ready(r, model=model) > 0:
                return r
    return candidates[0]


def acc_realm(account):
    return account.realm if account else runtime.CURRENT_REALM
