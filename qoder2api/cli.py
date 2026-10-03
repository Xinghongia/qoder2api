"""qoder2api.cli —— 由 qoder_proxy.py 拆分而来。"""

import argparse
import os
import sys
import threading
import time

from . import paths, runtime
from . import accounts as qoder_accounts
from . import net as qoder_net
from . import realm as realm_mod
from . import views
from .logbus import log
from .api import Handler
import json
from http.server import ThreadingHTTPServer

from . import settings as qoder_settings
from .body import BASEPROMPT, BASEPROMPT_PATH
from .views import current_account
from .realm import load_persisted_realm
from .responses import local_ip_addresses




def install_console_close_handler():
    """Release the port when the console window is closed by the user.

    Windows does not kill child processes when a console window closes, so
    the proxy (started by the .bat as a child of cmd.exe) would survive and
    keep the port bound - the next launch then wrongly reports "another
    proxy is already running". Registering an event-driven handler for
    CTRL_CLOSE_EVENT is the reliable signal. Harmless without a console.
    """
    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes
        PHANDLER_ROUTINE = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CTRL_CLOSE_EVENT = 2
        CTRL_LOGOFF_EVENT = 5
        CTRL_SHUTDOWN_EVENT = 6

        def _handler(event):
            if event in (CTRL_CLOSE_EVENT, CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT):
                try:
                    sys.stdout.flush()
                except Exception:
                    pass
                os._exit(0)
            return False

        handler = PHANDLER_ROUTINE(_handler)   # keep the callback referenced
        if not ctypes.windll.kernel32.SetConsoleCtrlHandler(handler, True):
            return None
        return handler
    except Exception:
        return None


def main():


    API_KEY_GENERATED = False
    ap = argparse.ArgumentParser(
        description="Qoder (qoder.com.cn / qoder.com) -> OpenAI-compatible proxy")
    ap.add_argument("--host", default=os.environ.get("HOST") or "127.0.0.1")
    # 8788 被 mimo-api-proxy.mjs 占用，8789 是 wb-proxy 默认端口；
    # Qoder 网关默认 8790。Docker 显式传 --port 8790。
    ap.add_argument("--port", type=int,
                    default=int(os.environ.get("PORT") or "8790"))
    ap.add_argument("--lan", action="store_true",
                    help="listen on every interface so other devices on the "
                         "LAN can reach it (implies --host 0.0.0.0 and forces "
                         "an api key)")
    ap.add_argument("--api-key",
                    default=os.environ.get("API_KEY")
                    or os.environ.get("QD_PROXY_KEY") or None,
                    help="require this bearer token on /v1/* (optional)")
    ap.add_argument("--system-prompt", default=runtime.DEFAULT_SYSTEM_PROMPT,
                    help="system message used when the request has none "
                         "(default: keep the official Qoder template prompt)")
    ap.add_argument("--usage-dir", default=None,
                    help="where to store usage.jsonl / usage-summary.json "
                         "(default: ./usage)")
    ap.add_argument("--accounts-dir",
                    default=os.environ.get("ACCOUNTS_DIR") or None,
                    help="where the per-account credential files live "
                         "(default: ./accounts)")
    ap.add_argument("--panel-password", default=None,
                    help="set the web panel password on startup (default: "
                         "admin)")
    args = ap.parse_args()

    if args.lan and args.host == "127.0.0.1":
        args.host = "0.0.0.0"
    if args.usage_dir:
        runtime.USAGE_DIR = os.path.abspath(args.usage_dir)
        runtime.USAGE_LOG = os.path.join(runtime.USAGE_DIR, "usage.jsonl")
        runtime.USAGE_SUMMARY = os.path.join(runtime.USAGE_DIR, "usage-summary.json")

    # 拒绝启动第二份：Windows 上 SO_REUSEADDR 会让两个 socket 绑同一端口，
    # 连接被静默分流，极难诊断。
    try:
        probe = qoder_net.urlopen(
            "http://%s:%d/health"
            % ("127.0.0.1" if args.host == "0.0.0.0" else args.host, args.port),
            timeout=2)
        existing = json.loads(probe.read().decode("utf-8"))
    except Exception:
        existing = None   # 没人应答 /health —— 让下面的 bind 决定
    if isinstance(existing, dict):
        # 只有我们的 /health 带账号池字段（"accounts"）。其它服务可能占用
        # 同端口也应答 /health JSON（例如 wb-proxy / mimo-api-proxy）。
        foreign = existing.get("service") not in (None, "qoder-proxy") \
            or "accounts" not in existing
        if foreign:
            who = existing.get("service") or "an unknown HTTP service"
            print()
            print("  [ERROR] port %d is already taken by another program: %s"
                  % (args.port, who))
            print("          qd-proxy itself is NOT running - nothing was "
                  "started.")
            print()
            print("  Fix: start qoder-proxy on a different port, e.g.")
            print("          start-qoder-proxy.bat %d" % (args.port + 1))
            print("          python qoder_proxy.py --port %d" % (args.port + 1))
            print()
            print("  Check who owns the port:  netstat -ano | findstr :%d"
                  % args.port)
            print()
            raise SystemExit(1)
        print()
        print("  [已有一个反代在 %d 端口运行，无需重复启动]" % args.port)
        print("  账号: %s @ %s" % (existing.get("uid", "?"),
                                   existing.get("domain", "?")))
        print("  看板: http://127.0.0.1:%d/" % args.port)
        print()
        print("  如果要重启: 先把原来那个窗口关掉（或结束 python 进程），"
              "再运行本程序。")
        print()
        return

    runtime.API_KEY = args.api_key
    runtime.SYSTEM_PROMPT = args.system_prompt
    if args.accounts_dir:
        runtime.ACCOUNTS_DIR = os.path.abspath(args.accounts_dir)
    # 让 qoder_accounts 的机器身份固定设置读到同一个 accounts 目录
    # （机器身份存 accounts/settings.json，查表走 ACCOUNTS_DIR 环境变量）
    os.environ["ACCOUNTS_DIR"] = runtime.ACCOUNTS_DIR
    # LAN 模式绝不能带默认密钥：网关花的是账号自己的上游额度，
    # 可猜的默认值等于让全网段的人白嫖。首次生成一次并持久化。
    if args.lan and not runtime.API_KEY:
        runtime.API_KEY, API_KEY_GENERATED = qoder_settings.ensure_launcher_key(
            runtime.ACCOUNTS_DIR)
    # 面板保存的 Key 优先于自动生成的 LAN Key（.bat 重启后浏览器改动仍生效）；
    # 命令行 --api-key 依然最高优先级。
    
    saved_key, key_from_panel = qoder_settings.api_key_override(runtime.ACCOUNTS_DIR)
    if key_from_panel and not args.api_key:
        runtime.API_KEY = saved_key
        runtime.API_KEY_FILE_SET = True
    if args.panel_password:
        qoder_settings.set_panel_password(runtime.ACCOUNTS_DIR, args.panel_password)
        log("panel      : password set from --panel-password")
    elif qoder_settings.panel_password_is_default(runtime.ACCOUNTS_DIR):
        log("panel      : password is still the default 'admin' - change it "
            "in the panel")

    # 出站代理模式（面板「设置 → 网络代理」可改；环境变量覆盖优先）
    qoder_net.load_from_settings(runtime.ACCOUNTS_DIR)
    log("proxy      : %s" % qoder_net.describe()["effective"])

    runtime.POOL = qoder_accounts.AccountPool(runtime.ACCOUNTS_DIR, log=log)
    runtime.POOL.load()
    load_persisted_realm()
    from qoder2api.scheduler import Scheduler
    runtime.SCHEDULER = Scheduler(runtime.POOL)
    runtime.SCHEDULER.start()

    if not runtime.POOL.accounts:
        # 永不静默采用本机客户端登录：先报告扫描结果，由用户在看板确认导入。
        try:
            detected = qoder_accounts.scan_desktop_credentials()
        except Exception:
            detected = []
        usable = [d for d in detected if d.get("valid")]
        if usable:
            log("no accounts yet - detected %d local credential(s), NOT importing"
                % len(usable))
            for d in usable:
                log("  available: %s  %s  %s" % (
                    (d.get("uid") or "?")[:8], d.get("nickname") or "(no name)",
                    d.get("realmName") or d.get("realm")))
            log("open the dashboard and click [Scan local credentials] to import")
        else:
            log("no accounts yet - no local Qoder credentials found on this machine")
        # 不在这里退出：看板必须可达，才能通过浏览器完成登录。
    rep = current_account()
    log("accounts   : %d total, %d usable"
        % (len(runtime.POOL.accounts), runtime.POOL.count_ready()))
    for account in runtime.POOL.accounts:
        log("  - %s  %s  %s  %s" % (
            account.uid[:8], account.nickname or "(no name)",
            account.realm, account.domain))
    log("store      : %s" % runtime.ACCOUNTS_DIR)
    log("credential : %s" % (rep.path if rep else "-"))
    log("account    : %s @ %s" % (rep.uid if rep else "-",
                                  rep.domain if rep else "-"))
    if runtime.REALM_MODE == "both":
        log("realm      : both (优先 %s，失效自动切换另一区)" % runtime.REALM_PREFERRED)
    else:
        log("realm      : %s (%s)" % (
            runtime.CURRENT_REALM,
            "qoder.com" if runtime.CURRENT_REALM == "intl" else "qoder.com.cn"))
    log("catalog    : %s" % (BASEPROMPT_PATH if BASEPROMPT
                             else "baseprompt.json MISSING"))

    if args.host == "0.0.0.0":
        ips = local_ip_addresses() or ["<this-pc-ip>"]
        print()
        print("  " + "=" * 62)
        print("  LAN MODE - reachable from other devices")
        print()
        for ip in ips:
            print("    API       : http://%s:%s/v1" % (ip, args.port))
            print("    Dashboard : http://%s:%s/" % (ip, args.port))
        print()
        print("    API Key   : %s" % runtime.API_KEY)
        if API_KEY_GENERATED:
            print("                (newly generated & saved to "
                  "accounts/settings.json)")
        else:
            print("                (reused from accounts/settings.json)")
        print()
        print("    Open the dashboard (key already included):")
        print("      http://%s:%s/?key=%s" % (ips[0], args.port, runtime.API_KEY))
        print()
        print("    Clients: Base URL = the API address above, then paste "
              "the key.")
        print()
        print("    If nothing can connect, allow python through the")
        print("    firewall: run allow-firewall.bat once as administrator.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

    if not runtime.POOL.accounts:
        print()
        print("  " + "=" * 62)
        print("  NO ACCOUNTS YET")
        print()
        print("  Open the dashboard and click [+ 添加账号 (OAuth)]:")
        print("      http://127.0.0.1:%d/" % args.port)
        print()
        print("  The browser flow adds the account automatically.")
        print("  This window must stay open.")
        print("  " + "=" * 62)
        print()
        sys.stdout.flush()

    try:
        server = ThreadingHTTPServer((args.host, args.port), Handler)
    except OSError as exc:
        # bind 前探测与 bind 之间端口被抢，或被不答 /health 的程序占着
        print()
        print("  [ERROR] failed to listen on %s:%d - %s"
              % (args.host, args.port, exc))
        print("          the port is reserved or held by another program;")
        print("          qd-proxy did NOT start.")
        print()
        print("  Fix: stop the program holding the port, or pick another "
              "port:")
        print("          netstat -ano | findstr :%d" % args.port)
        print("          start-qoder-proxy.bat %d" % (args.port + 1))
        print()
        raise SystemExit(1)
    log("listening  : http://%s:%d/v1  (api key: %s)"
        % (args.host, args.port, "on" if runtime.API_KEY else "off"))
    log("dashboard  : http://%s:%d/" % (args.host, args.port))
    # 进程生命周期内保持 handler 引用：SetConsoleCtrlHandler 存的是裸指针，
    # 回调被 GC 会在关窗时崩溃。
    _ctrl_handler = install_console_close_handler()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("bye")
    finally:
        try:
            server.server_close()
        except Exception:
            pass
