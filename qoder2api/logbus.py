"""qoder2api.logbus —— 由 qoder_proxy.py 拆分而来。"""

import os
import sys
import threading
import time
from collections import deque




LOG_BUFFER = deque(maxlen=2000)
_LOG_LOCK = threading.Lock()
_LOG_COUNTER = 0


def add_log_entry(msg, level=None, tag=None):
    global _LOG_COUNTER
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    t_short = time.strftime("%H:%M:%S")
    msg_str = str(msg).rstrip()
    if not level:
        lower = msg_str.lower()
        if any(k in lower for k in ("error", "exception", "failed", "traceback",
                                    "errno", "fatal", "token_expire", "12153")):
            level = "ERROR"
        elif any(k in lower for k in ("warn", "warning", "retry", "timeout")):
            level = "WARN"
        else:
            level = "INFO"
    if not tag:
        lower = msg_str.lower()
        if "chat:" in lower or "chat done" in lower or "/v1/chat" in lower \
                or "/chat/completions" in lower or "responses" in lower:
            tag = "chat"
        elif "scheduler" in lower or "调度器" in lower:
            tag = "scheduler"
        elif "task" in lower or "任务" in lower or "签到" in lower \
                or "checkin" in lower or "福利" in lower:
            tag = "tasks"
        elif "account" in lower or "账号" in lower or "pool" in lower \
                or "imported" in lower:
            tag = "accounts"
        elif "model" in lower or "catalog" in lower or "模型" in lower:
            tag = "catalog"
        elif "auth" in lower or "token" in lower or "oauth" in lower:
            tag = "auth"
        elif "settings" in lower or "设置" in lower:
            tag = "settings"
        else:
            tag = "system"
    with _LOG_LOCK:
        _LOG_COUNTER += 1
        entry = {
            "id": _LOG_COUNTER,
            "ts": ts,
            "time": t_short,
            "level": level,
            "tag": tag,
            "msg": msg_str,
        }
        LOG_BUFFER.append(entry)
    return entry


def log(msg, level=None, tag=None):
    sys.stderr.write("[qd-proxy] %s %s\n" % (time.strftime("%H:%M:%S"), msg))
    sys.stderr.flush()
    add_log_entry(msg, level=level, tag=tag)


def get_logs(limit=200, level="", tag="", search="", since_id=0):
    with _LOG_LOCK:
        items = list(LOG_BUFFER)
    if since_id > 0:
        items = [x for x in items if x["id"] > since_id]
    if level:
        items = [x for x in items if x["level"] == level.upper()]
    if tag:
        items = [x for x in items if x["tag"].lower() == tag.lower()]
    if search:
        s = search.lower()
        items = [x for x in items if s in x["msg"].lower() or s in x["tag"].lower()]
    total = len(items)
    if limit and limit > 0 and since_id == 0:
        items = items[-limit:]
    max_id = items[-1]["id"] if items else since_id
    return {"total": total, "logs": items, "max_id": max_id}


def clear_logs():
    with _LOG_LOCK:
        LOG_BUFFER.clear()
