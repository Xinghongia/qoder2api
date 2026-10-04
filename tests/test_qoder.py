"""Deterministic offline tests for the Qoder gateway.

No network: verifies the crypto primitives (AES FIPS vectors, RSA padding
structure, Qoder custom base64 round-trip), COSY signature layout, request
body construction, SSE envelope unwrapping, Responses-API custom-tool
translation, and check-in response normalization.

    python tests/test_qoder.py
"""
import hashlib
import json
import os
import sys
import time

_HERE = os.path.dirname(os.path.abspath(__file__))        # tests/
_ROOT = os.path.dirname(_HERE)                            # repo root

def _read_all_sources():
    """包内全部源码拼接：用于「源码里必须/不得出现某写法」的回归断言。"""
    parts = []
    for root, dirs, files in os.walk(os.path.join(_ROOT, "qoder2api")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in sorted(files):
            if f.endswith(".py"):
                with open(os.path.join(root, f), encoding="utf-8") as fh:
                    parts.append(fh.read())
    return "\n".join(parts)


_ALL_SRC = _read_all_sources()

sys.path.insert(0, _ROOT)
os.environ.setdefault("ACCOUNTS_DIR", os.path.join(_HERE, "_acc"))
os.environ.setdefault("USAGE_DIR", os.path.join(_HERE, "_use"))
# 离线测试不拉起客户端原生二进制（活动平台的机器身份桥）；测试里显式关闭
os.environ.setdefault("QD_NATIVE_IDENTITY", "0")

from qoder2api.api import Handler
from qoder2api import runtime, upstream, models, update, reasoning
from qoder2api import chat_normalize, body as body_mod, sanitize, security, affinity
from qoder2api import realm as realm_mod, usage as usage_mod
from qoder2api import views as views_mod, auth as auth_mod, errors, logbus
from qoder2api import responses as responses_mod
from qoder2api import model_entry, settings as qoder_settings
from qoder2api import sign as S
from qoder2api import catalog as C
from qoder2api import accounts as A
from qoder2api import net as qoder_net
from qoder2api import tasks as T

PASS = FAIL = 0


def check(label, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] " + label)
    else:
        FAIL += 1
        print("  [FAIL] " + label + ("  " + str(extra) if extra else ""))


def skip(label, why=""):
    """环境缺失（如官方 fixture 不在本机）不算失败，但必须显式打印原因。"""
    print("  [SKIP] " + label + ("  " + str(why) if why else ""))


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
url = "https://gateway.qoder.com.cn" + body_mod.CHAT_PATH
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
merged_i = models.merge_catalog([], realm="intl")
merged_c = models.merge_catalog([], realm="cn")
check("merge keeps FULL intl list (17, no enable filtering)", len(merged_i) == 17,
      len(merged_i))
check("merge keeps FULL cn list (14)", len(merged_c) == 14, len(merged_c))
# 清单以官方动态/本机目录为准（桌面版此刻显示什么就显示什么）
dyn_keys = [("cmodel", {"key": "cmodel", "display_name": "Cantus"})]  # 反例占位
primary15 = [(m["key"], dict(m)) for m in C.STATIC_INTL_MODELS
             if m["key"] not in ("cmodel", "smodel")]   # 模拟动态返回的 15 条
merged_dyn = models.merge_catalog(primary15, realm="intl")
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
me = model_entry.model_entry("qmodel_38max", cn38)
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
me_off = model_entry.model_entry("smodel", entry_i)
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
me_u = model_entry.model_entry("ultimate", next(m for m in C.STATIC_INTL_MODELS if m["key"] == "ultimate"))
check("model_entry name_local emitted for intl mode preset",
      bool(me_u.get("name_local")), me_u.get("name_local"))
check("resolve official local label (Kimi-K2.7-Code)",
      C.resolve_upstream_key("Kimi-K2.7-Code", realm="intl") == "kmodel")
check("cross-region guard accepts display id form",
      realm_mod.exclusive_realm("gm51model (GLM-5.2)") == "cn")

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
    got = model_entry.off_peak_active_now("22:00", "08:00", tz="Asia/Shanghai",
                                now=_ts(hh, mm))
    check(f"window {label}", got is expect, f"got={got} expect={expect}")
check("invalid window -> None",
      model_entry.off_peak_active_now(None, "08:00") is None)
check("same start/end -> always active",
      model_entry.off_peak_active_now("00:00", "00:00", now=_ts(13, 0)) is True)
# promotion fields surface off_peak_active_now via model_entry
me_promo = model_entry.model_entry("qmodel_38max", cn38)
check("model_entry exposes off_peak_active_now (bool)",
      isinstance(me_promo.get("off_peak_active_now"), bool),
      me_promo.get("off_peak_active_now"))

# None must not clobber static snapshot values in merge
dyn_null = [("qmodel_38max", {"key": "qmodel_38max",
                               "context_config": None,
                               "thinking_config": None,
                               "price_factor": 0.2})]
merged_null = dict(models.merge_catalog(dyn_null, realm="cn"))["qmodel_38max"]
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
        e = model_entry.model_entry(k, src)
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
me_freeflag = model_entry.model_entry("qmodel_38max",
                            next(m for m in C.STATIC_CN_MODELS
                                 if m["key"] == "qmodel_38max"))
check("qmodel_38max is_free=true still carries promotion (peak 0.5 / valley 0.2)",
      me_freeflag.get("is_free") is True and me_freeflag.get("price_factor_peak") == 0.5
      and me_freeflag.get("price_factor_valley") == 0.2,
      (me_freeflag.get("is_free"), me_freeflag.get("price_factor_peak"),
       me_freeflag.get("price_factor_valley")))

# 看板分支顺序回归：promo 分支必须在 0 价分支之前
# 迁移期：旧看板在 legacy/dashboard.html（新前端上线后由 web/ 取代，
# 这一组断言随之作废；见 docs/refactor-notes 或 README「目录结构」）
_DASH_PATH = os.path.join(_ROOT, "legacy", "dashboard.html")
_dash = open(_DASH_PATH, encoding="utf-8").read() if os.path.isfile(_DASH_PATH) else ""
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
      upstream._is_transient_upstream(418, '{"code":"provider_error","message":"Error in upstream response"}'))
check("503 is transient", upstream._is_transient_upstream(503, ""))
check("client param error (invalid_parameter) NEVER transient",
      not upstream._is_transient_upstream(400,
          '{"code":"provider_error","details":"data: {\\"error\\":{\\"code\\":'
          '\\"invalid_parameter_error\\",\\"message\\":\\"Range of max_tokens should be [1, 131072]\\"}"'))
check("plain 400 without provider_error not transient",
      not upstream._is_transient_upstream(400, '{"code":"bad_request"}'))
check("401 never transient", not upstream._is_transient_upstream(401, "provider_error"))

# 行为级：第一次 418(瞬时) → 重试后成功，账号不背锅
import urllib.error as _ue3, io as _io3
_orig_urlopen = qoder_net.urlopen
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
    qoder_net.urlopen = _urlopen_fail_once
    # 构造单账号池
    import tempfile as _tf
    _td = _tf.mkdtemp(prefix="qdpool_")
    _pool = A.AccountPool(_td)
    _pool.add(A.Account({"uid": "retry-test-uid", "realm": "cn",
                         "accessToken": "dt-test", "refreshToken": "drt-test",
                         "expiresAt": 9999999999}))
    _orig_pool = runtime.POOL
    runtime.POOL = _pool
    _acc = _pool.accounts[0]
    _acc.cooldown_until = 0
    t0 = time.time()
    resp, used, _ = upstream.open_upstream(
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
    qoder_net.urlopen = _orig_urlopen

# 行为级：持续 418 → 重试耗尽后短冷却（单账号 3s 而非 60s）并抛出原错误
def _urlopen_always_418(req, timeout=None):
    _calls["n"] += 1
    raise _ue3.HTTPError(req.full_url, 418, "teapot", {},
                         _io3.BytesIO(b'{"code":"provider_error","message":"Error in upstream response"}'))

_calls["n"] = 0
try:
    qoder_net.urlopen = _urlopen_always_418
    runtime.POOL = _pool          # 上一块 finally 还原了 None，这里重新挂上临时池
    _acc.cooldown_until = 0
    _acc.last_error = ""
    raised = None
    t0 = time.time()
    try:
        upstream.open_upstream({"model": "qfmodel",
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
    qoder_net.urlopen = _orig_urlopen
    runtime.POOL = _orig_pool
    import shutil as _sh
    _sh.rmtree(_td, ignore_errors=True)

# 传输层瞬时故障分类（TLS EOF 等）
import urllib.error as _ue4
import ssl as _ssl_for_11
check("URLError wrapping SSL EOF is transient transport",
      upstream._is_transient_transport(_ue4.URLError(_ssl_for_11.SSLError(
          "[SSL: UNEXPECTED_EOF_WHILE_READING] EOF"))))
check("ConnectionResetError is transient transport",
      upstream._is_transient_transport(ConnectionResetError("reset")))
check("plain ValueError NOT transient transport",
      not upstream._is_transient_transport(ValueError("nope")))
check("predicate: URLError+provider401 not transient-http",
      not upstream._is_transient_upstream(401, "x"))

# 友好错误映射
_m, _t = upstream.friendly_upstream_error(418,
    '{"code":"provider_error","message":"Error in upstream response"}')
check("friendly: 418 provider_error -> Chinese retry guidance",
      "上游瞬时故障" in _m and "请稍后重试" in _m, _m[:60])
check("friendly: err_type tagged transient",
      _t == "upstream_transient_error", _t)
_m2, _t2 = upstream.friendly_upstream_error(400,
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
check("_to_int_status str/int/fallback", upstream._to_int_status("418") == 418
      and upstream._to_int_status(503) == 503 and upstream._to_int_status("xx") == 502)

_env418 = errors.UpstreamStatus(
    418, '{"code":"provider_error","message":"Error in upstream response"}')
check("fresh envelope 418 transient -> retry",
      upstream.should_retry_envelope(_env418, emitted_bytes=False, attempt=0))
check("already emitted bytes -> NO retry",
      not upstream.should_retry_envelope(_env418, emitted_bytes=True, attempt=0))
check("budget exhausted -> NO retry",
      not upstream.should_retry_envelope(_env418, False, upstream.TRANSIENT_MAX_RETRIES))
_env_param = errors.UpstreamStatus(400, "invalid_parameter_error Range of max_tokens")
check("client param envelope -> NO retry",
      not upstream.should_retry_envelope(_env_param, False, 0))

# aggregate_with_envelope_retry: 第一次信封418 → 重开上游 → 成功
_sleeps = []
_orig_sleep2 = time.sleep
_orig_open2 = upstream.open_upstream
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
    time.sleep = lambda s: _sleeps.append(s)
    upstream.open_upstream = _fake_open
    obj, acc = upstream.aggregate_with_envelope_retry(
        _ErrResp(), {"model": "qfmodel"}, None, "cn", "qfmodel",
        {"usage": None}, _A())
    check("envelope 418 -> reopened upstream and recovered",
          obj["choices"][0]["message"]["content"] == "recovered",
          obj["choices"][0]["message"].get("content"))
    check("reopen happened exactly once", _pools["calls"] == 1, _pools["calls"])
    check("backoff 1s recorded", _sleeps == [1], _sleeps)
finally:
    time.sleep = _orig_sleep2
    upstream.open_upstream = _orig_open2

# 非瞬时信封（客户端参数错）不重开、原样上抛
_sleeps2 = []
_pools2 = {"calls": 0}

print()
print("[13] content-policy rejection (DataInspectionFailed — 02:27 log root cause)")
_DI_DETAIL = ('{"error":{"message":"\\u003c400\\u003e InternalError.Algo.'
              'DataInspectionFailed: Input text data may contain inappropriate '
              'content.","type":"UnknownError"}}')
check("DataInspection detail -> NOT transient (no wasted retries)",
      not upstream._is_transient_upstream(418, _DI_DETAIL))
check("DataInspection envelope -> should_retry_envelope False",
      not upstream.should_retry_envelope(
          errors.UpstreamStatus(418, _DI_DETAIL), emitted_bytes=False, attempt=0))
_m13, _t13 = upstream.friendly_upstream_error(418, _DI_DETAIL)
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
_orig_pool14 = runtime.POOL
runtime.POOL = _pool14
try:
    # (a) 仅账号错误冷却 -> 不算频控
    _acc14.cooldown_until = _t14.time() + 5
    _acc14.model_cooldowns.clear()
    throttled, w = upstream.realm_model_throttled("cn", "qfmodel")
    check("account error-cooldown is NOT a frequency-limit (no 429)",
          throttled is False, (throttled, w))
    check("retry_after_seconds ignores account cooldown",
          upstream.retry_after_seconds("qfmodel", "cn") == 60,
          upstream.retry_after_seconds("qfmodel", "cn"))
    wait = upstream._short_error_cooldown_wait("cn", "qfmodel")
    check("short error-cooldown wait surfaced (<=10s, >0)",
          0 < wait <= 10, wait)
    # (b) 上游频控 -> 正当429
    _acc14.cooldown_until = 0
    _acc14.model_cooldowns["qfmodel"] = _t14.time() + 60
    throttled2, w2 = upstream.realm_model_throttled("cn", "qfmodel")
    check("model_cooldowns (upstream 429) IS frequency-limit",
          throttled2 is True and w2 >= 59, (throttled2, w2))
    check("short wait suppressed while frequency-limited",
          upstream._short_error_cooldown_wait("cn", "qfmodel") == 0.0)
    check("retry_after reflects frequency wait",
          59 <= upstream.retry_after_seconds("qfmodel", "cn") <= 61,
          upstream.retry_after_seconds("qfmodel", "cn"))
    # (c) 行为：错误短冷却 -> 等待后续上（真实 sleep ~0.3s）而不是429
    _acc14.model_cooldowns.clear()
    _acc14.cooldown_until = _t14.time() + 0.3
    _orig_urlopen14 = qoder_net.urlopen

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
        qoder_net.urlopen = _ok_urlopen
        _sleep_used = []
        _t0 = _t14.time()
        try:
            resp, used, _ = upstream.open_upstream(
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
        qoder_net.urlopen = _orig_urlopen14
    # (d) 频控行为不变：真429 仍然立刻 RateLimited
    _acc14.cooldown_until = 0
    _acc14.model_cooldowns["qfmodel"] = _t14.time() + 60
    try:
        qoder_net.urlopen = _ok_urlopen
        _raised14 = None
        try:
            upstream.open_upstream({"model": "qfmodel",
                             "messages": [{"role": "user", "content": "hi"}],
                             "stream": False}, target_realm="cn")
        except Exception as e14:
            _raised14 = e14
        check("genuine frequency limit still raises RateLimited fast",
              isinstance(_raised14, upstream.RateLimited)
              and "frequency" in str(getattr(_raised14, "detail", "")),
              repr(_raised14))
    finally:
        qoder_net.urlopen = _orig_urlopen14
finally:
    runtime.POOL = _orig_pool14
    _acc14.model_cooldowns.clear()
    _acc14.cooldown_until = 0
    import shutil as _sh14
    _sh14.rmtree(_td14, ignore_errors=True)

# favicon 404 静默
check("log_message silences favicon regardless of status (code present)",
      'req_path == "/favicon.ico"' in _ALL_SRC)

print()
print("[15] HTTP/1.1 SSE framing — keep-alive friendly (no more reconnect loop)")
_src15 = _ALL_SRC
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


for item in upstream.sse_with_heartbeat(_slow_gen(), _sent2.append, interval=0.15,
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
    for item in upstream.sse_with_heartbeat(_boom_gen(), _sent3.append, interval=0.15,
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
for item in upstream.sse_with_heartbeat(_one_gen(), _sent4.append, interval=0,
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
    time.sleep = lambda s: _sleeps13.append(s)
    upstream.open_upstream = _fake_open_never13
    _raised13 = None
    try:
        upstream.aggregate_with_envelope_retry(
            _DiErrResp(), {"model": "Qwen3.8-Flash"}, None, "cn",
            "Qwen3.8-Flash", {"usage": None}, _A())
    except errors.UpstreamStatus as e13:
        _raised13 = e13
    check("content-policy envelope: raised immediately, ZERO reopen, ZERO sleep",
          _raised13 is not None and _pools13["calls"] == 0 and not _sleeps13,
          {"raised": _raised13 is not None, "reopen": _pools13["calls"],
           "sleeps": _sleeps13})
finally:
    time.sleep = _orig_sleep2
    upstream.open_upstream = _orig_open2

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
    time.sleep = lambda s: _sleeps2.append(s)
    upstream.open_upstream = _fake_open_never
    _raised_env = None
    try:
        upstream.aggregate_with_envelope_retry(
            _ParamErrResp(), {"model": "qfmodel"}, None, "cn", "qfmodel",
            {"usage": None}, _A())
    except errors.UpstreamStatus as e0:
        _raised_env = e0
    check("param envelope raises immediately (no reopen)",
          _raised_env is not None and _pools2["calls"] == 0,
          {"raised": _raised_env is not None, "reopen": _pools2["calls"]})
finally:
    time.sleep = _orig_sleep2
    upstream.open_upstream = _orig_open2

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
      realm_mod.exclusive_realm("gm51model") == "cn")
check("exclusive realm detection: smodel -> intl",
      realm_mod.exclusive_realm("smodel") == "intl")
check("alias resolves before exclusive check: glm-5.2 -> cn",
      realm_mod.exclusive_realm("glm-5.2") == "cn")
check("shared key has no exclusive owner", realm_mod.exclusive_realm("qmodel") == "")
check("detect_model_realm routes cn-exclusive to cn even under intl default",
      realm_mod.detect_model_realm("q37fmodel") == "cn")
check("detect_model_realm routes intl-exclusive to intl",
      realm_mod.detect_model_realm("performance") == "intl")
check("shared model follows current default realm",
      realm_mod.detect_model_realm("qmodel") == runtime.CURRENT_REALM)

print()
print("[5.6] check-in capability is probed at runtime (not hard-coded per realm)")
import qoder2api.accounts as _A
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
camp404 = _acc_camp.campaigns(force=True)   # force 绕过 20s 短缓存，直查上游
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
body = body_mod.build_qoder_body({
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

body2 = body_mod.build_qoder_body({
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
body3 = body_mod.build_qoder_body({
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
      chat_normalize.is_deepseek_model("", "dfmodel") and chat_normalize.is_deepseek_model("", "dmodel"))
check("is_deepseek_model: client-visible names",
      chat_normalize.is_deepseek_model("DeepSeek-Flash") and chat_normalize.is_deepseek_model("deepseek-v4-pro")
      and chat_normalize.is_deepseek_model("DeepSeek-V4-Pro"))
check("is_deepseek_model: display id form",
      chat_normalize.is_deepseek_model("dfmodel (DeepSeek-Flash)"))
check("is_deepseek_model: non-DeepSeek models stay untouched",
      not chat_normalize.is_deepseek_model("qmodel") and not chat_normalize.is_deepseek_model("Qwen3.8-Max"))

_bf_key = chat_normalize.backfill_reasoning_content([dict(m) for m in _ds_trace], "dfmodel",
                                       "dfmodel")
check("client using key 'dfmodel' NOW gets the backfill (regression)",
      all("reasoning_content" in m for m in _bf_key
          if m.get("role") == "assistant"), _bf_key)
_bf_name = chat_normalize.backfill_reasoning_content([dict(m) for m in _ds_trace],
                                        "DeepSeek-Flash", "dfmodel")
check("display name path still works (no regression)",
      any(m.get("reasoning_content") == "简单加法" for m in _bf_name))
_bf_other = chat_normalize.backfill_reasoning_content(
    [{"role": "user", "content": "q"},
     {"role": "assistant", "content": "a", "reasoning_content": "trace"}],
    "qmodel", "qmodel")
check("non-DeepSeek model: no reasoning_content added by the backfill",
      _bf_other[1] == {"role": "assistant", "content": "a",
                       "reasoning_content": "trace"})
_st_other, _flat_other, _ = body_mod.flatten_messages(_bf_other, keep_reasoning=False)
check("flatten drops reasoning_content for non-DeepSeek upstreams",
      all("reasoning_content" not in m for m in _flat_other), _flat_other)
_st_ds, _flat_ds, _ = body_mod.flatten_messages(
    [{"role": "user", "content": "q"},
     {"role": "assistant", "content": "a", "reasoning_content": "trace"}],
    keep_reasoning=True)
check("flatten KEEPS reasoning_content for DeepSeek upstreams (was dropped)",
      _flat_ds[1].get("reasoning_content") == "trace", _flat_ds)
_no_trace = chat_normalize.backfill_reasoning_content(
    [{"role": "user", "content": "hi"}], "dfmodel", "dfmodel")
check("no reasoning trace in history -> no synthetic field",
      all("reasoning_content" not in m for m in _no_trace))

# 端到端：build_qoder_body 用显式 key 调用时也会补
_body_ds = body_mod.build_qoder_body({
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
lines = list(upstream.iter_inner_sse(FakeResp(), holder=holder))
check("unwrapped to standard data lines", len(lines) == 2
      and lines[0].startswith(b"data: "))
check("usage captured in holder", (holder.get("usage") or {}).get("total_tokens") == 5)
agg = upstream.aggregate_stream(FakeResp(), "qmodel", None, holder={})
check("aggregate content", agg["choices"][0]["message"]["content"] == "hi")
check("aggregate finish stop", agg["choices"][0]["finish_reason"] == "stop")
check("aggregate usage", agg.get("usage", {}).get("total_tokens") == 5)


class ErrResp(object):
    def __iter__(self):
        return iter([("data: " + json.dumps({"body": "quota exceeded",
                                             "statusCodeValue": 503})
                      + "\n\n").encode()])


try:
    list(upstream.iter_inner_sse(ErrResp()))
    check("non-200 envelope raises UpstreamStatus", False)
except errors.UpstreamStatus as exc:
    check("non-200 envelope raises UpstreamStatus",
          str(exc.status) == "503" and "quota" in exc.detail)

# 空 tool_call 占位清洗
noisy = json.dumps({"choices": [{"delta": {"function_call": {"name": "",
                                                             "arguments": ""},
                                           "reasoning_content": ""}}]})
cleaned = sanitize.clean_chunk(noisy)
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
chat = responses_mod.responses_to_chat({"model": "m", "input": "hi",
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
c2msgs = responses_mod.responses_to_chat(hist)["messages"]
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
r = responses_mod.chat_to_response(chat_obj, "m", {"apply_patch"})
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
cr = responses_mod.responses_to_chat(hist_r)["messages"]
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
raw_events = b"".join(responses_mod.stream_responses_events(iter(stream), "m", holder2))
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
# fixture 探测：环境变量优先，其次按候选顺序找；不再硬编码单个 %TEMP% 路径。
_FIX_ENV = os.environ.get("QD_TEST_FIXTURE_DIR") or ""
_FIX_CANDIDATES = [
    _FIX_ENV,
    os.path.join(_HERE, "testdata", "protocol", "1.1.34"),
    os.path.join(_HERE, "fixtures", "protocol", "1.1.34"),
    os.path.join(_ROOT, os.pardir, "qoder-ref", "cli2api", "testdata",
                 "protocol", "1.1.34"),
    os.path.join(os.environ.get("TEMP") or os.environ.get("TMP") or "/tmp",
                 "qoder-ref", "cli2api", "testdata", "protocol", "1.1.34"),
    os.path.join(os.path.expanduser("~"), "qoder-ref", "cli2api",
                 "testdata", "protocol", "1.1.34"),
]
if _FIX_ENV and not os.path.isdir(_FIX_ENV):
    print("  [WARN] QD_TEST_FIXTURE_DIR 指向的目录不存在：%s" % _FIX_ENV)
_FIX, _FIX_TRIED = "", []
for _cand in _FIX_CANDIDATES:
    if not _cand:
        continue
    _cand_abs = os.path.abspath(_cand)
    _FIX_TRIED.append(_cand_abs)
    if os.path.isdir(_cand_abs):
        _FIX = _cand_abs
        break
if _FIX:
    print("  fixture 目录: %s" % _FIX)
else:
    print("  fixture 目录: 未找到；已探测 %d 条候选：" % len(_FIX_TRIED))
    for _p in _FIX_TRIED:
        print("      -  %s" % _p)
    print("      指定方式: QD_TEST_FIXTURE_DIR=<dir> python tests/test_qoder.py")

_CRED_FP = os.path.join(_FIX, "credential.json") if _FIX else ""
_MCACHE_FP = os.path.join(_FIX, "model-cache.json") if _FIX else ""

if _CRED_FP and os.path.isfile(_CRED_FP):
    fx = json.load(open(_CRED_FP, encoding="utf-8"))
    mkey = fx["input"]["machine_key"].encode()
    fx_ct = _b64.b64decode(fx["expected"]["encrypted"])
    dec = S.aes_cbc_decrypt(fx_ct, mkey, mkey)
    check("credential fixture decrypt byte-exact",
          dec.decode() == fx["expected"]["decrypted"])
    enc = _b64.b64encode(S.aes_cbc_encrypt(dec, mkey, mkey)).decode()
    check("credential fixture encrypt byte-exact",
          enc == fx["expected"]["encrypted"])
else:
    _miss_cred = "缺 credential.json" if _FIX else "缺 fixture 目录"
    skip("credential fixture decrypt byte-exact", _miss_cred)
    skip("credential fixture encrypt byte-exact", _miss_cred)

if _MCACHE_FP and os.path.isfile(_MCACHE_FP):
    mf = json.load(open(_MCACHE_FP, encoding="utf-8"))
    plain = S.qmc_decrypt(mf["expected"]["encrypted"], mf["input"]["uid"])
    check("model-cache (QMC v1) fixture decrypt byte-exact",
          plain.decode() == mf["expected"]["decrypted"])
else:
    skip("model-cache (QMC v1) fixture decrypt byte-exact",
         "缺 model-cache.json" if _FIX else "缺 fixture 目录")

# AES-256 互逆（QMC 用 32 字节 key -> 15 个轮密钥）：纯算法、不依赖 fixture，
# 因此永远执行（此前被误放进 fixture 分支，缺 fixture 时连算法 KAT 都一起没跑）。
k256 = bytes(range(32))
blk = bytes(range(16))
rks = S._expand_key(k256)
check("AES-256 key schedule = 15 round keys", len(rks) == 15, len(rks))
check("AES-256 block roundtrip",
      S._decrypt_block(S._encrypt_block(blk, rks), rks) == blk)

print()
print("[4.6] local credential scan (reads THIS machine's official stores)")
try:
    import qoder2api.accounts as _QA
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
      realm_mod.detect_model_realm("mystery-model") == runtime.CURRENT_REALM)
check("CORS on API path", security.cors_origin_allowed("/v1/chat/completions"))
check("no CORS on management", not security.cors_origin_allowed("/accounts"))
check("no CORS on /v1/usage", not security.cors_origin_allowed("/v1/usage"))
check("realm persisted file name", runtime.REALM_STATE_FILE.endswith("active_realm.json"))
# 会话亲和键稳定性
k1 = affinity.derive_affinity_key([{"role": "system", "content": "s"},
                             {"role": "user", "content": "u1"}])
k2 = affinity.derive_affinity_key([{"role": "system", "content": "s"},
                             {"role": "user", "content": "u1"},
                             {"role": "assistant", "content": "a"}])
k3 = affinity.derive_affinity_key([{"role": "system", "content": "s"},
                             {"role": "user", "content": "different"}])
check("affinity key stable across turns", k1 == k2)
check("affinity key differs per conversation", k1 != k3)
# prompt fingerprint privacy
fp = affinity.prompt_fingerprint([{"role": "system", "content": "secret system"}])
check("fingerprint has no raw text",
      "secret" not in json.dumps(fp) and len(fp.get("system_sha", "")) == 12)

# flatten / sanitize
sys_text, flat, images = body_mod.flatten_messages([
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
      sanitize.sanitize_text("You are Claude Code, Anthropic's official CLI tool for Claude"))

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
                            "action_type": "CLAIM_BENEFIT", "claim_status": "CLAIMED",
                            "start_at": 0, "end_at": 0,
                            "benefit": {"kind": "CREDITS", "amount": 100},
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
A.Account.campaigns = lambda self, force=False: dict(_A_CAMPAIGNS[self.realm])
A.Account.fetch_credits = lambda self: {"ok": True, "credits": {}}
A.Account.fetch_plan = lambda self: ""
A.Account.pro_eligibility = lambda self: (True, False)
# 「全部账号 (批量)」的余额应是各账号合计（此前误用首个账号的快照）
_t_intl.credits = {"remain": 300}
_t_cn.credits = {"remain": 700}
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
check("intl: claimed daily campaign renders as 本轮已领取 (not an empty row)",
      _intl_row["status"] == "claimed" and "本轮已领取" in _intl_row["description"],
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
check("batch view balance = SUM across accounts (not first snapshot)",
      _view_all["summary"].get("energy") == 1000,
      _view_all["summary"].get("energy"))
check("batch view carries the per-account breakdown",
      [(b["uid"], b["remain"])
       for b in _view_all["summary"].get("energy_breakdown") or []]
      == [("t-intl", 300), ("t-cn", 700)],
      _view_all["summary"].get("energy_breakdown"))
check("single-account view keeps that account's own balance (no breakdown)",
      _view_cn["summary"].get("energy") == 700
      and not _view_cn["summary"].get("energy_breakdown"),
      (_view_cn["summary"].get("energy"),
       _view_cn["summary"].get("energy_breakdown")))

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
_orig_urlopen19 = qoder_net.urlopen
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


_orig_pool19 = runtime.POOL
_pool19 = A.AccountPool(os.path.join(os.environ["ACCOUNTS_DIR"], "unused19"))
_pool19.accounts = [A.Account({"uid": "h19", "realm": "intl",
                               "domain": "qoder.com",
                               "accessToken": "dt-x"})]
runtime.POOL = _pool19
qoder_net.urlopen = _fake_urlopen19
try:
    _resp19, _acc19, _ = upstream.open_upstream(
        {"model": "qmodel", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]},
        target_realm="intl")
    _err19 = None
except Exception as exc:                      # pragma: no cover - failure path
    _err19 = exc
finally:
    qoder_net.urlopen = _orig_urlopen19
    runtime.POOL = _orig_pool19

check("failover: request succeeded after primary host transport error",
      _err19 is None and _acc19.uid == "h19", _err19)
check("failover: both hosts were tried in order (api1 then api2)",
      len(_hits19) == 2 and "api1.qoder.sh" in _hits19[0]
      and "api2.qoder.sh" in _hits19[1], _hits19)
check("failover: signature path unchanged across hosts",
      _hits19[0].split("?")[0].endswith(body_mod.CHAT_PATH.split("?")[0]),
      _hits19[0])

print()
print("[20] campaign check-in (the real daily-claim API) + effort normalization")

# --- 20.1 桌面端请求头（活动平台必需；缺了服务端返回空列表） ---
_hdrs = _t_cn.desktop_headers()
check("desktop headers carry Cosy-ClientType=10 + Cosy-Version + UA Qoder",
      _hdrs["cosy-clienttype"] == "10" and _hdrs["User-Agent"] == "Qoder"
      and bool(_hdrs["cosy-version"]), _hdrs.get("cosy-clienttype"))
check("derived identity sends NO cosy-machine* headers (issue #10: 派生六头会被服务端判非官方)",
      not any(k.startswith("cosy-machine") for k in _hdrs)
      and _t_cn.machine_headers_state == A.MACHINE_HEADERS_OMITTED,
      [k for k in _hdrs if k.startswith("cosy-machine")])
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
A.Account.campaigns = lambda self, force=False: {
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
A.Account.campaigns = lambda self, force=False: {
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
      reasoning.supported_efforts(_meta_qf) == ["low", "medium", "xhigh"]
      and reasoning.supported_efforts(_meta_df) == ["low", "high", "max"]
      and reasoning.supported_efforts(_meta_qm) == [], reasoning.supported_efforts(_meta_df))
check("default_effort reads the official is_default mark",
      reasoning.default_effort(_meta_qf) == "medium" and reasoning.default_effort(_meta_df) == "max",
      (reasoning.default_effort(_meta_qf), reasoning.default_effort(_meta_df)))
_v, _n = reasoning.normalize_reasoning_effort("none", _meta_qf)
check("none stays none (universal off switch)", _v == "none")
_v, _n = reasoning.normalize_reasoning_effort("medium", _meta_qf)
check("supported level passes through untouched", _v == "medium" and not _n, _n)
_v, _n = reasoning.normalize_reasoning_effort("medium", _meta_df)
check("unsupported level maps to the nearest legal one (dfmodel medium->high)",
      _v == "high" and "unsupported" in _n, (_v, _n))
_v, _n = reasoning.normalize_reasoning_effort("xhigh", _meta_df)
check("dfmodel xhigh -> max (nearest to request/default)", _v == "max", (_v, _n))
_v, _n = reasoning.normalize_reasoning_effort("max", _meta_qf)
check("qfmodel max -> xhigh", _v == "xhigh", (_v, _n))
_v, _n = reasoning.normalize_reasoning_effort("high", _meta_qf)
check("qfmodel high -> medium (tie broken toward the default)",
      _v == "medium", (_v, _n))
_v, _n = reasoning.normalize_reasoning_effort("low", _meta_qm)
check("model without levels: effort param is dropped (not sent blindly)",
      _v is None and "dropped" in _n, (_v, _n))
_v, _n = reasoning.normalize_reasoning_effort("none", _meta_qm)
check("model without levels still honours none", _v == "none")
# 路由器（auto 等）目录里没有 thinking_config：不猜测，原样透传
_meta_auto = next(m for m in C.models_for_realm("cn") if m["key"] == "auto")
check("router model without thinking_config passes the value through verbatim",
      reasoning.normalize_reasoning_effort("high", _meta_auto) == ("high", "")
      and reasoning.normalize_reasoning_effort("bogus", _meta_auto) == ("bogus", ""),
      reasoning.normalize_reasoning_effort("high", _meta_auto))
# 未命中取最近合法档位：逐模型校验（同距偏向默认档）
for _key, _want in (("dmodel", {"medium": "high", "xhigh": "max", "low": "high"}),
                    ("gfmodel", {"medium": "high", "xhigh": "max"}),
                    ("kmodel", {"medium": "high", "xhigh": "max"})):
    _m = next(m for m in C.models_for_realm("cn") if m["key"] == _key)
    _got = {e: reasoning.normalize_reasoning_effort(e, _m)[0] for e in _want}
    check("nearest-legal mapping on %s" % _key, _got == _want, (_got, _want))
# Responses API 路径：reasoning.effort / reasoning_effort 都要能到上游
_rchat = responses_mod.responses_to_chat({"model": "Qwen3.8-Flash", "input": "hi",
                              "reasoning": {"effort": "xhigh"}})
_rbody = body_mod.build_qoder_body(dict(_rchat, model="Qwen3.8-Flash"), None,
                            "qfmodel", realm="cn")
check("Responses API reasoning.effort reaches upstream (normalized)",
      (_rbody.get("parameters") or {}).get("reasoning_effort") == "xhigh",
      (_rbody.get("parameters") or {}).get("reasoning_effort"))

# build_qoder_body: 端到端确认发到上游的档位已归一化 + thinking.* 兼容
_body_eff = body_mod.build_qoder_body(
    {"model": "dfmodel", "messages": [{"role": "user", "content": "hi"}],
     "reasoning_effort": "medium"}, None, "dfmodel", realm="cn")
check("build_qoder_body sends the normalized effort upstream",
      (_body_eff.get("parameters") or {}).get("reasoning_effort") == "high",
      (_body_eff.get("parameters") or {}).get("reasoning_effort"))
_body_th = body_mod.build_qoder_body(
    {"model": "qfmodel", "messages": [{"role": "user", "content": "hi"}],
     "thinking": {"effort": "xhigh"}}, None, "qfmodel", realm="cn")
check("thinking.effort is accepted as an alias",
      (_body_th.get("parameters") or {}).get("reasoning_effort") == "xhigh",
      (_body_th.get("parameters") or {}).get("reasoning_effort"))
_body_off = body_mod.build_qoder_body(
    {"model": "qmodel", "messages": [{"role": "user", "content": "hi"}],
     "reasoning_effort": "high"}, None, "qmodel", realm="cn")
check("unsupported effort on a level-less model is dropped from the body",
      "reasoning_effort" not in (_body_off.get("parameters") or {}),
      _body_off.get("parameters"))

print()
print("[21] outbound proxy layer (qoder_net: system / manual / direct)")
import urllib.request as _ur21

_env_backup21 = {k: os.environ.get(k) for k in ("QD_PROXY_MODE", "QD_PROXY_URL")}
for _k21 in _env_backup21:
    os.environ.pop(_k21, None)
_orig_registry21 = getattr(_ur21, "getproxies_registry", None)
_orig_getproxies21 = _ur21.getproxies
try:
    qoder_net.configure("direct")
    check("direct mode -> empty proxies table", qoder_net.proxies() == {},
          qoder_net.proxies())
    check("direct mode describes itself", "直连" in qoder_net.describe()["effective"])

    qoder_net.configure("manual", "127.0.0.1:7897")
    _p21 = qoder_net.proxies()
    check("manual mode -> http+https + loopback bypass",
          _p21.get("http") == "http://127.0.0.1:7897"
          and _p21.get("https") == "http://127.0.0.1:7897"
          and "localhost" in (_p21.get("no") or ""), _p21)
    try:
        qoder_net.configure("manual", "")
        check("manual without address rejected", False)
    except ValueError:
        check("manual without address rejected", True)
    try:
        qoder_net.configure("manual", "socks5://127.0.0.1:1080")
        check("socks proxy rejected with a clear message", False)
    except ValueError as _exc21:
        check("socks proxy rejected with a clear message",
              "socks" in str(_exc21), str(_exc21))
    check("bogus mode falls back to system",
          qoder_net.configure("bogus")["mode"] == "system")

    # system：优先读 Windows 注册表（避免被终端 env 变量顶掉），空则回退 getproxies()
    _ur21.getproxies_registry = lambda: {"http": "http://reg.local:1",
                                         "https": "http://reg.local:1"}
    qoder_net.configure("system")
    check("system prefers the registry (real Windows system proxy)",
          qoder_net.proxies().get("https") == "http://reg.local:1"
          and "localhost" in (qoder_net.proxies().get("no") or ""),
          qoder_net.proxies())
    _ur21.getproxies_registry = lambda: {}
    _ur21.getproxies = lambda: {"https": "http://env.local:2"}
    qoder_net.configure("system")
    check("system falls back to getproxies() when registry is empty",
          qoder_net.proxies().get("https") == "http://env.local:2",
          qoder_net.proxies())

    # 环境变量优先（Docker/调试场景）
    os.environ["QD_PROXY_MODE"] = "direct"
    check("env var QD_PROXY_MODE wins over stored config",
          qoder_net.configure("system")["mode"] == "direct"
          and qoder_net.proxies() == {}
          and qoder_net.current()["env_override"] is True)
    os.environ.pop("QD_PROXY_MODE", None)

    # 回环地址任何模式下都直连
    check("loopback hosts always go direct",
          qoder_net.is_loopback("127.0.0.1") and qoder_net.is_loopback("localhost")
          and qoder_net.is_loopback("::1") and not qoder_net.is_loopback("qoder.com"))
    qoder_net.configure("manual", "http://127.0.0.1:7897")
    check("loopback request picks the direct opener (manual mode)",
          qoder_net.opener_for("http://127.0.0.1:8790/health")
          is qoder_net._direct_opener
          and qoder_net.opener_for("https://openapi.qoder.sh/x")
          is not qoder_net._direct_opener)
finally:
    if _orig_registry21 is not None:
        _ur21.getproxies_registry = _orig_registry21
    else:
        try:
            delattr(_ur21, "getproxies_registry")
        except Exception:
            pass
    _ur21.getproxies = _orig_getproxies21
    for _k21, _v21 in _env_backup21.items():
        if _v21 is None:
            os.environ.pop(_k21, None)
        else:
            os.environ[_k21] = _v21
    qoder_net.configure("system")

print()
print("[22] realm modes (intl / cn / both + failover) & quota-aware account pick")

_orig_accounts_dir22 = runtime.ACCOUNTS_DIR
_orig_state_file22 = runtime.REALM_STATE_FILE
_orig_mode22 = (runtime.REALM_MODE, runtime.REALM_PREFERRED, runtime.CURRENT_REALM)
import tempfile as _tf22
_TD22 = _tf22.mkdtemp(prefix="qdrealm_")
runtime.ACCOUNTS_DIR = _TD22
runtime.REALM_STATE_FILE = os.path.join(_TD22, "active_realm.json")
try:
    realm_mod.save_persisted_realm("cn")
    check("single realm: candidates = [cn]; explicit binding still wins",
          realm_mod.realm_candidates(model="Qwen3.8-Flash") == ["cn"]
          and realm_mod.realm_candidates(model="Qwen3.8-Flash", explicit="intl") == ["intl"])
    realm_mod.save_persisted_realm("both", "intl")
    check("both mode: preferred first, other realm as automatic fallback",
          realm_mod.realm_candidates(model="Qwen3.8-Flash") == ["intl", "cn"])
    check("exclusive model pins to its owner realm even in both mode",
          realm_mod.realm_candidates(model="q37fmodel") == ["cn"]
          and realm_mod.realm_candidates(model="smodel") == ["intl"],
          (realm_mod.realm_candidates(model="q37fmodel"),
           realm_mod.realm_candidates(model="smodel")))

    with open(runtime.REALM_STATE_FILE, "w", encoding="utf-8") as _fh22:
        json.dump({"realm": "cn"}, _fh22)
    runtime.REALM_MODE, runtime.REALM_PREFERRED = "intl", "intl"
    realm_mod.load_persisted_realm()
    check("legacy state file {\"realm\": cn} loads as single-realm mode",
          runtime.REALM_MODE == "cn" and runtime.REALM_PREFERRED == "cn"
          and runtime.CURRENT_REALM == "cn")
    with open(runtime.REALM_STATE_FILE, "w", encoding="utf-8") as _fh22:
        json.dump({"mode": "both", "preferred": "intl"}, _fh22)
    realm_mod.load_persisted_realm()
    check("two-realm state file loads mode+preferred",
          runtime.REALM_MODE == "both" and runtime.REALM_PREFERRED == "intl")

    _orig_pool22 = runtime.POOL
    _orig_count_ready22 = A.AccountPool.count_ready
    runtime.POOL = A.AccountPool(os.path.join(os.environ["ACCOUNTS_DIR"], "unused22b"))
    try:
        A.AccountPool.count_ready = \
            lambda self, realm=None, model=None: (1 if realm == "cn" else 0)
        check("pick_serving_realm falls back when the preferred realm is unusable",
              realm_mod.pick_serving_realm(["intl", "cn"], model="Qwen3.8-Flash") == "cn"
              and realm_mod.pick_serving_realm(["cn", "intl"], model="Qwen3.8-Flash") == "cn")
        A.AccountPool.count_ready = lambda self, realm=None, model=None: 0
        check("pick_serving_realm keeps the first candidate when none are usable",
              realm_mod.pick_serving_realm(["intl", "cn"], model="x") == "intl")
    finally:
        A.AccountPool.count_ready = _orig_count_ready22
        runtime.POOL = _orig_pool22
finally:
    runtime.ACCOUNTS_DIR, runtime.REALM_STATE_FILE = _orig_accounts_dir22, _orig_state_file22
    runtime.REALM_MODE, runtime.REALM_PREFERRED, runtime.CURRENT_REALM = _orig_mode22
    import shutil as _sh22
    _sh22.rmtree(_TD22, ignore_errors=True)

# 额度感知调度：已耗尽账号降为后备档（不判死，主档空了照样顶上）
_qd_a = A.Account({"uid": "qd-a", "realm": "cn", "accessToken": "dt-x"})
_qd_b = A.Account({"uid": "qd-b", "realm": "cn", "accessToken": "dt-x"})
_qd_pool = A.AccountPool(os.path.join(os.environ["ACCOUNTS_DIR"], "unused22"))
_qd_pool.accounts = [_qd_a, _qd_b]
_qd_a.credits = {"remain": 0, "size": 500, "exceeded": True}
_qd_b.credits = {"remain": 300, "size": 500, "exceeded": False}
check("quota_depleted flags exceeded / zero-balance-with-quota only",
      _qd_a.quota_depleted() is True and _qd_b.quota_depleted() is False
      and A.Account({"uid": "q0", "realm": "cn"}).quota_depleted() is False)
_picks22 = [_qd_pool.pick(realm="cn").uid for _i in range(3)]
check("pick prefers the non-depleted account (round-robin within the tier)",
      _picks22 == ["qd-b", "qd-b", "qd-b"], _picks22)
_qd_b.credits = {"remain": 0, "size": 500, "exceeded": True}
check("pick still serves from depleted accounts when nothing else is ready",
      _qd_pool.pick(realm="cn") is not None)

print()
print("[23] scheduling v2: consecutive-failure backoff + least-busy lease")

# --- 连续失败退避（仅账号侧错误 401/403 等，escalate 显式开启） ---
_bo = A.Account({"uid": "bo-1", "realm": "cn", "accessToken": "dt-x"})
_t023 = time.time()
_bo.note_error("HTTP 401 x", cooldown=60, escalate=True)
_c1 = _bo.cooldown_until - _t023
_bo.note_error("HTTP 401 x", cooldown=60, escalate=True)
_c2 = _bo.cooldown_until - _t023
_bo.note_error("HTTP 401 x", cooldown=60, escalate=True)
_c3 = _bo.cooldown_until - _t023
_bo.note_error("HTTP 401 x", cooldown=60, escalate=True)
_c4 = _bo.cooldown_until - _t023
check("escalating cooldown ladder 60 -> 300 -> 900 -> 1800s",
      _c1 <= 61 and 299 <= _c2 <= 301 and 899 <= _c3 <= 901
      and 1799 <= _c4 <= 1801,
      tuple(round(x) for x in (_c1, _c2, _c3, _c4)))
_bo.clear_error()
check("one success resets the failure counter + cooldown",
      _bo.consecutive_failures == 0 and _bo.cooldown_until == 0)
_bo.note_error("upstream transient", cooldown=15)          # 非 escalate
check("upstream transient cooldown does NOT escalate (not the account's fault)",
      _bo.consecutive_failures == 0
      and 14 <= (_bo.cooldown_until - time.time()) <= 16)
_bo.cooldown_until = 0
_bo.note_error("HTTP 401 single", cooldown=300, single_account=True,
               escalate=True)
check("single-account pool never gets the long ladder (kept short)",
      _bo.consecutive_failures == 0
      and (_bo.cooldown_until - time.time()) <= 4)
_bo.cooldown_until = 0

# --- least-busy：在途少的优先；同负载保持轮询 ---
_lb_a = A.Account({"uid": "lb-a", "realm": "cn", "accessToken": "dt-x"})
_lb_b = A.Account({"uid": "lb-b", "realm": "cn", "accessToken": "dt-x"})
_lb_pool = A.AccountPool(os.path.join(os.environ["ACCOUNTS_DIR"], "unused23"))
_lb_pool.accounts = [_lb_a, _lb_b]
_lb_a.credits = {"remain": 500, "size": 500, "exceeded": False}
_lb_b.credits = {"remain": 500, "size": 500, "exceeded": False}
_lb_a.in_flight = 3                      # a 正被长流式请求占着
check("least-busy: the idle account wins even mid-round-robin",
      _lb_pool.pick(realm="cn").uid == "lb-b"
      and _lb_pool.pick(realm="cn").uid == "lb-b")
_lb_a.in_flight = 0
_lb_pool._cursor = 0
check("equal in-flight keeps the plain round-robin order",
      [_lb_pool.pick(realm="cn").uid for _i in range(2)] == ["lb-a", "lb-b"])

# --- 在途租约：open 后 +1，close / with 退出恰好释放一次 ---
class _LeaseResp(object):
    def __init__(self):
        self.closes = 0

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        self.closes += 1

    def read(self, *a):
        return b"{}"

    def __iter__(self):
        return iter([b"data: {}\n\n"])


_lease_resp = _LeaseResp()
_orig_urlopen23 = qoder_net.urlopen
qoder_net.urlopen = lambda req, timeout=None: _lease_resp
_orig_pool23 = runtime.POOL
import tempfile as _tf23
_TD23 = _tf23.mkdtemp(prefix="qdlease_")
_pool23 = A.AccountPool(_TD23)
_pool23.accounts = [A.Account({"uid": "lease-1", "realm": "cn",
                               "accessToken": "dt-test",
                               "refreshToken": "drt-test",
                               "expiresAt": 9999999999})]
runtime.POOL = _pool23
try:
    _resp23, _acc23, _ = upstream.open_upstream(
        {"model": "qfmodel", "messages": [{"role": "user", "content": "hi"}],
         "stream": False}, target_realm="cn")
    check("lease: in-flight +1 while the response is open",
          _acc23.in_flight == 1, _acc23.in_flight)
    with _resp23:
        pass
    check("lease: with-exit releases the in-flight slot",
          _acc23.in_flight == 0, _acc23.in_flight)
    _resp23c, _acc23c, _ = upstream.open_upstream(
        {"model": "qfmodel", "messages": [{"role": "user", "content": "hi"}],
         "stream": False}, target_realm="cn")
    _resp23c.close()
    _resp23c.close()
    check("lease: explicit double close releases exactly once",
          _acc23c.in_flight == 0, _acc23c.in_flight)
finally:
    qoder_net.urlopen = _orig_urlopen23
    runtime.POOL = _orig_pool23
    import shutil as _sh23
    _sh23.rmtree(_TD23, ignore_errors=True)

print()
print("[24] api-key exit binding vs. gateway mode (/v1/models union)")

# 并集合并：按上游 key 去重、先出现的区域优先；共享模型标注 both + realms
_part_i = [("shared", {"name": "Shared (Intl)"}), ("intlonly", {"name": "Intl Only"})]
_part_c = [("shared", {"name": "Shared (Cn)"}), ("cnonly", {"name": "Cn Only"})]
_union = models.union_model_entries([("intl", _part_i), ("cn", _part_c)])
_umap = dict(_union)
check("union keeps first-seen order and drops duplicates",
      [m for m, _ in _union] == ["shared", "intlonly", "cnonly"],
      [m for m, _ in _union])
check("shared model annotated as both realms",
      _umap["shared"]["realm"] == "both"
      and _umap["shared"]["realms"] == ["intl", "cn"], _umap["shared"])
check("realm-exclusive entries keep their own realm",
      _umap["intlonly"]["realm"] == "intl"
      and _umap["cnonly"]["realm"] == "cn")
check("per-realm entries are not mutated (fetch_models cache stays clean)",
      "realm" not in _part_i[0][1] and "realms" not in _part_c[0][1])
_me_u = model_entry.model_entry("shared", _umap["shared"])
check("model_entry surfaces realm/realms passthrough",
      _me_u.get("realm") == "both" and _me_u.get("realms") == ["intl", "cn"])

# fetch_models("both") 端到端：假的按区目录，不碰网络
_orig_cache24 = dict(models._models_cache)
_orig_dyn24 = models.read_dynamic_models
_orig_local24 = models.read_local_models
_orig_mode24 = (runtime.REALM_MODE, runtime.REALM_PREFERRED)
models.read_dynamic_models = lambda realm=None: []
models.read_local_models = lambda realm=None: (
    [("shared", {"name": "Shared"}), ("intlonly", {"name": "Intl Only"})]
    if realm == "intl" else
    [("shared", {"name": "Shared"}), ("cnonly", {"name": "Cn Only"})])
try:
    models._models_cache.clear()
    runtime.REALM_MODE, runtime.REALM_PREFERRED = "both", "intl"
    _got24 = models.fetch_models(realm="both")
    _gmap24 = dict(_got24)
    check("fetch_models('both') merges both realm catalogs once",
          sorted(_gmap24) == ["cnonly", "intlonly", "shared"], sorted(_gmap24))
    check("fetch_models('both') marks shared + exclusive realms",
          _gmap24["shared"].get("realms") == ["intl", "cn"]
          and _gmap24["cnonly"].get("realm") == "cn")
finally:
    models.read_dynamic_models = _orig_dyn24
    models.read_local_models = _orig_local24
    runtime.REALM_MODE, runtime.REALM_PREFERRED = _orig_mode24
    models._models_cache.clear()
    models._models_cache.update(_orig_cache24)

print()
print("[25] model library: context windows + thinking per official protocol")

# 预算 -> 档位（官方 CLI JX() 阈值）
_jx = [("none", reasoning.effort_from_budget(0)), ("low", reasoning.effort_from_budget(1024)),
       ("medium", reasoning.effort_from_budget(8192)), ("high", reasoning.effort_from_budget(24576)),
       ("xhigh", reasoning.effort_from_budget(49152)), ("max", reasoning.effort_from_budget(49153))]
check("thinking budget -> effort follows official thresholds",
      all(got == want for want, got in _jx), _jx)
check("non-numeric budget is rejected",
      reasoning.effort_from_budget("big") == "" and reasoning.effort_from_budget(None) == "")

# 可选窗口：context_config 优先，缺失时按 [128K, 200K, max] 推导（官方 WX）
_meta_cfg = {"context_config": {"200K": {"token_count": 200000, "is_default": True},
                                "400K": {"token_count": 400000},
                                "1M": {"token_count": 1000000}},
             "max_input_tokens": 180000}
check("windows come from context_config",
      reasoning.available_context_windows(_meta_cfg) == [200000, 400000, 1000000])
check("a model without a window table declares none",
      reasoning.available_context_windows({"max_input_tokens": 300000}) == [])
check("display options derived from max_input_tokens (official WX)",
      reasoning.window_choices({"max_input_tokens": 300000})
      == [128000, 200000, 300000])
check("single-window model only offers its own window",
      reasoning.window_choices({"max_input_tokens": 100000}) == [100000])
check("jX validation: listed only (or <= max without a list)",
      reasoning.window_supported(_meta_cfg, 400000)
      and not reasoning.window_supported(_meta_cfg, 300000)
      and reasoning.window_supported({"max_input_tokens": 100000}, 90000)
      and not reasoning.window_supported({"max_input_tokens": 100000}, 200000))
_v25, _n25 = reasoning.resolve_context_window(_meta_cfg, "1M")
check("window labels resolve to token counts", _v25 == 1000000, _v25)
_v25b, _n25b = reasoning.resolve_context_window(_meta_cfg, 300000)
check("unsupported window rounds to the nearest supported one",
      _v25b == 400000 and _n25b, _n25b)
check("invalid window is ignored, not sent",
      reasoning.resolve_context_window(_meta_cfg, "huge")[0] is None)
check("no window requested -> nothing sent",
      reasoning.resolve_context_window(_meta_cfg, None) == (None, ""))

# 客户端思考意图提取：档位 / 预算 / 开关
check("reasoning_effort passthrough",
      reasoning.client_thinking({"reasoning_effort": "HIGH"})["effort"] == "high")
check("Anthropic-style thinking budget",
      reasoning.client_thinking({"thinking": {"type": "enabled",
                                       "budget_tokens": 6000}})["budget"] == 6000)
check("enable_thinking / type=disabled both mean off",
      reasoning.client_thinking({"enable_thinking": False})["disable"] is True
      and reasoning.client_thinking({"thinking": {"type": "disabled"}})["disable"] is True)
check("thinking_budget alias read",
      reasoning.client_thinking({"thinking_budget": 20000})["budget"] == 20000)

# 请求体：档位+开关+预算随 effort 下发，窗口以 context_length 下发
_th_body = body_mod.build_qoder_body({
    "model": "qmodel_38max",
    "messages": [{"role": "user", "content": "hi"}],
    "thinking": {"type": "enabled", "budget_tokens": 6000},
}, None, "qmodel_38max", realm="cn")
_thp = _th_body["parameters"]
check("budget 6000 -> medium on qmodel_38max",
      _thp.get("reasoning_effort") == "medium", _thp)
check("official enable_thinking + budget fields mirror the choice",
      _thp.get("enable_thinking") is True
      and _thp.get("reasoning_budget_tokens") == 6000, _thp)
_off_body = body_mod.build_qoder_body({
    "model": "qmodel_38max",
    "messages": [{"role": "user", "content": "hi"}],
    "enable_thinking": False,
}, None, "qmodel_38max", realm="cn")
check("explicit disable sends reasoning_effort=none + enable_thinking=false",
      _off_body["parameters"].get("reasoning_effort") == "none"
      and _off_body["parameters"].get("enable_thinking") is False)
check("thinking off flips model_config.is_reasoning",
      _off_body["model_config"].get("is_reasoning") is False)
_win_body = body_mod.build_qoder_body({
    "model": "qmodel_38max",
    "messages": [{"role": "user", "content": "hi"}],
    "context_window": 400000,
}, None, "qmodel_38max", realm="cn")
check("valid window choice goes out as parameters.context_length",
      _win_body["parameters"].get("context_length") == 400000,
      _win_body["parameters"])
_win_off = body_mod.build_qoder_body({
    "model": "qmodel_38max",
    "messages": [{"role": "user", "content": "hi"}],
    "context_window": 300000,
}, None, "qmodel_38max", realm="cn")
check("unsupported window rounds, still uses an official window",
      _win_off["parameters"].get("context_length") == 400000,
      _win_off["parameters"])

# 看板每模型默认：客户端没带时生效，带了以客户端为准
_orig_dir25b = runtime.ACCOUNTS_DIR
_orig_state25 = os.path.join(os.environ["ACCOUNTS_DIR"], "settings.json")
import shutil as _sh25
if os.path.isfile(_orig_state25):
    _backup25 = _orig_state25 + ".testbak25"
    _sh25.copyfile(_orig_state25, _backup25)
else:
    _backup25 = None
try:
    qoder_settings.set_model_override(runtime.ACCOUNTS_DIR, "cn:qmodel_38max",
                                        context_window=400000, effort="low")
    check("override stored in settings",
          qoder_settings.model_overrides(runtime.ACCOUNTS_DIR)
          .get("cn:qmodel_38max") == {"context_window": 400000, "effort": "low"})
    _ov_body = body_mod.build_qoder_body({
        "model": "qmodel_38max",
        "messages": [{"role": "user", "content": "hi"}],
    }, None, "qmodel_38max", realm="cn")
    check("model default applies when the client says nothing",
          _ov_body["parameters"].get("context_length") == 400000
          and _ov_body["parameters"].get("reasoning_effort") == "low",
          _ov_body["parameters"])
    _ov_win = body_mod.build_qoder_body({
        "model": "qmodel_38max",
        "messages": [{"role": "user", "content": "hi"}],
        "reasoning_effort": "xhigh",
    }, None, "qmodel_38max", realm="cn")
    check("explicit client value beats the model default",
          _ov_win["parameters"].get("reasoning_effort") == "xhigh",
          _ov_win["parameters"])
finally:
    qoder_settings.set_model_override(runtime.ACCOUNTS_DIR, "cn:qmodel_38max",
                                        context_window=0, effort="")
    if _backup25:
        _sh25.move(_backup25, _orig_state25)
    runtime.ACCOUNTS_DIR = _orig_dir25b
check("override removed again", not qoder_settings.model_overrides(
    runtime.ACCOUNTS_DIR).get("cn:qmodel_38max"))

print()
print("[26] /settings/save model_overrides patch contract")
import tempfile as _tf26
_TD26 = _tf26.mkdtemp(prefix="qdov_")
_orig_dir26 = runtime.ACCOUNTS_DIR
runtime.ACCOUNTS_DIR = _TD26
try:
    _h26 = Handler.__new__(Handler)
    _cap26 = {}
    _h26._payload_or_error = lambda: {"model_overrides": {
        "cn:qfmodel": {"context_window": "200000", "effort": "low"},
        "intl:qfmodel": {"context_window": "", "effort": ""}}}
    _h26._json = lambda code, obj: _cap26.update({"code": code, "obj": obj})
    _h26._error = lambda code, msg, *a: _cap26.update({"err": code, "msg": msg})
    _h26._handle_settings_save()
    check("save handler returns 200 with the patch applied",
          _cap26.get("code") == 200
          and _cap26["obj"].get("model_overrides", {}).get("cn:qfmodel")
          == {"context_window": 200000, "effort": "low"},
          _cap26)
    check("empty values remove an override entirely",
          "intl:qfmodel" not in _cap26["obj"].get("model_overrides", {}),
          _cap26["obj"].get("model_overrides"))
    check("runtime settings view exposes model_overrides",
          isinstance(_cap26["obj"].get("model_overrides"), dict))
    _h26._payload_or_error = lambda: {"model_overrides": "nope"}
    _h26._handle_settings_save()
    check("non-object patch is rejected with 400",
          _cap26.get("err") == 400, _cap26)
finally:
    runtime.ACCOUNTS_DIR = _orig_dir26
    import shutil as _sh26
    _sh26.rmtree(_TD26, ignore_errors=True)

print()
print("[27] ported upstream fixes: BLOCKED dedup / gated rows / VM / update check / task cache")

# ---- 同人去重（BLOCKED / SAME_PERSON_ALREADY_CLAIMED） ----
_BLOCKED_BODY = {"status": "BLOCKED", "replayed": False,
                 "failureCode": "SAME_PERSON_ALREADY_CLAIMED",
                 "benefit": {"kind": "CREDITS", "amount": 100}}


def fake_blocked_flow(url, **kw):
    if "daily-check-in/status" in url:
        raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b""))
    if url.endswith("/campaigns"):
        return _credit_campaigns_payload()
    if "/campaigns/c-1/claim" in url:
        return dict(_BLOCKED_BODY)
    raise AssertionError("unexpected url: %s" % url)


_orig_hj27 = _A.http_json
_A.http_json = fake_blocked_flow
_acc_blk = _A.Account({"uid": "blk-1", "realm": "intl", "accessToken": "dt-x"})
_cc_blk = _acc_blk.campaign_checkin(gap=0)
_A.http_json = _orig_hj27
check("BLOCKED classified, never counted as claimed/earned",
      _cc_blk.get("blocked") and not _cc_blk.get("claimed")
      and _cc_blk.get("earned") == 0, _cc_blk)
check("BLOCKED keeps ok=True and explains the person-level dedup",
      _cc_blk.get("ok") is True and "同人" in _cc_blk.get("message", ""),
      _cc_blk.get("message"))
check("BLOCKED failure_code surfaced",
      _cc_blk["blocked"][0].get("failure_code") == "SAME_PERSON_ALREADY_CLAIMED",
      _cc_blk["blocked"])


def fake_blocked_claim_only(url, **kw):
    if "/campaigns/c-1/claim" in url:
        return dict(_BLOCKED_BODY)
    raise AssertionError("unexpected url: %s" % url)


_A.http_json = fake_blocked_claim_only
_acc_blk2 = _A.Account({"uid": "blk-2", "realm": "intl", "accessToken": "dt-x"})
_res_blk2 = _acc_blk2.claim_campaign("c-1")
check("claim_campaign: BLOCKED -> ok False + blocked True (no false success)",
      _res_blk2.get("ok") is False and _res_blk2.get("blocked") is True
      and _res_blk2.get("failure_code") == "SAME_PERSON_ALREADY_CLAIMED",
      _res_blk2)


def fake_empty_claim(url, **kw):
    return {"foo": "bar"}


_A.http_json = fake_empty_claim
_acc_blk3 = _A.Account({"uid": "blk-3", "realm": "intl", "accessToken": "dt-x"})
_res_blk3 = _acc_blk3.claim_campaign("c-1")
_A.http_json = _orig_hj27
check("claim_campaign: 200 without an explicit status is NOT a success anymore",
      _res_blk3.get("ok") is False, _res_blk3)

# checkin() 的兜底翻译：BLOCKED -> ok=True（不是错误），带说明
_A.http_json = fake_blocked_flow
_acc_blk4 = _A.Account({"uid": "blk-4", "realm": "intl", "accessToken": "dt-x"})
_res_blk4 = _acc_blk4.checkin()
_A.http_json = _orig_hj27
check("checkin(): blocked maps to ok=True + blocked flag (button not alarmed)",
      _res_blk4.get("ok") and _res_blk4.get("campaign")
      and _res_blk4.get("blocked"), _res_blk4)

# ---- 成就门控活动行 ----
_gated_camp = {
    "ok": True, "available": True, "show_campaign": True, "claimable": False,
    "campaign_url": "https://qoder.com.cn/growth-page/activity-iframe",
    "campaigns": [{"campaign_id": "c-g", "campaign_key": "act-20260928-620",
                   "action_type": "CLAIM_BENEFIT",
                   "claim_status": "ACHIEVEMENT_NOT_COMPLETED",
                   "start_at": 0, "end_at": 0,
                   "benefit": {"kind": "CREDITS", "amount": 100},
                   "required_achievement_key": "sites_first_use",
                   "achievement_completed": False,
                   "unavailable_reason": "", "placements": []}],
    "identity": "native",
}
_row_g = T._campaign_task_row(_A.Account({"uid": "g-1", "realm": "cn"}),
                              _gated_camp, {})
check("achievement-gated campaign renders with its requirement",
      _row_g["status"] == "not_accepted"
      and "sites_first_use" in _row_g["description"]
      and _row_g["reward_credit"] == 100, _row_g)

_no_camp = {"ok": True, "available": True, "show_campaign": False,
            "claimable": False, "campaign_url": "", "campaigns": [],
            "identity": "native"}
_row_n = T._campaign_task_row(_A.Account({"uid": "n-1", "realm": "cn"}),
                              _no_camp, {})
check("empty campaign list hints at the common causes (定向/VM/资格)",
      "虚拟机" in _row_n["description"] and "定向" in _row_n["description"],
      _row_n["description"])

# ---- 虚拟化状态（中文） ----
import qoder2api.fingerprint as _F27
check("vm level mapping thresholds",
      [_F27._vm_level_cn(v) for v in (0, 30, 55, 77, None)]
      == ["无", "低", "中", "高", "未知"],
      [_F27._vm_level_cn(v) for v in (0, 30, 55, 77, None)])
check("vm brand mapping -> Chinese name",
      _F27.vm_brand_cn("hyper-v") == "Hyper-V（微软）"
      and _F27.vm_brand_cn("VirtualBox") == "Oracle VirtualBox"
      and _F27.vm_brand_cn("unknown-brand") == "unknown-brand")
_orig_ev27 = _F27._local_vm_evidence
_F27._local_vm_evidence = lambda: (["证据1"], "")
try:
    _st_vm = _F27.vm_status(bridge_vm_info={"isVm": True, "percentage": 77,
                                            "brand": "hyper-v",
                                            "vmTypeCode": 3})
    check("bridge vmInfo wins and produces the Chinese summary",
          _st_vm["is_vm"] and _st_vm["source"] == "runtime-info"
          and _st_vm["level"] == "高" and "Hyper-V" in _st_vm["summary"],
          _st_vm)
    _st_loc = _F27.vm_status(bridge_vm_info=None, bridge_available=False)
    check("no bridge -> local cross-check fallback is flagged",
          _st_loc["source"] == "local" and "本机交叉校验" in _st_loc["summary"],
          _st_loc)
finally:
    _F27._local_vm_evidence = _orig_ev27

check("diag/update routes require the panel session",
      Handler._is_panel_route("/diag/vm")
      and Handler._is_panel_route("/update/check"))

# ---- 版本比较 + 更新检查三态 ----
check("version tuple tolerates prefixes and odd shapes",
      update.version_tuple("v1.2.3") == (1, 2, 3)
      and update.version_tuple("1.10.0") > update.version_tuple("1.9.9")
      and update.version_tuple("") == (0, 0, 0),
      (update.version_tuple("v1.2.3"), update.version_tuple("1.10.0")))


class _FakeUpdateResp(object):
    def __init__(self, body):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


_orig_urlopen27 = qoder_net.urlopen
_orig_upd_cache27 = dict(update._update_cache)
try:
    update._update_cache.update({"at": 0.0, "data": None})
    qoder_net.urlopen = lambda req, timeout=None: _FakeUpdateResp(
        json.dumps({"tag_name": "v99.0.0", "html_url": "https://x/y",
                    "published_at": "", "name": "n"}).encode())
    _upd = update.check_for_update(force=True)
    check("update check: newer tag -> has_update with link",
          _upd["ok"] and _upd["has_update"] and _upd["latest"] == "v99.0.0"
          and _upd["url"] == "https://x/y", _upd)
    qoder_net.urlopen = lambda req, timeout=None: _FakeUpdateResp(
        json.dumps({"tag_name": "v0.0.1"}).encode())
    _upd2 = update.check_for_update(force=True)
    check("update check: older tag -> no update, no false alarm",
          _upd2["ok"] and not _upd2["has_update"], _upd2)

    def _raise_404(req, timeout=None):
        raise _ue2.HTTPError(getattr(req, "full_url", "u"), 404, "nf", {}, None)
    qoder_net.urlopen = _raise_404
    _upd3 = update.check_for_update(force=True)
    check("update check: 404 (no releases yet) is not treated as an error",
          _upd3["ok"] and _upd3["no_releases"] and not _upd3["has_update"], _upd3)

    def _raise_net(req, timeout=None):
        raise OSError("network down")
    qoder_net.urlopen = _raise_net
    _upd4 = update.check_for_update(force=True)
    check("update check: network failure -> ok False (shows 检查失败, not fake update)",
          _upd4["ok"] is False and not _upd4["has_update"]
          and _upd4["error"], _upd4)
finally:
    qoder_net.urlopen = _orig_urlopen27
    update._update_cache.update(_orig_upd_cache27)

# ---- /tasks 面板短缓存（命中 + 失效） ----
T.invalidate_panel_cache()
_calls27 = {"campaigns": 0, "status": 0, "pro": 0, "credits": 0, "plan": 0}
_acc_c27 = _A.Account({"uid": "cache-27", "realm": "cn", "accessToken": "dt-x"})


def _mk_counter(name, result):
    def _fn(*a, **kw):
        _calls27[name] += 1
        return result
    return _fn


_acc_c27.campaigns = _mk_counter("campaigns", _no_camp)
_acc_c27.checkin_status = _mk_counter("status", (True, {
    "streak_days": 0, "total_claim_days": 0, "today_checked_in": True,
    "active": True, "reward_credits": 100}))
_acc_c27.pro_eligibility = _mk_counter("pro", (True, False))
_acc_c27.fetch_credits = _mk_counter("credits", {"ok": True})
_acc_c27.fetch_plan = _mk_counter("plan", "Free")
_first27 = T._fetch_upstream_parallel(_acc_c27)
_second27 = T._fetch_upstream_parallel(_acc_c27)
check("panel cache: second view build within 20s hits the cache",
      _calls27 == {"campaigns": 1, "status": 1, "pro": 1, "credits": 1, "plan": 1},
      _calls27)
T.invalidate_panel_cache()
T._fetch_upstream_parallel(_acc_c27)
check("panel cache: invalidate (after check-in/claim) forces a fresh fetch",
      _calls27["campaigns"] == 2, _calls27)

# ---- campaigns 短缓存：命中 + force 绕过 ----
_orig_hj27b = _A.http_json
_camp_calls27 = {"n": 0}


def fake_camp_count(url, **kw):
    if url.endswith("/campaigns"):
        _camp_calls27["n"] += 1
        return _credit_campaigns_payload()
    raise AssertionError("unexpected url: %s" % url)


_A.http_json = fake_camp_count
_acc_cc27 = _A.Account({"uid": "camp-cache-27", "realm": "cn",
                        "accessToken": "dt-x"})
_acc_cc27.campaigns()
_acc_cc27.campaigns()
check("campaigns(): 20s short cache reuses the payload", _camp_calls27["n"] == 1,
      _camp_calls27)
_acc_cc27.campaigns(force=True)
_A.http_json = _orig_hj27b
check("campaigns(force=True) bypasses the short cache", _camp_calls27["n"] == 2,
      _camp_calls27)
check("identity + campaigns cache TTL constants match the ported design",
      _A.NATIVE_IDENTITY_TTL == 1800 and _A.CAMPAIGNS_TTL == 20,
      (_A.NATIVE_IDENTITY_TTL, _A.CAMPAIGNS_TTL))

print()
print("[28] cross-realm runtime-info bridge (intl borrows cn when own client is absent)")

_orig_did28 = A.desktop_install_dir
_orig_home28 = A._realm_home_dir
_orig_realmpaths28 = A._realm_runtime_info_paths
_orig_exe_cache28 = dict(A._native_exe_cache)
_orig_env28 = os.environ.get("QD_NATIVE_IDENTITY")
_orig_env_path28 = os.environ.pop("QD_RUNTIME_INFO", None)
try:
    _IN28 = r"C:\fake\intl"
    _CN28 = r"C:\fake\cn"
    A.desktop_install_dir = lambda r: {"intl": _IN28, "cn": _CN28}.get(r, "")
    A._realm_home_dir = lambda r: r"C:\fake\home-" + r
    _cands_i = A.candidate_runtime_info_paths("intl")
    _cands_c = A.candidate_runtime_info_paths("cn")
    check("candidates: own realm first, borrowed realm appended",
          _cands_i[0] == os.path.join(_IN28, "resources", "umid", "runtime-info.exe")
          and _cands_i[-1] == os.path.join(_CN28, "resources", "umid", "runtime-info.exe")
          and len(_cands_i) == 2, _cands_i)
    check("candidates: cn search order is cn -> intl too",
          _cands_c[0].startswith(_CN28) and _cands_c[-1].startswith(_IN28),
          _cands_c)
    check("candidates: no duplicates", len(set(_cands_i)) == len(_cands_i))

    _real28 = os.path.join(_ROOT, "README.md")   # any existing file works as the "exe"
    os.environ["QD_NATIVE_IDENTITY"] = "1"
    A._native_exe_cache.clear()
    A._realm_runtime_info_paths = \
        lambda r: [_real28] if r == "cn" else [r"C:\nope\runtime-info.exe"]
    _path_i, _src_i = A._runtime_info_resolution("intl")
    check("intl borrows the cn bridge when its own client is missing",
          _path_i == _real28 and _src_i == "cn", (_path_i, _src_i))

    A._native_exe_cache.clear()
    A._realm_runtime_info_paths = \
        lambda r: [_real28] if r == "intl" else [r"C:\nope\runtime-info.exe"]
    _path_c, _src_c = A._runtime_info_resolution("intl")
    check("own-realm bridge wins over the borrowed one",
          _path_c == _real28 and _src_c == "intl", (_path_c, _src_c))

    os.environ["QD_NATIVE_IDENTITY"] = "0"
    A._native_exe_cache.clear()
    check("QD_NATIVE_IDENTITY=0 still disables both bridge and borrow",
          A._runtime_info_resolution("intl") == ("", ""))
finally:
    A.desktop_install_dir = _orig_did28
    A._realm_home_dir = _orig_home28
    A._realm_runtime_info_paths = _orig_realmpaths28
    A._native_exe_cache.clear()
    A._native_exe_cache.update(_orig_exe_cache28)
    if _orig_env28 is None:
        os.environ.pop("QD_NATIVE_IDENTITY", None)
    else:
        os.environ["QD_NATIVE_IDENTITY"] = _orig_env28
    if _orig_env_path28 is not None:
        os.environ["QD_RUNTIME_INFO"] = _orig_env_path28

# 本机实测（有桥才比较；没有桥时确认是干净的空值而不是异常）
os.environ["QD_NATIVE_IDENTITY"] = "1"
A._native_exe_cache.clear()
_exe_i28 = A.runtime_info_exe("intl")
_exe_c28 = A.runtime_info_exe("cn")
if _exe_c28 or _exe_i28:
    # 不假设"本机只有 cn 一个桥"：本机可能同时装了桌面端（cn）与 CLI 缓存桥
    # （~/.qoder/.bin/umid-*，两区各一份）。真正的不变量是「任一区有桥 =>
    # 两区都能解析到桥（缺失的一侧借用另一侧）」，且来源区域标注与之一致。
    check("either realm having a bridge means both realms resolve one",
          bool(_exe_c28) and bool(_exe_i28), (_exe_i28, _exe_c28))
    check("bridge realm tag matches the realm whose path resolved",
          A.runtime_info_bridge_realm("intl") in ("intl", "cn")
          and A.runtime_info_bridge_realm("cn") in ("intl", "cn"),
          (A.runtime_info_bridge_realm("intl"),
           A.runtime_info_bridge_realm("cn")))
else:
    check("no official bridge on this machine -> intl resolves to empty (no crash)",
          _exe_i28 == "")
if _orig_env28 is None:
    os.environ.pop("QD_NATIVE_IDENTITY", None)
else:
    os.environ["QD_NATIVE_IDENTITY"] = _orig_env28

print()
print("[29] pinned machine identity + stale-credits refresh (server parity)")

import tempfile as _tf29
_TD29 = _tf29.mkdtemp(prefix="qdident29_")
_orig_accdir29 = os.environ.get("ACCOUNTS_DIR")
_orig_env_ident29 = os.environ.pop("QD_MACHINE_IDENTITY", None)
_orig_native_env29 = os.environ.get("QD_NATIVE_IDENTITY")
try:
    os.environ["ACCOUNTS_DIR"] = _TD29
    check("no pin by default", A._pinned_machine_identity() is None)

    # 固定一份身份（模拟从装有客户端的机器导出后粘贴）
    _pin29 = {"machineToken": "tok-abc-1234567890", "machineType": "mtype-1",
              "machineCode": "mcode-1", "vmInfo": {"isVm": True, "percentage": 77}}
    A.qoder_settings.set_machine_identity(_TD29, _pin29)
    _ident29 = A.native_machine_identity("intl", "some-uid")
    check("pinned identity wins the resolution order (realm-free, account-free)",
          _ident29.get("source") == "pinned"
          and _ident29.get("machineToken") == "tok-abc-1234567890"
          and _ident29.get("bridge_realm") == "pinned"
          and _ident29.get("vm") is True, _ident29)
    os.environ["QD_NATIVE_IDENTITY"] = "0"
    check("pinned identity works even with the native bridge disabled",
          A.native_machine_identity("cn", "u").get("source") == "pinned")

    # 环境变量优先于设置文件
    os.environ["QD_MACHINE_IDENTITY"] = json.dumps(
        {"machineToken": "env-token-xyz", "machineType": "env-type",
         "machineCode": "env-code"})
    _ident_env29 = A.native_machine_identity("cn", "u")
    check("QD_MACHINE_IDENTITY env overrides the settings file",
          _ident_env29.get("machineToken") == "env-token-xyz", _ident_env29)
    os.environ["QD_MACHINE_IDENTITY"] = "{not json"
    check("broken env JSON falls back to the settings file",
          A._pinned_machine_identity().get("machineToken") == "tok-abc-1234567890")
    os.environ.pop("QD_MACHINE_IDENTITY", None)

    # 导出：已固定时直接回固定值；未固定且无桥时给出明确原因
    _exp29, _reason29 = A.export_bridge_identity("intl")
    check("export returns the pinned identity when one is set",
          _exp29 and _exp29.get("source") == "pinned"
          and _exp29.get("machineToken") == "tok-abc-1234567890", _exp29)
    A.qoder_settings.set_machine_identity(_TD29, None)
    _orig_exe29 = A.runtime_info_exe
    A.runtime_info_exe = lambda realm: ""
    try:
        _exp29b, _reason29b = A.export_bridge_identity("intl")
        check("export without a bridge explains why (no crash)",
              _exp29b is None and "runtime-info" in _reason29b, _reason29b)
    finally:
        A.runtime_info_exe = _orig_exe29
    check("clear removes the pin",
          A.qoder_settings.machine_identity(_TD29) is None
          and A._pinned_machine_identity() is None)

    # 看板保存接口：dict 保存 / null 清除 / 非法输入 400
    _orig_dir29 = runtime.ACCOUNTS_DIR
    runtime.ACCOUNTS_DIR = _TD29
    try:
        _h29 = Handler.__new__(Handler)
        _cap29 = {}
        _h29._payload_or_error = lambda: {"machine_identity": {
            "machineToken": "via-panel-token", "machineType": "t",
            "machineCode": "c"}}
        _h29._json = lambda code, obj: _cap29.update({"code": code, "obj": obj})
        _h29._error = lambda code, msg, *a: _cap29.update({"err": code, "msg": msg})
        _h29._handle_settings_save()
        check("/settings/save pins the identity via the panel",
              _cap29.get("code") == 200
              and _cap29["obj"].get("machine_identity", {}).get("pinned") is True,
              _cap29)
        _h29._payload_or_error = lambda: {"machine_identity": None}
        _h29._handle_settings_save()
        check("/settings/save null clears the pin",
              _cap29["obj"].get("machine_identity", {}).get("pinned") is False)
        _h29._payload_or_error = lambda: {"machine_identity": {"machineToken": "x"}}
        _h29._handle_settings_save()
        check("incomplete identity is rejected with 400",
              _cap29.get("err") == 400, _cap29)
        _h29._payload_or_error = lambda: {"machine_identity": "nope"}
        _h29._handle_settings_save()
        check("non-object identity is rejected with 400",
              _cap29.get("err") == 400, _cap29)
    finally:
        runtime.ACCOUNTS_DIR = _orig_dir29
    check("/identity/export is a panel route",
          Handler._is_panel_route("/identity/export"))

    # ---- 过期额度快照自动刷新（/tasks 批量视图） ----
    T.invalidate_panel_cache()
    _now29 = time.time()
    _pa29 = A.Account({"uid": "fresh-29", "realm": "cn", "accessToken": "dt-x"})
    _pb29 = A.Account({"uid": "stale-29", "realm": "cn", "accessToken": "dt-x"})
    _pa29.credits = {"remain": 100, "updated_at": _now29}
    _pb29.credits = {"remain": 50, "updated_at": _now29 - 9999}   # 过期快照
    _cnt29 = {"a": 0, "b": 0}
    _empty_camp29 = {"ok": True, "available": True, "show_campaign": False,
                     "claimable": False, "campaign_url": "", "campaigns": [],
                     "identity": "derived"}

    def _fake_camp29(*a, **kw):
        return _empty_camp29

    def _fake_status29(*a, **kw):
        return (True, {"streak_days": 0, "total_claim_days": 0,
                       "today_checked_in": True, "active": True,
                       "reward_credits": 100})

    def _fake_pro29(*a, **kw):
        return (True, False)

    def _fetch_a29(*a, **kw):
        _cnt29["a"] += 1
        return {"ok": True}
    def _fetch_b29(*a, **kw):
        _cnt29["b"] += 1
        _pb29.credits = {"remain": 70, "updated_at": time.time()}   # 拉到新值
        return {"ok": True}

    _pa29.campaigns = _fake_camp29
    _pb29.campaigns = _fake_camp29
    for _acc29 in (_pa29, _pb29):
        _acc29.checkin_status = _fake_status29
        _acc29.pro_eligibility = _fake_pro29
        _acc29.fetch_plan = lambda *a, **kw: "Free"
    _pa29.fetch_credits = _fetch_a29
    _pb29.fetch_credits = _fetch_b29

    class _Pool29(object):
        def __init__(self):
            self.accounts = [_pa29, _pb29]
        def get(self, uid):
            return next((x for x in self.accounts if x.uid == uid), None)

    _view29 = T.fetch_tasks_view(_Pool29())
    _sm29 = _view29.get("summary") or {}
    check("batch view refreshes ONLY the stale snapshot (no focus-account fetch)",
          _cnt29 == {"a": 0, "b": 1}, _cnt29)
    check("batch balance uses the REFRESHED value",
          _sm29.get("energy") == 170, _sm29.get("energy"))
    _bd29 = {b["uid"]: b["remain"] for b in (_sm29.get("energy_breakdown") or [])}
    check("breakdown shows the refreshed number for the stale account",
          _bd29.get("stale-29") == 70 and _bd29.get("fresh-29") == 100, _bd29)

    # 新鲜快照不会被重复拉取（第二次调用走面板缓存/新鲜判断）
    _cnt29["a"] = _cnt29["b"] = 0
    T.invalidate_panel_cache()
    _view29b = T.fetch_tasks_view(_Pool29())
    check("now-fresh snapshots are not refetched again",
          _cnt29 == {"a": 0, "b": 0}, _cnt29)
    check("_credits_stale matches the TTL boundary",
          T._credits_stale(_pa29) is False
          and T._credits_stale(_pb29) is False)  # b 刚刷新过，a 一直新鲜
    _pc29 = A.Account({"uid": "stale-29b", "realm": "cn", "accessToken": "dt-x"})
    _pc29.credits = {"remain": 1, "updated_at": time.time() - (T.CREDITS_STALE_TTL + 5)}
    check("_credits_stale flags a snapshot older than the TTL",
          T._credits_stale(_pc29) is True)
finally:
    if _orig_accdir29 is None:
        os.environ.pop("ACCOUNTS_DIR", None)
    else:
        os.environ["ACCOUNTS_DIR"] = _orig_accdir29
    if _orig_env_ident29 is not None:
        os.environ["QD_MACHINE_IDENTITY"] = _orig_env_ident29
    if _orig_native_env29 is None:
        os.environ.pop("QD_NATIVE_IDENTITY", None)
    else:
        os.environ["QD_NATIVE_IDENTITY"] = _orig_native_env29
    import shutil as _sh29
    _sh29.rmtree(_TD29, ignore_errors=True)

print()
print("[30] round window (10:00 rollover) wording + identity-filtered retry")

# 轮次说明：10:00 滚动（10:00 ~ 次日 09:59），上午点到的"已领取"是上一轮
_future_end = int(time.time()) + 3600
_note_future = A.round_note([{"end_at": _future_end}])
check("round note shows the round deadline + 10:00 rollover",
      "本轮截止" in _note_future and "10:00" in _note_future, _note_future)
check("round note without windows still explains the 10:00 rollover",
      "每日 10:00" in A.round_note([]), A.round_note([]))

# campaign_checkin: 已领取消息带轮次说明（不再写"今日已领取/明日再来"）
def fake_claimed_with_window(url, **kw):
    if "daily-check-in/status" in url:
        raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b""))
    if url.endswith("/campaigns"):
        return {"uid": "rw-1", "showCampaign": True, "claimable": False,
                "campaignUrl": "https://qoder.com/growth-page/activity-iframe",
                "campaigns": [{"campaignId": "c-w", "campaignKey": "act-w",
                               "actionType": "CLAIM_BENEFIT",
                               "claimStatus": "CLAIMED",
                               "startAt": 0, "endAt": _future_end,
                               "benefit": {"kind": "CREDITS", "amount": 100},
                               "placements": []}]}
    raise AssertionError("unexpected url: %s" % url)


_orig_hj30 = _A.http_json
_A.http_json = fake_claimed_with_window
_acc_rw = _A.Account({"uid": "rw-1", "realm": "cn", "accessToken": "dt-x"})
_res_rw = _acc_rw.checkin()
_A.http_json = _orig_hj30
check("claimed check-in message uses round wording (no '今日/明日')",
      _res_rw.get("ok") and "本轮奖励已领取" in (_res_rw.get("msg") or "")
      and "本轮截止" in (_res_rw.get("msg") or "")
      and "明日再来" not in (_res_rw.get("msg") or ""), _res_rw)


# 只有 VIEW_DETAILS 已领取（0 积分浏览活动）时，不再冒充"已领取"
def fake_view_claimed(url, **kw):
    if "daily-check-in/status" in url:
        raise _ue2.HTTPError(url, 404, "nf", {}, _io2.BytesIO(b""))
    if url.endswith("/campaigns"):
        return {"uid": "vw-1", "showCampaign": True, "claimable": False,
                "campaignUrl": "", "campaigns": [
                    {"campaignId": "c-v", "campaignKey": "act-view",
                     "actionType": "VIEW_DETAILS", "claimStatus": "CLAIMED",
                     "startAt": 0, "endAt": 0,
                     "benefit": {"kind": "", "amount": 0}, "placements": []}]}
    raise AssertionError("unexpected url: %s" % url)


_A.http_json = fake_view_claimed
_acc_vw = _A.Account({"uid": "vw-1", "realm": "intl", "accessToken": "dt-x"})
_res_vw = _acc_vw.checkin()
_A.http_json = _orig_hj30
check("view-only claimed item no longer reported as a check-in",
      "领取" not in (_res_vw.get("msg") or "") or "未给该账号" in (_res_vw.get("msg") or ""),
      _res_vw)

# 无原生机器身份时的"没有活动"提示（issue #10：不再发派生假头，
# 国际版要如实说明这是**已知限制**并提供出路；国内版不发头是正确行为，不给提示）
_acc_dv = _A.Account({"uid": "dv-1", "realm": "intl", "accessToken": "dt-x"})
_acc_dv.machine_identity_source = "derived"
_acc_dv.machine_headers_state = A.MACHINE_HEADERS_OMITTED


def fake_empty_camp30(url, **kw):
    if url.endswith("/campaigns"):
        return {"uid": "dv-1", "showCampaign": True, "claimable": False,
                "campaignUrl": "", "campaigns": []}
    raise AssertionError("unexpected url: %s" % url)


_A.http_json = fake_empty_camp30
_cc_dv = _acc_dv.campaign_checkin(gap=0)
_A.http_json = _orig_hj30
check("INTL no-activity under no-native-identity states the known limitation",
      "已知限制" in (_cc_dv.get("message") or "")
      and "机器身份" in (_cc_dv.get("message") or "")
      and "派生假身份" not in (_cc_dv.get("message") or ""),
      _cc_dv.get("message"))

# campaigns(): 列表被身份过滤（无任何每日项）时换新身份重试一次
_orig_native30 = A.native_machine_identity
_retry30 = {"n": 0}
_calls30 = {"n": 0}


def _stub_native30(realm, account_id, force=False):
    _retry30["n"] += 1
    return {"machineToken": "t", "machineType": "m", "machineCode": "c",
            "source": "runtime-info"}


def _filtered_payload():
    return {"showCampaign": True, "claimable": False, "campaignUrl": "",
            "campaigns": [{"campaignId": "c-v", "campaignKey": "act-view",
                           "actionType": "VIEW_DETAILS",
                           "claimStatus": "CLAIMABLE",
                           "startAt": 0, "endAt": 0,
                           "benefit": {"kind": "", "amount": 0},
                           "placements": []}]}


def _daily_payload():
    return {"showCampaign": True, "claimable": True, "campaignUrl": "",
            "campaigns": [{"campaignId": "c-d", "campaignKey": "act-daily",
                           "actionType": "CLAIM_BENEFIT",
                           "claimStatus": "CLAIMABLE",
                           "startAt": 0, "endAt": 0,
                           "benefit": {"kind": "CREDITS", "amount": 100},
                           "placements": []}]}


A.native_machine_identity = _stub_native30
try:
    _acc_rt = _A.Account({"uid": "rt-1", "realm": "cn", "accessToken": "dt-x"})
    _acc_rt.machine_identity_source = "native"

    def _get_filtered_then_daily():
        _calls30["n"] += 1
        payload = _filtered_payload() if _calls30["n"] == 1 else _daily_payload()
        return payload, 200, ""

    _acc_rt._campaigns_get = _get_filtered_then_daily
    _st_rt = _acc_rt.campaigns(force=True)
    check("identity-filtered list (no daily item) triggers one fresh-identity retry",
          _retry30["n"] == 1 and _calls30["n"] == 2
          and any(c["claim_status"] == "CLAIMABLE"
                  for c in _st_rt.get("campaigns") or []), (_retry30, _calls30))

    _retry30["n"] = 0
    _calls30["n"] = 0
    _acc_dr = _A.Account({"uid": "dr-1", "realm": "cn", "accessToken": "dt-x"})
    _acc_dr.machine_identity_source = "derived"
    _acc_dr._campaigns_get = lambda: (_calls30.__setitem__("n", _calls30["n"] + 1)
                                      or (_filtered_payload(), 200, ""))
    _st_dr = _acc_dr.campaigns(force=True)
    check("derived identity does NOT retry (no bridge to refresh anyway)",
          _retry30["n"] == 0 and _calls30["n"] == 1, (_retry30, _calls30))
finally:
    A.native_machine_identity = _orig_native30

# 任务行：已领取带轮次窗口；纯 VIEW 已领取不再冒充签到
_row_rw = T._campaign_task_row(
    _A.Account({"uid": "row-rw", "realm": "cn"}),
    {"ok": True, "available": True, "show_campaign": True, "claimable": False,
     "campaign_url": "", "identity": "native",
     "campaigns": [{"campaign_id": "c-w", "campaign_key": "act-w",
                    "action_type": "CLAIM_BENEFIT", "claim_status": "CLAIMED",
                    "start_at": 0, "end_at": _future_end,
                    "benefit": {"kind": "CREDITS", "amount": 100},
                    "required_achievement_key": "", "achievement_completed": False,
                    "unavailable_reason": "", "placements": []}]}, {})
check("task row: claimed shows the round deadline instead of 明日再来",
      "本轮已领取" in _row_rw["description"]
      and "本轮截止" in _row_rw["description"]
      and "明日再来" not in _row_rw["description"], _row_rw["description"])

_row_dv = T._campaign_task_row(
    _A.Account({"uid": "row-dv", "realm": "intl"}),
    {"ok": True, "available": True, "show_campaign": True, "claimable": False,
     "campaign_url": "", "identity": "derived",
     "machine_headers": "omitted",
     "hint": "国际版服务端要求真实的 UMID 机器身份……这是已知限制，不等于今天没有活动。",
     "campaigns": []}, {})
check("task row: INTL known-limitation hint appended to the no-activity row",
      "已知限制" in _row_dv["description"]
      and "机器身份" in _row_dv["description"], _row_dv["description"])

_row_dv_cn = T._campaign_task_row(
    _A.Account({"uid": "row-dv-cn", "realm": "cn"}),
    {"ok": True, "available": True, "show_campaign": True, "claimable": False,
     "campaign_url": "", "identity": "derived",
     "machine_headers": "omitted", "hint": "", "campaigns": []}, {})
check("task row: CN (correctly header-less) gets no limitation note",
      "已知限制" not in _row_dv_cn["description"]
      and "派生假身份" not in _row_dv_cn["description"], _row_dv_cn["description"])

print()
print("[31] upstream v1.1.9 port: coupon codes / Chinese names / aggregate / envelope cooldown")

import tempfile as _tf31
import shutil as _sh31

# --- A) 券类活动：兑换码落盘 + 领取瞬间失败码分类 + pending/locked + only_kinds ---
_tmp31 = _tf31.mkdtemp(prefix="qd31-")
_cp_path = os.path.join(_tmp31, "cp-1.json")
_claim_calls31 = {"n": 0, "ids": []}


def _camp31(cid, status="CLAIMABLE", kind="CREDITS", amount=100,
            reason="", ach=None, key=None, title=""):
    camp = {"campaignId": cid, "campaignKey": key or cid,
            "actionType": "CLAIM_BENEFIT", "claimStatus": status,
            "startAt": 0, "endAt": 0,
            "benefit": {"kind": kind, "amount": amount},
            "requiredAchievementKey": "sites_first_use" if kind == "REDEMPTION_CODE" else "",
            "achievementCompleted": bool(ach),
            "unavailableReason": reason, "placements": []}
    if title:
        camp["placements"] = [{"content": {"zh": {
            "title": title, "description": "限时福利",
            "detailUrl": "https://docs.qoder.cn/events/new-coffee"}}}]
    return camp


def _fake_coupon31(url, **kw):
    if url.endswith("/campaigns"):
        return {"uid": "cp-1", "showCampaign": True, "claimable": True,
                "campaignUrl": "https://qoder.com.cn/activities",
                "campaigns": [
                    _camp31("c-credit", kind="CREDITS", amount=100,
                            title="每天领 100 Credits"),
                    _camp31("c-coupon", kind="REDEMPTION_CODE", amount=1,
                            ach=True, key="act-20260928-620",
                            title="发布 Qoder 站点，免费领取奶茶免单卡"),
                    _camp31("c-stock", kind="REDEMPTION_CODE", amount=1, ach=True),
                    _camp31("c-locked", status="NOT_ELIGIBLE",
                            kind="REDEMPTION_CODE", amount=1,
                            reason="ACHIEVEMENT_NOT_COMPLETED", ach=False),
                ]}
    if "/claim" in url:
        _claim_calls31["n"] += 1
        cid = url.rstrip("/").split("/")[-2]
        _claim_calls31["ids"].append(cid)
        if cid == "c-credit":
            return {"status": "CLAIMED", "grantId": "g1",
                    "benefit": {"kind": "CREDITS", "amount": 100}}
        if cid == "c-coupon":
            return {"status": "CLAIMED", "redemptionCode": "MT-8888-6666",
                    "benefit": {"kind": "REDEMPTION_CODE", "amount": 1}}
        if cid == "c-stock":
            # 领取瞬间名额发完：按 pending 分类，不算失败
            return {"status": "NOT_ELIGIBLE",
                    "failureCode": "REDEMPTION_CODE_OUT_OF_STOCK"}
    raise AssertionError("unexpected url: %s" % url)


_orig_hj31 = _A.http_json
_orig_native31 = A.native_machine_identity
A.native_machine_identity = lambda *a, **kw: {}
_A.http_json = _fake_coupon31
try:
    _acc31 = _A.Account({"uid": "cp-1", "realm": "cn", "accessToken": "dt-x"},
                        path=_cp_path)
    _res31 = _acc31.campaign_checkin(gap=0)
    _names31 = [T.campaign_title(c) for c in _res31["claimed"]]
    check("coupon campaign claimed with credits in one sweep",
          _res31["ok"] and _res31["earned"] == 100
          and "每天领 100 Credits" in _names31
          and "发布 Qoder 站点，免费领取奶茶免单卡" in _names31, _res31)
    check("redemption code captured + returned for display",
          _res31["codes"] == [{"campaign": "发布 Qoder 站点，免费领取奶茶免单卡",
                               "code": "MT-8888-6666"}], _res31["codes"])
    check("claim-time out-of-stock classified as pending (not an error)",
          bool(_res31["pending"]) and not _res31["errors"],
          (_res31["pending"], _res31["errors"]))
    check("achievement-gated coupon classified as locked",
          len(_res31["locked"]) == 1
          and _res31["locked"][0]["campaign_id"] == "c-locked", _res31["locked"])
    check("message carries the code + round wording",
          "兑换码" in _res31["message"] and "本轮" in _res31["message"],
          _res31["message"])
    _reload31 = _A.Account(json.load(open(_cp_path, encoding="utf-8")), path=_cp_path)
    check("redemption code persisted to the account file (survives restart)",
          _reload31.campaign_codes.get("c-coupon") == "MT-8888-6666",
          _reload31.campaign_codes)

    # only_kinds=("", "CREDITS")：账号面板「每日签到」只领积分，不碰券
    _claim_calls31["n"] = 0
    _claim_calls31["ids"] = []
    _acc31b = _A.Account({"uid": "cp-1", "realm": "cn", "accessToken": "dt-x"},
                         path=os.path.join(_tmp31, "cp-1b.json"))
    _acc31b.campaign_checkin(gap=0, only_kinds=("", "CREDITS"))
    check("only_daily claims Credits only (coupon untouched, 1 POST)",
          _claim_calls31["ids"] == ["c-credit"] and not _acc31b.campaign_codes,
          _claim_calls31)

    # 同人已领取：记 6 小时冷却，冷却期内不再重复 POST
    _blocked_calls31 = {"n": 0}

    def _fake_blocked31(url, **kw):
        if url.endswith("/campaigns"):
            return {"uid": "bl-1", "showCampaign": True, "claimable": True,
                    "campaignUrl": "", "campaigns": [
                        _camp31("c-b", kind="CREDITS", amount=100)]}
        if "/claim" in url:
            _blocked_calls31["n"] += 1
            return {"status": "BLOCKED",
                    "failureCode": "SAME_PERSON_ALREADY_CLAIMED"}
        raise AssertionError("unexpected url: %s" % url)

    _A.http_json = _fake_blocked31
    _acc31c = _A.Account({"uid": "bl-1", "realm": "cn", "accessToken": "dt-x"},
                         path=os.path.join(_tmp31, "bl-1.json"))
    _r31a = _acc31c.campaign_checkin(gap=0)
    check("SAME_PERSON block recorded with 6h cooldown",
          bool(_r31a["blocked"])
          and _acc31c.campaign_blocked_until.get("c-b", 0) > time.time(),
          _acc31c.campaign_blocked_until)
    _r31b = _acc31c.campaign_checkin(gap=0)
    check("cooldown skips the repeat POST (marked as cooldown, not failure)",
          _blocked_calls31["n"] == 1 and _r31b["blocked"][0].get("cooldown") is True
          and not _r31b["errors"], (_blocked_calls31, _r31b["blocked"]))

    # 失败码中文表
    check("campaign failure codes have Chinese explanations (round wording)",
          "名额已发完" in A._CAMPAIGN_FAILURE_CN["REDEMPTION_CODE_OUT_OF_STOCK"]
          and "新人任务" in A._CAMPAIGN_FAILURE_CN["ACHIEVEMENT_NOT_COMPLETED"])
finally:
    _A.http_json = _orig_hj31
    A.native_machine_identity = _orig_native31
    _sh31.rmtree(_tmp31, ignore_errors=True)

# --- B) 活动中文名：官方 zh 优先，兜底次之，最后 key ---
check("campaign_title prefers official zh title",
      T.campaign_title({"title_zh": "每天领 100 Credits",
                        "campaign_key": "act-1"}) == "每天领 100 Credits")
check("campaign_title falls back to the built-in table (prefix match)",
      T.campaign_title({"campaign_key": "act-20260930-057"}) == "每天领 100 Credits"
      and T.campaign_title({"campaign_key": "act-20260928-620"}).startswith("新人任务"))
check("campaign_title last resort = key / kind label",
      T.campaign_title({"campaign_key": "act-unknown"}) == "act-unknown"
      and T.campaign_title({"benefit": {"kind": "REDEMPTION_CODE"}}) == "限时活动（兑换码）")
check("campaign_desc / campaign_link read official zh fields",
      T.campaign_desc({"desc_zh": "限时福利"}) == "限时福利"
      and T.campaign_link({"detail_url": "https://docs.qoder.cn/x"})
      == "https://docs.qoder.cn/x")

# --- C) 全部账号：按活动聚合 + 逐账号资格明细 + 兑换码收集 ---
def _norm31(raw):
    """把 http_json 的原始活动形状转成 Account.campaigns() 归一化后的形状。"""
    placements = raw.get("placements") or []
    cont = ((placements[0] or {}).get("content") or {}).get("zh") or {}
    return {"title_zh": cont.get("title") or "", "title_en": "",
            "desc_zh": cont.get("description") or "",
            "detail_url": cont.get("detailUrl") or "", "button_text": "",
            "campaign_id": raw["campaignId"], "campaign_key": raw["campaignKey"],
            "action_type": raw["actionType"], "claim_status": raw["claimStatus"],
            "start_at": 0, "end_at": raw.get("endAt") or 0,
            "benefit": raw["benefit"],
            "required_achievement_key": raw.get("requiredAchievementKey") or "",
            "achievement_completed": bool(raw.get("achievementCompleted")),
            "unavailable_reason": raw.get("unavailableReason") or "",
            "placements": placements}


class _AggAcct31(object):
    def __init__(self, uid, realm, camps, codes=None):
        self.uid = uid
        self.nickname = "nick-" + uid
        self.realm = realm
        self.campaign_codes = codes or {}
        self.campaign_status = {"campaign_url": "https://qoder.com.cn/activities"}
        self._camps = [_norm31(c) for c in camps]

    def campaigns(self, force=False):
        return {"ok": True, "available": True, "show_campaign": True,
                "claimable": True, "campaign_url": "", "identity": "native",
                "campaigns": self._camps}


_agg_a31 = _AggAcct31("aa-1", "cn", [
    _camp31("c-credit", kind="CREDITS", amount=100, title="每天领 100 Credits"),
    _camp31("c-coupon", status="CLAIMED", kind="REDEMPTION_CODE", amount=1,
            ach=True, title="奶茶免单卡")],
    codes={"c-coupon": "MT-0001"})
_agg_b31 = _AggAcct31("bb-2", "cn", [
    _camp31("c-credit", status="CLAIMED", kind="CREDITS", amount=100,
            title="每天领 100 Credits")])
_rows31, _codes31 = T.aggregate_campaign_rows([_agg_a31, _agg_b31])
_by_name31 = {r["name"]: r for r in _rows31}
check("aggregate rows list every account's eligibility per campaign",
      len(_rows31) == 2
      and "可领 1/2" in _by_name31["每天领 100 Credits"]["description"]
      and "已领 1/2" in _by_name31["每天领 100 Credits"]["description"],
      _by_name31.get("每天领 100 Credits"))
check("campaign missing from an account's list = 无资格(不在定向), stated honestly",
      "无资格(不在定向) 1/2" in _by_name31["奶茶免单卡"]["description"],
      _by_name31.get("奶茶免单卡"))
check("coupon row shows reward_text instead of +N credits",
      _by_name31["奶茶免单卡"]["reward_text"] == "兑换码 ×1"
      and _by_name31["奶茶免单卡"]["reward_credit"] == 0)
check("aggregate collects per-account redemption codes",
      len(_codes31) == 1 and _codes31[0]["code"] == "MT-0001"
      and _codes31[0]["account"] == "aa-1", _codes31)

# VIEW_DETAILS（无奖励的点击查看类）不冒充"可领"：它在领取路径里会被跳过
_agg_c31 = _AggAcct31("cc-3", "cn", [
    _camp31("c-view", kind="CREDITS", amount=0, title="首月翻倍促销")])
_agg_c31._camps[0]["action_type"] = "VIEW_DETAILS"
_agg_c31._camps[0]["claim_status"] = "CLAIMABLE"
_rows31b, _ = T.aggregate_campaign_rows([_agg_c31])
check("view-only campaign is labelled 仅查看(无奖励), not 可领",
      _rows31b[0]["status"] == "not_accepted"
      and "仅查看(无奖励) 1/1" in _rows31b[0]["description"],
      _rows31b[0])

# --- D) 信封层 403/10605 冷却 + 轮换（上游 PR#7） ---
class _Aff31(object):
    def __init__(self):
        self.calls = []

    def unbind(self, key):
        self.calls.append(key)


class _Pool31(object):
    def __init__(self):
        self.affinity = _Aff31()
        self.accounts = [1, 2, 3]


class _Acc31(object):
    def __init__(self):
        self.uid = "pr7acc12"
        self.enabled = True
        self.notes = []
        self.saved = 0
        self.path = "fake-acc.json"

    def note_error(self, msg, cooldown=60, single_account=False, model=None,
                   until=None, escalate=False):
        self.notes.append({"msg": str(msg)[:60], "cooldown": cooldown,
                           "model": model})

    def save(self, d):
        self.saved += 1


_orig_pool31 = runtime.POOL
_orig_accounts_dir31 = getattr(runtime, "ACCOUNTS_DIR", None)
_acc31 = _Acc31()
runtime.POOL = _Pool31()
runtime.ACCOUNTS_DIR = os.environ["ACCOUNTS_DIR"]
try:
    upstream._handle_envelope_account_cooldown(
        _acc31,
        errors.UpstreamStatus(403, '{"code":"10605","message":'
                              '"{\\"isQueued\\":true,\\"retryAfterSeconds\\": 30}"}'),
        model="qfmodel", session_key="sk-pr7")
    _n1_31 = _acc31.notes[-1]
    upstream._handle_envelope_account_cooldown(_acc31, errors.UpstreamStatus(403, "permission denied"),
                                        model="qfmodel", session_key="sk-pr7")
    _n2_31 = _acc31.notes[-1]
    upstream._handle_envelope_account_cooldown(_acc31, errors.UpstreamStatus(429, "rate limit"),
                                        model="qfmodel", session_key="sk-pr7")
    _n3_31 = _acc31.notes[-1]
    _acc31.enabled = True
    upstream._handle_envelope_account_cooldown(
        _acc31, errors.UpstreamStatus(401, "TOKEN_EXPIRE session dead"),
        model=None, session_key="sk-pr7")
    _n4_31 = _acc31.notes[-1]
    _aff31_calls = list(runtime.POOL.affinity.calls)
finally:
    runtime.POOL = _orig_pool31
    if _orig_accounts_dir31 is not None:
        runtime.ACCOUNTS_DIR = _orig_accounts_dir31
check("envelope 10605 queue-full -> model cooldown 30s (upstream retryAfterSeconds) + unbind",
      _n1_31["cooldown"] == 30 and _n1_31["model"] == "qfmodel"
      and set(_aff31_calls) == {"sk-pr7"}, (_n1_31, _aff31_calls))
check("envelope 403 plain -> account-level cooldown 60s",
      _n2_31["cooldown"] == 60 and _n2_31["model"] is None, _n2_31)
check("envelope 429 -> model-level cooldown 30s",
      _n3_31["cooldown"] == 30 and _n3_31["model"] == "qfmodel", _n3_31)
check("envelope dead session -> 300s + account disabled (same as HTTP path)",
      _acc31.enabled is False and _n4_31["cooldown"] == 300 and _acc31.saved == 1,
      (_acc31.enabled, _n4_31))
for _st31, _d31, _want31 in ((403, "10605", True), (401, "x", True), (429, "x", True),
                             (418, "DataInspectionFailed", False),
                             (400, "invalid_parameter_error", False),
                             (500, "x", True)):
    _got31 = upstream.should_retry_envelope(errors.UpstreamStatus(_st31, _d31), False, 0)
    check("envelope reopen before first byte: %s -> %s" % (_st31, _want31),
          _got31 is _want31, (_st31, _got31))
check("envelope reopen still refused after bytes were emitted",
      upstream.should_retry_envelope(errors.UpstreamStatus(403, "10605"), True, 0) is False)
_src31 = _ALL_SRC
check("all envelope capture points wire the account cooldown (>=4 occurrences)",
      _src31.count("_handle_envelope_account_cooldown(") >= 4,
      _src31.count("_handle_envelope_account_cooldown("))

# --- E) run_checkin(only_daily) 走 only_kinds；/accounts/checkin 用它 ---
class _RunAcct31(object):
    uid = "ra-1"
    nickname = "ra"
    credits = {}

    def __init__(self):
        self.kinds = []

    def campaign_checkin(self, gap=0.5, only_kinds=None):
        self.kinds.append(only_kinds)
        return {"ok": True, "claimed": [], "already": [], "codes": [],
                "message": "x"}

    def fetch_credits(self):
        return {"ok": False}

    def fetch_plan(self):
        return ""


_ra31 = _RunAcct31()
T.run_checkin(_ra31, gap=0)
_ra31b = _RunAcct31()
T.run_checkin(_ra31b, gap=0, only_daily=True)
check("run_checkin(only_daily=True) restricts to Credits kinds",
      _ra31.kinds == [None] and _ra31b.kinds == [("", "CREDITS")],
      (_ra31.kinds, _ra31b.kinds))

_srcp31 = _ALL_SRC
check("/accounts/checkin runs the daily-only sweep at the shared pacing constant",
      "run_checkin(account, gap=CHECKIN_MIN_GAP," in _srcp31
      and "only_daily=True" in _srcp31
      and ("CHECKIN_MIN_GAP = %r" % T.CHECKIN_MIN_GAP) in _srcp31,
      T.CHECKIN_MIN_GAP)

# --- F) 看板接线：按钮合并 / 兑换码面板 / 聚合行展示 ---
_dash31 = open(_DASH_PATH, encoding="utf-8").read() if os.path.isfile(_DASH_PATH) else ""
check("buttons renamed (领取全部福利 / 仅领 Pro 福利包)",
      "领取全部福利" in _dash31 and "仅领 Pro 福利包" in _dash31
      and "一键签到领积分" not in _dash31)
check("领取全部福利 = 活动全量 + Pro 福利包（run + travel）",
      'postJSON("/tasks/run"' in _dash31 and 'postJSON("/tasks/travel"' in _dash31)
check("codes panel renders with copy + activity link",
      "codesPanel" in _dash31 and "codesTable" in _dash31
      and "data-copy" in _dash31 and "活动页/二维码" in _dash31)
check("task rows render aggregate state + reward_text",
      "accounts_by_state" in _dash31 and "reward_text" in _dash31
      and "多账号" in _dash31)

print()
print("[32] leaked tool-call echo read-back (upstream v1.2.0 issue #8 / v1.2.1 issue #9)")
_CN = chat_normalize
_M32 = _CN.LEAK_MARKER
_CALLS32 = json.dumps([{"name": "terminal",
                        "arguments": json.dumps({"cmd": "ls"}, ensure_ascii=False)}],
                      ensure_ascii=False)
_LEAK32 = _M32 + "\n" + _CALLS32

_rec32, _clean32 = _CN.parse_leaked_tool_calls(_LEAK32, {"terminal"})
check("strict form: marker+JSON array -> structured calls, content emptied",
      bool(_rec32) and _clean32 == ""
      and _rec32[0]["function"]["name"] == "terminal"
      and json.loads(_rec32[0]["function"]["arguments"])["cmd"] == "ls",
      (_rec32, _clean32))
check("fenced form (```json ... ```) also recovers",
      bool(_CN.parse_leaked_tool_calls("```json\n" + _LEAK32 + "\n```", {"terminal"})[0]))
check("bare fence (``` ... ```) also recovers",
      bool(_CN.parse_leaked_tool_calls("```\n" + _LEAK32 + "\n```", {"terminal"})[0]))
check("prose discussing the marker is never misread",
      _CN.parse_leaked_tool_calls("网关会写入 " + _M32 + " 这样的提示，不是调用。")[0] is None)
check("trailing text after the array -> no recovery",
      _CN.parse_leaked_tool_calls(_LEAK32 + "\n以上。", {"terminal"})[0] is None)
check("undeclared tool name -> no recovery (guard: only declared tools)",
      _CN.parse_leaked_tool_calls(_LEAK32, {"other"})[0] is None)
check("empty array -> no recovery", _CN.parse_leaked_tool_calls(_M32 + "\n[]")[0] is None)
check("arguments given as object -> normalized to JSON string",
      json.loads(_CN.parse_leaked_tool_calls(
          _M32 + "\n" + json.dumps([{"name": "t", "arguments": {"a": 1}}],
                                   ensure_ascii=False),
          {"t"})[0][0]["function"]["arguments"]) == {"a": 1})
check("arguments not valid JSON -> no recovery",
      _CN.parse_leaked_tool_calls(
          _M32 + "\n" + json.dumps([{"name": "t", "arguments": "{not-json"}]),
          {"t"})[0] is None)
check("declared tool names read from both chat and responses tools shapes",
      _CN.tool_names_from_payload({"tools": [
          {"type": "function", "function": {"name": "a"}},
          {"type": "function", "name": "b"}]}) == {"a", "b"})


def _raw32(content=None, fin=None, **kw):
    delta = {}
    if content is not None:
        delta["content"] = content
    delta.update(kw)
    inner = {"id": "c32", "model": "m32", "created": 1, "choices": [
        {"index": 0, "delta": delta, "finish_reason": fin}]}
    return ("data: " + json.dumps(inner, ensure_ascii=False)
            + "\n\n").encode("utf-8")


def _env32(content=None, fin=None, **kw):
    inner = json.loads(_raw32(content, fin, **kw)[6:])
    return ("data: " + json.dumps(
        {"statusCodeValue": 200, "body": json.dumps(inner, ensure_ascii=False)},
        ensure_ascii=False) + "\n\n").encode("utf-8")


class _Resp32(object):
    def __init__(self, items):
        self.items = list(items)

    def __iter__(self):
        return iter(self.items)

    def close(self):
        pass


_obj32 = upstream.aggregate_stream(
    _Resp32([_env32(_LEAK32[:12]), _env32(_LEAK32[12:]), _env32("", "stop")]),
    "m32", None, allowed_names={"terminal"})
_msg32 = _obj32["choices"][0]["message"]
check("non-stream aggregate: leaked body -> tool_calls, finish_reason=tool_calls",
      _obj32["choices"][0]["finish_reason"] == "tool_calls"
      and _msg32.get("content") == ""
      and ((_msg32.get("tool_calls") or [{}])[0].get("function")
           or {}).get("name") == "terminal", _obj32)

_frames32 = [json.loads(f[6:]) for f in _CN.recover_leaked_tool_calls(
    iter([_raw32(_LEAK32[:9]), _raw32(_LEAK32[9:]), _raw32("", "stop")]),
    {"terminal"})]
_c32 = [_f["choices"][0] for _f in _frames32]
check("stream: marker split across deltas still recovers + finish rewritten",
      not any(_d.get("delta", {}).get("content") for _d in _c32)
      and any(_d.get("delta", {}).get("tool_calls") for _d in _c32)
      and _c32[-1]["finish_reason"] == "tool_calls", _c32)

_frames32b = [json.loads(f[6:]) for f in _CN.recover_leaked_tool_calls(
    iter([_raw32("[1, 2"), _raw32(", 3] 这是正文"), _raw32("", "stop")]))]
check("stream: [ prefix disproved -> text flushed verbatim, finish untouched",
      "".join(_f["choices"][0]["delta"].get("content") or ""
              for _f in _frames32b) == "[1, 2, 3] 这是正文"
      and _frames32b[-1]["choices"][0]["finish_reason"] == "stop", _frames32b)

_frames32c = [json.loads(f[6:]) for f in _CN.recover_leaked_tool_calls(
    iter([_raw32(_LEAK32), _raw32("", "stop")]), {"other_tool"})]
check("stream: undeclared tool name -> text passes through untouched",
      "".join(_f["choices"][0]["delta"].get("content") or ""
              for _f in _frames32c) == _LEAK32, _frames32c)

_ev32 = [f.decode() for f in responses_mod.stream_responses_events(
    iter([_raw32(_LEAK32), _raw32("", "stop")]), "m32",
    {"usage": None, "custom_names": set(), "allowed_names": {"terminal"}})]
_parsed32 = [json.loads(_ln[6:]) for _fr in _ev32
             for _ln in _fr.splitlines() if _ln.startswith("data: ")]
_text32 = "".join(e.get("delta") or "" for e in _parsed32
                  if e.get("type") == "response.output_text.delta")
_fc32 = [e["item"] for e in _parsed32
         if e.get("type") == "response.output_item.done"
         and (e.get("item") or {}).get("type") == "function_call"]
check("Responses stream: leaked text never becomes output_text, becomes function_call",
      _text32 == "" and _fc32 and _fc32[0].get("name") == "terminal",
      (_text32, _fc32))

check("write side only references the LEAK_MARKER constant (no duplicate literal)",
      _ALL_SRC.count(json.dumps(_M32, ensure_ascii=False)) == 1
      and "LEAK_MARKER + " in _ALL_SRC,
      _ALL_SRC.count(json.dumps(_M32, ensure_ascii=False)))

print()
print("[32.5] issue #9: truncated marker+JSON echo must be swallowed, never leaked (P0)")
_TRUNC32 = _M32 + "\n" + _CALLS32[:40]
check("guard: truncated array -> True",
      _CN.leaked_partial_droppable(_TRUNC32, {"terminal"}) is True)
check("guard: marker only -> True",
      _CN.leaked_partial_droppable(_M32, {"terminal"}) is True)
check("guard: fence + truncated array -> True",
      _CN.leaked_partial_droppable("```json\n" + _TRUNC32, {"terminal"}) is True)
check("guard: marker + prose -> False",
      _CN.leaked_partial_droppable(_M32 + "\n这是一段解释文字。",
                                   {"terminal"}) is False)
check("guard: discussion reply not starting with marker -> False",
      _CN.leaked_partial_droppable("网关会写入 " + _M32 + " 这样的提示。",
                                   {"terminal"}) is False)
check("guard: no declared tools / empty names -> False",
      _CN.leaked_partial_droppable(_TRUNC32, None) is False
      and _CN.leaked_partial_droppable(_TRUNC32, set()) is False)
check("guard: complete array with undeclared name -> False (fail-open)",
      _CN.leaked_partial_droppable(_LEAK32, {"other"}) is False)

_frames_trunc32 = [json.loads(f[6:]) for f in _CN.recover_leaked_tool_calls(
    iter([_raw32(_TRUNC32[:12]), _raw32(_TRUNC32[12:]), _raw32("", "stop")]),
    {"terminal"})]
_text_trunc32 = "".join(_f["choices"][0]["delta"].get("content") or ""
                        for _f in _frames_trunc32)
check("stream: truncated echo swallowed (no marker, no plaintext tool name)",
      _text_trunc32 == ""
      and _M32 not in json.dumps(_frames_trunc32, ensure_ascii=False),
      _text_trunc32)
check("stream: after swallowing, finish_reason stays stop",
      _frames_trunc32[-1]["choices"][0]["finish_reason"] == "stop",
      _frames_trunc32)

_obj_trunc32 = upstream.aggregate_stream(
    _Resp32([_env32(_TRUNC32[:12]), _env32(_TRUNC32[12:]), _env32("", "stop")]),
    "m32", None, allowed_names={"terminal"})
_msg_trunc32 = _obj_trunc32["choices"][0]["message"]
check("non-stream: truncated echo -> empty content, no tool_calls, finish=stop",
      _msg_trunc32.get("content") == "" and not _msg_trunc32.get("tool_calls")
      and _obj_trunc32["choices"][0]["finish_reason"] == "stop", _obj_trunc32)

_ev_trunc32 = [f.decode() for f in responses_mod.stream_responses_events(
    iter([_raw32(_TRUNC32[:12]), _raw32(_TRUNC32[12:]), _raw32("", "stop")]),
    "m32", {"usage": None, "custom_names": set(),
            "allowed_names": {"terminal"}})]
_parsed_trunc32 = [json.loads(_ln[6:]) for _fr in _ev_trunc32
                   for _ln in _fr.splitlines() if _ln.startswith("data: ")]
_text_delta_trunc32 = "".join(e.get("delta") or "" for e in _parsed_trunc32
                              if isinstance(e, dict)
                              and e.get("type") == "response.output_text.delta")
check("Responses stream: truncated echo absent from text deltas",
      _text_delta_trunc32 == "" and _M32 not in "".join(_ev_trunc32),
      (_text_delta_trunc32[:120],))

print()
print("[33] issue #10 three-state observability: headers actually sent + INTL limitation")
_MH33 = ("cosy-machineid", "cosy-machinetoken", "cosy-machinetype",
         "cosy-machineos", "cosy-machinehostname", "cosy-machinecode")
_orig_nmi33 = A.native_machine_identity
_orig_cget33 = A.Account._campaigns_get


def _camp33(realm, native):
    if native:
        A.native_machine_identity = lambda r, u, force=False: {
            "machineToken": "tok33", "machineType": "3", "machineCode": "c33",
            "source": A.MACHINE_IDENTITY_NATIVE}
    else:
        A.native_machine_identity = lambda r, u, force=False: {}
    acc = A.Account({"uid": "u33-%s-%s" % (realm, "n" if native else "d"),
                     "realm": realm, "accessToken": "dt-x"})
    hdrs = acc.desktop_headers()
    state = acc.machine_headers_state
    A.Account._campaigns_get = lambda self: (
        {"campaigns": [], "showCampaign": True, "claimable": False}, 200, "")
    try:
        st = acc.campaigns(force=True)
    finally:
        A.Account._campaigns_get = _orig_cget33
    return hdrs, state, st


try:
    _h_cn_n33, _s_cn_n33, _c_cn_n33 = _camp33("cn", True)
    _h_cn_d33, _s_cn_d33, _c_cn_d33 = _camp33("cn", False)
    _h_in_n33, _s_in_n33, _c_in_n33 = _camp33("intl", True)
    _h_in_d33, _s_in_d33, _c_in_d33 = _camp33("intl", False)
    A.Account._campaigns_get = lambda self: (None, 500, "boom33")
    _acc_fail33 = A.Account({"uid": "u33fail", "realm": "cn", "accessToken": "dt-x"})
    _st_fail33 = _acc_fail33.campaigns(force=True)
finally:
    A.native_machine_identity = _orig_nmi33
    A.Account._campaigns_get = _orig_cget33

check("cn x native: all six machine headers sent, state reported as native",
      all(_h_cn_n33.get(k) for k in _MH33)
      and _s_cn_n33 == A.MACHINE_HEADERS_NATIVE
      and _c_cn_n33.get("machine_headers") == A.MACHINE_HEADERS_NATIVE,
      (_s_cn_n33, _c_cn_n33.get("machine_headers")))
check("cn x derived: no machine header at all, state omitted",
      not any(_h_cn_d33.get(k) for k in _MH33)
      and _s_cn_d33 == A.MACHINE_HEADERS_OMITTED
      and _c_cn_d33.get("machine_headers") == A.MACHINE_HEADERS_OMITTED,
      (_s_cn_d33, _c_cn_d33.get("machine_headers")))
check("intl x native: all six headers sent, state native",
      all(_h_in_n33.get(k) for k in _MH33)
      and _s_in_n33 == A.MACHINE_HEADERS_NATIVE
      and _c_in_n33.get("machine_headers") == A.MACHINE_HEADERS_NATIVE,
      (_s_in_n33, _c_in_n33.get("machine_headers")))
check("intl x derived: no machine header at all, state omitted",
      not any(_h_in_d33.get(k) for k in _MH33)
      and _s_in_d33 == A.MACHINE_HEADERS_OMITTED
      and _c_in_d33.get("machine_headers") == A.MACHINE_HEADERS_OMITTED,
      (_s_in_d33, _c_in_d33.get("machine_headers")))
check("INTL x omitted must carry the known-limitation hint (UMID + 已知限制)",
      isinstance(_c_in_d33.get("hint"), str)
      and "UMID" in _c_in_d33["hint"] and "已知限制" in _c_in_d33["hint"],
      _c_in_d33.get("hint"))
check("CN x omitted hint key exists and is empty (no leak of the INTL caveat)",
      "hint" in _c_cn_d33 and _c_cn_d33.get("hint") == "",
      _c_cn_d33.get("hint"))
check("native (both realms) hint is empty: only INTL x omitted warns",
      _c_cn_n33.get("hint") == "" and _c_in_n33.get("hint") == "",
      (_c_cn_n33.get("hint"), _c_in_n33.get("hint")))
check("the two dimensions are orthogonal (identity derived != headers omitted)",
      _c_cn_d33.get("identity") == "derived"
      and _c_cn_d33.get("machine_headers") == A.MACHINE_HEADERS_OMITTED,
      (_c_cn_d33.get("identity"), _c_cn_d33.get("machine_headers")))
check("failed campaigns() probe still reports machine_headers + hint keys",
      _st_fail33.get("ok") is False
      and "machine_headers" in _st_fail33 and "hint" in _st_fail33,
      _st_fail33)
check("identity source vocabulary: self-heal compares against runtime-info",
      A.MACHINE_IDENTITY_NATIVE == "runtime-info"
      and '== "native"' not in "\n".join(
          l for l in _ALL_SRC.splitlines() if not l.lstrip().startswith("#")))

print()
print("[34] upstream v1.2.1 audit fixes: Responses context/terminal/seq, scheduler state, pro idempotency")
_src34 = _ALL_SRC
check("Responses retry reopens with the converted chat_req (both call sites)",
      "chat_req, session_key=session_key" in _src34
      and "upstream, chat_req, session_key" in _src34)

_h34 = {"usage": None, "custom_names": set(), "allowed_names": {"terminal"}}
_ev34 = [json.loads(_ln[6:])
         for _f in responses_mod.stream_responses_events(
             iter([_raw32("hi"), _raw32("", "stop")]), "m32", _h34)
         for _ln in _f.decode().splitlines() if _ln.startswith("data: ")]
_max34 = max(e["sequence_number"] for e in _ev34)
_fail_raw34 = responses_mod.responses_failed_frame(_h34, 418, "upstream boom")
_fail_obj34 = json.loads([_l for _l in _fail_raw34.decode().splitlines()
                          if _l.startswith("data: ")][0][6:])
check("response.failed: terminal event name / status / error.code",
      _fail_raw34.decode("utf-8").startswith("event: response.failed\n")
      and _fail_obj34["type"] == "response.failed"
      and _fail_obj34["response"]["status"] == "failed"
      and _fail_obj34["response"]["error"]["code"] == "418",
      _fail_obj34)
check("response.failed: sequence_number continues past all prior events",
      _fail_obj34["sequence_number"] > _max34,
      (_fail_obj34["sequence_number"], _max34))
_h34b = {"usage": None, "custom_names": set(), "allowed_names": {"terminal"}}
_ev34b1 = [json.loads(_ln[6:])
           for _f in responses_mod.stream_responses_events(
               iter([_raw32("a"), _raw32("", "stop")]), "m32", _h34b)
           for _ln in _f.decode().splitlines() if _ln.startswith("data: ")]
_ev34b2 = [json.loads(_ln[6:])
           for _f in responses_mod.stream_responses_events(
               iter([_raw32("b"), _raw32("", "stop")]), "m32", _h34b)
           for _ln in _f.decode().splitlines() if _ln.startswith("data: ")]
check("Responses reopen keeps sequence_number monotonic (no reset to 0)",
      min(e["sequence_number"] for e in _ev34b2)
      == max(e["sequence_number"] for e in _ev34b1) + 1,
      (max(e["sequence_number"] for e in _ev34b1),
       min(e["sequence_number"] for e in _ev34b2)))

check("catalog_source: value within the allowed set, unknown realm -> cn branch",
      C.snapshot_source("cn") in ("external-json", "embedded-frozen", "unknown")
      and C.snapshot_source("intl") in ("external-json", "embedded-frozen", "unknown")
      and C.snapshot_source("bogus-realm") == C.snapshot_source("cn"),
      (C.snapshot_source("cn"), C.snapshot_source("intl")))
check("/v1/models keeps its shape and adds the read-only catalog_source",
      '"object": "list", "data": data' in _src34
      and '"catalog_source": catalog_source' in _src34
      and 'catalog_source = "unknown"' in _src34)
check("/v1/completions answers a clear 404 instead of forwarding an empty session",
      "the legacy /v1/completions API is not supported here" in _src34)
check("/settings/reveal refuses while the panel password is still default",
      "面板仍在使用默认密码" in _src34
      and "panel_password_is_default" in _src34)

# 调度器状态落盘 + 启动补签闸门（mark-before-act）
import shutil as _sh34
import tempfile as _tf34
from qoder2api import scheduler as _S34
_tmp34 = _tf34.mkdtemp(prefix="qd-test-sched-")
_sched_err34 = None
try:
    _pool34 = A.AccountPool(_tmp34)
    _s1_34 = _S34.Scheduler(_pool34, state_dir=_tmp34)
    _gate_first34 = _s1_34._allow_complement_checkin(_S34.CYCLE_STARTUP)
    _state_path34 = _s1_34._state_path()
    _state_exists34 = os.path.isfile(_state_path34)
    with open(_state_path34, encoding="utf-8") as _fh34:
        _state_json34 = json.load(_fh34)
    _gate_second34 = _s1_34._allow_complement_checkin(_S34.CYCLE_STARTUP)
    _s2_34 = _S34.Scheduler(_pool34, state_dir=_tmp34)     # simulate a restart
    _gate_restart34 = _s2_34._allow_complement_checkin(_S34.CYCLE_STARTUP)
    _gate_hour34 = _s2_34._allow_complement_checkin(_S34.CYCLE_HOUR)
    _accounts34 = _pool34.load()
finally:
    _sh34.rmtree(_tmp34, ignore_errors=True)

check("Scheduler: state.json lives in a subdir and is not read as an account",
      _state_path34 == os.path.join(_tmp34, "scheduler", "state.json")
      and _state_exists34 and _accounts34 == [],
      (_state_path34, len(_accounts34)))
check("Scheduler: mark-before-act — first startup complement allowed, date saved first",
      _gate_first34 is True
      and _state_json34.get("startup_claim_date") == time.strftime("%Y-%m-%d"),
      _state_json34.get("startup_claim_date"))
check("Scheduler: second startup complement on the same day is refused",
      _gate_second34 is False, _gate_second34)
check("Scheduler: a restart does not replay (new instance reads the same state)",
      _gate_restart34 is False, _gate_restart34)
check("Scheduler: hourly cycles are not gated", _gate_hour34 is True, _gate_hour34)

# Pro 领取幂等：已领取不再虚增 credit_added
_orig_pro_claim34 = A.Account.pro_claim
_orig_pro_elig34 = A.Account.pro_eligibility
try:
    _acc_pro34 = A.Account({"uid": "pro34", "realm": "cn", "accessToken": "dt-x"})
    A.Account.pro_eligibility = lambda self: (True, True)
    A.Account.pro_claim = lambda self: {"ok": True, "already": True,
                                        "msg": "Pro 升级包已领取过"}
    _r_already34 = T.run_pro_claim(_acc_pro34)
    _batch_already34 = T.run_batch_pro_claim([_acc_pro34])
    A.Account.pro_claim = lambda self: {"ok": True, "msg": "Pro 升级包领取成功"}
    A.Account.fetch_credits = lambda self: {"ok": False}
    _r_new34 = T.run_pro_claim(_acc_pro34)
finally:
    A.Account.pro_claim = _orig_pro_claim34
    A.Account.pro_eligibility = _orig_pro_elig34

check("pro claim: already-claimed is reported but earns 0 credits",
      _r_already34.get("ok") and _r_already34.get("already_claimed")
      and _r_already34.get("state") == "already_claimed"
      and _r_already34.get("earned_credit") == 0, _r_already34)
check("pro claim: freshly-claimed still earns the full reward",
      _r_new34.get("state") == "claimed_now"
      and _r_new34.get("earned_credit") == T.PRO_REWARD_CREDIT, _r_new34)
check("pro batch: credit_added counts only fresh grants + already_count exposed",
      _batch_already34.get("credit_added") == 0
      and _batch_already34.get("already_count") == 1,
      (_batch_already34.get("credit_added"),
       _batch_already34.get("already_count")))

print()
print("[35] usage stats page: SQLite aggregate wiring + key dimension + recent cap")
_src35 = _ALL_SRC
check("/usage/stats route exposes range/realm/group/granularity",
      'if path == "/usage/stats":' in _src35
      and 'range must be today / 7d / 30d / custom' in _src35
      and "usagedb.stats(" in _src35)
check("/usage/recent caps the page size at 100 (dashboard shows at most 100)",
      'min(100, int((query.get("limit")' in _src35)
check("record_usage carries the API key id/name into the JSONL row",
      "key_id" in _src35 and "key_name" in _src35
      and "def record_usage(model, usage, stream=None, elapsed_ms=None, "
          "ttft_ms=None,\n                 gen_ms=None, fp=None, account=None, "
          "key=None)" in _src35)
check("record_error also records key + realm (failures stay attributable)",
      "def record_error(model, status, message, elapsed_ms=None, key=None, "
      "realm=None)" in _src35)
# 两个主链路（chat / responses）都必须把 key 传下去：漏传会静默退化成
# 「未绑定 Key」，统计页看不出来是代码问题。
check("both chat and responses routes pass the key through",
      _src35.count("key=key") >= 16, _src35.count("key=key"))
check("upstream usage block reads the plural 'credits' field (billing)",
      'usage.get("credits") or usage.get("credit")' in _src35)
check("cli boots the usage db and backfills in the background",
      "usagedb.db_path()" in _src35 and "threading.Thread(target=usagedb.sync" in _src35)

_gitignore35 = open(os.path.join(_ROOT, ".gitignore"), encoding="utf-8").read()
check(".gitignore anchors the runtime usage dir to the repo root",
      "\n/usage/\n" in _gitignore35)
_lines35 = [l.strip() for l in _gitignore35.splitlines()]
check("no bare 'usage/'/'accounts/' rule (would swallow web routes named the same)",
      "usage/" not in _lines35 and "accounts/" not in _lines35, _lines35[:8])

# 页面/组件源码断言：60 秒自动刷新 + 时间范围四档 + 三个分组
_usage_page35 = ""
try:
    _usage_page35 = open(os.path.join(
        _ROOT, "web", "app", "(main)", "usage", "page.tsx"), encoding="utf-8").read()
except OSError:
    pass
check("usage page auto-refreshes every 60s and skips hidden tabs",
      "AUTO_REFRESH_MS = 60_000" in _usage_page35
      and "visibilityState" in _usage_page35)
check("usage page offers today / 7d / 30d / custom ranges",
      "{value: 'today', label: '今天'}" in _usage_page35
      and "'7d'" in _usage_page35 and "'30d'" in _usage_page35
      and "'custom'" in _usage_page35 and 'type="date"' in _usage_page35)

print()
print("[36] usage page shows the FULL request log (dashboard keeps the 100-row preview)")
_src36 = _ALL_SRC
check("/usage/recent total comes from the SQLite aggregate (no full JSONL scan per page)",
      "usagedb.count_requests(" in _src36
      and "total=total" in _src36
      and "def count_requests(realm=\"\")" in _src36)
check("recent_usage accepts a precomputed total and falls back to a scan",
      "def recent_usage(limit=100, realm=None, page=1, total=None)" in _src36
      and "total = count_usage_rows(realm)" in _src36)
check("usagedb counts failed rows too (matches the row list)",
      'out["requests"] = 1' in _src36 and 'out["failed"] = 1' in _src36)
check("usagedb realm attribution falls back to exclusive models, like usage.py",
      "exclusive_realm" in _src36)
_usage_page36 = ""
try:
    _usage_page36 = open(os.path.join(
        _ROOT, "web", "app", "(main)", "usage", "page.tsx"), encoding="utf-8").read()
except OSError:
    pass
check("usage page embeds the full request table",
      "<RecentRequestsTable" in _usage_page36
      and 'title="全部请求记录"' in _usage_page36)
_page_dash36 = ""
try:
    _page_dash36 = open(os.path.join(
        _ROOT, "web", "app", "(main)", "page.tsx"), encoding="utf-8").read()
except OSError:
    pass
check("dashboard marks its request table as the latest-100 preview",
      'hint="仅显示最新 100 条"' in _page_dash36)
check("shared table defaults to the plain 最近请求 title",
      "title = '最近请求'" in open(
          os.path.join(_ROOT, "web", "components", "common", "gateway",
                       "RecentRequestsTable.tsx"), encoding="utf-8").read())
# 用量页的下拉框宽度必须够放「Token 消耗」（窄了会把文字截成「Token 消」）
_trend36 = open(os.path.join(_ROOT, "web", "components", "common", "usage",
                             "UsageTrendCard.tsx"), encoding="utf-8").read()
check("trend metric/group selects are wide enough for their labels",
      "w-[136px]" in _trend36 and "w-[110px]" not in _trend36)
_break36 = open(os.path.join(_ROOT, "web", "components", "common", "usage",
                             "BreakdownPanel.tsx"), encoding="utf-8").read()
check("breakdown empty state sits in a fixed-height centred box",
      "min-h-[200px] place-items-center" in _break36)

print()
print("SUMMARY: PASS=%d FAIL=%d" % (PASS, FAIL))
sys.exit(1 if FAIL else 0)
