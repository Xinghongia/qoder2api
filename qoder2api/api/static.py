"""qoder2api.api.static —— 托管前端静态产物（Next.js 静态导出 web/out）。

生产路径：`web` 用 `npm run build:export` 生成 `web/out/`（已提交入库），
本模块把它直接发给浏览器 —— 最终用户只需要 Python，不需要 Node。

规则：
  · 目录穿越防护：解析后的路径必须仍在 STATIC_DIR 内；
  · 路由候选顺序：精确文件 → `<path>/index.html`（trailingSlash 导出）→
    `<path>.html`；
  · **API 前缀命中时绝不回退到 index.html**，交回给路由层回 JSON 404；
  · `/_next/static/**` 内容寻址，可长缓存；其余 HTML no-store（与旧看板一致）。

`/`、`/dashboard` 指向新前端；`/ui` 301 到 `/`；`/legacy` 在新前端上线后
仍可访问旧看板（dashboard.html），下个版本删除。
"""
import os
import posixpath

from .. import paths

# 这些前缀属于后端接口：静态层一律不处理，避免把 API 404 变成 index.html
API_PREFIXES = (
    "/v1", "/accounts", "/usage", "/tasks", "/scheduler", "/settings", "/logs",
    "/panel", "/realm", "/diag", "/identity", "/update", "/health", "/healthz",
    "/ping", "/livez", "/readyz", "/models",
)

# 前端路由 → 导出文件（trailingSlash 导出目录形式）
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".map": "application/json; charset=utf-8",
}


def api_takes_path(path):
    """该 GET 路径是否归后端接口所有（静态层必须让路）。

    前端页面的路由名与接口撞车时（`/logs` 与 `/settings` 既是接口又是页面），
    用**尾斜杠**区分：导出产物是 `<route>/index.html`，`next/link` 在
    `trailingSlash: true` 下生成的就是 `/logs/`；而接口调用永远是不带尾斜杠的
    `/logs?since_id=...`。所以「无尾斜杠精确命中接口前缀」= 接口，
    「带尾斜杠」= 前端页面。
    """
    if path == "/":
        return False
    if path.endswith("/"):
        return False
    for prefix in API_PREFIXES:
        if path == prefix or path.startswith(prefix + "/"):
            return True
    return False


def _safe_join(root, url_path):
    """把 URL 路径安全地拼到 root 下；越界/非法返回 None。"""
    rel = posixpath.normpath("/" + (url_path or "/")).lstrip("/")
    if rel.startswith("..") or os.path.isabs(rel):
        return None
    candidate = os.path.normpath(os.path.join(root, *rel.split("/")))
    root_norm = os.path.normpath(root)
    if candidate != root_norm and not candidate.startswith(root_norm + os.sep):
        return None
    return candidate


def resolve(path):
    """返回 (文件绝对路径, 是否长缓存) 或 (None, False)。

    候选顺序：精确文件 → `<path>/index.html` → `<path>.html`。
    """
    root = str(paths.static_dir())
    if not os.path.isdir(root):
        return None, False
    clean = path.split("?", 1)[0]
    if not clean.startswith("/"):
        clean = "/" + clean
    target = _safe_join(root, clean)
    if target is None:
        return None, False
    for candidate in (target,
                      os.path.join(target, "index.html"),
                      target + ".html"):
        if os.path.isfile(candidate):
            return candidate, clean.startswith("/_next/static/")
    return None, False


def content_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    return _MIME.get(ext, "application/octet-stream")


def index_html_path():
    """新前端首页（web/out/index.html）；不可用时返回 None。"""
    root = str(paths.static_dir())
    candidate = os.path.join(root, "index.html")
    return candidate if os.path.isfile(candidate) else None


def legacy_dashboard_path():
    """旧单文件看板（迁移期挂在 /legacy）。"""
    root = str(paths.ROOT)
    for candidate in (str(paths.DASHBOARD_HTML),
                      os.path.join(root, "legacy", "dashboard.html")):
        if os.path.isfile(candidate):
            return candidate
    return None
