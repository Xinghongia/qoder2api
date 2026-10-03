"""qoder2api.reasoning —— 由 qoder_proxy.py 拆分而来。"""

import json
import re




# 思考档位的官方词表顺序（用于把客户端档位映射到模型支持档位）
EFFORT_RANK = {"none": 0, "minimal": 1, "low": 2, "medium": 3, "high": 4,
               "xhigh": 5, "max": 6}
_EFFORT_OFF = ("none", "off", "disabled", "disable", "false", "0", "no")


def supported_efforts(meta):
    """该模型官方支持的思考档位（取自目录 thinking_config.enabled.efforts）。

    空列表表示该模型只有"开/关"没有档位（如 cn 的 qmodel）。
    """
    tc = (meta or {}).get("thinking_config") or {}
    enabled = tc.get("enabled") if isinstance(tc.get("enabled"), dict) else {}
    efforts = enabled.get("efforts") if isinstance(enabled.get("efforts"), dict) else {}
    return [e for e in EFFORT_RANK if e in efforts]


def default_effort(meta):
    """该模型的默认思考档位（is_default 标注项），无则取支持表中间档。"""
    tc = (meta or {}).get("thinking_config") or {}
    enabled = tc.get("enabled") if isinstance(tc.get("enabled"), dict) else {}
    efforts = enabled.get("efforts") if isinstance(enabled.get("efforts"), dict) else {}
    for e, cfg in efforts.items():
        if isinstance(cfg, dict) and cfg.get("is_default"):
            return e
    sup = supported_efforts(meta)
    return sup[len(sup) // 2] if sup else ""


def normalize_reasoning_effort(effort, meta):
    """把请求里的思考档位归一化到该模型**官方支持**的档位集合。

    为什么必须做：上游对不支持的档位**不报错、直接忽略**（回落到模型默认档）。
    实测：dfmodel 官方只支持 low/high/max（默认 max），传 medium/xhigh 时输出
    与默认档一致——客户端以为"思考档位没生效"；cn 的 qmodel 只有开/关，传任何
    档位都被忽略（只有 none 能关掉思考）。

    规则：
      - 关闭类取值（none/off/disabled/...）统一映射为 "none"（官方通用关闭值）；
      - 模型有档位表：命中则原样透传；未命中取"最近的合法档位"（同距时偏向
        模型默认档），并给出说明性 note；
      - 模型有 thinking_config 但无档位表（仅开/关）：除 none 外不下发该参数
        （避免给上游发它不认识的档位）；
      - 模型没有 thinking_config（如路由器 `auto`）：**原样透传**，不做猜测。

    返回 (value|None, note)：value=None 表示不下发 reasoning_effort。
    """
    e = str(effort or "").strip().lower()
    if not e:
        return None, ""
    if e in _EFFORT_OFF:
        return "none", ""
    tc = (meta or {}).get("thinking_config")
    if not isinstance(tc, dict):
        # 目录里没有该模型的思考配置（路由器/未知模型）：不猜测，原样透传
        return e, ""
    sup = supported_efforts(meta)
    if not sup:
        return None, "dropped (model has no effort levels; use none to disable)"
    if e in sup:
        return e, ""
    if e not in EFFORT_RANK:
        return default_effort(meta) or sup[0], \
            "unknown level -> %s" % (default_effort(meta) or sup[0])
    d = default_effort(meta)
    want = EFFORT_RANK[e]

    def key(cand):
        return (abs(EFFORT_RANK[cand] - want),
                abs(EFFORT_RANK[cand] - EFFORT_RANK.get(d, want)))

    best = sorted(sup, key=key)[0]
    return best, "unsupported level %s -> %s" % (e, best)


# ---- 官方协议：上下文窗口 + 思考预算/档位（0.4.3 桌面端 / CLI 1.1.62） ----
# 档位 ↔ 思考预算的官方换算表（CLI: Mvc / JX()）：
EFFORT_BUDGET = {"none": 0, "low": 1024, "medium": 8192, "high": 24576,
                 "xhigh": 49152, "max": 65536}
EFFORT_BUDGET_STEPS = ((0, "none"), (1024, "low"), (8192, "medium"),
                       (24576, "high"), (49152, "xhigh"))


def effort_from_budget(value):
    """思考预算（token 数）→ 官方档位（CLI: JX()）。非数字/缺省返回 ""。"""
    try:
        n = int(value)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return "none"
    for ceiling, level in EFFORT_BUDGET_STEPS:
        if n <= ceiling:
            return level
    return "max"


def available_context_windows(meta):
    """目录里声明的上下文窗口（token 数，去重后升序）。

    优先官方 context_config 多窗口 → available_context_windows 字段。
    推导出来的 128K/200K 不算"声明"，只用于 UI 选项（见 window_choices）。
    """
    meta = meta or {}
    windows = []
    ctx = meta.get("context_config")
    if isinstance(ctx, dict):
        for conf in ctx.values():
            if isinstance(conf, dict) and conf.get("token_count"):
                try:
                    windows.append(int(conf["token_count"]))
                except (TypeError, ValueError):
                    pass
    if not windows:
        raw = meta.get("available_context_windows")
        if isinstance(raw, (list, tuple)):
            for w in raw:
                try:
                    w = int(w)
                except (TypeError, ValueError):
                    continue
                if w > 0:
                    windows.append(w)
    return sorted(set(windows))


def window_choices(meta):
    """可展示的窗口选项：声明的表优先，否则按官方 WX() 推导。"""
    wins = available_context_windows(meta)
    if wins:
        return wins
    try:
        top = int((meta or {}).get("max_input_tokens") or 0)
    except (TypeError, ValueError):
        top = 0
    if top <= 0:
        return []
    return sorted({w for w in (128000, 200000, top) if w <= top})


def window_supported(meta, want):
    """官方 jX()：有声明表时必须命中表内值；无表时不得超过 max_input_tokens。"""
    try:
        want = int(want)
    except (TypeError, ValueError):
        return False
    if want <= 0:
        return False
    wins = available_context_windows(meta)
    if wins:
        return want in wins
    try:
        top = int((meta or {}).get("max_input_tokens") or 0)
    except (TypeError, ValueError):
        top = 0
    return top <= 0 or want <= top


def resolve_context_window(meta, want):
    """把请求/默认里的窗口值解析成可下发的 token 数。

    支持 token 数或 "1M"/"400K" 标签；越界时取**不小于要求的最近窗口**
    （官方是直接丢弃用默认，这里让客户端的意图尽量达成并记日志）。
    返回 (value|None, note)：value=None 表示不下发 context_length。
    """
    if want in (None, ""):
        return None, ""
    text = str(want).strip().upper().replace("_", "")
    if not text.isdigit():
        mult = 1
        # 官方目录标签是 1000 进制（"200K"=200000、"1M"=1000000）
        if text.endswith("K"):
            mult, text = 1000, text[:-1]
        elif text.endswith("M"):
            mult, text = 1000000, text[:-1]
        try:
            want = int(float(text) * mult)
        except (TypeError, ValueError):
            return None, "ignored (invalid context window: %s)" % want
    want = int(want)
    if want <= 0:
        return None, ""
    if window_supported(meta, want):
        return want, ""
    wins = available_context_windows(meta)
    if not wins:
        # 无声明表（max_input_tokens 为王）：原样下发，上游自己裁决
        return want, ""
    bigger = [w for w in wins if w >= want]
    best = bigger[0] if bigger else wins[-1]
    return best, "unsupported window %d -> %d" % (want, best)


def client_thinking(payload):
    """从请求里提取客户端的思考意图。

    返回 {"effort": str, "budget": int|None, "disable": bool}。覆盖字段：
      - 档位：reasoning_effort / reasoning.effort / thinking.effort|level
      - 预算：thinking.budget_tokens / reasoning.budget_tokens /
        thinking_budget / reasoning_budget_tokens（数字，官方 JX() 换算）
      - 开关：enable_thinking=false / thinking.type=disabled /
        reasoning.enabled=false（反向 enabled=true 且带预算走预算）
    """
    out = {"effort": "", "budget": None, "disable": False}
    effort = payload.get("reasoning_effort")
    reasoning = payload.get("reasoning") if isinstance(payload.get("reasoning"), dict) else {}
    thinking = payload.get("thinking") if isinstance(payload.get("thinking"), dict) else {}
    if not effort:
        effort = reasoning.get("effort")
    if not effort:
        effort = thinking.get("effort") or thinking.get("level")
    if effort:
        out["effort"] = str(effort).strip().lower()
    budget = thinking.get("budget_tokens")
    if budget is None:
        budget = thinking.get("budget")
    if budget is None:
        budget = reasoning.get("budget_tokens")
    if budget is None:
        budget = payload.get("thinking_budget") \
            or payload.get("reasoning_budget_tokens")
    if budget not in (None, ""):
        try:
            out["budget"] = int(budget)
        except (TypeError, ValueError):
            out["budget"] = None
    th_type = str(thinking.get("type") or "").strip().lower()
    if payload.get("enable_thinking") is False or th_type == "disabled" \
            or reasoning.get("enabled") is False:
        out["disable"] = True
    return out
