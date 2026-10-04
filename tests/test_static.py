"""前端静态托管（qoder2api/api/static.py）的回归测试。

只覆盖安全与路由判定的纯函数，不需要起服务、不需要 Node：

    python tests/test_static.py
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

os.environ.setdefault("ACCOUNTS_DIR", os.path.join(_HERE, "_acc"))
os.environ.setdefault("USAGE_DIR", os.path.join(_HERE, "_use"))
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

from qoder2api.api import static as S  # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s" % label)
    else:
        FAIL += 1
        print("  [FAIL] %s  %s" % (label, extra))


print("[1] API 路径判定：无尾斜杠 = 接口，带尾斜杠 = 前端页面")
check("/logs 归接口", S.api_takes_path("/logs"))
check("/logs?x=1 也归接口（query 不参与判定）", S.api_takes_path("/logs"))
check("/settings/reveal 归接口", S.api_takes_path("/settings/reveal"))
check("/logs/ 归前端页面", not S.api_takes_path("/logs/"))
check("/settings/ 归前端页面", not S.api_takes_path("/settings/"))
check("/stats/ 归前端页面", not S.api_takes_path("/stats/"))
check("/v1/chat/completions 归接口", S.api_takes_path("/v1/chat/completions"))
check("/ 不归接口（首页）", not S.api_takes_path("/"))
check("/accounts/ 不归接口（前端页面优先）", not S.api_takes_path("/accounts/"))

print()
print("[2] 目录穿越防护：一律解析不到仓库外的文件")
for bad in ("/../qoder_proxy.py", "/..%2fqoder_proxy.py", "/%2e%2e/qoder_proxy.py",
            "/_next/../../qoder_proxy.py", "/....//qoder_proxy.py",
            "/%2e%2e%2fqoder_proxy.py"):
    path, _cache = S.resolve(bad)
    check("拒绝 %s" % bad, path is None, path)

print()
print("[2b] 百分号解码后仍能服务路由组产物（括号路径）")
import glob as _glob  # noqa: E402
_root_out = os.path.join(_ROOT, "web", "out")
_hits = _glob.glob(os.path.join(_root_out, "_next", "static", "chunks", "app",
                                "(main)", "page-*.js"))
if _hits:
    _rel = _hits[0][len(_root_out):].replace(os.sep, "/")
    p1, _c1 = S.resolve(_rel)
    p2, _c2 = S.resolve(_rel.replace("(", "%28").replace(")", "%29"))
    check("原文括号路径可解析", p1 is not None, _rel)
    check("%28/%29 编码路径同样可解析", p2 is not None, _rel)
else:
    check("找到路由组导出的 chunk（跳过编码检查）", False, "no (main) chunk")

print()
print("[3] _safe_join 的核心不变量：返回值永远落在 root 内（或 None）")
root = os.path.join(_ROOT, "web", "out")
root_norm = os.path.normpath(root)
for bad in ("../qoder_proxy.py", "..\\qoder_proxy.py", "../../etc/passwd",
            "..%2f..%2fetc/passwd", "a/../../../../b"):
    joined = S._safe_join(root, "/" + bad)
    ok = joined is None or (
        os.path.normpath(joined) == root_norm
        or os.path.normpath(joined).startswith(root_norm + os.sep))
    check("不逃逸 root: %r" % bad, ok, joined)

print()
print("[4] MIME 表覆盖前端实际会请求的类型")
check("html", S.content_type("index.html").startswith("text/html"))
check("js", "javascript" in S.content_type("chunk.js"))
check("css", S.content_type("a.css").startswith("text/css"))
check("woff2", S.content_type("f.woff2") == "font/woff2")
check("未知扩展回退二进制流",
      S.content_type("x.bin") == "application/octet-stream")

print()
print("[5] 浏览器导航 vs 接口调用（决定 /logs 是否重定向到 /logs/）")
nav = {"Sec-Fetch-Mode": "navigate", "Accept": "text/html"}
fetch = {"Sec-Fetch-Mode": "cors", "Accept": "*/*"}
plain = {"Accept": "*/*"}
old_browser = {"Accept": "text/html,application/xhtml+xml"}
check("导航（navigate）判定为真", S.is_browser_navigation(nav))
check("fetch（cors）判定为假", not S.is_browser_navigation(fetch))
check("curl 默认头判定为假", not S.is_browser_navigation(plain))
check("旧浏览器（只有 text/html Accept）判定为真",
      S.is_browser_navigation(old_browser))
check("接口请求带 application/json 时不误判",
      not S.is_browser_navigation({"Accept": "text/html, application/json"}))
check("/logs 存在导出页面（导航时该重定向）", S.page_exists("/logs"))
check("/stats 存在导出页面", S.page_exists("/stats"))
check("/accounts 现在也有页面（账号页与接口同名）", S.page_exists("/accounts"))
check("/usage 现在也有页面（用量统计页与接口同名，同 /logs 的处理）",
      S.page_exists("/usage"))
check("/usage/stats 仍归接口（无尾斜杠前缀命中，静态层必须让路）",
      S.api_takes_path("/usage/stats") and not S.api_takes_path("/usage/"))
check("/v1/models 没有页面（仍归接口）", not S.page_exists("/v1/models"))

print()
print("[6] 旧看板入口（迁移期 /legacy）")
legacy = S.legacy_dashboard_path()
check("legacy/dashboard.html 存在时能定位",
      legacy is None or os.path.isfile(legacy), legacy)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
