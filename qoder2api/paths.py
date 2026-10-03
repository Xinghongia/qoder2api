"""qoder2api/paths.py —— 仓库路径解析（唯一权威来源）

对外契约：`accounts/` 与 `usage/` 默认仍在**仓库根目录**，与拆分前完全一致
（.bat 无参启动、docker-compose 卷挂载、ACCOUNTS_DIR / QD_PROXY_USAGE_DIR
环境变量都锚定这个位置）。本文件是唯一允许 `..` 上溯的地方，其余模块一律
从这里取路径。
"""
import os
from pathlib import Path

PACKAGE_DIR = Path(__file__).resolve().parent
ROOT = PACKAGE_DIR.parent
ASSETS_DIR = PACKAGE_DIR / "assets"

DASHBOARD_HTML = ROOT / "dashboard.html"
LEGACY_DASHBOARD_HTML = ROOT / "legacy" / "dashboard.html"
WEB_OUT_DIR = ROOT / "web" / "out"


def accounts_dir():
    """账号目录：ACCOUNTS_DIR 环境变量 > 仓库根 accounts/。"""
    return Path(os.environ.get("ACCOUNTS_DIR") or (ROOT / "accounts"))


def usage_dir():
    """用量目录：QD_PROXY_USAGE_DIR 环境变量 > 仓库根 usage/。"""
    return Path(os.environ.get("QD_PROXY_USAGE_DIR") or (ROOT / "usage"))


def static_dir():
    """前端静态产物目录：QD_WEB_DIR 环境变量 > 仓库根 web/out。"""
    return Path(os.environ.get("QD_WEB_DIR") or WEB_OUT_DIR)


def baseprompt_path():
    """官方请求体模板（随包分发）。"""
    return ASSETS_DIR / "baseprompt.json"
