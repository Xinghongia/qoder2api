"""qoder_tasks.py —— Qoder 每日签到、额度与福利包自动化引擎

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

import qoder_accounts
from qoder_accounts import get_realm_config

_log = lambda msg: None

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
      CLAIMABLE + CLAIM_BENEFIT -> "completed"（待领奖，点「一键签到」即自动领取）
      全部 CLAIMED              -> "claimed"  （今日已领取）
      其它/无活动               -> "not_accepted"
    """
    website = get_realm_config(account.realm)["website"]
    jump = (camp.get("campaign_url") or website + "/activities") \
        if camp.get("ok") else website + "/activities"
    items = [{"key": c["campaign_key"] or c["campaign_id"],
              "id": c["campaign_id"],
              "action_type": c.get("action_type", ""),
              "claim_status": c.get("claim_status", ""),
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
    claimable = [c for c in items
                 if c["claim_status"] == "CLAIMABLE"
                 and c["action_type"] in ("", "CLAIM_BENEFIT")]
    claimed = [c for c in items if c["claim_status"] == "CLAIMED"]
    # 成就门控活动（如 CN 新人「奶茶免单卡」需先完成 sites_first_use）：
    # 服务端状态 ACHIEVEMENT_NOT_COMPLETED —— 显示成就要求，不能直接领取
    gated = [c for c in items if c["claim_status"] == "ACHIEVEMENT_NOT_COMPLETED"]
    if claimable:
        amount = sum(c["benefit_amount"] or 0 for c in claimable)
        keys = ", ".join(c["key"] for c in claimable)
        return {
            "task_code": "daily_checkin",
            "name": "每日签到（每日领取 Credits）",
            "description": "可领取 %s Credits（%s）—— 点「一键签到」或该账号行的「签到」直接领取"
                           % (amount or "-", keys),
            "jump_url": jump,
            "status": "completed",
            "current": 1,
            "target": 1,
            "reward_credit": amount,
            "reward_energy": 0,
        }
    if claimed:
        amount = sum(c["benefit_amount"] or 0 for c in claimed)
        keys = ", ".join(c["key"] for c in claimed)
        return {
            "task_code": "daily_checkin",
            "name": "每日签到（每日领取 Credits）",
            "description": "今日已领取%s（%s），明日再来"
                           % ((" +%s Credits" % amount) if amount else "", keys),
            "jump_url": jump,
            "status": "claimed",
            "current": 1,
            "target": 1,
            "reward_credit": amount,
            "reward_energy": 0,
        }
    if gated:
        keys = ", ".join(c["key"] for c in gated)
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
    acc = None
    if uid and uid != "all":
        target = pool.get(uid)
        if target and target in eligible:
            acc = target
    batch_mode = acc is None
    if acc is None:
        acc = eligible[0]
    tasks, summary = fetch_task_view(acc)
    if batch_mode:
        # 「全部账号 (批量)」视图：余额卡片显示**各账号合计**（此前用的是
        # 首个账号的快照，账号一多就对不上；单账号视图保持原样）。
        breakdown, total = [], 0
        for a in eligible:
            if not a.credits:
                try:
                    a.fetch_credits()
                except Exception:
                    pass
            remain = int((a.credits or {}).get("remain") or 0)
            total += remain
            breakdown.append({"uid": a.uid,
                              "nickname": a.nickname or a.uid[:8],
                              "realm": a.realm,
                              "remain": remain})
        summary["energy"] = total
        summary["energy_breakdown"] = breakdown
    acct_list = [{"uid": a.uid, "nickname": a.nickname or a.uid[:8],
                  "realm": a.realm} for a in eligible]
    return {"tasks": tasks, "summary": summary, "account": acc.public(),
            "accounts": acct_list}


# ---------------------------------------------------------------------------
# 单账号：签到执行
# ---------------------------------------------------------------------------
def run_checkin(account, gap=1.0):
    """为一个账号执行签到闭环。返回 {ok, logs, earned_credit, credits}。

    顺序（与官方现状一致）：
      1. **活动平台**（campaign-checkin）：当前"每日领取 100 Credits"等限时活动
         的真实入口——带桌面端请求头列出活动 → 对 CLAIMABLE 的 Credits 活动
         POST /claim（幂等，已领过返回 replayed）。双区域通用。
      2. 旧 sash 签到接口：活动平台没拿到东西时兜底（老账号/老活动仍可能有效）。
    """
    logs = []
    name = account.nickname or account.uid[:8]
    logs.append(f"开始为账号 [{name}] 执行每日签到...")

    # --- 1) 活动平台（官方现行机制） ---
    camp = account.campaign_checkin()
    earned = 0
    if camp.get("claimed"):
        keys = ", ".join(c.get("campaign_key") or c.get("campaign_id")
                         for c in camp["claimed"])
        earned = int(camp.get("earned") or 0)
        logs.append(f"✓ [{name}] 活动领取成功 +{earned} Credits（{keys}）")
    elif camp.get("blocked"):
        codes = ", ".join(b.get("failure_code") or "?" for b in camp["blocked"])
        logs.append(f"⚠ [{name}] 同人已领取：同一设备/身份下其他账号本轮已领"
                    f"（服务端按人去重，{codes}），本号本轮不再发放")
    elif camp.get("already"):
        keys = ", ".join(c.get("campaign_key") or c.get("campaign_id")
                         for c in camp["already"])
        logs.append(f"✓ [{name}] 今日活动奖励已领取（{keys}）")
    elif camp.get("ok"):
        logs.append(f"— [{name}] {camp.get('message')}")
    else:
        logs.append(f"! [{name}] 活动平台查询失败：{camp.get('error')}")

    if camp.get("claimed") or camp.get("already") or camp.get("ok"):
        time.sleep(gap)
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
    time.sleep(gap)
    if account.fetch_credits().get("ok"):
        remain = account.credits.get("remain", 0)
        logs.append(f"  当前额度余额: {remain}")
    account.fetch_plan()
    return {"ok": True, "logs": logs, "earned_credit": earned,
            "credits": account.credits}


def run_pro_claim(account):
    """领取一次性 Pro 升级包（+1800）。"""
    logs = []
    name = account.nickname or account.uid[:8]
    ok, elig = account.pro_eligibility()
    if not ok:
        logs.append(f"! [{name}] Pro 升级包资格查询失败: {elig}")
        return {"ok": False, "logs": logs, "earned_credit": 0}
    if not elig:
        logs.append(f"— [{name}] Pro 升级包不可领取（已领或活动未开放）")
        return {"ok": True, "logs": logs, "earned_credit": 0}
    res = account.pro_claim()
    earned = 0
    if res.get("ok"):
        logs.append(f"✓ [{name}] {res.get('msg')}")
        time.sleep(1.0)
        if account.fetch_credits().get("ok"):
            logs.append(f"  当前额度余额: {account.credits.get('remain', 0)}")
        earned = 1800
    else:
        logs.append(f"! [{name}] Pro 升级包领取失败: {res.get('error')}")
    return {"ok": bool(res.get("ok")), "logs": logs, "earned_credit": earned,
            "credits": account.credits}


# ---------------------------------------------------------------------------
# 批量：看板「一键签到领积分」/「领取福利包」
# ---------------------------------------------------------------------------
def run_batch_checkin(targets, gap=1.0, inter_gap=1.5):
    """批量签到。返回 {ok, logs, credit_added, accounts_count}。"""
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
    """批量领取福利包。返回 {ok, logs, credit_added, accounts_count, results}。"""
    combined, total, results = [], 0, []
    for i, acc in enumerate(targets):
        nick = acc.nickname or acc.uid[:8]
        res = run_pro_claim(acc)
        total += res.get("earned_credit") or 0
        msg = (res.get("logs") or [""])[-1]
        results.append({"uid": acc.uid, "nickname": nick,
                        "action": "pro_claim", "msg": msg,
                        "reward_credit": res.get("earned_credit") or 0})
        for line in res.get("logs") or []:
            combined.append(line)
        if i < len(targets) - 1:
            time.sleep(inter_gap)
    summary_msg = "\n".join(f"{r['nickname']}: {r['msg']}" for r in results)
    return {"ok": True, "logs": combined, "credit_added": total,
            "accounts_count": len(targets), "results": results,
            "msg": summary_msg}


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
