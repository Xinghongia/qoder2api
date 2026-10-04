"""qoder2api.usagedb 的离线测试（用量统计 SQLite 聚合）。

用临时目录 + 合成 JSONL 驱动（**每个场景前 _reset()**，避免场景间互相污染），
覆盖：
  · 全量回填计数 / 重复 sync 幂等 / 半行不导入、补齐后导入
  · 文件截断（轮转）时的 offset 夹回（不重复计数）+ rebuild 全量重放
  · 区域过滤（行内 realm + 账号池反查）、时间范围、非法范围
  · 按模型 / 按密钥 / 总量分组、趋势桶（小时/天）、缺失桶补零、top-N 折叠

    python tests/test_usagedb.py
"""
import json
import os
import shutil
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from qoder2api import runtime, usagedb      # noqa: E402

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


_TMP = tempfile.mkdtemp(prefix="qd-usagedb-")
runtime.USAGE_DIR = _TMP
runtime.USAGE_LOG = os.path.join(_TMP, "usage.jsonl")

_NOW = time.time()
_TODAY = time.strftime("%Y-%m-%d", time.localtime(_NOW))
_DAY = lambda off: time.strftime("%Y-%m-%d", time.localtime(_NOW + off * 86400))


def _reset():
    """清空库与日志，回到「全新安装」状态。

    必须先关连接：Windows 上删除仍被打开的文件会失败（PermissionError），
    库文件删不掉就会带着上一场景的数据继续用（曾经因此假失败）。
    """
    if usagedb._CONN is not None:
        try:
            usagedb._CONN.close()
        except Exception:
            pass
    usagedb._CONN = None
    usagedb._SCHEMA_READY = False
    for name in ("usage.db", "usage.db-wal", "usage.db-shm"):
        try:
            os.unlink(os.path.join(_TMP, name))
        except OSError:
            pass
    try:
        os.unlink(runtime.USAGE_LOG)
    except OSError:
        pass


def _write(rows):
    """整份重写 JSONL（只在新场景开头用；文件会变短 -> 触发截断夹回）。"""
    with open(runtime.USAGE_LOG, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _append_text(text):
    with open(runtime.USAGE_LOG, "a", encoding="utf-8") as fh:
        fh.write(text)


def _ok(hours_ago, model="auto", realm="intl", key=("k0", "主 Key"),
        tokens=100, prompt=80, cached=40, credit=0.5):
    return {"at": _NOW - hours_ago * 3600, "model": model, "stream": True,
            "total_tokens": tokens, "prompt_tokens": prompt,
            "completion_tokens": max(0, tokens - prompt), "cached_tokens": cached,
            "credit": credit, "realm": realm,
            "key_id": key[0] if key else "", "key_name": key[1] if key else ""}


def _err(hours_ago, model="auto", realm="intl", key=("k0", "主 Key")):
    row = {"at": _NOW - hours_ago * 3600, "model": model, "error": True,
           "status": 502, "realm": realm}
    if key:
        row["key_id"], row["key_name"] = key
    return row


print("[1] 全量回填 / 幂等 / 半行 / 计数口径")
_reset()
_write([
    _ok(1, "auto", "intl", ("k0", "主 Key"), 100, 80, 40, 0.5),
    _ok(3, "cmodel", "cn", ("k1", "国内 Key"), 200, 160, 60, 1.0),
    _err(4, "auto", "intl", ("k0", "主 Key")),
])
n1 = usagedb.sync()
check("首次 sync 导入全部整行", n1 == 3, n1)
check("重复 sync 幂等（offset 已前移，不再重复计数）", usagedb.sync() == 0)

st_all = usagedb.stats(_TODAY, _TODAY)
check("requests 计入失败行，tokens/credit 只算成功行",
      st_all["summary"]["requests"] == 3 and st_all["summary"]["failed"] == 1
      and st_all["summary"]["total_tokens"] == 300
      and abs(st_all["summary"]["credit"] - 1.5) < 1e-9,
      st_all["summary"])
check("缓存命中率 = cached / prompt（100/240 -> 41.7%）",
      st_all["summary"]["cache_hit_pct"] == 41.7,
      st_all["summary"]["cache_hit_pct"])

_append_text('{"at": %s, "model": "half"' % _NOW)         # 半行：无换行
check("半行停在原地不导入", usagedb.sync() == 0)
_append_text(', "total_tokens": 7, "realm": "intl"}\n')   # 补齐成完整行
check("补齐换行后下一轮导入该行", usagedb.sync() == 1)
st_half = usagedb.stats(_TODAY, _TODAY, realm="intl")
check("半行补齐后的行进入统计（half 出现在按模型列表，+7 tokens）",
      st_half["summary"]["requests"] == 3
      and st_half["summary"]["total_tokens"] == 107
      and "half" in [r["model"] for r in st_half["by_model"]],
      st_half["summary"])

print()
print("[2] 区域过滤 / 账号池反查 / 时间范围")
st_cn = usagedb.stats(_TODAY, _TODAY, realm="cn")
check("realm=cn 只统计国内行", st_cn["summary"]["requests"] == 1
      and st_cn["summary"]["total_tokens"] == 200, st_cn["summary"])
check("realm=intl 只统计国际行（含补齐的半行）",
      st_half["summary"]["requests"] == 3
      and st_half["summary"]["total_tokens"] == 107, st_half["summary"])


class _Acc(object):
    realm = "cn"


class _Pool(object):
    def __init__(self):
        self._acc = _Acc()

    def get(self, uid):
        return self._acc if uid == "acct-cn" else None


_orig_pool = runtime.POOL
runtime.POOL = _Pool()
try:
    _append_text(json.dumps({"at": _NOW, "model": "auto", "error": True,
                             "account": "acct-cn"}, ensure_ascii=False) + "\n")
    usagedb.sync()
    _reek = usagedb.stats(_TODAY, _TODAY, realm="cn")["summary"]["requests"]
finally:
    runtime.POOL = _orig_pool
check("行内没有 realm 时按账号池反查（失败行也能归到正确区域）", _reek == 2, _reek)

check("时间范围外没有计数",
      usagedb.stats(_DAY(-1), _DAY(-1))["summary"]["requests"] == 0)
try:
    usagedb.stats(_TODAY, _DAY(-1))
    check("from > to 抛 ValueError", False)
except ValueError:
    check("from > to 抛 ValueError", True)
try:
    usagedb.stats("2000-01-01", _TODAY)
    check("超长跨度被拒绝（MAX_SPAN_DAYS）", False)
except ValueError:
    check("超长跨度被拒绝（MAX_SPAN_DAYS）", True)

print()
print("[3] 分组：按模型 / 按密钥 / 趋势桶 / top-N 折叠 / 总数口径")
_reset()
_write([
    _ok(1, "auto", "intl", ("k0", "主 Key"), 100),
    _ok(2, "auto", "intl", ("k1", "副 Key"), 100),
    _ok(3, "cmodel", "intl", ("k0", "主 Key"), 300),
])
usagedb.sync()
st = usagedb.stats(_TODAY, _TODAY, realm="intl", group="key_id")
check("by_model 按 Token 降序",
      [r["model"] for r in st["by_model"]] == ["cmodel", "auto"],
      [r["model"] for r in st["by_model"]])
check("by_key 带 key_id/key_name，按 Token 降序",
      [(r["key_id"], r["key_name"], r["total_tokens"]) for r in st["by_key"]]
      == [("k0", "主 Key", 400), ("k1", "副 Key", 100)],
      st["by_key"])

hourly = usagedb.stats(_TODAY, _TODAY, realm="intl")["trend"]
check("单日趋势按小时、24 个桶、缺失补零",
      hourly["granularity"] == "hour" and len(hourly["labels"]) == 24
      and len(hourly["series"][0]["tokens"]) == 24, hourly["granularity"])
check("小时桶累加正确且总 token 守恒",
      sum(hourly["series"][0]["tokens"]) == 500,
      hourly["series"][0]["tokens"])

by_key_trend = usagedb.stats(_TODAY, _TODAY, realm="intl",
                             group="key_id")["trend"]
check("按密钥分组：序列 name 是 key_id、title 是 key_name",
      [(s["name"], s["title"]) for s in by_key_trend["series"]]
      == [("k0", "主 Key"), ("k1", "副 Key")],
      [(s["name"], s["title"]) for s in by_key_trend["series"]])
check("按密钥分组：各序列 token 总数与 by_key 一致",
      [sum(s["tokens"]) for s in by_key_trend["series"]] == [400, 100],
      [sum(s["tokens"]) for s in by_key_trend["series"]])

day_trend = usagedb.stats(_DAY(-4), _TODAY, realm="intl")["trend"]
check("跨度 > 2 天自动按天粒度、标签为日期",
      day_trend["granularity"] == "day" and len(day_trend["labels"]) == 5
      and day_trend["labels"][-1] == _TODAY, day_trend["labels"])
hour_trend = usagedb.stats(_DAY(-1), _TODAY, realm="intl",
                           granularity="hour")["trend"]
check("显式 granularity=hour 跨天时用「MM-DD HH:00」标签",
      len(hour_trend["labels"]) == 48
      and "-" in hour_trend["labels"][0]
      and hour_trend["labels"][0].endswith(":00"), hour_trend["labels"][:2])

# count_requests：/usage/recent 的分页总数（避免逐行扫描 JSONL），
# 口径必须与 summary.requests 一致（含失败行，见 [1] 的断言）
check("count_requests 与 summary.requests 同口径（按区域 / 全部）",
      usagedb.count_requests("intl") == 3 == st["summary"]["requests"]
      and usagedb.count_requests("cn") == 0
      and usagedb.count_requests("") == 3,
      (usagedb.count_requests("intl"), usagedb.count_requests("cn"),
       usagedb.count_requests("")))

_reset()
_write([_ok(1, "m%02d" % i, "intl", ("k0", "K"), 100 + i) for i in range(10)])
usagedb.sync()
_top = usagedb.stats(_TODAY, _TODAY, realm="intl", group="model")["trend"]
check("超过 TOP_SERIES 时折叠成「其他」且总数守恒",
      len(_top["series"]) == usagedb.TOP_SERIES
      and any(s["title"] == "其他" for s in _top["series"])
      and sum(sum(s["tokens"]) for s in _top["series"]) == sum(range(100, 110)),
      [_s["title"] for _s in _top["series"]])

print()
print("[4] 文件截断（轮转）与 rebuild")
_reset()
_write([_ok(1, model="a", tokens=10), _ok(2, model="b", tokens=20)])
usagedb.sync()
_before = usagedb.stats(_TODAY, _TODAY)["summary"]
_write([_ok(0, model="c", tokens=42)])            # 文件变短（模拟轮转/截断）
usagedb.sync()
_after = usagedb.stats(_TODAY, _TODAY)["summary"]
check("截断后 offset 夹回文件末尾：历史聚合保留、新内容不被当旧内容二次累加",
      _before["requests"] == 2 and _before["total_tokens"] == 30
      and _after["requests"] == 2 and _after["total_tokens"] == 30, _after)
_append_text(json.dumps(_ok(0, model="d", tokens=5), ensure_ascii=False) + "\n")
usagedb.sync()
_grown = usagedb.stats(_TODAY, _TODAY)["summary"]
check("夹回后的后续新行照常增量导入（+5 tokens）",
      _grown["requests"] == 3 and _grown["total_tokens"] == 35, _grown)
_rb = usagedb.rebuild()
check("rebuild 全量重放：只按当前文件内容重建（a/b 的历史随截断一起消失）",
      _rb["imported"] == 2
      and usagedb.stats(_TODAY, _TODAY)["summary"]["total_tokens"] == 47, _rb)
_status = usagedb.status()
check("status 暴露库路径 / offset / 聚合行数",
      _status["ok"] and _status["path"].endswith("usage.db")
      and _status["jsonl_offset"] == _status["jsonl_size"]
      and _status["hourly_rows"] >= 1, _status)

shutil.rmtree(_TMP, ignore_errors=True)
print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
