"""qoder2api.api.accounts_routes —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import time

from .. import runtime, paths
from .. import accounts as qoder_accounts
from .. import tasks as qoder_tasks
from .. import realm as realm_mod
from .. import views
from ..logbus import log
import urllib.parse

from .. import upstream as upstream_mod
from ..views import account_views
from ..realm import acc_realm, save_persisted_realm
from ..upstream import (RateLimited, aggregate_stream)
from ..tasks import CHECKIN_MIN_GAP
from ..usage import billed_credit
from ..logbus import clear_logs


class AccountsRoutesMixin(object):

    def _handle_accounts(self, path, payload):
        """Account-management endpoints (dashboard uses these)."""
        if runtime.POOL is None:
            return self._error(503, "account pool unavailable")
        if path == "/accounts/import" and isinstance(payload, list):
            payload = {"data": payload}
        if not isinstance(payload, dict):
            return self._error(400, "expected a JSON object",
                               "invalid_request_error")
        if path in ("/accounts/credits", "/accounts/credits/fetch"):
            uid = payload.get("uid")
            realm = payload.get("realm")
            if uid:
                targets = [runtime.POOL.get(uid)]
            elif realm and realm != "all":
                targets = [a for a in runtime.POOL.accounts if a.realm == realm]
            else:
                targets = list(runtime.POOL.accounts)
            # ttl=秒：看板进入页面时的自动刷新用它走服务端 TTL（快照够新就不回源，
            # cached=True）；不传 = 手动刷新，始终回源。
            max_age = None
            if payload.get("ttl") is not None:
                try:
                    max_age = max(0.0, float(payload.get("ttl")))
                except (TypeError, ValueError):
                    max_age = None
            results = qoder_accounts.refresh_credits(targets, max_age=max_age)
            # 手动刷新额度后，看板 /tasks 也看到新快照（不吃 20s 面板缓存）
            qoder_tasks.invalidate_panel_cache()
            return self._json(200, {"results": results,
                                    "accounts": account_views()})
        if path == "/tasks/run":
            if not runtime.POOL:
                return self._json(200, {"ok": False, "msg": "账号池不可用"})
            uid = payload.get("uid")
            if uid and uid != "all":
                target = runtime.POOL.get(uid)
                if not target:
                    return self._json(200, {"ok": False,
                                            "msg": "未找到指定的账号"})
                targets = [target]
            else:
                # 不再按区域过滤：签到能力运行时探测，接口不存在的账号会在
                # 日志里给出明确原因（历史问题：国际版点签到完全没反应）。
                targets = [a for a in runtime.POOL.accounts
                           if a.enabled and a.access_token]
            if not targets:
                return self._json(200, {"ok": False, "msg": "未找到可用账号（账号已禁用或缺少凭证）"})
            res = qoder_tasks.run_batch_checkin(targets, gap=1.0)
            qoder_tasks.invalidate_panel_cache()   # 写操作后失效面板短缓存
            return self._json(200, {
                "ok": res["ok"],
                "credit_added": res["credit_added"],
                "logs": res["logs"],
                "accounts_count": res["accounts_count"],
            })
        if path == "/tasks/travel":
            # 看板福利按钮的对应动作：批量领取 Pro 福利包（官方仅国内版 sash 活动）
            if not runtime.POOL:
                return self._json(200, {"ok": False, "msg": "账号池不可用"})
            uid = payload.get("uid")
            if uid and uid != "all":
                target = runtime.POOL.get(uid)
                if not target:
                    return self._json(200, {"ok": False,
                                            "msg": "未找到指定的账号"})
                targets = [target]
            else:
                targets = [a for a in runtime.POOL.accounts
                           if a.enabled and a.access_token]
            if not targets:
                return self._json(200, {"ok": False, "msg": "未找到可用账号（账号已禁用或缺少凭证）"})
            res = qoder_tasks.run_batch_pro_claim(targets)
            qoder_tasks.invalidate_panel_cache()   # 写操作后失效面板短缓存
            return self._json(200, {
                "ok": True,
                "results": res["results"],
                "msg": res["msg"],
                "logs": res["logs"],
                "accounts_count": res["accounts_count"],
                "already_count": res.get("already_count", 0),
            })
        if path == "/scheduler/trigger":
            if runtime.SCHEDULER:
                return self._json(200, runtime.SCHEDULER.trigger_now())
            return self._json(200, {"ok": False, "msg": "调度器未初始化"})
        if path == "/scheduler/toggle":
            if runtime.SCHEDULER:
                runtime.SCHEDULER.enabled = not runtime.SCHEDULER.enabled
                runtime.SCHEDULER.log("用户切换调度器状态为: %s"
                              % ("启用" if runtime.SCHEDULER.enabled else "暂停"))
                return self._json(200, runtime.SCHEDULER.status())
            return self._json(200, {"ok": False, "msg": "调度器未初始化"})
        if path == "/logs/clear":
            clear_logs()
            return self._json(200, {"ok": True})
        if path == "/realm":
            # 支持 {"mode": intl|cn|both, "preferred": intl|cn}；
            # 旧客户端 {"realm": intl|cn} 仍可用（等价于单区模式）。
            mode = payload.get("mode")
            if mode is None:
                mode = payload.get("realm")
            if str(mode or "").strip().lower() not in ("intl", "cn", "both"):
                return self._error(400, "mode must be intl / cn / both",
                                   "invalid_request_error")
            save_persisted_realm(mode, payload.get("preferred"))
            return self._json(200, {"ok": True, "current": runtime.CURRENT_REALM,
                                    "mode": runtime.REALM_MODE,
                                    "preferred": runtime.REALM_PREFERRED,
                                    "persisted": True})
        if path == "/accounts/checkin":
            # 账号面板的「每日签到」按钮：**只做每日签到领积分**——Credits 类活动
            # （如"每天领 100 Credits"，旧 sash 接口兜底），不领券/兑换码类活动，
            # 也不领 Pro 福利包（那些走签到与福利中心的按钮）。
            uid = payload.get("uid")
            targets = [runtime.POOL.get(uid)] if uid else list(runtime.POOL.accounts)
            results = []
            last_at = None
            for account in targets:
                if account is None:
                    continue
                if not account.enabled or not account.access_token:
                    continue            # 跳过的账号不占用防风控等待
                if last_at is not None:
                    # 账号之间保持 >= CHECKIN_MIN_GAP（与 tasks 声明的 >=1.0s
                    # 一致）；try/finally 让失败路径同样计时间隔。
                    idle = CHECKIN_MIN_GAP - (time.time() - last_at)
                    if idle > 0:
                        time.sleep(idle)
                try:
                    res = qoder_tasks.run_checkin(account, gap=CHECKIN_MIN_GAP,
                                                  only_daily=True)
                finally:
                    last_at = time.time()
                # 签到后顺手刷新额度快照：账号行的积分列与"更新于"随点击更新
                try:
                    account.fetch_credits()
                except Exception:
                    pass
                results.append({
                    "uid": account.uid,
                    "nickname": account.nickname,
                    "ok": bool(res.get("ok")),
                    "earned_credit": res.get("earned_credit") or 0,
                    "msg": (res.get("logs") or [""])[-1],
                    "logs": res.get("logs") or [],
                    "credits": res.get("credits"),
                    # 活动平台的精确结论（upstream issue #20）：ok 只代表"这次
                    # 请求没报错"，是否真到账要看 claimed / earned_credit。
                    # 新增而非替换：msg/logs/credits 原样保留；旧 sash 兜底路径
                    # 没有这些字段时是 None，由前端兼容。
                    "message": res.get("message"),
                    "claimed": res.get("claimed"),
                    "next_available_at": res.get("next_available_at"),
                    "next_available_note": res.get("next_available_note"),
                })
            qoder_tasks.invalidate_panel_cache()   # 写操作后失效面板短缓存
            return self._json(200, {"results": results,
                                    "accounts": account_views()})
        if path == "/accounts/login/start":
            platform = payload.get("platform") or "CLI"
            target_realm = payload.get("realm") or runtime.CURRENT_REALM
            if target_realm not in ("intl", "cn"):
                target_realm = "cn"
            try:
                started = runtime.POOL.start_login(realm=target_realm,
                                           platform=platform)
            except Exception as exc:
                return self._error(502, "could not start login: %s" % exc)
            log("oauth device login started (realm=%s, state=%s)"
                % (target_realm, started["state"][:8]))
            return self._json(200, started)
        if path == "/accounts/login/cancel":
            state = payload.get("state") or ""
            return self._json(200, {"cancelled": runtime.POOL.cancel_login(state)})
        if path == "/accounts/import/pat":
            pat = payload.get("pat") or ""
            realm = payload.get("realm") or runtime.CURRENT_REALM
            if realm not in ("intl", "cn"):
                realm = "cn"
            try:
                account = runtime.POOL.import_pat(pat, realm=realm)
            except Exception as exc:
                return self._error(400, "PAT import failed: %s" % exc)
            log("imported PAT account %s (realm=%s)"
                % (account.uid[:8], realm))
            return self._json(200, {"imported": [account.public()],
                                    "accounts": account_views()})
        if path == "/accounts/import/desktop":
            # 两步确认：{} 只读扫描（双区：桌面 App auth.v1.dat + CLI user）；
            # {"path":...} 按确认导入该凭证；{"all":true} 导入全部有效项。
            target_path = payload.get("path")
            if target_path:
                realm = payload.get("realm")
                try:
                    account = qoder_accounts.import_desktop_credential(
                        path=target_path, realm=realm)
                except Exception as exc:
                    return self._error(400, "import failed: %s" % exc)
                log("imported %s (%s) from local client credential"
                    % (account.uid[:8], account.realm), tag="accounts")
                return self._json(200, {
                    "imported": [account.public()],
                    "accounts": account_views(),
                    "pool_uids": [a.uid for a in runtime.POOL.accounts],
                })
            if payload.get("all"):
                try:
                    imported = qoder_accounts.import_desktop_credential()
                except Exception as exc:
                    return self._error(400, "import failed: %s" % exc)
                for account in imported:
                    log("imported %s (%s) from local client credential"
                        % (account.uid[:8], account.realm), tag="accounts")
                return self._json(200, {
                    "imported": [a.public() for a in imported],
                    "accounts": account_views(),
                    "pool_uids": [a.uid for a in runtime.POOL.accounts],
                })
            try:
                detected = qoder_accounts.scan_desktop_credentials()
            except Exception as exc:
                detected = []
                log("desktop credential scan failed: %s" % exc)
            return self._json(200, {
                "detected": detected,
                "accounts": account_views(),
                "pool_uids": [a.uid for a in runtime.POOL.accounts],
            })
        if path == "/accounts/refresh":
            uid = payload.get("uid")
            targets = [runtime.POOL.get(uid)] if uid else list(runtime.POOL.accounts)
            results = []
            for account in targets:
                if account is None:
                    continue
                ok = account.refresh()
                account.save(runtime.ACCOUNTS_DIR)
                results.append({"uid": account.uid, "ok": ok,
                                "error": account.last_error})
            return self._json(200, {"results": results})
        if path == "/accounts/test":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            account = runtime.POOL.get(uid)
            if not account:
                return self._error(404, "no such account")
            test_model = payload.get("model") or "auto"
            test_payload = {
                "model": test_model,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": False,
            }
            t0 = time.time()
            try:
                resp, acc, _ = upstream_mod.open_upstream(test_payload, target_realm=acc_realm(account))
                with resp:
                    chat_obj = aggregate_stream(resp, test_model, None)
                wall_ms = int((time.time() - t0) * 1000)
                choices = chat_obj.get("choices") or []
                msg = (choices[0].get("message") or {}) if choices else {}
                reply_text = (msg.get("content") or msg.get("reasoning_content")
                              or "OK").strip()
                if len(reply_text) > 80:
                    reply_text = reply_text[:77] + "..."
                account.clear_error()
                # 用量块一并回报：调免费模型（Qwen3.8-Flash）时 billable=false、
                # 实扣积分为 0，面板据此显示「0 积分（免费）」，不虚报消耗。
                usage_block = chat_obj.get("usage") or {}
                log("account test: uid=%s model=%s wall=%dms ok=True credits=%s"
                    % (account.uid[:8], test_model, wall_ms,
                       billed_credit(usage_block)), tag="accounts")
                return self._json(200, {"ok": True, "uid": account.uid,
                                        "model": test_model,
                                        "elapsed_ms": wall_ms,
                                        "reply": reply_text,
                                        "credits": billed_credit(usage_block),
                                        "billable": usage_block.get("billable")
                                        is not False,
                                        "total_tokens": usage_block.get("total_tokens") or 0})
            except RateLimited as exc:
                wall_ms = int((time.time() - t0) * 1000)
                return self._json(200, {"ok": False, "uid": account.uid,
                                        "status": 429,
                                        "error": "rate limited: %s" % exc.detail[:150],
                                        "elapsed_ms": wall_ms})
            except urllib.error.HTTPError as exc:
                wall_ms = int((time.time() - t0) * 1000)
                try:
                    detail = exc.read(400).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                account.note_error("HTTP %d: %s" % (exc.code, detail[:80]),
                                   cooldown=60)
                log("account test: uid=%s model=%s wall=%dms error=%d"
                    % (account.uid[:8], test_model, wall_ms, exc.code),
                    level="WARN", tag="accounts")
                return self._json(200, {"ok": False, "uid": account.uid,
                                        "status": exc.code,
                                        "error": "HTTP %d: %s"
                                        % (exc.code, detail[:150]),
                                        "elapsed_ms": wall_ms})
            except Exception as exc:
                wall_ms = int((time.time() - t0) * 1000)
                account.note_error(str(exc)[:80], cooldown=60)
                log("account test: uid=%s model=%s wall=%dms exc=%s"
                    % (account.uid[:8], test_model, wall_ms, exc),
                    level="WARN", tag="accounts")
                return self._json(200, {"ok": False, "uid": account.uid,
                                        "status": 500, "error": str(exc),
                                        "elapsed_ms": wall_ms})
        if path == "/accounts/set":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            updated = runtime.POOL.set_enabled(uid, bool(payload.get("enabled")))
            if updated is None:
                return self._error(404, "no such account")
            log("account %s %s" % (uid[:8],
                                   "enabled" if payload.get("enabled")
                                   else "disabled"))
            return self._json(200, {"account": updated})
        if path == "/accounts/set-all":
            runtime.POOL.set_all_enabled(bool(payload.get("enabled")))
            return self._json(200, {"accounts": account_views()})
        if path == "/accounts/delete":
            uid = payload.get("uid")
            if not uid:
                return self._error(400, "uid required")
            removed = runtime.POOL.remove(uid)
            log("account %s deleted" % uid[:8])
            return self._json(200, {"deleted": removed,
                                    "accounts": account_views()})
        if path == "/accounts/import":
            # 支持的文档形态见 qoder_accounts._coerce_account_rows。
            # 选项：dryRun / overwrite / realm ("intl"|"cn")
            blob = payload.get("data") if "data" in payload else payload
            if not isinstance(blob, (dict, list)):
                return self._error(400, "the document must be a JSON object "
                                   "or array", "invalid_request_error")
            rows, problem = qoder_accounts._coerce_account_rows(blob)
            if problem:
                return self._error(400, "cannot read the document: %s" % problem,
                                   "invalid_request_error")
            dry_run = bool(payload.get("dryRun"))
            overwrite = bool(payload.get("overwrite"))
            forced_realm = (payload.get("realm") or "").strip().lower() or None
            if forced_realm and forced_realm not in ("intl", "cn"):
                return self._error(400, "realm must be intl or cn",
                                   "invalid_request_error")
            if dry_run:
                return self._json(200, {
                    "dryRun": True,
                    "count": len(rows),
                    "result": runtime.POOL.preview_import_rows(rows, realm=forced_realm,
                                                       overwrite=overwrite),
                    "accounts": account_views(),
                })
            report = runtime.POOL.import_rows(rows, realm=forced_realm,
                                      overwrite=overwrite)
            log("account import: %d added, %d updated, %d skipped, %d invalid"
                % (len(report["added"]), len(report["updated"]),
                   len(report["skipped"]), len(report["invalid"])))
            return self._json(200, {
                "count": len(rows),
                "result": report,
                "accounts": account_views(),
            })
        return self._error(404, "unknown account endpoint",
                           "invalid_request_error")
