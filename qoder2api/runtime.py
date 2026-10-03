"""qoder2api.runtime —— 由 qoder_proxy.py 拆分而来。"""

import os
import threading
from collections import deque

from . import paths, settings as qoder_settings


MAX_PAYLOAD_BYTES = int(os.environ.get("QD_MAX_PAYLOAD_BYTES", 50 * 1024 * 1024))  # 50MB limit

CURRENT_REALM = os.environ.get("QD_PROXY_DEFAULT_REALM", "cn")
if CURRENT_REALM not in ("intl", "cn"):
    CURRENT_REALM = "cn"
# 出口模式：intl / cn = 仅该区；both = 双区都启用（优先 REALM_PREFERRED，
# 首选无可用账号时自动切到另一区）。CURRENT_REALM 始终等于首选/单区出口，
# 供面板展示与默认上下文使用。
REALM_MODE = CURRENT_REALM
REALM_PREFERRED = "cn" if REALM_MODE == "both" else REALM_MODE
DEFAULT_SYSTEM_PROMPT = "You are a helpful assistant."

# Usage accounting: every upstream response carries a usage block, and the
# proxy also records one JSONL line per request.
USAGE_DIR = os.environ.get("QD_PROXY_USAGE_DIR") \
    or os.path.join(str(paths.ROOT), "usage")
USAGE_LOG = os.path.join(USAGE_DIR, "usage.jsonl")
USAGE_SUMMARY = os.path.join(USAGE_DIR, "usage-summary.json")

# Web-panel access control. The panel is gated by its own password (default
# "admin"), independent of the /v1 API key. Sessions live in memory only.
PANEL = qoder_settings.PanelSessions()
API_KEY_FILE_SET = False


POOL = None
SCHEDULER = None
ACCOUNTS_DIR = str(paths.accounts_dir())
REALM_STATE_FILE = os.path.join(ACCOUNTS_DIR, "active_realm.json")


API_KEY = None
SYSTEM_PROMPT = DEFAULT_SYSTEM_PROMPT
