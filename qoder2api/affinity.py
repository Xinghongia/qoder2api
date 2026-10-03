"""qoder2api.affinity —— 由 qoder_proxy.py 拆分而来。"""

import hashlib
import json
import os
import re
import time

from . import runtime




# ---------------------------------------------------------------------------
# 前缀会话亲和（同一对话固定同一账号，命中上游按账号的 prompt 缓存）
# ---------------------------------------------------------------------------
AFFINITY_BY_PREFIX = os.environ.get("QD_AFFINITY_BY_PREFIX", "1").lower() not in (
    "0", "false", "no", "off")
AFFINITY_DEBUG = os.environ.get("QD_AFFINITY_DEBUG", "0").lower() in (
    "1", "true", "yes", "on")


def derive_affinity_key(messages):
    """Derive a stable affinity key from a conversation's stable prefix.

    前两条消息（system + 首轮 user）在整段对话生命周期内不变，因此同一对话
    每一轮都落到同一上游账号 —— 正是 prompt 缓存需要的；不同对话首轮不同，
    依旧分散到各账号，负载均衡不受影响。
    """
    if not AFFINITY_BY_PREFIX:
        return None
    try:
        msgs = messages or []
        if not msgs:
            return None
        head = msgs[:2]
        blob = json.dumps(head, ensure_ascii=False,
                          sort_keys=True).encode("utf-8")
        return "pfx-" + hashlib.sha256(blob).hexdigest()[:16]
    except Exception:
        return None


def prompt_fingerprint(messages):
    """Privacy-safe fingerprint of the outgoing prompt.

    Cache hits need a byte-identical prefix, so these hashes answer "is my
    prefix stable / is my conversation continuous?" without storing any text.
    """
    try:
        def h(obj):
            blob = json.dumps(obj, ensure_ascii=False,
                              sort_keys=True).encode("utf-8")
            return hashlib.sha256(blob).hexdigest()[:12]
        msgs = messages or []
        out = {"msgs_sha": h(msgs), "n_msgs": len(msgs)}
        if msgs:
            out["system_sha"] = h(msgs[0]) if msgs[0].get("role") == "system" else ""
            out["prefix_sha"] = h(msgs[:-1]) if len(msgs) > 1 else ""
        return out
    except Exception:
        return {}
