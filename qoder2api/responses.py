"""qoder2api.responses —— 由 qoder_proxy.py 拆分而来。"""

import json
import time
import uuid

from . import usage
from . import reasoning
from . import chat_normalize
from . import model_entry
from . import sanitize
import socket

from .logbus import log
from .sanitize import strip_data_prefix
from .chat_normalize import (_new_id, parse_dsml_tool_calls,
                              DSML_CALLS_RE, parse_leaked_tool_calls,
                              leak_prefix_hold, leaked_partial_droppable)




# ---------------------------------------------------------------------------
# Responses API (/v1/responses) <-> Chat Completions translation
# ---------------------------------------------------------------------------
def local_ip_addresses():
    """Every non-loopback IPv4 address this machine answers on."""
    found = []
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in found and not ip.startswith("127."):
                found.append(ip)
    except Exception:
        pass
    if not found:
        try:
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            probe.connect(("8.8.8.8", 80))
            found.append(probe.getsockname()[0])
            probe.close()
        except Exception:
            pass
    return found


def _flatten_content(content):
    """Flatten Responses-style content into text, or OpenAI vision parts."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content)
    texts, parts = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            parts.append({"type": "text", "text": piece})
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("input_text", "output_text", "text", "summary_text"):
            t = piece.get("text") or ""
            texts.append(t)
            parts.append({"type": "text", "text": t})
        elif ptype in ("input_image", "image_url", "image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = "data:%s;base64,%s" % (mime, piece["data"])
            if url:
                parts.append({"type": "image_url", "image_url": {"url": url}})
    if any(p.get("type") == "image_url" for p in parts):
        return parts          # multimodal: keep structured parts
    return "\n".join(t for t in texts if t)


# ---------------------------------------------------------------------------
# Responses API "custom" (freeform) tools
#
# Codex 等客户端把文件编辑工具声明为 custom freeform tool：
#     {"type": "custom", "name": "apply_patch", "format": {...grammar...}}
# 上游 chat 端点没有 custom 概念 -> 出站降级为单 "input" 字符串参数的
# function tool，入站再还原为 custom_tool_call。否则工具被静默忽略。
# ---------------------------------------------------------------------------
CUSTOM_TOOL_HINT = (
    "This is a freeform tool. Put the COMPLETE raw payload into the single "
    "'input' string parameter, verbatim. Do not wrap it in JSON, do not wrap "
    "it in markdown code fences, do not add commentary."
)


def _is_custom_tool(tool):
    return isinstance(tool, dict) and str(tool.get("type") or "").lower() == "custom"


def custom_tool_names(tools):
    """Names of tools declared as freeform/custom in a Responses request."""
    names = set()
    for t in tools or []:
        if _is_custom_tool(t) and t.get("name"):
            names.add(str(t["name"]))
    return names


def _downgrade_custom_tool(tool):
    """Rewrite a Responses custom tool into a Chat function tool."""
    desc = tool.get("description") or ""
    fmt = tool.get("format") or {}
    extra = ""
    if isinstance(fmt, dict) and fmt.get("definition"):
        extra = "\n\nGrammar:\n" + str(fmt["definition"])
    elif isinstance(fmt, dict) and fmt.get("syntax"):
        extra = "\n\nGrammar syntax: %s" % str(fmt["syntax"])
    return {
        "type": "function",
        "name": tool.get("name") or "",
        "description": (desc + "\n\n" + CUSTOM_TOOL_HINT + extra).strip(),
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "Complete raw payload for this tool, verbatim.",
                }
            },
            "required": ["input"],
        },
    }


def _tools_for_chat(tools):
    """Downgrade custom tools; leave everything else untouched."""
    out = []
    for t in tools or []:
        if not isinstance(t, dict):
            continue
        out.append(_downgrade_custom_tool(t) if _is_custom_tool(t) else t)
    return out


def _unwrap_custom_input(args):
    """Pull the freeform string back out of an {"input": "..."} argument blob."""
    if not isinstance(args, str):
        return json.dumps(args or "", ensure_ascii=False)
    try:
        parsed = json.loads(args)
    except Exception:
        return args
    if isinstance(parsed, dict):
        val = parsed.get("input")
        if isinstance(val, str):
            return val
        if val is not None:
            return json.dumps(val, ensure_ascii=False)
    if isinstance(parsed, str):
        return parsed
    return args


def responses_to_chat(payload):
    """Translate a Responses API request body into a Chat Completions body."""
    messages = []
    instructions = payload.get("instructions")
    if isinstance(instructions, str) and instructions.strip():
        messages.append({"role": "system", "content": instructions})
    inp = payload.get("input")
    pending_reasoning = ""
    if isinstance(inp, str):
        messages.append({"role": "user", "content": inp})
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                messages.append({"role": "user", "content": item})
                continue
            if not isinstance(item, dict):
                continue
            itype = item.get("type")
            if itype in (None, "message"):
                body = _flatten_content(item.get("content"))
                if body:
                    role = item.get("role") or "user"
                    if role == "developer":
                        role = "system"
                    # 与相邻 function_call 的 assistant 文本合并到同一条，
                    # 避免打断 tool 序列
                    if role == "assistant" and messages \
                            and messages[-1].get("role") == "assistant":
                        prev = messages[-1]
                        if prev.get("content"):
                            prev["content"] = str(prev["content"]) + "\n" + str(body)
                        else:
                            prev["content"] = body
                        if pending_reasoning and "reasoning_content" not in prev:
                            prev["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                    else:
                        msg_dict = {"role": role, "content": body}
                        if role == "assistant" and pending_reasoning:
                            msg_dict["reasoning_content"] = pending_reasoning
                            pending_reasoning = ""
                        messages.append(msg_dict)
            elif itype == "reasoning":
                r_text = ""
                summ = item.get("summary")
                if isinstance(summ, list):
                    r_text = "\n".join(p.get("text", "") for p in summ
                                       if isinstance(p, dict) and p.get("text"))
                elif isinstance(summ, str):
                    r_text = summ
                if not r_text:
                    cnt = item.get("content")
                    if isinstance(cnt, str):
                        r_text = cnt
                    elif isinstance(cnt, list):
                        r_text = _flatten_content(cnt)
                if r_text:
                    if messages and messages[-1].get("role") == "assistant":
                        messages[-1]["reasoning_content"] = r_text
                    else:
                        pending_reasoning = r_text
            elif itype == "function_call_output":
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    if raw_out.get("type") in ("input_image", "image_url", "image") \
                            or "image_url" in raw_out:
                        content = _flatten_content([raw_out])
                    else:
                        content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            elif itype == "function_call":
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": item.get("arguments") or "{}",
                    },
                }
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {"role": "assistant", "content": "",
                                "tool_calls": [tc_item]}
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call":
                raw_input = item.get("input")
                if isinstance(raw_input, (dict, list)):
                    raw_input = json.dumps(raw_input, ensure_ascii=False)
                if not isinstance(raw_input, str):
                    raw_input = "" if raw_input is None else str(raw_input)
                tc_item = {
                    "id": item.get("call_id") or item.get("id") or "",
                    "type": "function",
                    "function": {
                        "name": item.get("name") or "",
                        "arguments": json.dumps({"input": raw_input},
                                                ensure_ascii=False),
                    },
                }
                if messages and messages[-1].get("role") == "assistant":
                    prev = messages[-1]
                    if "tool_calls" in prev:
                        prev["tool_calls"].append(tc_item)
                    else:
                        prev["tool_calls"] = [tc_item]
                    if pending_reasoning and "reasoning_content" not in prev:
                        prev["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                else:
                    msg_dict = {"role": "assistant", "content": "",
                                "tool_calls": [tc_item]}
                    if pending_reasoning:
                        msg_dict["reasoning_content"] = pending_reasoning
                        pending_reasoning = ""
                    messages.append(msg_dict)
            elif itype == "custom_tool_call_output":
                raw_out = item.get("output")
                if isinstance(raw_out, list):
                    content = _flatten_content(raw_out)
                elif isinstance(raw_out, dict):
                    content = json.dumps(raw_out, ensure_ascii=False)
                elif isinstance(raw_out, str):
                    content = raw_out
                else:
                    content = str(raw_out)
                messages.append({
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": content,
                })
            else:
                # 未知 item 不静默丢弃：丢工具调用会让上游记录不一致
                log("responses: WARNING unhandled input item type=%r keys=%s"
                    % (itype, sorted(item.keys())[:8]))
    chat = {"model": payload.get("model"), "messages": messages}
    for key in ("temperature", "top_p", "seed"):
        if payload.get(key) is not None:
            chat[key] = payload[key]
    if payload.get("max_output_tokens") is not None:
        chat["max_tokens"] = payload["max_output_tokens"]
    effort = None
    reasoning = payload.get("reasoning")
    if isinstance(reasoning, dict):
        effort = reasoning.get("effort")
    if not effort:
        effort = payload.get("reasoning_effort")
    if effort:
        chat["reasoning_effort"] = effort
    if payload.get("tools"):
        chat["tools"] = _tools_for_chat(payload["tools"])
    if payload.get("tool_choice"):
        chat["tool_choice"] = payload["tool_choice"]
    if payload.get("parallel_tool_calls") is not None:
        chat["parallel_tool_calls"] = payload["parallel_tool_calls"]
    return chat


def _responses_usage(u):
    if not u:
        return None
    det = u.get("completion_tokens_details") or {}
    pdet = u.get("prompt_tokens_details") or {}
    return {
        "input_tokens": u.get("prompt_tokens") or 0,
        "input_tokens_details": {
            "cached_tokens": u.get("prompt_cache_hit_tokens")
            or det.get("cached_tokens") or pdet.get("cached_tokens") or 0,
        },
        "output_tokens": u.get("completion_tokens") or 0,
        "output_tokens_details": {
            "reasoning_tokens": det.get("reasoning_tokens") or 0},
        "total_tokens": u.get("total_tokens") or 0,
    }


def chat_to_response(chat_obj, model, custom_names=None):
    """Fold a Chat Completions object into a Responses API response object.

    custom_names: the set of tool names the client declared as freeform.
    Calls to those tools are re-inflated into custom_tool_call items so
    clients such as Codex recognise them.
    """
    custom_names = custom_names or set()
    choice = (chat_obj.get("choices") or [{}])[0]
    msg = choice.get("message") or {}
    text = msg.get("content") or ""
    reasoning = msg.get("reasoning_content") or ""
    output = []
    if reasoning:
        output.append({
            "id": _new_id("rs_"),
            "type": "reasoning",
            "status": "completed",
            "summary": [{"type": "summary_text", "text": reasoning}],
        })
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function") or {}
        call_id = tc.get("id") or _new_id("call_")
        name = fn.get("name") or ""
        if name and name in custom_names:
            output.append({
                "id": _new_id("ctc_"),
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "input": _unwrap_custom_input(fn.get("arguments") or ""),
            })
        else:
            output.append({
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": call_id,
                "name": name,
                "arguments": fn.get("arguments") or "{}",
            })
    # DeepSeek DSML 工具调用回退
    if not (msg.get("tool_calls")):
        dsml_calls, clean_t = parse_dsml_tool_calls(text)
        if dsml_calls:
            for dc in dsml_calls:
                output.append({
                    "id": _new_id("fc_"),
                    "type": "function_call",
                    "status": "completed",
                    "call_id": dc.get("id") or _new_id("call_"),
                    "name": dc.get("name") or "",
                    "arguments": dc.get("arguments") or "{}",
                })
            text = clean_t
    if text or not output:
        output.append({
            "id": _new_id("msg_"),
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": text,
                         "annotations": []}] if text else [],
        })
    finish = choice.get("finish_reason") or "stop"
    obj = {
        "id": _new_id("resp_"),
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed" if finish != "length" else "incomplete",
        "model": model,
        "output": output,
        "output_text": text,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "metadata": {},
    }
    u = _responses_usage(chat_obj.get("usage"))
    if u:
        obj["usage"] = u
    if finish == "length":
        obj["incomplete_details"] = {"reason": "max_output_tokens"}
    return obj


def stream_responses_events(inner_lines, model, holder):
    """Yield Responses-API SSE frames translated from chat-completions chunks.

    inner_lines: 已解包的 "data: {chunk}" 行迭代器（iter_inner_sse 输出）。
    """
    resp_id, msg_id, rs_id = _new_id("resp_"), _new_id("msg_"), _new_id("rs_")
    created = int(time.time())
    # 上游重开后本生成器会被重建：若 seq 从 0 重来，客户端看到的
    # sequence_number 会回退。从 holder 恢复历史最大号，保证严格单调。
    seq = int(holder.get("seq") or 0) if isinstance(holder, dict) else 0
    if isinstance(holder, dict):
        holder["resp_id"] = resp_id
    text_parts, reason_parts = [], []
    outputs = []
    reason_index = None
    msg_index = None
    finish = "stop"
    usage = None
    tool_calls_map = {}
    text_buffer = ""
    dsml_tool_calls = []
    custom_names = set(holder.get("custom_names") or ())

    def resp_obj(status):
        obj = {
            "id": resp_id,
            "object": "response",
            "created_at": created,
            "status": status,
            "model": model,
            "output": [o for o in outputs if o],
            "output_text": "".join(text_parts),
            "parallel_tool_calls": True,
            "tool_choice": "auto",
            "tools": [],
            "metadata": {},
        }
        u = _responses_usage(usage)
        if u:
            obj["usage"] = u
        return obj

    def ev(etype, payload_obj):
        nonlocal seq
        seq += 1
        if isinstance(holder, dict):
            holder["seq"] = seq        # 供外层补发终态事件时续号
        data = {"type": etype, "sequence_number": seq}
        data.update(payload_obj)
        body = json.dumps(data, ensure_ascii=False)
        return ("event: " + etype + "\ndata: " + body + "\n\n").encode("utf-8")

    def reason_item(status):
        return {
            "id": rs_id,
            "type": "reasoning",
            "status": status,
            "summary": [{"type": "summary_text", "text": "".join(reason_parts)}],
        }

    def msg_item(status):
        item = {"id": msg_id, "type": "message", "status": status,
                "role": "assistant", "content": []}
        if text_parts:
            item["content"] = [{"type": "output_text",
                                "text": "".join(text_parts),
                                "annotations": []}]
        return item

    yield ev("response.created", {"response": resp_obj("in_progress")})
    yield ev("response.in_progress", {"response": resp_obj("in_progress")})
    for raw in inner_lines:
        data = strip_data_prefix(raw.decode("utf-8", "replace")
                                 if isinstance(raw, bytes) else raw)
        if not data or data == "[DONE]":
            continue
        try:
            chunk = json.loads(data)
        except Exception:
            continue
        if usage is None and chunk.get("usage"):
            usage = chunk["usage"]
            holder["usage"] = usage
        for choice in chunk.get("choices") or []:
            delta = choice.get("delta") or {}
            piece = delta.get("reasoning_content")
            if piece:
                if reason_index is None:
                    reason_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added",
                             {"output_index": reason_index,
                              "item": reason_item("in_progress")})
                    yield ev("response.reasoning_summary_part.added", {
                        "item_id": rs_id, "output_index": reason_index,
                        "summary_index": 0,
                        "part": {"type": "summary_text", "text": ""},
                    })
                reason_parts.append(piece)
                yield ev("response.reasoning_summary_text.delta", {
                    "item_id": rs_id, "output_index": reason_index,
                    "summary_index": 0, "delta": piece,
                })
            for tc in delta.get("tool_calls") or []:
                idx = tc.get("index", 0)
                fn = tc.get("function") or {}
                fn_name = fn.get("name") or ""
                fn_args = fn.get("arguments") or ""
                call_id = tc.get("id") or ""
                if idx not in tool_calls_map:
                    out_idx = len(outputs)
                    outputs.append(None)
                    c_id = call_id or _new_id("call_")
                    is_custom = bool(fn_name) and fn_name in custom_names
                    entry = {
                        "output_index": out_idx,
                        "id": c_id,
                        "name": fn_name,
                        "arguments": fn_args,
                        "custom": is_custom,
                        "item_id": _new_id("ctc_" if is_custom else "fc_"),
                    }
                    tool_calls_map[idx] = entry
                    item = {"id": entry["item_id"], "status": "in_progress",
                            "call_id": c_id, "name": fn_name}
                    if is_custom:
                        item["type"] = "custom_tool_call"
                        item["input"] = ""
                    else:
                        item["type"] = "function_call"
                        item["arguments"] = ""
                    yield ev("response.output_item.added",
                             {"output_index": out_idx, "item": item})
                else:
                    entry = tool_calls_map[idx]
                    if fn_name and not entry["name"]:
                        entry["name"] = fn_name
                        if fn_name in custom_names:
                            entry["custom"] = True
                    if fn_args:
                        entry["arguments"] += fn_args
                        if entry.get("custom"):
                            yield ev("response.custom_tool_call_input.delta", {
                                "output_index": entry["output_index"],
                                "item_id": entry["item_id"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
                        else:
                            yield ev("response.function_call_arguments.delta", {
                                "output_index": entry["output_index"],
                                "call_id": entry["id"],
                                "delta": fn_args,
                            })
            piece = delta.get("content")
            if piece:
                if msg_index is None:
                    if reason_index is not None:
                        full_r = "".join(reason_parts)
                        yield ev("response.reasoning_summary_text.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0, "text": full_r,
                        })
                        yield ev("response.reasoning_summary_part.done", {
                            "item_id": rs_id, "output_index": reason_index,
                            "summary_index": 0,
                            "part": {"type": "summary_text", "text": full_r},
                        })
                        outputs[reason_index] = reason_item("completed")
                        yield ev("response.output_item.done",
                                 {"output_index": reason_index,
                                  "item": outputs[reason_index]})
                    msg_index = len(outputs)
                    outputs.append(None)
                    yield ev("response.output_item.added", {
                        "output_index": msg_index,
                        "item": {"id": msg_id, "type": "message",
                                 "status": "in_progress", "role": "assistant",
                                 "content": []},
                    })
                    yield ev("response.content_part.added", {
                        "item_id": msg_id, "output_index": msg_index,
                        "content_index": 0,
                        "part": {"type": "output_text", "text": "",
                                 "annotations": []},
                    })
                # DSML 缓冲：不把原始 DSML 标签流给客户端
                text_buffer += piece
                # 泄漏回读（issue #8）：正文还一个字没吐、缓冲仍是
                # 「marker + JSON 数组」候选时先压住，流末统一判定。
                _leak_defer = not any(text_parts) and leak_prefix_hold(
                    text_buffer)
                while text_buffer and not _leak_defer:
                    idx = text_buffer.find("<")
                    if idx == -1:
                        text_parts.append(text_buffer)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": text_buffer,
                        })
                        text_buffer = ""
                        break
                    m = DSML_CALLS_RE.search(text_buffer)
                    if m and m.start() == idx:
                        if idx > 0:
                            lead = text_buffer[:idx]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                        calls_found, _ = parse_dsml_tool_calls(m.group(0))
                        if calls_found:
                            dsml_tool_calls.extend(calls_found)
                        text_buffer = text_buffer[m.end():]
                        continue
                    cand = text_buffer[idx:idx + 30]
                    is_cand = ("DSML" in cand) or (
                        len(cand) < 10 and not any(c in cand
                                                   for c in (" ", "\t", "\n", ">")))
                    if is_cand:
                        lead = text_buffer[:idx]
                        text_parts.append(lead)
                        yield ev("response.output_text.delta", {
                            "item_id": msg_id, "output_index": msg_index,
                            "content_index": 0, "delta": lead,
                        })
                        text_buffer = text_buffer[idx:]
                        break
                    else:
                        next_lt = text_buffer[idx + 1:].find("<")
                        if next_lt != -1:
                            flush_len = idx + 1 + next_lt
                            lead = text_buffer[:flush_len]
                            text_parts.append(lead)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": lead,
                            })
                            text_buffer = text_buffer[flush_len:]
                        else:
                            text_parts.append(text_buffer)
                            yield ev("response.output_text.delta", {
                                "item_id": msg_id, "output_index": msg_index,
                                "content_index": 0, "delta": text_buffer,
                            })
                            text_buffer = ""
                            break
            if choice.get("finish_reason"):
                finish = choice["finish_reason"]
    if reason_index is not None and outputs[reason_index] is None:
        full_r = "".join(reason_parts)
        yield ev("response.reasoning_summary_text.done", {
            "item_id": rs_id, "output_index": reason_index,
            "summary_index": 0, "text": full_r,
        })
        yield ev("response.reasoning_summary_part.done", {
            "item_id": rs_id, "output_index": reason_index,
            "summary_index": 0,
            "part": {"type": "summary_text", "text": full_r},
        })
        outputs[reason_index] = reason_item("completed")
        yield ev("response.output_item.done",
                 {"output_index": reason_index, "item": outputs[reason_index]})
    # 1. 已完成的结构化工具调用
    for idx in sorted(tool_calls_map.keys()):
        entry = tool_calls_map[idx]
        if entry.get("custom"):
            yield ev("response.custom_tool_call_input.done", {
                "output_index": entry["output_index"],
                "item_id": entry["item_id"],
                "call_id": entry["id"],
                "input": _unwrap_custom_input(entry["arguments"]),
            })
            fc_item = {
                "id": entry["item_id"],
                "type": "custom_tool_call",
                "status": "completed",
                "call_id": entry["id"],
                "name": entry["name"],
                "input": _unwrap_custom_input(entry["arguments"]),
            }
        else:
            yield ev("response.function_call_arguments.done", {
                "output_index": entry["output_index"],
                "call_id": entry["id"],
                "arguments": entry["arguments"],
            })
            fc_item = {
                "id": entry["item_id"],
                "type": "function_call",
                "status": "completed",
                "call_id": entry["id"],
                "name": entry["name"],
                "arguments": entry["arguments"],
            }
        outputs[entry["output_index"]] = fc_item
        yield ev("response.output_item.done",
                 {"output_index": entry["output_index"], "item": fc_item})
    # 冲刷剩余缓冲文本
    if text_buffer:
        leak_calls, leak_clean = (None, text_buffer)
        dropped_leak = False
        if not any(text_parts):
            leak_calls, leak_clean = parse_leaked_tool_calls(
                text_buffer, holder.get("allowed_names"))
            if leak_calls is None and leaked_partial_droppable(
                    text_buffer, holder.get("allowed_names")):
                dropped_leak = True
        if leak_calls:
            dsml_tool_calls.extend(
                {"id": c.get("id"),
                 "name": (c.get("function") or {}).get("name") or "",
                 "arguments": (c.get("function") or {}).get("arguments") or "{}"}
                for c in leak_calls)
            calls_rem, clean_rem = None, leak_clean
        elif dropped_leak:
            # issue #9：截断的 marker+JSON 回声 -> 不输出文本、不走 DSML 回退。
            log("dropped truncated leaked tool-call echo (%d chars) — issue #9"
                % len(text_buffer), level="WARN", tag="chat")
            calls_rem, clean_rem = None, None
        else:
            calls_rem, clean_rem = parse_dsml_tool_calls(text_buffer)
        if calls_rem:
            dsml_tool_calls.extend(calls_rem)
        if clean_rem:
            text_parts.append(clean_rem)
            if msg_index is not None:
                yield ev("response.output_text.delta", {
                    "item_id": msg_id, "output_index": msg_index,
                    "content_index": 0, "delta": clean_rem,
                })
        text_buffer = ""
    # 2. DSML 回退：无结构化工具调用时用解析出的 DSML 调用
    full_text = "".join(text_parts)
    dsml_calls = dsml_tool_calls
    if not dsml_calls:
        extra_calls, clean_text = parse_dsml_tool_calls(full_text)
        if extra_calls:
            dsml_calls = extra_calls
            full_text = clean_text
    if dsml_calls and not tool_calls_map:
        for dc in dsml_calls:
            out_idx = len(outputs)
            fc_item = {
                "id": _new_id("fc_"),
                "type": "function_call",
                "status": "completed",
                "call_id": dc.get("id") or _new_id("call_"),
                "name": dc.get("name") or "",
                "arguments": dc.get("arguments") or "{}",
            }
            outputs.append(fc_item)
            yield ev("response.output_item.added", {
                "output_index": out_idx,
                "item": dict(fc_item, status="in_progress", arguments=""),
            })
            yield ev("response.function_call_arguments.delta", {
                "output_index": out_idx,
                "call_id": fc_item["call_id"],
                "delta": fc_item["arguments"],
            })
            yield ev("response.function_call_arguments.done", {
                "output_index": out_idx,
                "call_id": fc_item["call_id"],
                "arguments": fc_item["arguments"],
            })
            yield ev("response.output_item.done",
                     {"output_index": out_idx, "item": fc_item})
    # 3. 有文本或没有任何输出项时，补 message 项
    has_other_items = any(o for o in outputs if o)
    if msg_index is not None or full_text or not has_other_items:
        if msg_index is None:
            msg_index = len(outputs)
            outputs.append(None)
            yield ev("response.output_item.added", {
                "output_index": msg_index,
                "item": {"id": msg_id, "type": "message",
                         "status": "in_progress", "role": "assistant",
                         "content": []},
            })
            yield ev("response.content_part.added", {
                "item_id": msg_id, "output_index": msg_index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "",
                         "annotations": []},
            })
        yield ev("response.output_text.done", {
            "item_id": msg_id, "output_index": msg_index,
            "content_index": 0, "text": full_text,
        })
        yield ev("response.content_part.done", {
            "item_id": msg_id, "output_index": msg_index,
            "content_index": 0,
            "part": {"type": "output_text", "text": full_text,
                     "annotations": []},
        })
        outputs[msg_index] = msg_item("completed")
        yield ev("response.output_item.done",
                 {"output_index": msg_index, "item": outputs[msg_index]})
    status = "completed" if finish != "length" else "incomplete"
    final = resp_obj(status)
    if finish == "length":
        final["incomplete_details"] = {"reason": "max_output_tokens"}
    yield ev("response.completed", {"response": final})


def responses_failed_frame(holder, code, message):
    """Responses 流的终态失败事件（response.failed）。

    stream_responses_events 的 ev() 闭包把最新 sequence_number 同步进
    holder["seq"]，这里在其上 +1 续号，保证严格大于已发出的事件——
    Responses 客户端只认终态事件，缺了它会一直等（原实现只 _sse_end）。
    """
    seq = int((holder or {}).get("seq") or 0) + 1
    data = {
        "type": "response.failed",
        "sequence_number": seq,
        "response": {
            "id": (holder or {}).get("resp_id") or _new_id("resp_"),
            "object": "response",
            "status": "failed",
            "error": {"code": str(code), "message": message},
        },
    }
    return ("event: response.failed\ndata: "
            + json.dumps(data, ensure_ascii=False) + "\n\n").encode("utf-8")
