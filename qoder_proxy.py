#!/usr/bin/env python3
"""qoder_proxy.py —— 兼容入口（实现在 qoder2api/ 包里）。

保留本文件是为了让 start-qoder-proxy.bat / Dockerfile / 文档里的
`python qoder_proxy.py --host ... --port ...` 照旧可用；真实代码按职责
拆在 qoder2api/ 包内，等价入口：`python -m qoder2api`。
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from qoder2api.cli import main  # noqa: E402

if __name__ == "__main__":
    main()
