"""qoder2api.chat_normalize —— 由 qoder_proxy.py 拆分而来。"""

import json
import re
import uuid



# DeepSeek 上游 model key（官方目录：dmodel=DeepSeek-V4-Pro / dfmodel=DeepSeek-Flash）。
# 这族模型的多轮一致性与 reasoning_content 强相关，见 is_deepseek_model。
DEEPSEEK_MODEL_KEYS = ("dmodel", "dfmodel")


def is_deepseek_model(model="", model_key=""):
    """判断这次请求最终打到的是不是一个 DeepSeek 上游模型。
    客户端完全可能直接写 `dfmodel`/`dmodel`（仓库文档里就把 DeepSeek-Flash
    标为「内部 key：dfmodel」），而展示名是 `DeepSeek-Flash`。此前只按名字
    前缀 "deepseek" 判断，走 key 的请求拿不到 reasoning_content 兼容处理，
    多轮会话会被上游拒绝——表现为"偶发失败、重试有时能过"。
    """
    key = str(model_key or "").strip().lower()
    if key in DEEPSEEK_MODEL_KEYS:
        return True
    low = str(model or "").strip().lower()
    if not low:
        return False
    if low in DEEPSEEK_MODEL_KEYS or low.startswith("deepseek"):
        return True
    # 展示 id「dfmodel (DeepSeek-Flash)」/ 别名 deepseek-v4-flash 等
    return "deepseek" in low


def backfill_reasoning_content(messages, model, model_key=""):
    """DeepSeek 多轮一致性：assistant 历史补 reasoning_content。

    触发条件只看"上游是不是 DeepSeek"，与客户端用 key 还是展示名无关。
    """
    if not is_deepseek_model(model, model_key):
        return messages
    has_trace = False
    for m in messages:
        if isinstance(m, dict):
            if m.get("reasoning") or "reasoning_content" in m:
                has_trace = True
                break
    if not has_trace:
        return messages
    out = []
    for m in messages:
        if isinstance(m, dict) and m.get("role") == "assistant":
            item = dict(m)
            if "reasoning_content" not in item:
                if item.get("reasoning"):
                    item["reasoning_content"] = str(item["reasoning"])
                else:
                    item["reasoning_content"] = ""
            out.append(item)
        else:
            out.append(m)
    return out


def normalize_tool_choice(obj):
    """把 OpenAI tool_choice 归一成上游可接受的形态（避免 400）。"""
    if "tool_choice" not in obj:
        return
    tc = obj["tool_choice"]
    if isinstance(tc, str):
        val = tc.strip().lower()
        if val == "none":
            obj.pop("tool_choice", None)
            obj.pop("tools", None)
        return
    if isinstance(tc, dict):
        typ = (tc.get("type") or "").strip().lower()
        if typ == "none":
            obj.pop("tool_choice", None)
            obj.pop("tools", None)
        elif typ in ("auto", "required"):
            obj["tool_choice"] = typ
        elif typ == "function":
            name = (tc.get("function") or {}).get("name") or tc.get("name") or ""
            obj["tool_choice"] = name.strip() or "auto"
        else:
            obj.pop("tool_choice", None)
    else:
        obj.pop("tool_choice", None)


def normalize_tools(obj):
    """把顶层 name 型工具定义包成 Chat Completions function schema。"""
    tools = obj.get("tools")
    if not tools or not isinstance(tools, list):
        return
    norm = []
    for t in tools:
        if not isinstance(t, dict):
            continue
        if "name" in t and "function" not in t and t.get("type") == "function":
            fn = {
                "name": t.get("name") or "",
                "description": t.get("description") or "",
                "parameters": t.get("parameters") or {},
            }
            if "strict" in t:
                fn["strict"] = t["strict"]
            norm.append({"type": "function", "function": fn})
        else:
            norm.append(t)
    obj["tools"] = norm


def translate_max_completion_tokens(obj):
    alias = obj.pop("max_completion_tokens", None)
    if alias is None:
        return
    if "max_tokens" in obj:
        return
    try:
        val = int(alias)
        if val > 0:
            obj["max_tokens"] = val
    except (TypeError, ValueError):
        pass


# ---------------------------------------------------------------------------
# DeepSeek DSML 工具调用回退解析
# ---------------------------------------------------------------------------
TAG_START = r"<[^>]*DSML[^>]*"
DSML_CALLS_RE = re.compile(TAG_START + r"calls>(.*?)</[^>]*DSML[^>]*calls>",
                           re.DOTALL)
DSML_INVOKE_RE = re.compile(
    TAG_START + r"invoke\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27]>(.*?)</[^>]*invoke>",
    re.DOTALL)
DSML_PARAM_RE = re.compile(
    TAG_START + r"parameter\s+name=[\x22\x27]([^\x22\x27]+)[\x22\x27][^>]*>(.*?)</[^>]*parameter>",
    re.DOTALL)


def parse_dsml_tool_calls(text):
    if not text or "DSML" not in text:
        return None, text
    match = DSML_CALLS_RE.search(text)
    if not match:
        return None, text
    calls_block = match.group(1)
    tool_calls = []
    for inv_match in DSML_INVOKE_RE.finditer(calls_block):
        func_name = inv_match.group(1)
        params_block = inv_match.group(2)
        params = {}
        for p_match in DSML_PARAM_RE.finditer(params_block):
            p_name = p_match.group(1)
            p_val = p_match.group(2).strip()
            params[p_name] = p_val
        tool_calls.append({
            "id": _new_id("call_"),
            "name": func_name,
            "arguments": json.dumps(params, ensure_ascii=False),
        })
    clean = (text[:match.start()].strip() + " " + text[match.end():].strip()).strip()
    return tool_calls, clean


def _new_id(prefix):
    return prefix + uuid.uuid4().hex
