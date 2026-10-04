"""qoder2api.tasks —— Qoder 每日签到、额度与福利包自动化引擎

对应 WorkBuddy 网关的 wb_tasks（成长任务中心），Qoder 的“日常任务中心”是：

1. 每日签到 (daily check-in)：查状态 -> 未签则领取（100 积分），
   409 ALREADY_CLAIMED 归一化为“今日已签到”。
2. Pro 升级包 (pro-upgrade)：一次性 +1800 积分，eligibility -> claim。
3. 额度与套餐：quota/usage 聚合、user/plan 套餐名。
4. 为看板合成“任务行”视图（status/current/target/reward_credit），
   让签到中心复用与成长任务一致的表格渲染。
5. 严格遵守 >= 1.0s 防风控间隔，并使用 qoder_fingerprint 的稳定设备指纹。
"""
import sys
import threading
import time

from . import accounts as qoder_accounts
from .accounts import get_realm_config

_log = lambda msg: None

# 签到防风控：账号之间的最小间隔（秒）。模块文档声明的 ">= 1.0s" 由本常量
# 落地——单账号路径（api/accounts_routes.py）也按它取值并保证账号间隔。
CHECKIN_MIN_GAP = 1.0

# ---------------------------------------------------------------------------
# 面板读路径短缓存
# ---------------------------------------------------------------------------
# 看板每次切视图/切账号都会重建任务视图，而各上游小查询（签到状态 / Pro 资格 /
# 额度 / 套餐）每个约 1–4 秒（TLS/风控身份），即使并行也会拖时间。这些字段变化
# 极慢（额度/套餐按天，活动按天），因此面板路径做 20 秒短缓存；**签到/领取等
# 写操作后由调用方 invalidate_panel_cache() 立即失效**，所以用户点完签到看到的
# 一定是新数据。
PANEL_CACHE_TTL = 20
_panel_cache = {}
_panel_lock = threading.Lock()

# 额度快照的新鲜度：超过这个秒数的快照在 /tasks 批量视图里重新拉取。
# 此前只补"完全缺失"的快照，部署端会出现几个小时前的旧余额（本地领了
# 积分、服务器页面还是旧数，刷新页面也不变）——这正是"两边数字不一样"的根因。
CREDITS_STALE_TTL = 180


def _refresh_credits_safe(account):
    try:
        account.fetch_credits()
    except Exception:
        pass


def _credits_stale(account, now=None, ttl=CREDITS_STALE_TTL):
    credits = getattr(account, "credits", None) or {}
    try:
        updated = float(credits.get("updated_at") or 0)
    except (TypeError, ValueError):
        updated = 0.0
    return (now or time.time()) - updated > ttl


def invalidate_panel_cache(uid=None):
    """清除面板短缓存；uid 为空时全部清除（写操作后调用）。"""
    with _panel_lock:
        if uid:
            for k in [k for k in _panel_cache if k[0] == uid]:
                _panel_cache.pop(k, None)
        else:
            _panel_cache.clear()


def set_logger(fn):
    """把任务诊断路由到调用方的日志器（端点吞错时依然可见）。"""
    global _log
    _log = fn or (lambda msg: None)


# ---------------------------------------------------------------------------
# 单账号：状态聚合
# ---------------------------------------------------------------------------
def fetch_task_view(account):
    """为一个账号合成看板任务视图。

    返回 (tasks, summary)：
      tasks   - [ {task_code, name, description, status, current, target,
                   reward_credit, reward_energy} ]
      summary - {streak_days, energy, travel:{state, ...}, plan, campaigns}

    能力门控是**运行时探测**的（Account.checkin_capability），不再按区域硬编码：
    国际版同样挂着"每日领取 100 Credits"，只是接口位置/承接方式随官方调整。
    """
    tasks = []
    summary = {
        "streak_days": 0,
        "energy": 0,
        "travel": {"state": "unknown"},
        "plan": account.plan or "-",
        "credits": account.credits or {},
        "realm": account.realm,
        "campaigns": {"show": False, "claimable": False, "url": "", "items": []},
    }

    # --- 每日签到：以官方活动平台为准（旧 sash 接口仅在仍然开放时附一行） ---
    # 这几个上游调用彼此独立，**并行**发出（原先串行可达 3–9 秒，是看板切换
    # 视图/账号卡顿的主因）；账号文件写入由 Account._SAVE_LOCK 串行化，并行安全。
    camp, status_pair, pro_pair, credits_res, plan_name = \
        _fetch_upstream_parallel(account)
    if plan_name:
        account.plan = plan_name
    tasks.append(_campaign_task_row(account, camp, summary))
    # 券类/兑换码活动（非 Credits 奖励）单独成行：领取后兑换码要能展示
    tasks.extend(_extra_campaign_rows(account, camp))
    ok, st = status_pair
    if ok:
        summary["streak_days"] = st["streak_days"]
    if ok and st.get("active"):
        # 旧接口仍开放：附上历史签到状态（连续天数/累计天数）
        if st["today_checked_in"]:
            l_status, l_desc = "claimed", "今日已签到，明日再来（连续 %d 天，累计 %d 天）" % (
                st["streak_days"], st["total_claim_days"])
        else:
            l_status, l_desc = "completed", "可领取 %d 积分（连续 %d 天，累计 %d 天）" % (
                st["reward_credits"] or 100, st["streak_days"],
                st["total_claim_days"])
        tasks.append({
            "task_code": "daily_checkin_legacy",
            "name": "每日签到（旧活动批次）",
            "description": l_desc,
            "jump_url": get_realm_config(account.realm)["website"] + "/",
            "status": l_status,
            "current": 1,
            "target": 1,
            "reward_credit": st["reward_credits"] or 100,
            "reward_energy": 0,
        })

    # --- Pro 升级包（一次性福利） ---
    ok2, elig = pro_pair
    if ok2:
        if elig:
            p_status, p_cur = "completed", 1     # 待领取
            p_desc = "一次性 Pro 升级包，可领取 +1800 积分"
        else:
            p_status, p_cur = "claimed", 1       # 已领取或活动结束
            p_desc = "已领取或当前不可领取"
        tasks.append({
            "task_code": "pro_upgrade",
            "name": "Pro 升级包",
            "description": p_desc,
            "status": p_status,
            "current": p_cur,
            "target": 1,
            "reward_credit": 1800,
            "reward_energy": 0,
        })
    else:
        tasks.append({
            "task_code": "pro_upgrade",
            "name": "Pro 升级包",
            "description": "eligibility 查询失败: %s" % elig,
            "status": "not_accepted",
            "current": 0,
            "target": 1,
            "reward_credit": 1800,
            "reward_energy": 0,
        })

    # --- 额度卡片（energy -> 积分余额；travel -> 福利包状态） ---
    if account.credits:
        summary["energy"] = account.credits.get("remain", 0)
    elif credits_res.get("ok"):
        summary["energy"] = (account.credits or {}).get("remain", 0)
    if ok2 and elig:
        summary["travel"] = {"state": "arrived", "reward_credit": 1800}
    else:
        summary["travel"] = {"state": "idle", "daily_limit_reached": True}

    summary["plan"] = account.plan or summary["plan"]
    # 本账号已领取的兑换码（券类活动）：按账号分别保存，看板/接口可直接展示
    _by_id31 = {c["campaign_id"]: c for c in (camp.get("campaigns") or [])}
    summary["codes"] = [
        {"campaign": campaign_title(_by_id31.get(key)) or key, "code": code,
         "url": camp.get("campaign_url") or ""}
        for key, code in (getattr(account, "campaign_codes", {}) or {}).items()
        if code
    ]
    return tasks, summary


def _fetch_upstream_parallel(account, force=False):
    """并行取回任务视图所需的 5 组上游数据（互不依赖）。

    返回 (campaigns, (ok, status), (ok, elig), credits_res, plan_name)。
    单个调用失败不影响其余（各自在内部吞错并返回错误结构），因此并行是安全的；
    账号文件保存已由 Account._SAVE_LOCK 串行化。

    QD_TASKS_DEBUG=1 时把每个子调用的耗时打到 stderr（排查看板卡顿用）。
    """
    import os
    import time as _t
    from concurrent.futures import ThreadPoolExecutor

    def timed(name, fn):
        key = (account.uid, name)
        now = _t.time()
        if not force:
            with _panel_lock:
                hit = _panel_cache.get(key)
            if hit and now - hit[0] < PANEL_CACHE_TTL:
                if os.environ.get("QD_TASKS_DEBUG"):
                    try:
                        sys.stderr.write("[tasks-debug] %-18s cached\n" % name)
                        sys.stderr.flush()
                    except Exception:
                        pass
                return hit[1]
        t0 = now
        try:
            out = fn()
        except Exception:
            raise
        else:
            with _panel_lock:
                now_i = _t.time()
                # 顺手清理过期条目，避免长时间运行后字典无界增长
                for k in [k for k, v in _panel_cache.items()
                          if now_i - v[0] > 300]:
                    _panel_cache.pop(k, None)
                _panel_cache[key] = (now_i, out)
            return out
        finally:
            if os.environ.get("QD_TASKS_DEBUG"):
                try:
                    sys.stderr.write("[tasks-debug] %-18s %.2fs\n"
                                     % (name, _t.time() - t0))
                    sys.stderr.flush()
                except Exception:
                    pass

    with ThreadPoolExecutor(max_workers=5) as ex:
        f_camp = ex.submit(timed, "campaigns", account.campaigns)
        f_status = ex.submit(timed, "checkin_status", account.checkin_status)
        f_pro = ex.submit(timed, "pro_eligibility", account.pro_eligibility)
        f_credits = ex.submit(timed, "fetch_credits", account.fetch_credits)
        f_plan = ex.submit(timed, "fetch_plan", account.fetch_plan)

        def _safe(fut, fallback):
            try:
                return fut.result()
            except Exception:
                return fallback

        camp = _safe(f_camp, {"ok": False, "available": True,
                              "error": "parallel fetch failed", "campaigns": []})
        status_pair = _safe(f_status, (False, {"error": "parallel fetch failed"}))
        pro_pair = _safe(f_pro, (False, "parallel fetch failed"))
        credits_res = _safe(f_credits, {"ok": False})
        plan_name = _safe(f_plan, "")
    return camp, status_pair, pro_pair, credits_res, plan_name


def _campaign_task_row(account, camp, summary):
    """把活动平台状态渲染成看板任务行（task_code=daily_checkin）。

    状态语义：
      CLAIMABLE + CLAIM_BENEFIT -> "completed"（待领奖，点「领取全部福利」即自动领取）
      全部 CLAIMED              -> "claimed"  （今日已领取）
      其它/无活动               -> "not_accepted"
    """
    website = get_realm_config(account.realm)["website"]
    jump = (camp.get("campaign_url") or website + "/activities") \
        if camp.get("ok") else website + "/activities"
    items = [{"key": c["campaign_key"] or c["campaign_id"],
              "title": campaign_title(c),
              "desc": campaign_desc(c),
              "detail_url": campaign_link(c),
              "id": c["campaign_id"],
              "action_type": c.get("action_type", ""),
              "claim_status": c.get("claim_status", ""),
              "benefit_kind": str((c.get("benefit") or {}).get("kind") or "").upper(),
              "benefit_amount": (c.get("benefit") or {}).get("amount", 0),
              "required_achievement_key": c.get("required_achievement_key", ""),
              "start_at": c["start_at"],
              "end_at": c["end_at"]} for c in camp.get("campaigns") or []]
    summary["campaigns"] = {
        "show": bool(camp.get("show_campaign")),
        "claimable": bool(camp.get("claimable")),
        "url": camp.get("campaign_url") or "",
        "items": items,
    }
    if not camp.get("ok"):
        return {
            "task_code": "daily_checkin",
            "name": "每日签到（活动平台）",
            "description": ("活动平台在本区域不可用" if not camp.get("available")
                            else "活动状态查询失败: %s" % (camp.get("error") or "?")),
            "jump_url": jump,
            "status": "not_accepted",
            "current": 0,
            "target": 1,
            "reward_credit": 0,
            "reward_energy": 0,
        }
    # 每日签到行只统计"可领取的 Credits 类"活动：券类由独立行呈现（见
    # _extra_campaign_rows），VIEW_DETAILS（无奖励的详情活动）不算"签到奖励"
    def _is_daily(c):
        return (c["action_type"] in ("", "CLAIM_BENEFIT")
                and c["benefit_kind"] in ("", "CREDITS"))

    daily_items = [c for c in items if _is_daily(c)]
    claimable = [c for c in daily_items if c["claim_status"] == "CLAIMABLE"]
    # 只把"每日领取 Credits"类的已领取计入任务行；券类/详情类不混进「已领取」文案
    claimed = [c for c in daily_items if c["claim_status"] == "CLAIMED"]
    # 成就门控活动（如 CN 新人「奶茶免单卡」需先完成 sites_first_use）：
    # 服务端状态 ACHIEVEMENT_NOT_COMPLETED —— 显示成就要求，不能直接领取
    gated = [c for c in daily_items
             if c["claim_status"] == "ACHIEVEMENT_NOT_COMPLETED"]

    if claimable:
        amount = sum(c["benefit_amount"] or 0 for c in claimable)
        keys = ", ".join(c["title"] for c in claimable)
        return {
            "task_code": "daily_checkin",
            "name": "每日签到（每日领取 Credits）",
            "description": "可领取 %s Credits（%s）—— 点「领取全部福利」或该账号行的「签到」直接领取；%s"
                           % (amount or "-", keys,
                              qoder_accounts.round_note(claimable)),
            "jump_url": jump,
            "status": "completed",
            "current": 1,
            "target": 1,
            "reward_credit": amount,
            "reward_energy": 0,
        }
    if claimed:
        amount = sum(c["benefit_amount"] or 0 for c in claimed)
        keys = ", ".join(c["title"] for c in claimed)
        return {
            "task_code": "daily_checkin",
            "name": "每日签到（每日领取 Credits）",
            "description": "本轮已领取%s（%s）；%s"
                           % ((" +%s Credits" % amount) if amount else "", keys,
                              qoder_accounts.round_note(claimed)),
            "jump_url": jump,
            "status": "claimed",
            "current": 1,
            "target": 1,
            "reward_credit": amount,
            "reward_energy": 0,
        }
    if gated:
        keys = ", ".join(c["title"] for c in gated)
        reqs = ", ".join(c.get("required_achievement_key") or "?"
                         for c in gated)
        return {
            "task_code": "daily_checkin",
            "name": "每日签到（每日领取 Credits）",
            "description": "有活动但需先完成成就：%s（活动 %s）——在官方桌面端"
                           "完成对应任务后可领" % (reqs, keys),
            "jump_url": jump,
            "status": "not_accepted",
            "current": 0,
            "target": 1,
            "reward_credit": sum(c["benefit_amount"] or 0 for c in gated),
            "reward_energy": 0,
        }
    desc = ("当前账号暂无可参与的官方活动（每日 100 为定向下发：常见原因——账号未在"
            "活动定向内、虚拟机环境、试用资格已用尽/冻结；详见 README「活动与新人权益规则」）")
    if camp.get("show_campaign"):
        desc = "活动进行中，当前账号暂无可领取项"
    if camp.get("hint"):
        # 无原生机器身份时后端不再发派生假头（issue #10）：INTL 侧活动列表
        # 可能因此不可见——这是**已知限制**，不要误报成"今天没有活动"。
        # 文案由后端 hint 统一给出（含"不等于今天没有活动"的澄清），这里只补出路。
        desc += "；" + camp["hint"] + "（可在「设置 → 机器身份」固定真身份）"
    return {
        "task_code": "daily_checkin",
        "name": "每日签到（每日领取 Credits）",
        "description": desc,
        "jump_url": jump,
        "status": "not_accepted",
        "current": 0,
        "target": 1,
        "reward_credit": 0,
        "reward_energy": 0,
    }


def _acct_name(a):
    return (a.nickname or a.uid[:8]).strip()


def _kind_label(kind):
    """非 Credits 奖励的形态名（兑换码/兑换券/优惠券）。"""
    return {"REDEMPTION_CODE": "兑换码", "REDEMPTION_COUPON": "兑换券",
            "COUPON": "优惠券"}.get(str(kind or "").upper(), str(kind or "").upper() or "奖励")


# 活动中文名兜底（服务端 placements 没带 content.zh 时用；前缀匹配）
CAMPAIGN_NAME_FALLBACK = {
    "act-20260928-620": "新人任务：发布 Qoder 站点领奶茶免单卡",
    "act-20260928": "新人任务：发布 Qoder 站点领奶茶免单卡",
    "act-20260930": "每天领 100 Credits",
    "act-20260923": "每天领 100 Credits",
    "act-20260901-922": "限时福利：专业版/高级版首月 Credits 翻倍",
    "act-20260901": "全新 Qoder 上线福利",
}


def campaign_title(c):
    """活动中文名：官方 content.zh.title 优先，其次内置兜底，最后回退 key。"""
    c = c or {}
    t = str(c.get("title_zh") or "").strip()
    if t:
        return t
    key = str(c.get("campaign_key") or c.get("campaign_id") or "")
    if key in CAMPAIGN_NAME_FALLBACK:
        return CAMPAIGN_NAME_FALLBACK[key]
    for prefix, name in CAMPAIGN_NAME_FALLBACK.items():
        if key.startswith(prefix):
            return name
    kind = str((c.get("benefit") or {}).get("kind") or "").upper()
    return {"REDEMPTION_CODE": "限时活动（兑换码）",
            "REDEMPTION_COUPON": "限时活动（兑换券）",
            "COUPON": "限时活动（优惠券）",
            "CREDITS": "限时活动（Credits）"}.get(kind) or (key or "限时活动")


def campaign_desc(c):
    """活动官方中文说明（content.zh.description）。"""
    return str((c or {}).get("desc_zh") or "").strip()


def campaign_link(c):
    """活动详情页（优先官方 detailUrl，如 docs 活动页），其次 campaignUrl。"""
    return str((c or {}).get("detail_url") or "").strip()


def _extra_campaign_rows(account, camp):
    """把「非 Credits 奖励」的活动渲染成独立任务行（如奶茶免单卡）。

    官方活动平台里 `benefit.kind` 决定奖励形态：
      CREDITS        -> 每日 100 等，由 daily_checkin 行统一呈现
      REDEMPTION_CODE/REDEMPTION_COUPON/COUPON -> 兑换码/券（如「奶茶免单卡」），
                      领取后代码必须展示给用户（官方客户端是二维码+兑换码）
    状态语义与官方前端一致：
      CLAIMABLE -> completed（待领奖）；CLAIMED -> claimed（已领，附兑换码）
      REDEMPTION_CODE_OUT_OF_STOCK -> 本轮名额已发完，10:00 开启新一轮
      ACHIEVEMENT_NOT_COMPLETED    -> 需先完成指定成就（新人任务）
    """
    rows = []
    for c in camp.get("campaigns") or []:
        kind = str((c.get("benefit") or {}).get("kind") or "").upper()
        if kind in ("", "CREDITS"):
            continue          # Credits 类已由 daily_checkin 行呈现
        key = c.get("campaign_key") or c.get("campaign_id")
        status_raw = str(c.get("claim_status") or "").upper()
        zh_name = campaign_title(c)
        zh_desc = campaign_desc(c)
        link = campaign_link(c) or (camp.get("campaign_url") or "")
        reason = str(c.get("unavailable_reason") or "").upper()
        need = str(c.get("required_achievement_key") or "")
        label = _kind_label(kind)
        reward_text = "%s ×%s" % (label, (c.get("benefit") or {}).get("amount") or 1)
        code = ((account.campaign_codes or {}).get(c["campaign_id"]) or "") \
            if hasattr(account, "campaign_codes") else ""
        if status_raw == "CLAIMED":
            row_status = "claimed"
            desc = "已领取：%s" % reward_text
            if code:
                desc += "，兑换码 %s（在官方活动页可扫码）" % code
            else:
                desc += "，兑换码发放确认中"
        elif status_raw == "CLAIMABLE":
            row_status = "completed"
            desc = "可领取：%s —— 点「领取全部福利」自动领取" % reward_text
        elif reason == "REDEMPTION_CODE_OUT_OF_STOCK" or (
                status_raw == "NOT_ELIGIBLE" and not need):
            row_status = "not_accepted"
            desc = "本轮名额已发完（每日 10:00 开启新一轮）：%s" % reward_text
        elif reason == "ACHIEVEMENT_NOT_COMPLETED" \
                or c.get("achievement_completed") is False:
            row_status = "not_accepted"
            desc = "需先在官方桌面端完成新人任务（成就 %s）后可领：%s" % (
                need or "?", reward_text)
        elif reason in ("CAMPAIGN_NOT_ACTIVE",):
            row_status, desc = "not_accepted", "活动已结束/未开始：%s" % reward_text
        elif c.get("end_at") and c["end_at"] < time.time():
            row_status, desc = "not_accepted", "活动已结束：%s" % reward_text
        else:
            row_status = "not_accepted"
            desc = "暂不可领取%s：%s" % (("（%s）" % reason) if reason else "",
                                     reward_text)
        if zh_desc:
            desc = "%s（%s）" % (desc, zh_desc)
        rows.append({
            "task_code": "campaign:%s" % key,
            "name": zh_name,
            "description": desc,
            "jump_url": link,
            "status": row_status,
            "current": 1 if row_status in ("completed", "claimed") else 0,
            "target": 1,
            "reward_credit": 0,
            "reward_energy": 0,
            "reward_text": reward_text,
            "code": code,
        })
    return rows


def _campaign_state_cn(status, reason, achievement_ok, action_type=""):
    """把活动状态归一成中文分类（与官方前端状态机一致）。

    action_type 非 CLAIM_BENEFIT（如 VIEW_DETAILS 的"点击查看"类活动）单独归为
    view_only：它没有奖励可领，聚合行里标成"可领"会让人点了「领取全部福利」
    却什么都没发生（领取路径会跳过它）。
    """
    st = str(status or "").upper()
    rs = str(reason or "").upper()
    if str(action_type or "") not in ("", "CLAIM_BENEFIT"):
        return "view_only"
    if st == "CLAIMABLE":
        return "claimable"
    if st == "CLAIMED":
        return "claimed"
    if rs == "REDEMPTION_CODE_OUT_OF_STOCK":
        return "out_of_stock"
    if rs == "CAMPAIGN_NOT_ACTIVE":
        return "inactive"
    if rs == "RISK_BLOCKED":
        return "risk_blocked"
    if rs == "ACHIEVEMENT_NOT_COMPLETED" or achievement_ok is False:
        return "task_required"
    return "ineligible"


def aggregate_campaign_rows(accounts, gap=0.0):
    """uid=all：把全部账号的活动**按活动聚合成行**，并给出每账号资格明细。

    现场反馈：只显示"某个账号"的活动会让人以为没资格的活动不存在；这里对每个
    活动列出哪几个账号可领/已领/名额发完/需先完成任务/**无资格（不在定向内）**，
    批量领取时仍然只对"服务端判定可领"的账号发起（逐个账号独立判断，只有上游
    真的返回 SAME_PERSON_ALREADY_CLAIMED 才退避）。

    返回 (rows, codes)：rows 为任务行；codes 为各账号已领取的兑换码明细。
    """
    merged = {}          # campaign_key -> {"c": 样例活动, "by": {状态: [账号名]}}
    codes = []
    total = len(accounts)
    # 每个账号的活动列表都要拉一次（上游最慢的一环）：并行取回，保持面板
    # 秒级响应；campaigns() 自带 20s 缓存，短时间重复查看不会重复打上游。
    states = {}
    if len(accounts) > 1:
        from concurrent.futures import ThreadPoolExecutor
        with ThreadPoolExecutor(max_workers=min(6, len(accounts))) as ex:
            futs = {acc.uid: ex.submit(acc.campaigns) for acc in accounts}
            for acc in accounts:
                try:
                    states[acc.uid] = futs[acc.uid].result()
                except Exception:
                    states[acc.uid] = {"ok": False}
    else:
        for acc in accounts:
            try:
                states[acc.uid] = acc.campaigns()
            except Exception:
                states[acc.uid] = {"ok": False}
    for acc in accounts:
        st = states.get(acc.uid) or {"ok": False}
        for cid, code in (getattr(acc, "campaign_codes", {}) or {}).items():
            if code:
                codes.append({"account": acc.uid, "nickname": _acct_name(acc),
                              "realm": acc.realm, "campaign_id": cid,
                              "campaign": cid, "code": code,
                              "url": (acc.campaign_status or {}).get("campaign_url") or ""})
        if not st.get("ok"):
            continue
        for c in st.get("campaigns") or []:
            key = c["campaign_key"] or c["campaign_id"]
            slot = merged.setdefault(key, {"c": c, "by": {}})
            state = _campaign_state_cn(c.get("claim_status"),
                                       c.get("unavailable_reason"),
                                       c.get("achievement_completed"),
                                       c.get("action_type"))
            slot["by"].setdefault(state, []).append(_acct_name(acc))
    # 活动没出现在某账号列表里 = 该账号不在定向内（无资格）——如实标注
    for key, slot in merged.items():
        listed = [n for names in slot["by"].values() for n in names]
        missing = [a for a in (_acct_name(x) for x in accounts) if a not in listed]
        if missing:
            slot["by"]["no_eligibility"] = missing

    ORDER = ("claimable", "claimed", "out_of_stock", "task_required",
             "risk_blocked", "inactive", "view_only", "ineligible",
             "no_eligibility")
    LABEL = {"claimable": "可领", "claimed": "已领", "out_of_stock": "名额发完",
             "task_required": "需先完成任务", "risk_blocked": "风控拦截",
             "inactive": "活动已结束", "view_only": "仅查看(无奖励)",
             "ineligible": "暂不可领",
             "no_eligibility": "无资格(不在定向)"}
    rows = []
    for key, slot in merged.items():
        c = slot["c"]
        by = slot["by"]
        parts = ["%s %d/%d：%s" % (LABEL[k], len(by[k]), total, "、".join(by[k]))
                 for k in ORDER if by.get(k)]
        kind = str((c.get("benefit") or {}).get("kind") or "").upper()
        amount = (c.get("benefit") or {}).get("amount") or 0
        if kind in ("", "CREDITS"):
            reward_text, reward_credit = "", amount
        else:
            reward_text = "%s ×%s" % (_kind_label(kind), amount or 1)
            reward_credit = 0
        if by.get("claimable"):
            status = "completed"
        elif by.get("claimed") and not by.get("claimable"):
            status = "claimed"
        else:
            status = "not_accepted"
        zh_name = campaign_title(c)
        zh_desc = campaign_desc(c)
        desc_all = "；".join(parts)
        if zh_desc:
            desc_all = "%s（%s）" % (desc_all, zh_desc)
        rows.append({
            "task_code": "campaign:%s" % key,
            "name": zh_name,
            "description": desc_all,
            "jump_url": campaign_link(c),
            "status": status,
            "current": 1 if status in ("completed", "claimed") else 0,
            "target": 1,
            "reward_credit": reward_credit,
            "reward_energy": 0,
            "reward_text": reward_text,
            "accounts_by_state": by,
        })
    rows.sort(key=lambda r: (r["status"] != "completed", r["status"] != "claimed",
                             r["name"]))
    return rows, codes


def fetch_tasks_view(pool, realm=None, uid=None):
    """看板 /tasks 聚合：选定账号的任务行 + 全部可操作账号列表。

    任务中心列出所有启用账号（不再按区域过滤）；签到能力由运行时探测决定，
    接口不存在的区域会显示明确原因而不是空白。
    """
    if not pool or not pool.accounts:
        return {"tasks": [], "summary": {}, "accounts": [],
                "msg": "未找到可用账号"}
    eligible = [a for a in pool.accounts
                if a.enabled and a.access_token
                and (not realm or a.realm == realm)]
    if not eligible:
        return {"tasks": [], "summary": {}, "accounts": [],
                "msg": "未找到可用账号（账号已禁用、缺少凭证或不属于该区域）"}
    acct_list = [{"uid": a.uid, "nickname": a.nickname or a.uid[:8],
                  "realm": a.realm} for a in eligible]
    if not uid or uid == "all":
        # 「全部账号 (批量)」视图：按"活动"聚合（谁有资格/谁没有一目了然），
        # 批量领取仍由 run_batch_checkin 逐账号判断（只对服务端判定可领的账号发起）。
        # 快照过期（> CREDITS_STALE_TTL）的账号并行重拉，避免部署端长期
        # 显示旧余额；20 秒面板缓存已在上层挡住高频重复请求。
        stale = [a for a in eligible if _credits_stale(a)]
        if stale:
            from concurrent.futures import ThreadPoolExecutor
            with ThreadPoolExecutor(max_workers=min(4, len(stale))) as ex:
                list(ex.map(_refresh_credits_safe, stale))
        tasks, codes = aggregate_campaign_rows(eligible)
        breakdown, total = [], 0
        for a in eligible:
            remain = int((a.credits or {}).get("remain") or 0)
            total += remain
            breakdown.append({"uid": a.uid,
                              "nickname": a.nickname or a.uid[:8],
                              "realm": a.realm,
                              "remain": remain})
        summary = {"mode": "all", "realm": realm or "",
                   "accounts_total": len(eligible), "energy": total,
                   "energy_breakdown": breakdown,
                   "streak_days": 0, "travel": {"state": "unknown"},
                   "plan": "全部账号", "codes": codes,
                   "credits": {"remain": total},
                   "campaigns": {"show": True, "claimable": False,
                                 "url": "", "items": []}}
        return {"tasks": tasks, "summary": summary, "account": None,
                "accounts": acct_list, "mode": "all"}
    acc = None
    if uid:
        target = pool.get(uid)
        if target and target in eligible:
            acc = target
    if acc is None:
        acc = eligible[0]
    tasks, summary = fetch_task_view(acc)
    return {"tasks": tasks, "summary": summary, "account": acc.public(),
            "accounts": acct_list}


# ---------------------------------------------------------------------------
# 单账号：签到执行
# ---------------------------------------------------------------------------
def run_checkin(account, gap=1.0, only_daily=False, claim_gap=None):
    """为一个账号执行签到闭环。返回 {ok, logs, earned_credit, credits}。

    顺序（与官方现状一致）：
      1. **活动平台**（campaign-checkin）：当前"每日领取 100 Credits"等限时活动
         的真实入口——带桌面端请求头列出活动 → 对 CLAIMABLE 的 Credits 活动
         POST /claim（幂等，已领过返回 replayed）。双区域通用。
      2. 旧 sash 签到接口：活动平台没拿到东西时兜底（老账号/老活动仍可能有效）。

    only_daily=True：只做"每日签到领积分"（Credits 类活动），不触碰兑换码/券类
    福利与 Pro 包——账号面板的「每日签到」按钮走这条。

    间隔语义（分工，切勿重复 clamp）：
      gap             调用方决定的请求节奏（秒），默认 1.0；None = 交给 accounts 层
                      的安全默认（CLAIM_GAP_DEFAULT）。
      claim_gap       活动平台**内部相邻两次 claim** 的间隔（秒）；None（默认）=
                      跟随 gap。0 是合法值（不等待），因此这里只做 is None 判断，
                      既不用 falsy 判断，也不在本层 max(1.0, ...) 之类地 clamp。
    """
    settle = 1.0 if gap is None else max(0.0, float(gap))
    # 透传给活动平台内部：只有调用方显式给了 claim_gap 才覆盖 gap（gap=0 不丢）
    camp_gap = settle if claim_gap is None else max(0.0, float(claim_gap))
    only_kinds = ("", "CREDITS") if only_daily else None
    logs = []
    name = account.nickname or account.uid[:8]
    logs.append(f"开始为账号 [{name}] 执行每日签到...")

    # --- 1) 活动平台（官方现行机制） ---
    camp = account.campaign_checkin(gap=camp_gap, only_kinds=only_kinds)
    earned = 0
    if camp.get("claimed"):
        keys = ", ".join(qoder_accounts.campaign_label(c) for c in camp["claimed"])
        earned = int(camp.get("earned") or 0)
        head = ("活动领取成功 +%d Credits（%s）" % (earned, keys)) if earned \
            else ("活动领取成功（%s）" % keys)
        logs.append(f"✓ [{name}] {head}"
                    f"{qoder_accounts.round_note(camp['claimed'])}")
    elif camp.get("blocked"):
        codes = ", ".join(b.get("failure_code") or "?" for b in camp["blocked"])
        logs.append(f"⚠ [{name}] 同人已领取：同一设备/身份下其他账号本轮已领"
                    f"（服务端按人去重，{codes}），本号本轮不再发放")
    elif camp.get("already"):
        keys = ", ".join(qoder_accounts.campaign_label(c)
                         for c in camp["already"])
        # 轮次不是自然日（10:00 滚动）：日志用"本轮"，不再写"今日"，
        # 否则上午 10 点前看到的"已领取"会被误当成今天领过
        logs.append(f"✓ [{name}] 本轮奖励已领取（{keys}）"
                    f"{qoder_accounts.round_note(camp['already'])}")
    elif camp.get("ok"):
        logs.append(f"— [{name}] {camp.get('message')}")
    else:
        logs.append(f"! [{name}] 活动平台查询失败：{camp.get('error')}")

    # 兑换码（券类已领/刚领）：单独列出，方便用户复制
    for item in camp.get("codes") or []:
        logs.append(f"  [{name}] {item['campaign']} 兑换码：{item['code']}"
                    f"（官方活动页可扫码兑换）")

    if camp.get("claimed") or camp.get("already") or camp.get("ok"):
        for item in camp.get("pending") or []:
            logs.append("  [%s] %s：本轮名额已发完，10:00 开启新一轮后自动重试"
                        % (name, qoder_accounts.campaign_label(item)))
        for item in camp.get("locked") or []:
            logs.append("  [%s] %s：需先在官方桌面端完成新人任务（成就 %s）"
                        % (name, qoder_accounts.campaign_label(item),
                           item.get("required_achievement_key") or "?"))
        time.sleep(settle)
        if account.fetch_credits().get("ok"):
            logs.append(f"  当前额度余额: {account.credits.get('remain', 0)}")
        account.fetch_plan()
        return {"ok": True, "logs": logs, "earned_credit": earned,
                "credits": account.credits, "campaign": camp.get("message")}

    # --- 2) 旧 sash 签到接口兜底 ---
    # Account.checkin() 内部已带状态前置与 DISABLED 守卫（不硬 claim）
    res2 = account.checkin()
    if res2.get("ok"):
        if res2.get("unavailable") or res2.get("disabled"):
            logs.append(f"— [{name}] {res2.get('msg')}，本次跳过")
            return {"ok": True, "logs": logs, "earned_credit": 0,
                    "credits": account.credits,
                    "unavailable": bool(res2.get("unavailable")),
                    "disabled": bool(res2.get("disabled"))}
        if res2.get("already"):
            logs.append(f"✓ [{name}] {res2.get('msg')}")
        else:
            earned = int(res2.get("reward_credits") or 0)
            logs.append(f"✓ [{name}] 签到成功 +{earned} 积分（连续 {res2.get('streak_days', '-')} 天）")
    else:
        logs.append(f"! [{name}] 签到失败: {res2.get('error')}")
        return {"ok": False, "logs": logs, "earned_credit": 0,
                "error": res2.get("error")}

    # 签到后刷新额度与套餐快照（发放有秒级延迟，失败不影响签到结果）
    time.sleep(settle)
    if account.fetch_credits().get("ok"):
        remain = account.credits.get("remain", 0)
        logs.append(f"  当前额度余额: {remain}")
    account.fetch_plan()
    return {"ok": True, "logs": logs, "earned_credit": earned,
            "credits": account.credits}


# Pro 升级包一次性奖励面额：上游 eligibility/claim 目前不回传金额，故此处兜底；
# 若将来 qoder_accounts 在响应里给出 amount，则优先采用真实值（不硬编码）。
PRO_REWARD_CREDIT = 1800


def run_pro_claim(account):
    """领取一次性 Pro 升级包（+1800）。返回 {ok, logs, earned_credit,
    already_claimed, state, msg, credits}。

    幂等语义：上游把"已领取"归一成**成功**（accounts.Account.pro_claim
    对 HTTP 409 / ALREADY 返回 ok=True + already=True），所以必须区分两件事——
      · state="claimed_now"     本轮真实新增 → 才计入 earned_credit；
      · state="already_claimed" 本就已领取   → earned_credit 记 0，只如实回报事实。
    否则批量汇总的 credit_added 每次都会虚增 +1800（历史缺陷）。
    """
    logs = []
    name = account.nickname or account.uid[:8]
    ok, elig = account.pro_eligibility()
    if not ok:
        logs.append(f"! [{name}] Pro 升级包资格查询失败: {elig}")
        return {"ok": False, "logs": logs, "earned_credit": 0,
                "already_claimed": False, "state": "error",
                "msg": "资格查询失败: %s" % elig}
    if not elig:
        # 资格为假 = 已领过或活动未开放（端点 404/403/410 亦归此）
        logs.append(f"— [{name}] Pro 升级包不可领取（已领或活动未开放）")
        return {"ok": True, "logs": logs, "earned_credit": 0,
                "already_claimed": False, "state": "not_available",
                "msg": "不可领取（已领或活动未开放）"}
    res = account.pro_claim()
    if not res.get("ok"):
        logs.append(f"! [{name}] Pro 升级包领取失败: {res.get('error')}")
        return {"ok": False, "logs": logs, "earned_credit": 0,
                "already_claimed": False, "state": "error",
                "msg": "领取失败: %s" % (res.get("error") or "?"),
                "credits": account.credits}
    if res.get("already"):
        # 上游 409/ALREADY：本轮没有发放，不等待秒级延迟、不计数
        logs.append(f"✓ [{name}] {res.get('msg')}（不重复发放，本次不计新增）")
        return {"ok": True, "logs": logs, "earned_credit": 0,
                "already_claimed": True, "state": "already_claimed",
                "msg": "已领取（不重复发放，本次不计新增）",
                "credits": account.credits}
    logs.append(f"✓ [{name}] {res.get('msg')}")
    time.sleep(1.0)          # 发放有秒级延迟，先等一下再取余额快照
    if account.fetch_credits().get("ok"):
        logs.append(f"  当前额度余额: {account.credits.get('remain', 0)}")
    earned = int(res.get("amount") or 0) or PRO_REWARD_CREDIT
    return {"ok": True, "logs": logs, "earned_credit": earned,
            "already_claimed": False, "state": "claimed_now",
            "msg": "领取成功 +%d 积分" % earned,
            "credits": account.credits}


# ---------------------------------------------------------------------------
# 批量：看板「领取全部福利」/「仅领 Pro 福利包」
# ---------------------------------------------------------------------------
def run_batch_checkin(targets, gap=1.0, inter_gap=1.5):
    """批量签到（全部活动，含券类）。返回 {ok, logs, credit_added, accounts_count}。"""
    combined, total, done = [], 0, 0
    for i, acc in enumerate(targets):
        nick = acc.nickname or acc.uid[:8]
        combined.append("====== 正在为账号 [%s (%s)] 执行每日签到 (%d/%d) ======"
                        % (nick, acc.uid, i + 1, len(targets)))
        res = run_checkin(acc, gap=gap)
        total += res.get("earned_credit") or 0
        done += 1 if res.get("ok") else 0
        for line in res.get("logs") or []:
            combined.append("  " + line)
        if i < len(targets) - 1:
            time.sleep(inter_gap)
    combined.append("====== 全部 %d 个账号签到完毕，累计新增积分: +%d ======"
                    % (len(targets), total))
    for line in combined:
        _log(line)
    return {"ok": done > 0, "logs": combined, "credit_added": total,
            "accounts_count": len(targets)}


def run_batch_pro_claim(targets, gap=1.0, inter_gap=1.5):
    """批量领取福利包。返回 {ok, logs, credit_added, accounts_count,
    already_count, results, msg}。

    credit_added **只累计本轮真实新增**：已领取（already）与不可领取都记 0，
    看板「+N 积分」不会再被"本来就已领过"的账号虚增（历史缺陷）。
    """
    combined, total, results = [], 0, []
    already_count = 0
    for i, acc in enumerate(targets):
        nick = acc.nickname or acc.uid[:8]
        res = run_pro_claim(acc)
        total += res.get("earned_credit") or 0
        if res.get("already_claimed"):
            already_count += 1
        msg = res.get("msg") or (res.get("logs") or [""])[-1]
        results.append({"uid": acc.uid, "nickname": nick,
                        "action": "pro_claim", "msg": msg,
                        "state": res.get("state") or "",
                        "already_claimed": bool(res.get("already_claimed")),
                        "reward_credit": res.get("earned_credit") or 0})
        for line in res.get("logs") or []:
            combined.append(line)
        if i < len(targets) - 1:
            time.sleep(inter_gap)
    if already_count:
        combined.append("====== %d 个账号的 Pro 升级包此前已领取，本次不计新增 ======"
                        % already_count)
    summary_msg = "\n".join(f"{r['nickname']}: {r['msg']}" for r in results)
    return {"ok": True, "logs": combined, "credit_added": total,
            "accounts_count": len(targets), "already_count": already_count,
            "results": results, "msg": summary_msg}


# ---------------------------------------------------------------------------
# 保活（token refresh 巡检）
# ---------------------------------------------------------------------------
def run_keepalive(pool, force=False, threshold_seconds=4 * 3600):
    """刷新凭证：force=True 刷新全部；否则只刷新剩余寿命不足阈值的账号。

    返回 {refreshed, failed, logs}。
    """
    logs, refreshed, failed = [], 0, 0
    now = time.time()
    for acc in list(pool.accounts if pool else []):
        if not acc.enabled or not acc.access_token:
            continue
        remain = (acc.expires_at or 0) - now
        if not force and remain > threshold_seconds:
            continue
        nick = acc.nickname or acc.uid[:8]
        if force or remain <= threshold_seconds:
            logs.append(f"账号 [{nick}] Token 剩余 {_fmt_eta(remain)}，执行主动保活刷新...")
            if acc.refresh():
                refreshed += 1
                logs.append(f"✓ 账号 [{nick}] Token 保活刷新成功（{_fmt_eta((acc.expires_at or 0) - time.time())}）")
            else:
                failed += 1
                logs.append(f"! 账号 [{nick}] Token 保活刷新失败: {acc.last_error}")
            time.sleep(1.0)
    if not logs:
        logs.append("所有账号 Token 均未临近过期，无需刷新")
    for line in logs:
        _log(line)
    return {"refreshed": refreshed, "failed": failed, "logs": logs}


def _fmt_eta(seconds):
    if seconds <= 0:
        return "已过期"
    if seconds >= 86400:
        return "%.1f 天" % (seconds / 86400)
    if seconds >= 3600:
        return "%.1f 小时" % (seconds / 3600)
    return "%d 分钟" % int(seconds / 60)
