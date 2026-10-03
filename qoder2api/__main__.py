"""python -m qoder2api 入口（等价于 python qoder_proxy.py）。"""
import sys

if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    from .cli import main

    main()
