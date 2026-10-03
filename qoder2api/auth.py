"""qoder2api.auth —— 由 qoder_proxy.py 拆分而来。"""

from . import runtime, settings as qoder_settings
from .logbus import log




def configured_keys():
    """Panel-managed API keys, always read fresh so panel edits apply at once."""
    try:
        return qoder_settings.api_keys(runtime.ACCOUNTS_DIR)
    except Exception as exc:
        log("could not read api keys: %s" % exc)
        return []


def auth_required():
    """Whether /v1 calls must present a key at all."""
    if qoder_settings.auth_disabled(runtime.ACCOUNTS_DIR):
        return False
    if any(entry.get("enabled") for entry in configured_keys()):
        return True
    return bool(runtime.API_KEY)


def identify_key(supplied):
    """Return the key entry a caller used, or None when nothing matches.

    Once the panel has at least one key, those keys are the only accepted
    credentials - otherwise a launcher key left in a .bat file would silently
    keep working after the panel was locked down.
    """
    extra = () if configured_keys() else (runtime.API_KEY,)
    return qoder_settings.match_api_key(runtime.ACCOUNTS_DIR, supplied, extra_keys=extra)
