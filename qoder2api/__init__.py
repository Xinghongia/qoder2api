"""qoder2api —— Qoder 多账号反向代理网关（纯标准库，零第三方依赖）。

包内模块（依赖方向自上而下，禁止反向 import）：
    paths/runtime                    路径与可变运行态
    logbus fingerprint sign settings 叶子工具
    net accounts tasks scheduler     账号与任务
    catalog                          模型目录
    api/*                             HTTP 服务
    cli                              入口
"""

__version__ = "1.2.13"
VERSION = __version__
