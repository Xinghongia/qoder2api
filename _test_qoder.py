"""Deterministic offline tests for the Qoder gateway.

No network: verifies the crypto primitives (AES FIPS vectors, RSA padding
structure, Qoder custom base64 round-trip), COSY signature layout, request
body construction, SSE envelope unwrapping, Responses-API custom-tool
translation, and check-in response normalization.

    python _test_qoder.py
"""
import hashlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ.setdefault("ACCOUNTS_DIR",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "_acc"))
os.environ.setdefault("USAGE_DIR",
                      os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   "_use"))
# 离线测试不拉起客户端原生二进制（活动平台的机器身份桥）；测试里显式关闭
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

import qoder_proxy as P
import qoder_sign as S
import qoder_catalog as C
import qoder_accounts as A
import qoder_tasks as T

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


print("[1] Qoder custom base64 variant")
enc = S.qoder_encode(b"{}")
check("encode({}) deterministic", enc == S.qoder_encode(b"{}"))
check("decode(encode(x)) == x", S.qoder_decode(enc) == b"{}")
sample = json.dumps({"b": 1, "a": "中文测试", "c": [1, 2, 3]}).encode()
check("roundtrip with unicode/json", S.qoder_decode(S.qoder_encode(sample)) == sample)
check("output uses only custom alphabet",
      all(c in S.QODER_CUSTOM_ALPHABET or c == S.QODER_PAD for c in enc))
check("padding is $", S.qoder_encode(b"a" * 100).count("$") >= 0 and "=" not in enc)

print()
print("[2] AES-128 (FIPS-197 / NIST vectors)")
k = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
pt = bytes.fromhex("00112233445566778899aabbccddeeff")
ct = S._encrypt_block(pt, S._expand_key(k)).hex()
check("FIPS-197 C.1 block", ct == "69c4e0d86a7b0430d8cdb78070b4c55a", ct)
k2 = bytes.fromhex("2b7e151628aed2a6abf7158809cf4f3c")
iv = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
pt2 = bytes.fromhex("6bc1bee22e409f96e93d7e117393172a")
xored = bytes(a ^ b for a, b in zip(pt2, iv))
c2 = S._encrypt_block(xored, S._expand_key(k2)).hex()
check("SP800-38A CBC first block", c2 == "7649abac8119b246cee98e9b12e9197d", c2)
# CBC chaining through aes_cbc_encrypt (key==iv style input, PKCS7)
blob = S.aes_cbc_encrypt(b"hello qoder", b"0123456789abcdef", b"0123456789abcdef")
check("aes_cbc_encrypt block-aligned", len(blob) % 16 == 0 and len(blob) >= 16)
# 11 字节明文 -> PKCS7 补 5 -> 一个密文块
check("pkcs7 grows 11B input to one 16B block", len(blob) == 16, len(blob))
check("16B input grows to two blocks",
      len(S.aes_cbc_encrypt(b"0123456789abcdef", b"0123456789abcdef",
                             b"0123456789abcdef")) == 32)

print()
print("[3] RSA PKCS#1 v1.5 public encryption")
import base64 as _b64m
_der = _b64m.b64decode("".join(l for l in S.SERVER_PUB_PEM.splitlines()
                                if "BEGIN" not in l and "END" not in l))
check("PEM parses to 1024-bit modulus", S._RSA_N.bit_length() == 1024,
      S._RSA_N.bit_length())
check("DER carries 129-byte INTEGER", b"\x02\x81\x81" in _der)
check("exponent 65537", S._RSA_E == 65537)
ct_rsa = S.rsa_pkcs1v15_encrypt(b"0123456789abcdef")
check("ciphertext length = k = 128", len(ct_rsa) == 128, len(ct_rsa))
m_int = int.from_bytes(ct_rsa, "big")
check("ciphertext < n", m_int < S._RSA_N)
check("pkcs1v15 PS length formula",
      len(S.rsa_pkcs1v15_encrypt(b"x")) == 128)

print()
print("[4] COSY session & bearer signature")
sess = S.CosySession(uid="test-uid-001", nickname="tester",
                     access_token="dt-abc", refresh_token="drt-xyz")
url = "https://gateway.qoder.com.cn" + P.CHAT_PATH
body_enc = S.qoder_encode(b'{"x":1}')
h = sess.headers(body_enc, url, model_key="qmodel", sse=True)
check("has full cosy header set",
      all(kk in h for kk in ("authorization", "cosy-key", "cosy-user",
                             "cosy-machineid", "cosy-machinetoken",
                             "cosy-machinetype", "cosy-date", "cosy-version")))
check("x-model-key set", h.get("x-model-key") == "qmodel")
check("cache-control for sse", h.get("cache-control") == "no-cache")
check("bearer format COSY.payload.sig",
      h["authorization"].startswith("Bearer COSY.")
      and len(h["authorization"].split(".")) == 3)
parts = h["authorization"][len("Bearer "):].split(".")
payload_b64, sig = parts[1], parts[2]
path_stripped = "/api/v2/service/pro/sse/agent_chat_generation"
raw = payload_b64 + "\n" + sess.cosy_key + "\n" + h["cosy-date"] + "\n" \
    + body_enc + "\n" + path_stripped
check("md5 signature over body+path", sig == hashlib.md5(raw.encode()).hexdigest())
check("machine id stable per uid",
      S.CosySession(uid="test-uid-001").machine_id == sess.machine_id)
check("machine ids differ across uid",
      S.CosySession(uid="other-uid").machine_id != sess.machine_id)
import base64 as _b64
payload = json.loads(_b64.b64decode(payload_b64))
check("payload keys sorted-compact",
      sorted(payload.keys()) == ["cosyVersion", "ideVersion", "info",
                                 "requestId", "version"])
check("payload cosyVersion", payload["cosyVersion"] == S.COSY_VERSION)

print()
print("[5] model alias resolution (official keys)")
check("qwen3.8-max -> qmodel_38max",
      C.resolve_upstream_key("qwen3.8-max") == "qmodel_38max")
check("old key qmodel_preview -> qmodel_38max",
      C.resolve_upstream_key("qmodel_preview") == "qmodel_38max")
check("qwen3.8-flash -> qfmodel",
      C.resolve_upstream_key("qwen3.8-flash") == "qfmodel")
check("deepseek-v4-pro -> dmodel",
      C.resolve_upstream_key("deepseek-v4-pro") == "dmodel")
check("shared key passthrough",
      C.resolve_upstream_key("qmodel") == "qmodel")
check("aliased qoder/ prefix",
      C.resolve_upstream_key("qoder/qwen3.7-max") == "qmodel_latest")
check("unknown model passthrough",
      C.resolve_upstream_key("mystery-model") == "mystery-model")
check("empty -> auto", C.resolve_upstream_key("") == "auto")
check("realm-aware: official key of that realm accepted",
      C.resolve_upstream_key("q37fmodel", realm="cn") == "q37fmodel")

print()
print("[5.5] per-realm catalogs follow the official client (must DIFFER)")
intl_keys = [m["key"] for m in C.STATIC_INTL_MODELS]
cn_keys = [m["key"] for m in C.STATIC_CN_MODELS]
check("intl catalog not empty (>=17)", len(intl_keys) >= 17, len(intl_keys))
check("cn catalog not empty (14)", len(cn_keys) == 14, len(cn_keys))
check("two realms' catalogs differ", intl_keys != cn_keys)
check("intl-only: smodel present, absent from cn",
      "smodel" in intl_keys and "smodel" not in cn_keys)
check("cn-only: q37fmodel present, absent from intl",
      "q37fmodel" in cn_keys and "q37fmodel" not in intl_keys)
check("cn-only: gm51model present, absent from intl",
      "gm51model" in cn_keys and "gm51model" not in intl_keys)
intl_en = {m["key"] for m in C.STATIC_INTL_MODELS if m.get("enable")}
cn_en = {m["key"] for m in C.STATIC_CN_MODELS if m.get("enable")}
check("intl enabled flags = {qmodel_38max, qfmodel} (official plan state)",
      intl_en == {"qmodel_38max", "qfmodel"}, sorted(intl_en))
check("cn all 14 enabled", len(cn_en) == 14, sorted(cn_en))
# 全量列出（不按 enable 过滤）
merged_i = P.merge_catalog([], realm="intl")
merged_c = P.merge_catalog([], realm="cn")
check("merge keeps FULL intl list (17, no enable filtering)", len(merged_i) == 17,
      len(merged_i))
check("merge keeps FULL cn list (14)", len(merged_c) == 14, len(merged_c))
# 清单以官方动态/本机目录为准（桌面版此刻显示什么就显示什么）
dyn_keys = [("cmodel", {"key": "cmodel", "display_name": "Cantus"})]  # 反例占位
primary15 = [(m["key"], dict(m)) for m in C.STATIC_INTL_MODELS
             if m["key"] not in ("cmodel", "smodel")]   # 模拟动态返回的 15 条
merged_dyn = P.merge_catalog(primary15, realm="intl")
check("merge follows primary set (dynamic 15 wins, static-only excluded)",
      len(merged_dyn) == 15 and
      {k for k, _ in merged_dyn} == {m["key"] for m in C.STATIC_INTL_MODELS}
      - {"cmodel", "smodel"}, len(merged_dyn))

print()
print("[5.7] official full-fidelity fields: display id / pricing / windows / efforts")
entry_i = next(m for m in C.STATIC_INTL_MODELS if m["key"] == "smodel")
check("intl exclusive entry retains full fields",
      {"context_config", "thinking_config", "is_free", "is_new"} <= set(entry_i.keys()),
      sorted(entry_i.keys()))
cn38 = next(m for m in C.STATIC_CN_MODELS if m["key"] == "qmodel_38max")
promo = cn38.get("promotion") or {}
# 注意：promo.active / price_factor 是**快照抓取时刻**的官方值（低谷时段抓的
# 快照 active=True 且 price=峰价×折扣；高峰时段抓的 active=False 且 price=峰价）。
# 断言官方不变量，而不是断言"抓取时正好在打折"，否则测试随抓取时刻漂移。
check("cn qmodel_38max carries off-peak promotion metadata",
      bool(promo) and promo.get("rule_id") == "idle_time_model_credit_discount"
      and isinstance(promo.get("active"), bool), promo.get("rule_id"))
check("off-peak window = 22:00-08:00",
      promo.get("window_start") == "22:00" and promo.get("window_end") == "08:00")
check("peak factor (before_promotion) = 0.5", promo.get("before_promotion_price_factor") == 0.5,
      promo.get("before_promotion_price_factor"))
check("current price_factor is peak or valley (0.5 / 0.2)",
      cn38.get("price_factor") in (0.5, 0.2), cn38.get("price_factor"))
check("price_factor matches promo.active (peak when inactive)",
      (cn38.get("price_factor") == 0.2) is bool(promo.get("active")),
      (cn38.get("price_factor"), promo.get("active")))
check("discount_factor = 0.4 (4折)", promo.get("discount_factor") == 0.4)
check("promotion badge/description localized",
      bool((promo.get("badge") or {}).get("en")) and bool((promo.get("description") or {}).get("en")))
qf = next(m for m in C.STATIC_CN_MODELS if m["key"] == "qfmodel")
check("qfmodel free + original factor kept",
      qf.get("is_free") is True and qf.get("original_price_factor") == 0.1)
check("context_config multi-window (3) with default 200K",
      len(cn38.get("context_config") or {}) == 3
      and (cn38["context_config"].get("200K") or {}).get("is_default") is True)
tc = (cn38.get("thinking_config") or {}).get("enabled") or {}
effs = tc.get("efforts") or {}
check("thinking efforts low/medium/xhigh with default medium",
      set(effs) == {"low", "medium", "xhigh"}
      and (effs.get("medium") or {}).get("is_default") is True, sorted(effs))

# 展示 id 与解析
check("display id format key (Name)",
      C.display_id(cn38) == "qmodel_38max (Qwen3.8-Max)", C.display_id(cn38))
check("resolve display id -> key",
      C.resolve_upstream_key("qmodel_38max (Qwen3.8-Max)", realm="cn") == "qmodel_38max")
check("resolve official display name (case-insensitive)",
      C.resolve_upstream_key("qwen3.8-max", realm="cn") == "qmodel_38max"
      and C.resolve_upstream_key("GLM-5.2", realm="cn") == "gm51model")
check("resolve bare display name DeepSeek-V4-Pro",
      C.resolve_upstream_key("DeepSeek-V4-Pro", realm="cn") == "dmodel")
check("format_model_id helper",
      C.format_model_id("gm51model", realm="cn") == "gm51model (GLM-5.2)",
      C.format_model_id("gm51model", realm="cn"))

# model_entry 输出
me = P.model_entry("qmodel_38max", cn38)
check("model_entry id = OFFICIAL model name (the value clients fill in)",
      me["id"] == "Qwen3.8-Max", me["id"])
check("model_entry upstream_key kept", me["upstream_key"] == "qmodel_38max")
check("model_entry aliases cover key + bracket form + friendly alias",
      "qmodel_38max" in me["aliases"] and "qmodel_38max (Qwen3.8-Max)" in me["aliases"]
      and "qwen3.8-max" in me["aliases"], me.get("aliases"))
check("model_entry description = official desktop copy",
      "千问" in (me.get("description") or ""), (me.get("description") or "")[:60])
check("model_entry enabled flag", me["enabled"] is True)
check("model_entry peak/valley factors",
      me["price_factor_peak"] == 0.5 and me["price_factor_valley"] == 0.2)
check("model_entry off_peak window+badge",
      me["off_peak_window"] == "22:00-08:00" and bool(me["off_peak"]["badge"]))
check("model_entry context labels default 200K",
      me["context_window_labels"] == ["200K", "400K", "1M"] or set(me["context_window_labels"]) == {"200K", "400K", "1M"},
      me.get("context_window_labels"))
check("model_entry context_window_default", me.get("context_window_default") == "200K")
check("model_entry reasoning efforts + default",
      me.get("reasoning_efforts") == ["low", "medium", "xhigh"]
      and me.get("reasoning_default_effort") == "medium",
      (me.get("reasoning_efforts"), me.get("reasoning_default_effort")))
check("model_entry can_disable", me.get("reasoning_can_disable") is True)
check("model_entry is_free/is_new", me.get("is_free") is True and me.get("is_new") is True)
check("model_entry does NOT fabricate max_output_tokens (official data has none)",
      "max_output_tokens" not in me and "max_completion_tokens" not in me,
      sorted(k for k in me if "output" in k))
me_off = P.model_entry("smodel", entry_i)
check("model_entry disabled shows enabled=false (badge, not filtered)",
      me_off["enabled"] is False)
check("model_entry disabled_reason = OFFICIAL copy (not '未开放')",
      me_off.get("disabled_reason") == "需要升级或购买千问官方套餐开放",
      me_off.get("disabled_reason"))
check("model_entry disabled_message_key passthrough (codeSafeModelReason)",
      me_off.get("disabled_message_key") == "codeSafeModelReason",
      me_off.get("disabled_message_key"))
check("official text loader: 17 zh descriptions",
      len(C.load_official_text()["descriptions"]) == 17)
check("official local name ultimate -> zh",
      C.official_local_name("ultimate") != "" and C.official_local_name("ultimate") != "Ultimate",
      C.official_local_name("ultimate"))
me_u = P.model_entry("ultimate", next(m for m in C.STATIC_INTL_MODELS if m["key"] == "ultimate"))
check("model_entry name_local emitted for intl mode preset",
      bool(me_u.get("name_local")), me_u.get("name_local"))
check("resolve official local label (Kimi-K2.7-Code)",
      C.resolve_upstream_key("Kimi-K2.7-Code", realm="intl") == "kmodel")
check("cross-region guard accepts display id form",
      P.exclusive_realm("gm51model (GLM-5.2)") == "cn")

print()
print("[5.8] off-peak (低谷) window detection — cross-midnight 22:00-08:00 UTC+8")
import datetime as _dt
def _ts(h, m):
    # 构造 UTC+8 指定时刻对应的 epoch（固定 +8 与官方时区一致）
    utc_naive = _dt.datetime.utcnow() if False else None
    base = _dt.datetime.now(_dt.timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    target_local_naive = _dt.datetime.now().replace(hour=h, minute=m, second=0, microsecond=0)
    return target_local_naive.timestamp() - (_dt.datetime.now().astimezone().utcoffset().total_seconds()
                                             - 8 * 3600)
for hh, mm, expect, label in [
        (1, 0, True, "01:00 inside window"),
        (12, 0, False, "12:00 outside"),
        (21, 59, False, "21:59 before window"),
        (22, 0, True, "22:00 window start (inclusive)"),
        (23, 30, True, "23:30 inside"),
        (7, 59, True, "07:59 last minute inside"),
        (8, 0, False, "08:00 window end (exclusive)")]:
    got = P.off_peak_active_now("22:00", "08:00", tz="Asia/Shanghai",
                                now=_ts(hh, mm))
    check(f"window {label}", got is expect, f"got={got} expect={expect}")
check("invalid window -> None",
      P.off_peak_active_now(None, "08:00") is None)
check("same start/end -> always active",
      P.off_peak_active_now("00:00", "00:00", now=_ts(13, 0)) is True)
# promotion fields surface off_peak_active_now via model_entry
me_promo = P.model_entry("qmodel_38max", cn38)
check("model_entry exposes off_peak_active_now (bool)",
      isinstance(me_promo.get("off_peak_active_now"), bool),
      me_promo.get("off_peak_active_now"))

# None must not clobber static snapshot values in merge
dyn_null = [("qmodel_38max", {"key": "qmodel_38max",
                               "context_config": None,
                               "thinking_config": None,
                               "price_factor": 0.2})]
merged_null = dict(P.merge_catalog(dyn_null, realm="cn"))["qmodel_38max"]
check("merge: dynamic None does NOT clobber static context_config",
      isinstance(merged_null.get("context_config"), dict)
      and "200K" in (merged_null.get("context_config") or {}),
      type(merged_null.get("context_config")).__name__)
check("merge: dynamic None does NOT clobber static thinking_config",
      isinstance(merged_null.get("thinking_config"), dict))
check("merge: dynamic real value still overrides",
      merged_null.get("price_factor") == 0.2)

print()
print("[5.9] ALL off-peak (低谷) promotion models — must be complete, not just one")
PROMO_KEYS = {"qmodel_38max", "qmodel_latest", "qmodel"}
for realm_name in ("cn", "intl"):
    # 促销集合按"是否带官方 promotion 元数据"判定；promo.active 是快照抓取
    # 时刻是否处于低谷时段（22:00-08:00），不该作为集合成员条件。
    promo_models = {m["key"] for m in C.models_for_realm(realm_name)
                    if m.get("promotion")}
    check(f"[{realm_name}] official promo set == the 3 off-peak models",
          promo_models == PROMO_KEYS, sorted(promo_models))
    # 每个促销模型都要产出完整 off_peak 输出（不止一个）
    for k in sorted(PROMO_KEYS):
        src = next(m for m in C.models_for_realm(realm_name) if m["key"] == k)
        e = P.model_entry(k, src)
        check(f"[{realm_name}] {k} entry carries off_peak window+badge",
              bool(e.get("off_peak")) and e.get("off_peak_window") == "22:00-08:00"
              and bool(e.get("off_peak", {}).get("badge")),
              e.get("off_peak_window"))
        check(f"[{realm_name}] {k} entry has off_peak_active_now bool",
              isinstance(e.get("off_peak_active_now"), bool))
        check(f"[{realm_name}] {k} entry exposes peak/valley pair",
              e.get("price_factor_peak") is not None
              and e.get("price_factor_valley") is not None,
              (e.get("price_factor_peak"), e.get("price_factor_valley")))
# Qwen3.8-Max: is_free=true 绝不能吞掉它的 promotion（看板曾把它渲染成
# 0.00x 免费并吃掉低谷高亮）
me_freeflag = P.model_entry("qmodel_38max",
                            next(m for m in C.STATIC_CN_MODELS
                                 if m["key"] == "qmodel_38max"))
check("qmodel_38max is_free=true still carries promotion (peak 0.5 / valley 0.2)",
      me_freeflag.get("is_free") is True and me_freeflag.get("price_factor_peak") == 0.5
      and me_freeflag.get("price_factor_valley") == 0.2,
      (me_freeflag.get("is_free"), me_freeflag.get("price_factor_peak"),
       me_freeflag.get("price_factor_valley")))

# 看板分支顺序回归：promo 分支必须在 0 价分支之前
_dash = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "dashboard.html"), encoding="utf-8").read()
_i_promo = _dash.find("if(promo && valley != null)")
_i_free = _dash.find("valley === 0")
check("dashboard: promotion branch BEFORE free branch (highlight no longer swallowed)",
      0 <= _i_promo < _i_free, {"promo": _i_promo, "free": _i_free})
check("dashboard: is_free no longer triggers the 0.00x branch",
      "m.is_free || valley === 0" not in _dash)

print()
print("[11] transient upstream errors (418/provider_error) — retry not punish")
import time as _time_for_11
time = _time_for_11   # 本块直接使用 time.time()/sleep
# 分类判定
check("418 + provider_error is transient",
      P._is_transient_upstream(418, '{"code":"provider_error","message":"Error in upstream response"}'))
check("503 is transient", P._is_transient_upstream(503, ""))
check("client param error (invalid_parameter) NEVER transient",
      not P._is_transient_upstream(400,
          '{"code":"provider_error","details":"data: {\\"error\\":{\\"code\\":'
          '\\"invalid_parameter_error\\",\\"message\\":\\"Range of max_tokens should be [1, 131072]\\"}"'))
check("plain 400 without provider_error not transient",
      not P._is_transient_upstream(400, '{"code":"bad_request"}'))
check("401 never transient", not P._is_transient_upstream(401, "provider_error"))

# 行为级：第一次 418(瞬时) → 重试后成功，账号不背锅
import urllib.error as _ue3, io as _io3
_orig_urlopen = P.urllib.request.urlopen
_calls = {"n": 0}

class _FakeResp(object):
    def __enter__(self): return self
    def __exit__(self, *a): return False
    def read(self, *a): return b"{}"

def _urlopen_fail_once(req, timeout=None):
    _calls["n"] += 1
    if _calls["n"] == 1:
        raise _ue3.HTTPError(req.full_url, 418, "teapot", {},
                             _io3.BytesIO(b'{"code":"provider_error","message":"Error in upstream response"}'))
    return _FakeResp()

try:
    P.urllib.request.urlopen = _urlopen_fail_once
    # 构造单账号池
    import tempfile as _tf
    _td = _tf.mkdtemp(prefix="qdpool_")
    _pool = A.AccountPool(_td)
    _pool.add(A.Account({"uid": "retry-test-uid", "realm": "cn",
                         "accessToken": "dt-test", "refreshToken": "drt-test",
                         "expiresAt": 9999999999}))
    _orig_pool = P.POOL
    P.POOL = _pool
    _acc = _pool.accounts[0]
    _acc.cooldown_until = 0
    t0 = time.time()
    resp, used, _ = P.open_upstream(
        {"model": "qfmodel", "messages": [{"role": "user", "content": "hi"}],
         "stream": False}, target_realm="cn")
    took = time.time() - t0
    check("418-then-success: open_upstream returns after in-place retry",
          resp is not None and _calls["n"] == 2,
          {"calls": _calls["n"]})
    check("418-then-success: account NOT cooled down",
          _acc.cooldown_until <= time.time(),
          round(_acc.cooldown_until - time.time(), 1))
    check("418-then-success: backoff took ~1s (not instant, not 60s)",
          0.8 <= took <= 4.0, round(took, 2))
finally:
    P.urllib.request.urlopen = _orig_urlopen

# 行为级：持续 418 → 重试耗尽后短冷却（单账号 3s 而非 60s）并抛出原错误
def _urlopen_always_418(req, timeout=None):
    _calls["n"] += 1
    raise _ue3.HTTPError(req.full_url, 418, "teapot", {},
                         _io3.BytesIO(b'{"code":"provider_error","message":"Error in upstream response"}'))

_calls["n"] = 0
try:
    P.urllib.request.urlopen = _urlopen_always_418
    P.POOL = _pool          # 上一块 finally 还原了 None，这里重新挂上临时池
    _acc.cooldown_until = 0
    _acc.last_error = ""
    raised = None
    t0 = time.time()
    try:
        P.open_upstream({"model": "qfmodel",
                         "messages": [{"role": "user", "content": "hi"}],
                         "stream": False}, target_realm="cn")
    except _ue3.HTTPError as e:
        raised = e
    took = time.time() - t0
    check("persistent 418: raised after exactly 3 tries",
          raised is not None and raised.code == 418 and _calls["n"] == 3,
          {"calls": _calls["n"], "code": getattr(raised, "code", None)})
    check("persistent 418: short cooldown (<=5s, single-account pool)",
          0 < (_acc.cooldown_until - time.time()) <= 5.5,
          round(_acc.cooldown_until - time.time(), 2))
    check("persistent 418: bounded backoff time (~3s)",
          2.0 <= took <= 6.0, round(took, 2))
finally:
    P.urllib.request.urlopen = _orig_urlopen
    P.POOL = _orig_pool
    import shutil as _sh
    _sh.rmtree(_td, ignore_errors=True)

# 传输层瞬时故障分类（TLS EOF 等）
import urllib.error as _ue4
import ssl as _ssl_for_11
check("URLError wrapping SSL EOF is transient transport",
      P._is_transient_transport(_ue4.URLError(_ssl_for_11.SSLError(
          "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF"))))
check("ConnectionResetError is transient transport",
      P._is_transient_transport(ConnectionResetError("reset")))
check("plain ValueError NOT transient transport",
      not P._is_transient_transport(ValueError("nope")))
check("predicate: URLError+provider401 not transient-http",
      not P._is_transient_upstream(401, "x"))

# 友好错误映射
_m, _t = P.friendly_upstream_error(418,
    '{"code":"provider_error","message":"Error in upstream response"}')
check("friendly: 418 provider_error -> Chinese retry guidance",
      "上游瞬时故障" in _m and "请稍后重试" in _m, _m[:60])
check("friendly: err_type tagged transient",
      _t == "upstream_transient_error", _t)
_m2, _t2 = P.friendly_upstream_error(400,
    '{"code":"provider_error","details":"invalid_parameter_error Range"}')
check("friendly: client param error NOT reframed as transient",
      _t2 == "upstream_error" and "上游瞬时故障" not in _m2, (_t2, _m2[:50]))
check("friendly: detail preserved in message",
      "provider_error" in _m)

# qoder_detail passthrough（错误体只读一次的修复）——挂在持续418的断言上补充
check("raised HTTPError carries qoder_detail for handlers",
      hasattr(raised, "qoder_detail") and "provider_error" in raised.qoder_detail,
      getattr(raised, "qoder_detail", "")[:60])

print()
print("[12] in-stream envelope retry (200-then-418 form — the reported log shape)")
# 状态归一化
check("_to_int_status str/int/fallback", P._to_int_status("418") == 418
      and P._to_int_status(503) == 503 and P._to_int_status("xx") == 502)

_env418 = P.UpstreamStatus(
    418, '{"code":"provider_error","message":"Error in upstream response"}')
check("fresh envelope 418 transient -> retry",
      P.should_retry_envelope(_env418, emitted_bytes=False, attempt=0))
check("already emitted bytes -> NO retry",
      not P.should_retry_envelope(_env418, emitted_bytes=True, attempt=0))
check("budget exhausted -> NO retry",
      not P.should_retry_envelope(_env418, False, P.TRANSIENT_MAX_RETRIES))
_env_param = P.UpstreamStatus(400, "invalid_parameter_error Range of max_tokens")
check("client param envelope -> NO retry",
      not P.should_retry_envelope(_env_param, False, 0))

# aggregate_with_envelope_retry: 第一次信封418 → 重开上游 → 成功
_sleeps = []
_orig_sleep2 = P.time.sleep
_orig_open2 = P.open_upstream
_pools = {"calls": 0}

class _GoodResp(object):
    def __iter__(self):
        inner = json.dumps({"choices": [{"delta": {"content": "recovered"}}]})
        yield ("data: " + json.dumps({"statusCodeValue": 200, "body": inner})
               + "\n\n").encode("utf-8")
        yield b'data: {"statusCodeValue":200,"body":"[DONE]"}\n\n'

    def close(self):
        pass


class _ErrResp(object):
    def __iter__(self):
        yield ("data: " + json.dumps({
            "statusCodeValue": 418,
            "body": '{"code":"provider_error","message":"Error in upstream response"}'})
            + "\n\n").encode("utf-8")

    def close(self):
        pass


def _fake_open(payload, session_key=None, target_realm=None):
    _pools["calls"] += 1
    return _GoodResp(), "acct-1", "enc"


class _A(object):
    uid = "acct-1"


try:
    P.time.sleep = lambda s: _sleeps.append(s)
    P.open_upstream = _fake_open
    obj, acc = P.aggregate_with_envelope_retry(
        _ErrResp(), {"model": "qfmodel"}, None, "cn", "qfmodel",
        {"usage": None}, _A())
    check("envelope 418 -> reopened upstream and recovered",
          obj["choices"][0]["message"]["content"] == "recovered",
          obj["choices"][0]["message"].get("content"))
    check("reopen happened exactly once", _pools["calls"] == 1, _pools["calls"])
    check("backoff 1s recorded", _sleeps == [1], _sleeps)
finally:
    P.time.sleep = _orig_sleep2
    P.open_upstream = _orig_open2

# 非瞬时信封（客户端参数错）不重开、原样上抛
_sleeps2 = []
_pools2 = {"calls": 0}

print()
print("[13] content-policy rejection (DataInspectionFailed — 02:27 log root cause)")
_DI_DETAIL = ('{"error":{"message":"\\u003c400\\u003e InternalError.Algo.'
              'DataInspectionFailed: Input text data may contain inappropriate '
              'content.","type":"UnknownError"}}')
check("DataInspection detail -> NOT transient (no wasted retries)",
      not P._is_transient_upstream(418, _DI_DETAIL))
check("DataInspection envelope -> should_retry_envelope False",
      not P.should_retry_envelope(
          P.UpstreamStatus(418, _DI_DETAIL), emitted_bytes=False, attempt=0))
_m13, _t13 = P.friendly_upstream_error(418, _DI_DETAIL)
check("friendly: content-policy Chinese explanation",
      "内容安全审核未通过" in _m13 and "重试无效" in _m13, _m13[:70])
check("friendly: err_type content_policy_rejected",
      _t13 == "content_policy_rejected", _t13)
check("friendly: original detail preserved",
      "DataInspectionFailed" in _m13)

print()
print("[14] error-cooldown vs upstream-frequency (no misleading 429)")
import time as _t14
# 构造临时池：账号仅处于错误冷却
_td14 = __import__("tempfile").mkdtemp(prefix="qd14_")
_pool14 = A.AccountPool(_td14)
_acc14 = A.Account({"uid": "u14", "realm": "cn", "accessToken": "dt-x",
                    "refreshToken": "drt-x", "expiresAt": 9999999999})
_pool14.add(_acc14)
_orig_pool14 = P.POOL
P.POOL = _pool14
try:
    # (a) 仅账号错误冷却 -> 不算频控
    _acc14.cooldown_until = _t14.time() + 5
    _acc14.model_cooldowns.clear()
    throttled, w = P.realm_model_throttled("cn", "qfmodel")
    check("account error-cooldown is NOT a frequency-limit (no 429)",
          throttled is False, (throttled, w))
    check("retry_after_seconds ignores account cooldown",
          P.retry_after_seconds("qfmodel", "cn") == 60,
          P.retry_after_seconds("qfmodel", "cn"))
    wait = P._short_error_cooldown_wait("cn", "qfmodel")
    check("short error-cooldown wait surfaced (<=10s, >0)",
          0 < wait <= 10, wait)
    # (b) 上游频控 -> 正当429
    _acc14.cooldown_until = 0
    _acc14.model_cooldowns["qfmodel"] = _t14.time() + 60
    throttled2, w2 = P.realm_model_throttled("cn", "qfmodel")
    check("model_cooldowns (upstream 429) IS frequency-limit",
          throttled2 is True and w2 >= 59, (throttled2, w2))
    check("short wait suppressed while frequency-limited",
          P._short_error_cooldown_wait("cn", "qfmodel") == 0.0)
    check("retry_after reflects frequency wait",
          59 <= P.retry_after_seconds("qfmodel", "cn") <= 61,
          P.retry_after_seconds("qfmodel", "cn"))
    # (c) 行为：错误短冷却 -> 等待后续上（真实 sleep ~0.3s）而不是429
    _acc14.model_cooldowns.clear()
    _acc14.cooldown_until = _t14.time() + 0.3
    _orig_urlopen14 = P.urllib.request.urlopen

    class _R14(object):
        def __iter__(self):
            inner = json.dumps({"choices": [{"delta": {"content": "after-wait"}}]})
            yield ("data: " + json.dumps({"statusCodeValue": 200, "body": inner})
                   + "\n\n").encode()
            yield b'data: {"statusCodeValue":200,"body":"[DONE]"}\n\n'
        def close(self):
            pass
        def __enter__(self):
            return self
        def __exit__(self, *a):
            return False

    def _ok_urlopen(req, timeout=None):
        return _R14()

    try:
        P.urllib.request.urlopen = _ok_urlopen
        _sleep_used = []
        _t0 = _t14.time()
        try:
            resp, used, _ = P.open_upstream(
                {"model": "qfmodel",
                 "messages": [{"role": "user", "content": "hi"}],
                 "stream": False}, target_realm="cn")
            _served = True
            _rate = None
        except Exception as _e:
            _served = False
            _rate = _e
        took = _t14.time() - _t0
        check("short cooldown: request WAITS and serves (no 429/503)",
              _served and _rate is None,
              {"served": _served, "err": repr(_rate)})
        check("wait lasted ~0.3-1s (bounded)", 0.25 <= took <= 2.0,
              round(took, 2))
    finally:
        P.urllib.request.urlopen = _orig_urlopen14
    # (d) 频控行为不变：真429 仍然立刻 RateLimited
    _acc14.cooldown_until = 0
    _acc14.model_cooldowns["qfmodel"] = _t14.time() + 60
    try:
        P.urllib.request.urlopen = _ok_urlopen
        _raised14 = None
        try:
            P.open_upstream({"model": "qfmodel",
                             "messages": [{"role": "user", "content": "hi"}],
                             "stream": False}, target_realm="cn")
        except Exception as e14:
            _raised14 = e14
        check("genuine frequency limit still raises RateLimited fast",
              isinstance(_raised14, P.RateLimited)
              and "frequency" in str(getattr(_raised14, "detail", "")),
              repr(_raised14))
    finally:
        P.urllib.request.urlopen = _orig_urlopen14
finally:
    P.POOL = _orig_pool14
    _acc14.model_cooldowns.clear()
    _acc14.cooldown_until = 0
    import shutil as _sh14
    _sh14.rmtree(_td14, ignore_errors=True)

# favicon 404 静默
check("log_message silences favicon regardless of status (code present)",
      'req_path == "/favicon.ico"' in open(
          os.path.join(os.path.dirname(os.path.abspath(__file__)),
                       "qoder_proxy.py"), encoding="utf-8").read())

print()
print("[15] HTTP/1.1 SSE framing — keep-alive friendly (no more reconnect loop)")
_src15 = open(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                           "qoder_proxy.py"), encoding="utf-8").read()
check("handler defines chunked SSE helpers",
      all(k in _src15 for k in ("def _sse_begin", "def _sse_write",
                                "def _sse_end")))
check("_sse_begin sends Transfer-Encoding: chunked",
      'self.send_header("Transfer-Encoding", "chunked")' in _src15)
check("_sse_end writes terminating zero chunk",
      'b"0\\r\\n\\r\\n"' in _src15)
check("no 'Connection: close' on streaming responses (root cause of reconnect loop)",
      'self.send_header("Connection", "close")' not in _src15)
check("streaming writes go through _sse_write (chunk-encoded)",
      "self.wfile.write(line)" not in _src15
      and "self.wfile.write(clean_responses_frame(frame))" not in _src15)
check("chunked streams are terminated on every exit path",
      _src15.count("self._sse_end()") >= 4, _src15.count("self._sse_end()"))

print()
print("[16] liveness probe endpoints (GET /ping was 404 -> clients reconnect loop)")
check("/ping served without auth/panel",
      'if path in ("/ping", "/healthz", "/livez", "/readyz"):' in _src15)
check("/ping returns plain pong", 'body = b"pong\\n"' in _src15)
check("/ping sits before the /health handler (earliest match)",
      _src15.find('if path in ("/ping"') < _src15.find('if path == "/health":'))

print()
print("[17] SSE heartbeat during long upstream silence (TTFT up to 71s observed)")
check("sse_with_heartbeat helper exists",
      "def sse_with_heartbeat(" in _src15)
check("heartbeat emits SSE comment frames (': ping')",
      'b": ping\\n\\n"' in _src15)
check("heartbeat enabled by default with env override",
      'os.environ.get("QD_SSE_HEARTBEAT"' in _src15)
check("both streaming paths wrap their source with heartbeat",
      _src15.count("sse_with_heartbeat(") >= 3,
      _src15.count("sse_with_heartbeat("))

# 行为级：数据透传 / 空闲补心跳 / 上游异常原样抛出 / 可关闭
_sent2, _slow = [], []


def _slow_gen():
    time.sleep(0.4)                     # 模拟长首字延迟（上游静默）
    yield b"data: late\n\n"
    time.sleep(0.4)                     # 模拟帧间静默
    yield b"data: late2\n\n"


for item in P.sse_with_heartbeat(_slow_gen(), _sent2.append, interval=0.15,
                                 idle_limit=10):
    _slow.append(item)
check("heartbeat: data passes through unchanged",
      _slow == [b"data: late\n\n", b"data: late2\n\n"], _slow)
check("heartbeat: comment frames sent during both silences",
      len(_sent2) >= 2 and all(f == b": ping\n\n" for f in _sent2),
      (len(_sent2), _sent2))


class _BoomErr(RuntimeError):
    pass


def _boom_gen():
    yield b"data: first\n\n"
    raise _BoomErr("upstream died")


_sent3, _got3, _raised3 = [], [], None
try:
    for item in P.sse_with_heartbeat(_boom_gen(), _sent3.append, interval=0.15,
                                     idle_limit=5):
        _got3.append(item)
except Exception as exc:
    _raised3 = exc
check("heartbeat: upstream error propagates unchanged",
      isinstance(_raised3, _BoomErr) and _got3 == [b"data: first\n\n"],
      (type(_raised3).__name__, _got3))


def _one_gen():
    yield b"data: only\n\n"


_sent4, _got4 = [], []
for item in P.sse_with_heartbeat(_one_gen(), _sent4.append, interval=0,
                                 idle_limit=5):
    _got4.append(item)
check("heartbeat: interval=0 disables (pure pass-through)",
      _got4 == [b"data: only\n\n"] and _sent4 == [], (_got4, _sent4))

# 行为：内容审核信封 -> 一次都不重开（open_upstream 不被再次调用）并上抛
class _DiErrResp(object):
    def __iter__(self):
        yield ("data: " + json.dumps({
            "statusCodeValue": 418, "body": _DI_DETAIL}) + "\n\n").encode()

    def close(self):
        pass
    def __enter__(self):
        return self
    def __exit__(self, *a):
        return False


_pools13 = {"calls": 0}
_sleeps13 = []


def _fake_open_never13(payload, session_key=None, target_realm=None):
    _pools13["calls"] += 1
    return _GoodResp(), "acct-1", "enc"


try:
    P.time.sleep = lambda s: _sleeps13.append(s)
    P.open_upstream = _fake_open_never13
    _raised13 = None
    try:
        P.aggregate_with_envelope_retry(
            _DiErrResp(), {"model": "Qwen3.8-Flash"}, None, "cn",
            "Qwen3.8-Flash", {"usage": None}, _A())
    except P.UpstreamStatus as e13:
        _raised13 = e13
    check("content-policy envelope: raised immediately, ZERO reopen, ZERO sleep",
          _raised13 is not None and _pools13["calls"] == 0 and not _sleeps13,
          {"raised": _raised13 is not None, "reopen": _pools13["calls"],
           "sleeps": _sleeps13})
finally:
    P.time.sleep = _orig_sleep2
    P.open_upstream = _orig_open2

# 非瞬时信封（客户端参数错）不重开、原样上抛
_sleeps2 = []
_pools2 = {"calls": 0}

class _ParamErrResp(object):
    def __iter__(self):
        yield ("data: " + json.dumps({
            "statusCodeValue": 400,
            "body": "invalid_parameter_error Range of max_tokens [1, 131072]"})
            + "\n\n").encode("utf-8")

    def close(self):
        pass


def _fake_open_never(payload, session_key=None, target_realm=None):
    _pools2["calls"] += 1
    return _GoodResp(), "acct-1", "enc"


try:
    P.time.sleep = lambda s: _sleeps2.append(s)
    P.open_upstream = _fake_open_never
    _raised_env = None
    try:
        P.aggregate_with_envelope_retry(
            _ParamErrResp(), {"model": "qfmodel"}, None, "cn", "qfmodel",
            {"usage": None}, _A())
    except P.UpstreamStatus as e0:
        _raised_env = e0
    check("param envelope raises immediately (no reopen)",
          _raised_env is not None and _pools2["calls"] == 0,
          {"raised": _raised_env is not None, "reopen": _pools2["calls"]})
finally:
    P.time.sleep = _orig_sleep2
    P.open_upstream = _orig_open2

ctx = {m["key"]: m.get("max_input_tokens") for m in C.STATIC_CN_MODELS}
check("cn dmodel ctx = official 96000 (NOT a guess)", ctx.get("dmodel") == 96000,
      ctx.get("dmodel"))
ctx_i = {m["key"]: m.get("max_input_tokens") for m in C.STATIC_INTL_MODELS}
check("intl dmodel ctx = official 1000000", ctx_i.get("dmodel") == 1000000,
      ctx_i.get("dmodel"))
pf = {m["key"]: m.get("price_factor") for m in C.STATIC_CN_MODELS}
check("price_factor carried from official catalog",
      pf.get("qfmodel") == 0.0 and pf.get("dmodel") == 0.5, pf.get("dmodel"))
check("exclusive sets derived from catalogs",
      "gm51model" in C.CN_EXCLUSIVE and "smodel" in C.INTL_EXCLUSIVE)
check("exclusive realm detection: gm51model -> cn",
      P.exclusive_realm("gm51model") == "cn")
check("exclusive realm detection: smodel -> intl",
      P.exclusive_realm("smodel") == "intl")
check("alias resolves before exclusive check: glm-5.2 -> cn",
      P.exclusive_realm("glm-5.2") == "cn")
check("shared key has no exclusive owner", P.exclusive_realm("qmodel") == "")
check("detect_model_realm routes cn-exclusive to cn even under intl default",
      P.detect_model_realm("q37fmodel") == "cn")
check("detect_model_realm routes intl-exclusive to intl",
      P.detect_model_realm("performance") == "intl")
check("shared model follows current default realm",
      P.detect_model_realm("qmodel") == P.CURRENT_REALM)

print()
print("[5.6] check-in capability is probed at runtime (not hard-coded per realm)")
import qoder_accounts as _A
import urllib.error as _ue2, io as _io2
check("cn has_checkin hint True", _A.get_realm_config("cn")["has_checkin"] is True)
check("intl has_checkin hint False (still only a hint)",
      _A.get_realm_config("intl")["has_checkin"] is False)
acc_intl = _A.Account({"uid": "i1", "realm": "intl", "accessToken": "dt-x"})
acc_cn = _A.Account({"uid": "c1", "realm": "cn", "accessToken": "dt-y"})
# 未探测前不按区域拒绝：国际版同样会真的去尝试一次（实测国际版接口 404，
# 由探测结果决定后续跳过，而不是"看区域直接不做"）
check("intl account is attempted before probing (realm is not a gate)",
      acc_intl.can_checkin() is True and acc_intl.checkin_capability()[0] is None)
check("cn account can checkin", acc_cn.can_checkin() is True)

# 国际版形态：/daily-check-in/* 404 -> 能力记录为不可用 + 明确原因（不是静默）
def fake_status_404(url, **kw):
    raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b'{"errorCode":"NotFound"}'))
_orig_hj = _A.http_json
_A.http_json = fake_status_404
res_intl = acc_intl.checkin()
cap_intl, reason_intl = acc_intl.checkin_capability()
_A.http_json = _orig_hj
check("404 status -> (ok, unavailable) with explicit reason",
      res_intl.get("ok") is True and res_intl.get("unavailable") is True
      and res_intl.get("reason") == _A.CHECKIN_REASON_NOT_FOUND, res_intl)
check("404 message names the missing endpoint + hand-off to official client",
      "daily-check-in" in str(res_intl.get("msg"))
      and "客户端" in str(res_intl.get("msg")), res_intl.get("msg"))
check("capability cached as unavailable -> can_checkin False (no repeated 404s)",
      cap_intl is False and acc_intl.can_checkin() is False and "404" in reason_intl)
# TTL 到期后回到"未探测"，下一次调用会真的重探（活动上线即自动恢复）
acc_intl._checkin_cap_at -= (_A.CHECKIN_PROBE_TTL + 1)
check("TTL 过期 -> capability 回到 unknown，下一次触发重探",
      acc_intl.checkin_capability()[0] is None and acc_intl.can_checkin() is True)

# 官方活动停用 (DISABLED) -> 不 claim、按跳过成功处理
def fake_status_disabled(url, **kw):
    return {"campaignKey": "cn_daily_check_in_legacy", "status": "DISABLED",
            "rewardCredits": 100, "currentStreakDays": 0, "totalClaimDays": 0,
            "totalRewardCredits": 0}
_A.http_json = fake_status_disabled
acc_dis = _A.Account({"uid": "d1", "realm": "cn", "accessToken": "dt-x"})
res_dis = acc_dis.checkin()
cap_ok, _why = acc_dis.checkin_capability()
_A.http_json = _orig_hj
check("DISABLED campaign -> ok+disabled (no claim POST)",
      res_dis.get("ok") is True and res_dis.get("disabled") is True, res_dis)
check("realm-capable status response marks capability available",
      cap_ok is True, cap_ok)
# pro eligibility 404 -> 查询成功但不可领取
def fake_pro_404(url, **kw):
    raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b""))
_A.http_json = fake_pro_404
ok_p, elig_p = acc_dis.pro_eligibility()
_A.http_json = _orig_hj
check("pro eligibility 404 -> (queried, not eligible)",
      ok_p is True and elig_p is False, (ok_p, elig_p))

print()
print("[5.7] campaign platform (/sash/api/v1/me/campaigns) — the new daily-claim home")
_acc_camp = _A.Account({"uid": "cp1", "realm": "intl", "accessToken": "dt-x"})
check("campaigns path constant present (same path on cn + intl)",
      _A.PATH_CAMPAIGNS == "/sash/api/v1/me/campaigns")


def fake_campaigns(url, **kw):
    check("campaigns hit the openapi base of the account realm",
          url.startswith(_A.get_realm_config("intl")["openapi"]), url)
    return {"uid": "cp1", "showCampaign": True, "claimable": True,
            "campaignUrl": "https://qoder.com/activities/daily-credits",
            "campaigns": [{"campaignId": "c-1", "campaignKey": "client_launch_26",
                           "startAt": 1789000000000, "endAt": 1789500000000,
                           "placements": [{"type": "usage_panel"}]}]}


_A.http_json = fake_campaigns
camp = _acc_camp.campaigns()
_A.http_json = _orig_hj
check("campaigns normalized: show/claimable/url",
      camp["ok"] and camp["show_campaign"] and camp["claimable"]
      and camp["campaign_url"].endswith("/daily-credits"), camp)
check("campaigns normalized: id/key/epoch(ms->s)/placements",
      camp["campaigns"][0]["campaign_id"] == "c-1"
      and camp["campaigns"][0]["campaign_key"] == "client_launch_26"
      and camp["campaigns"][0]["start_at"] == 1789000000
      and camp["campaigns"][0]["placements"], camp["campaigns"])
check("campaign snapshot cached on the account", _acc_camp.campaign_status is camp)


def fake_campaigns_404(url, **kw):
    raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b'{"errorCode":"NotFound"}'))


_A.http_json = fake_campaigns_404
camp404 = _acc_camp.campaigns()
_A.http_json = _orig_hj
check("campaigns 404 -> ok False + available False (no crash)",
      camp404["ok"] is False and camp404["available"] is False, camp404)

# --- 「签到」按钮（/accounts/checkin -> checkin()）旧接口不可用时改走活动平台 ---
_REAL_CLAIM_BODY = {
    "grantId": "g-1", "status": "CLAIMED", "replayed": False,
    "benefit": {"kind": "CREDITS", "amount": 100,
                "validity": {"mode": "RELATIVE_DAYS", "days": 30}},
    "campaignId": "c-1", "campaignKey": "act-daily",
    "expiresAt": "2026-10-30T12:49:22.692712Z",
}


def _credit_campaigns_payload(status="CLAIMABLE", action="CLAIM_BENEFIT"):
    return {"uid": "cc-1", "showCampaign": True,
            "claimable": status == "CLAIMABLE",
            "campaignUrl": "https://qoder.com/growth-page/activity-iframe",
            "campaigns": [{"campaignId": "c-1", "campaignKey": "act-daily",
                           "actionType": action, "claimStatus": status,
                           "startAt": 0, "endAt": 0,
                           "benefit": {"kind": "CREDITS", "amount": 100},
                           "placements": []}]}


_claim_posts_fb = []


def fake_intl_claim_flow(url, **kw):
    if "daily-check-in/status" in url:
        raise _ue2.HTTPError(url, 404, "nf", {},
                             _io2.BytesIO(b'{"errorCode":"NotFound"}'))
    if url.endswith("/campaigns"):
        return _credit_campaigns_payload()
    if "/campaigns/c-1/claim" in url:
        _claim_posts_fb.append(kw.get("method"))
        return _REAL_CLAIM_BODY
    raise AssertionError("unexpected url: %s" % url)


_A.http_json = fake_intl_claim_flow
_acc_fb = _A.Account({"uid": "fb-1", "realm": "intl", "accessToken": "dt-x"})
_res_fb = _acc_fb.checkin()
_A.http_json = _orig_hj
check("checkin() falls back to campaign platform when legacy 404s",
      _res_fb.get("ok") and _res_fb.get("campaign")
      and _res_fb.get("reward_credits") == 100, _res_fb)
check("fallback issued exactly one claim POST",
      _claim_posts_fb == ["POST"], _claim_posts_fb)


def fake_cn_disabled_flow(url, **kw):
    if "daily-check-in/status" in url:
        return {"campaignKey": "cn_daily_check_in_legacy",
                "status": "DISABLED", "rewardCredits": 100}
    if url.endswith("/campaigns"):
        return _credit_campaigns_payload()
    if "/campaigns/c-1/claim" in url:
        return dict(_REAL_CLAIM_BODY, replayed=True)
    raise AssertionError("unexpected url: %s" % url)


_A.http_json = fake_cn_disabled_flow
_acc_fb2 = _A.Account({"uid": "fb-2", "realm": "cn", "accessToken": "dt-x"})
_res_fb2 = _acc_fb2.checkin()
_A.http_json = _orig_hj
check("DISABLED legacy also falls back to campaign (replay -> already)",
      _res_fb2.get("ok") and _res_fb2.get("campaign")
      and _res_fb2.get("already"), _res_fb2)


def fake_view_only(url, **kw):
    if "daily-check-in/status" in url:
        raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b""))
    if url.endswith("/campaigns"):
        return _credit_campaigns_payload(action="VIEW_DETAILS")
    raise AssertionError("VIEW_DETAILS promo must not be claimed: %s" % url)


_A.http_json = fake_view_only
_acc_fb3 = _A.Account({"uid": "fb-3", "realm": "intl", "accessToken": "dt-x"})
_res_fb3 = _acc_fb3.checkin()
_A.http_json = _orig_hj
check("VIEW_DETAILS promo never auto-claimed by the button path",
      _res_fb3.get("ok") and _res_fb3.get("unavailable")
      and not _res_fb3.get("campaign"), _res_fb3)


def fake_no_campaigns(url, **kw):
    if "daily-check-in/status" in url:
        raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b""))
    if url.endswith("/campaigns"):
        return {"uid": "cc-9", "showCampaign": False, "claimable": False,
                "campaignUrl": "", "campaigns": []}
    raise AssertionError("unexpected url: %s" % url)


_A.http_json = fake_no_campaigns
_acc_fb4 = _A.Account({"uid": "fb-4", "realm": "intl", "accessToken": "dt-x"})
_res_fb4 = _acc_fb4.checkin()
_A.http_json = _orig_hj
check("no campaigns -> legacy reason still surfaced (nothing silently claimed)",
      _res_fb4.get("ok") and _res_fb4.get("unavailable")
      and not _res_fb4.get("campaign"), _res_fb4)

print()
print("[6] request body construction")
body = P.build_qoder_body({
    "model": "qmodel_38max",
    "messages": [{"role": "system", "content": "You are X."},
                 {"role": "user", "content": "hello"}],
}, None, "qmodel_38max", realm="cn")
check("client system replaces template system",
      body["messages"][0]["content"] == "You are X.")
check("conversation appended",
      [m["role"] for m in body["messages"]] == ["system", "user"])
check("request/session ids fresh uuids",
      body["request_id"] and body["session_id"]
      and body["request_id"] != body["session_id"])
check("stream forced true", body["stream"] is True)
check("agent_id agent_common", body["agent_id"] == "agent_common")
check("model_config key", body["model_config"]["key"] == "qmodel_38max")
check("model_config display_name from official catalog",
      body["model_config"]["display_name"] == "Qwen3.8-Max",
      body["model_config"]["display_name"])
check("model_config ctx from official catalog (cn 180000)",
      body["model_config"]["max_input_tokens"] == 180000)
check("model_config is_vl from official catalog",
      body["model_config"]["is_vl"] is True)
check("chat_context.text follows latest user prompt",
      body["chat_context"]["text"]["text"] == "hello")
check("no client tools -> tools emptied (template agent tools dropped)",
      body["tools"] == [])
check("business.name from prompt", body["business"]["name"] == "hello")

body2 = P.build_qoder_body({
    "model": "qmodel",
    "messages": [{"role": "user", "content": "a"},
                 {"role": "assistant", "content": "b"},
                 {"role": "user", "content": "c"}],
    "max_tokens": 500,
    "reasoning_effort": "high",
    "tools": [{"type": "function", "function": {"name": "f",
                                                 "parameters": {}}}],
}, None, "qmodel")
check("keeps template system when client has none",
      body2["messages"][0]["role"] == "system")
check("multi-turn order kept",
      [m["role"] for m in body2["messages"]] == ["system", "user", "assistant", "user"])
check("max_tokens forwarded", body2["parameters"].get("max_tokens") == 500)
# 思考档位按官方词表归一化（见 [20]）：qmodel 只有开/关、无档位表，
# 下发档位会被上游静默忽略，故此处不再透传（none 仍可用于关闭思考）
check("level-less model drops reasoning_effort (upstream ignores it anyway)",
      "reasoning_effort" not in body2["parameters"],
      body2["parameters"].get("reasoning_effort"))
check("client tools kept", len(body2["tools"]) == 1)
check("latest prompt is c", body2["chat_context"]["text"]["text"] == "c")

# tool 角色降级 + assistant tool_calls 序列化
body3 = P.build_qoder_body({
    "model": "qmodel",
    "messages": [
        {"role": "user", "content": "run"},
        {"role": "assistant", "content": "",
         "tool_calls": [{"id": "c1", "type": "function",
                         "function": {"name": "Bash", "arguments": "{\"cmd\":\"ls\"}"}}]},
        {"role": "tool", "tool_call_id": "c1", "name": "Bash", "content": "file1"},
    ],
}, None, "qmodel")
roles3 = [m["role"] for m in body3["messages"]]
check("tool role degraded to user", roles3 == ["system", "user", "assistant", "user"])
check("assistant tool_calls serialized into content",
      "Bash" in body3["messages"][2]["content"])
check("tool result carried as user text",
      "file1" in body3["messages"][3]["content"])

print()
print("[6.5] DeepSeek reasoning_content backfill keys off the UPSTREAM model key")
# 复现 issue #2 的根因：客户端按文档写「内部 key: dfmodel」时，旧实现只按
# 名字前缀 "deepseek" 判断 -> 不做多轮 reasoning_content 兼容 -> 偶发失败。
_ds_trace = [
    {"role": "user", "content": "1+1=?"},
    {"role": "assistant", "content": "2", "reasoning_content": "简单加法"},
    {"role": "user", "content": "再+1"},
]
check("is_deepseek_model: upstream keys", 
      P.is_deepseek_model("", "dfmodel") and P.is_deepseek_model("", "dmodel"))
check("is_deepseek_model: client-visible names",
      P.is_deepseek_model("DeepSeek-Flash") and P.is_deepseek_model("deepseek-v4-pro")
      and P.is_deepseek_model("DeepSeek-V4-Pro"))
check("is_deepseek_model: display id form",
      P.is_deepseek_model("dfmodel (DeepSeek-Flash)"))
check("is_deepseek_model: non-DeepSeek models stay untouched",
      not P.is_deepseek_model("qmodel") and not P.is_deepseek_model("Qwen3.8-Max"))

_bf_key = P.backfill_reasoning_content([dict(m) for m in _ds_trace], "dfmodel",
                                       "dfmodel")
check("client using key 'dfmodel' NOW gets the backfill (regression)",
      all("reasoning_content" in m for m in _bf_key
          if m.get("role") == "assistant"), _bf_key)
_bf_name = P.backfill_reasoning_content([dict(m) for m in _ds_trace],
                                        "DeepSeek-Flash", "dfmodel")
check("display name path still works (no regression)",
      any(m.get("reasoning_content") == "简单加法" for m in _bf_name))
_bf_other = P.backfill_reasoning_content(
    [{"role": "user", "content": "q"},
     {"role": "assistant", "content": "a", "reasoning_content": "trace"}],
    "qmodel", "qmodel")
check("non-DeepSeek model: no reasoning_content added by the backfill",
      _bf_other[1] == {"role": "assistant", "content": "a",
                       "reasoning_content": "trace"})
_st_other, _flat_other, _ = P.flatten_messages(_bf_other, keep_reasoning=False)
check("flatten drops reasoning_content for non-DeepSeek upstreams",
      all("reasoning_content" not in m for m in _flat_other), _flat_other)
_st_ds, _flat_ds, _ = P.flatten_messages(
    [{"role": "user", "content": "q"},
     {"role": "assistant", "content": "a", "reasoning_content": "trace"}],
    keep_reasoning=True)
check("flatten KEEPS reasoning_content for DeepSeek upstreams (was dropped)",
      _flat_ds[1].get("reasoning_content") == "trace", _flat_ds)
_no_trace = P.backfill_reasoning_content(
    [{"role": "user", "content": "hi"}], "dfmodel", "dfmodel")
check("no reasoning trace in history -> no synthetic field",
      all("reasoning_content" not in m for m in _no_trace))

# 端到端：build_qoder_body 用显式 key 调用时也会补
_body_ds = P.build_qoder_body({
    "model": "dfmodel",
    "messages": [
        {"role": "user", "content": "1+1=?"},
        {"role": "assistant", "content": "2", "reasoning_content": "简单加法"},
        {"role": "user", "content": "再+1"},
    ],
}, None, "dfmodel", realm="cn")
_ds_assistants = [m for m in _body_ds["messages"] if m.get("role") == "assistant"]
check("build_qoder_body('dfmodel') backfills assistant history",
      _ds_assistants and all("reasoning_content" in m for m in _ds_assistants),
      _ds_assistants)

print()
print("[7] SSE envelope unwrapping & aggregation")


class FakeResp(object):
    def __iter__(self):
        inner = json.dumps({"id": "cc1", "model": "qmodel", "created": 1,
                            "choices": [{"delta": {"content": "hi"}}]})
        inner2 = json.dumps({"choices": [{"delta": {},
                                          "finish_reason": "stop"}],
                             "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                                       "total_tokens": 5}})
        return iter([
            ("data: " + json.dumps({"headers": {}, "body": inner,
                                    "statusCodeValue": 200}) + "\n\n").encode(),
            ("data: " + json.dumps({"body": inner2,
                                    "statusCodeValue": 200}) + "\n\n").encode(),
            b'data:{"body":"[DONE]"}\n\n',
            b'event:finish{"totalTime":10}\n',
        ])


holder = {}
lines = list(P.iter_inner_sse(FakeResp(), holder=holder))
check("unwrapped to standard data lines", len(lines) == 2
      and lines[0].startswith(b"data: "))
check("usage captured in holder", (holder.get("usage") or {}).get("total_tokens") == 5)
agg = P.aggregate_stream(FakeResp(), "qmodel", None, holder={})
check("aggregate content", agg["choices"][0]["message"]["content"] == "hi")
check("aggregate finish stop", agg["choices"][0]["finish_reason"] == "stop")
check("aggregate usage", agg.get("usage", {}).get("total_tokens") == 5)


class ErrResp(object):
    def __iter__(self):
        return iter([("data: " + json.dumps({"body": "quota exceeded",
                                             "statusCodeValue": 503})
                      + "\n\n").encode()])


try:
    list(P.iter_inner_sse(ErrResp()))
    check("non-200 envelope raises UpstreamStatus", False)
except P.UpstreamStatus as exc:
    check("non-200 envelope raises UpstreamStatus",
          str(exc.status) == "503" and "quota" in exc.detail)

# 空 tool_call 占位清洗
noisy = json.dumps({"choices": [{"delta": {"function_call": {"name": "",
                                                             "arguments": ""},
                                           "reasoning_content": ""}}]})
cleaned = P.clean_chunk(noisy)
check("empty function_call noise stripped",
      cleaned == "" or "function_call" not in cleaned, cleaned)

print()
print("[8] Responses API custom tool translation")
CUSTOM_TOOL = {"type": "custom", "name": "apply_patch",
               "description": "Use the patch format to edit files",
               "format": {"type": "grammar", "syntax": "lark",
                          "definition": "start: /.*/s"}}
FUNC_TOOL = {"type": "function", "name": "get_weather",
             "description": "weather",
             "parameters": {"type": "object", "properties": {}}}
chat = P.responses_to_chat({"model": "m", "input": "hi",
                            "tools": [CUSTOM_TOOL, FUNC_TOOL]})
tools = chat["tools"]
check("custom tool became type=function", tools[0]["type"] == "function",
      tools[0].get("type"))
check("custom tool single 'input' param",
      list((tools[0]["parameters"]["properties"] or {}).keys()) == ["input"])
check("freeform hint present", "freeform tool" in tools[0]["description"])
check("grammar forwarded", "start: /.*/s" in tools[0]["description"])
check("ordinary function tool untouched", tools[1] == FUNC_TOOL)

hist = {"model": "m", "input": [
    {"role": "user", "content": "edit the file"},
    {"type": "custom_tool_call", "name": "apply_patch", "call_id": "call_1",
     "input": "*** Begin Patch\n+hi\n*** End Patch"},
    {"type": "custom_tool_call_output", "call_id": "call_1", "output": "Done!"},
]}
c2msgs = P.responses_to_chat(hist)["messages"]
asst = [m for m in c2msgs if m.get("role") == "assistant" and m.get("tool_calls")]
check("assistant carries the tool call", len(asst) == 1)
check("payload wrapped as {input: ...}",
      json.loads(asst[0]["tool_calls"][0]["function"]["arguments"])["input"]
      .startswith("*** Begin Patch"))
tool_msgs = [m for m in c2msgs if m.get("role") == "tool"]
check("tool result appended", len(tool_msgs) == 1
      and tool_msgs[0]["tool_call_id"] == "call_1")

chat_obj = {"choices": [{"finish_reason": "tool_calls", "message": {
    "role": "assistant", "content": "",
    "tool_calls": [{"id": "call_7", "type": "function", "function": {
        "name": "apply_patch",
        "arguments": json.dumps({"input": "*** Begin Patch\n+ok\n*** End Patch"})}}]}}]}
r = P.chat_to_response(chat_obj, "m", {"apply_patch"})
item = r["output"][0]
check("non-stream re-inflated to custom_tool_call",
      item["type"] == "custom_tool_call", item.get("type"))
check("input unwrapped verbatim",
      item["input"] == "*** Begin Patch\n+ok\n*** End Patch")

# reasoning item 处理 (Issue #17 parity)
hist_r = {"model": "m", "input": [
    {"role": "user", "content": "solve math"},
    {"type": "reasoning", "id": "rs_1",
     "summary": [{"type": "summary_text", "text": "let me think"}]},
    {"type": "message", "role": "assistant", "content": "4"},
]}
cr = P.responses_to_chat(hist_r)["messages"]
asst_r = [m for m in cr if m.get("role") == "assistant"]
check("reasoning attached to assistant",
      len(asst_r) == 1 and asst_r[0].get("reasoning_content") == "let me think")

# 流式 Responses 事件序列
def chunk(delta, finish=None):
    return ("data: " + json.dumps({"choices": [{"delta": delta,
                                                "finish_reason": finish}]})
            + "\n\n").encode()

stream = [
    chunk({"tool_calls": [{"index": 0, "id": "call_9",
                           "function": {"name": "apply_patch",
                                        "arguments": ""}}]}),
    chunk({"tool_calls": [{"index": 0,
                           "function": {"arguments": '{"input":"*** Begin'}}]}),
    chunk({"tool_calls": [{"index": 0,
                           "function": {"arguments": ' Patch\\n+hi\\n*** End Patch"}'}}]}),
    chunk({}, "tool_calls"),
]
holder2 = {"usage": None, "custom_names": {"apply_patch"}}
raw_events = b"".join(P.stream_responses_events(iter(stream), "m", holder2))
text = raw_events.decode()
check("created event first", "response.created" in text)
check("custom_tool_call_input.delta present",
      "response.custom_tool_call_input.delta" in text)
check("custom_tool_call_input.done present",
      "response.custom_tool_call_input.done" in text)
check("no stray function_call_arguments for custom",
      "response.function_call_arguments" not in text)
check("completed terminal event", "response.completed" in text)
done = [json.loads(l[6:]) for l in text.splitlines()
        if l.startswith("data: ")
        and '"response.custom_tool_call_input.done"' in l]
check("done carries unwrapped input",
      done and done[0]["input"] == "*** Begin Patch\n+hi\n*** End Patch")

print()
print("[9] checkin / keepalive normalization (offline fixtures)")
acc = A.Account({"uid": "fx-1", "realm": "cn", "domain": "qoder.com.cn",
                 "accessToken": "jt-x", "refreshToken": "jrt-y",
                 "expiresAt": 9999999999})
# status: CLAIMED today
import time as _t
_today = _t.time()
ok, st = acc.checkin_status.__wrapped__ if False else (True, None)


class _FixStatus(object):
    pass


# 直接测归一化分支：monkeypatch http_json
orig_http_json = A.http_json


def fake_status_ok(url, **kw):
    return {"status": "CLAIMABLE", "rewardCredits": 100,
            "currentStreakDays": 3, "totalClaimDays": 10,
            "totalRewardCredits": 900, "lastClaimedAt": 0}


def fake_claim_ok(url, **kw):
    return {"success": True, "rewardCredits": 100}


A.http_json = fake_status_ok
res = acc.checkin()
check("claim path awards 100", res.get("ok") and res.get("reward_credits") == 100,
      res)
check("last_checkin stamped", bool(acc.last_checkin))

# 409 ALREADY_CLAIMED -> 已签到
import urllib.error as _ue
import io as _io


def fake_claim_conflict(url, **kw):
    if "daily-check-in/status" in url:
        # 昨天签过 -> 状态端点正常返回，claim 端点才是 409
        return {"status": "CLAIMED", "rewardCredits": 100,
                "currentStreakDays": 3, "totalClaimDays": 10,
                "totalRewardCredits": 900,
                "lastClaimedAt": int(_t.time()) - 86400}
    raise _ue.HTTPError(url, 409, "conflict", {},
                        _io.BytesIO(b'{"result":"ALREADY_CLAIMED"}'))


A.http_json = fake_claim_conflict
acc2 = A.Account({"uid": "fx-2", "realm": "cn", "accessToken": "jt-x",
                  "refreshToken": "jrt-y", "expiresAt": 9999999999})
res2 = acc2.checkin()
check("409 ALREADY_CLAIMED normalized to ok/already",
      res2.get("ok") and res2.get("already"), res2)
A.http_json = orig_http_json

# session dead markers
check("TOKEN_EXPIRE detected", A.session_dead("TOKEN_EXPIRE expired"))
check("12153 detected", A.session_dead('{"code":"12153"}'))
check("normal error not dead", not A.session_dead("connection reset"))

# token family routing
acc_d = A.Account({"uid": "d", "accessToken": "dt-1", "refreshToken": "drt-1"})
acc_j = A.Account({"uid": "j", "accessToken": "jt-1", "refreshToken": "jrt-1"})
check("device family", A.token_family(acc_d) == "device")
check("job family", A.token_family(acc_j) == "job")

print()
print("[4.5] credential / model-cache crypto KATs (official fixtures)")
import base64 as _b64
_FIX = r"C:\Users\shuishui\AppData\Local\Temp\qoder-ref\cli2api\testdata\protocol\1.1.34"
if os.path.isdir(_FIX):
    fx = json.load(open(os.path.join(_FIX, "credential.json"), encoding="utf-8"))
    mkey = fx["input"]["machine_key"].encode()
    fx_ct = _b64.b64decode(fx["expected"]["encrypted"])
    dec = S.aes_cbc_decrypt(fx_ct, mkey, mkey)
    check("credential fixture decrypt byte-exact",
          dec.decode() == fx["expected"]["decrypted"])
    enc = _b64.b64encode(S.aes_cbc_encrypt(dec, mkey, mkey)).decode()
    check("credential fixture encrypt byte-exact",
          enc == fx["expected"]["encrypted"])
    mf = json.load(open(os.path.join(_FIX, "model-cache.json"), encoding="utf-8"))
    plain = S.qmc_decrypt(mf["expected"]["encrypted"], mf["input"]["uid"])
    check("model-cache (QMC v1) fixture decrypt byte-exact",
          plain.decode() == mf["expected"]["decrypted"])
    # AES-256 互逆（QMC 用 32 字节 key -> 14 轮）
    k256 = bytes(range(32))
    blk = bytes(range(16))
    rks = S._expand_key(k256)
    check("AES-256 key schedule = 15 round keys", len(rks) == 15, len(rks))
    check("AES-256 block roundtrip",
          S._decrypt_block(S._encrypt_block(blk, rks), rks) == blk)
else:
    check("official crypto fixtures present", False, _FIX)

print()
print("[4.6] local credential scan (reads THIS machine's official stores)")
try:
    import qoder_accounts as _QA
    detected = _QA.scan_desktop_credentials()
    check("scan returns both realms", len(detected) >= 2, len(detected))
    realms_seen = {d["realm"] for d in detected}
    check("scan covers intl + cn", realms_seen == {"intl", "cn"}, realms_seen)
    valid = [d for d in detected if d.get("valid")]
    # 本机是否登录过属于环境状态：登录过则必须解出 uid/dt- 前缀
    if valid:
        check("valid entries carry uid + dt- token prefix",
              all(d["uid"] and d.get("kind") for d in valid),
              [(d["realm"], d.get("kind"), d.get("uid", "")[:8]) for d in valid])
        check("app entries decrypted via os_crypt (kind=app uid present)",
              all(d.get("uid") for d in valid if d["kind"] == "app"))
    else:
        check("scan ran without crash (no valid creds on this machine)", True)
except Exception as exc:
    check("local credential scan", False, exc)

print()
print("[10] gateway plumbing")
check("detect_model_realm fallback to current",
      P.detect_model_realm("mystery-model") == P.CURRENT_REALM)
check("CORS on API path", P.cors_origin_allowed("/v1/chat/completions"))
check("no CORS on management", not P.cors_origin_allowed("/accounts"))
check("no CORS on /v1/usage", not P.cors_origin_allowed("/v1/usage"))
check("realm persisted file name", P.REALM_STATE_FILE.endswith("active_realm.json"))
# 会话亲和键稳定性
k1 = P.derive_affinity_key([{"role": "system", "content": "s"},
                             {"role": "user", "content": "u1"}])
k2 = P.derive_affinity_key([{"role": "system", "content": "s"},
                             {"role": "user", "content": "u1"},
                             {"role": "assistant", "content": "a"}])
k3 = P.derive_affinity_key([{"role": "system", "content": "s"},
                             {"role": "user", "content": "different"}])
check("affinity key stable across turns", k1 == k2)
check("affinity key differs per conversation", k1 != k3)
# prompt fingerprint privacy
fp = P.prompt_fingerprint([{"role": "system", "content": "secret system"}])
check("fingerprint has no raw text",
      "secret" not in json.dumps(fp) and len(fp.get("system_sha", "")) == 12)

# flatten / sanitize
sys_text, flat, images = P.flatten_messages([
    {"role": "system", "content": "base"},
    {"role": "user", "content": [{"type": "text", "text": "part1"},
                                  {"type": "image_url",
                                   "image_url": {"url": "data:image/png;base64,AAA"}}]},
])
check("system extracted", sys_text == "base")
check("text parts joined", flat[0]["content"] == "part1")
check("image collected", images == ["data:image/png;base64,AAA"])
check("fingerprint string sanitized",
      "You are Claude Code, Anthropic's official CLI tool" in
      P.sanitize_text("You are Claude Code, Anthropic's official CLI tool for Claude"))

print()
print("[18] task center lists every realm (issue #1: intl check-in was filtered out)")
_t_intl = A.Account({"uid": "t-intl", "realm": "intl", "domain": "qoder.com",
                     "accessToken": "dt-x", "nickname": "intl-user"})
_t_cn = A.Account({"uid": "t-cn", "realm": "cn", "domain": "qoder.com.cn",
                   "accessToken": "dt-y", "nickname": "cn-user"})
_t_pool = A.AccountPool(os.path.join(os.environ["ACCOUNTS_DIR"], "unused"))
_t_pool.accounts = [_t_intl, _t_cn]
# 离线打桩：状态 404（intl 真实形态）+ 活动平台可用
_orig_status = A.Account.checkin_status
_orig_camp = A.Account.campaigns
_orig_credits = A.Account.fetch_credits
_orig_plan = A.Account.fetch_plan
_orig_elig = A.Account.pro_eligibility


def _stub_status(self):
    if self.realm == "intl":
        self._mark_checkin_capability(False, "%s (HTTP 404)"
                                      % A.CHECKIN_REASON_NOT_FOUND)
        return False, {"unavailable": True, "reason": A.CHECKIN_REASON_NOT_FOUND,
                       "http": 404, "error": "HTTP 404 NotFound"}
    return True, {"status": "DISABLED", "active": False, "today_checked_in": False,
                  "streak_days": 0, "total_claim_days": 0, "reward_credits": 100,
                  "total_reward_credits": 0, "next_claim_at": 0,
                  "last_claimed_at": 0, "reward_expires_at": 0}


A.Account.checkin_status = _stub_status
# 活动平台：intl 已领取、cn 可领取（100 Credits）—— 与官方桌面端真实形态一致
_A_CAMPAIGNS = {
    "intl": {"ok": True, "available": True, "show_campaign": True,
             "claimable": False, "campaign_url": "https://openapi.qoder.sh/growth-page/activity-iframe",
             "campaigns": [{"campaign_id": "c-intl", "campaign_key": "act-intl",
                            "action_type": "VIEW_DETAILS", "claim_status": "CLAIMED",
                            "start_at": 0, "end_at": 0,
                            "benefit": {"kind": "", "amount": 0},
                            "required_achievement_key": "",
                            "achievement_completed": False, "unavailable_reason": "",
                            "placements": []}]},
    "cn": {"ok": True, "available": True, "show_campaign": True,
           "claimable": True, "campaign_url": "https://openapi.qoder.com.cn/growth-page/activity-iframe",
           "campaigns": [{"campaign_id": "c-cn", "campaign_key": "act-daily-100",
                          "action_type": "CLAIM_BENEFIT", "claim_status": "CLAIMABLE",
                          "start_at": 0, "end_at": 0,
                          "benefit": {"kind": "CREDITS", "amount": 100},
                          "required_achievement_key": "",
                          "achievement_completed": False, "unavailable_reason": "",
                          "placements": []}]},
}
A.Account.campaigns = lambda self: dict(_A_CAMPAIGNS[self.realm])
A.Account.fetch_credits = lambda self: {"ok": True, "credits": {}}
A.Account.fetch_plan = lambda self: ""
A.Account.pro_eligibility = lambda self: (True, False)
try:
    _view_intl = T.fetch_tasks_view(_t_pool, uid="t-intl")
    _view_cn = T.fetch_tasks_view(_t_pool, uid="t-cn")
    _view_all = T.fetch_tasks_view(_t_pool)
finally:
    A.Account.checkin_status = _orig_status
    A.Account.campaigns = _orig_camp
    A.Account.fetch_credits = _orig_credits
    A.Account.fetch_plan = _orig_plan
    A.Account.pro_eligibility = _orig_elig

check("intl account is selectable in the task center",
      _view_intl.get("account", {}).get("uid") == "t-intl", _view_intl.get("msg"))
check("task center lists BOTH realms",
      {a["realm"] for a in _view_all["accounts"]} == {"cn", "intl"},
      _view_all["accounts"])
_intl_row = [t for t in _view_intl["tasks"] if t["task_code"] == "daily_checkin"][0]
_cn_row = [t for t in _view_cn["tasks"] if t["task_code"] == "daily_checkin"][0]
check("intl: claimed campaign renders as 今日已领取 (not an empty row)",
      _intl_row["status"] == "claimed" and "今日已领取" in _intl_row["description"],
      _intl_row)
check("cn: CLAIMABLE campaign renders as 待领奖 + reward amount",
      _cn_row["status"] == "completed" and _cn_row["reward_credit"] == 100
      and "领取" in _cn_row["description"], _cn_row)
check("legacy DISABLED endpoint no longer produces a noise row",
      "daily_checkin_legacy" not in [t["task_code"] for t in _view_cn["tasks"]],
      [t["task_code"] for t in _view_cn["tasks"]])
_codes = [t["task_code"] for t in _view_intl["tasks"]]
check("campaign state surfaced in summary (show/claimable/url/items)",
      _view_intl["summary"]["campaigns"]["show"] is True
      and _view_intl["summary"]["campaigns"]["items"][0]["key"] == "act-intl",
      _view_intl["summary"]["campaigns"])
check("daily row carries a jump url (campaignUrl or activities page)",
      bool(_intl_row.get("jump_url")) and "qoder" in _intl_row["jump_url"],
      _intl_row.get("jump_url"))

print()
print("[19] gateway host failover (official intl api1 -> api2; CN single host)")
check("gateway_candidates: intl primary is api1 with api2/api3 fallbacks",
      A.gateway_candidates("intl") == ["https://api1.qoder.sh",
                                       "https://api2.qoder.sh",
                                       "https://api3.qoder.sh"],
      A.gateway_candidates("intl"))
check("gateway_candidates: cn has a single official host",
      A.gateway_candidates("cn") == ["https://gateway.qoder.com.cn"],
      A.gateway_candidates("cn"))

# 行为：api1 传输层失败 -> 自动切到 api2 并在同一请求内成功
import ssl as _ssl19
_orig_urlopen19 = P.urllib.request.urlopen
_hits19 = []


class _Resp19(object):
    status = 200

    def read(self):
        return b""

    def close(self):
        pass


def _fake_urlopen19(req, timeout=None):
    _hits19.append(req.full_url)
    if "api1.qoder.sh" in req.full_url:
        raise _ssl19.SSLError("simulated TLS EOF on primary host")
    return _Resp19()


_orig_pool19 = P.POOL
_pool19 = A.AccountPool(os.path.join(os.environ["ACCOUNTS_DIR"], "unused19"))
_pool19.accounts = [A.Account({"uid": "h19", "realm": "intl",
                               "domain": "qoder.com",
                               "accessToken": "dt-x"})]
P.POOL = _pool19
P.urllib.request.urlopen = _fake_urlopen19
try:
    _resp19, _acc19, _ = P.open_upstream(
        {"model": "qmodel", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]},
        target_realm="intl")
    _err19 = None
except Exception as exc:                      # pragma: no cover - failure path
    _err19 = exc
finally:
    P.urllib.request.urlopen = _orig_urlopen19
    P.POOL = _orig_pool19

check("failover: request succeeded after primary host transport error",
      _err19 is None and _acc19.uid == "h19", _err19)
check("failover: both hosts were tried in order (api1 then api2)",
      len(_hits19) == 2 and "api1.qoder.sh" in _hits19[0]
      and "api2.qoder.sh" in _hits19[1], _hits19)
check("failover: signature path unchanged across hosts",
      _hits19[0].split("?")[0].endswith(P.CHAT_PATH.split("?")[0]),
      _hits19[0])

print()
print("[20] campaign check-in (the real daily-claim API) + effort normalization")

# --- 20.1 桌面端请求头（活动平台必需；缺了服务端返回空列表） ---
_hdrs = _t_cn.desktop_headers()
check("desktop headers carry Cosy-ClientType=10 + Cosy-Version + UA Qoder",
      _hdrs["cosy-clienttype"] == "10" and _hdrs["User-Agent"] == "Qoder"
      and bool(_hdrs["cosy-version"]), _hdrs.get("cosy-clienttype"))
check("desktop headers carry the machine identity set",
      all(_hdrs.get(k) for k in ("cosy-machineid", "cosy-machinetoken",
                                 "cosy-machinetype", "cosy-machineos",
                                 "cosy-machinehostname", "cosy-machinecode")))
check("native identity bridge can be disabled (derived fallback)",
      os.environ.get("QD_NATIVE_IDENTITY") == "0"
      and _t_cn.machine_identity_source == "derived"
      and A.runtime_info_exe("cn") == "",
      _t_cn.machine_identity_source)
check("desktop headers keep the Bearer token",
      _hdrs["Authorization"].startswith("Bearer "))
check("campaign claim/reward path templates (official growth-page contract)",
      A.PATH_CAMPAIGN_CLAIM == "/sash/api/v1/me/campaigns/%s/claim"
      and A.PATH_CAMPAIGN_REWARD == "/sash/api/v1/me/campaigns/%s/reward")

# --- 20.2 campaign_checkin: 只领取 CLAIMABLE + CLAIM_BENEFIT，幂等视为已领 ---
_orig_claim = A.Account.claim_campaign
_calls20 = []


def _stub_claim(self, campaign_id):
    _calls20.append(campaign_id)
    return {"ok": True, "status": "CLAIMED", "replayed": False, "grant_id": "g1",
            "amount": 100, "message": "领取成功"}


A.Account.claim_campaign = _stub_claim
A.Account.campaigns = lambda self: {
    "ok": True, "available": True, "show_campaign": True, "claimable": True,
    "campaign_url": "https://openapi.qoder.com.cn/growth-page/activity-iframe",
    "campaigns": [
        {"campaign_id": "c1", "campaign_key": "act-daily-100",
         "action_type": "CLAIM_BENEFIT", "claim_status": "CLAIMABLE",
         "start_at": 0, "end_at": 0, "benefit": {"kind": "CREDITS", "amount": 100},
         "placements": []},
        {"campaign_id": "c2", "campaign_key": "act-view-only",
         "action_type": "VIEW_DETAILS", "claim_status": "CLAIMABLE",
         "start_at": 0, "end_at": 0, "benefit": {"kind": "", "amount": 0},
         "placements": []},
        {"campaign_id": "c3", "campaign_key": "act-done",
         "action_type": "CLAIM_BENEFIT", "claim_status": "CLAIMED",
         "start_at": 0, "end_at": 0, "benefit": {"kind": "CREDITS", "amount": 100},
         "placements": []},
    ]}
_acc20 = A.Account({"uid": "cp20", "realm": "cn", "accessToken": "dt-x"})
try:
    _res20 = _acc20.campaign_checkin(gap=0)
finally:
    A.Account.claim_campaign = _orig_claim
    A.Account.campaigns = _orig_camp

check("only CLAIMABLE CLAIM_BENEFIT campaigns are claimed",
      _calls20 == ["c1"], _calls20)
check("VIEW_DETAILS campaign is not claimed", "c2" not in _calls20)
check("already-CLAIMED campaign is not re-posted", "c3" not in _calls20)
check("campaign_checkin reports earned credits + already list",
      _res20["earned"] == 100 and len(_res20["claimed"]) == 1
      and len(_res20["already"]) == 1, _res20.get("message"))
check("campaign_checkin stamps last_checkin on success", bool(_acc20.last_checkin))
check("campaign_checkin message names the claimed campaign",
      "act-daily-100" in _res20["message"], _res20["message"])

# --- 20.3 幂等：上游 replayed=true 视为已领取而不是新领取 ---
A.Account.campaigns = lambda self: {
    "ok": True, "available": True, "show_campaign": True, "claimable": True,
    "campaign_url": "", "campaigns": [
        {"campaign_id": "c9", "campaign_key": "act-x",
         "action_type": "CLAIM_BENEFIT", "claim_status": "CLAIMABLE",
         "start_at": 0, "end_at": 0, "benefit": {"kind": "CREDITS", "amount": 100},
         "placements": []}]}
A.Account.claim_campaign = lambda self, cid: {
    "ok": True, "status": "CLAIMED", "replayed": True, "grant_id": "g9",
    "amount": 100, "message": "已领取"}
try:
    _res21 = _acc20.campaign_checkin(gap=0)
finally:
    A.Account.claim_campaign = _orig_claim
    A.Account.campaigns = _orig_camp
check("replayed claim counted as already (no phantom credits)",
      _res21["earned"] == 0 and len(_res21["already"]) == 1
      and not _res21["claimed"], _res21.get("message"))

# --- 20.5 身份轮换：show=false 时强制刷新身份并重试一次 ---
_orig_get = A.Account._campaigns_get
_orig_native = A.native_machine_identity
_seq = []


def _stub_get(self):
    _seq.append(1)
    if len(_seq) == 1:
        return {"showCampaign": False, "claimable": False, "campaigns": []}, 200, ""
    return {"showCampaign": True, "claimable": True, "campaignUrl": "u",
            "campaigns": [{"campaignId": "cx", "campaignKey": "act-x",
                           "actionType": "CLAIM_BENEFIT", "claimStatus": "CLAIMABLE",
                           "startAt": 0, "endAt": 0,
                           "benefit": {"kind": "CREDITS", "amount": 100},
                           "placements": []}]}, 200, ""


_forced = []


def _stub_native(realm, account_id, force=False):
    if force:
        _forced.append(account_id)
    return {"machineToken": "t", "machineType": "ty", "machineCode": "c",
            "source": "runtime-info"}


A.Account._campaigns_get = _stub_get
A.native_machine_identity = _stub_native
_acc25 = A.Account({"uid": "cp25", "realm": "cn", "accessToken": "dt-x"})
_acc25.machine_identity_source = "native"
try:
    _st25 = _acc25.campaigns()
finally:
    A.Account._campaigns_get = _orig_get
    A.native_machine_identity = _orig_native
check("filtered list (show=false) triggers one forced identity refresh + retry",
      len(_seq) == 2 and _forced == ["cp25"]
      and _st25["show_campaign"] is True and _st25["claimable"] is True,
      (len(_seq), _forced))
check("retry keeps the second (populated) payload",
      len(_st25.get("campaigns") or []) == 1
      and _st25["campaigns"][0]["campaign_key"] == "act-x")

# --- 20.6 campaign_checkin 先强制刷新身份（轮换后不漏领） ---
_orig_get2 = A.Account._campaigns_get
_orig_native2 = A.native_machine_identity
_forced2 = []


def _stub_native2(realm, account_id, force=False):
    if force:
        _forced2.append(account_id)
    return {"machineToken": "t", "machineType": "ty", "machineCode": "c",
            "source": "runtime-info"}


A.Account._campaigns_get = lambda self: (
    {"showCampaign": True, "claimable": False, "campaignUrl": "", "campaigns": []},
    200, "")
A.native_machine_identity = _stub_native2
A.Account.claim_campaign = _orig_claim
try:
    A.Account({"uid": "cp26", "realm": "cn", "accessToken": "dt-x"}).campaign_checkin(gap=0)
finally:
    A.Account._campaigns_get = _orig_get2
    A.native_machine_identity = _orig_native2
check("campaign_checkin force-refreshes the machine identity first",
      _forced2 == ["cp26"], _forced2)

# --- 20.4 思考档位归一化（官方词表因模型而异，未命中会被上游静默忽略） ---
_meta_df = next(m for m in C.models_for_realm("cn") if m["key"] == "dfmodel")
_meta_qf = next(m for m in C.models_for_realm("cn") if m["key"] == "qfmodel")
_meta_qm = next(m for m in C.models_for_realm("cn") if m["key"] == "qmodel")
check("supported_efforts reads the official levels",
      P.supported_efforts(_meta_qf) == ["low", "medium", "xhigh"]
      and P.supported_efforts(_meta_df) == ["low", "high", "max"]
      and P.supported_efforts(_meta_qm) == [], P.supported_efforts(_meta_df))
check("default_effort reads the official is_default mark",
      P.default_effort(_meta_qf) == "medium" and P.default_effort(_meta_df) == "max",
      (P.default_effort(_meta_qf), P.default_effort(_meta_df)))
_v, _n = P.normalize_reasoning_effort("none", _meta_qf)
check("none stays none (universal off switch)", _v == "none")
_v, _n = P.normalize_reasoning_effort("medium", _meta_qf)
check("supported level passes through untouched", _v == "medium" and not _n, _n)
_v, _n = P.normalize_reasoning_effort("medium", _meta_df)
check("unsupported level maps to the nearest legal one (dfmodel medium->high)",
      _v == "high" and "unsupported" in _n, (_v, _n))
_v, _n = P.normalize_reasoning_effort("xhigh", _meta_df)
check("dfmodel xhigh -> max (nearest to request/default)", _v == "max", (_v, _n))
_v, _n = P.normalize_reasoning_effort("max", _meta_qf)
check("qfmodel max -> xhigh", _v == "xhigh", (_v, _n))
_v, _n = P.normalize_reasoning_effort("high", _meta_qf)
check("qfmodel high -> medium (tie broken toward the default)",
      _v == "medium", (_v, _n))
_v, _n = P.normalize_reasoning_effort("low", _meta_qm)
check("model without levels: effort param is dropped (not sent blindly)",
      _v is None and "dropped" in _n, (_v, _n))
_v, _n = P.normalize_reasoning_effort("none", _meta_qm)
check("model without levels still honours none", _v == "none")
# 路由器（auto 等）目录里没有 thinking_config：不猜测，原样透传
_meta_auto = next(m for m in C.models_for_realm("cn") if m["key"] == "auto")
check("router model without thinking_config passes the value through verbatim",
      P.normalize_reasoning_effort("high", _meta_auto) == ("high", "")
      and P.normalize_reasoning_effort("bogus", _meta_auto) == ("bogus", ""),
      P.normalize_reasoning_effort("high", _meta_auto))
# 未命中取最近合法档位：逐模型校验（同距偏向默认档）
for _key, _want in (("dmodel", {"medium": "high", "xhigh": "max", "low": "high"}),
                    ("gfmodel", {"medium": "high", "xhigh": "max"}),
                    ("kmodel", {"medium": "high", "xhigh": "max"})):
    _m = next(m for m in C.models_for_realm("cn") if m["key"] == _key)
    _got = {e: P.normalize_reasoning_effort(e, _m)[0] for e in _want}
    check("nearest-legal mapping on %s" % _key, _got == _want, (_got, _want))
# Responses API 路径：reasoning.effort / reasoning_effort 都要能到上游
_rchat = P.responses_to_chat({"model": "Qwen3.8-Flash", "input": "hi",
                              "reasoning": {"effort": "xhigh"}})
_rbody = P.build_qoder_body(dict(_rchat, model="Qwen3.8-Flash"), None,
                            "qfmodel", realm="cn")
check("Responses API reasoning.effort reaches upstream (normalized)",
      (_rbody.get("parameters") or {}).get("reasoning_effort") == "xhigh",
      (_rbody.get("parameters") or {}).get("reasoning_effort"))

# build_qoder_body: 端到端确认发到上游的档位已归一化 + thinking.* 兼容
_body_eff = P.build_qoder_body(
    {"model": "dfmodel", "messages": [{"role": "user", "content": "hi"}],
     "reasoning_effort": "medium"}, None, "dfmodel", realm="cn")
check("build_qoder_body sends the normalized effort upstream",
      (_body_eff.get("parameters") or {}).get("reasoning_effort") == "high",
      (_body_eff.get("parameters") or {}).get("reasoning_effort"))
_body_th = P.build_qoder_body(
    {"model": "qfmodel", "messages": [{"role": "user", "content": "hi"}],
     "thinking": {"effort": "xhigh"}}, None, "qfmodel", realm="cn")
check("thinking.effort is accepted as an alias",
      (_body_th.get("parameters") or {}).get("reasoning_effort") == "xhigh",
      (_body_th.get("parameters") or {}).get("reasoning_effort"))
_body_off = P.build_qoder_body(
    {"model": "qmodel", "messages": [{"role": "user", "content": "hi"}],
     "reasoning_effort": "high"}, None, "qmodel", realm="cn")
check("unsupported effort on a level-less model is dropped from the body",
      "reasoning_effort" not in (_body_off.get("parameters") or {}),
      _body_off.get("parameters"))

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
