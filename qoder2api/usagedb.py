"""qoder2api.usagedb —— 用量统计的 SQLite 聚合存储（stdlib sqlite3，零第三方依赖）。

定位：`usage/usage.jsonl` 是**唯一真源**（每行一条请求记录，追加写、永久保留），
本模块把它的原始行**增量投影**成两级聚合表，供 /usage/stats 做任意时间范围、
按模型 / 按密钥的快速统计。这样：
  · 统计查询不再逐行扫 JSONL（30 天/自定义范围的扫描成本恒定在聚合行数上）；
  · 无限期保留（聚合行每「天×区域×模型×密钥」一行，一年也不过几千行）；
  · 与写路径解耦——统计库损坏/丢失可以直接删库重建（下次 sync 全量重放）。

一致性设计（关键，改动前务必读懂）：
  · meta 表记录 `jsonl_offset`：**已导入到 JSONL 的哪个字节**；
  · 导入在**同一个事务**里「写入聚合行 + 前移 offset」，崩溃只会回滚、
    绝不会出现「行已计数但 offset 未前移」导致的重复计数；
  · 聚合是 UPSERT 累加（+），因此**绝不能重复导入同一段字节**——这是
    offset 必须与聚合行同事务提交的根本原因；
  · JSONL 被截断/轮转（文件比 offset 短）时把 offset 夹回文件长度并告警：
    宁可丢一段历史计数，也不能把新文件内容当成旧内容二次累加。
  另有一个已知小窗口：JSONL 的最后一行可能只写了一半（进程被杀）——导入
  遇到没有换行结尾的尾行会停在那里，下次 sync 再补，不会把半行当完整行。

线程模型：一个进程内共享一条只读/写连接（check_same_thread=False）+ 一把
模块锁串行化所有写入与查询（个人网关的 QPS 很低，简单优先）。WAL 模式让
读不阻塞写。
"""
import json
import os
import sqlite3
import threading
import time

from . import runtime
from .logbus import log

SCHEMA_VERSION = "2"
DB_FILENAME = "usage.db"

# 聚合口径的哨兵与显示名：
#   ""  —— 行里明确没有密钥（未开启鉴权的调用：key_id 字段存在但为空）
#   LEGACY_KEY_ID —— v1.2.7 之前落的历史行**根本没有 key_id 字段**（当时还没有
#   这个维度）。两者分开统计，面板上分别显示，不再混成一句含糊的「未绑定 Key」。
LEGACY_KEY_ID = "__legacy__"
KEY_LABELS = {
    "": "未使用密钥",
    LEGACY_KEY_ID: "旧版数据（升级前）",
}

# 趋势图最多画多少条序列（超出折叠为「其他」）；表页仍返回全部行。
TOP_SERIES = 8
# 一次查询允许的最大跨度（天）；"不限时间"指存储保留，不是允许一次拉十年。
MAX_SPAN_DAYS = 400

_LOCK = threading.RLock()
_CONN = None
_SCHEMA_READY = False


def db_path():
    return os.path.join(runtime.USAGE_DIR, DB_FILENAME)


def _connect():
    """惰性建连接 + 建表（进程内只做一次 DDL）。"""
    global _CONN, _SCHEMA_READY
    if _CONN is not None and _SCHEMA_READY:
        return _CONN
    path = db_path()
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    except Exception:
        pass
    _ensure_schema(conn)
    _CONN = conn
    _SCHEMA_READY = True
    return conn


_COLUMNS = (
    "requests", "failed", "prompt_tokens", "completion_tokens",
    "reasoning_tokens", "cached_tokens", "total_tokens", "credit",
)


def _create_table(conn, name, extra_col, pk_cols):
    conn.execute(
        "CREATE TABLE IF NOT EXISTS %s (\n"
        "  %s\n"
        "  realm TEXT NOT NULL DEFAULT '',\n"
        "  model TEXT NOT NULL DEFAULT '',\n"
        "  key_id TEXT NOT NULL DEFAULT '',\n"
        "  key_name TEXT NOT NULL DEFAULT '',\n"
        "  requests INTEGER NOT NULL DEFAULT 0,\n"
        "  failed INTEGER NOT NULL DEFAULT 0,\n"
        "  prompt_tokens INTEGER NOT NULL DEFAULT 0,\n"
        "  completion_tokens INTEGER NOT NULL DEFAULT 0,\n"
        "  reasoning_tokens INTEGER NOT NULL DEFAULT 0,\n"
        "  cached_tokens INTEGER NOT NULL DEFAULT 0,\n"
        "  total_tokens INTEGER NOT NULL DEFAULT 0,\n"
        "  credit REAL NOT NULL DEFAULT 0,\n"
        "  PRIMARY KEY (%s)\n"
        ")" % (name, extra_col, pk_cols))


def _ensure_schema(conn):
    _create_table(conn, "usage_hourly", "day TEXT NOT NULL,\n  hour INTEGER NOT NULL,",
                  "day, hour, realm, model, key_id")
    _create_table(conn, "usage_daily", "day TEXT NOT NULL,", "day, realm, model, key_id")
    conn.execute("CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_hourly_day ON usage_hourly(day)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_daily_day ON usage_daily(day)")
    row = conn.execute("SELECT v FROM meta WHERE k = 'schema_version'").fetchone()
    old = row["v"] if row else ""
    if old and old != SCHEMA_VERSION:
        # 口径升级：聚合表只是 JSONL 的投影，直接清空重放（成本 = 一次全量导入）。
        # v1 -> v2：把「没有 key_id 字段」的历史行与「有字段但为空」的行分开，
        # 否则面板上历史数据会冒充成"未使用密钥"。
        conn.execute("DELETE FROM usage_hourly")
        conn.execute("DELETE FROM usage_daily")
        conn.execute("DELETE FROM meta WHERE k = 'jsonl_offset'")
        log("usagedb: 聚合口径 v%s -> v%s，清空聚合表按新口径重放 JSONL"
            % (old, SCHEMA_VERSION))
    conn.execute("INSERT OR REPLACE INTO meta(k, v) VALUES ('schema_version', ?)",
                 (SCHEMA_VERSION,))
    conn.commit()


def _meta_get(conn, key, default=""):
    row = conn.execute("SELECT v FROM meta WHERE k = ?", (key,)).fetchone()
    return row["v"] if row else default


def _meta_set(conn, key, value):
    conn.execute("INSERT INTO meta(k, v) VALUES (?, ?) "
                 "ON CONFLICT(k) DO UPDATE SET v = excluded.v", (key, str(value)))


# ---------------------------------------------------------------------------
# 原始行 -> 聚合桶
# ---------------------------------------------------------------------------
def _day_hour(at):
    """本地时区切天/切小时（与 usage_daily 的仪表盘口径一致）。"""
    lt = time.localtime(at)
    return time.strftime("%Y-%m-%d", lt), lt.tm_hour


def _row_realm(row):
    """行所属区域：优先行内 realm；老数据没有时按账号池反查，再退到区域独占模型。

    判定顺序与 usage.row_matches_realm 对齐（那边多一层"动态当前出口"的兜底，
    这里刻意不做：导入是持久的，不能把导入时刻的全局开关固化进行里）。
    """
    realm = str(row.get("realm") or "").strip().lower()
    if realm in ("cn", "intl"):
        return realm
    uid = str(row.get("account") or "")
    if uid and runtime.POOL is not None:
        try:
            acc = runtime.POOL.get(uid)
            if acc is not None and acc.realm in ("cn", "intl"):
                return acc.realm
        except Exception:
            pass
    model = str(row.get("model") or "")
    if model:
        try:
            from .realm import exclusive_realm      # 区域独占模型是静态事实
            owner = exclusive_realm(model)
            if owner in ("cn", "intl"):
                return owner
        except Exception:
            pass
    return ""


def _row_counters(row):
    """一行 JSONL -> 该行对聚合桶的贡献（与 usage_daily 的计数口径一致：
    requests 含失败行；tokens/credit 只算成功行）。"""
    out = {k: 0 for k in _COLUMNS}
    out["requests"] = 1
    if row.get("error"):
        out["failed"] = 1
        return out
    for k in ("prompt_tokens", "completion_tokens", "reasoning_tokens",
              "cached_tokens", "total_tokens"):
        try:
            out[k] = int(row.get(k) or 0)
        except Exception:
            out[k] = 0
    try:
        out["credit"] = float(row.get("credit") or 0)
    except Exception:
        out["credit"] = 0.0
    return out


def _row_bucket(row):
    """一行 JSONL -> ((day, hour), (realm, model, key_id, key_name), counters)。

    密钥维度分三态：有 key_id 字段且非空 -> 真实密钥；有字段但为空 -> 未使用
    密钥（未开鉴权的调用）；**根本没有该字段** -> v1.2.7 之前的旧版数据
    （那时还没记录密钥维度），用哨兵值单独成桶，不得冒充"未使用密钥"。
    """
    at = row.get("at")
    if not isinstance(at, (int, float)):
        return None
    day, hour = _day_hour(at)
    if "key_id" in row:
        key_id = str(row.get("key_id") or "")
        key_name = str(row.get("key_name") or "")
    else:
        key_id, key_name = LEGACY_KEY_ID, ""
    return (day, hour), (_row_realm(row), str(row.get("model") or ""),
                         key_id, key_name), _row_counters(row)


def _upsert_many(conn, table, rows):
    """rows: list of (bucket_tuple, dims_tuple, counters)。table 决定列数。"""
    if not rows:
        return
    hourly = table == "usage_hourly"
    fixed = ["day", "hour", "realm", "model", "key_id", "key_name"] if hourly \
        else ["day", "realm", "model", "key_id", "key_name"]
    cols = ", ".join(fixed + list(_COLUMNS))
    ph = ", ".join("?" * (len(fixed) + len(_COLUMNS)))
    updates = ", ".join("%s = %s + excluded.%s" % (c, c, c) for c in _COLUMNS)
    updates += ", key_name = excluded.key_name"     # 名称以最后一次见到为准
    sql = ("INSERT INTO %s (%s) VALUES (%s) "
           "ON CONFLICT DO UPDATE SET %s" % (table, cols, ph, updates))
    conn.executemany(sql, [
        tuple(bucket) + tuple(dims) + tuple(counters[c] for c in _COLUMNS)
        for bucket, dims, counters in rows])


# ---------------------------------------------------------------------------
# 增量导入（JSONL -> SQLite）
# ---------------------------------------------------------------------------
def sync(max_bytes=None):
    """把 JSONL 自上次 offset 起的新行导入聚合表。返回导入的行数。

    与聚合行同事务写入 offset（见模块 docstring 的一致性设计）。
    读取时按行计字节：只有以 "\\n" 结尾的整行才计入 offset。
    """
    path = runtime.USAGE_LOG
    with _LOCK:
        try:
            conn = _connect()
        except Exception as exc:
            log("usagedb: 打开统计库失败: %s" % exc)
            return 0
        try:
            size = os.path.getsize(path)
        except OSError:
            return 0                                   # 还没有任何请求
        offset = 0
        try:
            offset = int(_meta_get(conn, "jsonl_offset", "0") or 0)
        except Exception:
            offset = 0
        if offset > size:
            log("usagedb: usage.jsonl 比已导入位置短（%d < %d），判定为截断/"
                "轮转：把导入位置夹回文件末尾（这段历史不重复计数）"
                % (size, offset), level="WARN")
            with conn:
                _meta_set(conn, "jsonl_offset", size)
            return 0
        if offset == size:
            return 0
        rows = []
        consumed = offset
        try:
            with open(path, "rb") as fh:
                fh.seek(offset)
                while True:
                    line = fh.readline()
                    if not line:
                        break
                    if not line.endswith(b"\n"):
                        break                              # 半行：下次再读
                    if max_bytes is not None and consumed - offset >= max_bytes:
                        break
                    consumed += len(line)
                    text = line.strip()
                    if not text:
                        continue
                    try:
                        row = json.loads(text.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if not isinstance(row, dict):
                        continue
                    bucket = _row_bucket(row)
                    if bucket is None:
                        continue
                    rows.append(bucket)
        except OSError as exc:
            log("usagedb: 读取 usage.jsonl 失败: %s" % exc)
            return 0
        try:
            with conn:                                  # 一个事务：行 + offset
                _upsert_many(conn, "usage_hourly", rows)
                _upsert_many(conn, "usage_daily", [
                    ((day,), dims, counters) for (day, _hour), dims, counters in rows])
                _meta_set(conn, "jsonl_offset", consumed)
                _meta_set(conn, "last_sync_at", time.strftime("%Y-%m-%d %H:%M:%S"))
        except Exception as exc:
            log("usagedb: 导入失败（下一次 sync 会重试）: %s" % exc)
            return 0
        return len(rows)


def status():
    """只读诊断：库路径、已导入位置、聚合行数、最近一次同步时间。"""
    with _LOCK:
        try:
            conn = _connect()
            hourly = conn.execute("SELECT COUNT(*) AS n FROM usage_hourly").fetchone()["n"]
            daily = conn.execute("SELECT COUNT(*) AS n FROM usage_daily").fetchone()["n"]
            offset = _meta_get(conn, "jsonl_offset", "0")
            last = _meta_get(conn, "last_sync_at", "")
        except Exception as exc:
            return {"ok": False, "error": str(exc), "path": db_path()}
        try:
            size = os.path.getsize(runtime.USAGE_LOG)
        except OSError:
            size = 0
        return {"ok": True, "path": db_path(), "jsonl_offset": int(offset or 0),
                "jsonl_size": size, "hourly_rows": hourly, "daily_rows": daily,
                "last_sync_at": last}


# ---------------------------------------------------------------------------
# 查询
# ---------------------------------------------------------------------------
def _clamp_days(from_day, to_day):
    """校验 YYYY-MM-DD 顺序与跨度；返回 (from, to, days)。"""
    fmt = "%Y-%m-%d"
    f = time.strptime(from_day, fmt)
    t = time.strptime(to_day, fmt)
    if f > t:
        raise ValueError("from must not be after to")
    days = int((time.mktime(t) - time.mktime(f)) / 86400) + 1
    if days > MAX_SPAN_DAYS:
        raise ValueError("range too large (max %d days)" % MAX_SPAN_DAYS)
    return time.strftime(fmt, f), time.strftime(fmt, t), days


def _day_labels(from_day, to_day):
    fmt = "%Y-%m-%d"
    start = time.mktime(time.strptime(from_day, fmt))
    end = time.mktime(time.strptime(to_day, fmt))
    out = []
    cur = start
    while cur <= end:
        out.append(time.strftime(fmt, time.localtime(cur)))
        cur += 86400
    return out


def _hour_labels(from_day, to_day, days):
    """按小时的桶标签：单日 = 00:00..23:00；跨日 = 每天的 24 个桶（MM-DD HH:00）。"""
    if days <= 1:
        return ["%02d:00" % h for h in range(24)]
    return ["%s %02d:00" % (day[5:], h)
            for day in _day_labels(from_day, to_day) for h in range(24)]


def _where(realm, from_day, to_day):
    sql = "day BETWEEN ? AND ?"
    args = [from_day, to_day]
    if realm in ("cn", "intl"):
        sql += " AND realm = ?"
        args.append(realm)
    return sql, args


def _sum_row(row):
    return {
        "requests": int(row["requests"] or 0),
        "failed": int(row["failed"] or 0),
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "reasoning_tokens": int(row["reasoning_tokens"] or 0),
        "cached_tokens": int(row["cached_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "credit": round(float(row["credit"] or 0), 4),
    }


_SUM_COLS = ", ".join("SUM(%s) AS %s" % (c, c) for c in _COLUMNS)


def _query_range(conn, table, from_day, to_day, realm, group_cols, bucket_cols):
    where, args = _where(realm, from_day, to_day)
    cols = ", ".join(bucket_cols + group_cols)
    sql = ("SELECT %s, %s FROM %s WHERE %s GROUP BY %s"
           % (cols, _SUM_COLS, table, where, ", ".join(bucket_cols + group_cols)))
    return conn.execute(sql, args).fetchall()


def _granularity_for(days, requested):
    if requested in ("hour", "day"):
        return requested
    return "hour" if days <= 2 else "day"


def key_display(key_id, key_name=""):
    """密钥分组的展示名：哨兵桶（旧版数据 / 未使用密钥）用固定中文标签，
    真实密钥用记录时的名字，名字缺失才回落 key_id 本身。"""
    if key_id in KEY_LABELS:
        return KEY_LABELS[key_id]
    return key_name or key_id


def _trend(conn, from_day, to_day, realm, group, granularity, labels, days):
    """趋势序列：labels 长度的数组，缺失桶补 0；按 total_tokens 取前 N 条。"""
    if granularity == "hour":
        table = "usage_hourly"
        bucket_cols = ["day", "hour"] if days > 1 else ["hour"]
    else:
        table = "usage_daily"
        bucket_cols = ["day"]

    group_cols = [] if group == "total" else [group]     # model | key_id
    rows = _query_range(conn, table, from_day, to_day, realm,
                        group_cols, bucket_cols)

    # 密钥分组时 id 是稳定键、名称会变（改名/删除），显示名单独解析：
    # 取窗口内最后见到的 key_name；空桶/旧版桶走 key_display 的固定标签。
    key_titles = {}
    if group == "key_id":
        where, args = _where(realm, from_day, to_day)
        for kr in conn.execute(
                "SELECT key_id, MAX(key_name) AS key_name FROM usage_daily "
                "WHERE %s GROUP BY key_id" % where, args).fetchall():
            kid = str(kr["key_id"] or "")
            key_titles[kid] = key_display(kid, str(kr["key_name"] or ""))

    index = {label: i for i, label in enumerate(labels)}
    series = {}
    for r in rows:
        if granularity == "hour":
            label = ("%s %02d:00" % (r["day"][5:], int(r["hour"])) if days > 1
                     else "%02d:00" % int(r["hour"]))
        else:
            label = str(r["day"])
        i = index.get(label)
        if i is None:
            continue                                     # 本不该发生（防御）
        if group == "total":
            name = title = "总量"
        elif group == "key_id":
            name = str(r["key_id"] or "")
            title = key_titles.get(name) or "未使用密钥"
        else:
            name = title = str(r["model"] or "")
        slot = series.setdefault(name, {
            "name": name, "title": title, "requests": [0] * len(labels),
            "tokens": [0] * len(labels), "failed": [0] * len(labels)})
        slot["requests"][i] += int(r["requests"] or 0)
        slot["tokens"][i] += int(r["total_tokens"] or 0)
        slot["failed"][i] += int(r["failed"] or 0)

    ordered = sorted(series.values(),
                     key=lambda s: sum(s["tokens"]) or sum(s["requests"]),
                     reverse=True)
    if len(ordered) > TOP_SERIES:                        # 其余折叠为「其他」
        rest = ordered[TOP_SERIES - 1:]
        ordered = ordered[:TOP_SERIES - 1]
        other = {"name": "其他", "title": "其他", "requests": [0] * len(labels),
                 "tokens": [0] * len(labels), "failed": [0] * len(labels)}
        for s in rest:
            for i in range(len(labels)):
                other["requests"][i] += s["requests"][i]
                other["tokens"][i] += s["tokens"][i]
                other["failed"][i] += s["failed"][i]
        ordered.append(other)
    if not ordered:
        ordered = [{"name": "总量", "title": "总量", "requests": [0] * len(labels),
                    "tokens": [0] * len(labels), "failed": [0] * len(labels)}]
    return {"granularity": granularity, "group": group, "labels": labels,
            "series": ordered}


def stats(from_day, to_day, realm="", group="total", granularity=None):
    """一个时间范围的完整统计：KPI + 按模型 + 按密钥 + 趋势。

    from_day/to_day: 本地日期 YYYY-MM-DD（含两端）；realm: cn/intl/''(全部)；
    group: total/model/key_id（趋势的分组维度）；
    granularity: auto/hour/day（auto: 跨度 <=2 天按小时，否则按天）。
    """
    sync()          # 读前先增量导入：统计永远是"读到最新一行"的
    from_day, to_day, days = _clamp_days(from_day, to_day)
    group = group if group in ("total", "model", "key_id") else "total"
    gran = _granularity_for(days, granularity)
    labels = (_hour_labels(from_day, to_day, days) if gran == "hour"
              else _day_labels(from_day, to_day))
    with _LOCK:
        conn = _connect()
        where, args = _where(realm, from_day, to_day)
        summary = _sum_row(conn.execute(
            "SELECT %s FROM usage_daily WHERE %s" % (_SUM_COLS, where),
            args).fetchone())
        by_model = [_sum_row(r) | {"model": r["model"]} for r in conn.execute(
            "SELECT model, %s FROM usage_daily WHERE %s "
            "GROUP BY model ORDER BY SUM(total_tokens) DESC, SUM(requests) DESC"
            % (_SUM_COLS, where), args).fetchall()]
        by_key = []
        for r in conn.execute(
                "SELECT key_id, MAX(key_name) AS key_name, %s FROM usage_daily "
                "WHERE %s GROUP BY key_id "
                "ORDER BY SUM(total_tokens) DESC, SUM(requests) DESC"
                % (_SUM_COLS, where), args).fetchall():
            kid = str(r["key_id"] or "")
            by_key.append(_sum_row(r) | {
                "key_id": kid,
                "key_name": key_display(kid, str(r["key_name"] or "")),
                # 标记该桶是"没有真实密钥"的聚合桶（前端可用于弱化显示）
                "is_placeholder": kid in KEY_LABELS})
        trend = _trend(conn, from_day, to_day, realm, group, gran, labels, days)
    summary["cache_hit_pct"] = (round(summary["cached_tokens"] * 100.0
                                      / summary["prompt_tokens"], 1)
                                if summary["prompt_tokens"] else 0.0)
    return {
        "ok": True,
        "range": {"from": from_day, "to": to_day, "days": days,
                  "granularity": gran, "group": group,
                  "realm": realm if realm in ("cn", "intl") else "all"},
        "summary": summary,
        "by_model": by_model,
        "by_key": by_key,
        "trend": trend,
    }


def count_requests(realm="", from_day=None, to_day=None):
    """请求总条数（含失败行）—— /usage/recent 分页总数用，避免全扫 JSONL。

    口径：一行请求记 1（错误行也记，与 recent_usage 的返回行数一致）。
    可选 from_day/to_day（YYYY-MM-DD，本地日，含两端）把计数限制在时间范围内
    ——用量页的「全部请求记录」跟随日期选择走。先做一次增量 sync，保证刚发生
    的请求立刻计入；统计库不可用时由调用方回退到逐行扫描。
    """
    sync()
    clauses, args = [], []
    if from_day and to_day:
        clauses.append("day BETWEEN ? AND ?")
        args += [from_day, to_day]
    if realm in ("cn", "intl"):
        clauses.append("realm = ?")
        args.append(realm)
    sql = "SELECT SUM(requests) AS n FROM usage_daily"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    with _LOCK:
        conn = _connect()
        row = conn.execute(sql, args).fetchone()
        return int((row["n"] if row else 0) or 0)


def rebuild():
    """删掉聚合内容并全量重放 JSONL（统计口径变化或库损坏时用）。

    只清聚合表与 offset，**不删库文件**（连接仍可用）；WAL 一并清理。
    """
    with _LOCK:
        conn = _connect()
        with conn:
            conn.execute("DELETE FROM usage_hourly")
            conn.execute("DELETE FROM usage_daily")
            _meta_set(conn, "jsonl_offset", 0)
        n = sync()
        return {"ok": True, "imported": n, "status": status()}
