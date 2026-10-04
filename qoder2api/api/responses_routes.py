"""qoder2api.api.responses_routes —— 由 qoder_proxy.py 拆分而来。"""

import json
import time

from .. import runtime
from .. import usage
from .. import reasoning
from .. import responses as responses_mod
from ..logbus import log
import urllib.parse

from .. import upstream as upstream_mod
from ..affinity import prompt_fingerprint
from ..usage import record_error, record_usage
from ..sanitize import clean_responses_frame
from ..responses import (chat_to_response, custom_tool_names,
                         responses_to_chat, stream_responses_events,
                         responses_failed_frame)
from ..chat_normalize import tool_names_from_payload
from ..upstream import (RateLimited, TRANSIENT_MAX_RETRIES,
                        _handle_envelope_account_cooldown,
                        _to_int_status, aggregate_with_envelope_retry,
                        extract_session_key, friendly_upstream_error,
                        iter_inner_sse,
                        should_retry_envelope, sse_with_heartbeat)
from ..errors import UpstreamStatus


class ResponsesRoutesMixin(object):

    def _handle_responses(self, payload):
        """Serve /v1/responses by translating to chat completions upstream."""
        session_key = extract_session_key(self.headers, payload)
        custom_names = custom_tool_names(payload.get("tools"))
        chat_req = responses_to_chat(payload)
        model = payload.get("model") or "auto"
        want_stream = bool(payload.get("stream"))
        # 本次请求所用的 API Key（面板会话/未开鉴权时为空）：用量统计按密钥维度靠它
        key = self.key_entry
        t_start = time.time()
        fp = prompt_fingerprint(chat_req.get("messages"))
        log("responses: model=%s stream=%s msgs=%d effort=%r custom_tools=%s"
            % (model, want_stream, len(chat_req.get("messages") or []),
               chat_req.get("reasoning_effort"),
               sorted(custom_names) or "-"))
        holder = {"usage": None, "custom_names": custom_names,
                  "allowed_names": tool_names_from_payload(chat_req)}
        try:
            # None = 未显式绑定出口：交给 open_upstream 按面板模式路由
            #（双区=优先出口失效时自动切换）
            req_realm = self._request_realm()
            blocked = self._cross_realm_error(chat_req.get("model"), req_realm)
            if blocked:
                return self._error(400, blocked, "invalid_request_error")
            upstream, account, _ = upstream_mod.open_upstream(
                chat_req, session_key=session_key, target_realm=req_realm)
        except RateLimited as exc:
            t = time.time() - t_start
            record_error(model, 429, exc.detail[:200], key=key,
                         elapsed_ms=int(t * 1000))
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
            record_error(model, exc.code, detail, key=key,
                         elapsed_ms=int((time.time() - t_start) * 1000))
            msg, etype = friendly_upstream_error(exc.code, detail)
            return self._error(exc.code, msg, etype)
        except Exception as exc:
            message = str(exc)
            record_error(model, 502, message, key=key,
                         elapsed_ms=int((time.time() - t_start) * 1000))
            if message.startswith("no usable account"):
                return self._error(503, self._no_account_message(message))
            return self._error(502, "upstream unreachable: %s" % exc)
        with upstream:
            if want_stream:
                self._sse_begin()
                first_ms = None
                # 流内信封重试：上游可能 HTTP200 建流后在信封里投 418
                # （access log 记 200 + 业务错 418 即此形态）。只要还没向
                # 客户端写出任何上游字节，就重开上游再试。
                attempts = 0
                cur = upstream
                pump_exc = None
                try:
                    while True:
                        try:
                            # 统计已消费的上游数据行：错误信封本身不产出
                            # 行，故 n==0 表示还没消费到任何上游数据 → 可重开
                            consumed = {"n": 0}

                            def _count_src(src, _c=consumed):
                                for item in src:
                                    _c["n"] += 1
                                    yield item

                            inner = _count_src(
                                sse_with_heartbeat(
                                    iter_inner_sse(cur, holder=holder),
                                    self._sse_write))
                            for frame in stream_responses_events(inner, model, holder):
                                if first_ms is None:
                                    first_ms = int((time.time() - t_start) * 1000)
                                self._sse_write(clean_responses_frame(frame))
                            break
                        except (BrokenPipeError, ConnectionResetError,
                                ConnectionAbortedError):
                            wall = int((time.time() - t_start) * 1000)
                            record_usage(model, holder.get("usage"), stream=True,
                                         elapsed_ms=wall, ttft_ms=first_ms,
                                         gen_ms=(wall - first_ms)
                                         if first_ms is not None else None,
                                         fp=fp, account=account.uid, key=key)
                            return
                        except UpstreamStatus as exc:
                            # 控制帧（response.created 等）先于数据，不能算
                            # “已输出”；以上游数据行计数判断能否重开。
                            _handle_envelope_account_cooldown(
                                account, exc, model=model,
                                session_key=session_key)
                            if should_retry_envelope(exc, False, attempts) \
                                    and consumed.get("n", 0) == 0:
                                attempts += 1
                                log("responses in-stream envelope status %s "
                                    "(try %d/%d), reopening upstream"
                                    % (exc.status, attempts + 1,
                                       TRANSIENT_MAX_RETRIES + 1),
                                    level="WARN", tag="chat")
                                time.sleep(attempts)
                                try:
                                    # 必须用转换后的 chat 请求体重开：原始
                                    # Responses 体的会话在 input 字段里，而
                                    # build_qoder_body 只读 messages -> 空会话。
                                    cur2, account, _ = upstream_mod.open_upstream(
                                        chat_req, session_key=session_key,
                                        target_realm=req_realm)
                                except Exception as rex:
                                    log("responses reopen failed: %s"
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
                    record_error(model, pump_exc.status, pump_exc.detail, key=key,
                                 elapsed_ms=int((time.time() - t_start) * 1000))
                    msg, _ = friendly_upstream_error(
                        _to_int_status(pump_exc.status), pump_exc.detail)
                    log("responses upstream status %s: %s"
                        % (pump_exc.status, msg[:200]), level="ERROR",
                        tag="chat")
                    # Responses 协议靠终态事件收尾：只关流会让客户端一直等。
                    try:
                        self._sse_write(responses_failed_frame(
                            holder, _to_int_status(pump_exc.status), msg))
                    except Exception:
                        pass
                    self._sse_end()
                    return
                self._sse_end()
                wall = int((time.time() - t_start) * 1000)
                record_usage(model, holder.get("usage"), stream=True,
                             elapsed_ms=wall, ttft_ms=first_ms,
                             gen_ms=(wall - first_ms)
                             if first_ms is not None else None,
                             fp=fp, account=account.uid, key=key)
                return
            try:
                chat_obj, account = aggregate_with_envelope_retry(
                    upstream, chat_req, session_key, req_realm, model,
                    holder, account)
            except UpstreamStatus as exc:
                record_error(model, exc.status, exc.detail, key=key,
                             elapsed_ms=int((time.time() - t_start) * 1000))
                msg, etype = friendly_upstream_error(_to_int_status(exc.status),
                                                     exc.detail)
                return self._error(exc.status if str(exc.status).isdigit() else 502,
                                   msg, etype)
            except Exception as exc:
                record_error(model, 502, str(exc), key=key,
                             elapsed_ms=int((time.time() - t_start) * 1000))
                return self._error(502, "upstream stream error: %s" % exc)
            wall = int((time.time() - t_start) * 1000)
            result = chat_to_response(chat_obj, model, custom_names)
            record_usage(model, chat_obj.get("usage"), stream=False, key=key,
                         elapsed_ms=wall, fp=fp, account=account.uid)
            return self._json(200, result)
