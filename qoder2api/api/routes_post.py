"""qoder2api.api.routes_post —— 由 qoder_proxy.py 拆分而来。"""

import json
import time

from .. import runtime, realm as realm_mod, usage, affinity
from .. import errors
from ..errors import UpstreamStatus
from ..logbus import log
import urllib.parse

from .. import upstream as upstream_mod
from ..affinity import prompt_fingerprint
from ..usage import record_error, record_usage
from ..chat_normalize import (normalize_tool_choice, normalize_tools,
                              translate_max_completion_tokens)
from ..upstream import (RateLimited, TRANSIENT_MAX_RETRIES,
                        _handle_envelope_account_cooldown,
                        _to_int_status, aggregate_with_envelope_retry,
                        extract_session_key, friendly_upstream_error,
                        iter_inner_sse,
                        should_retry_envelope, sse_with_heartbeat)


class PostRoutesMixin(object):

    def do_POST(self):
        path = self.path.split("?")[0]
        if path == "/settings/save":
            if not self._panel_ok():
                return self._error(401, "panel password required",
                                   "invalid_request_error")
            return self._handle_settings_save()
        if path in ("/panel/login", "/panel/logout", "/panel/password"):
            return self._handle_panel(path)
        if self._is_panel_route(path) and not self._panel_ok():
            return self._error(401, "panel password required",
                               "invalid_request_error")
        is_account_route = (
            path.startswith("/accounts/")
            or path == "/realm"
            or path.startswith("/tasks")
            or path.startswith("/scheduler")
            or path.startswith("/logs")
        )
        if not is_account_route and path not in (
                "/v1/chat/completions", "/chat/completions",
                "/v1/completions", "/completions",
                "/v1/responses", "/responses"):
            return self._error(404, "not found", "invalid_request_error")
        if not self._authorized():
            return
        payload = self._payload_or_error(allow_list=(path == "/accounts/import"))
        if payload is None:
            return
        if is_account_route:
            return self._handle_accounts(path, payload)
        if path in ("/v1/responses", "/responses"):
            return self._handle_responses(payload)

        # ---- Chat Completions 主链路 ----
        normalize_tool_choice(payload)
        normalize_tools(payload)
        translate_max_completion_tokens(payload)
        model = payload.get("model") or "auto"
        payload.setdefault("stream", False)
        # 保持原始终端 stream 意图
        want_stream = bool(payload.get("stream"))
        # 转换统一在 build_qoder_body 内做（压平/清洗/工具）
        session_key = extract_session_key(self.headers, payload)
        fp = prompt_fingerprint(payload.get("messages"))
        t_start = time.time()
        effort = payload.get("reasoning_effort") or \
            (payload.get("reasoning") or {}).get("effort") \
            if isinstance(payload.get("reasoning"), dict) \
            else payload.get("reasoning_effort")
        log("chat: model=%s client_effort=%r stream=%s msgs=%d"
            % (model, effort, want_stream,
               len(payload.get("messages") or [])))
        try:
            # None = 未显式绑定出口：交给 open_upstream 按面板模式路由
            #（双区=优先出口失效时自动切换）
            req_realm = self._request_realm()
            blocked = self._cross_realm_error(model, req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            upstream, account, _ = upstream_mod.open_upstream(payload, session_key=session_key,
                                                 target_realm=req_realm)
        except RateLimited as exc:
            record_error(model, 429, exc.detail[:200],
                         elapsed_ms=int((time.time() - t_start) * 1000))
            return self._rate_limited(exc)
        except urllib.error.HTTPError as exc:
            # 错误体在 open_upstream 中已读过（挂在 qoder_detail），二次 read
            # 会拿到残缺内容——优先取挂载值。
            detail = getattr(exc, "qoder_detail", "")
            if not detail:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
            record_error(model, exc.code, detail,
                         elapsed_ms=int((time.time() - t_start) * 1000))
            msg, etype = friendly_upstream_error(exc.code, detail)
            return self._error(exc.code, msg, etype)
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message,
                         elapsed_ms=int((time.time() - t_start) * 1000))
            if message.startswith("no usable account"):
                return self._error(503, self._no_account_message(message))
            return self._error(502, "upstream unreachable: %s" % exc)

        with upstream:
            holder = {"usage": None}
            if want_stream:
                self._sse_begin()
                emitted = False
                first_ms = None
                # 流内信封重试（同上：200 建流后信封投 418 的形态），仅在
                # 尚未向客户端写出任何上游字节时重开。
                attempts = 0
                cur = upstream
                pump_exc = None
                try:
                    while True:
                        try:
                            for line in sse_with_heartbeat(
                                    iter_inner_sse(cur, holder=holder),
                                    self._sse_write):
                                if first_ms is None:
                                    first_ms = int((time.time() - t_start) * 1000)
                                emitted = True
                                self._sse_write(line)
                            break
                        except (BrokenPipeError, ConnectionResetError,
                                ConnectionAbortedError):
                            # 客户端断开；上游已产出的部分照常记账。
                            wall = int((time.time() - t_start) * 1000)
                            record_usage(model, holder.get("usage"), stream=True,
                                         elapsed_ms=wall, ttft_ms=first_ms,
                                         gen_ms=(wall - first_ms)
                                         if first_ms is not None else None,
                                         fp=fp, account=account.uid)
                            return
                        except UpstreamStatus as exc:
                            _handle_envelope_account_cooldown(
                                account, exc, model=model,
                                session_key=session_key)
                            if should_retry_envelope(exc, emitted, attempts):
                                attempts += 1
                                log("chat in-stream envelope status %s on "
                                    "model '%s' (try %d/%d), reopening upstream"
                                    % (exc.status, model, attempts + 1,
                                       TRANSIENT_MAX_RETRIES + 1),
                                    level="WARN", tag="chat")
                                time.sleep(attempts)
                                try:
                                    cur2, account, _ = upstream_mod.open_upstream(
                                        payload, session_key=session_key,
                                        target_realm=req_realm)
                                except Exception as rex:
                                    log("chat reopen failed: %s"
                                        % str(rex)[:160], level="WARN")
                                    pump_exc = exc
                                    break
                                if cur is not upstream:
                                    try:
                                        cur.close()
                                    except Exception:
                                        pass
                                cur = cur2
                                continue
                            pump_exc = exc
                            break
                finally:
                    if cur is not upstream:
                        try:
                            cur.close()
                        except Exception:
                            pass
                if pump_exc is not None:
                    exc = pump_exc
                    wall = int((time.time() - t_start) * 1000)
                    record_error(model, exc.status, exc.detail, elapsed_ms=wall)
                    msg, etype = friendly_upstream_error(
                        _to_int_status(exc.status), exc.detail)
                    err = json.dumps({"error": {
                        "message": msg, "type": etype,
                        "code": exc.status}}, ensure_ascii=False)
                    try:
                        self._sse_write(("data: %s\n\n" % err).encode("utf-8"))
                        self._sse_write(b"data: [DONE]\n\n")
                    except Exception:
                        pass
                    self._sse_end()
                    return
                if not emitted:
                    err = json.dumps({"error": {
                        "message": "empty upstream stream",
                        "type": "server_error"}})
                    self._sse_write(("data: %s\n\n" % err).encode("utf-8"))
                self._sse_write(b"data: [DONE]\n\n")
                self._sse_end()
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, holder.get("usage"), stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms)
                             if first_ms is not None else None,
                             fp=fp, account=account.uid)
                return
            try:
                result, account = aggregate_with_envelope_retry(
                    upstream, payload, session_key, req_realm, model,
                    holder, account)
            except UpstreamStatus as exc:
                record_error(model, exc.status, exc.detail,
                             elapsed_ms=int((time.time() - t_start) * 1000))
                code = exc.status if str(exc.status).isdigit() else 502
                try:
                    code = int(code)
                except Exception:
                    code = 502
                if code < 400 or code > 599:
                    code = 502
                msg, etype = friendly_upstream_error(
                    _to_int_status(exc.status), exc.detail)
                return self._error(code, msg, etype)
            except Exception as exc:
                record_error(model, 502, str(exc),
                             elapsed_ms=int((time.time() - t_start) * 1000))
                return self._error(502, "upstream stream error: %s" % exc)
            wall = int((time.time() - t_start) * 1000)
            first_at = result.get("first_chunk_at")
            first_ms = int((first_at - t_start) * 1000) if first_at else None
            record_usage(model, result.get("usage"), stream=False,
                         elapsed_ms=wall, ttft_ms=first_ms,
                         gen_ms=(wall - first_ms) if first_ms is not None else None,
                         fp=fp, account=account.uid)
            return self._json(200, result)
