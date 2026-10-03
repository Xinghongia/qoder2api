"""qoder2api.api —— HTTP 层：Handler 由各组路由 mixin 组合而成。

    base             BaseHTTPRequestHandler 基类 + SSE/JSON/鉴权/面板公共方法
    routes_get       do_GET（状态、模型、用量、账号列表、日志等读接口）
    routes_post      do_POST（Chat/Completions 主链路与分发）
    panel_routes     面板登录 / 设置保存
    accounts_routes  账号、任务、调度、日志等管理端点
    responses_routes /v1/responses
"""
from .base import HandlerBase
from .routes_get import GetRoutesMixin
from .routes_post import PostRoutesMixin
from .panel_routes import PanelRoutesMixin
from .accounts_routes import AccountsRoutesMixin
from .responses_routes import ResponsesRoutesMixin


class Handler(PanelRoutesMixin, AccountsRoutesMixin, ResponsesRoutesMixin,
              GetRoutesMixin, PostRoutesMixin, HandlerBase):
    """完整 HTTP 处理器（方法分组在各 mixin 文件里定义）。"""
