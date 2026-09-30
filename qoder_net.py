"""qoder_net.py —— 全项目统一出站网络层（代理模式：跟随系统 / 手动 / 直连）

项目所有出站 HTTP（账号 openapi、活动平台、模型清单、推理流、设备轮询、
辅助脚本）统一经过这里，保证看板「设置 → 网络代理」里选的模式对**所有**
请求生效：

  - "system"：跟随系统代理——Windows 优先读 Internet Settings 注册表
    （真正的“系统代理”，不受终端环境变量污染），拿不到再回退标准
    `urllib.request.getproxies()`（环境变量 / 平台默认）；
  - "manual"：手动指定一个 HTTP(S) 代理地址（如 http://127.0.0.1:7897）；
  - "direct"：直连，永不使用代理。

本机回环（localhost / 127.* / ::1 / *.local）在任何模式下都直连，保证
网关自检、脚本探活不会被代理拦截。

运行时配置来自 accounts/settings.json（面板修改即时生效）；环境变量
QD_PROXY_MODE / QD_PROXY_URL 优先级更高，便于 Docker 与临时调试。仅标准库。
"""

import os
import threading
import urllib.parse
import urllib.request

MODES = ("system", "manual", "direct")
_LOCAL_BYPASS = "localhost,127.*,[::1],<local>"

_lock = threading.RLock()
_config = {"mode": "system", "url": ""}
_opener = None
_opener_key = None
_direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def normalize_url(url):
    """规范化手动代理地址：无 scheme 补 http://；只接受 http/https。

    urllib 不支持 socks/socks5（那是需要第三方库的协议），明确报错而不是
    静默连不上。
    """
    value = str(url or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "http://" + value
    scheme = value.split("://", 1)[0].lower()
    if scheme not in ("http", "https"):
        raise ValueError(
            "代理地址只支持 http:// 或 https://（收到 %s://；"
            "urllib 不支持 socks，请用 mixed/http 端口）" % scheme)
    return value


def configure(mode=None, url=None):
    """设置代理模式（manual 需带 url）。校验失败抛 ValueError。

    环境变量 QD_PROXY_MODE / QD_PROXY_URL 存在时优先，忽略传参。
    """
    global _config, _opener, _opener_key
    env_mode = (os.environ.get("QD_PROXY_MODE") or "").strip().lower()
    env_url = (os.environ.get("QD_PROXY_URL") or "").strip()
    resolved = env_mode or str(mode or "system").strip().lower()
    if resolved not in MODES:
        resolved = "system"
    value = env_url or str(url or "").strip()
    if resolved == "manual":
        value = normalize_url(value)
        if not value:
            raise ValueError("手动代理模式需要填写代理地址（如 http://127.0.0.1:7897）")
    with _lock:
        _config = {"mode": resolved, "url": value}
        _opener = None
        _opener_key = None
    return current()


def load_from_settings(accounts_dir):
    """从 accounts/settings.json 装载并生效（启动时调用；坏配置回退 system）。"""
    try:
        import qoder_settings
        cfg = qoder_settings.proxy_config(accounts_dir)
    except Exception:
        cfg = {"mode": "system", "url": ""}
    try:
        return configure(cfg.get("mode"), cfg.get("url"))
    except ValueError:
        return configure("system", "")


def current():
    """当前配置（含环境变量覆盖标记），供面板/日志展示。"""
    with _lock:
        cfg = dict(_config)
    cfg["env_override"] = bool((os.environ.get("QD_PROXY_MODE") or "").strip()
                               or (os.environ.get("QD_PROXY_URL") or "").strip())
    return cfg


def proxies():
    """当前模式对应的 ProxyHandler 参数表（direct 为空 dict）。"""
    with _lock:
        mode, url = _config["mode"], _config["url"]
    if mode == "direct":
        return {}
    if mode == "manual":
        return {"http": url, "https": url, "no": _LOCAL_BYPASS}
    # system：优先 Windows 注册表（系统代理开关的真实来源，避免被终端里
    # 残留的 HTTP_PROXY/ALL_PROXY 环境变量“顶掉”），拿不到再回退标准实现。
    result = {}
    registry = getattr(urllib.request, "getproxies_registry", None)
    if registry is not None:
        try:
            result = registry() or {}
        except Exception:
            result = {}
    if not result:
        try:
            result = urllib.request.getproxies() or {}
        except Exception:
            result = {}
    if result and not result.get("no"):
        result = dict(result)
        result["no"] = _LOCAL_BYPASS
    return result


def is_loopback(host):
    """本机回环地址判定（始终直连，不走代理）。"""
    host = str(host or "").strip().lower().strip("[]")
    return (host == "localhost" or host.startswith("127.")
            or host in ("::1", "0.0.0.0") or host.endswith(".local"))


def opener_for(url):
    """按目标地址选 opener：回环直连；其余走当前代理模式。"""
    global _opener, _opener_key
    try:
        host = urllib.parse.urlsplit(str(url)).hostname or ""
    except Exception:
        host = ""
    if is_loopback(host):
        return _direct_opener
    with _lock:
        mode, config_url = _config["mode"], _config["url"]
        key = (mode, config_url)
        if _opener is None or _opener_key != key:
            _opener = urllib.request.build_opener(
                urllib.request.ProxyHandler(proxies()))
            _opener_key = key
        return _opener


def urlopen(req, timeout=30):
    """统一 urlopen：接受 str 或 Request，返回原始响应对象（支持流式）。"""
    if isinstance(req, str):
        req = urllib.request.Request(req)
    return opener_for(req.full_url).open(req, timeout=timeout)


def describe():
    """一句话描述当前生效的代理路径（面板与启动日志共用）。"""
    cfg = current()
    table = proxies()
    mode = cfg["mode"]
    if mode == "direct":
        effective = "直连（不使用代理）"
    elif mode == "manual":
        effective = "手动代理 %s" % cfg["url"]
    else:
        target = table.get("https") or table.get("http") or ""
        effective = ("跟随系统代理 %s" % target) if target \
            else "跟随系统代理（当前系统未启用代理 → 实际直连）"
    if cfg["env_override"]:
        effective += "（被环境变量 QD_PROXY_MODE/QD_PROXY_URL 覆盖）"
    return {"mode": mode, "url": cfg["url"], "effective": effective,
            "env_override": cfg["env_override"], "proxies": table}
