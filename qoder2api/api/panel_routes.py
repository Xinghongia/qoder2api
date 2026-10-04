"""qoder2api.api.panel_routes —— 由 qoder_proxy.py 拆分而来。"""

import json

from .. import runtime
from .. import settings as qoder_settings
from .. import views
from ..logbus import log
import time

from .. import net as qoder_net
from ..auth import configured_keys
from ..views import machine_identity_view, runtime_settings_view
from ..security import (_login_attempts, _login_lock,
                        _prune_login_attempts)


class PanelRoutesMixin(object):

    def _handle_settings_save(self):
        """Persist panel-managed settings from the web settings tab."""
        payload = self._payload_or_error()
        if payload is None:
            return
        reply = {}
        if "api_keys" in payload:
            raw = payload.get("api_keys")
            if not isinstance(raw, list):
                return self._error(400, "api_keys must be a list",
                                   "invalid_request_error")
            # 面板只画掩码 Key：空值表示“保留原值”，不是“清空”。
            existing = {entry.get("id"): entry for entry in configured_keys()}
            cleaned = []
            for index, item in enumerate(raw):
                if not isinstance(item, dict):
                    return self._error(400, "each api key must be an object",
                                       "invalid_request_error")
                entry_id = str(item.get("id") or "").strip()
                value = str(item.get("key") or "").strip()
                if not value and entry_id and entry_id in existing:
                    value = existing[entry_id].get("key") or ""
                if not entry_id:
                    entry_id = "k%d" % index
                if value and len(value) < 4:
                    return self._error(400,
                                       "api key must be at least 4 characters",
                                       "invalid_request_error")
                if not value:
                    return self._error(400,
                                       "a key entry is empty - fill it in or "
                                       "remove the row",
                                       "invalid_request_error")
                realm = str(item.get("realm") or "").strip().lower()
                if realm not in ("", "intl", "cn"):
                    return self._error(400, "realm must be intl, cn or empty",
                                       "invalid_request_error")
                created_at = item.get("created_at") \
                    or (existing.get(entry_id, {}).get("created_at")
                        if entry_id in existing else None) \
                    or time.strftime("%Y/%m/%d %H:%M")
                cleaned.append({
                    "id": entry_id,
                    "name": str(item.get("name") or "").strip(),
                    "key": value,
                    "realm": realm,
                    "enabled": item.get("enabled", True) is not False,
                    "created_at": created_at,
                })
            qoder_settings.set_api_keys(runtime.ACCOUNTS_DIR, cleaned)
            reply["api_keys_saved"] = len(cleaned)
        if "panel_username" in payload:
            # 登录用户名（默认 admin）：与密码一起构成登录凭据。
            try:
                reply["panel_username"] = qoder_settings.set_panel_username(
                    runtime.ACCOUNTS_DIR, payload.get("panel_username"))
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
        if "auth_disabled" in payload:
            qoder_settings.set_auth_disabled(runtime.ACCOUNTS_DIR,
                                             payload.get("auth_disabled"))
            reply["auth_disabled"] = bool(payload.get("auth_disabled"))
        if "model_overrides" in payload:
            # 看板模型库改默认值：{key: {context_window, effort}}，
            # 空字符串/0 表示删除该项（恢复跟随官方默认）。
            patch = payload.get("model_overrides")
            if not isinstance(patch, dict):
                return self._error(400, "model_overrides must be an object",
                                   "invalid_request_error")
            saved = {}
            for key, value in patch.items():
                if not isinstance(value, dict):
                    return self._error(400,
                                       "each model override must be an object",
                                       "invalid_request_error")
                try:
                    item = qoder_settings.set_model_override(
                        runtime.ACCOUNTS_DIR, key,
                        context_window=value.get("context_window"),
                        effort=value.get("effort"))
                except (TypeError, ValueError) as exc:
                    return self._error(400, "bad model override '%s': %s"
                                       % (key, exc), "invalid_request_error")
                saved[key] = item
            reply["model_overrides_saved"] = saved
            reply["model_overrides"] = qoder_settings.model_overrides(
                runtime.ACCOUNTS_DIR)
            log("model override updated: %s"
                % (", ".join("%s -> %s" % (k, v) for k, v in saved.items())
                   or "(nothing to change)"))
        new_key = payload.get("api_key")
        if new_key is not None:
            new_key = str(new_key).strip()
            if new_key and len(new_key) < 4:
                return self._error(400, "api key must be at least 4 characters",
                                   "invalid_request_error")
            
            qoder_settings.set_api_key(runtime.ACCOUNTS_DIR, new_key)
            runtime.API_KEY = new_key
            runtime.API_KEY_FILE_SET = True
            reply["api_key_set"] = bool(new_key)
        if "proxy_mode" in payload:
            mode = str(payload.get("proxy_mode") or "").strip().lower()
            url = str(payload.get("proxy_url") or "").strip()
            if mode not in qoder_net.MODES:
                return self._error(400,
                                   "proxy_mode must be system / manual / direct",
                                   "invalid_request_error")
            try:
                qoder_net.configure(mode, url)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            qoder_settings.set_proxy_config(runtime.ACCOUNTS_DIR, mode, url)
            reply["proxy_saved"] = qoder_net.describe()["effective"]
            log("proxy      : %s" % reply["proxy_saved"])
        if "machine_identity" in payload:
            # 固定/清除机器身份（看板「设置 → 机器身份」）：
            # 值为 {machineToken, machineType, machineCode[, vmInfo]} 或 null。
            value = payload.get("machine_identity")
            if value is not None and not isinstance(value, dict):
                return self._error(400, "machine_identity must be an object or null",
                                   "invalid_request_error")
            try:
                saved_ident = qoder_settings.set_machine_identity(
                    runtime.ACCOUNTS_DIR, value)
            except ValueError as exc:
                return self._error(400, str(exc), "invalid_request_error")
            reply["machine_identity"] = machine_identity_view()
            log("machine identity %s" % (
                "pinned" if saved_ident else "cleared"))
        if payload.get("restart_scheduler"):
            if runtime.SCHEDULER:
                runtime.SCHEDULER.stop()
                runtime.SCHEDULER.start()
            reply["scheduler"] = "restarted"
        reply.update(runtime_settings_view())
        return self._json(200, reply)

    def _handle_panel(self, path):
        """Panel login, logout and the settings screen (password)."""
        payload = self._payload_or_error()
        if payload is None:
            return
        if path == "/panel/login":
            client_ip = self.client_address[0] \
                if hasattr(self, "client_address") and self.client_address \
                else "127.0.0.1"
            now = time.time()
            with _login_lock:
                _prune_login_attempts(now)
                attempts = [t for t in _login_attempts.get(client_ip, [])
                            if now - t < 60]
                _login_attempts[client_ip] = attempts
                if len(attempts) >= 5:
                    wait_sec = int(60 - (now - attempts[0]))
                    return self._error(429,
                                       "too many login attempts, please wait %ds"
                                       % max(1, wait_sec),
                                       "rate_limit_error")
            username = payload.get("username")
            password = str(payload.get("password") or "")
            # 登录页发 {username, password}：用户名不符直接失败（不透露是哪一项错）。
            # 旧客户端（旧看板 dashboard.html、脚本）只发 password，视为默认用户名，
            # 保持兼容——真正的秘密始终是密码。
            if username is not None:
                expected = qoder_settings.panel_username(runtime.ACCOUNTS_DIR)
                if str(username).strip() != expected:
                    with _login_lock:
                        _login_attempts.setdefault(client_ip, []).append(now)
                    time.sleep(0.5)   # 撞库缓解
                    return self._error(401, "invalid username or password",
                                       "invalid_request_error")
            if not qoder_settings.verify_panel_password(runtime.ACCOUNTS_DIR, password):
                with _login_lock:
                    _login_attempts.setdefault(client_ip, []).append(now)
                time.sleep(0.5)   # 撞库缓解
                return self._error(401, "invalid panel password",
                                   "invalid_request_error")
            with _login_lock:
                _login_attempts.pop(client_ip, None)
            token = runtime.PANEL.create()
            return self._json(200, {
                "ok": True,
                "token": token,
                "using_default_password":
                    qoder_settings.panel_password_is_default(runtime.ACCOUNTS_DIR),
            })
        if path == "/panel/logout":
            runtime.PANEL.revoke(self._panel_token())
            return self._json(200, {"ok": True})
        # 之后所有路由都要面板会话
        if not self._panel_ok():
            return self._error(401, "panel password required",
                               "invalid_request_error")
        if path == "/panel/password":
            current = str(payload.get("current") or "")
            new = str(payload.get("new") or "")
            if not qoder_settings.verify_panel_password(runtime.ACCOUNTS_DIR, current):
                return self._error(401, "current password is wrong",
                                   "invalid_request_error")
            if len(new) < 4:
                return self._error(400, "new password must be at least 4 characters",
                                   "invalid_request_error")
            qoder_settings.set_panel_password(runtime.ACCOUNTS_DIR, new)
            if new != qoder_settings.DEFAULT_PANEL_PASSWORD:
                # 换密码使其它浏览器会话全部失效
                runtime.PANEL.revoke_all()
            token = runtime.PANEL.create()
            return self._json(200, {"ok": True, "token": token})
        return self._error(404, "not found", "invalid_request_error")
