"""Runtime settings for the Qoder gateway: panel password and API keys.

Everything lives in `accounts/settings.json` so a change made from the web
panel survives a restart without editing the launcher .bat files. The panel
password is never stored in clear text - only a PBKDF2-SHA256 digest.

Only the Python standard library is required.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
from pathlib import Path

DEFAULT_PANEL_PASSWORD = "admin"
PBKDF2_ROUNDS = 120_000
SESSION_TTL = 7 * 24 * 3600

_lock = threading.RLock()


def settings_path(accounts_dir):
    return os.path.join(accounts_dir, "settings.json")


def _digest(password, salt_hex, rounds=PBKDF2_ROUNDS):
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode("utf-8"), bytes.fromhex(salt_hex), rounds
    ).hex()


def load(accounts_dir):
    """Return the persisted settings, or an empty dict on a fresh install."""
    path = settings_path(accounts_dir)
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict):
            return data
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return {}


def save(accounts_dir, data):
    """Atomic write so a crash cannot leave a half-written settings file."""
    with _lock:
        base = Path(accounts_dir).resolve()
        base.mkdir(parents=True, exist_ok=True)
        path = base / "settings.json"
        tmp = base / "settings.json.tmp"
        if not (path.is_relative_to(base) and tmp.is_relative_to(base)):
            raise ValueError("path escapes base directory")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2),
                       encoding="utf-8")
        os.replace(tmp, path)
        return str(path)


def panel_password_is_default(accounts_dir):
    data = load(accounts_dir)
    if not data.get("panel_password_hash"):
        return True
    return data.get("panel_password_default") is True


def verify_panel_password(accounts_dir, password):
    """True when `password` opens the web panel."""
    password = password or ""
    data = load(accounts_dir)
    stored = data.get("panel_password_hash")
    if not stored:
        return password == DEFAULT_PANEL_PASSWORD
    if data.get("panel_password_default") is True:
        return password == DEFAULT_PANEL_PASSWORD
    salt = data.get("panel_password_salt")
    if not salt:
        return False
    rounds = int(data.get("panel_password_rounds") or PBKDF2_ROUNDS)
    try:
        given = _digest(password, salt, rounds)
    except Exception:
        return False
    return hmac.compare_digest(given, stored)


def set_panel_password(accounts_dir, password):
    with _lock:
        data = load(accounts_dir)
        if password == DEFAULT_PANEL_PASSWORD:
            data.pop("panel_password_salt", None)
            data.pop("panel_password_rounds", None)
            data["panel_password_hash"] = ""
            data["panel_password_default"] = True
        else:
            salt = secrets.token_hex(16)
            data["panel_password_salt"] = salt
            data["panel_password_rounds"] = PBKDF2_ROUNDS
            data["panel_password_hash"] = _digest(password, salt)
            data["panel_password_default"] = False
        save(accounts_dir, data)


def api_key_override(accounts_dir):
    """Return (key, is_set). `is_set` means the panel manages the key."""
    data = load(accounts_dir)
    if not data.get("api_key_set"):
        return None, False
    return str(data.get("api_key") or ""), True


def set_api_key(accounts_dir, key):
    with _lock:
        data = load(accounts_dir)
        data["api_key"] = key or ""
        data["api_key_set"] = True
        save(accounts_dir, data)


def ensure_launcher_key(accounts_dir):
    """Return the persisted LAN key, creating one on first use.

    LAN mode must never ship a well-known default: the gateway spends the
    account's own upstream quota, so anyone on the same network could drain it.
    The value is generated once and stored so clients keep working across
    restarts. Returns (key, created) so the caller can tell the user whether
    this run minted a fresh credential.
    """
    with _lock:
        data = load(accounts_dir)
        existing = str(data.get("launcher_key") or "").strip()
        if existing:
            return existing, False
        key = "qd-" + secrets.token_urlsafe(24)
        data["launcher_key"] = key
        save(accounts_dir, data)
        return key, True


# --------------------------------------------------------------- API keys
# Each key can be bound to one upstream realm, so several clients can hit
# different exits at the same time instead of sharing the global switch.

REALMS = ("", "intl", "cn")


def _clean_key_entry(entry):
    """Normalize one stored key entry; returns None when unusable."""
    if not isinstance(entry, dict):
        return None
    key = str(entry.get("key") or "").strip()
    if not key:
        return None
    realm = str(entry.get("realm") or "").strip().lower()
    if realm not in REALMS:
        realm = ""
    return {
        "id": str(entry.get("id") or secrets.token_hex(6)),
        "name": str(entry.get("name") or "").strip() or "未命名",
        "key": key,
        "realm": realm,
        "enabled": entry.get("enabled", True) is not False,
        "created_at": entry.get("created_at") or time.strftime("%Y/%m/%d %H:%M"),
    }


def api_keys(accounts_dir):
    """Every configured key, newest shape first.

    A settings file written by an older build only has the single
    `api_key`/`api_key_set` pair; that is surfaced as one unbound entry so
    upgrades keep working without a migration step.
    """
    data = load(accounts_dir)
    stored = data.get("api_keys")
    if isinstance(stored, list):
        out = []
        seen = set()
        for raw in stored:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                out.append(entry)
        return out

    if data.get("api_key_set"):
        legacy = str(data.get("api_key") or "").strip()
        if legacy:
            return [{
                "id": "legacy",
                "name": "默认（跟随网关出口）",
                "key": legacy,
                "realm": "",
                "enabled": True,
            }]
    return []


def set_api_keys(accounts_dir, keys):
    """Replace the whole key list. Returns the stored list."""
    with _lock:
        cleaned = []
        seen = set()
        for raw in keys or []:
            entry = _clean_key_entry(raw)
            if entry and entry["key"] not in seen:
                seen.add(entry["key"])
                cleaned.append(entry)
        data = load(accounts_dir)
        data["api_keys"] = cleaned
        # The single-key fields are now derived; drop them so there is one
        # source of truth and the list survives a restart.
        data.pop("api_key", None)
        data.pop("api_key_set", None)
        save(accounts_dir, data)
        return cleaned


def match_api_key(accounts_dir, supplied, extra_keys=()):
    """Find which configured key a request presented, if any.

    Returns a copy of the entry (with a `source` field) so the caller can read
    the bound realm, or None when nothing matches.
    """
    supplied = (supplied or "").strip()
    if not supplied:
        return None
    for entry in api_keys(accounts_dir):
        if entry["enabled"] and hmac.compare_digest(supplied, entry["key"]):
            out = dict(entry)
            out["source"] = "panel"
            return out
    for candidate in extra_keys:
        candidate = (candidate or "").strip()
        if candidate and hmac.compare_digest(supplied, candidate):
            return {
                "id": "launcher",
                "name": "启动参数",
                "key": candidate,
                "realm": "",
                "enabled": True,
                "source": "launcher",
            }
    return None


def auth_disabled(accounts_dir):
    """True when the operator switched API-key checking off entirely."""
    return load(accounts_dir).get("auth_disabled") is True


def set_auth_disabled(accounts_dir, disabled):
    with _lock:
        data = load(accounts_dir)
        data["auth_disabled"] = bool(disabled)
        save(accounts_dir, data)


# ------------------------------------------------- per-model defaults
# 官方桌面端为每个模型单独存「上下文窗口 + 思考档位」偏好（本地 model
# preferences 表），网关这边用同样的思路存一份：**客户端请求里带了什么就
# 用什么，没带才落到这里的默认值**。键为 "<realm>:<上游 key>"（同一个 key
# 在两区的窗口/档位可能不同）。

def model_overrides(accounts_dir):
    """所有模型的默认上下文窗口 / 思考档位，{realm:key: {...}}。"""
    data = load(accounts_dir)
    raw = data.get("model_overrides")
    out = {}
    if isinstance(raw, dict):
        for key, value in raw.items():
            name = str(key or "").strip()
            if not name or not isinstance(value, dict):
                continue
            item = {}
            try:
                window = int(value.get("context_window"))
                if window > 0:
                    item["context_window"] = window
            except (TypeError, ValueError):
                pass
            effort = str(value.get("effort") or "").strip().lower()
            if effort:
                item["effort"] = effort
            if item:
                out[name] = item
    return out


def set_model_override(accounts_dir, key, context_window=None, effort=None):
    """写入/更新/删除单个模型的默认值。

    传 None 表示「不动这一项」；传空字符串或 0 表示「删除这一项」。
    返回该模型剩余的默认值。
    """
    with _lock:
        data = load(accounts_dir)
        raw = data.get("model_overrides")
        raw = dict(raw) if isinstance(raw, dict) else {}
        name = str(key or "").strip()
        if not name:
            raise ValueError("model key is required")
        item = dict(raw.get(name) or {})
        if context_window is not None:
            text = str(context_window).strip()
            if not text or text == "0":
                item.pop("context_window", None)
            else:
                item["context_window"] = int(text)
        if effort is not None:
            text = str(effort).strip().lower()
            if not text:
                item.pop("effort", None)
            else:
                item["effort"] = text
        if item:
            raw[name] = item
        else:
            raw.pop(name, None)
        data["model_overrides"] = raw
        save(accounts_dir, data)
        return item


# ------------------------------------------------- machine identity
# 服务器（Linux/Docker）没有官方客户端，取不到风控桥 runtime-info.exe，机器
# 身份只能用"派生假值"，设备定向活动（每日 100 等）可能被服务端静默过滤。
# 身份是**机器级、与区域无关**的（实测同一台机器上国内/国际账号经桥取到的
# token/type/code 完全一致），因此允许把一台机器的真实身份**固定**到另一台：
# 在装有官方客户端的机器上看板「导出本机身份」→ 到服务器看板粘贴保存。

_IDENTITY_FIELDS = ("machineToken", "machineType", "machineCode")


def machine_identity(accounts_dir):
    """当前固定的机器身份（未固定返回 None）。"""
    data = load(accounts_dir)
    raw = data.get("machine_identity")
    if not isinstance(raw, dict):
        return None
    out = {}
    for key in _IDENTITY_FIELDS:
        value = str(raw.get(key) or "").strip()
        if not value:
            return None
        out[key] = value
    if isinstance(raw.get("vmInfo"), dict):
        out["vmInfo"] = raw["vmInfo"]
    out["pinned_at"] = str(raw.get("pinned_at") or "")
    return out


def set_machine_identity(accounts_dir, identity):
    """固定/清除机器身份；identity=None 表示清除。返回保存后的值。"""
    with _lock:
        data = load(accounts_dir)
        if identity is None:
            data.pop("machine_identity", None)
        else:
            clean = {}
            for key in _IDENTITY_FIELDS:
                value = str((identity or {}).get(key) or "").strip()
                if not value:
                    raise ValueError(
                        "machine identity needs machineToken/machineType/machineCode")
                clean[key] = value
            if isinstance((identity or {}).get("vmInfo"), dict):
                clean["vmInfo"] = identity["vmInfo"]
            clean["pinned_at"] = time.strftime("%Y/%m/%d %H:%M")
            data["machine_identity"] = clean
        save(accounts_dir, data)
        return machine_identity(accounts_dir)


# ----------------------------------------------------------- outbound proxy
# Panel-managed proxy mode for every outbound request (see qoder_net.py).
# "system" follows the OS settings; "manual" uses proxy_url; "direct" never
# proxies. Environment variables QD_PROXY_MODE / QD_PROXY_URL still win.

PROXY_MODES = ("system", "manual", "direct")


def proxy_config(accounts_dir):
    """Return {"mode": system|manual|direct, "url": str} (defaults to system)."""
    data = load(accounts_dir)
    mode = str(data.get("proxy_mode") or "system").strip().lower()
    if mode not in PROXY_MODES:
        mode = "system"
    return {"mode": mode, "url": str(data.get("proxy_url") or "").strip()}


def set_proxy_config(accounts_dir, mode, url):
    with _lock:
        data = load(accounts_dir)
        mode = str(mode or "system").strip().lower()
        data["proxy_mode"] = mode if mode in PROXY_MODES else "system"
        data["proxy_url"] = str(url or "").strip() if data["proxy_mode"] == "manual" else ""
        save(accounts_dir, data)


class PanelSessions(object):
    """In-memory bearer tokens handed out after a successful panel login.

    Deliberately not persisted: restarting the gateway logs browsers out, which
    is the safer default for a LAN tool that people expose behind a port map.
    """

    def __init__(self, ttl=SESSION_TTL):
        self.ttl = ttl
        self._tokens = {}
        self._lock = threading.RLock()

    def create(self):
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._tokens[token] = time.time() + self.ttl
        return token

    def valid(self, token):
        if not token:
            return False
        with self._lock:
            expiry = self._tokens.get(token)
            if not expiry:
                return False
            if expiry < time.time():
                self._tokens.pop(token, None)
                return False
            return True

    def revoke(self, token):
        if not token:
            return
        with self._lock:
            self._tokens.pop(token, None)

    def revoke_all(self):
        with self._lock:
            self._tokens.clear()
