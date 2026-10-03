"""qoder2api.model_entry —— 由 qoder_proxy.py 拆分而来。"""

import time

from . import catalog as qoder_catalog
from . import accounts as qoder_accounts
from . import settings as qoder_settings
from . import models
from . import reasoning
from . import update
from . import runtime
import re

from .reasoning import window_choices




def _parse_hhmm(value):
    """'22:00' -> 1320 分钟数；解析失败返回 None。"""
    try:
        parts = str(value).split(":")
        return int(parts[0]) * 60 + int(parts[1])
    except Exception:
        return None


def off_peak_active_now(window_start, window_end, tz=None, now=None):
    """判断当前时刻是否处于官方低谷（Off-Peak）时段。

    官方 promotion.window 为 22:00-08:00（跨午夜），文案标注 UTC+8；
    官方时区 Asia/Shanghai / Asia/Singapore 均为 UTC+8，因此按固定 +8
    计算（无 tzdata 依赖），未知时区也按官方标注的 UTC+8 兜底。
    返回 True（窗口内）/ False（窗口外）/ None（无有效窗口）。
    """
    s = _parse_hhmm(window_start)
    e = _parse_hhmm(window_end)
    if s is None or e is None:
        return None
    if now is None:
        now = time.time()
    import datetime
    offset_hours = 8
    if isinstance(tz, str):
        if tz.upper().startswith("UTC"):
            m = re.search(r"UTC([+-]\d{1,2})", tz.upper())
            if m:
                offset_hours = int(m.group(1))
        # Asia/Shanghai / Asia/Singapore -> +8（默认即 8）
    utc = datetime.datetime.fromtimestamp(now, datetime.timezone.utc)
    local = utc + datetime.timedelta(hours=offset_hours)
    cur = local.hour * 60 + local.minute
    if s == e:
        return True
    if s < e:
        return s <= cur < e
    return cur >= s or cur < e      # 跨午夜窗口（22:00-08:00）


def model_entry(mid, meta):
    """Build a rich /v1/models entry.

    - `id` = **官方模型名 display_name**（如 `Qwen3.8-Max`）——客户端唯一需要
      填的值，与官方桌面版选择器显示一致；`upstream_key` 保留缩写 key，
      `aliases` 列出所有可接受形式（key / 「key (Name)」/ 人类别名）。
    - `description` = 官方桌面版介绍文案（dynamic-text zh.detail）。
    - `name_local` = 官方本地化名（zh.label 与 display_name 不同时，如
      Ultimate -> 极致、旧文案 Kimi-K2.7-Code）。
    - 上下文窗口来自官方 context_config；思考档位来自 thinking_config。
    - 峰谷价来自 promotion（peak -> valley + 时段窗口与官方错峰文案）。
    - `enabled=false` 时给出 `disabled_reason`（官方原文：需要升级或购买
      千问官方套餐开放）与上游 `disabled_message_key`。
    - **最大输出**：官方（catalog / 动态接口原始响应）均无此字段，因此
      仅当来源真实携带时才输出，绝不编造。
    """
    meta = meta or {}
    display_name = meta.get("name") or meta.get("display_name") or mid
    item = {
        "id": display_name,
        "object": "model",
        "created": int(time.time()),
        "owned_by": "qoder",
        "upstream_key": mid,
        "enabled": meta.get("enable", True) is not False,
    }
    # 双区并集清单（union_model_entries）会标注该模型可用的出口：
    # 单区条目 `realm`，两区共享条目 `realm="both"` + `realms` 列表。
    if meta.get("realms"):
        item["realms"] = list(meta["realms"])
    if meta.get("realm"):
        item["realm"] = meta["realm"]
    item["name"] = display_name
    # 描述：来源自带动态/本地 description，否则取官方桌面版文案
    desc = meta.get("description") or ""
    if not desc:
        try:
            desc = qoder_catalog.official_description(mid)
        except Exception:
            desc = ""
    if desc:
        item["description"] = desc
    # 官方本地化显示名（与 display_name 不同时透出）
    try:
        local = qoder_catalog.official_local_name(mid)
    except Exception:
        local = ""
    if local:
        item["name_local"] = local
    # 所有可接受的填写形式
    aliases = [mid, "%s (%s)" % (mid, display_name)] if display_name != mid else [mid]
    for ak, av in qoder_catalog.MODEL_ALIASES.items():
        if av == mid and ak not in aliases:
            aliases.append(ak)
    item["aliases"] = aliases
    # 禁用态：官方原因文案（未开通千问套餐时官方桌面版所示）
    if not item["enabled"]:
        item["disabled_reason"] = getattr(
            qoder_catalog, "OFFICIAL_DISABLED_REASON",
            "需要升级或购买千问官方套餐开放")
        strategies = meta.get("strategies")
        if isinstance(strategies, list):
            for st in strategies:
                if isinstance(st, dict) and st.get("disabled_message_key"):
                    item["disabled_message_key"] = st["disabled_message_key"]
                    break
            item["strategies"] = strategies
    vision = bool(meta.get("is_vl") or meta.get("supportsImages")) \
        and not meta.get("disabledMultimodal")
    tools = bool(meta.get("supportsToolCall", True))
    thinking_cfg = meta.get("thinking_config") if isinstance(
        meta.get("thinking_config"), dict) else {}
    thinks = bool(meta.get("is_reasoning") or meta.get("supportsReasoning")
                  or isinstance(thinking_cfg.get("enabled"), dict))
    inputs = ["text"] + (["image"] if vision else [])
    item["capabilities"] = {"vision": vision, "tool_calls": tools,
                            "reasoning": thinks}
    item["supports_vision"] = vision
    item["supports_images"] = vision
    item["supports_tool_calls"] = tools
    item["supports_reasoning"] = thinks
    item["vision"] = vision
    item["multimodal"] = vision
    item["abilities"] = {"vision": vision, "functionCall": tools,
                         "function_call": tools, "reasoning": thinks}
    item["input_modalities"] = inputs
    item["output_modalities"] = ["text"]
    item["modalities"] = {"input": inputs, "output": ["text"]}
    item["architecture"] = {
        "input_modalities": inputs,
        "output_modalities": ["text"],
        "modality": "+".join(inputs) + "->text",
    }
    # ---- limits：官方 context_config 多窗口 + 默认输入上限 ----
    max_in = meta.get("maxInputTokens") or meta.get("max_input_tokens")
    ctx_cfg = meta.get("context_config") if isinstance(
        meta.get("context_config"), dict) else {}
    windows, labels, default_label = [], [], ""
    for label, conf in ctx_cfg.items():
        if not isinstance(conf, dict):
            continue
        tok = conf.get("token_count")
        if tok:
            labels.append(str(label))
            windows.append(tok)
            if conf.get("is_default"):
                default_label = str(label)
    if not default_label and labels:
        default_label = labels[0]
    if max_in:
        item["context_length"] = max_in
        item["max_input_tokens"] = max_in
    if windows:
        item["context_windows"] = windows
        item["context_window_labels"] = labels
        item["context_window_default"] = default_label
    elif max_in:
        # 官方没给多窗口表时，按官方 WX() 推导可选项（128K/200K/上限）：
        # 此时上游只校验「不超过上限」，看板照样能选。
        derived = window_choices(meta)
        if derived:
            def _win_label(v):
                # 与官方版本一致用 1000 进制（200000 -> "200K"，不是 "195K"）
                if v >= 1000000:
                    return ("%g" % (v / 1000000)) + "M"
                if v >= 1000:
                    return ("%g" % (v / 1000)) + "K"
                return str(v)
            item["context_windows"] = derived
            item["context_window_labels"] = [_win_label(v) for v in derived]
            item["context_window_default"] = _win_label(derived[-1])
    # 最大输出：官方（catalog/动态接口）均无此字段——不输出、不展示（用户
    # 已确认不再测量该值）。
    max_out = meta.get("maxOutputTokens") or meta.get("max_output_tokens")
    if max_out:
        item["max_output_tokens"] = max_out
        item["max_completion_tokens"] = max_out
    # ---- 思考档位：官方 thinking_config ----
    enabled_think = thinking_cfg.get("enabled")
    if isinstance(enabled_think, dict):
        order = ["low", "medium", "high", "xhigh", "max"]
        eff_map = enabled_think.get("efforts") or {}
        efforts = [e for e in order if e in eff_map]
        if efforts:
            item["reasoning_efforts"] = efforts
        for e in efforts:
            if isinstance(eff_map.get(e), dict) and eff_map[e].get("is_default"):
                item["reasoning_default_effort"] = e
                break
    if "disabled" in thinking_cfg:
        item["reasoning_can_disable"] = True
    # ---- 兼容旧 catalog 形态 ----
    effort_fixed = (meta.get("reasoning") or {}).get("effort")
    if effort_fixed:
        item["reasoning_fixed_effort"] = effort_fixed
    efforts_old = (meta.get("reasoning") or {}).get("supportedEfforts")
    if efforts_old and "reasoning_efforts" not in item:
        item["reasoning_efforts"] = efforts_old
    if (meta.get("reasoning") or {}).get("defaultEffort") \
            and "reasoning_default_effort" not in item:
        item["reasoning_default_effort"] = meta["reasoning"]["defaultEffort"]
    # ---- 计费：峰谷价（官方 promotion） ----
    price_now = meta.get("price_factor")
    promo = meta.get("promotion") if isinstance(meta.get("promotion"), dict) else {}
    if price_now is not None:
        item["price_factor"] = price_now
    peak = None
    if promo.get("before_promotion_price_factor") is not None:
        peak = promo.get("before_promotion_price_factor")
    elif meta.get("original_price_factor") is not None:
        peak = meta.get("original_price_factor")
    if peak is not None:
        item["price_factor_peak"] = peak
    # 低谷价 = 峰值 × 折扣（官方 promotion 的定义，与快照抓取时刻无关）。
    # 旧实现直接把"当前 price_factor"当成低谷价：快照若在非低谷时段抓取，
    # 报出的低谷价就会等于峰价（看板/客户端据此算错费用）。
    discount = promo.get("discount_factor")
    valley = None
    try:
        if peak is not None and discount and 0 < float(discount) <= 1:
            valley = round(float(peak) * float(discount), 4)
    except (TypeError, ValueError):
        valley = None
    if valley is None and price_now is not None:
        valley = price_now
    if valley is not None:
        item["price_factor_valley"] = valley
    # 只要官方给了 promotion 就下发峰谷元数据（不限于"此刻正在打折"）：
    # 高峰期抓取的快照里 promo.active=False，旧实现会整块跳过，客户端于是
    # 既看不到低谷窗口、也看不到峰价。active 如实透传官方值，"现在是否处于
    # 低谷"另由 off_peak_active_now（按本地时间复算）给出。
    if promo:
        badge = promo.get("badge") or {}
        desc_p = promo.get("description") or {}
        link = promo.get("link_url") or {}
        item["off_peak"] = {
            "active": bool(promo.get("active")),
            "window_start": promo.get("window_start"),
            "window_end": promo.get("window_end"),
            "timezone": promo.get("timezone"),
            "discount_factor": promo.get("discount_factor"),
            "badge": badge.get("zh") or badge.get("en") or "",
            "badge_en": badge.get("en") or "",
            "description": desc_p.get("zh") or desc_p.get("en") or "",
            "description_en": desc_p.get("en") or "",
            "link": link.get("zh") or link.get("en") or "",
        }
        item["off_peak_window"] = "%s-%s" % (promo.get("window_start"),
                                             promo.get("window_end"))
        item["promotion"] = promo
        # 当前是否正处于低谷时段（供看板做视觉高亮；前端亦会按本地时间复算）
        active = off_peak_active_now(promo.get("window_start"),
                                     promo.get("window_end"),
                                     tz=promo.get("timezone"))
        item["off_peak_active_now"] = bool(active)
    if meta.get("original_price_factor") is not None:
        item["original_price_factor"] = meta.get("original_price_factor")
    if meta.get("is_free") is not None:
        item["is_free"] = bool(meta.get("is_free"))
    if meta.get("is_new"):
        item["is_new"] = True
    if meta.get("icon"):
        item["icon"] = meta.get("icon")
    if meta.get("credits"):
        item["credits"] = meta["credits"]
    return item
