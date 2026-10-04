"""qoder2api.chat_normalize —— 由 qoder_proxy.py 拆分而来。"""

import json
import re
import time
import uuid

from .logbus import log
from .sanitize import strip_data_prefix



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


# ---------------------------------------------------------------------------
# 历史工具调用文本形态的「回读」（issue #8，上游 v1.2.0/v1.2.1）
# ---------------------------------------------------------------------------
# flatten_messages() 会把 assistant 历史里的 tool_calls 序列化成
#   marker（LEAK_MARKER，见下）+ 一个 JSON 数组 [{name, arguments}, ...]
# 给上游模型当上下文。长会话里模型偶发**照格式复述**成正文（没有结构化
# tool_calls），客户端就会把这段 JSON 当正文显示、本轮工具调用不执行。
# 这里补齐回读：在严格守卫下还原成结构化 tool_calls 并从正文移除。
LEAK_MARKER = "[assistant 请求调用工具]"


def parse_leaked_tool_calls(text, allowed_names=None):
    """把模型复述的「marker + JSON 数组」还原成结构化 tool_calls。

    严格守卫（避免把"讨论该标记的普通回复"误判为工具调用）：
      1) 整段正文 strip 后必须**恰好**是 marker + JSON 数组（可带 ``` 围栏）；
      2) 数组非空，每项 name 为非空字符串、arguments 为合法 JSON（字符串或对象）；
      3) 本次请求声明了 tools 时，所有 name 必须命中声明集合。

    返回 (tool_calls|None, clean_text)：识别成功时 clean_text 为空串
    （整段都是该块）；未识别时原样返回 text。
    """
    t = (text or "").strip()
    # 整段被 ``` 围栏包裹时先剥壳（模型复述时常带围栏）
    if t.startswith("```") and t.endswith("```") and len(t) > 6:
        inner = t[3:]
        if inner[:4].lower() == "json":
            inner = inner[4:]
        t = inner[:-3].strip()
    if not t.startswith(LEAK_MARKER):
        return None, text
    rest = t[len(LEAK_MARKER):].strip()
    if not (rest.startswith("[") and rest.endswith("]")):
        return None, text
    try:
        arr = json.loads(rest)
    except Exception:
        return None, text
    if not isinstance(arr, list) or not arr:
        return None, text
    calls = []
    for item in arr:
        if not isinstance(item, dict):
            return None, text
        name = item.get("name")
        args = item.get("arguments")
        if not isinstance(name, str) or not name.strip():
            return None, text
        if isinstance(args, (dict, list)):
            args = json.dumps(args, ensure_ascii=False)
        if not isinstance(args, str):
            return None, text
        if args.strip():
            try:
                json.loads(args)
            except Exception:
                return None, text
        calls.append({
            "id": _new_id("call_"),
            "type": "function",
            "function": {"name": name.strip(), "arguments": args},
        })
    if allowed_names:
        if not all(c["function"]["name"] in allowed_names for c in calls):
            return None, text
    return calls, ""


def _leak_body_offset(text):
    """把「marker 之前的围栏前缀 / marker 之后的正文」解析出来。

    返回 (state, body)：state='pre'（还处在围栏/前缀区，未到 marker）、
    state='body'（已越过 marker，body 为其后文本）、state='no'（显然不是）。
    流式暂存与判真共用同一套前缀规则（含 ``` 与 ```json 围栏）。
    """
    t = (text or "").lstrip()
    if not t or LEAK_MARKER.startswith(t):
        return "pre", None
    if t.startswith(LEAK_MARKER):
        return "body", t[len(LEAK_MARKER):]
    if not t.startswith("`"):
        return "no", None
    if len(t) < 3:
        return ("pre", None) if "```".startswith(t) else ("no", None)
    if not t.startswith("```"):
        return "no", None
    rest = t[3:]
    low = rest.lstrip().lower()
    if low.startswith("json"):
        after = rest.lstrip()[4:].lstrip()
    elif LEAK_MARKER.startswith(low) or low.startswith(LEAK_MARKER):
        after = rest.lstrip()          # 无语言标记的 ``` 围栏
    elif "json".startswith(low):
        return "pre", None             # 语言标记还没打完（j/js/json）
    else:
        return "no", None
    if LEAK_MARKER.startswith(after):
        return "pre", None
    if after.startswith(LEAK_MARKER):
        return "body", after[len(LEAK_MARKER):]
    return "no", None


def leak_prefix_hold(text):
    """文本仍可能是泄漏块的前缀？（流式 hold-back 用）"""
    return _leak_body_offset(text)[0] != "no"


def tool_names_from_payload(payload):
    """取客户端本次请求声明的工具名集合（回读守卫用）。"""
    names = set()
    for t in (payload or {}).get("tools") or []:
        if not isinstance(t, dict):
            continue
        fn = t.get("function") if isinstance(t.get("function"), dict) else t
        name = fn.get("name") or ""
        if name:
            names.add(str(name))
    return names or None


def _json_array_prefix_ok(text):
    """text 是否仍可能扩展成合法 JSON 数组字面量（流式暂存判定用）。"""
    s = (text or "").strip()
    if not s:
        return True
    if not s.startswith("["):
        return False
    try:
        json.loads(s)
        return True
    except Exception as exc:
        msg = str(exc)
        # 未闭合字符串的报错位置在字符串开头，但仍可继续扩展
        if msg.startswith("Unterminated string"):
            return True
        pos = getattr(exc, "pos", None)
        if pos is None:
            return False
        if msg.startswith("Extra data"):
            # 数组已完整，尾随的只能是（可能还没打完的）``` 收尾围栏
            tail = s[pos:].strip()
            return (not tail) or "```".startswith(tail)
        # 语法错误点落在末尾（未闭合括号等）→ 还允许继续扩展
        return pos >= len(s) - 1


def _still_leak_candidate(text):
    """整段文本目前是否仍是「marker + JSON 数组」候选？（流式暂存判定用）"""
    state, body = _leak_body_offset(text)
    if state == "pre":
        return True
    if state == "no":
        return False
    rest = (body or "").lstrip()
    if not rest:
        return True
    if rest.endswith("```"):
        rest = rest[:-3]
    return _json_array_prefix_ok(rest)


def leaked_partial_droppable(text, allowed_names=None):
    """issue #9：正文是网关自己写入历史的工具调用回声、但被截断（JSON 不完整/
    非法）时，应吞掉整段而不是透传给终端用户。三个条件同时成立才吞：
      1) strip 后以 LEAK_MARKER 开头（真实散文回复几乎不会这样开头）；
      2) 本次请求声明了 tools（allowed_names 非空）——与恢复路径同前置条件；
      3) marker 之后（剥掉围栏后）仍是 JSON 数组字面量的前缀
         （复用现成的 _json_array_prefix_ok）——即"本该是数组，只是没写完"。
    返回 True = 应吞掉正文（输出空 content）。

    完整且合法的 JSON 数组不算：那种情况下恢复失败另有原因（工具名未声明 /
    arguments 非法），保持既有 fail-open 透传语义（见 tests 里"未声明工具名
    -> 不吞正文"的断言），只有"数组本身没写完 / 非法"才吞。
    """
    if not text or not allowed_names:
        return False
    t = text.strip()
    if not t:
        return False
    if t == LEAK_MARKER:
        # 只有 marker、连数组都没开始写：判据 3 的退化情形，同样是回声。
        return True
    state, body = _leak_body_offset(t)
    if state != "body":
        return False
    rest = (body or "").strip()
    if rest.endswith("```"):
        rest = rest[:-3].strip()
    if not _json_array_prefix_ok(rest):
        return False
    try:
        json.loads(rest)
        return False
    except Exception:
        return True


def _leak_chunk_frame(meta, delta, finish_reason=None):
    """按流式 chat.completion.chunk 形态合成一帧（沿用上游的 id/model）。"""
    payload = {
        "id": meta.get("id") or "chatcmpl-qoder",
        "object": "chat.completion.chunk",
        "created": meta.get("created") or int(time.time()),
        "model": meta.get("model") or "",
        "choices": [{"index": 0, "delta": delta,
                     "finish_reason": finish_reason}],
    }
    return ("data: " + json.dumps(payload, ensure_ascii=False)
            + "\n\n").encode("utf-8")


def _frame_without_content(payload):
    """复制一帧并去掉 delta.content（保留 tool_calls/reasoning/收尾信息）。"""
    out = dict(payload)
    choices = []
    for ch in payload.get("choices") or []:
        if not isinstance(ch, dict):
            choices.append(ch)
            continue
        ch2 = dict(ch)
        delta = ch2.get("delta")
        if isinstance(delta, dict) and "content" in delta:
            d2 = dict(delta)
            d2.pop("content", None)
            ch2["delta"] = d2
        choices.append(ch2)
    out["choices"] = choices
    return out


def recover_leaked_tool_calls(frames, allowed_names=None):
    """流式回读（issue #8）：把模型逐块吐出的「marker + JSON 数组」正文恢复
    成结构化 tool_calls 增量帧；其余内容原样透传（fail-open）。

    与 parse_leaked_tool_calls 同一套严格守卫，另加：
      - 只有当已输出的正文仍是该块的「前缀/候选」时才暂存（期间不发正文）；
      - 候选被证伪（出现别的字 / JSON 非法）→ 立刻把暂存整段补发为正文；
      - 收尾帧暂存到流末，识别成功时改写 finish_reason=tool_calls；
      - 上游中断时暂存直接丢弃（正文未发，交给上游重试/错误帧处理）。
    """
    hold = None          # 正在暂存的候选正文（None = 未暂存）
    finished = None      # 暂存的收尾帧：(payload, 原 finish_reason)
    recovered = False
    meta = {"id": None, "model": None, "created": None}

    for raw in frames:
        text = (raw.decode("utf-8", "replace")
                if isinstance(raw, bytes) else raw)
        data = strip_data_prefix(text)
        payload = None
        if data and data != "[DONE]":
            try:
                payload = json.loads(data)
            except Exception:
                payload = None
        if not isinstance(payload, dict):
            yield raw
            continue
        if payload.get("id"):
            meta["id"] = payload["id"]
        if payload.get("model"):
            meta["model"] = payload["model"]
        if payload.get("created"):
            meta["created"] = payload["created"]
        choices = payload.get("choices") or []
        choice = (choices[0] if choices and isinstance(choices[0], dict)
                  else {})
        delta = choice.get("delta") or {}
        piece_raw = delta.get("content")
        piece = piece_raw if isinstance(piece_raw, str) else ""
        fin = choice.get("finish_reason")
        if piece_raw is not None and not isinstance(piece_raw, str):
            # 非字符串 content（罕见）：不参与暂存判断，原样透传
            if hold is not None:
                yield _leak_chunk_frame(meta, {"content": hold})
                hold = None
            yield raw
            continue
        if hold is not None:
            hold += piece
            if _still_leak_candidate(hold):
                if fin:
                    finished = (payload, fin)
                elif (delta.get("tool_calls")
                      or delta.get("reasoning_content")
                      or delta.get("role")):
                    other = _frame_without_content(payload)
                    yield ("data: "
                           + json.dumps(other, ensure_ascii=False)
                           + "\n\n").encode("utf-8")
                continue
            # 证伪：暂存整段补发为普通正文
            yield _leak_chunk_frame(meta, {"content": hold})
            hold = None
            if fin:
                finished = (payload, fin)
            continue
        if fin and not piece:
            finished = (payload, fin)
            continue
        if piece and leak_prefix_hold(piece):
            hold = piece
            if fin:
                finished = (payload, fin)
            continue
        if piece and fin:
            yield _leak_chunk_frame(meta, {"content": piece}, fin)
            continue
        if piece:
            yield _leak_chunk_frame(meta, {"content": piece})
            continue
        yield raw

    if hold is not None:
        calls, clean = parse_leaked_tool_calls(hold, allowed_names)
        if calls:
            recovered = True
            for i, call in enumerate(calls):
                yield _leak_chunk_frame(meta, {"tool_calls": [{
                    "index": i,
                    "id": call.get("id"),
                    "type": call.get("type") or "function",
                    "function": call.get("function") or {},
                }]})
            if clean:
                yield _leak_chunk_frame(meta, {"content": clean})
        elif leaked_partial_droppable(hold, allowed_names):
            # issue #9：截断的 marker+JSON 回声无法还原成结构化调用，吞掉整段，
            # 绝不把网关内部协议文本透传给终端用户。
            log("dropped truncated leaked tool-call echo (%d chars) — issue #9"
                % len(hold), level="WARN", tag="chat")
        else:
            yield _leak_chunk_frame(meta, {"content": hold})
    if finished is not None:
        payload, fin = finished
        if recovered:
            payload = json.loads(json.dumps(payload))
            for ch in payload.get("choices") or []:
                if isinstance(ch, dict) and ch.get("finish_reason"):
                    ch["finish_reason"] = "tool_calls"
        yield ("data: " + json.dumps(payload, ensure_ascii=False)
               + "\n\n").encode("utf-8")
    elif recovered:
        yield _leak_chunk_frame(meta, {}, "tool_calls")
