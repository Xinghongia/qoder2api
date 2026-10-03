"""qoder_accounts.py —— Qoder 双区域账号池、OAuth 设备授权与凭证生命周期

覆盖与 WorkBuddy 网关同等完整的账号能力：

  - 双区域常量表 REALM_CONFIGS（国内 qoder.com.cn / 国际 qoder.com）
  - OAuth 设备授权（PKCE S256，浏览器授权 + /deviceToken/poll 轮询，
    dt- 30 天 / drt- 1 年）——免桌面客户端一键登录
  - PAT 导入（pt- 长期令牌 -> jobToken 交换 jt-/jrt-）
  - 按 token 前缀路由的刷新（drt- -> deviceToken/refresh；
    jrt- -> jobToken/refresh，失败回落 PAT 重新交换）
  - 每日签到 / 额度（quota）/ 套餐（plan）查询
  - 会话亲和（同一对话固定落到同一账号）与轮询负载
  - 账号导入导出（Dry-Run 预检）与 JSON 持久化（原子写）
"""
import base64
import ipaddress
import json
import os
import re
import socket
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
import uuid

from qoder_fingerprint import (derive_id, generate_request_id,
                               derive_machine_token, derive_machine_type,
                               vm_status)
import qoder_net
import qoder_settings

# ---------------------------------------------------------------------------
# 区域常量（逆向自官方桌面/CLI 客户端）
# ---------------------------------------------------------------------------
REALM_CONFIGS = {
    "cn": {
        "name": "国内版 (China)",
        "openapi": "https://openapi.qoder.com.cn",
        "gateway": "https://gateway.qoder.com.cn",
        "website": "https://qoder.com.cn",
        "client_id": "1c5e33e1-364d-4ce6-b02c-acaa81274a5c",
        "redirect_uri": "qoder-work-cn://",
        "domain": "qoder.com.cn",
        "ua": "QoderWork/1.1.64",
        # has_checkin 只是"历史上该区域曾开放 sash 签到"的提示位，**不再作为
        # 门控**：能力改为运行时探测（见 Account.checkin_capability）。官方把
        # 每日领取活动搬到 campaign 平台后，任何区域都可能新增/下线接口。
        "has_checkin": True,
        "send_client_id": True,    # CN 设备授权 URL: client_id + machine_id + redirect_uri
        "send_redirect_uri": True,
        "nonce_dashed": True,      # CN nonce 使用带横线 uuid
        "home_dir": ".qoder-cn",   # 官方客户端本地目录（凭证 / 模型目录缓存）
        "app_dir": "com.qodercn.app.stable",   # 桌面 App Roaming 数据目录
    },
    "intl": {
        "name": "国际版 (Global)",
        "openapi": "https://openapi.qoder.sh",
        # 推理主机取自 0.4.3 客户端的 endpoint 缓存/内置候选（api1 主选，
        # api2/api3 为官方故障切换域名）：api1 连不上时按顺序切换。
        "gateway": "https://api1.qoder.sh",
        "gateway_fallbacks": ("https://api2.qoder.sh", "https://api3.qoder.sh"),
        "website": "https://qoder.com",
        "client_id": "e883ade2-e6e3-4d6d-adf7-f92ceff5fdcb",
        "redirect_uri": "qoder://aicoding.aicoding-agent/login-success",
        "domain": "qoder.com",
        "ua": "Qoder/1.1.64",
        # 国际版目前 /sash/api/v1/me/daily-check-in/* 返回 404（实测），但活动
        # 页面同样挂着"每日领取 100 Credits"。该字段仅作提示，门控靠运行时探测。
        "has_checkin": False,
        "send_client_id": True,    # Intl 设备授权 URL: client_id + machine_id (无 redirect_uri)
        "send_redirect_uri": False,
        "nonce_dashed": False,     # intl nonce 为 32-hex uuid-simple
        "home_dir": ".qoder",
        "app_dir": "com.qoder.app.stable",
    },
}

CLIENT_UA = "Go-http-client/2.0"
LOGIN_TTL_SECONDS = 600
DEFAULT_USER_TYPE = "personal_professional_trial"

# 业务端点（全部挂 openapi 基址，纯 Bearer，无 COSY 签名）
PATH_DEVICE_POLL = "/api/v1/deviceToken/poll"
PATH_DEVICE_REFRESH = "/api/v1/deviceToken/refresh"
PATH_JOB_EXCHANGE = "/api/v1/jobToken/exchange"
PATH_JOB_REFRESH = "/api/v1/jobToken/refresh"
PATH_USERINFO = "/api/v1/userinfo"
PATH_QUOTA = "/api/v2/quota/usage"
PATH_PLAN = "/api/v2/user/plan"
PATH_CHECKIN_STATUS = "/sash/api/v1/me/daily-check-in/status"
PATH_CHECKIN_CLAIM = "/sash/api/v1/me/daily-check-in/claim"
PATH_PRO_ELIGIBILITY = "/sash/api/v1/me/pro-upgrade/eligibility"
PATH_PRO_CLAIM = "/sash/api/v1/me/pro-upgrade/claim"
# 官方新活动平台（双区域通用，实测 cn/intl 均 200）：服务端下发活动列表与
# campaignUrl/JS，领取动作由桌面客户端承接；网关用它做状态呈现与提示。
PATH_CAMPAIGNS = "/sash/api/v1/me/campaigns"
# 单个活动的奖励查询 / 领取（逆向自官方 growth-page/activity-iframe 页面：
#   GET  /sash/api/v1/me/campaigns/{campaignId}/reward
#   POST /sash/api/v1/me/campaigns/{campaignId}/claim   —— 领取（幂等：
#        已领取返回 {"status":"CLAIMED","replayed":true}，不会重复发放）
PATH_CAMPAIGN_REWARD = "/sash/api/v1/me/campaigns/%s/reward"
PATH_CAMPAIGN_CLAIM = "/sash/api/v1/me/campaigns/%s/claim"

# 桌面端专用请求头（活动平台必需；CLI=5 / QoderWork=6 / 桌面端=10）
DESKTOP_CLIENT_TYPE = "10"
DESKTOP_CLIENT_VERSION = "0.4.3"      # 可用 QD_DESKTOP_VERSION 覆盖
MACHINE_OS = "x86_64_win32"
MACHINE_HOSTNAME = "DESKTOP-QODER"


def desktop_version():
    """桌面端版本号（Cosy-Version）；客户端更新后可用环境变量覆盖，
    或直接跑 `python _refresh_catalog.py` 时按已安装客户端自动对齐。"""
    return (os.environ.get("QD_DESKTOP_VERSION") or DESKTOP_CLIENT_VERSION).strip() \
        or DESKTOP_CLIENT_VERSION


# ---------------------------------------------------------------------------
# 官方桌面端原生风控身份（activity/campaign 列表按它过滤，必须是"真"身份）
# ---------------------------------------------------------------------------
# 官方桌面端在调用活动平台前，会 spawn 自己的原生桥取机器身份：
#     <install>/resources/umid/runtime-info.exe prod --account-stdin
#     stdin: {"account": <uid>}   stdout: {"machineToken","machineType","machineCode",...}
# 服务端**按这些值过滤设备定向活动**：派生的假身份不会报错，但活动列表里
# 会静默少掉"每日领取 100 Credits"这类条目（实测：换用原生身份后立刻出现
# CLAIMABLE 活动）。因此网关优先调用同一个官方二进制取真值，失败才回退派生值。
# 原生身份缓存：身份会随时间轮换，但**旧身份仍被服务端接受**（上游实测复用
# 后依然 showCampaign=true），真正的成本是每次强制刷新要跑约 3.7 秒的官方
# 二进制——这正是首屏/切视图卡顿的来源。因此做长缓存（30 分钟），并用
# "列表被判为未认可时刷新重试一次"（campaigns() 内）与"领取前强制刷新"
# （campaign_checkin）兜底自愈。
# 注意：身份是**机器级**的（不同账号/不存在的账号 id/甚至不同区域都返回同一份，
# 实测一致），因此按区域缓存即可，同一台机器上的多个账号共用是正确的；
# **本区域没装客户端时借用另一区域的桥**（如只装了国内版客户端时，国际版账号
# 也能拿到真实身份，不再退化为派生假值——见 _realm_search_order）。
NATIVE_IDENTITY_TTL = 1800
_native_exe_cache = {}      # realm -> (exe路径, 桥来源区域)
_native_ident_cache = {}


def _localappdata():
    return os.environ.get("LOCALAPPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Local")


def _read_ini(path, key):
    """读 launcher 的 state.ini（UTF-8 或 UTF-16），返回 key=value 的 value。"""
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except Exception:
        return ""
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = raw.decode(enc)
        except Exception:
            continue
        for line in text.splitlines():
            line = line.strip()
            if line.lower().startswith(key.lower() + "="):
                return line.split("=", 1)[1].strip()
    return ""


def desktop_install_dir(realm):
    """桌面端安装目录（launcher state.ini 的 installDir；找不到返回空串）。"""
    base = _localappdata()
    names = ("Qoder CN", "QoderCN", "Qoder") if realm == "cn" else ("Qoder",)
    for name in names:
        for launcher in ("%s Launcher" % name, "Launcher"):
            ini = os.path.join(base, name, launcher, "state.ini")
            if os.path.isfile(ini):
                d = _read_ini(ini, "installDir")
                if d and os.path.isdir(d):
                    return d
    for name in names:
        d = os.path.join(base, "Programs", name)
        if os.path.isdir(d):
            return d
    return ""


def _realm_search_order(realm):
    """找风控桥的区域搜索顺序：本区域优先，其次借用另一区域。

    机器身份是**机器级、与区域无关**的：实测同一台机器上，国内账号与国际账号
    走同一个桥取到的 machineToken/machineType/machineCode **完全一致**（桥的
    输出里也没有任何区域字段）。因此本机只装了国内版客户端时，国际版账号
    借用国内版的桥是安全的——这正是"国际账号一直在用派生假身份"的修复。
    """
    return ("cn", "intl") if realm == "cn" else ("intl", "cn")


def _realm_home_dir(r):
    """该区域 CLI/agent 的用户目录（.qoder-cn / .qoder）。"""
    return os.path.join(os.path.expanduser("~"),
                        ".qoder-cn" if r == "cn" else ".qoder")


def _realm_runtime_info_paths(r):
    """单区域的桥候选来源（不跨区借用）：
      1) 桌面端安装目录 `<install>/resources/umid/runtime-info.exe`；
      2) CLI/agent 缓存 `~/.qoder-cn(.qoder)/.bin/umid-*/runtime-info.exe`
         （只跑过 CLI、没装桌面端时也有）。
    """
    out = []
    d = desktop_install_dir(r)
    if d:
        out.append(os.path.join(d, "resources", "umid", "runtime-info.exe"))
    bin_dir = os.path.join(_realm_home_dir(r), ".bin")
    try:
        names = sorted(os.listdir(bin_dir))
    except Exception:
        names = []
    for name in names:
        if name.startswith("umid"):
            out.append(os.path.join(bin_dir, name, "runtime-info.exe"))
    return out


def _runtime_info_candidates(realm):
    """(路径, 来源区域) 列表：本区域在前、借用区域在后，去重保序。"""
    seen, out = set(), []
    for r in _realm_search_order(realm):
        for cand in _realm_runtime_info_paths(r):
            key = cand.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append((cand, r))
    return out


def candidate_runtime_info_paths(realm):
    """按优先级列出该区域可用的 runtime-info.exe 路径（不校验存在性）。"""
    return [p for p, _r in _runtime_info_candidates(realm)]


def _runtime_info_resolution(realm):
    """定位风控桥，返回 (exe路径, 桥来源区域)，按区域缓存。

    来源区域：'cn'/'intl'（真实区域）、'explicit'（QD_RUNTIME_INFO 指定）、
    ''（未找到）。QD_NATIVE_IDENTITY=0 关闭全部原生桥（返回空）。
    """
    if (os.environ.get("QD_NATIVE_IDENTITY") or "1").strip() in ("0", "false", "no"):
        return "", ""
    if realm in _native_exe_cache:
        return _native_exe_cache[realm]
    resolved = ("", "")
    explicit = (os.environ.get("QD_RUNTIME_INFO") or "").strip()
    if explicit and os.path.isfile(explicit):
        resolved = (explicit, "explicit")
    else:
        for cand, r in _runtime_info_candidates(realm):
            if os.path.isfile(cand):
                resolved = (cand, r)
                break
    _native_exe_cache[realm] = resolved
    return resolved


def runtime_info_exe(realm):
    """定位官方 runtime-info.exe（原生风控身份桥）；找不到返回空串。

    可用 QD_NATIVE_IDENTITY=0 关闭；QD_RUNTIME_INFO=<路径> 显式指定。
    本区域没装客户端时借用另一区域的桥（详见 `_realm_search_order`）。
    """
    return _runtime_info_resolution(realm)[0]


def runtime_info_bridge_realm(realm):
    """当前实际使用的桥来自哪个区域（'cn'/'intl'/'explicit'/''）。"""
    return _runtime_info_resolution(realm)[1]


# ---------------------------------------------------------------------------
# 机器身份固定（服务器没有官方风控桥时，对齐到装有客户端的机器）
# ---------------------------------------------------------------------------
# 身份是机器级、与区域无关的。服务器（Linux/Docker）装不了官方客户端，取不到
# 桥就只能用派生假值，设备定向活动可能被静默过滤；把真机器的身份"导出→固定"
# 过来即可完全对齐。来源优先级：QD_MACHINE_IDENTITY 环境变量 > 设置文件
# （accounts/settings.json 的 machine_identity，由看板「机器身份」区保存）。

def _settings_accounts_dir():
    return os.environ.get("ACCOUNTS_DIR") or os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "accounts")


def _pinned_machine_identity():
    """固定的机器身份（未固定/不完整返回 None）。"""
    data = None
    raw = (os.environ.get("QD_MACHINE_IDENTITY") or "").strip()
    if raw:
        try:
            data = json.loads(raw)
        except Exception:
            data = None
    if not isinstance(data, dict):
        try:
            data = qoder_settings.machine_identity(_settings_accounts_dir())
        except Exception:
            data = None
    if not isinstance(data, dict):
        return None
    token = str(data.get("machineToken") or "").strip()
    mtype = str(data.get("machineType") or "").strip()
    code = str(data.get("machineCode") or "").strip()
    if not (token and mtype and code):
        return None
    out = {"machineToken": token, "machineType": mtype, "machineCode": code}
    if isinstance(data.get("vmInfo"), dict):
        out["vmInfo"] = data["vmInfo"]
    return out


def export_bridge_identity(realm):
    """导出本机机器身份（给没有官方客户端的机器固定用）。

    已固定时直接返回固定值；否则调用官方风控桥取真值。
    返回 (identity|None, reason)。
    """
    pin = _pinned_machine_identity()
    if pin:
        out = dict(pin)
        out["source"] = "pinned"
        return out, ""
    exe = runtime_info_exe(realm)
    if not exe:
        return None, ("本机未找到官方风控桥 runtime-info.exe"
                      "（该导出需要在装有官方客户端的机器上操作）")
    data = run_runtime_info(realm, "")
    token = str(data.get("machineToken") or "").strip()
    mtype = str(data.get("machineType") or "").strip()
    code = str(data.get("machineCode") or "").strip()
    if not (token and mtype and code):
        return None, "风控桥调用失败或返回不完整，请稍后重试"
    out = {"machineToken": token, "machineType": mtype, "machineCode": code}
    if isinstance(data.get("vmInfo"), dict):
        out["vmInfo"] = data["vmInfo"]
    out["source"] = "runtime-info"
    return out, ""


def run_runtime_info(realm, account_id=""):
    """调用官方 runtime-info.exe，返回其 JSON（失败返回 {}）。

    account 为空串同样可用：机器身份是机器级的，活动平台之外（如虚拟化
    体检 _diag_campaign.py / 看板卡片）不需要账号上下文。
    """
    exe = runtime_info_exe(realm)
    if not exe:
        return {}
    try:
        import subprocess
        proc = subprocess.run(
            [exe, "prod", "--account-stdin"],
            input=json.dumps({"account": account_id or ""}).encode("utf-8") + b" ",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=25,
            cwd=os.path.dirname(exe))
        out = proc.stdout.decode("utf-8", "replace").strip()
        if out:
            return json.loads(out.split("\n", 1)[0])
    except Exception:
        pass
    return {}


_vm_cache = {}


def local_vm_status(realm=None, force=False):
    """本机虚拟化状态（**中文输出**）：看板与 _diag_campaign.py 共用。

    优先用官方风控桥的 vmInfo（官方客户端就是这么判的），桥不可用时退化为
    本机交叉校验（CPU 型号 / 系统制造商 / 虚拟化驱动文件）。结果缓存 300s。
    """
    r = realm if realm in ("cn", "intl") else "cn"
    now = time.time()
    hit = _vm_cache.get(r)
    if hit and not force and now - hit[0] < 300:
        return hit[1]
    data = run_runtime_info(r)
    vm_info = data.get("vmInfo") if isinstance(data.get("vmInfo"), dict) else {}
    st = vm_status(bridge_vm_info=vm_info, bridge_available=bool(runtime_info_exe(r)))
    st["realm"] = r
    st["bridge_available"] = bool(runtime_info_exe(r))
    st["bridge_realm"] = runtime_info_bridge_realm(r)
    _vm_cache[r] = (now, st)
    return st


def native_machine_identity(realm, account_id, force=False):
    """调用官方原生桥取真实机器身份；任何失败返回 {}（调用方回退派生值）。

    优先级：**固定身份（pinned）** > 官方风控桥（本区安装目录 → 本区 CLI
    缓存 → 借用另一区域，见 `_realm_search_order`）> 派生回退。固定身份来自
    QD_MACHINE_IDENTITY 环境变量或看板「机器身份」保存的设置（服务器对齐用）。

    身份是**机器级**的（实测不同 account id / 不同区域返回同一份），按区域
    缓存 NATIVE_IDENTITY_TTL 秒（长缓存；旧身份实测仍被服务端接受）；
    force=True 时跳过缓存重新取值（领取路径强制刷新，见 campaign_checkin）。
    """
    pin = _pinned_machine_identity()
    if pin:
        ident = dict(pin)
        vm_info = ident.get("vmInfo") if isinstance(ident.get("vmInfo"), dict) else {}
        ident["vm"] = bool(vm_info.get("isVm"))
        ident["vm_info"] = vm_info
        ident["source"] = "pinned"
        ident["bridge_realm"] = "pinned"
        return ident
    now = time.time()
    if not force:
        hit = _native_ident_cache.get(realm)
        if hit and now - hit[0] < NATIVE_IDENTITY_TTL:
            return hit[1]
    ident = {}
    data = run_runtime_info(realm, account_id)
    if data:
        token = str(data.get("machineToken") or "").strip()
        mtype = str(data.get("machineType") or "").strip()
        code = str(data.get("machineCode") or "").strip()
        vm_info = data.get("vmInfo") if isinstance(data.get("vmInfo"), dict) else {}
        if token and mtype and code:
            ident = {"machineToken": token, "machineType": mtype,
                     "machineCode": code,
                     "vm": bool(vm_info.get("isVm")),
                     "vm_info": vm_info,
                     "bridge_realm": runtime_info_bridge_realm(realm),
                     "source": "runtime-info"}
    _native_ident_cache[realm] = (now, ident)
    return ident

# 签到能力探测缓存：404（接口不存在）后 N 秒内不再重复探测，避免每次巡检都
# 打一个必然失败的请求；到期自动重探，官方上线即可自动恢复。
CHECKIN_PROBE_TTL = 6 * 3600
CHECKIN_REASON_NOT_FOUND = "checkin_endpoint_not_found"
# 账号文件写入锁：/tasks 面板接口会并行刷新同一账号的多个字段（credits/plan/…），
# 每个都可能触发 save()，同一 tmp 路径被两个线程同时写会落出坏 JSON。
_SAVE_LOCK = threading.Lock()
# 活动列表缓存 TTL：活动状态变化很慢（每日一轮），20 秒内复用可让看板切换视图
# /账号不再等那 1–4 秒的上游请求；领取动作会 force=True 绕过并立即失效缓存。
CAMPAIGNS_TTL = 20

# 会话死亡标记：上游主动吊销离线会话，刷新已无意义，需要重新登录。
SESSION_DEAD_MARKERS = ("TOKEN_EXPIRE", "12153", "Offline user session not found")


def session_dead(msg):
    s = str(msg or "")
    return any(m in s for m in SESSION_DEAD_MARKERS)


def get_realm_config(realm):
    return REALM_CONFIGS.get(realm) or REALM_CONFIGS["cn"]


def gateway_candidates(realm):
    """该区域的推理主机候选列表（官方客户端同款：主选 + 故障切换域名）。

    签名只覆盖 path，因此同一请求换主机后签名依旧有效。
    """
    cfg = get_realm_config(realm)
    out = [cfg["gateway"]]
    for host in cfg.get("gateway_fallbacks") or ():
        if host and host not in out:
            out.append(host)
    return out


def detect_realm_from_domain(domain):
    d = str(domain or "").lower()
    if "qoder.sh" in d or (d.endswith("qoder.com") and "qoder.com.cn" not in d) \
            or "qoder.com/" in d:
        return "intl"
    return "cn"


def normalize_epoch(value):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0
    if number > 1e11:      # 毫秒
        number /= 1000.0
    return int(number)


# ---------------------------------------------------------------------------
# 带重试的 HTTP JSON 工具
# ---------------------------------------------------------------------------
# 198.18.0.0/15 (RFC 2544 benchmarking) 与 fdfe:dcba:9876::/48 被 Clash/mihomo
# 等本地代理用作 fake-IP DNS 段：开启透明代理的机器上所有公网域名都会解析到
# 这些网段。命中它说明 DNS 已被本机代理接管、真实 IP 不可见，此时跳过解析级
# 校验（名称级校验已完成）。
_FAKEIP_NETS = [
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("2001:2::/48"),
    ipaddress.ip_network("fdfe:dcba:9876::/48"),
]


def _host_boundary_violation(ip, allow_local):
    """True when the resolved/IP address must be refused."""
    if ip.is_loopback:
        return not allow_local
    if (ip.is_private or ip.is_reserved or ip.is_link_local
            or ip.is_multicast or ip.is_unspecified):
        return True
    return False


def validate_public_http_url(url, allow_local=False):
    """SSRF 防护：仅允许 http/https，且 host 不得指向本机/私有/保留网段。

    上游网关与 openapi 域名均为公网地址；任何指向 localhost、回环、内网或
    保留地址的 URL 一律拒绝，防止上游配置或导入数据把请求引向内网。
    allow_local 仅供显式面向本机网关的开发/验证脚本开启（如 _verify_models.py），
    服务端请求路径一律使用默认 False。
    """
    parsed = urllib.parse.urlsplit(str(url or ""))
    if parsed.scheme not in ("http", "https"):
        raise ValueError("only http/https URLs are allowed")
    host = (parsed.hostname or "").strip().strip("[]").lower()
    if not host:
        raise ValueError("URL host is required")
    if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
        if not allow_local:
            raise ValueError("requests to localhost are not allowed")
        return url
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None:
        if _host_boundary_violation(literal, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % literal)
        return url
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError("cannot resolve URL host %r: %s" % (host, exc))
    addrs = []
    for info in infos:
        addr = str(info[4][0]).strip("[]")
        try:
            addrs.append(ipaddress.ip_address(addr))
        except ValueError:
            raise ValueError("URL host resolved to a non-IP address: %r" % addr)
    if addrs and all(any(ip in net for net in _FAKEIP_NETS) for ip in addrs):
        return url  # fake-IP DNS：真实 IP 不可见，名称级校验已通过
    for ip in addrs:
        if _host_boundary_violation(ip, allow_local):
            raise ValueError(
                "requests to private/reserved address %s are not allowed" % ip)
    return url


def _retryable(exc):
    """Transient network faults worth another attempt (TLS resets, timeouts, 5xx)."""
    if isinstance(exc, ssl.SSLError):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500
    if isinstance(exc, urllib.error.URLError):
        return True
    if isinstance(exc, (TimeoutError, ConnectionResetError, ConnectionAbortedError, OSError)):
        return True
    return False


def http_json(url, data=None, method=None, headers=None, timeout=30,
              retries=3, backoff=1.0, log=None):
    """urlopen + json decode with retries. 所有 openapi 调用统一走这里。"""
    validate_public_http_url(url)
    attempts = max(1, int(retries or 1))
    last = None
    for attempt in range(1, attempts + 1):
        req = urllib.request.Request(
            url,
            data=data,
            method=method or ("POST" if data is not None else "GET"),
            headers=headers or {},
        )
        try:
            with qoder_net.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            last = exc
            if attempt >= attempts or not _retryable(exc):
                break
            if log:
                log("network retry %d/%d after %s" % (attempt, attempts, exc))
            time.sleep(backoff * attempt)
    raise last


# ---------------------------------------------------------------------------
# Account
# ---------------------------------------------------------------------------
def round_note(items):
    """活动轮次说明：本轮截止时间 + 每日 10:00 开启新一轮。

    轮次不是自然日：官方每日领取按 10:00（UTC+8）滚动（本轮窗口
    10:00 ~ 次日 09:59）。凌晨~上午 10 点前点签到看到的"已领取"是
    **上一轮**的状态，此前文案写"今日已领取、明日再来"会把用户绕晕。
    """
    ends = []
    for c in items or []:
        try:
            e = int(c.get("end_at") or 0)
        except (TypeError, ValueError):
            e = 0
        if e:
            ends.append(e)
    if ends:
        nxt = max(ends)
        if nxt > time.time():
            return "本轮截止 %s（每日 10:00 开启新一轮）" % time.strftime(
                "%m-%d %H:%M", time.localtime(nxt))
    return "每日 10:00 开启新一轮"


def _is_daily_item(c):
    """是否为"每日领取 Credits"类活动（CLAIM_BENEFIT；空 action 视为可领）。"""
    return str(c.get("action_type") or "") in ("", "CLAIM_BENEFIT")


class Account(object):
    def __init__(self, data, path=None):
        data = data or {}
        self.path = path
        self.uid = str(data.get("uid") or "")
        self.nickname = str(data.get("nickname") or "")
        self.domain = str(data.get("domain") or "")
        self.realm = str(data.get("realm") or detect_realm_from_domain(self.domain))
        if self.realm not in REALM_CONFIGS:
            self.realm = "cn"
        if not self.domain:
            self.domain = get_realm_config(self.realm)["domain"]
        self.platform = str(data.get("platform") or "CLI")
        self.access_token = str(data.get("accessToken") or "")
        self.refresh_token = str(data.get("refreshToken") or "")
        self.personal_token = str(data.get("personalToken") or "")
        self.expires_at = normalize_epoch(data.get("expiresAt"))
        self.added_at = data.get("addedAt") or time.time()
        self.source = str(data.get("source") or "oauth")
        self.enabled = data.get("enabled", True)
        self.last_error = str(data.get("lastError") or "")
        self.cooldown_until = float(data.get("cooldownUntil") or 0)
        # 调度状态（仅内存）：在途请求数（least-busy 平手裁决，响应关闭时释放）
        # 与账号侧连续失败数（401/403 等退避用，成功一次清零）
        self.in_flight = 0
        self.consecutive_failures = 0
        # 按模型粒度的限流冷却：上游频控只针对单模型，不能拖垮整个账号。
        self.model_cooldowns = {}
        self.credits = data.get("credits") or None
        self.plan = str(data.get("plan") or "")
        self.last_checkin = data.get("lastCheckin") or None
        self.user_type = str(data.get("userType") or "") or DEFAULT_USER_TYPE
        self.organization_id = str(data.get("organizationId") or "")
        self.organization_name = str(data.get("organizationName") or "")
        # 签到能力：None=未探测 / True=接口存在 / False=接口不存在（404）。
        # 运行时探测而非按区域硬编码——官方随时可能在任一区域增删活动接口。
        self._checkin_cap = None
        self._checkin_cap_reason = ""
        self._checkin_cap_at = 0.0
        # 最近一次 campaign 平台状态快照（/sash/api/v1/me/campaigns）
        self.campaign_status = None
        # 活动列表短缓存 (at, payload)：该请求约 1–4 秒（上游最慢的一环），
        # 看板切换视图/账号会连续取，缓存后由"领取动作"显式失效。
        self._campaigns_cache = None
        # 活动平台用的机器身份来源：native(官方原生桥) / derived(派生回退)
        self.machine_identity_source = "derived"

    # -- 持久化 ------------------------------------------------------------
    def to_dict(self):
        return {
            "uid": self.uid,
            "nickname": self.nickname,
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "accessToken": self.access_token,
            "refreshToken": self.refresh_token,
            "personalToken": self.personal_token,
            "expiresAt": self.expires_at,
            "addedAt": self.added_at,
            "source": self.source,
            "enabled": self.enabled,
            "lastError": self.last_error,
            "cooldownUntil": self.cooldown_until,
            "credits": self.credits,
            "plan": self.plan,
            "lastCheckin": self.last_checkin,
            "userType": self.user_type,
            "organizationId": self.organization_id,
            "organizationName": self.organization_name,
        }

    def public(self):
        exp = self.expires_at
        return {
            "uid": self.uid,
            "nickname": self.nickname or (self.uid[:8] if self.uid else "?"),
            "domain": self.domain,
            "realm": self.realm,
            "platform": self.platform,
            "enabled": bool(self.enabled),
            "source": self.source,
            "tokenFamily": token_family(self),
            "expiresAt": exp,
            "expiresIn": _human_delta(exp - time.time()) if exp else None,
            "hasRefreshToken": bool(self.refresh_token),
            "hasPAT": bool(self.personal_token),
            "lastError": self.last_error,
            "inFlight": getattr(self, "in_flight", 0),
            "consecutiveFailures": getattr(self, "consecutive_failures", 0),
            "inCooldown": self.cooldown_until > time.time(),
            "cooldownFor": round(max(0.0, self.cooldown_until - time.time())) or None,
            "addedAt": self.added_at,
            "file": os.path.basename(self.path) if self.path else None,
            "credits": self.credits,
            "plan": self.plan,
            "lastCheckin": self.last_checkin,
            # 运行时探测：None=未探测（照常尝试）/ True / False（本区域无接口）
            "canCheckin": self.can_checkin(),
            "checkinCapability": ("unknown" if self.checkin_capability()[0] is None
                                  else ("available" if self.checkin_capability()[0]
                                        else "not_found")),
            "checkinReason": self.checkin_capability()[1],
            "userType": self.user_type,
            "machineId": derive_id(self.uid, "machine"),
            "sessionId": derive_id(self.uid, "session"),
        }

    def save(self, directory):
        base = Path(directory).resolve()
        base.mkdir(parents=True, exist_ok=True)
        safe_uid = re.sub(r"[^A-Za-z0-9_-]", "_", str(self.uid or "")).strip("_ ")
        name = (safe_uid or uuid.uuid4().hex) + ".json"
        path = base / name
        tmp = base / (name + ".tmp")
        if not (path.is_relative_to(base) and tmp.is_relative_to(base)):
            raise ValueError("invalid path for account save")
        # 并发保护：/tasks 面板接口会并行刷新同一账号的多个字段，每个都可能
        # 触发 save；同一 tmp 路径被两个线程同时写会落出坏 JSON。
        with _SAVE_LOCK:
            tmp.write_text(json.dumps(self.to_dict(), ensure_ascii=False,
                                      indent=2),
                           encoding="utf-8")
            os.replace(tmp, path)
        self.path = str(path)
        return self.path

    def delete(self):
        if self.path and os.path.exists(self.path):
            os.remove(self.path)

    # -- 健康与冷却 --------------------------------------------------------
    def ready(self, model=None):
        if not self.enabled or not self.access_token:
            return False
        if self.cooldown_until > time.time():
            return False
        if model and self.model_cooldowns.get(model, 0.0) > time.time():
            return False
        exp = self.expires_at
        if not exp:
            return True
        remaining = exp - time.time()
        if remaining > 240:          # 剩余 >4 分钟直接用（jt- 24h / dt- 30d）
            return True
        if remaining > 0:
            self.refresh()
            return True
        return self.refresh()

    def quota_depleted(self):
        """额度快照是否已知耗尽——调度时降为后备档（不排除使用）。

        只有明确的信号才算：isQuotaExceeded=True，或"总额度>0 且余额=0"。
        快照可能过期（官方额度刷新/签到到账后有延迟），所以这只是一个
        排序偏好：后备档在主档全部不可用时照常顶上，不会把账号判死。
        """
        c = self.credits or {}
        if not c:
            return False
        if c.get("exceeded") is True:
            return True
        try:
            remain = float(c.get("remain") or 0)
            size = float(c.get("size") or 0)
        except (TypeError, ValueError):
            return False
        return remain <= 0 and size > 0

    def note_error(self, message, cooldown=60, single_account=False, model=None,
                   until=None, escalate=False):
        self.last_error = str(message)[:200]
        if model:
            wait = max(1.0, float(until) - time.time()) if until else (
                3.0 if single_account else float(cooldown))
            self.model_cooldowns[model] = time.time() + wait
            return
        actual_cooldown = 3 if single_account else cooldown
        if escalate and not single_account:
            # 账号侧连续失败（如 401/403 凭证被拒）：冷却按 60s→5min→15min→
            # 30min 阶梯退避，避免坏账号每轮冷却后被反复选中白烧请求；
            # 任意一次成功（clear_error）即清零。上游瞬时故障不在此列——
            # 那是上游的锅，不该罚账号（调用方按 escalate 显式区分）。
            self.consecutive_failures = getattr(self, "consecutive_failures", 0) + 1
            ladder = (60, 300, 900, 1800)
            actual_cooldown = max(
                actual_cooldown,
                ladder[min(self.consecutive_failures - 1, len(ladder) - 1)])
        self.cooldown_until = time.time() + actual_cooldown

    def throttle_wait(self, model=None):
        """Seconds until this account can serve `model` again (0 = right now)."""
        if not self.enabled or not self.access_token:
            return 0.0
        now = time.time()
        wait = max(0.0, self.cooldown_until - now)
        if model:
            wait = max(wait, max(0.0, self.model_cooldowns.get(model, 0.0) - now))
        return wait

    def clear_error(self, model=None):
        if model:
            self.model_cooldowns.pop(model, None)
        else:
            self.model_cooldowns.clear()
        self.consecutive_failures = 0
        if self.last_error or self.cooldown_until:
            self.last_error = ""
            self.cooldown_until = 0

    # -- 出站头 ------------------------------------------------------------
    def headers(self, purpose="openapi"):
        cfg = get_realm_config(self.realm)
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/plain, */*",
            "User-Agent": CLIENT_UA,
            "Authorization": "Bearer " + self.access_token,
            "X-Request-ID": generate_request_id(self.uid),
            "X-Machine-ID": derive_id(self.uid, "machine"),
            "X-Session-ID": derive_id(self.uid, "session"),
            "Origin": cfg["website"],
            "Referer": cfg["website"] + "/",
        }

    def desktop_headers(self):
        """桌面端 0.4.3 同款出站头（活动平台 /sash/... 必需）。

        官方桌面端调用 `/sash/api/v1/me/campaigns` 时携带：
          Authorization: Bearer <token>
          User-Agent: Qoder
          Cosy-ClientType: 10（桌面端；CLI 是 5、QoderWork 是 6）
          Cosy-Version: <桌面端版本>
          Cosy-MachineOS / MachineHostname / MachineId / MachineToken /
          MachineType / MachineCode

        两层坑（都已踩过）：
          1. 缺这些头 → 服务端不报错但返回**空活动列表**；
          2. 机器身份用派生假值 → 列表里**静默少掉设备定向活动**
             （"每日领取 100 Credits"），只有官方原生桥取到的真身份才完整。
        因此这里优先用 runtime-info.exe 的真值，失败才回退稳定派生值。
        """
        h = dict(self.headers())
        h["User-Agent"] = "Qoder"
        h["cosy-clienttype"] = DESKTOP_CLIENT_TYPE
        h["cosy-version"] = desktop_version()
        ident = native_machine_identity(self.realm, self.uid)
        h["cosy-machineid"] = derive_id(self.uid, "machine")
        h["cosy-machinetoken"] = ident.get("machineToken") or \
            derive_machine_token(self.uid)
        h["cosy-machinetype"] = ident.get("machineType") or \
            derive_machine_type(self.uid)
        h["cosy-machinecode"] = ident.get("machineCode") or \
            derive_id(self.uid, "machinecode")
        h["cosy-machineos"] = MACHINE_OS
        h["cosy-machinehostname"] = MACHINE_HOSTNAME
        self.machine_identity_source = ident.get("source") or "derived"
        return h

    # -- 刷新（按 token 前缀路由） ----------------------------------------
    def refresh(self):
        """刷新 access token。drt- 走 deviceToken，jrt-/PAT 走 jobToken。

        PAT 永不覆盖活跃的 OAuth 会话，只做 jrt- 过期后的最终兜底。
        """
        cfg = get_realm_config(self.realm)
        base = cfg["openapi"]
        # 1) OAuth 设备族
        if self.refresh_token.startswith("drt-"):
            return self._post_token(base + PATH_DEVICE_REFRESH,
                                    {"refresh_token": self.refresh_token}, kind="device")
        # 2) jobToken 族：jrt- 优先，失败回落 PAT 重新交换
        if self.refresh_token:
            if self._post_token(base + PATH_JOB_REFRESH,
                                {"refresh_token": self.refresh_token}, kind="job"):
                return True
        if self.personal_token:
            if self._post_token(base + PATH_JOB_EXCHANGE,
                                {"personal_token": self.personal_token}, kind="job"):
                return True
        if not self.refresh_token and not self.personal_token:
            self.last_error = "no refresh token; sign in again"
        return False

    def _post_token(self, url, payload, kind):
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": CLIENT_UA,
        }
        try:
            data = http_json(url, data=json.dumps(payload).encode(), method="POST",
                             headers=headers, timeout=30)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            self.last_error = "refresh failed: HTTP %d %s" % (exc.code, body[:160])
            if exc.code in (401, 403) and session_dead(body):
                self.enabled = False
                self.last_error = "session dead (TOKEN_EXPIRE): re-login required"
            return False
        except Exception as exc:
            self.last_error = "refresh failed: %s" % exc
            return False

        if kind == "device":
            token = data.get("token") or data.get("device_token") or ""
            refresh = data.get("refresh_token") or self.refresh_token
            exp = _device_expiry(data)
        else:
            token = data.get("token") or ""
            refresh = data.get("refresh_token") or self.refresh_token
            if data.get("expires_in"):
                exp = int(time.time() + int(data["expires_in"]) / 1000)
            else:
                exp = self.expires_at
        if not token:
            self.last_error = "refresh returned no token"
            return False
        self.access_token = token
        self.refresh_token = refresh
        self.expires_at = exp or self.expires_at
        self.last_error = ""
        self.cooldown_until = 0
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        try:
            from qoder_sign import SESSIONS
            SESSIONS.invalidate(self.uid)   # 旧 COSY 会话携带旧 token，必须重建
        except Exception:
            pass
        return True

    # -- 签到 / 额度 / 套餐 ------------------------------------------------
    def _mark_checkin_capability(self, available, reason=""):
        self._checkin_cap = bool(available)
        self._checkin_cap_reason = reason or ""
        self._checkin_cap_at = time.time()

    def checkin_capability(self):
        """签到能力（运行时探测结果）。

        None  = 尚未探测（调用方应实际尝试一次）
        True  = 本账号所在区域存在 /daily-check-in 接口
        False = 接口不存在（404/405/410，实测国际版即如此）——缓存 TTL 内跳过
        """
        if self._checkin_cap is None:
            return None, ""
        if time.time() - self._checkin_cap_at > CHECKIN_PROBE_TTL:
            return None, self._checkin_cap_reason
        return self._checkin_cap, self._checkin_cap_reason

    def can_checkin(self):
        """今日是否还需要签到（能力由运行时探测，不再按区域硬编码）。"""
        capable, _ = self.checkin_capability()
        if capable is False:
            return False
        if not self.last_checkin:
            return True
        today_str = time.strftime("%Y-%m-%d")
        return not str(self.last_checkin).startswith(today_str)

    def checkin_status(self):
        """GET daily-check-in/status -> (ok, summary|error)。

        接口在本区域不存在时返回 (False, {"unavailable": True, reason:...})，
        并记录能力探测结果（活动上线后 TTL 到期会自动重探）。
        """
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_CHECKIN_STATUS
        try:
            q = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=2)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if exc.code in (404, 405, 410):
                self._mark_checkin_capability(
                    False, "%s (HTTP %d)" % (CHECKIN_REASON_NOT_FOUND, exc.code))
                return False, {
                    "unavailable": True,
                    "reason": CHECKIN_REASON_NOT_FOUND,
                    "http": exc.code,
                    "error": "HTTP %d %s" % (exc.code, body[:160]),
                }
            return False, {"unavailable": False,
                           "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return False, {"unavailable": False, "error": str(exc)}
        self._mark_checkin_capability(True, "")
        last = ""
        if q.get("lastClaimedAt"):
            try:
                last = time.strftime("%Y-%m-%d",
                                     time.localtime(int(q["lastClaimedAt"])))
            except Exception:
                last = ""
        today = time.strftime("%Y-%m-%d")
        status = str(q.get("status") or "")
        return True, {
            "status": status,
            "active": status in ("CLAIMABLE", "CLAIMED"),
            "today_checked_in": status == "CLAIMED" and last == today,
            "streak_days": int(q.get("currentStreakDays") or 0),
            "total_claim_days": int(q.get("totalClaimDays") or 0),
            "reward_credits": int(q.get("rewardCredits") or 0),
            "total_reward_credits": int(q.get("totalRewardCredits") or 0),
            "next_claim_at": int(q.get("nextClaimAt") or 0),
            "last_claimed_at": int(q.get("lastClaimedAt") or 0),
            "reward_expires_at": int(q.get("rewardExpiresAt") or 0),
        }

    def checkin(self):
        """每日签到：先查状态，未签则领取。返回 {ok, msg, ...}。

        不再按区域门控：接口不存在（国际版 404）按"本区域无此接口"跳过并给
        出明确原因，而不是静默什么都不做（历史问题：看板点签到毫无反应）。
        """
        ok, st = self.checkin_status()
        if not ok:
            if st.get("unavailable"):
                # 旧接口不存在（如国际版 404）：改走活动平台直领
                res = self._campaign_checkin_result()
                if res is not None:
                    return res
                return {"ok": True, "unavailable": True,
                        "reason": st.get("reason"),
                        "msg": "本区域未开放 /sash/api/v1/me/daily-check-in 接口"
                               "（HTTP %s）：每日领取活动改由官方客户端承接"
                               % st.get("http")}
            return {"ok": False, "error": st.get("error") or str(st)}
        if st["today_checked_in"]:
            return {"ok": True, "already": True, "msg": "今日已签到",
                    "streak_days": st["streak_days"], "reward_credits": st["reward_credits"]}
        if not st["active"]:
            # CLAIMABLE / CLAIMED 之外的状态（如 DISABLED：旧批次下线）：
            # 不硬领旧接口，改走活动平台直领（"每日领取 100 Credits"等新活动）。
            res = self._campaign_checkin_result()
            if res is not None:
                return res
            return {"ok": True, "disabled": True, "status": st.get("status"),
                    "msg": "官方签到活动未开放 (status=%s)" % (st.get("status") or "?")}
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_CHECKIN_CLAIM
        try:
            res = http_json(url, data=b"{}", method="POST",
                            headers=self.headers(), timeout=15, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            # 上游并发/重复领取返回 409 ALREADY_CLAIMED —— 归一化为“已签”
            if "ALREADY_CLAIMED" in body:
                self._stamp_checkin()
                return {"ok": True, "already": True, "msg": "今日已签到"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if str(res.get("result") or "") == "ALREADY_CLAIMED" or res.get("success") is False and "ALREADY" in str(res.get("error") or ""):
            self._stamp_checkin()
            return {"ok": True, "already": True, "msg": "今日已签到"}
        if res.get("success") is False:
            # 复查一次：上游可能已记账
            ok2, st2 = self.checkin_status()
            if ok2 and st2["today_checked_in"]:
                self._stamp_checkin()
                return {"ok": True, "already": True, "msg": "今日已签到（复查确认）"}
            return {"ok": False, "error": str(res.get("error") or res)[:160]}
        reward = int(res.get("rewardCredits") or 0)
        self._stamp_checkin()
        return {"ok": True, "msg": "签到成功 +%d 积分" % reward,
                "reward_credits": reward}

    def _campaigns_get(self):
        """一次活动列表请求（桌面端头 → 401/403 回退普通头）。

        返回 (payload|None, http_code, error_str)；payload 为服务端 JSON。
        """
        url = get_realm_config(self.realm)["openapi"] + PATH_CAMPAIGNS
        try:
            return http_json(url, method="GET", headers=self.desktop_headers(),
                             timeout=15, retries=1), 200, ""
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            # 桌面端头被拒（401/403）时回退普通头，至少保留"能不能看到"的信息
            if exc.code in (401, 403):
                try:
                    return http_json(url, method="GET", headers=self.headers(),
                                     timeout=15, retries=1), 200, ""
                except urllib.error.HTTPError as exc2:
                    return None, exc2.code, ("HTTP %d (desktop) / HTTP %d (plain)"
                                             % (exc.code, exc2.code))
                except Exception as exc2:
                    return None, exc.code, "HTTP %d (desktop) / %s (plain)" % (exc.code, exc2)
            return None, exc.code, "HTTP %d %s" % (exc.code, body[:160])
        except Exception as exc:
            return None, 0, str(exc)

    def campaigns(self, force=False):
        """GET /sash/api/v1/me/campaigns -> 官方活动平台状态（双区域通用）。

        官方把"每日领取 100 Credits"等限时活动搬到了 campaign 平台，取列表要
        **两层都对**：① 桌面端请求头（`desktop_headers()`）；② 服务端认可的
        **真实机器身份**（原生桥取，见 `native_machine_identity`）。两者任一
        不对都表现为 HTTP 200 + 列表里少活动（不报错），这正是"领不到"的根因。

        机器身份会轮换：若本次 `showCampaign=false`（通常意味着身份被判定为
        非官方客户端），强制刷新一次身份并重试，避免缓存过期导致整天领不到。
        结果短缓存 CAMPAIGNS_TTL 秒（force=True 绕过）——该请求是上游最慢的
        一环，看板切换视图时不该重复等它。

        返回 {ok, available, show_campaign, claimable, campaign_url, campaigns,
              identity}，每条活动含 action_type / claim_status / benefit / end_at。
        """
        now = time.time()
        if not force and self._campaigns_cache \
                and now - self._campaigns_cache[0] < CAMPAIGNS_TTL:
            return self._campaigns_cache[1]
        q, code, err = self._campaigns_get()
        # 身份被判定为非官方客户端时列表会被静默过滤：既可能整体不显示
        # （showCampaign=false），也可能只把"每日领取"这类设备定向活动滤掉
        # （列表里没有任何 CLAIM_BENEFIT 项）。两种形态都换一次新身份重试。
        if isinstance(q, dict) and self._campaigns_needs_identity_retry(q) \
                and getattr(self, "machine_identity_source", "") == "native":
            native_machine_identity(self.realm, self.uid, force=True)
            q2, code2, err2 = self._campaigns_get()
            if isinstance(q2, dict) \
                    and (q2.get("showCampaign") or self._raw_has_daily(q2)):
                q, code, err = q2, code2, err2
        if not isinstance(q, dict):
            return {"ok": False, "available": code not in (404, 405, 410),
                    "error": err or ("HTTP %d" % code)}
        items = []
        raw = q.get("campaigns")
        for c in (raw if isinstance(raw, list) else []):
            if not isinstance(c, dict):
                continue
            placements = c.get("placements")
            benefit = c.get("benefit") if isinstance(c.get("benefit"), dict) else {}
            items.append({
                "campaign_id": str(c.get("campaignId") or c.get("campaign_id") or ""),
                "campaign_key": str(c.get("campaignKey") or c.get("campaign_key") or ""),
                "action_type": str(c.get("actionType") or c.get("action_type") or ""),
                "claim_status": str(c.get("claimStatus") or c.get("claim_status") or ""),
                "start_at": normalize_epoch(c.get("startAt") or c.get("start_at")),
                "end_at": normalize_epoch(c.get("endAt") or c.get("end_at")),
                "benefit": {
                    "kind": str(benefit.get("kind") or ""),
                    "amount": int(benefit.get("amount") or 0),
                },
                "required_achievement_key": str(
                    c.get("requiredAchievementKey") or ""),
                "achievement_completed": bool(c.get("achievementCompleted")),
                "unavailable_reason": str(c.get("unavailableReason") or ""),
                "placements": placements if isinstance(placements, list) else [],
            })
        st = {
            "ok": True,
            "available": True,
            "show_campaign": bool(q.get("showCampaign")),
            "claimable": bool(q.get("claimable")),
            "campaign_url": str(q.get("campaignUrl") or ""),
            "campaigns": items,
            # 机器身份来源：derived 时设备定向活动可能被服务端过滤（列表偏少）
            "identity": getattr(self, "machine_identity_source", "derived"),
        }
        self.campaign_status = st
        self._campaigns_cache = (time.time(), st)
        return st

    def campaign_reward(self, campaign_id):
        """GET …/campaigns/{id}/reward -> 该活动的发放状态（幂等，只读）。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + (PATH_CAMPAIGN_REWARD % campaign_id)
        try:
            return http_json(url, method="GET", headers=self.desktop_headers(),
                             timeout=20, retries=1)
        except Exception as exc:
            return {"error": str(exc)}

    def _campaigns_needs_identity_retry(self, q):
        """列表疑似被身份过滤：整体隐藏，或没有任何每日领取项。"""
        if not q.get("showCampaign"):
            return True
        return not self._raw_has_daily(q)

    @staticmethod
    def _raw_has_daily(q):
        raw = q.get("campaigns") if isinstance(q, dict) else None
        for c in (raw if isinstance(raw, list) else []):
            if not isinstance(c, dict):
                continue
            action = str(c.get("actionType") or c.get("action_type") or "")
            if action in ("", "CLAIM_BENEFIT"):
                return True
        return False

    def claim_campaign(self, campaign_id):
        """POST …/campaigns/{id}/claim -> 领取该活动奖励（官方幂等语义）。

        返回 {ok, status, replayed, failure_code, grant_id, amount, message}。
        服务端按"人"去重（同一机器指纹下的多账号合并为一人，实测）：
          - 已领取 -> status=CLAIMED + replayed=true（幂等，不重复发放）；
          - 同人已领 -> status=BLOCKED + failureCode=SAME_PERSON_ALREADY_CLAIMED
            （同机多号共享每轮一次的额度，第二个号会被 BLOCKED 且列表里
            隐藏该活动——官方文档写"每账号"，实际执行是"每人"）。
        """
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + (PATH_CAMPAIGN_CLAIM % campaign_id)
        try:
            r = http_json(url, data=b"{}", method="POST",
                          headers=self.desktop_headers(), timeout=20, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            code = ""
            try:
                code = str((json.loads(body) or {}).get("errorCode") or "")
            except Exception:
                code = ""
            if exc.code == 409 or code.upper() in ("ALREADY_CLAIMED", "REPLAYED"):
                return {"ok": True, "status": "CLAIMED", "replayed": True,
                        "message": "今日已领取（上游幂等确认）"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        status = str(r.get("status") or "").upper()
        failure = str(r.get("failureCode") or "").upper()
        if failure == "SAME_PERSON_ALREADY_CLAIMED" or status == "BLOCKED":
            return {"ok": False, "blocked": True, "status": status or "BLOCKED",
                    "replayed": False, "failure_code": failure or "BLOCKED",
                    "amount": int(((r.get("benefit") or {})
                                   if isinstance(r.get("benefit"), dict)
                                   else {}).get("amount") or 0),
                    "message": "同人已领取（同一设备/身份下其他账号本轮已领，"
                               "服务端按人去重）",
                    "raw": r}
        if not status and r.get("success") is False:
            return {"ok": False, "error": str(r.get("error") or r)[:160]}
        return {
            "ok": status in ("CLAIMED", "GRANTED", "SUCCESS"),
            "status": status,
            "replayed": bool(r.get("replayed")),
            "failure_code": failure or "",
            "grant_id": str(r.get("grantId") or ""),
            "amount": int(((r.get("benefit") or {}) if isinstance(r.get("benefit"), dict)
                           else {}).get("amount") or r.get("amount") or 0),
            "message": "已领取" if r.get("replayed") else "领取成功",
            "raw": r,
        }

    def campaign_checkin(self, gap=0.5):
        """活动平台签到：领取所有 CLAIMABLE 的 Credits 活动（每日 100 等）。

        先强制刷新原生机器身份（身份会轮换，缓存过期会让列表被过滤 → 漏领），
        再列活动、逐个领取。多账户场景下每个账号独立走这一遍。

        返回 {ok, claimed:[...], already:[...], earned, message, campaigns}
          - 已是 CLAIMED 的活动计入 already（"今日已领取"）
          - 无可领取项且没有任何活动 -> ok=True + message 说明
        """
        native_machine_identity(self.realm, self.uid, force=True)
        st = self.campaigns(force=True)      # 领取路径必须绕过缓存，看最新状态
        if not st.get("ok"):
            return {"ok": False, "error": st.get("error") or "campaigns 查询失败",
                    "earned": 0, "claimed": [], "already": [], "blocked": []}
        claimed, already, earned, errors, blocked = [], [], 0, [], []
        for c in st["campaigns"]:
            if c["claim_status"] == "CLAIMED":
                already.append(c)
                continue
            if c["claim_status"] != "CLAIMABLE":
                continue
            if c["action_type"] and c["action_type"] != "CLAIM_BENEFIT":
                continue      # VIEW_DETAILS 类活动无需（也不能）领取
            res = self.claim_campaign(c["campaign_id"])
            if res.get("ok"):
                amount = res.get("amount") or c["benefit"]["amount"] or 0
                self._stamp_checkin()
                if res.get("replayed"):
                    already.append(c)
                else:
                    claimed.append(c)
                    earned += int(amount or 0)
                time.sleep(max(0.0, gap))
            elif res.get("blocked"):
                # 服务端按"人"去重：同机器/同身份下其他账号本轮已领
                blocked.append({"campaign": c["campaign_key"] or c["campaign_id"],
                                "failure_code": res.get("failure_code")})
            else:
                errors.append("%s: %s" % (c["campaign_key"] or c["campaign_id"],
                                          res.get("error")))
        daily_already = [c for c in already if _is_daily_item(c)]
        if claimed:
            msg = "活动领取成功 +%d Credits（%s）；%s" % (
                earned, ", ".join(c["campaign_key"] or c["campaign_id"]
                                  for c in claimed), round_note(claimed))
        elif blocked:
            msg = ("同人已领取：同一设备/身份下的其他账号本轮已领过（服务端按人去重，"
                   "failureCode=%s）" % blocked[0].get("failure_code"))
        elif daily_already:
            msg = "本轮奖励已领取（%s）；%s" % (
                ", ".join(c["campaign_key"] or c["campaign_id"]
                          for c in daily_already),
                round_note(daily_already))
        elif errors:
            msg = "活动领取失败：%s" % "; ".join(errors)[:200]
        else:
            ident = getattr(self, "machine_identity_source", "derived")
            if ident == "derived":
                msg = ("服务端未给该账号下发每日领取活动（当前用的是**派生假身份**，"
                       "设备定向活动可能被静默过滤——在看板「设置 → 机器身份」"
                       "固定真身份后重试）")
            else:
                msg = ("服务端未给该账号下发每日领取活动（账号未在活动定向内；"
                       "常见原因见 README「活动与新人权益规则」）")
        # 领取动作会改变活动状态：让下一次列表查询重新拉取（不吃 20s 缓存）
        self._campaigns_cache = None
        return {"ok": not errors, "claimed": claimed, "already": already,
                "blocked": blocked, "earned": earned, "message": msg,
                "campaigns": st["campaigns"], "errors": errors}

    def _campaign_checkin_result(self):
        """旧签到接口不可用/停用时改走活动平台（checkin() 的兜底分支）。

        看板「每日签到」/账号行「签到」按钮走的都是 /accounts/checkin →
        checkin()；官方现行机制在活动平台，这里把 campaign_checkin() 的
        结果翻译成 checkin() 的返回契约。当前账号没有可领/可报的活动时返回
        None，调用方回退到旧接口的说明文案。
        """
        camp = self.campaign_checkin()
        if camp.get("claimed"):
            return {"ok": True, "campaign": True,
                    "reward_credits": int(camp.get("earned") or 0),
                    "msg": camp.get("message")}
        if camp.get("blocked"):
            # 同人去重不算失败：本轮该"人"的额度已被其他账号领走
            return {"ok": True, "campaign": True, "blocked": True,
                    "msg": camp.get("message")}
        if camp.get("already"):
            return {"ok": True, "campaign": True, "already": True,
                    "msg": camp.get("message")}
        if camp.get("errors"):
            return {"ok": False, "campaign": True,
                    "error": "；".join(camp.get("errors") or [])[:200]}
        return None

    def _stamp_checkin(self):
        self.last_checkin = time.strftime("%Y-%m-%d %H:%M:%S")
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))

    def fetch_credits(self):
        """GET /api/v2/quota/usage -> 聚合基础额度 + 赠送/签到额度。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_QUOTA
        try:
            q = http_json(url, method="GET", headers=self.headers(), timeout=30)
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        uq = q.get("userQuota") or {}
        aq = q.get("addOnQuota") or {}

        def _num(d, k):
            try:
                return float(d.get(k) or 0)
            except Exception:
                return 0.0

        remain = int(_num(uq, "remaining") + _num(aq, "remaining"))
        used = int(_num(uq, "used") + _num(aq, "used"))
        size = int(_num(uq, "total") + _num(aq, "total"))
        self.credits = {
            "remain": remain,
            "used": used,
            "size": size,
            "exceeded": bool(q.get("isQuotaExceeded")),
            "usage_pct": q.get("totalUsagePercentage"),
            "expires_at": normalize_epoch(q.get("expiresAt")),
            "packages": [
                {"name": "基础额度", "remain": int(_num(uq, "remaining")),
                 "used": int(_num(uq, "used")), "size": int(_num(uq, "total"))},
                {"name": "赠送/签到额度", "remain": int(_num(aq, "remaining")),
                 "used": int(_num(aq, "used")), "size": int(_num(aq, "total"))},
            ],
            "updated_at": time.time(),
            "updated_iso": time.strftime("%Y-%m-%d %H:%M:%S"),
        }
        if self.path and os.path.exists(os.path.dirname(self.path)):
            self.save(os.path.dirname(self.path))
        return {"ok": True, "credits": self.credits}

    def fetch_plan(self):
        """GET /api/v2/user/plan -> 套餐名（Pro Trial 等）。"""
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PLAN
        try:
            p = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=1)
        except Exception:
            return self.plan
        name = str(p.get("plan_tier_name") or p.get("user_type") or "")
        if name and name != self.plan:
            self.plan = name
            if self.path and os.path.exists(os.path.dirname(self.path)):
                self.save(os.path.dirname(self.path))
        return name

    # -- Pro 升级包（一次性 +1800） ---------------------------------------
    def pro_eligibility(self):
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PRO_ELIGIBILITY
        try:
            m = http_json(url, method="GET", headers=self.headers(), timeout=15,
                          retries=1)
            return True, bool(m.get("eligible"))
        except urllib.error.HTTPError as exc:
            if exc.code in (404, 403, 410):
                # 端点不存在 / 活动已下线：查询成功，只是不可领取
                return True, False
            return False, "HTTP %d" % exc.code
        except Exception as exc:
            return False, str(exc)

    def pro_claim(self):
        cfg = get_realm_config(self.realm)
        url = cfg["openapi"] + PATH_PRO_CLAIM
        try:
            m = http_json(url, data=b"{}", method="POST", headers=self.headers(),
                          timeout=15, retries=1)
        except urllib.error.HTTPError as exc:
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            if exc.code == 409 or "ALREADY" in body:
                return {"ok": True, "already": True, "msg": "Pro 升级包已领取过"}
            return {"ok": False, "error": "HTTP %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
        if m.get("success") is False:
            return {"ok": False, "error": str(m.get("message") or m)[:160]}
        return {"ok": True, "msg": "Pro 升级包领取成功", "data": m}


def token_family(acc):
    """返回账号当前的凭证族：device(OAuth) / job(PAT交换) / pat。"""
    rt = acc.refresh_token or ""
    if rt.startswith("drt-"):
        return "device"
    if rt.startswith("jrt-"):
        return "job"
    if (acc.access_token or "").startswith("pt-"):
        return "pat"
    return "unknown"


def _device_expiry(data):
    """deviceToken 响应的过期时间：expires_in(ms) / expires_at(RFC3339)，默认 30 天。"""
    if data.get("expires_in"):
        return int(time.time() + int(data["expires_in"]) / 1000)
    if data.get("expires_at"):
        try:
            import datetime
            dt = datetime.datetime.strptime(str(data["expires_at"])[:19],
                                             "%Y-%m-%dT%H:%M:%S")
            return int(dt.timestamp())
        except Exception:
            pass
    return int(time.time()) + 30 * 86400


def _human_delta(seconds):
    if seconds is None:
        return None
    if seconds <= 0:
        return "expired"
    days = seconds / 86400.0
    if days >= 1:
        return "%.0f days" % days
    hours = seconds / 3600.0
    if hours >= 1:
        return "%.1f hours" % hours
    return "%d min" % int(seconds / 60)


# ---------------------------------------------------------------------------
# 会话亲和（同一对话固定同一账号，上游按账号缓存 prompt prefix）
# ---------------------------------------------------------------------------
class SessionAffinity(object):
    def __init__(self, ttl=7200, max_entries=5000):
        self.ttl = ttl
        self.max_entries = max_entries
        self.bindings = {}
        self._lock = threading.Lock()

    def get(self, key):
        if not key:
            return None
        with self._lock:
            entry = self.bindings.get(key)
            if not entry:
                return None
            uid, exp = entry
            if time.time() > exp:
                self.bindings.pop(key, None)
                return None
            self.bindings[key] = (uid, time.time() + self.ttl)
            return uid

    def bind(self, key, uid):
        if not key or not uid:
            return
        with self._lock:
            if len(self.bindings) >= self.max_entries:
                now = time.time()
                self.bindings = {k: v for k, v in self.bindings.items() if v[1] > now}
            self.bindings[key] = (uid, time.time() + self.ttl)

    def unbind(self, key):
        if not key:
            return
        with self._lock:
            self.bindings.pop(key, None)


# ---------------------------------------------------------------------------
# AccountPool
# ---------------------------------------------------------------------------
_ACTIVE_POOL = None


def add_to_pool(account):
    """把导入的账号写入活动账号池（AccountPool 构造时自注册）。"""
    if _ACTIVE_POOL is None:
        raise RuntimeError("account pool not initialised")
    return _ACTIVE_POOL.add(account)


class AccountPool(object):
    def __init__(self, directory, log=None):
        global _ACTIVE_POOL
        self.dir = directory
        self.log = log or (lambda msg: None)
        self.accounts = []
        self.logins = {}
        self._lock = threading.RLock()
        self._cursor = 0
        self.affinity = SessionAffinity()
        _ACTIVE_POOL = self

    def load(self):
        with self._lock:
            self.accounts = []
            if not os.path.isdir(self.dir):
                return self.accounts
            for name in sorted(os.listdir(self.dir)):
                if not name.endswith(".json"):
                    continue
                if name in ("settings.json", "active_realm.json"):
                    continue
                path = os.path.join(self.dir, name)
                try:
                    with open(path, encoding="utf-8") as fh:
                        account = Account(json.load(fh), path)
                except Exception as exc:
                    self.log("account %s unreadable: %s" % (name, exc))
                    continue
                if account.uid:
                    self.accounts.append(account)
            return self.accounts

    def list_public(self, realm=None):
        with self._lock:
            accs = self.accounts if (not realm or realm == "all") else \
                [a for a in self.accounts if a.realm == realm]
            return [a.public() for a in accs]

    def get(self, uid):
        with self._lock:
            for account in self.accounts:
                if account.uid == uid:
                    return account
        return None

    def add(self, account):
        with self._lock:
            existing = self.get(account.uid)
            if existing is not None:
                account.added_at = existing.added_at
                account.path = existing.path
                if not account.credits and existing.credits:
                    account.credits = existing.credits
                if not account.plan and existing.plan:
                    account.plan = existing.plan
                if not account.last_checkin and existing.last_checkin:
                    account.last_checkin = existing.last_checkin
                if not account.personal_token and existing.personal_token:
                    account.personal_token = existing.personal_token
                self.accounts[self.accounts.index(existing)] = account
            else:
                self.accounts.append(account)
            account.save(self.dir)
            return account

    def remove(self, uid):
        with self._lock:
            account = self.get(uid)
            if account is None:
                return False
            account.delete()
            self.accounts.remove(account)
            return True

    # -- 选择与健康 --------------------------------------------------------
    def count_ready(self, realm=None, model=None):
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
        return sum(1 for a in snapshot if a.enabled and a.access_token and
                   a.ready(model=model))

    def pick_for_session(self, realm=None, session_key=None, exclude=None, model=None):
        exclude = exclude or set()
        if session_key:
            bound_uid = self.affinity.get(session_key)
            if bound_uid and bound_uid not in exclude:
                account = self.get(bound_uid)
                if account and account.realm == realm and account.ready(model=model):
                    return account
                self.affinity.unbind(session_key)
        account = self.pick(realm=realm, exclude=exclude, model=model)
        if account and session_key:
            self.affinity.bind(session_key, account.uid)
        return account

    def pick(self, realm=None, exclude=None, model=None):
        """轮询选择一个账号：额度未耗尽的一档优先，耗尽/未知为后备档。

        两档内都沿用轮询游标（均摊负载），并跳过禁用/冷却中/该模型频控中
        的账号。会话粘性（同一对话固定账号以命中上游 prompt 缓存）由
        pick_for_session 在此之上处理。
        """
        exclude = exclude or set()
        with self._lock:
            snapshot = [a for a in self.accounts if not realm or a.realm == realm]
            start = self._cursor
        total = len(snapshot)
        if total == 0:
            return None
        ready_list = []
        for offset in range(total):
            account = snapshot[(start + offset) % total]
            if account.uid in exclude:
                continue
            if account.ready(model=model):
                ready_list.append(account)
        if not ready_list:
            return None
        primary = [a for a in ready_list if not a.quota_depleted()]
        pool_list = primary or ready_list
        # least-busy：在途请求少的优先；稳定排序，同负载时保持轮询顺序。
        # （长时间流式请求占着账号时，新请求会落到更空闲的账号上。）
        pool_list.sort(key=lambda a: getattr(a, "in_flight", 0))
        account = pool_list[0]
        with self._lock:
            self._cursor = (snapshot.index(account) + 1) % total
        return account

    def representative(self, realm=None):
        with self._lock:
            candidates = [a for a in self.accounts if not realm or a.realm == realm]
            for account in candidates:
                if account.access_token:
                    return account
            return candidates[0] if candidates else None

    def set_enabled(self, uid, enabled):
        account = self.get(uid)
        if account is None:
            return None
        account.enabled = bool(enabled)
        if enabled:
            account.clear_error()
        account.save(self.dir)
        return account.public()

    def set_all_enabled(self, enabled, realm=None):
        with self._lock:
            for account in self.accounts:
                if realm and account.realm != realm:
                    continue
                account.enabled = bool(enabled)
                if enabled:
                    account.clear_error()
                account.save(self.dir)

    # -- 导入 / 导出 -------------------------------------------------------
    def preview_import_rows(self, rows, realm=None, overwrite=False):
        """报告 import_rows() 会做什么，不触碰账号池（Dry-Run）。"""
        preview = {"added": [], "updated": [], "skipped": [], "invalid": []}
        known = {a.uid for a in self.accounts}
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                preview["invalid"].append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                preview["skipped"].append({"uid": uid,
                                           "reason": "duplicate inside the document"})
            elif uid in known and not overwrite:
                preview["skipped"].append({"uid": uid, "reason": "already exists"})
            elif uid in known:
                preview["updated"].append(uid)
            else:
                preview["added"].append(uid)
            seen.add(uid)
        return preview

    def import_rows(self, rows, realm=None, overwrite=False):
        """从导出/外部文档批量导入账号。

        返回报告：added / updated / skipped / invalid。
        一行解析失败不影响其余行；全部解析通过才写盘。
        """
        added, updated, skipped, invalid = [], [], [], []
        seen = set()
        for index, row in enumerate(rows):
            try:
                kwargs = normalise_import_row(row, realm=realm)
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue
            uid = kwargs["uid"]
            if uid in seen:
                skipped.append({"uid": uid,
                                "reason": "duplicate inside the document"})
                continue
            seen.add(uid)
            existing = self.get(uid) is not None
            if existing and not overwrite:
                skipped.append({"uid": uid, "reason": "already exists"})
                continue
            try:
                self.add(Account(kwargs))
            except Exception as exc:
                invalid.append({"index": index + 1, "reason": str(exc)})
                continue
            (updated if existing else added).append(uid)
        return {
            "added": added,
            "updated": updated,
            "skipped": skipped,
            "invalid": invalid,
        }

    # -- OAuth 设备授权登录 ------------------------------------------------
    @staticmethod
    def _local_machine_id(realm):
        """读取本机官方客户端的 machine_id（优先，保持设备一致），缺则生成。"""
        cfg = get_realm_config(realm)
        home = os.path.join(os.path.expanduser("~"), cfg["home_dir"], ".auth")
        p = os.path.join(home, "machine_id")
        try:
            with open(p, encoding="utf-8") as fh:
                mid = fh.read().strip()
            if mid:
                return mid
        except Exception:
            pass
        return str(uuid.uuid4())

    def start_login(self, realm="cn", platform="CLI"):
        """构造 PKCE 设备授权 URL（浏览器打开完成授权）。

        双区 URL 参数差异（官方逆向）：
          CN   : challenge, challenge_method, nonce(带横线), redirect_uri,
                 client_id, machine_id
          Intl : challenge, challenge_method, nonce(32-hex), client_id,
                 machine_id（新协议带 client_id/machine_id、不带 redirect_uri）
        """
        cfg = get_realm_config(realm)
        verifier, challenge = _make_pkce()
        nonce = uuid.uuid4().hex if not cfg["nonce_dashed"] else str(uuid.uuid4())
        q = {
            "challenge": challenge,
            "challenge_method": "S256",
            "nonce": nonce,
        }
        if cfg.get("send_redirect_uri"):
            q["redirect_uri"] = cfg["redirect_uri"]
        if cfg.get("send_client_id"):
            q["client_id"] = cfg["client_id"]
            q["machine_id"] = self._local_machine_id(realm)
        auth_url = cfg["website"] + "/device/selectAccounts?" + urllib.parse.urlencode(q)
        state = "qd-%d" % time.time_ns()
        with self._lock:
            self.logins[state] = {
                "created": time.time(),
                "verifier": verifier,
                "nonce": nonce,
                "region": realm,
                "platform": platform,
            }
        return {"state": state, "authUrl": auth_url, "realm": realm,
                "platform": platform}

    def poll_login(self, state):
        state = str(state or "").strip()
        with self._lock:
            info = self.logins.get(state)
        if not info:
            return {"status": "unknown",
                    "message": "state not recognised - start the login again"}
        if time.time() - info["created"] > LOGIN_TTL_SECONDS:
            with self._lock:
                self.logins.pop(state, None)
            return {"status": "expired", "message": "login window expired - start again"}
        realm = info.get("region") or "cn"
        cfg = get_realm_config(realm)
        q = urllib.parse.urlencode({
            "nonce": info["nonce"],
            "verifier": info["verifier"],
            "challenge_method": "S256",
        })
        url = cfg["openapi"] + PATH_DEVICE_POLL + "?" + q
        validate_public_http_url(url)
        req = urllib.request.Request(url, method="GET", headers={
            "Accept": "application/json",
            "User-Agent": "QoderWork",
        })
        try:
            with qoder_net.urlopen(req, timeout=20) as resp:
                raw = resp.read().decode("utf-8")
                status = resp.status
        except urllib.error.HTTPError as exc:
            # 404 / 202 = 用户尚未完成授权（继续轮询）
            if exc.code in (404, 202):
                return {"status": "pending",
                        "message": "等待浏览器完成 Qoder 设备授权"}
            try:
                body = exc.read().decode("utf-8", "replace")
            except Exception:
                body = ""
            return {"status": "error", "message": "poll http %d %s" % (exc.code, body[:160])}
        except Exception as exc:
            return {"status": "pending", "message": "poll error: %s" % exc}
        if status in (404, 202):
            return {"status": "pending", "message": "等待浏览器完成 Qoder 设备授权"}
        try:
            data = json.loads(raw)
        except Exception:
            return {"status": "pending", "message": "waiting for grant"}
        token = data.get("token") or data.get("device_token") or ""
        if not token:
            return {"status": "pending", "message": "waiting for token"}

        uid = str(data.get("user_id") or "")
        nickname = ""
        # 拉取 userinfo 补全昵称/用户类型（尽力而为，不阻塞入库）
        try:
            ui_url = cfg["openapi"] + PATH_USERINFO
            validate_public_http_url(ui_url)
            req_ui = urllib.request.Request(ui_url, method="GET", headers={
                "Accept": "application/json",
                "User-Agent": CLIENT_UA,
                "Authorization": "Bearer " + token,
            })
            with qoder_net.urlopen(req_ui, timeout=15) as resp_ui:
                ui = json.loads(resp_ui.read().decode("utf-8"))
            uid = str(ui.get("id") or uid)
            nickname = str(ui.get("name") or "")
            user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
            org_id = str(ui.get("organization_id") or "")
            org_name = str(ui.get("organization_name") or "")
        except Exception:
            user_type, org_id, org_name = DEFAULT_USER_TYPE, "", ""

        account = Account({
            "uid": uid or ("q-" + uuid.uuid4().hex[:24]),
            "nickname": nickname or ("u" + (uid[-8:] if uid else "")),
            "domain": cfg["domain"],
            "realm": realm,
            "platform": info.get("platform") or "CLI",
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "expiresAt": _device_expiry(data),
            "source": "oauth",
            "enabled": True,
            "userType": user_type,
            "organizationId": org_id,
            "organizationName": org_name,
        })
        self.add(account)
        with self._lock:
            self.logins.pop(state, None)
        return {"status": "ok", "account": account.public()}

    def cancel_login(self, state):
        with self._lock:
            return self.logins.pop(state, None) is not None

    # -- PAT 导入 ----------------------------------------------------------
    def import_pat(self, pat, realm="cn"):
        """导入 pt- 个人访问令牌：交换 jobToken 并拉取身份后入库。"""
        pat = str(pat or "").strip()
        if not pat.startswith("pt-"):
            raise ValueError("PAT must start with pt-")
        cfg = get_realm_config(realm)
        data = http_json(cfg["openapi"] + PATH_JOB_EXCHANGE,
                         data=json.dumps({"personal_token": pat}).encode(),
                         method="POST",
                         headers={"Content-Type": "application/json",
                                  "Accept": "application/json",
                                  "User-Agent": CLIENT_UA},
                         timeout=30)
        token = data.get("token") or ""
        if not token:
            raise ValueError("jobToken exchange returned no token")
        uid, nickname, user_type, org_id, org_name = "", "", DEFAULT_USER_TYPE, "", ""
        try:
            ui = http_json(cfg["openapi"] + PATH_USERINFO, method="GET",
                           headers={"Accept": "application/json",
                                    "User-Agent": CLIENT_UA,
                                    "Authorization": "Bearer " + token},
                           timeout=15, retries=2)
            uid = str(ui.get("id") or "")
            nickname = str(ui.get("name") or "")
            user_type = str(ui.get("user_type") or "") or DEFAULT_USER_TYPE
            org_id = str(ui.get("organization_id") or "")
            org_name = str(ui.get("organization_name") or "")
        except Exception:
            pass
        if data.get("expires_in"):
            exp = int(time.time() + int(data["expires_in"]) / 1000)
        else:
            exp = int(time.time()) + 24 * 3600
        account = Account({
            "uid": uid or ("p-" + uuid.uuid4().hex[:24]),
            "nickname": nickname or ("u" + (uid[-8:] if uid else uid[:8])),
            "domain": cfg["domain"],
            "realm": realm,
            "platform": "CLI",
            "accessToken": token,
            "refreshToken": data.get("refresh_token") or "",
            "personalToken": pat,
            "expiresAt": exp,
            "source": "pat",
            "enabled": True,
            "userType": user_type,
            "organizationId": org_id,
            "organizationName": org_name,
        })
        self.add(account)
        return account


def _make_pkce():
    """RFC 7636 S256: (verifier, challenge)。"""
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    raw = os.urandom(64)
    verifier = "".join(alphabet[b % len(alphabet)] for b in raw)
    challenge = __import__("hashlib").sha256(verifier.encode("ascii")).digest()
    import base64 as _b64
    return verifier, _b64.urlsafe_b64encode(challenge).decode("ascii").rstrip("=")


# ---------------------------------------------------------------------------
# 本机已登录凭证的只读探测与导入（与 wb 网关的桌面扫描同构，双区都支持）
#
# 两类官方存储：
#   1. 桌面 App（Electron）： %APPDATA%\com.qoder[.cn].app.stable\auth.v1.dat
#      布局 "v10" + AES-256-GCM；密钥在同目录 Local State 的
#      os_crypt.encrypted_key（DPAPI 保护）-> 剥 "DPAPI" 前缀 -> DPAPI 解出。
#      明文 schema: {schemaVersion, token(dt-), refreshToken(drt-), expiresAt,
#                    user:{id,name,email,...}}
#   2. CLI/官方客户端： ~/.qoder[.cn]/.auth/user[.{profile}]
#      AES-128-CBC key=iv=machine_id 前 16 字符，标准 Base64（strict padding）；
#      明文 UserInfo JSON（或明文以 "{" 开头的兼容形态）。
# 扫描全程只读；看板两步确认后才写入网关账号池。
# ---------------------------------------------------------------------------
def _read_chromium_os_crypt_key(app_dir):
    """Local State.os_crypt.encrypted_key -> DPAPI 解出的 32 字节 AES key。"""
    import base64 as _b64
    from qoder_sign import dpapi_unprotect
    p = os.path.join(app_dir, "Local State")
    with open(p, encoding="utf-8") as fh:
        state = json.load(fh)
    ek = (state.get("os_crypt") or {}).get("encrypted_key")
    if not ek:
        raise RuntimeError("Local State has no os_crypt.encrypted_key")
    blob = _b64.b64decode(ek)
    if blob[:5] != b"DPAPI":
        raise RuntimeError("unexpected encrypted_key header %r" % blob[:5])
    return dpapi_unprotect(blob[5:])


def _roaming_app_dir(cfg):
    base = os.environ.get("APPDATA") or os.path.join(
        os.path.expanduser("~"), "AppData", "Roaming")
    return os.path.join(base, cfg["app_dir"])


def _load_app_auth(realm):
    """解出桌面 App auth.v1.dat 的明文 dict；失败抛异常。"""
    from qoder_sign import chromium_decrypt_v10
    cfg = get_realm_config(realm)
    app_dir = _roaming_app_dir(cfg)
    key = _read_chromium_os_crypt_key(app_dir)
    with open(os.path.join(app_dir, "auth.v1.dat"), "rb") as fh:
        blob = fh.read()
    plain = chromium_decrypt_v10(blob, key)
    data = json.loads(plain.decode("utf-8"))
    if not isinstance(data, dict) or not data.get("token"):
        raise RuntimeError("auth.v1.dat has unexpected schema")
    return data


def _load_cli_user(realm, path, machine_key):
    """解 CLI 端 ~/.qoder*/.auth/user（AES-128-CBC）或明文兼容形态。"""
    from qoder_sign import aes_cbc_decrypt
    with open(path, encoding="utf-8") as fh:
        raw = fh.read().strip()
    if raw.startswith("{"):
        return json.loads(raw)
    key = (machine_key or "")[:16].encode("utf-8")
    if len(key) != 16:
        raise RuntimeError("machine_id shorter than 16 bytes")
    pt = aes_cbc_decrypt(base64.b64decode(raw), key, key)
    return json.loads(pt.decode("utf-8"))


def scan_desktop_credentials():
    """只读探测本机双区已登录凭证。返回候选列表（不含任何明文令牌）。"""
    found = []
    for realm in ("intl", "cn"):
        cfg = get_realm_config(realm)
        # 1) 桌面 App (auth.v1.dat)
        item = {
            "kind": "app",
            "path": os.path.join(_roaming_app_dir(cfg), "auth.v1.dat"),
            "file": "auth.v1.dat",
            "realm": realm,
            "realmName": cfg["name"],
            "domain": cfg["domain"],
            "readable": False,
            "valid": False,
            "uid": "",
            "nickname": "",
            "expiresAt": 0,
            "error": "",
        }
        try:
            data = _load_app_auth(realm)
            user = data.get("user") or {}
            item["readable"] = True
            exp = normalize_epoch(data.get("expiresAt")) or 0
            if not exp and data.get("token"):
                # expiresAt 是 RFC3339 -> normalize_epoch 处理不了，单独解析
                try:
                    import datetime
                    exp = int(datetime.datetime.strptime(
                        str(data["expiresAt"])[:19], "%Y-%m-%dT%H:%M:%S"
                    ).timestamp())
                except Exception:
                    exp = 0
            token_prefix = str(data.get("token") or "")[:3]
            item.update({
                "valid": token_prefix == "dt-" or bool(data.get("refreshToken")),
                "uid": str(user.get("id") or ""),
                "nickname": str(user.get("name") or ""),
                "expiresAt": exp,
                "expiresIn": _human_delta(exp - time.time()) if exp else None,
            })
        except FileNotFoundError:
            item["error"] = "not found (未登录或未安装该版本客户端)"
        except Exception as exc:
            item["error"] = str(exc)
        found.append(item)

        # 2) CLI 端 user / user.{profile}
        auth_dir = os.path.join(os.path.expanduser("~"), cfg["home_dir"], ".auth")
        if os.path.isdir(auth_dir):
            machine_key = ""
            try:
                with open(os.path.join(auth_dir, "machine_id"),
                          encoding="utf-8") as fh:
                    machine_key = fh.read().strip()
            except Exception:
                pass
            try:
                names = [n for n in os.listdir(auth_dir)
                         if n == "user" or n.startswith("user.")]
            except Exception:
                names = []
            for n in names:
                p = os.path.join(auth_dir, n)
                cli_item = {
                    "kind": "cli",
                    "path": p,
                    "file": n,
                    "realm": realm,
                    "realmName": cfg["name"],
                    "domain": cfg["domain"],
                    "readable": False,
                    "valid": False,
                    "uid": "",
                    "nickname": "",
                    "expiresAt": 0,
                    "error": "",
                }
                try:
                    data = _load_cli_user(realm, p, machine_key)
                    token = str(data.get("access_token") or "")
                    cli_item["readable"] = True
                    exp = normalize_epoch(data.get("expire_time"))
                    cli_item.update({
                        "valid": token.startswith(("dt-", "jt-")),
                        "uid": str(data.get("uid") or ""),
                        "nickname": str(data.get("name") or ""),
                        "expiresAt": exp,
                        "expiresIn": _human_delta(exp - time.time()) if exp else None,
                    })
                except Exception as exc:
                    cli_item["error"] = str(exc)
                found.append(cli_item)
    return found


def import_desktop_credential(path=None, realm=None):
    """把扫描到的凭证导入账号池。path=None 时导入扫描到的全部有效项。"""
    if not path:
        imported, errors = [], []
        for item in scan_desktop_credentials():
            if not item.get("valid"):
                continue
            try:
                imported.append(import_desktop_credential(
                    path=item["path"], realm=item["realm"]))
            except Exception as exc:
                errors.append("%s/%s: %s" % (item["realm"], item["file"], exc))
        if errors:
            raise RuntimeError("; ".join(errors[:3]))
        return imported

    # 定位该 path 归属的 realm（按扫描结果匹配；否则按目录名猜）
    target_realm = realm
    matched = None
    for item in scan_desktop_credentials():
        if os.path.abspath(item["path"]) == os.path.abspath(path):
            matched = item
            target_realm = item["realm"]
            break
    if target_realm not in REALM_CONFIGS:
        target_realm = "cn"
    cfg = get_realm_config(target_realm)

    token = refresh = ""
    uid = nickname = ""
    exp = 0
    base = os.path.basename(path)
    if base == "auth.v1.dat":
        data = _load_app_auth(target_realm)
        token = str(data.get("token") or "")
        refresh = str(data.get("refreshToken") or "")
        user = data.get("user") or {}
        uid = str(user.get("id") or "")
        nickname = str(user.get("name") or "")
        try:
            import datetime
            exp = int(datetime.datetime.strptime(
                str(data.get("expiresAt") or "")[:19], "%Y-%m-%dT%H:%M:%S"
            ).timestamp())
        except Exception:
            exp = 0
    else:
        auth_dir = os.path.dirname(path)
        machine_key = ""
        try:
            with open(os.path.join(auth_dir, "machine_id"),
                      encoding="utf-8") as fh:
                machine_key = fh.read().strip()
        except Exception:
            pass
        data = _load_cli_user(target_realm, path, machine_key)
        token = str(data.get("access_token") or "")
        refresh = str(data.get("refresh_token") or "")
        uid = str(data.get("uid") or "")
        nickname = str(data.get("name") or "")
        exp = normalize_epoch(data.get("expire_time"))

    if not token:
        raise RuntimeError("credential has no access token")
    if not uid:
        uid = "d-" + uuid.uuid4().hex[:24]
    account = Account({
        "uid": uid,
        "nickname": nickname or uid[:8],
        "domain": cfg["domain"],
        "realm": target_realm,
        "platform": "CLI",
        "accessToken": token,
        "refreshToken": refresh,
        "expiresAt": exp or (int(time.time()) + 30 * 86400),
        "source": "desktop-app",
        "enabled": True,
    })
    return add_to_pool(account)


# ---------------------------------------------------------------------------
# 导入 / 导出（与 WorkBuddy 网关同构的文档格式）
# ---------------------------------------------------------------------------
EXPORT_FORMAT = "qoder-accounts"
EXPORT_VERSION = 1

# 描述运行期状态而非凭证本身的字段：导入时导出可查、但绝不信任。
VOLATILE_FIELDS = ("cooldownUntil", "lastError", "credits", "lastCheckin", "plan")


def account_to_export(account):
    data = account.to_dict()
    data.pop("path", None)
    return data


def build_export_document(accounts, realm=None, include_secrets=True, uids=None):
    wanted = None
    if uids is not None:
        wanted = {str(u) for u in uids}
    rows = []
    for account in accounts:
        if realm and account.realm != realm:
            continue
        if wanted is not None and account.uid not in wanted:
            continue
        row = account_to_export(account)
        if not include_secrets:
            row.pop("accessToken", None)
            row.pop("refreshToken", None)
            row.pop("personalToken", None)
        rows.append(row)
    return {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "exportedAt": time.strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(rows),
        "accounts": rows,
    }


def _coerce_account_rows(blob):
    """把任意受支持的容器规整成账号 dict 列表。返回 (rows, error)。"""
    if isinstance(blob, list):
        rows = blob
    elif isinstance(blob, dict) and isinstance(blob.get("accounts"), list):
        rows = blob["accounts"]
    elif isinstance(blob, dict):
        looks_like_account = (
            blob.get("accessToken")
            or isinstance(blob.get("auth"), dict)
            or isinstance(blob.get("account"), dict)
        )
        if not looks_like_account:
            keys = ", ".join(sorted(blob.keys())[:6]) or "none"
            return [], ("not an account document (expected an accounts array, "
                        "a list, or an account object; got keys: %s)" % keys)
        rows = [blob]
    else:
        return [], "expected an object or a list of accounts"

    out = []
    for index, row in enumerate(rows):
        if not isinstance(row, dict):
            return [], "account #%d is not an object" % (index + 1)
        out.append(row)
    if not out:
        return [], "no accounts found in the document"
    return out, ""


def normalise_import_row(row, realm=None):
    """把一行导入数据规整成 Account kwargs；无可用凭证时 raise ValueError。"""
    auth = row.get("auth") if isinstance(row.get("auth"), dict) else None
    profile = row.get("account") if isinstance(row.get("account"), dict) else None

    def pick(key, default=None):
        for layer in (row, auth, profile):
            if isinstance(layer, dict) and layer.get(key) not in (None, ""):
                return layer.get(key)
        return default

    token = str(pick("accessToken") or "").strip()
    if not token:
        raise ValueError("no accessToken")
    detected = str(realm or pick("realm") or "").strip().lower()
    if detected not in ("cn", "intl"):
        detected = detect_realm_from_domain(pick("domain"))
    cfg = get_realm_config(detected)
    raw_uid = str(pick("uid") or "").strip()
    uid = re.sub(r"[^A-Za-z0-9_-]", "_", raw_uid).strip("_ ")
    if not uid:
        uid = "p-" + uuid.uuid4().hex[:24]
    exp = normalize_epoch(pick("expiresAt"))
    if not exp:
        exp = int(time.time()) + 3600
    return {
        "uid": uid,
        "nickname": str(pick("nickname") or ""),
        "domain": str(pick("domain") or cfg["domain"]),
        "realm": detected,
        "platform": str(pick("platform") or "CLI"),
        "accessToken": token,
        "refreshToken": str(pick("refreshToken") or ""),
        "personalToken": str(pick("personalToken") or ""),
        "expiresAt": exp,
        "source": "import",
        "enabled": True,
        "userType": str(pick("userType") or "") or DEFAULT_USER_TYPE,
        "organizationId": str(pick("organizationId") or ""),
        "organizationName": str(pick("organizationName") or ""),
        "lastError": "",
        "cooldownUntil": 0.0,
    }
