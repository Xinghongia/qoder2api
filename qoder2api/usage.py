"""qoder2api.usage —— 由 qoder_proxy.py 拆分而来。"""

import json
import os
import threading
import time
from collections import deque

from . import runtime
from . import paths
from pathlib import Path

from .security import _lock
from .realm import detect_model_realm
from .views import current_account
from .logbus import log


USAGE_FIELDS = ("prompt_tokens", "completion_tokens", "reasoning_tokens",
                "cached_tokens", "total_tokens", "credit")


def _empty_stats():
    return {"requests": 0, "errors": 0, "prompt_tokens": 0,
            "completion_tokens": 0, "reasoning_tokens": 0, "cached_tokens": 0,
            "total_tokens": 0, "credit": 0.0, "started": time.time(),
            "by_model": {},
            "ttft_ms_sum": 0, "ttft_samples": 0,
            "gen_ms_sum": 0, "gen_samples": 0,
            "wall_ms_sum": 0, "wall_samples": 0}


_usage = _empty_stats()


def _extract_usage(usage):
    """Normalize the upstream usage block into the fields we track."""
    if not usage:
        return {}
    details = usage.get("completion_tokens_details") or {}
    prompt_details = usage.get("prompt_tokens_details") or {}
    return {
        "prompt_tokens": usage.get("prompt_tokens") or 0,
        "completion_tokens": usage.get("completion_tokens") or 0,
        "reasoning_tokens": details.get("reasoning_tokens") or 0,
        "cached_tokens": usage.get("prompt_cache_hit_tokens")
        or details.get("cached_tokens") or prompt_details.get("cached_tokens") or 0,
        "total_tokens": usage.get("total_tokens") or 0,
        "credit": usage.get("credit") or 0,
    }


def row_matches_realm(row, realm):
    if not realm:
        return True
    r = row.get("realm")
    if r:
        return r == realm
    acct_uid = row.get("account")
    if acct_uid and runtime.POOL:
        acc = runtime.POOL.get(acct_uid)
        if acc:
            return acc.realm == realm
    model = row.get("model")
    if model:
        return detect_model_realm(model) == realm
    return realm == "cn"


def record_usage(model, usage, stream=None, elapsed_ms=None, ttft_ms=None,
                 gen_ms=None, fp=None, account=None):
    """Accumulate stats, append a JSONL row, and persist the summary."""
    fields = _extract_usage(usage)
    if not fields:
        return None
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "stream": bool(stream),
        "elapsed_ms": elapsed_ms,
        "ttft_ms": ttft_ms,
        "gen_ms": gen_ms,
    }
    row.update(fields)
    if fp:
        row.update(fp)
    if account:
        row["account"] = account
    acc = runtime.POOL.get(account) if (account and runtime.POOL) else None
    row["realm"] = acc.realm if acc else runtime.CURRENT_REALM
    if gen_ms and gen_ms > 0:
        row["tokens_per_sec"] = round(
            fields["completion_tokens"] / (gen_ms / 1000.0), 2)
    if fields["prompt_tokens"] > 0:
        row["cache_hit_pct"] = round(
            fields["cached_tokens"] * 100.0 / fields["prompt_tokens"], 1)
    with _lock:
        _usage["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                _usage[k] += fields[k]
        if ttft_ms is not None:
            _usage["ttft_ms_sum"] += ttft_ms
            _usage["ttft_samples"] += 1
        if gen_ms is not None:
            _usage["gen_ms_sum"] += gen_ms
            _usage["gen_samples"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
        per = _usage["by_model"].setdefault(
            model, {"requests": 0, **{k: 0 for k in USAGE_FIELDS}})
        per["requests"] += 1
        for k in USAGE_FIELDS:
            if k in fields:
                per[k] += fields[k]
        summary = json.loads(json.dumps(_usage))
    _persist_usage(row, summary, "usage persist failed")
    try:
        t_tokens = fields.get("total_tokens", 0)
        dur = " %dms" % elapsed_ms if elapsed_ms is not None else ""
        acc_tag = " acct=%s" % account[:8] if account else ""
        speed_tag = " %st/s" % row.get("tokens_per_sec", 0) \
            if row.get("tokens_per_sec") else ""
        log("chat done: model=%s%s%s tokens=%d (in=%d out=%d)%s"
            % (model, acc_tag, dur, t_tokens, fields.get("prompt_tokens", 0),
               fields.get("completion_tokens", 0), speed_tag), tag="chat")
    except Exception:
        pass
    return row


def _persist_usage(row, summary, fail_label):
    """Append one JSONL row and atomically rewrite the summary.

    The temp file carries a unique suffix: two threads writing the same
    "<summary>.tmp" race, and the loser's os.replace() fails with ENOENT
    because the winner already renamed the file away.
    """
    try:
        usage_dir = Path(runtime.USAGE_DIR).resolve()
        usage_dir.mkdir(parents=True, exist_ok=True)
        log_path = usage_dir / os.path.basename(runtime.USAGE_LOG)
        summary_path = usage_dir / os.path.basename(runtime.USAGE_SUMMARY)
        if not (log_path.is_relative_to(usage_dir)
                and summary_path.is_relative_to(usage_dir)):
            raise ValueError("usage path escapes base directory")
        with open(log_path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        tmp_summary = usage_dir / (
            os.path.basename(runtime.USAGE_SUMMARY)
            + ".%d.%d.tmp" % (os.getpid(), threading.get_ident()))
        if not tmp_summary.is_relative_to(usage_dir):
            raise ValueError("usage path escapes base directory")
        try:
            tmp_summary.write_text(json.dumps(summary, ensure_ascii=False,
                                              indent=2), encoding="utf-8")
            os.replace(tmp_summary, summary_path)
        except Exception:
            try:
                os.unlink(tmp_summary)
            except Exception:
                pass
            raise
    except Exception as exc:
        log("%s: %s" % (fail_label, exc))


def record_error(model, status, message, elapsed_ms=None):
    """Count a failed request and append it to the log so errors are visible."""
    row = {
        "at": time.time(),
        "iso": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "model": model,
        "error": True,
        "status": status,
        # 保留更完整的上游错误（provider_error 的内层 details 常在 200+ 字节）
        "message": str(message)[:400],
        "elapsed_ms": elapsed_ms,
    }
    with _lock:
        _usage["errors"] += 1
        if elapsed_ms is not None:
            _usage["wall_ms_sum"] += elapsed_ms
            _usage["wall_samples"] += 1
        summary = json.loads(json.dumps(_usage))
    _persist_usage(row, summary, "error persist failed")
    dur = " %dms" % elapsed_ms if elapsed_ms is not None else ""
    log("request error: model=%s%s status=%s msg=%s"
        % (model, dur, status, str(message)[:360]),
        level="ERROR", tag="chat")
    return row


def _pct(values, q):
    """Nearest-rank percentile (no interpolation) - good enough for latency."""
    if not values:
        return None
    ordered = sorted(values)
    idx = int(round((q / 100.0) * (len(ordered) - 1)))
    return ordered[max(0, min(len(ordered) - 1, idx))]


_perf_cache = {}
_perf_lock = threading.Lock()


def perf_stats(sample=5000, realm=None, ttl=10):
    """Cached wrapper: parsing thousands of rows is CPU-heavy, and the
    dashboard polls this endpoint every few seconds."""
    r = realm or runtime.CURRENT_REALM
    try:
        key = (int(sample), r)
    except Exception:
        key = (5000, r)
    now = time.time()
    with _perf_lock:
        hit = _perf_cache.get(key)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _perf_stats_uncached(sample, realm)
    with _perf_lock:
        _perf_cache[key] = (time.time(), data)
    return data


def _perf_stats_uncached(sample=5000, realm=None):
    """Latency percentiles + derived rates, computed from the JSONL log."""
    ttfts, gens, walls, rates, hits, tok_rates = [], [], [], [], [], []
    total = ok = err = 0
    m_buckets = {}
    # 只读日志末尾 sample 行：readlines() 会把整个日志读成字符串列表。
    rows = [raw.decode("utf-8", "replace") for raw in _tail_lines(runtime.USAGE_LOG, sample)]
    for line in rows:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if realm and not row_matches_realm(r, realm):
            continue
        total += 1
        if r.get("error"):
            err += 1
            if r.get("elapsed_ms"):
                walls.append(r["elapsed_ms"])
            continue
        ok += 1
        if r.get("ttft_ms") is not None:
            ttfts.append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            gens.append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            walls.append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            tok_rates.append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            hits.append(r["cache_hit_pct"])
        m_id = r.get("model") or "unknown"
        mb = m_buckets.setdefault(m_id, {"total": 0, "ok": 0, "err": 0,
                                         "ttfts": [], "gens": [], "walls": [],
                                         "tok_rates": [], "hits": []})
        mb["total"] += 1
        if r.get("error"):
            mb["err"] += 1
        else:
            mb["ok"] += 1
        if r.get("ttft_ms") is not None:
            mb["ttfts"].append(r["ttft_ms"])
        if r.get("gen_ms") is not None:
            mb["gens"].append(r["gen_ms"])
        if r.get("elapsed_ms") is not None:
            mb["walls"].append(r["elapsed_ms"])
        if r.get("tokens_per_sec"):
            mb["tok_rates"].append(r["tokens_per_sec"])
        if r.get("cache_hit_pct") is not None:
            mb["hits"].append(r["cache_hit_pct"])

    def block(vals):
        if not vals:
            return None
        return {
            "avg": round(sum(vals) / len(vals), 1),
            "p50": _pct(vals, 50),
            "p90": _pct(vals, 90),
            "p99": _pct(vals, 99),
            "max": max(vals),
            "samples": len(vals),
        }

    return {
        "sampled": total,
        "success": ok,
        "errors": err,
        "success_rate_pct": round(ok * 100.0 / total, 1) if total else None,
        "ttft_ms": block(ttfts),
        "generation_ms": block(gens),
        "wall_ms": block(walls),
        "tokens_per_sec": block(tok_rates),
        "cache_hit_pct": block(hits),
        "by_model": {
            mid: {
                "requests": mb["total"],
                "errors": mb["err"],
                "success_rate_pct": round(mb["ok"] * 100.0 / mb["total"], 1)
                if mb["total"] else None,
                "ttft_ms": block(mb["ttfts"]),
                "generation_ms": block(mb["gens"]),
                "wall_ms": block(mb["walls"]),
                "tokens_per_sec": block(mb["tok_rates"]),
                "cache_hit_pct": block(mb["hits"]),
            } for mid, mb in m_buckets.items()
        },
    }


_snap_cache = {}
_snap_lock = threading.Lock()


def usage_snapshot(realm=None, ttl=10):
    """Cached wrapper: the dashboard polls this every few seconds."""
    r = realm or runtime.CURRENT_REALM
    now = time.time()
    with _snap_lock:
        hit = _snap_cache.get(r)
        if hit is not None and (now - hit[0]) < ttl:
            return hit[1]
    data = _usage_snapshot_uncached(r)
    with _snap_lock:
        _snap_cache[r] = (time.time(), data)
    return data


def _usage_snapshot_uncached(realm=None):
    r = realm or runtime.CURRENT_REALM
    rep = runtime.POOL.representative(realm=r) if runtime.POOL else current_account()
    snap = _empty_stats()
    snap["started"] = _usage.get("started", time.time())
    try:
        with open(runtime.USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if r and not row_matches_realm(row, r):
                    continue
                if row.get("error"):
                    snap["errors"] += 1
                else:
                    snap["requests"] += 1
                    for k in USAGE_FIELDS:
                        if k in row:
                            snap[k] += (row[k] or 0)
                    m = row.get("model") or "unknown"
                    per = snap["by_model"].setdefault(
                        m, {"requests": 0, "accounts": {},
                            **{k: 0 for k in USAGE_FIELDS}})
                    per["requests"] += 1
                    for k in USAGE_FIELDS:
                        if k in row:
                            per[k] += (row[k] or 0)
                    acct_id = row.get("account")
                    if acct_id:
                        per.setdefault("accounts", {})
                        per["accounts"][acct_id] = per["accounts"].get(acct_id, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage snapshot read failed: %s" % exc)
    snap["since"] = time.strftime("%Y-%m-%d %H:%M:%S",
                                  time.localtime(snap.get("started", time.time())))
    snap["log_file"] = runtime.USAGE_LOG
    snap["realm"] = r
    snap["accounts_map"] = {a.uid: {"nickname": a.nickname, "realm": a.realm}
                            for a in runtime.POOL.accounts} if runtime.POOL else {}
    snap["account"] = {
        "uid": (rep.uid if rep else ""),
        "domain": (rep.domain if rep else ""),
        "issuer": ("qoder" if rep else ""),
        "credential_file": (os.path.basename(rep.path) if rep and rep.path else ""),
        "expires_at": (rep.expires_at if rep else 0),
        "accounts": (len(runtime.POOL.accounts) if runtime.POOL else 0),
        "accounts_ready": (runtime.POOL.count_ready() if runtime.POOL else 0),
    }
    return snap


def _tail_lines(path, max_lines, chunk=256 * 1024):
    """Return up to the last `max_lines` non-empty lines, oldest first.

    The usage log passes 20MB within a day. Scanning it end to end on every
    dashboard poll was the dominant cost behind slow /usage/* responses.
    """
    lines = []
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(lines) < max_lines:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    if not raw.strip():
                        continue
                    lines.append(raw)
                    if len(lines) >= max_lines:
                        break
            if len(lines) < max_lines and buf.strip():
                lines.append(buf)
    except FileNotFoundError:
        return []
    except Exception as exc:
        log("tail read failed: %s" % exc)
        return []
    lines.reverse()
    return lines


def count_usage_rows(realm=None):
    """Cheap row count - substring match instead of a full JSON parse."""
    needles = ()
    if realm:
        needles = ('"realm": "%s"' % realm, '"realm":"%s"' % realm)
    n = 0
    try:
        with open(runtime.USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                if not needles:
                    n += 1
                    continue
                if any(x in line for x in needles):
                    n += 1
                    continue
                if '"realm"' in line:
                    continue
                try:
                    if row_matches_realm(json.loads(line), realm):
                        n += 1
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception:
        pass
    return n


def recent_usage(limit=100, realm=None, page=1):
    """Paginated rows from the tail of the log (page 1 is latest)."""
    try:
        limit = max(1, int(limit))
    except Exception:
        limit = 100
    try:
        page = max(1, int(page))
    except Exception:
        page = 1
    total = count_usage_rows(realm)
    total_pages = max(1, (total + limit - 1) // limit) if total > 0 else 1
    page = min(page, total_pages)
    target_count = page * limit
    matching = []
    chunk = 256 * 1024
    try:
        with open(runtime.USAGE_LOG, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            pos = fh.tell()
            buf = b""
            while pos > 0 and len(matching) < target_count:
                step = min(chunk, pos)
                pos -= step
                fh.seek(pos)
                buf = fh.read(step) + buf
                parts = buf.split(b"\n")
                buf = parts[0]
                for raw in reversed(parts[1:]):
                    st = raw.strip()
                    if not st:
                        continue
                    try:
                        item = json.loads(st.decode("utf-8", "replace"))
                    except Exception:
                        continue
                    if realm and not row_matches_realm(item, realm):
                        continue
                    matching.append(item)
                    if len(matching) >= target_count:
                        break
            if len(matching) < target_count and buf.strip():
                try:
                    item = json.loads(buf.strip().decode("utf-8", "replace"))
                    if not realm or row_matches_realm(item, realm):
                        matching.append(item)
                except Exception:
                    pass
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("recent_usage read failed: %s" % exc)
    start_idx = (page - 1) * limit
    end_idx = start_idx + limit
    page_rows = matching[start_idx:end_idx]
    return {
        "total": total,
        "page": page,
        "limit": limit,
        "total_pages": total_pages,
        "rows": page_rows,
    }


_byacct_cache = {"at": 0.0, "data": None}
_byacct_lock = threading.Lock()


def usage_by_account(ttl=10):
    """Cached wrapper: full aggregation over the whole log is expensive."""
    now = time.time()
    with _byacct_lock:
        if _byacct_cache["data"] is not None and (now - _byacct_cache["at"]) < ttl:
            return _byacct_cache["data"]
    data = _usage_by_account_uncached()
    with _byacct_lock:
        _byacct_cache["at"] = time.time()
        _byacct_cache["data"] = data
    return data


def _usage_by_account_uncached():
    """Aggregate the JSONL log per account id."""
    buckets = {}
    try:
        with open(runtime.USAGE_LOG, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if row.get("error"):
                    continue
                key = row.get("account") or "(unattributed)"
                bucket = buckets.setdefault(key, {
                    "account": key, "requests": 0, "prompt_tokens": 0,
                    "completion_tokens": 0, "reasoning_tokens": 0,
                    "cached_tokens": 0, "total_tokens": 0, "models": {},
                })
                bucket["requests"] += 1
                for field in ("prompt_tokens", "completion_tokens",
                              "reasoning_tokens", "cached_tokens",
                              "total_tokens"):
                    bucket[field] += row.get(field) or 0
                model = row.get("model") or "?"
                bucket["models"][model] = bucket["models"].get(model, 0) + 1
    except FileNotFoundError:
        pass
    except Exception as exc:
        log("usage_by_account failed: %s" % exc)
    out = sorted(buckets.values(), key=lambda b: -b["total_tokens"])
    for item in out:
        item["models"] = sorted(item["models"].items(), key=lambda kv: -kv[1])[:5]
    return out


def compute_usage_analytics():
    """Detailed analytics for Token, Cache, and Reasoning metrics page."""
    now = time.localtime()
    today_ts = time.mktime((now.tm_year, now.tm_mon, now.tm_mday, 0, 0, 0, 0, 0, -1))

    def new_stat():
        return {
            "requests": 0, "errors": 0,
            "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "cached_tokens": 0, "total_tokens": 0,
            "ttft_sum": 0.0, "ttft_n": 0,
            "speed_sum": 0.0, "speed_n": 0,
            "elapsed_sum": 0.0, "elapsed_n": 0,
        }

    all_summary = new_stat()
    today_summary = new_stat()
    acct_map = {}
    model_map = {}
    if os.path.exists(runtime.USAGE_LOG):
        try:
            with open(runtime.USAGE_LOG, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        r = json.loads(line)
                    except Exception:
                        continue
                    is_err = bool(r.get("error"))
                    at = r.get("at", 0)
                    is_today = (at >= today_ts)
                    acct_uid = r.get("account") or "(unattributed)"
                    m_id = r.get("model") or "(unknown)"

                    def feed(stat_obj, is_error):
                        if is_error:
                            stat_obj["errors"] += 1
                        else:
                            stat_obj["requests"] += 1
                            stat_obj["prompt_tokens"] += (r.get("prompt_tokens") or 0)
                            stat_obj["completion_tokens"] += (r.get("completion_tokens") or 0)
                            stat_obj["reasoning_tokens"] += (r.get("reasoning_tokens") or 0)
                            stat_obj["cached_tokens"] += (r.get("cached_tokens") or 0)
                            stat_obj["total_tokens"] += (r.get("total_tokens") or 0)
                            if r.get("ttft_ms"):
                                stat_obj["ttft_sum"] += r["ttft_ms"]
                                stat_obj["ttft_n"] += 1
                            if r.get("tokens_per_sec"):
                                stat_obj["speed_sum"] += r["tokens_per_sec"]
                                stat_obj["speed_n"] += 1
                            if r.get("elapsed_ms"):
                                stat_obj["elapsed_sum"] += r["elapsed_ms"]
                                stat_obj["elapsed_n"] += 1

                    feed(all_summary, is_err)
                    if is_today:
                        feed(today_summary, is_err)
                    if acct_uid not in acct_map:
                        acct_map[acct_uid] = {
                            "uid": acct_uid,
                            "nickname": acct_uid,
                            "realm": r.get("realm", ""),
                            "domain": "",
                            "today": new_stat(),
                            "all_time": new_stat(),
                            "today_models": {},
                            "all_models": {},
                        }
                    feed(acct_map[acct_uid]["all_time"], is_err)
                    if is_today:
                        feed(acct_map[acct_uid]["today"], is_err)
                    if not is_err:
                        tm = acct_map[acct_uid]["all_models"].setdefault(
                            m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                        tm["requests"] += 1
                        tm["tokens"] += (r.get("total_tokens") or 0)
                        tm["reasoning"] += (r.get("reasoning_tokens") or 0)
                        if is_today:
                            tdm = acct_map[acct_uid]["today_models"].setdefault(
                                m_id, {"requests": 0, "tokens": 0, "reasoning": 0})
                            tdm["requests"] += 1
                            tdm["tokens"] += (r.get("total_tokens") or 0)
                            tdm["reasoning"] += (r.get("reasoning_tokens") or 0)
                    if m_id not in model_map:
                        model_map[m_id] = {"model": m_id, "today": new_stat(),
                                           "all_time": new_stat()}
                    feed(model_map[m_id]["all_time"], is_err)
                    if is_today:
                        feed(model_map[m_id]["today"], is_err)
        except Exception as exc:
            log("compute_usage_analytics failed: %s" % exc)
    if runtime.POOL:
        for a in runtime.POOL.accounts:
            if a.uid in acct_map:
                acct_map[a.uid]["nickname"] = a.nickname
                acct_map[a.uid]["realm"] = a.realm
                acct_map[a.uid]["domain"] = a.domain
                acct_map[a.uid]["credits"] = getattr(a, "credits", None) or {}
            else:
                acct_map[a.uid] = {
                    "uid": a.uid,
                    "nickname": a.nickname,
                    "realm": a.realm,
                    "domain": a.domain,
                    "credits": getattr(a, "credits", None) or {},
                    "today": new_stat(),
                    "all_time": new_stat(),
                    "today_models": {},
                    "all_models": {},
                }

    def finalize(stat_obj):
        p = stat_obj["prompt_tokens"]
        c = stat_obj["cached_tokens"]
        out = stat_obj["completion_tokens"]
        reas = stat_obj["reasoning_tokens"]
        stat_obj["cache_hit_pct"] = round((c / p * 100), 1) if p > 0 else 0.0
        stat_obj["reasoning_ratio"] = round((reas / out * 100), 1) if out > 0 else 0.0
        stat_obj["ttft_ms_avg"] = round(stat_obj["ttft_sum"] / stat_obj["ttft_n"]) \
            if stat_obj["ttft_n"] > 0 else 0
        stat_obj["speed_avg"] = round(stat_obj["speed_sum"] / stat_obj["speed_n"], 1) \
            if stat_obj["speed_n"] > 0 else 0.0
        stat_obj["elapsed_ms_avg"] = round(stat_obj["elapsed_sum"] / stat_obj["elapsed_n"]) \
            if stat_obj["elapsed_n"] > 0 else 0
        return stat_obj

    finalize(all_summary)
    finalize(today_summary)
    for a in acct_map.values():
        finalize(a["today"])
        finalize(a["all_time"])
    for m in model_map.values():
        finalize(m["today"])
        finalize(m["all_time"])
    accts_list = sorted(acct_map.values(),
                        key=lambda a: (-a["today"]["total_tokens"],
                                       -a["all_time"]["total_tokens"]))
    models_list = sorted(model_map.values(),
                         key=lambda m: (-m["today"]["total_tokens"],
                                        -m["all_time"]["total_tokens"]))
    return {
        "today_ts": today_ts,
        "summary": {"today": today_summary, "all_time": all_summary},
        "accounts": accts_list,
        "models": models_list,
    }
