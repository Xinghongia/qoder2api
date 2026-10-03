"""qoder2api.views —— 由 qoder_proxy.py 拆分而来。"""

import time

from . import paths, runtime
from . import accounts as qoder_accounts
from . import settings as qoder_settings
from . import net as qoder_net
from . import catalog as qoder_catalog
from . import VERSION
from .auth import auth_required, configured_keys
from .accounts import runtime_info_exe




def account_views(realm=None):
    """List view of every account, including a live readiness flag."""
    if not runtime.POOL:
        return []
    return runtime.POOL.list_public(realm=realm)


def machine_identity_view():
    """看板「机器身份」状态：是否已固定 / 生效来源 / 桥是否可用。"""
    pin = qoder_settings.machine_identity(runtime.ACCOUNTS_DIR)
    bridge = bool(runtime_info_exe(runtime.CURRENT_REALM))
    view = {
        "pinned": bool(pin),
        "pinned_at": (pin or {}).get("pinned_at") or "",
        "bridge_available": bridge,
        "effective_source": ("pinned" if pin
                             else ("runtime-info" if bridge else "derived")),
    }
    if pin:
        view["preview"] = ("%s…%s" % (pin["machineToken"][:8],
                                      pin["machineToken"][-6:]))
    return view


def runtime_settings_view():
    """Current panel-visible settings (never returns the password or the key)."""
    key = runtime.API_KEY or ""
    if len(key) > 8:
        masked = key[:4] + "*" * 6 + key[-4:]
    else:
        masked = "*" * len(key)
    keys = []
    for entry in configured_keys():
        raw = entry.get("key") or ""
        keys.append({
            "id": entry.get("id") or "",
            "name": entry.get("name") or "",
            "realm": entry.get("realm") or "",
            "enabled": entry.get("enabled", True) is not False,
            "masked": (raw[:4] + "*" * 6 + raw[-4:]) if len(raw) > 8
            else "*" * len(raw),
            "source": entry.get("source") or "panel",
            "created_at": entry.get("created_at") or "",
        })
    return {
        "panel_password_is_default":
            qoder_settings.panel_password_is_default(runtime.ACCOUNTS_DIR),
        "api_key_set": bool(key),
        "api_key_set_by_panel": runtime.API_KEY_FILE_SET,
        "api_key_masked": masked,
        "auth_required": auth_required(),
        "api_keys": keys,
        "model_overrides": qoder_settings.model_overrides(runtime.ACCOUNTS_DIR),
        "proxy": qoder_net.describe(),
        "machine_identity": machine_identity_view(),
        "accounts_dir": runtime.ACCOUNTS_DIR,
        "usage_dir": runtime.USAGE_DIR,
        "settings_file": qoder_settings.settings_path(runtime.ACCOUNTS_DIR),
        "version": VERSION,
    }


def current_account():
    """Account used for display purposes (health / usage summaries)."""
    return runtime.POOL.representative() if runtime.POOL else None
