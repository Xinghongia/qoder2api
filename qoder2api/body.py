"""qoder2api.body —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import re

from . import paths
from . import catalog as qoder_catalog
from . import settings as qoder_settings
from . import sanitize
import time
import uuid

from . import sign as qoder_sign
from . import runtime, paths
from .logbus import log
from .reasoning import (available_context_windows, client_thinking,
                        effort_from_budget, normalize_reasoning_effort,
                        resolve_context_window, supported_efforts)
from .chat_normalize import (is_deepseek_model,
                              backfill_reasoning_content, LEAK_MARKER)
from .sanitize import (sanitize_text, sanitize_messages,
                       normalize_roles)




CHAT_PATH = ("/algo/api/v2/service/pro/sse/agent_chat_generation"
             "?FetchKeys=llm_model_result&AgentId=agent_common&Encode=1")
MODELS_PATH = "/algo/api/v2/model/list?Encode=1"

# 官方 baseprompt 模板（与桌面端一致的请求体骨架）
BASEPROMPT_PATH = str(paths.baseprompt_path())
try:
    with open(BASEPROMPT_PATH, encoding="utf-8") as _bp_fh:
        BASEPROMPT = json.load(_bp_fh)
except Exception:
    BASEPROMPT = {}


# ---------------------------------------------------------------------------
# Qoder 数据面：请求体构造
# ---------------------------------------------------------------------------
def _flatten_content_text(content):
    """把 OpenAI content（str 或 parts 列表）压成纯文本；图片另行收集。"""
    if content is None:
        return "", []
    if isinstance(content, str):
        return content, []
    if not isinstance(content, list):
        return str(content), []
    texts, images = [], []
    for piece in content:
        if isinstance(piece, str):
            texts.append(piece)
            continue
        if not isinstance(piece, dict):
            continue
        ptype = piece.get("type") or ""
        if ptype in ("text", "input_text", "output_text", "summary_text"):
            texts.append(piece.get("text") or "")
        elif ptype in ("image_url", "image", "input_image") or "image_url" in piece:
            url = piece.get("image_url") or piece.get("url")
            if isinstance(url, dict):
                url = url.get("url")
            if not url and piece.get("data"):
                mime = piece.get("mimeType") or piece.get("mime_type") or "image/png"
                url = "data:%s;base64,%s" % (mime, piece["data"])
            if url:
                images.append(url)
    return "\n".join(t for t in texts if t), images


def flatten_messages(messages, keep_reasoning=False):
    """把客户端会话压平成 Qoder 上游可接受的 {role, content} 序列。

    返回 (system_text, flat_msgs, images)：
      - 首个 system/developer 消息抽为 system_text（模板 system 的替换源）
      - user/assistant 原位保留（content 压平为字符串）
      - tool 结果降级为 user 消息（上游不认识 tool 角色）
      - assistant 的 tool_calls 序列化进 content，保证上下文不丢
      - assistant 的 reasoning_content 仅在 keep_reasoning=True（DeepSeek 族）
        时保留：这族模型的多轮一致性与该字段绑定，其它模型不带（避免上游
        因未知字段拒答）。此前 flatten 无条件丢弃该字段，使
        backfill_reasoning_content 的兼容处理在真实请求路径上完全失效。
    """
    system_text = None
    flat, images = [], []
    for m in messages or []:
        if not isinstance(m, dict):
            continue
        role = m.get("role")
        text, imgs = _flatten_content_text(m.get("content"))
        images.extend(imgs)
        text = sanitize_text(text) if isinstance(text, str) else text
        if role in ("system", "developer"):
            if system_text is None and text:
                system_text = text
            continue
        if role == "tool":
            name = ""
            tc = m.get("name") or ""
            if tc:
                name = " (%s)" % tc
            flat.append({"role": "user",
                         "content": "[工具结果%s]\n%s" % (name, text or "")})
            continue
        if role == "assistant" and m.get("tool_calls"):
            calls = []
            for tc in m["tool_calls"]:
                fn = (tc or {}).get("function") or {}
                calls.append({"name": fn.get("name") or "",
                              "arguments": fn.get("arguments") or ""})
            if calls:
                text = (text or "") + "\n\n" + LEAK_MARKER + "\n" + \
                    json.dumps(calls, ensure_ascii=False)
            item = {"role": "assistant", "content": text or ""}
            if keep_reasoning and m.get("reasoning_content") is not None:
                item["reasoning_content"] = m.get("reasoning_content") or ""
            flat.append(item)
            continue
        if role in ("user", "assistant"):
            item = {"role": role, "content": text or ""}
            if role == "assistant" and keep_reasoning \
                    and m.get("reasoning_content") is not None:
                item["reasoning_content"] = m.get("reasoning_content") or ""
            flat.append(item)
        elif role:  # 未知角色一律降级为 user，不静默丢弃
            flat.append({"role": "user", "content": text or ""})
    return system_text, flat, images


def build_qoder_body(payload, account, model_key, realm=None):
    """构造 agent_chat_generation 上游请求体（返回 dict，随后 qoder_encode）。

    以官方 baseprompt.json 为骨架：每次覆写 request/session id、时间戳、
    模型配置（按出口区域的官方清单取元数据）、系统提示词与会话、工具、
    参数与 business 会话名。
    """
    r = realm or (account.realm if account else runtime.CURRENT_REALM)
    body = json.loads(json.dumps(BASEPROMPT))   # deep copy
    messages = normalize_roles(payload.get("messages") or [])
    messages = sanitize_messages(messages)
    model = payload.get("model") or ""
    messages = backfill_reasoning_content(messages, model, model_key)

    system_text, flat, images = flatten_messages(
        messages, keep_reasoning=is_deepseek_model(model, model_key))
    # 模板只保留 system 基座（其自带的示例 user 轮次是样例内容，必须丢弃），
    # 真实会话随后追加。
    tmpl_system = [m for m in (body.get("messages") or [])
                   if (m or {}).get("role") == "system"]
    if system_text is None:
        # 客户端没给 system：优先用 --system-prompt 覆盖值（非默认时），
        # 否则保留模板自带的 Qoder 系统提示词。
        if runtime.SYSTEM_PROMPT and runtime.SYSTEM_PROMPT != runtime.DEFAULT_SYSTEM_PROMPT:
            system_text = runtime.SYSTEM_PROMPT
        else:
            body["messages"] = tmpl_system
    if system_text is not None:
        body["messages"] = [{"role": "system", "content": system_text}]
    # 追加真实会话
    body["messages"].extend(flat)

    # 最新用户提示词（上游 chat_context 高亮 + business 会话名）
    prompt = ""
    for m in reversed(flat):
        if m["role"] == "user" and m.get("content"):
            prompt = m["content"]
            break

    nid = str(uuid.uuid4())
    body["request_id"] = nid
    body["chat_record_id"] = nid
    body["request_set_id"] = str(uuid.uuid4())
    body["session_id"] = str(uuid.uuid4())
    body["stream"] = True
    body["agent_id"] = "agent_common"
    body["aliyun_user_type"] = getattr(account, "user_type", "") or \
        qoder_sign.DEFAULT_USER_TYPE

    # model_config（按出口区域的官方清单取元数据 + 动态 key）
    catalog_meta = {}
    for m in qoder_catalog.models_for_realm(r):
        if m.get("key") == model_key:
            catalog_meta = m
            break
    if not catalog_meta:
        for m in qoder_catalog.STATIC_INTL_MODELS + qoder_catalog.STATIC_CN_MODELS:
            if m.get("key") == model_key:
                catalog_meta = m
                break
    mc = body.get("model_config") or {}
    mc["key"] = model_key
    mc["display_name"] = catalog_meta.get("display_name") or model_key
    mc["model"] = ""
    mc["format"] = "openai"
    mc["is_vl"] = bool(catalog_meta.get("is_vl"))
    mc["is_reasoning"] = bool(catalog_meta.get("is_reasoning"))
    mc["api_key"] = ""
    mc["url"] = ""
    mc["source"] = "system"
    mc["max_input_tokens"] = catalog_meta.get("max_input_tokens") or 180000
    body["model_config"] = mc

    # ---- 思考 + 上下文窗口：客户端请求优先，其次看板里该模型的默认设置 ----
    # 官方语义（CLI 1.1.62）：预算/档位先换算再按模型支持集合归一化；窗口
    # 经官方校验后以 parameters.context_length 下发。
    try:
        model_override = qoder_settings.model_overrides(runtime.ACCOUNTS_DIR) \
            .get("%s:%s" % (r, model_key)) or {}
    except Exception:
        model_override = {}
    think = client_thinking(payload)
    effort = think["effort"]
    think_budget = think["budget"]
    if think["disable"] and not effort:
        effort = "none"
    if not effort and think_budget is not None:
        effort = effort_from_budget(think_budget)
    if not effort and model_override.get("effort"):
        effort = model_override["effort"]
    effort_norm = None
    if effort:
        effort_norm, note = normalize_reasoning_effort(effort, catalog_meta)
        if note:
            log("reasoning_effort %s on '%s' (supported=%s)"
                % (note, model_key or model, supported_efforts(catalog_meta)),
                tag="chat")
    if effort_norm == "none":
        # 官方：思考关闭时同步把 model_config.is_reasoning 置 false
        mc["is_reasoning"] = False
    want_window = payload.get("context_window") \
        or payload.get("context_window_tokens") \
        or payload.get("context_length") \
        or model_override.get("context_window")
    ctx_len, window_note = resolve_context_window(catalog_meta, want_window)
    if window_note:
        log("context_window %s on '%s' (available=%s)"
            % (window_note, model_key or model,
               available_context_windows(catalog_meta)), tag="chat")

    # chat_context 高亮与模型配置副本
    cc = body.get("chat_context") or {}
    txt = cc.get("text") or {}
    txt["text"] = prompt
    cc["text"] = txt
    extra = cc.get("extra") or {}
    oc = extra.get("originalContent") or {}
    oc["text"] = prompt
    extra["originalContent"] = oc
    extra["modelConfig"] = dict(mc)
    cc["extra"] = extra
    if images:
        cc["imageUrls"] = images
        body["image_urls"] = images
    body["chat_context"] = cc

    # parameters：max_tokens / 思考（档位 + 开关 + 预算）/ 上下文窗口
    params = body.get("parameters") or {}
    max_tokens = payload.get("max_tokens") or payload.get("max_completion_tokens")
    if max_tokens:
        try:
            params["max_tokens"] = int(max_tokens)
        except (TypeError, ValueError):
            pass
    if effort_norm:
        params["reasoning_effort"] = effort_norm
        # 与官方 SDK 一致：同步下发开关（none = 关思考，其余 = 开思考）；
        # 客户端给了思考预算时原样透传 reasoning_budget_tokens。
        if effort_norm == "none":
            params["enable_thinking"] = False
        else:
            params["enable_thinking"] = True
            if think_budget and think_budget > 0:
                params["reasoning_budget_tokens"] = int(think_budget)
    if ctx_len:
        params["context_length"] = ctx_len
    body["parameters"] = params

    # tools：客户端给了就用客户端的（custom freeform 已降级），否则置空，
    # 避免把 Qoder 桌面端自带的 agent 工具（Bash/Edit/...）泄漏给普通客户端。
    tools = payload.get("tools")
    if tools:
        body["tools"] = tools
    else:
        body["tools"] = []

    # business 会话卡片
    biz = body.get("business") or {}
    biz["id"] = str(uuid.uuid4())
    biz["begin_at"] = int(time.time() * 1000)
    biz["name"] = (prompt[:30] if prompt else "chat")
    biz.setdefault("product", "cli")
    biz.setdefault("type", "agent")
    biz.setdefault("stage", "start")
    body["business"] = biz
    return body
