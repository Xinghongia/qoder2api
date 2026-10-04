"""qoder2api.api.routes_get —— 由 qoder_proxy.py 拆分而来。"""

import json
import time
import urllib.parse

from .. import realm, runtime, usage, views, models as models_mod
from .. import catalog as qoder_catalog
from .. import accounts as qoder_accounts
from .. import tasks as qoder_tasks
from ..logbus import log
from ..errors import UpstreamStatus
from .. import update as update_mod
import os
from urllib.parse import urlparse, parse_qs

from .. import VERSION
from .. import settings as qoder_settings
from ..views import account_views, current_account, runtime_settings_view
from ..auth import configured_keys
from ..security import cors_origin_allowed
from ..accounts import (export_bridge_identity, local_vm_status)
from ..update import check_for_update
from ..models import fetch_models
from ..model_entry import model_entry
from ..usage import (compute_usage_analytics, perf_stats,
                     recent_usage, usage_by_account, usage_snapshot)
from ..logbus import get_logs


class GetRoutesMixin(object):

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        query = parse_qs(parsed.query)
        from . import static as static_mod
        if path in ("/", "/dashboard", "/ui", "/legacy"):
            return self._dashboard(path)
        # 「/logs」「/settings」既是接口也是页面：无尾斜杠 + 浏览器导航（地址栏
        # 手输、书签、旧链接）时重定向到带斜杠的页面；接口调用（fetch）不带
        # Sec-Fetch-Mode: navigate，照常走下面。query 原样保留（?key= 引导）。
        if (not path.endswith("/") and static_mod.is_browser_navigation(self.headers)
                and static_mod.page_exists(path)):
            location = path + "/" + (("?" + parsed.query) if parsed.query else "")
            return self._redirect(location)
        # 前端静态页面（/stats/、/logs/、/settings/ 等导出路由）优先直出，
        # 不参与面板鉴权；接口路径（/logs、/settings/... 无尾斜杠）继续走下面。
        if not static_mod.api_takes_path(path):
            _fp, _lc = static_mod.resolve(path)
            if _fp is not None:
                return self._send_static(_fp, long_cache=_lc)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required",
                               "invalid_request_error")
        if path == "/panel/status":
            # 不带 token 也要回答：看板需要先知道是否显示登录页。
            info = {
                "panel_password_required": True,
                "panel_password_is_default":
                    qoder_settings.panel_password_is_default(runtime.ACCOUNTS_DIR),
                "authenticated": self._panel_ok(),
            }
            info["api_key_set"] = bool(runtime.API_KEY)
            return self._json(200, info)
        if path in ("/ping", "/healthz", "/livez", "/readyz"):
            # 极简探活端点：**不需要面板密码/API Key、不查账号池**。
            # 客户端/守护脚本常用 GET /ping 判活，之前返回 404 会被判成
            # “网关不可用” → 反复重连。这里返回纯文本 pong。
            body = b"pong\n"
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            if cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            return self.wfile.write(body)
        if path == "/health":
            rep = current_account()
            info = {
                "ok": True,
                "service": "qoder-proxy",
                "version": VERSION,
                "realm": runtime.CURRENT_REALM,
                "accounts": len(runtime.POOL.accounts) if runtime.POOL else 0,
                "accounts_ready": runtime.POOL.count_ready() if runtime.POOL else 0,
                "api_key_required": bool(runtime.API_KEY),
            }
            if self._key_ok():
                info.update({
                    "uid": rep.uid if rep else None,
                    "domain": rep.domain if rep else None,
                    "issuer": "qoder" if rep else None,
                    "credential_file": os.path.basename(rep.path)
                    if rep and rep.path else None,
                    "expires_at": rep.expires_at if rep else None,
                })
            return self._json(200, info)
        if path == "/realm":
            return self._json(200, {"current": runtime.CURRENT_REALM,
                                    "mode": runtime.REALM_MODE,
                                    "preferred": runtime.REALM_PREFERRED,
                                    "options": ["intl", "cn"]})
        if path == "/diag/vm":
            # 本机虚拟化检测（看板「签到与福利中心」展示；中文输出）。
            # 以官方风控桥 runtime-info.exe 的 vmInfo 为准，桥不可用时本机交叉校验。
            _q = parse_qs(urlparse(self.path).query)
            try:
                return self._json(200, local_vm_status(
                    _q.get("realm", [None])[0] or runtime.CURRENT_REALM,
                    force=bool(_q.get("force", [None])[0])))
            except Exception as exc:
                return self._error(500, "vm status failed: %s" % exc)
        if path == "/identity/export":
            # 导出本机机器身份（给没有官方客户端的机器固定用；面板鉴权）。
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            ident, reason = export_bridge_identity(runtime.CURRENT_REALM)
            return self._json(200, {
                "ok": bool(ident), "identity": ident or None,
                "reason": reason,
                "note": "在目标机器看板「设置 → 机器身份」粘贴保存即可固定"})
        if path == "/update/check":
            # 项目新版本检测（看板「运行信息 · 新版本检测」；缓存 6h，force=1 强刷）
            _q = parse_qs(urlparse(self.path).query)
            try:
                return self._json(200, check_for_update(
                    force=bool(_q.get("force", [None])[0])))
            except Exception as exc:
                return self._error(500, "update check failed: %s" % exc)
        if path in ("/v1/models", "/models"):
            if not self._authorized():
                return
            # Key 绑定的出口优先；未绑定（或显式值非法）时按网关出口模式：
            # 双区 = 两区模型并集（每条标注 realm/realms，还能发现另一区模型），
            # 单区 = 该区清单。看板始终显式带 ?realm=，展示不受影响。
            req_realm = self._request_realm()
            if req_realm not in ("intl", "cn"):
                req_realm = "both" if runtime.REALM_MODE == "both" else runtime.REALM_MODE
            try:
                entries = fetch_models(realm=req_realm)
            except Exception as exc:
                return self._error(502, str(exc))
            data = [model_entry(mid, meta) for mid, meta in entries]
            return self._json(200, {"object": "list", "data": data,
                                    "realm": req_realm})
        if path in ("/usage", "/v1/usage"):
            if not self._authorized():
                return
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or runtime.CURRENT_REALM
            return self._json(200, usage_snapshot(realm=req_realm))
        if path == "/usage/recent":
            if not self._authorized():
                return
            try:
                limit = max(1, min(1000, int((query.get("limit") or ["100"])[0])))
            except ValueError:
                limit = 100
            try:
                page = max(1, int((query.get("page") or ["1"])[0]))
            except ValueError:
                page = 1
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or runtime.CURRENT_REALM
            return self._json(200, recent_usage(limit, realm=req_realm, page=page))
        if path == "/accounts/credits":
            if not self._authorized():
                return
            for a in (runtime.POOL.accounts if runtime.POOL else []):
                a.fetch_credits()
            return self._json(200, {"accounts": account_views()})
        if path == "/accounts":
            if not self._authorized():
                return
            return self._json(200, {
                "accounts": account_views(realm=query.get("realm", [None])[0]
                                          or runtime.CURRENT_REALM),
                "storage": runtime.ACCOUNTS_DIR,
                "usable": runtime.POOL.count_ready() if runtime.POOL else 0,
            })
        if path == "/accounts/export":
            if not self._authorized():
                return
            realm = (query.get("realm") or [None])[0] or None
            if realm not in ("intl", "cn"):
                realm = None
            include_secrets = (query.get("secrets") or ["1"])[0] \
                not in ("0", "false", "no")
            uids = []
            for raw in query.get("uid") or []:
                uids.extend(part.strip() for part in str(raw).split(",")
                            if part.strip())
            if uids:
                known = {a.uid for a in (runtime.POOL.accounts if runtime.POOL else [])}
                missing = [u for u in uids if u not in known]
                if missing:
                    return self._error(404, "no such account: %s"
                                       % ", ".join(missing[:5]),
                                       "invalid_request_error")
            doc = qoder_accounts.build_export_document(
                runtime.POOL.accounts if runtime.POOL else [],
                realm=realm, include_secrets=include_secrets,
                uids=uids or None)
            if (query.get("download") or ["0"])[0] in ("1", "true", "yes"):
                stamp = time.strftime("%Y%m%d-%H%M%S")
                if len(uids) == 1:
                    label = uids[0][:8]
                else:
                    label = realm + "-" if realm else ""
                name = "qoder-accounts-%s%s.json" % (label, stamp)
                return self._download(name, doc)
            return self._json(200, doc)
        if path == "/accounts/login/poll":
            if not self._authorized():
                return
            state = (query.get("state") or [""])[0]
            return self._json(200, runtime.POOL.poll_login(state))
        if path == "/usage/analytics":
            if not self._authorized():
                return
            return self._json(200, compute_usage_analytics())
        if path == "/usage/by-account":
            if not self._authorized():
                return
            return self._json(200, {"accounts": usage_by_account()})
        if path == "/usage/perf":
            if not self._authorized():
                return
            try:
                sample = max(10, min(20000,
                                     int((query.get("sample") or ["5000"])[0])))
            except ValueError:
                sample = 5000
            req_realm = query.get("realm", [None])[0] \
                or self.headers.get("X-Realm") or runtime.CURRENT_REALM
            return self._json(200, perf_stats(sample, realm=req_realm))
        if path == "/tasks":
            if not self._authorized():
                return
            from qoder2api import tasks as qoder_tasks
            uid = (query.get("uid") or [None])[0]
            view = qoder_tasks.fetch_tasks_view(runtime.POOL, uid=uid)
            if view.get("msg") and not view.get("tasks"):
                return self._json(200, view)
            return self._json(200, view)
        if path == "/scheduler":
            if not self._authorized():
                return
            return self._json(200, runtime.SCHEDULER.status() if runtime.SCHEDULER
                              else {"enabled": False, "msg": "未运行"})
        if path == "/settings":
            if not self._authorized():
                return
            return self._json(200, runtime_settings_view())
        if path == "/logs":
            if not self._authorized():
                return
            try:
                limit = int(query.get("limit", ["200"])[0])
            except (ValueError, TypeError):
                limit = 200
            level = query.get("level", [""])[0]
            tag = query.get("tag", [""])[0]
            search = query.get("search", [""])[0]
            try:
                since_id = int(query.get("since_id", ["0"])[0])
            except (ValueError, TypeError):
                since_id = 0
            return self._json(200, get_logs(limit=limit, level=level, tag=tag,
                                            search=search, since_id=since_id))
        if path == "/logs/export":
            if not self._authorized():
                return
            log_data = get_logs(limit=5000)
            lines = ["[%s] [%s] [%s] %s" % (item["ts"], item["level"],
                                             item["tag"], item["msg"])
                     for item in log_data["logs"]]
            text_content = "\n".join(lines).encode("utf-8")
            filename = "qd-proxy-%s.log" % time.strftime("%Y%m%d-%H%M%S")
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition",
                             'attachment; filename="%s"' % filename)
            self.send_header("Content-Length", str(len(text_content)))
            if cors_origin_allowed(self.path):
                self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(text_content)
            return
        if path == "/settings/reveal":
            # 看板只画掩码 Key，复制明文需要显式请求；必须面板会话。
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            wanted = (query.get("id") or [""])[0]
            for entry in configured_keys():
                if entry.get("id") == wanted:
                    return self._json(200, {"id": wanted,
                                            "key": entry.get("key") or ""})
            return self._error(404, "no such key", "invalid_request_error")
        # 未命中的非接口路径：交给前端静态兜底（未知路由 404）。
        return self._dashboard(path)
