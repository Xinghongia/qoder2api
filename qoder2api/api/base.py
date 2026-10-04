"""qoder2api.api.base —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import socket
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler

from .. import paths, runtime
from .. import errors
from .. import auth
from .. import realm
from .. import usage
from .. import views
from .. import affinity
from .. import settings as qoder_settings
from ..logbus import log, add_log_entry
from ..errors import BodyTooLarge, BadJSON, UpstreamStatus
import re
from urllib.parse import urlparse, parse_qs

from .. import VERSION
from ..auth import auth_required, identify_key
from ..security import cors_origin_allowed
from ..realm import exclusive_realm


class HandlerBase(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    # Which configured API key the caller used, set by _key_ok(). Its bound
    # realm decides the upstream exit for this request alone.
    key_entry = None

    def handle(self):
        try:
            super().handle()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    def finish(self):
        try:
            super().finish()
        except (ConnectionResetError, BrokenPipeError, ConnectionAbortedError):
            pass

    server_version = "qd-proxy/" + VERSION

    def log_message(self, fmt, *args):
        # 静默看板高频轮询的正常 200 GET，异常状态与业务操作照常记录。
        try:
            req_path = (getattr(self, "path", None)
                        or (args[0] if args else "")).split("?")[0]
            if req_path == "/favicon.ico":
                return           # 浏览器自动请求的 404 不刷屏
            status_code = int(args[1]) if len(args) > 1 \
                and str(args[1]).isdigit() else 200
            if status_code < 400 and getattr(self, "command", "GET") == "GET":
                quiet_prefixes = (
                    "/logs", "/usage", "/accounts", "/scheduler",
                    "/health", "/panel/status", "/realm",
                )
                if any(req_path == p or req_path.startswith(p + "/")
                       for p in quiet_prefixes):
                    return
        except Exception:
            pass
        log(fmt % args)

    def _sse_begin(self, fallback=None):
        """开始一个 HTTP/1.1 SSE 流：用 chunked 编码，保持连接可复用。

        之前用 `Connection: close` + 裸写字节：客户端（连接池型 harness）
        会把该连接视为可复用，下一次请求落在已半关闭的连接上，表现为
        “一直重连 / 连不上”。改为 chunked 后，流结束发 0 长度的终止块，
        连接保持 keep-alive，可安全复用。

        fallback（open_upstream 的回退信息，形如 {"from","to","reason"}）：
        有值时在流首带一个 SSE 注释帧（`: qoder-fallback: ...`，客户端按
        规范忽略），让 curl / 抓包的人一眼看到"这条请求其实跑在了另一区"。
        """
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Transfer-Encoding", "chunked")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self._chunked = True
        if fallback:
            try:
                note = json.dumps(fallback, ensure_ascii=False)
                self._sse_write((": qoder-fallback: %s\n\n" % note).encode("utf-8"))
            except Exception:
                pass

    def _sse_write(self, data):
        """按 chunked 编码写一段数据（data: bytes）。"""
        if not data:
            return
        if not getattr(self, "_chunked", False):
            # 理论上不会发生；保守回退成裸写
            self.wfile.write(data)
            self.wfile.flush()
            return
        self.wfile.write(("%x\r\n" % len(data)).encode("ascii"))
        self.wfile.write(data)
        self.wfile.write(b"\r\n")
        self.wfile.flush()

    def _sse_end(self):
        """结束 chunked 流（写 0 长度终止块），连接保持可复用。"""
        if not getattr(self, "_chunked", False):
            return
        try:
            self.wfile.write(b"0\r\n\r\n")
            self.wfile.flush()
        except Exception:
            pass
        self._chunked = False

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _error(self, code, message, err_type="server_error"):
        self._json(code, {"error": {"message": message, "type": err_type,
                                    "code": code}})

    def _redirect(self, location, code=302):
        """302 重定向（带 query 原样转发，`?key=` 引导链接不丢）。"""
        self.send_response(code)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def _rate_limited(self, exc):
        """429 with Retry-After, so clients back off instead of hammering."""
        wait = max(1, int(getattr(exc, "wait", 60) or 60))
        body = json.dumps({
            "error": {
                "message": ("upstream rate limit reached for this model; "
                            "retry in %ds" % wait)
                + ((" - " + exc.detail[:200]) if getattr(exc, "detail", "") else ""),
                "type": "rate_limit_error",
                "code": 429,
                "retry_after": wait,
            }
        }, ensure_ascii=False).encode("utf-8")
        self.send_response(429)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Retry-After", str(wait))
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _download(self, filename, obj):
        """Send a JSON document as a browser download.

        Content-Disposition is quoted because the filename is generated from
        user-controlled parts (the realm filter) and could otherwise break the
        header or allow a response-splitting attempt.
        """
        body = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        safe = re.sub(r'[^A-Za-z0-9._-]', "_", str(filename))[:120] \
            or "export.json"
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Disposition",
                         'attachment; filename="%s"' % safe)
        self.send_header("Cache-Control", "no-store")
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def _supplied_key(self):
        """The key the caller presented, from the header or the ?key= query."""
        supplied = (self.headers.get("Authorization") or "").removeprefix(
            "Bearer ").strip()
        if supplied:
            return supplied
        # 浏览器顶层导航无法设置头，所以 ?key= 也接受（看板跨设备打开用）。
        try:
            query = parse_qs(urlparse(self.path).query)
            return (query.get("key") or [""])[0].strip()
        except Exception:
            return ""

    def _key_ok(self):
        """True when the request carries a right key (or no key is needed)."""
        # 面板会话同时解锁管理 API，浏览器无需在 localStorage 存 API key。
        if self._panel_ok():
            return True
        self.key_entry = identify_key(self._supplied_key())
        if self.key_entry:
            return True
        if not auth_required():
            return True
        return False

    def _key_realm(self):
        """Realm bound to the key this request used, or "" when unbound."""
        return (self.key_entry or {}).get("realm") or ""

    def _cross_realm_error(self, model, realm):
        """解释模型/出口错配，替代上游晦涩的 403。"""
        if not realm or not model:
            return ""
        owner = exclusive_realm(model)
        if not owner or owner == realm:
            return ""
        name = (self.key_entry or {}).get("name") or "当前 Key"
        served = "国内版" if owner == "cn" else "国际版"
        used = "国内版" if realm == "cn" else "国际版"
        return ("模型 %s 只在%s提供，但「%s」绑定的是%s出口。"
                "请改用对应出口的 Key，或把该 Key 的出口改为"
                "「跟随网关出口（自动）」。"
                % (model, served, name, used))

    def _no_account_message(self, message):
        """「没有可用账号」时，结合 Key 的出口绑定给出具体原因与出路。"""
        bound = self._key_realm()
        if not bound or not runtime.POOL:
            return message + " - add or enable one at the dashboard (/)"
        name = (self.key_entry or {}).get("name") or "当前 Key"
        bound_name = "国内版" if bound == "cn" else "国际版"
        other = "cn" if bound == "intl" else "intl"
        other_name = "国内版" if other == "cn" else "国际版"
        if runtime.POOL.count_ready(other) > 0:
            return ("%s - 「%s」固定走%s出口，该出口当前没有可用账号；"
                    "%s出口仍有可用账号，把该 Key 的出口改为"
                    "「跟随网关出口（自动）」即可失效自动切换"
                    % (message, name, bound_name, other_name))
        return ("%s - 「%s」固定走%s出口，请在看板 (/) 启用或导入该区域的账号"
                % (message, name, bound_name))

    def _request_realm(self, explicit=None):
        """Pick the upstream exit for this request.

        Priority: an explicit ?realm= argument, then the realm bound to the
        API key, then the X-Realm header / ?realm= query, and finally the
        global switch. Returning None lets open_upstream() fall back to
        model-based detection.
        """
        if explicit:
            return explicit
        bound = self._key_realm()
        if bound:
            return bound
        header = self.headers.get("X-Realm")
        if header:
            return header
        try:
            return parse_qs(urlparse(self.path).query).get("realm", [None])[0]
        except Exception:
            return None

    def _authorized(self):
        if self._key_ok():
            return True
        self._error(401, "invalid api key", "invalid_request_error")
        return False

    # ---- web panel access ----
    def _panel_token(self):
        """Session token from the X-Panel-Token header.

        Deliberately header-only: a token in the query string leaks through
        browser history, the Referer header and any reverse-proxy access log.
        """
        return (self.headers.get("X-Panel-Token") or "").strip()

    def _panel_ok(self):
        return runtime.PANEL.valid(self._panel_token())

    @staticmethod
    def _is_panel_route(path):
        """Management endpoints shown in the web panel.

        Model listings stay reachable with the API key alone so that plain
        OpenAI clients can keep discovering models.
        """
        if path.startswith("/accounts"):
            return True
        if path.startswith("/usage") or path.startswith("/v1/usage"):
            return True
        if path.startswith("/tasks") or path.startswith("/scheduler"):
            return True
        if path.startswith("/settings"):
            return True
        if path.startswith("/logs"):
            return True
        if path.startswith("/diag"):
            return True
        if path.startswith("/update"):
            return True
        if path.startswith("/identity"):
            return True
        return False

    def do_OPTIONS(self):
        self.send_response(204)
        if cors_origin_allowed(self.path):
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "*")
            self.send_header("Access-Control-Allow-Methods",
                             "GET, POST, OPTIONS")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _dashboard(self, path="/"):
        """托管前端：优先新前端静态产物（web/out），缺失时提示如何构建。

        静态产物缺失（没跑过 `npm run build:export`）时，根路径给出可操作的
        提示页而不是 500 —— 只有 Python 的机器上仍能照常使用接口。
        """
        from . import static as static_mod

        if path == "/legacy":
            legacy = static_mod.legacy_dashboard_path()
            if legacy:
                return self._send_static(legacy, long_cache=False)
            return self._error(404, "legacy dashboard not found")
        if path in ("/", "/dashboard", "/ui"):
            index = static_mod.index_html_path()
            if index:
                return self._send_static(index)
            return self._static_missing_page(path)
        if static_mod.api_takes_path(path):
            return self._error(404, "not found", "invalid_request_error")
        file_path, long_cache = static_mod.resolve(path)
        if file_path is not None:
            return self._send_static(file_path, long_cache=long_cache)
        return self._error(404, "not found", "invalid_request_error")

    def _send_static(self, file_path, long_cache=False):
        from . import static as static_mod

        try:
            with open(file_path, "rb") as fh:
                body = fh.read()
        except Exception as exc:
            return self._error(500, "static file unavailable: %s" % exc)
        self.send_response(200)
        self.send_header("Content-Type", static_mod.content_type(file_path))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control",
                         "public, max-age=31536000, immutable" if long_cache
                         else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _static_missing_page(self, path):
        body = (
            "<!doctype html><meta charset=\"utf-8\">"
            "<title>前端产物缺失</title>"
            "<body style=\"font:15px/1.7 system-ui;max-width:640px;margin:12vh auto;padding:0 24px\">"
            "<h2>前端静态产物缺失</h2>"
            "<p>后端接口正常，但没有找到 <code>web/out</code> 产物。</p>"
            "<p>两种原因：① 这是源码仓库，构建产物未生成；"
            "② 打包时漏拷了 <code>web/out</code>。</p>"
            "<p>修复：<code>cd web &amp;&amp; npm install &amp;&amp; npm run build:export</code>"
            "（需要 Node，仅构建一次），或改设环境变量 "
            "<code>QD_WEB_DIR</code> 指向已有的产物目录。</p>"
            "<p>接口仍可直接使用：<code>/v1/models</code>、<code>/health</code>；"
            "旧看板：<a href=\"/legacy\">/legacy</a>。</p>"
            "<p style=\"color:#888\">请求路径：%s</p></body>"
        ) % path
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _read_payload(self, max_bytes=runtime.MAX_PAYLOAD_BYTES, allow_list=False):
        """Parse the request body into a dict (or a list when allow_list)."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except Exception:
            length = 0
        if length > max_bytes:
            raise BodyTooLarge(length)
        if length < 0:
            raise BadJSON()
        try:
            raw = self.rfile.read(length).decode("utf-8") if length else "{}"
            data = json.loads(raw or "{}")
        except Exception:
            raise BadJSON()
        if isinstance(data, dict):
            return data
        if allow_list and isinstance(data, list):
            return data
        return {}

    def _payload_or_error(self, allow_list=False):
        """Read the body, replying with the right error and returning None."""
        try:
            return self._read_payload(allow_list=allow_list)
        except BodyTooLarge as exc:
            self._error(413, "payload too large (%d bytes > %d limit)"
                        % (exc.length, runtime.MAX_PAYLOAD_BYTES),
                        "invalid_request_error")
            return None
        except BadJSON:
            self._error(400, "invalid JSON body", "invalid_request_error")
            return None
