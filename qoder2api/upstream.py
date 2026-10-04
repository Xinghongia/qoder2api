"""qoder2api.upstream —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import queue as _queue
import socket
import ssl
import threading as _threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import runtime
from . import errors
from . import affinity
from . import realm
from . import usage
from . import accounts as qoder_accounts
from . import net as qoder_net
from . import sign as qoder_sign
from . import settings as qoder_settings
from .logbus import log, add_log_entry
from .errors import BodyTooLarge, BadJSON, UpstreamStatus
import re

from . import catalog as qoder_catalog
from .affinity import AFFINITY_DEBUG, derive_affinity_key
from .accounts import CLIENT_UA, gateway_candidates
from .sign import SESSIONS, qoder_encode
from .body import CHAT_PATH, build_qoder_body
from .sanitize import clean_chunk, strip_data_prefix
from .realm import pick_serving_realm, realm_candidates
from .chat_normalize import (_new_id, tool_names_from_payload,
                             parse_leaked_tool_calls, leaked_partial_droppable)
from .security import validate_public_http_url




# ---------------------------------------------------------------------------
# Qoder 数据面：上游打开与 SSE 信封解包
# ---------------------------------------------------------------------------
class RateLimited(Exception):
    """Upstream throttled this request (429 / 频控)。"""

    def __init__(self, http_error=None, detail="", wait=60):
        self.http_error = http_error
        self.detail = detail or ""
        self.wait = max(1, int(wait or 60))
        super(RateLimited, self).__init__(
            "upstream rate limit: %s" % (self.detail[:200] or "429"))


# 账号级错误短冷却的“等待续上”上限：单账号池在传输/瞬时故障后只有
# 3s（多账号 15s）冷却，此时**等待**比报 429 更正确——429 只留给真正的
# 上游频控（model_cooldowns，由上游 HTTP 429 设置）。
ERROR_COOLDOWN_WAIT_MAX = 10.0


def _short_error_cooldown_wait(realm, model, exclude=None):
    """账号级（错误/传输）短冷却的最短剩余秒数；不适用则返回 0。

    - 只看 cooldown_until（错误冷却）；若该模型正被上游频控
      （model_cooldowns）则返回 0，交给429路径处理——两者语义严格分开。
    - exclude（本请求已试过的账号）中的冷却账号**不算**：等它冷却好了也
      不会再被本请求使用，纯属浪费。
    """
    if not runtime.POOL:
        return 0.0
    exclude = exclude or set()
    now = time.time()
    waits = []
    for a in runtime.POOL.accounts:
        if a.realm != realm or not a.enabled or not a.access_token:
            continue
        if a.uid in exclude:
            continue
        if a.model_cooldowns.get(model, 0.0) > now:
            return 0.0          # 上游频控生效中 -> 正当429，不等待
        remain = a.cooldown_until - now
        if 0 < remain <= ERROR_COOLDOWN_WAIT_MAX:
            waits.append(remain)
    return min(waits) if waits else 0.0


def retry_after_seconds(model, realm):
    """上游频控的最短等待（仅 model_cooldowns——429 的正当语义）。"""
    if not runtime.POOL:
        return 60
    now = time.time()
    waits = [a.model_cooldowns.get(model, 0.0) - now
             for a in runtime.POOL.accounts
             if a.realm == realm and a.enabled and a.access_token]
    active = [w for w in waits if w > 0]
    return int(min(active)) if active else 60


def realm_model_throttled(realm, model):
    """True 仅当该区域账号全部被**上游频控**（model_cooldowns）挡住。

    账号级错误冷却（cooldown_until）不算频控——否则传输故障后客户端会连续
    收到误导性的 `429 usage exceeds frequency limit`。
    """
    if not runtime.POOL:
        return (False, 0)
    existing = [a for a in runtime.POOL.accounts
                if a.realm == realm and a.enabled and a.access_token]
    if not existing:
        return (False, 0)
    now = time.time()
    waits = [a.model_cooldowns.get(model, 0.0) - now for a in existing]
    if waits and all(w > 0 for w in waits):
        return (True, max(1, int(min(waits))))
    return (False, 0)


def extract_session_key(headers, payload):
    key = (
        headers.get("X-Conversation-Id") or
        headers.get("Conversation-Id") or
        headers.get("X-Session-Id") or
        headers.get("Session-Id") or
        payload.get("conversation_id") or
        payload.get("session_id") or
        (payload.get("metadata") or {}).get("conversation_id")
    )
    if key:
        return str(key).strip()
    return None


# 上游瞬时故障（与客户端参数无关，值得同账号快速重试）
TRANSIENT_HTTP_CODES = (418, 500, 502, 503, 504)
TRANSIENT_MAX_RETRIES = 2          # 同账号额外重试次数（1s、2s 退避）
_CLIENT_FAULT_MARKERS = (
    "invalid_parameter_error",     # 如 Range of max_tokens 校验失败
    "invalid_request_error",
    "authentication_error",
    "permission_error",
    '"Range of ',
    # 上游内容安全审核：确定性拒绝，重试无效（本轮日志实证
    # InternalError.Algo.DataInspectionFailed: Input text data may contain
    # inappropriate content.）
    "DataInspectionFailed",
    "inappropriate content",
    "input text data may contain",
    "ContentFilter",
    "SensitiveContent",
)

# 内容审核类错误（用户输入侧问题，需专门的中文解释）
_CONTENT_POLICY_MARKERS = (
    "DataInspectionFailed",
    "inappropriate content",
    "input text data may contain",
    "ContentFilter",
    "SensitiveContent",
)


def _is_transient_upstream(code, detail):
    """判断一次上游 HTTP 错误是否属于瞬时故障（可重试）。

    - 客户端参数/权限类错误（invalid_parameter_error 等）→ 永不重试
    - 418（上游把自己的 provider 故障包装成 418+provider_error）与 5xx → 瞬时
    - 其余 4xx 带 provider_error（"Error in upstream response"）→ 瞬时
    """
    detail = detail or ""
    if code in (401, 403, 429):
        return False        # 凭证/频控各自有专门处理路径，不属瞬时重试类
    if any(m in detail for m in _CLIENT_FAULT_MARKERS):
        return False
    if code in TRANSIENT_HTTP_CODES or code >= 500:
        return True
    if 400 <= code < 500 and "provider_error" in detail:
        return True
    return False


def _is_transient_transport(exc):
    """传输层瞬时故障（对 qoder.sh 的 TLS/连接抖动很常见）：可同账号重试。

    覆盖 SSL EOF/重置、连接重置/中止、超时、以及 URLError 包装的上述原因
    （含按字符串描述判断的情形，如 "SSL: UNEXPECTED_EOF_WHILE_READING"）。
    """
    if isinstance(exc, urllib.error.URLError):
        return _is_transient_transport(getattr(exc, "reason", None))
    if isinstance(exc, (ssl.SSLError, ConnectionResetError,
                        ConnectionAbortedError, TimeoutError,
                        ConnectionError, OSError)):
        return True
    if isinstance(exc, str):
        low = exc.lower()
        return any(k in low for k in ("ssl", "eof", "reset", "timed out",
                                      "broken pipe", "connection"))
    return False


def friendly_upstream_error(code, detail):
    """把上游错误转成对客户端可读的消息；瞬时故障给出重试指引。

    返回 (message, err_type)。
    """
    detail = (detail or "").strip()
    try:
        code_i = int(code)
    except Exception:
        code_i = 502
    # 1) 上游内容安全审核（确定性拒绝，先于瞬时判断——重试无效）
    if any(m in detail for m in _CONTENT_POLICY_MARKERS):
        return ("上游内容安全审核未通过 (DataInspectionFailed)：输入可能含不当内容，"
                "属确定性拒绝、重试无效。请检查/缩短输入（系统提示词、超长历史、"
                "工具定义或粘贴的代码/文本）后重试。上游详情：%s"
                % detail[:300],
                "content_policy_rejected")
    if _is_transient_upstream(code_i, detail) or (
            "provider_error" in detail and "invalid_" not in detail):
        return ("上游瞬时故障 (HTTP %s)：网关已对同账号自动重试仍失败，"
                "请稍后重试。上游详情：%s"
                % (code_i, detail[:300] or "(无详情)"),
                "upstream_transient_error")
    return ("upstream %s: %s" % (code_i, detail)), "upstream_error"


def _to_int_status(status):
    """信封 statusCodeValue 可能是 int 或 str，统一成 int（失败回 502）。"""
    try:
        return int(status)
    except Exception:
        return 502


def _handle_envelope_account_cooldown(account, exc, model=None, session_key=None):
    """信封层非 200（403 队列满/Token 被拒、429 等）冷却该账号并解绑会话，触发换号。

    上游有两种投递错误的形态：urlopen 直接抛 HTTPError（open_upstream 已完整
    处理），以及先建 HTTP200 流、把错误装进 SSE 信封 statusCodeValue（如
    10605 队列已满）——后者此前既不冷却也不轮换，会让同一个"队列满"的账号被
    反复选中（上游 PR#7 的场景）。这里补齐信封层语义：

      - 10605/isQueued：按上游 retryAfterSeconds（缺省 30s）做**模型级**冷却；
      - 401/403 其他：账号级冷却 60s；会话被吊销（TOKEN_EXPIRE）则 300s + 停用；
      - 429：模型级冷却（按 retryAfterSeconds）；
      - 其它：短冷却 15s。冷却同时解绑会话亲和，让下次选号轮换。
    """
    if not account or not hasattr(account, "note_error"):
        return
    status_int = _to_int_status(getattr(exc, "status", 502))
    detail = getattr(exc, "detail", "") or ""
    if session_key and runtime.POOL:
        try:
            runtime.POOL.affinity.unbind(session_key)
        except Exception:
            pass
    total = len(runtime.POOL.accounts) if runtime.POOL else 1
    retry_secs = 30
    m = re.search(r"retryAfterSeconds\D+(\d+)", detail)
    if m:
        try:
            retry_secs = int(m.group(1))
        except Exception:
            pass
    if status_int == 429:
        account.note_error("envelope 429", model=model, cooldown=retry_secs)
        log("account %s throttled via envelope (429), cooling for %ds"
            % (account.uid[:8], retry_secs), level="WARN", tag="chat")
    elif status_int in (401, 403):
        dead = qoder_accounts.session_dead(detail)
        is_queue = "10605" in detail or "isQueued" in detail
        cd = 300 if dead else (retry_secs if is_queue else 60)
        account.note_error("envelope HTTP %s: %s" % (status_int, detail[:80]),
                           cooldown=cd, single_account=(total <= 1),
                           model=model if is_queue else None)
        log("account %s rejected via envelope (HTTP %s, queue=%s), cooling for %ds"
            % (account.uid[:8], status_int, is_queue, cd), level="WARN", tag="chat")
        if dead:
            account.enabled = False
            account.save(runtime.ACCOUNTS_DIR) if account.path else None
            log("account %s session dead via envelope (TOKEN_EXPIRE) - disabled"
                % account.uid[:8], level="ERROR")
    else:
        account.note_error("envelope HTTP %s: %s" % (status_int, detail[:80]),
                           cooldown=15, single_account=(total <= 1))


def should_retry_envelope(exc, emitted_bytes, attempt):
    """流内错误信封是否值得**重开上游**再试。

    关键前提（access log 记 200 而业务错 418 的根因）：上游先以 HTTP200
    建流，provider 故障以 SSE 信封 statusCodeValue=418 投递——此时 urlopen
    层重试覆盖不到。只要 **尚未向客户端发出任何字节**、未超预算、且错误属
    瞬时类（418/5xx/provider_error，且非客户端参数错），就值得重开。

    401/403/429 同样允许换号重开：它们不是"重试同一账号"的瞬时故障，而是
    "这个账号/这条队列不行"——重开会经 open_upstream 选到别的账号（账号冷却
    由 _handle_envelope_account_cooldown 负责）。
    """
    if emitted_bytes:
        return False
    if attempt >= TRANSIENT_MAX_RETRIES:
        return False
    status_int = _to_int_status(getattr(exc, "status", 502))
    if status_int in (401, 403, 429):
        return True
    return _is_transient_upstream(status_int, getattr(exc, "detail", "") or "")


def aggregate_with_envelope_retry(resp, payload, session_key, realm, model,
                                  holder, account):
    """非流式：流内瞬时错误信封 / 传输抖动 -> 重开上游重新聚合。

    仅用于客户端尚未收到任何字节的非流式路径。返回 (chat_obj, account)：
      - 最终信封错误以 UpstreamStatus 抛出（调用方既有分支处理：记账+友好提示）
      - 重开时 open_upstream 的 RateLimited/HTTPError 记日志后仍以**原信封**
        错误上抛（原错误才是本次请求的真实结果，且其分支已具备友好映射）
      - 传输层瞬时错误（TLS EOF 等）同样触发重开
    传入的 resp 由调用方的 with/finally 关闭；本函数只负责关闭重开的新连接。
    """
    cur = resp
    try:
        for attempt in range(TRANSIENT_MAX_RETRIES + 1):
            try:
                obj = aggregate_stream(cur, model, None, holder=holder,
                                       allowed_names=tool_names_from_payload(payload))
                return obj, account
            except UpstreamStatus as exc:
                # 信封层 403/10605、429、死会话：冷却该账号 + 解绑会话亲和
                # （换号语义由 should_retry_envelope 放行 401/403/429 完成）
                _handle_envelope_account_cooldown(account, exc, model=model,
                                                  session_key=session_key)
                if not should_retry_envelope(exc, False, attempt):
                    raise
                log("in-stream envelope status %s on model '%s' "
                    "(try %d/%d), reopening upstream"
                    % (exc.status, model, attempt + 1,
                       TRANSIENT_MAX_RETRIES + 1), level="WARN", tag="chat")
                time.sleep(attempt + 1)
                try:
                    new_resp, account, _ = open_upstream(
                        payload, session_key=session_key, target_realm=realm)
                except Exception as reopen_exc:
                    log("reopen after envelope error failed: %s"
                        % str(reopen_exc)[:160], level="WARN", tag="chat")
                    raise exc      # 以原信封错误进入既有处理路径
                if cur is not resp:
                    try:
                        cur.close()
                    except Exception:
                        pass
                cur = new_resp
            except Exception as exc:
                if attempt >= TRANSIENT_MAX_RETRIES or \
                        not _is_transient_transport(exc):
                    raise
                log("in-stream transport error on model '%s' (try %d/%d): "
                    "%s - reopening upstream"
                    % (model, attempt + 1, TRANSIENT_MAX_RETRIES + 1,
                       str(exc)[:120]), level="WARN", tag="chat")
                time.sleep(attempt + 1)
                try:
                    new_resp, account, _ = open_upstream(
                        payload, session_key=session_key, target_realm=realm)
                except Exception as reopen_exc:
                    log("reopen after transport error failed: %s"
                        % str(reopen_exc)[:160], level="WARN", tag="chat")
                    raise exc
                if cur is not resp:
                    try:
                        cur.close()
                    except Exception:
                        pass
                cur = new_resp
    finally:
        if cur is not resp:
            try:
                cur.close()
            except Exception:
                pass


class _LeasedResp(object):
    """带账号在途计数的上游响应代理（least-busy 调度用）。

    流式响应从 open 到 close / with 退出期间在途数 +1；close 或退出时
    恰好释放一次（重复 close 安全）。其余属性/方法原样转发给底层响应。
    """

    __slots__ = ("_resp", "_account", "_released")

    def __init__(self, resp, account):
        self._resp = resp
        self._account = account
        self._released = False

    def _release(self):
        if not self._released:
            self._released = True
            try:
                self._account.in_flight = max(
                    0, getattr(self._account, "in_flight", 0) - 1)
            except Exception:
                pass

    def __enter__(self):
        enter = getattr(self._resp, "__enter__", None)
        if enter is not None:
            try:
                enter()
            except Exception:
                pass
        return self

    def __exit__(self, *args):
        try:
            exit_fn = getattr(self._resp, "__exit__", None)
            if exit_fn is not None:
                return exit_fn(*args)
        finally:
            self._release()

    def close(self):
        try:
            close = getattr(self._resp, "close", None)
            if close is not None:
                close()
        finally:
            self._release()

    def __iter__(self):
        return iter(self._resp)

    def __getattr__(self, name):
        return getattr(self._resp, name)


def open_upstream(payload, session_key=None, target_realm=None):
    """构造 COSY 签名请求并打开上游 SSE。返回 (resp, account, encoded)。

    账号轮换规则：
      - 401/403：凭证被拒 -> 冷却该账号（单账号池时短冷却）换号
      - 429    ：模型粒度频控 -> 解析 reset 时间做模型冷却换号
      - 418/5xx/provider_error（瞬时上游故障）-> 同账号快速重试 2 次
                （1s/2s 退避），仍失败短冷却(15s/单账号3s)换号
      - 其他 4xx（含客户端参数错）-> 快速失败，冷却换号，不重试
    全部账号失败后抛 RateLimited 或最后一个错误。
    """
    model = str(payload.get("model") or "")
    candidates = realm_candidates(model=model, explicit=target_realm)
    realm = pick_serving_realm(candidates, model=model)
    if realm != candidates[0]:
        log("realm fallback: %s -> %s (首选出口当前没有可用账号)"
            % (candidates[0], realm), level="WARN", tag="chat")
    model_key = qoder_catalog.resolve_upstream_key(model, realm=realm)

    representative = runtime.POOL.pick(realm=realm) if runtime.POOL else None
    body_obj = build_qoder_body(payload, representative, model_key, realm=realm)
    encoded = qoder_encode(json.dumps(body_obj, ensure_ascii=False).encode("utf-8"))

    if not session_key:
        session_key = derive_affinity_key(body_obj.get("messages"))
        if session_key and AFFINITY_DEBUG:
            log("affinity: derived %s for %d msgs"
                % (session_key, len(body_obj.get("messages") or [])))

    total = max(1, runtime.POOL.count_ready(realm, model=model)) if runtime.POOL else 1
    tried = set()
    last_error = None
    last_429 = None
    last_429_detail = ""
    waited_cool = False

    # 额外 +2 次迭代预算：只供“短错误冷却等待续上”使用（正常轮换仍由
    # tried 集合自然终止）。
    for _ in range(total + 2):
        account = runtime.POOL.pick_for_session(realm=realm, session_key=session_key,
                                        exclude=tried, model=model) if runtime.POOL else None
        if account is None:
            if not waited_cool:
                wait = _short_error_cooldown_wait(realm, model,
                                                  exclude=tried)
                if wait > 0:
                    waited_cool = True
                    log("accounts in short error-cooldown for '%s' "
                        "(%.1fs left) - waiting instead of failing"
                        % (model, wait), level="WARN", tag="chat")
                    time.sleep(wait + 0.25)
                    continue     # 冷却到期后重新 pick，服务该请求
            break
        if account.realm != realm:
            if session_key and runtime.POOL:
                runtime.POOL.affinity.unbind(session_key)
            continue
        tried.add(account.uid)
        # 身份相关的 aliyun_user_type 需要真实账号
        body_obj["aliyun_user_type"] = account.user_type or \
            qoder_sign.DEFAULT_USER_TYPE
        encoded = qoder_encode(
            json.dumps(body_obj, ensure_ascii=False).encode("utf-8"))
        # 官方候选推理主机（国际版 api1/api2/api3）：传输层失败时切换域名，
        # 签名只覆盖 path，换主机不影响签名有效性。
        hosts = gateway_candidates(account.realm)
        host_i = 0
        raw_url = hosts[host_i] + CHAT_PATH

        # ---- 发起请求（瞬时上游故障同账号快速重试） ----
        resp = None
        last_exc = None
        detail = ""
        for tries in range(TRANSIENT_MAX_RETRIES + 1):
            try:
                sess = SESSIONS.get(account)
                headers = sess.headers(encoded, raw_url, model_key=model_key,
                                       sse=True)
                headers["User-Agent"] = CLIENT_UA
                chat_url = validate_public_http_url(raw_url)
                req = urllib.request.Request(chat_url,
                                             data=encoded.encode("utf-8"),
                                             method="POST", headers=headers)
                resp = qoder_net.urlopen(req, timeout=600)
                break
            except urllib.error.HTTPError as exc:
                try:
                    detail = exc.read(600).decode("utf-8", "replace")
                except Exception:
                    detail = ""
                # 错误体只能读一次：把已读详情挂到异常上，处理器二次 read
                # 会拿到残缺/空内容，统一从 qoder_detail 取。
                try:
                    exc.qoder_detail = detail
                except Exception:
                    pass
                last_exc = exc
                # 429/401/403 与客户端参数错：立即进入分类，不重试
                if exc.code in (429, 401, 403):
                    break
                if _is_transient_upstream(exc.code, detail) \
                        and tries < TRANSIENT_MAX_RETRIES:
                    backoff = tries + 1      # 1s, 2s（tries 从 0 计）
                    log("transient upstream HTTP %d on '%s' model '%s' "
                        "(try %d/%d), retry in %ds"
                        % (exc.code, account.uid[:8], model, tries + 1,
                           TRANSIENT_MAX_RETRIES + 1, backoff),
                        level="WARN", tag="chat")
                    time.sleep(backoff)
                    continue
                break
            except Exception as exc:
                last_exc = exc
                detail = ""
                # 传输层瞬时故障（TLS EOF / 连接重置 / 超时）同样原地重试；
                # 若还有官方备用推理域名，优先换域名（对整域故障更有效）。
                if _is_transient_transport(exc) \
                        and tries < TRANSIENT_MAX_RETRIES:
                    if host_i + 1 < len(hosts):
                        host_i += 1
                        raw_url = hosts[host_i] + CHAT_PATH
                        log("transient transport error on '%s' model '%s' "
                            "(try %d/%d): %s - switching gateway host to %s"
                            % (account.uid[:8], model, tries + 1,
                               TRANSIENT_MAX_RETRIES + 1, str(exc)[:120],
                               hosts[host_i]),
                            level="WARN", tag="chat")
                    else:
                        log("transient transport error on '%s' model '%s' "
                            "(try %d/%d): %s - retry in %ds"
                            % (account.uid[:8], model, tries + 1,
                               TRANSIENT_MAX_RETRIES + 1,
                               str(exc)[:120], tries + 1),
                            level="WARN", tag="chat")
                    time.sleep(tries + 1)
                    continue
                break

        if resp is not None:
            account.clear_error(model=model)
            # 在途计数 +1：响应关闭（close / with 退出）时释放，供 least-busy 调度
            account.in_flight = getattr(account, "in_flight", 0) + 1
            return _LeasedResp(resp, account), account, encoded

        exc = last_exc
        # ---- 错误分类（与原有轮换语义一致） ----
        if isinstance(exc, urllib.error.HTTPError):
            if exc.code == 429:
                account.note_error("HTTP 429 (model throttled)", model=model,
                                   cooldown=60)
                log("account %s throttled on '%s' (429), retry in 60s"
                    % (account.uid[:8], model))
                if session_key and runtime.POOL:
                    runtime.POOL.affinity.unbind(session_key)
                last_error = exc
                last_429 = exc
                last_429_detail = detail
                continue
            if exc.code in (401, 403):
                log("account %s rejected (HTTP %s), rotating"
                    % (account.uid[:8], exc.code))
                if session_key and runtime.POOL:
                    runtime.POOL.affinity.unbind(session_key)
                dead = qoder_accounts.session_dead(detail)
                account.note_error("HTTP %s %s" % (exc.code, detail[:80]),
                                   cooldown=300 if dead else 60,
                                   single_account=(total <= 1),
                                   escalate=True)
                if dead:
                    account.enabled = False
                    account.save(runtime.ACCOUNTS_DIR) if account.path else None
                    log("account %s session dead (TOKEN_EXPIRE) - disabled"
                        % account.uid[:8], level="ERROR")
                last_error = exc
                continue
            if _is_transient_upstream(exc.code, detail):
                # 重试后仍是瞬时故障：上游侧问题，短冷却换号（不重罚账号）
                if session_key and runtime.POOL:
                    runtime.POOL.affinity.unbind(session_key)
                account.note_error(
                    "HTTP %s upstream transient: %s" % (exc.code, detail[:80]),
                    cooldown=15, single_account=(total <= 1))
                log("upstream still transient (HTTP %d) after %d tries on "
                    "'%s' - short cooldown, rotating"
                    % (exc.code, TRANSIENT_MAX_RETRIES + 1, account.uid[:8]),
                    level="WARN", tag="chat")
                last_error = exc
                continue
            # 其他 4xx（客户端参数/请求形态问题）：快速失败，冷却换号
            if 400 <= exc.code < 500:
                if session_key and runtime.POOL:
                    runtime.POOL.affinity.unbind(session_key)
                account.note_error("HTTP %s: %s" % (exc.code, detail[:80]),
                                   cooldown=60, single_account=(total <= 1))
                last_error = exc
                continue
            raise
        else:
            if session_key and runtime.POOL:
                runtime.POOL.affinity.unbind(session_key)
            if exc is not None:
                if _is_transient_transport(exc):
                    # 传输抖动重试耗尽：短冷却换号（不重罚账号）
                    account.note_error("transport transient: %s" % str(exc)[:100],
                                       cooldown=15, single_account=(total <= 1))
                    log("upstream transport still failing after %d tries on "
                        "'%s' - short cooldown"
                        % (TRANSIENT_MAX_RETRIES + 1, account.uid[:8]),
                        level="WARN", tag="chat")
                else:
                    account.note_error(str(exc)[:120], cooldown=60,
                                       single_account=(total <= 1))
                last_error = exc
            continue

    if last_error is not None:
        if last_429 is not None:
            raise RateLimited(last_429, last_429_detail,
                              wait=retry_after_seconds(model, realm))
        raise last_error
    throttled, wait = realm_model_throttled(realm, model)
    if throttled:
        raise RateLimited(None, "usage exceeds frequency limit", wait=wait)
    raise RuntimeError(
        "no usable account for realm '%s': all are disabled, cooling down, or expired"
        % realm)


def sse_with_heartbeat(source, send, interval=None, idle_limit=None):
    """在等待上游数据的空档里发送 SSE 注释心跳（`: ping`），并原样转发数据。

    背景：`xhigh` 长上下文请求的上游首字延迟可达 40–71 秒（实测日志），
    这段时间网关对客户端一言不发；客户端/中间代理一旦空闲超时就会断开并
    重连（表现为“一直重连”）。SSE 规范允许以 `:` 开头的注释行，客户端会
    忽略，因此用它做保活。

    source     : 上游数据迭代器（iter_inner_sse 的输出，产出 bytes）
    send       : 发送函数（接收 bytes）
    interval   : 空闲多久发一次心跳（秒），默认 5（可用 QD_SSE_HEARTBEAT 覆盖）
    idle_limit : 单次等待上限（秒），默认 900；超过抛 TimeoutError

    读线程把上游数据放入队列，主线程只在“队列空闲”时补心跳，保证顺序与
    错误传播（上游异常原样在主线程抛出）。
    """
    if interval is None:
        try:
            interval = float(os.environ.get("QD_SSE_HEARTBEAT", "5") or 5)
        except Exception:
            interval = 5.0
    if interval <= 0:
        # 显式关闭心跳：退化为直通
        for item in source:
            yield item
        return
    if idle_limit is None:
        idle_limit = 900.0

    import queue as _queue
    import threading as _threading

    box = _queue.Queue()
    done = object()

    def _reader():
        try:
            for item in source:
                box.put(item)
        except BaseException as exc:          # 上游错误/断连也要送主线程
            box.put(exc)
        finally:
            box.put(done)

    reader = _threading.Thread(target=_reader, daemon=True)
    reader.start()

    waited = 0.0
    while True:
        try:
            item = box.get(timeout=interval)
        except _queue.Empty:
            send(b": ping\n\n")               # SSE 注释：客户端忽略
            waited += interval
            if waited >= idle_limit:
                raise TimeoutError(
                    "upstream produced no data for %.0fs" % waited)
            continue
        if item is done:
            return
        if isinstance(item, BaseException):
            raise item
        waited = 0.0
        yield item


def iter_inner_sse(resp, holder=None):
    """把上游 SSE 信封流解包成标准 OpenAI chunk 的 "data: ..." 行。

    上游帧形态：
        data:{"headers":{...},"body":"<内层 OpenAI chunk JSON 字符串>","statusCodeValue":200}
        data:{"body":"[DONE]"}
        event:finish{...}          <- 计时元数据，忽略

    规则：
      - statusCodeValue != 200 -> 抛 UpstreamStatus（body 为错误详情）
      - body == "[DONE]"       -> 结束迭代
      - 内层 chunk 里的 usage 记入 holder
    """
    for raw in resp:
        if isinstance(raw, bytes):
            try:
                line = raw.decode("utf-8")
            except Exception:
                continue
        else:
            line = raw
        data = strip_data_prefix(line)
        if not data:
            continue
        try:
            outer = json.loads(data)
        except Exception:
            continue
        if not isinstance(outer, dict):
            continue
        status = outer.get("statusCodeValue")
        body = outer.get("body")
        if status not in (None, 200, "200"):
            raise UpstreamStatus(status, body if isinstance(body, str)
                                 else json.dumps(outer, ensure_ascii=False)[:400])
        if not isinstance(body, str):
            continue
        if body == "[DONE]":
            break
        try:
            inner = json.loads(body)
        except Exception:
            continue
        if holder is not None and inner.get("usage") and not holder.get("usage"):
            holder["usage"] = inner["usage"]
        cleaned = clean_chunk(body)
        if not cleaned:
            continue
        yield ("data: " + cleaned + "\n\n").encode("utf-8")


def aggregate_stream(resp, model, resp_id=None, holder=None, allowed_names=None):
    """把上游信封流折叠成一个非流式 chat.completion 对象。

    allowed_names：本次请求声明的工具名集合；用于「泄漏文本回读」的守卫
    （见 chat_normalize.parse_leaked_tool_calls / issue #8）。
    """
    content, reasoning, finish = [], [], "stop"
    tool_calls_map = {}
    usage = holder.get("usage") if holder else None
    started = time.time()
    first_chunk_at = None
    created = None

    for line in iter_inner_sse(resp, holder=holder):
        data = strip_data_prefix(line.decode("utf-8", "replace"))
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if first_chunk_at is None:
            first_chunk_at = time.time()
        if chunk.get("id"):
            resp_id = chunk["id"]
        if chunk.get("model"):
            model = chunk["model"]
        if chunk.get("created"):
            created = chunk["created"]
        if chunk.get("usage"):
            usage = chunk["usage"]
            if holder is not None:
                holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            if delta.get("content"):
                _c = delta["content"]
                if isinstance(_c, str):
                    content.append(_c)
                elif isinstance(_c, list):
                    # 防御：非字符串 content（parts 列表）展开而非让聚合器崩
                    for _p in _c:
                        if isinstance(_p, str):
                            content.append(_p)
                        elif (isinstance(_p, dict)
                              and isinstance(_p.get("text"), str)):
                            content.append(_p["text"])
            if delta.get("reasoning_content"):
                reasoning.append(delta["reasoning_content"])
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index")
                if idx is None:
                    idx = len(tool_calls_map)
                fn = tc.get("function") or {}
                call_id = tc.get("id")
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": call_id or _new_id("call_"),
                        "type": tc.get("type") or "function",
                        "function": {"name": fn_name, "arguments": fn_args},
                    }
                else:
                    entry = tool_calls_map[idx]
                    if call_id:
                        entry["id"] = call_id
                    if fn_name:
                        entry["function"]["name"] = \
                            (entry["function"]["name"] or "") + fn_name
                    if fn_args:
                        entry["function"]["arguments"] = \
                            (entry["function"]["arguments"] or "") + fn_args
            fc = delta.get("function_call")
            if fc and isinstance(fc, dict) and fc.get("name"):
                idx = 0
                if idx not in tool_calls_map:
                    tool_calls_map[idx] = {
                        "id": _new_id("call_"),
                        "type": "function",
                        "function": {"name": fc.get("name") or "",
                                     "arguments": fc.get("arguments") or ""},
                    }
                else:
                    entry = tool_calls_map[idx]
                    if fc.get("name") and not entry["function"]["name"]:
                        entry["function"]["name"] = fc["name"]
                    if fc.get("arguments"):
                        entry["function"]["arguments"] += fc["arguments"]
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]

    message = {"role": "assistant", "content": "".join(content)}
    if reasoning:
        message["reasoning_content"] = "".join(reasoning)
    # 回读（issue #8）：上游未给结构化 tool_calls，但正文恰好是网关自己写入
    # 历史的「LEAK_MARKER + JSON 数组」形态（模型照格式复述）——还原为结构化
    # 调用并从正文移除，避免客户端把 JSON 当正文显示、本轮调用不执行。
    if not tool_calls_map and message.get("content"):
        _rec, _clean = parse_leaked_tool_calls(message["content"], allowed_names)
        if _rec:
            message["content"] = _clean
            tool_calls_map = {i: c for i, c in enumerate(_rec)}
        elif leaked_partial_droppable(message["content"], allowed_names):
            # issue #9：截断的 marker+JSON 回声不还原、也不透传，直接清空正文。
            log("dropped truncated leaked tool-call echo (%d chars) — issue #9"
                % len(message["content"]), level="WARN", tag="chat")
            message["content"] = ""
    # 二次防御：剔除「无函数名」的空 tool_call，防止客户端死等
    if tool_calls_map:
        tool_calls_map = {k: v for k, v in tool_calls_map.items()
                          if (v.get("function") or {}).get("name")}
    if tool_calls_map:
        ordered = [tool_calls_map[k] for k in sorted(tool_calls_map.keys())]
        message["tool_calls"] = ordered
        if finish in ("stop", None):
            finish = "tool_calls"
    elif finish == "tool_calls":
        # 占位被全部过滤掉 -> 降级为正常结束，防止客户端无限挂起
        finish = "stop"
    out = {
        "id": resp_id or "chatcmpl-qoder",
        "object": "chat.completion",
        "created": created or int(time.time()),
        "model": model,
        "choices": [{"index": 0, "message": message, "finish_reason": finish}],
    }
    if usage:
        out["usage"] = usage
    out["elapsed_ms"] = int((time.time() - started) * 1000)
    out["first_chunk_at"] = first_chunk_at
    return out
