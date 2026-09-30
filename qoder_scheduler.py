"""qoder_scheduler.py —— 后台定时调度器 (Scheduler)

负责常驻后台自动执行：
1. 每日签到 (Daily Checkin)：每日 09:00 / 21:00 为所有账号自动签到领积分。
2. 福利包巡检：签到巡检时顺带刷新额度与套餐快照。
3. Token 保活 (Keepalive)：每日 22:00 集中刷新；另对剩余寿命不足 4 小时的
   账号在任意巡检中提前刷新（drt- / jrt- 按 token 前缀路由，PAT 兜底）。
4. 状态持久化与看板展示：暴露状态、执行记录、支持手动立即触发与开关切换。
"""
import threading
import time

import qoder_tasks
from qoder_tasks import set_logger, run_batch_checkin, run_keepalive


class Scheduler(object):
    def __init__(self, pool):
        self.pool = pool
        # 对齐社区默认排程 (本地时区 24 小时制)
        self.checkin_hours = [9, 21]     # 每日 09:00、21:00 签到
        self.keepalive_hours = [22]      # 每日 22:00 集中 Token 保活
        self.all_hours = sorted(set(self.checkin_hours + self.keepalive_hours))
        self.enabled = True
        self._stop_event = threading.Event()
        self._thread = None
        self.last_run_time = None
        self.next_run_time = None
        self.logs = []
        # 防止重叠执行：trigger_now() 每次点击都会开线程，手动触发可能
        # 落在整点巡检之上。
        self._run_lock = threading.Lock()
        self._calc_next_fire()
        # 把任务层失败（死端点、上游结构变化）也打进看板日志。
        set_logger(self.log)

    def log(self, msg):
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        entry = "[%s] %s" % (ts, msg)
        self.logs.append(entry)
        if len(self.logs) > 60:
            self.logs = self.logs[-60:]
        try:
            import qoder_proxy
            qoder_proxy.add_log_entry("[调度器] %s" % msg, tag="scheduler")
        except Exception:
            pass

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()
        self.log("后台定时调度器已启动")

    def stop(self):
        self._stop_event.set()
        self.log("后台定时调度器已暂停")

    def _run_loop(self):
        # 启动后先休眠 10 秒等待主服务就绪，然后执行初次检查
        time.sleep(10)
        try:
            self._execute_cycle("启动初次初始化巡检")
        except Exception as exc:
            self.log("初次巡检异常: %s" % exc)

        while not self._stop_event.is_set():
            self._calc_next_fire()
            now = time.localtime()
            cur_hour = now.tm_hour
            cur_min = now.tm_min
            if self.enabled:
                if cur_min == 0 and cur_hour in self.all_hours:
                    reason = "整点排程命中 (%d:00)" % cur_hour
                    try:
                        self._execute_cycle(reason)
                    except Exception as exc:
                        self.log("排程执行异常: %s" % exc)
                    time.sleep(65)   # 避开当前这一分钟重复触发
            self._stop_event.wait(30)

    def _calc_next_fire(self):
        now = time.localtime()
        cur_h = now.tm_hour
        next_h = None
        for h in self.all_hours:
            if h > cur_h or (h == cur_h and now.tm_min == 0 and now.tm_sec < 10):
                next_h = h
                break
        if next_h is not None:
            t_struct = time.struct_time(
                (now.tm_year, now.tm_mon, now.tm_mday, next_h, 0, 0, 0, 0, -1))
        else:
            t_tomorrow = time.time() + 86400
            now_tom = time.localtime(t_tomorrow)
            first_h = self.all_hours[0]
            t_struct = time.struct_time(
                (now_tom.tm_year, now_tom.tm_mon, now_tom.tm_mday, first_h, 0, 0, 0, 0, -1))
        self.next_run_time = time.strftime("%Y-%m-%d %H:%M:%S", t_struct)

    def trigger_now(self):
        """手动立即触发一次调度检查。"""
        if self._run_lock.locked():
            return {"ok": False, "msg": "已有巡检正在执行，请稍候再试"}
        threading.Thread(target=self._execute_cycle, args=("手动立即触发",),
                         daemon=True).start()
        return {"ok": True, "msg": "已触发后台调度执行"}

    def _execute_cycle(self, trigger_reason="周期巡检"):
        if not self._run_lock.acquire(blocking=False):
            self.log("跳过本次巡检 (%s)：上一轮仍在执行" % trigger_reason)
            return
        try:
            self._run_cycle(trigger_reason)
        finally:
            self._run_lock.release()

    def _run_cycle(self, trigger_reason="周期巡检"):
        self.last_run_time = time.strftime("%Y-%m-%d %H:%M:%S")
        self.log("开始执行任务 (%s)..." % trigger_reason)
        if not self.pool or not self.pool.accounts:
            self.log("暂无可用的活跃账号，跳过本次巡检")
            return

        cur_hour = time.localtime().tm_hour
        keepalive_due = cur_hour in self.keepalive_hours
        checkin_due = cur_hour in self.checkin_hours

        # 1. Token 保活：整点 22:00 全量刷新；其余巡检只刷新临近过期的
        force = keepalive_due
        ka = run_keepalive(self.pool, force=force)
        self.log("Token 保活：刷新 %d 个，失败 %d 个%s"
                 % (ka["refreshed"], ka["failed"],
                    "（22:00 集中保活）" if force else "（临近过期）"))
        for line in ka["logs"]:
            if line.startswith("!"):
                self.log(line)

        # 2. 每日签到：整点签到窗口内执行；其余巡检只补签未签账号
        #    （能力运行时探测：接口不存在的区域由 run_checkin 给出原因并跳过）
        targets = [a for a in self.pool.accounts
                   if a.enabled and a.access_token]
        pending = targets if checkin_due else [a for a in targets if a.can_checkin()]
        checkin_count = 0
        earned = 0
        if pending:
            self.log("检测到 %d 个账号需要签到，执行自动签到..." % len(pending))
            res = run_batch_checkin(pending, gap=1.0, inter_gap=1.0)
            checkin_count = res["accounts_count"]
            earned = res.get("credit_added") or 0
            for line in res["logs"]:
                if line.startswith("✓") or line.startswith("!"):
                    self.log(line)
        elif checkin_due:
            self.log("所有账号今日已签到")

        # 3. 刷新额度快照（看板积分卡片依赖）
        for acc in targets:
            try:
                acc.fetch_credits()
            except Exception:
                pass
            time.sleep(0.5)

        self.log("巡检完成：Token 保活 %d 个，签到 %d 个，本次新增积分 +%d"
                 % (ka["refreshed"], checkin_count, earned))

    def status(self):
        return {
            "enabled": self.enabled,
            "mode": "整点排程 (09:00/21:00 签到 · 22:00 Token 保活)",
            "mode_cn": "整点排程 (09:00/21:00 每日签到 · 22:00 Token 保活)",
            "mode_intl": "整点排程 (09:00/21:00 每日签到 · 22:00 Token 保活)",
            "last_run_time": self.last_run_time or "尚未运行",
            "next_run_time": self.next_run_time or "待调度",
            "logs": self.logs[-20:],
        }
