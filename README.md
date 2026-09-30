# Qoder2API-Hub — 国际版、国内版多账号网关中枢

<p align="center">
  <img src="https://img.shields.io/badge/Release-v1.1.8-2496ED?style=flat-square" alt="Version 1.1.8">
  <img src="https://img.shields.io/badge/Python-3.9+-blue.svg?style=flat-square" alt="Python">
  <img src="https://img.shields.io/badge/API-OpenAI_Compatible-412991?style=flat-square" alt="OpenAI API">
  <img src="https://img.shields.io/badge/Dual_Realm-CN_&_Intl-0DBD8B?style=flat-square" alt="Dual Realm">
  <img src="https://img.shields.io/badge/License-MIT-green.svg?style=flat-square" alt="License">
  <img src="https://img.shields.io/badge/Zero-Dependency-ff69b4?style=flat-square" alt="Zero Dependency">
</p>

本项目为 **Qoder2API-Hub**，将阿里 **[qoder.com.cn](https://qoder.com.cn)** (国内版) 与 **[qoder.com](https://qoder.com)** (国际版) 的原生服务封装为标准 OpenAI 兼容接口，支持 Chat Completions 与 Responses API。具备多账号负载轮询、稳定物理设备指纹隔离、OAuth 设备授权一键免客户端登录、每日签到与实时额度查询、Pro 福利包自动领取、后台常驻定时调度器、Web 监控看板等全套能力 —— 与 WorkBuddy2API-Hub 同构的完整功能矩阵。

- **开箱即用**：双击批处理脚本即启；亦支持 Docker 容器化部署，零外部 pip 依赖。
- **本机已登录凭证一键入池（双区）**：只读探测桌面 App（`auth.v1.dat`，os_crypt/DPAPI 解密）与 Qoder CLI（`~/.qoder*/.auth/user`，AES-128-CBC）两类官方存储，看板两步确认导入，永不静默采用。
- **模型清单完全跟官方走（双区不同、以官方此刻为准）**：三源优先级 —— 动态 `/algo/api/v2/model/list`（COSY 签名，**GET 需携带与签名一致的 `{}` body，否则 403**）> 本机官方客户端模型目录缓存（`~/.qoder*/.models/<uid>/catalog-v6`，QMC/HKDF+AES-256-GCM 解密）> 双区官方快照文件（`qoder_catalog_intl.json`/`qoder_catalog_cn.json`，`python _refresh_catalog.py` 一键随客户端更新）；清单**以动态源返回的集合为准**（官方桌面版此刻显示什么这里就显示什么，如国际版动态 15 条就不多塞静态独有的 `smodel/cmodel`）。**逐字段忠实保留**：`id` = **官方模型名**（如 `Qwen3.8-Max`，客户端唯一需要填的值；`upstream_key`/`aliases` 同时给出 key、`key (Name)` 与人类别名等全部可填形式）、官方桌面版介绍文案（`description`，取自客户端 dynamic-text）、本地化名（`name_local`，如 Ultimate→极致）、`context_config` 多窗口（200K 默认/400K/1M）、`thinking_config` 思考档位（low/medium/high/xhigh/max + 默认标注 + 可关闭）、**峰谷价**（`price_factor_peak` 促销前倍率 → `price_factor_valley` 谷时倍率 + `off_peak` 时段窗口 22:00-08:00 与官方错峰文案）、`is_free/is_new`；官方 `enable=false` 条目不过滤，附**官方原文禁用原因**（`disabled_reason = "需要升级或购买千问官方套餐开放"`，并透传上游 `disabled_message_key`）。**最大输出**：官方 catalog 与动态接口原始响应均无此字段，故不再输出/展示任何编造值。
- **双区域独立路由（三档出口模式）**：支持 🌐 国际版 (qoder.com / api1.qoder.sh，备用 api2/api3 自动故障切换) 与 🇨🇳 国内版 (qoder.com.cn / gateway.qoder.com.cn) 独立配置与管理；出口可选 **仅国际 / 仅国内 / 双区**——双区模式下选定**优先出口**，首选出口无可用账号（全部停用/冷却/凭证失效）时**自动切到另一区**，首选恢复后自动切回；区域独占模型（如国内 `q37fmodel`/`glm-5.2`、国际 `smodel`/`ultimate`）始终路由到归属出口并拦截错配 Key；选择落盘持久化、看板即改即生效。
- **COSY 签名推理链路**：RSA 包裹 AES 会话密钥 + MD5 请求签名 + 自定义 Base64 请求体编码，纯标准库实现（含 AES-128/256、RSA-PKCS1v15、GCM、DPAPI、QMC 纯 Python 实现，Docker alpine 下同样零依赖），逆向对齐官方桌面/CLI 客户端协议。
- **稳定物理设备指纹隔离 (`derive_id`)**：以账号自身 UID 稳定哈希派生专属 `cosy-machineid` / `cosy-machinetoken` / 会话标识，同一账号长期固定在同一台虚拟物理设备，天然防多号关联风控。
- **OAuth 设备授权一键免客户端登录**：PKCE (S256) 设备流（双区 URL 参数按官方差异构造：国内带 `redirect_uri+client_id+machine_id`，国际带 `client_id+machine_id`），点击看板链接在浏览器完成授权即可自动入池；亦支持 PAT (`pt-`) 导入，jobToken 自动交换与轮换。
- **每日签到与额度体系（双区域 · 真实领取）**：「每日领取 100 Credits」等活动**由网关直接领取**——用桌面端请求头（`Cosy-ClientType: 10` + 机器头，缺了服务端会返回空列表）列出活动 → 对 `CLAIMABLE` 的 Credits 活动 `POST /sash/api/v1/me/campaigns/{id}/claim`（官方幂等：已领返回 `replayed`，不会重复发放）；旧 sash 签到接口仅在仍开放时兜底（能力运行时探测，404 记「本区域无此接口」6 小时后自动重探）；Pro 升级包资格检查与领取、quota/usage 额度与套餐快照实时刷新。
- **统一出站网络层（三档代理模式）**：所有出站请求（模型推理、活动领取、账号接口、模型清单、辅助脚本）统一经过 `qoder_net`——**跟随系统代理**（Windows 直读 Internet Settings 注册表，不受终端环境变量污染）/ **手动指定代理**（如 `http://127.0.0.1:7897`）/ **直连**；看板「设置 → 网络代理」即改即生效，环境变量 `QD_PROXY_MODE`/`QD_PROXY_URL` 可覆盖；本机回环始终直连。
- **后台常驻定时调度器**：每日整点排程（09:00 / 21:00 签到 · 22:00 Token 集中保活），`drt-` / `jrt-` 按前缀路由刷新，PAT 最终兜底。
- **双协议全功能支持**：同时支持标准 OpenAI Chat Completions 协议与 Responses API (Codex / Claude Code)，含 custom freeform 工具（`apply_patch`）双向转译与 DSML 工具调用回退解析。
- **现代化 Web 看板**：弹性指标卡片、签到与福利中心、模型能力清单、性能指标与用量透视、实时请求流水与运行日志。

> ⚡ 本项目架构与交互对齐 WorkBuddy2API-Hub，上游协议替换为 Qoder COSY 签名体系。

---

## 🖼️ 看板预览 (Dashboard Preview)

**网关总览** —— 双区出口状态、调度器、账号池与请求流水一屏尽览：

![网关总览](docs/img/dashboard.png)

**模型清单** —— 与官方桌面版同源：官方模型名、峰谷价、上下文多窗口、思考档位、能力徽标：

![模型清单](docs/img/models.png)

**数据指标看板** —— Token 消耗透视、TTFT 首字延迟、生成速度与缓存命中率：

![数据指标看板](docs/img/metrics.png)

**签到与福利中心** —— 每日签到领积分、Pro 福利包一键领取、限时活动真实状态（双区域）：

![签到与福利中心](docs/img/benefits.png)

---

## 一、快速启动

### 1. 本机单机使用
双击运行 **`start-qoder-proxy.bat`**，保持窗口运行：
- **API 接口地址**：`http://127.0.0.1:8790/v1`
- **Web 监控看板**：`http://127.0.0.1:8790/`

> ⚠️ **"客户端一直显示工作中、一个字都不吐，网关日志也毫无变化"** → 九成是**网关没在跑**：请求根本没到达，所以日志自然一动不动。一条命令确诊：
> ```bash
> python _diag_gateway.py --chat   # 端口 → /ping → /health → /v1/models → 真实流式，逐项报出断在哪
> ```
> - **生命周期就是那个 cmd 窗口**（刻意的设计）：窗口开着网关就活着，**关掉窗口网关即停**，不会有后台残留进程；下次要用重新双击 `start-qoder-proxy.bat` 即可。
> - 判别口诀：日志时间戳停在某一刻、此后再无 `POST /v1/chat/completions` = 网关已停；重新双击启动脚本即可恢复。

> ℹ️ 默认端口 **8790**（8788 被 `mimo-api-proxy.mjs` 占用，8789 为 wb-proxy 默认）。改端口：`start-qoder-proxy.bat 8791`。

首次启动若无账号，直接打开看板点击 **「+ 添加账号 (OAuth)」**，在浏览器完成设备授权即可自动加入；或点 **「🔑 导入 PAT」** 粘贴个人访问令牌。

### 2. 面板访问密码

打开看板需要先输入**面板访问密码**，默认是 `admin`。它与 API Key 相互独立：

- 面板密码只用于打开网页看板，可在看板「设置」页修改（也可启动时 `--panel-password` 指定）；
- 密码以 PBKDF2-SHA256 摘要形式保存在 `accounts/settings.json`，不存明文；
- 登录状态存放在浏览器会话中，关闭浏览器或重启网关后需要重新输入。

> 首次登录后请立即到「设置」修改默认密码。

### 3. 局域网共享模式
双击运行 **`start-qoder-proxy-lan.bat`**，允许局域网内其他设备访问：
- **Base URL**：`http://<本机局域网IP>:8790/v1`
- **密钥随机生成并持久化**：LAN 模式首次启动生成高强度随机 API Key（`qd-` 前缀），保存到 `accounts/settings.json` 并在终端打印，重启复用。
- **自定义 Key**：`start-qoder-proxy-lan.bat 8790 我的Key`
- 支持带密钥直达面板：`http://<IP>:8790/?key=生成的Key`。
- 其他设备连不上时，管理员运行一次 `allow-firewall.bat` 放行防火墙。

### 4. 多 API Key 管理与出口绑定

网关支持**多 API Key 并行管理**，并可为每个 Key 指定独立出口：

- **添加与在线生成**：看板「设置」页，输入名称 + 一键生成随机 Key；
- **出口自由绑定**：
  - 🌐 **固定国际版出口**：该 Key 流量强制走 `api1.qoder.sh`（连不上自动切 api2/api3），不参与区域失效切换
  - 🇨🇳 **固定国内版出口**：该 Key 流量强制走 `gateway.qoder.com.cn`，不参与区域失效切换
  - **跟随网关出口（默认）**：未绑定出口的 Key 实时跟随看板顶部出口模式——单区 = 该区；双区 = 优先区失效自动切换另一区
- **状态管理**：单独启停、一键删除，删除即刻失效；配置持久化到 `accounts/settings.json`；
- **安全防冲突**：面板配置过 Key 后，启动脚本里的旧 `--api-key` 自动失效；
- **模型区域自检**：Key 出口与模型区域不匹配时返回通俗 400，杜绝上游晦涩拒流报错。

### 5. Docker 容器化部署

```bash
# 1. 后台启动容器 (自动构建并运行)
docker compose up -d

# 2. 查看网关日志
docker compose logs -f
```

或直接 `docker run`：

```bash
docker run -d --name qoder-proxy --restart unless-stopped \
  -p 8790:8790 -v $(pwd)/accounts:/app/accounts -v $(pwd)/usage:/app/usage \
  -e API_KEY=your_secret_key $(docker build -q .)
```

- **持久化目录**：`./accounts`（账号凭证及出口设置）与 `./usage`（请求流水与指标快照）；
- **配置参数**：环境变量 `API_KEY`、`PORT`。

---

## 二、核心特性详解

### 1. 请求链路（COSY 签名推理）

**瞬时故障韧性（双层）**：上游把自己的 provider 故障包装成 `418/5xx + provider_error` 抛回，或对 `qoder.sh` 出现 TLS/连接抖动（`SSL: UNEXPECTED_EOF...`）时：
- **连接层**（urlopen 时刻的 HTTP/传输错误）：同账号快速重试 2 次（1s/2s 退避）；
- **流内信封层**（关键形态：上游先 HTTP200 建流、再在 SSE 信封里投 `statusCodeValue=418`——表现为 access log 记 200 而业务错 418）：在**尚未向客户端写出任何上游字节**前重开上游重试 2 次（chat 流式/非流式 + Responses 全覆盖），流式客户端全程无感。

重试仍失败才**短冷却（15s，单账号池实际 3s）换号**——不因上游的锅罚账号 60 秒；**短错误冷却期间（≤10s）后续请求改为「等待续上」而非报错**，且 `429 usage exceeds frequency limit` **只在上游真频控时出现**（账号错误冷却不再被误标为频控）。客户端参数错误（`invalid_parameter_error` 等）与**上游内容安全审核拒绝**（`InternalError.Algo.DataInspectionFailed: Input text data may contain inappropriate content`）**绝不重试**、快速失败——后者返回中文解释（`content_policy_rejected`：确定性拒绝、重试无效，请检查/缩短输入），由调用方修改输入而非等待。耗尽后其余瞬时错误客户端收到中文友好提示（`upstream_transient_error`）；错误详情经 `qoder_detail` 挂载保留 400 字节完整送达日志（含内层 `details`）。

**HTTP 帧层与保活（治“一直重连/连不上”）**：

- **流式响应使用 HTTP/1.1 `Transfer-Encoding: chunked`** 并以 `0\r\n\r\n` 正确收尾，**不再发 `Connection: close`**：同一个 keep-alive 连接可连续复用（实测同连接连发 5 次流式全部成功）。此前裸写字节 + `close` 会让连接池型客户端复用已半关闭的连接，表现为反复重连。
- **SSE 心跳保活**：上游首字延迟实测可达 **40–71 秒**（`xhigh` + 2–3 万 token 长上下文），等待期间网关每 5 秒发送一个 SSE 注释帧 `: ping`（客户端规范要求忽略），避免客户端/中间代理空闲超时断连重连。可用环境变量 `QD_SSE_HEARTBEAT` 调整间隔（秒，`0` 关闭）。
- **探活端点**：`GET /ping`（以及 `/healthz`、`/livez`、`/readyz`）返回纯文本 `pong`，**不需要面板密码或 API Key、不查账号池**——供客户端/脚本判活用；此前返回 404 会被判成网关不可用而反复重连。完整状态仍看 `GET /health`。

**DeepSeek-Flash 偶发失败修复（issue #2）**：这族模型的多轮一致性与 `reasoning_content` 绑定，而旧实现有两处断点，导致"偶发失败、重试有时能过"：

- **判定看的是客户端名字而不是上游模型**：旧逻辑只认名字前缀 `deepseek`，客户端按文档写「内部 key：`dfmodel`」时**整套兼容处理不会执行**。现按上游 key 判定（`is_deepseek_model`：`dmodel`/`dfmodel`/`DeepSeek-Flash`/展示 id「`dfmodel (DeepSeek-Flash)`」都命中）；
- **补好的字段在真实请求路径上被丢掉**：`backfill_reasoning_content()` 写入 `reasoning_content` 后，`flatten_messages()` 压平会话时无条件丢弃该字段——即"兼容层写了但从没发出去"。现按目标模型保留（DeepSeek 族保留、其它模型不带，避免上游因未知字段拒答）。

修复后已对国内版 `dfmodel` 实测：纯问答、展示名 `DeepSeek-Flash`、带 `reasoning_content` 的多轮历史、空 `reasoning_content`、`reasoning` 别名、工具调用历史、流式 27 帧全部 200 正常收尾。

**客户端版本对齐（0.4.3 双区桌面端）**：协议常量按官方客户端当前版本逐项核对——

- `cosy-version` = **1.1.64**（更新自旧 CLI 的 `0.1.43`；取自 0.4.3 内置 `qoder-agent-sdk`/`qoder-cn-agent-sdk` 的版本常量，实测模型列表与推理均正常）；
- 国际版推理主机 = **`api1.qoder.sh`**（客户端 endpoint 缓存里的主选；`api2`/`api3` 为官方故障切换域名，网关同样按序切换，单个域名故障不再拖垮全部请求）；
- 国内版主机 `gateway.qoder.com.cn`、双区 `openapi` 基址、`/algo/api/v2/service/pro/sse/agent_chat_generation`（推理）、`/algo/api/v2/model/list`（模型清单）、`/api/v1/deviceToken|jobToken/*`、`/api/v1/userinfo`、`/api/v2/quota/usage`、`/api/v2/user/plan`、`/sash/api/v1/me/*`（签到/活动平台）**均与新客户端一致**，无变化；
- COSY RSA 公钥与新客户端内置 PEM **逐字节相同**；官方模型目录快照已用新版客户端缓存刷新（价格倍率/上下文/思考默认档等）。

**思考档位（`reasoning_effort`）归一化**：官方上游字段就是 `parameters.reasoning_effort`（0.4.3 SDK 参数表里的 `reasoning_effort`，取值 `none`/`low`/`medium`/`high`/`xhigh`/`max`），但**每个模型的合法档位不同**，而**上游对不支持的档位不报错、直接忽略（回落到模型默认档）**——这就是"给 Qwen3.8-Flash 传档位没反应"的原因：

| 模型 | 官方支持档位 | 默认 | 传 `medium`/`high` 会怎样 |
|---|---|---|---|
| `qfmodel`（Qwen3.8-Flash） | `low` / `medium` / `xhigh` | `medium` | `medium` 生效；`high` 不在表内 → 被忽略 |
| `qmodel_38max`（Qwen3.8-Max） | `low` / `medium` / `xhigh` | `medium` | 同上 |
| `dfmodel`（DeepSeek-Flash） | `low` / `high` / `max` | `max` | `medium`/`xhigh` 都不在表内 → 被忽略（实测输出与默认档一致） |
| `qmodel`（Qwen3.7-Plus） | 无档位（仅开/关） | — | 任何档位都被忽略（只有 `none` 能关掉思考） |

网关现在按**官方目录里该模型的档位表**归一化（`normalize_reasoning_effort()`）：命中原样透传；未命中取"最近的合法档位"（同距时偏向该模型默认档，如 `dfmodel` 的 `medium→high`、`xhigh→max`，`qfmodel` 的 `high→medium`、`max→xhigh`），并在日志标注；模型**有 thinking_config 但无档位表**时不再下发无效档位（只保留 `none`）；模型**完全没有 thinking_config**（如路由器 `auto`）则原样透传，不做猜测。`/v1/models` 的 `reasoning_efforts` / `reasoning_default_effort` 字段即为该模型的合法档位与默认档。另兼容 `reasoning.effort` 与 `thinking.effort/level` 三种客户端写法。

**思考预算与开关（v1.1.8，对齐官方 CLI 1.1.62）**：第三方客户端（Claude Code / Cline / 各家 SDK）常见三种写法全部识别——

- **思考预算**：`thinking.budget_tokens` / `reasoning.budget_tokens` / `thinking_budget`（Anthropic 风格），按官方阈值表换算成档位：`0→none`、`≤1024→low`、`≤8192→medium`、`≤24576→high`、`≤49152→xhigh`、`>49152→max`，再走档位表归一化；
- **开关**：`enable_thinking: false` / `thinking.type: "disabled"` → 下发 `reasoning_effort=none`，并同步官方的 `parameters.enable_thinking=false` 与 `model_config.is_reasoning=false`；开思考时下发 `enable_thinking=true`，带预算时一并透传 `reasoning_budget_tokens`（与官方 SDK 逐字段一致）；
- **上下文窗口**：请求里带 `context_window`（token 数或 `"1M"`/`"400K"` 标签）、`context_window_tokens`、`context_length` 任一即可指定本次请求的上下文窗口，校验通过后以官方字段 `parameters.context_length` 下发；不在官方窗口表内时取**最接近的合法窗口**并在日志标注（官方 CLI 是直接丢弃回默认，这里让客户端意图尽量达成）。

**看板模型库可改默认值（v1.1.8）**：`上下文窗口`、`思考档位` 两列现在是下拉框——每个模型（`<区>:<上游 key>` 维度，两区独立）可以存一份**默认值**（`accounts/settings.json` 的 `model_overrides`），客户端请求里没带对应字段时生效；选「跟随官方默认」即恢复。客户端请求里带了值时**永远以客户端为准**。单窗口模型也给出可选窗口（官方 `WX()` 推导的 128K/200K/上限）。

```
客户端 OpenAI 请求
  → build_qoder_body()   官方 baseprompt 模板 + 会话压平（system/工具/参数覆写）
  → qoder_encode()       Qoder 自定义 Base64（标准 B64 三段轮转 + 字母表映射, '=' → '$'）
  → COSY 签名            RSA(1024) 包裹 AES-128 会话密钥 → info(AES-CBC 身份)
                         Bearer = COSY.{payloadB64}.{md5(payload\ncosyKey\ndate\nbody\npath)}
  → POST {gateway}/algo/api/v2/service/pro/sse/agent_chat_generation?…&Encode=1
  → SSE 信封解包          {"headers","body","statusCodeValue"} 嵌套帧 → 内层 OpenAI chunk
  → 标准 OpenAI SSE / chat.completion 回给客户端
```

模型清单按**三源优先级**完全对齐官方（详见「核心特性 · 模型清单」）：

```
1) 动态接口  GET {gateway}/algo/api/v2/model/list?Encode=1   （COSY 签名，需账号，300s 缓存）
2) 本机官方客户端目录 ~/.qoder*/.models/<uid>/catalog-v6      （QMC 解密，离线可用）
3) 内置双区官方快照 qoder_catalog_intl.json / qoder_catalog_cn.json
   （客户端更新后 `python _refresh_catalog.py` 一条命令重新导出并打印差异；
    两个文件缺失时才回退 qoder_catalog.py 内嵌的冻结副本并打印 WARNING）
```

**双区清单不同**（源自本机官方客户端 catalog 的**全字段**忠实快照，chat 场景；`id` = 官方模型名，直接照抄即可）：

- 🇨🇳 **国内版 (动态 14 条，全部开通)**：`Auto` · `Qwen3.8-Max` · `Qwen3.8-Flash` · `Qwen3.7-Max` · `Qwen3.7-Plus` · `Qwen3.7-Flash` · `DeepSeek-V4-Pro` (96K) · `DeepSeek-Flash` · `GLM-5.3` · `GLM-5.3-Flash` (1M) · `GLM-5.2` · `Kimi-K3` · `Kimi-K2.8-Preview` · `MiniMax-M2.7`
- 🌐 **国际版 (动态 17 条；开通 2、未开通 15)**：`Qwen3.8-Max`、`Qwen3.8-Flash` 开放；其余（`Ultimate`/`Performance`/`Efficient`/`Sonus`/`Cantus`/`DeepSeek-V4-Pro`/`MiniMax-M3`/`Auto` 等）标注**官方原文**「需要升级或购买千问官方套餐开放」+ 上游 `disabled_message_key`（`codeSafeModelReason`），**不隐藏条目**（与桌面版此刻同一份清单）。

**峰谷价（官方 `promotion` 字段）——低谷折扣模型共 3 个（双区一致），全部高亮**：

| 模型 | 峰价 | 谷价 | 折扣（官方 badge） |
|---|---|---|---|
| `Qwen3.8-Max` (`qmodel_38max`) | 0.50x | 0.20x | 错峰 4 折 |
| `Qwen3.7-Max` (`qmodel_latest`) | 0.50x | 0.10x | 错峰 2 折 |
| `Qwen3.7-Plus` (`qmodel`) | 0.10x | 动态为准（快照 0.04x） | 错峰 4 折 |

均为 22:00-08:00 窗口（`Qwen3.8-Flash` 另有限时免费 0.00x，原 `0.10x`）。`/v1/models` 与看板对**每个**促销模型输出 `price_factor_peak` / `price_factor_valley` / `off_peak{window_start,window_end,badge,description,discount_factor,timezone}` 与 **`off_peak_active_now`**（按官方时区 UTC+8 跨午夜窗口判定当前是否处于低谷）。判定顺序上 **promotion 分支优先于 0 价分支**——`is_free` 表示"含免费权益"而非 0 价（Qwen3.8-Max `is_free=true` 但价 0.20x），不会被错标成免费、也不会吞掉低谷高亮（单测有分支顺序回归断言）。

**低谷时段视觉高亮**：看板在低谷窗口（22:00-08:00）内把价签切换为**亮绿发光高亮块**——大号谷价 +「● 低谷生效中」徽标 + 峰价红色删除线 + 时段/折扣文案；非低谷时段显示常规「峰 x → 谷 x」并提示「低谷自 22:00 起」。判定用后端字段 + 浏览器本地时间即时复算双保险，跨窗口自动切换。

同 key 跨区也可能不同（`mmodel` 国际=MiniMax-M3、国内=MiniMax-M2.7）；上下文窗口、倍率、视觉/推理标志逐项取自官方条目（如国内 `dmodel` 96000、国际 `dmodel` 1000000）。请求侧接受 key / 展示 id「key (Name)」/ 人类可读别名 / 官方显示名任意形式；区域独占模型（国内 `q37fmodel`/`glm-5.2`、国际 `smodel`/`ultimate` 等）自动路由到归属出口。

### 2. 稳定物理设备指纹隔离 (`derive_id`)

双区域统一方案：以账号 UID + 业务盐单向 MD5 派生固定 `machineId` / `sessionId` / `machineType` / `machineToken`，COSY 签名头逐请求携带：

- **同一账号长期稳定**：出站请求永远来自同一台虚拟物理设备，规避机器码漂移风控；
- **多账号天然隔离**：不同账号机器码彼此独立，阻断跨账号关联检测。

### 3. 每日签到、额度与 Pro 福利包（双区域 · 真实领取）

**当前官方机制 = 活动平台领取**（`Account.campaign_checkin()`），网关直接完成领取：

- **列表**：`GET /sash/api/v1/me/campaigns`（双区域通用）——**必须同时满足两层**，缺一层都会静默少活动：
  1. **桌面端请求头**（`Cosy-ClientType: 10` + `Cosy-Version` + 机器头 + `UA: Qoder`）；缺了 → 服务端不报错、直接返回**空列表**；
  2. **真实机器身份**（`Cosy-MachineToken/Type/Code`）——官方桌面端在拉活动前会 spawn 自带的风控桥 `resources/umid/runtime-info.exe prod --account-stdin`（stdin `{"account": <uid>}`）取真值；用派生假值时列表会**静默少掉设备定向活动**（「每日领取 100 Credits」即其中之一）。网关现在调用同一个官方二进制取真值（结果缓存 6h，`QD_NATIVE_IDENTITY=0` 可关闭），失败才回退派生值并在活动状态里标注 `identity=derived`；
- **领取**：对 `claimStatus=CLAIMABLE` 且 `actionType=CLAIM_BENEFIT` 的活动 `POST /sash/api/v1/me/campaigns/{campaignId}/claim`（逆向自官方 `growth-page/activity-iframe` 页面 JS）。**官方幂等**：已领取返回 `{"status":"CLAIMED","replayed":true}`，不会重复发放；`GET …/{id}/reward` 可查发放状态；
- **任务中心**：`daily_checkin` 行直接反映真实活动状态——可领取显示「可领取 100 Credits（act-…）—— 点『一键签到』自动领取」，已领取显示「今日已领取 +100 Credits，明日再来」；
- **旧 sash 接口**（`/sash/api/v1/me/daily-check-in/*`）仅在仍开放时作为兜底并附一行历史状态；能力运行时探测（404/405/410 记「本区域无此接口」，6 小时后自动重探）。实测国内版 `status=DISABLED`、国际版全 404；
- **额度体系**：`/api/v2/quota/usage` 聚合基础额度 + 赠送/签到额度；`/api/v2/user/plan` 套餐名（Pro Trial 等）；
- **Pro 福利包**：一次性 +1800 积分，`eligibility → claim` 两步走（端点 404 时视为活动未开放）；
- **看板「签到与福利中心」**：连续签到天数、积分余额、福利包状态卡片 + 任务行表格，支持单账号/批量；国内版与国际版账号都会列出。

### 4. 后台常驻定时调度器 (Scheduler)

- **每日 09:00 & 21:00**：全量自动签到（补签未签账号）+ 额度快照刷新；
- **每日 22:00**：集中 Token 保活 —— `drt-` → `deviceToken/refresh`，`jrt-` → `jobToken/refresh`，失败回落 PAT 重新交换；
- 任意巡检中对剩余寿命不足 4 小时的 Token 提前刷新；
- **会话死亡识别**：上游 `TOKEN_EXPIRE` / `12153` / `Offline user session not found` → 自动停用账号并标注需重新登录。

### 5. 凭证家族与生命周期

```
accessToken:   dt- (OAuth 设备流, ~30天)  或 jt- (PAT 交换, 24小时)
refreshToken:  drt- (~1年, 旋转)          或 jrt- (48小时)
personalToken: pt- (长期兜底, 看板导入)
```

刷新按 `refreshToken` 前缀路由，PAT 永不覆盖活跃 OAuth 会话，只做最终兜底；`access token` 轮换后 COSY 会话自动重建。

---

## 三、账号添加与管理

打开看板 `http://127.0.0.1:8790/`，在「账号」区域操作：

### 方式零：扫描本机已登录凭证（推荐，双区）
1. 点击 **「扫描本地凭证」**（只读，不写入）；
2. 弹窗分区域列出检测到的凭证：
   - **桌面 App**：`%APPDATA%\com.qoder[.cn].app.stable\auth.v1.dat`
     （Chromium `v10` 布局，`Local State` 的 os_crypt 密钥经 DPAPI 解出后 AES-256-GCM 解密）
   - **Qoder CLI**：`~/.qoder[.cn]/.auth/user[.{profile}]`
     （AES-128-CBC，key = `machine_id` 前 16 字符）
3. 点击对应行的 **「导入」**（或启动时日志只会提示发现 N 条、绝不静默采用）。

### 方式一：OAuth 设备授权（推荐，免客户端）
1. 点击 **「+ 添加账号 (OAuth)」**；
2. 选择登录区域（国内版 / 国际版），点击弹出的官方授权链接；
3. 浏览器完成登录授权（PKCE S256），网关自动轮询取回 `dt-`/`drt-` 并入池。

### 方式二：PAT 导入
1. 在 Qoder 网页版「设置 → Personal Access Token」创建 `pt-` 令牌；
2. 看板点击 **「🔑 导入 PAT」**，选择区域并粘贴；
3. 网关自动交换 `jt-`/`jrt-`、拉取账号身份入池。

### 方式三：JSON 导入 / 导出
- 支持全量/单账号导出（可选带密钥）、Dry-Run 预检导入、覆盖同 UID；
- 兼容本网关导出格式、账号数组、单个账号对象。

---

## 四、客户端配置与接入

### OpenAI 兼容客户端 (Chatbox / NextChat / Cherry Studio / Kelivo 等)
- **API 接口地址 (Base URL)**：`http://127.0.0.1:8790/v1`（局域网为 `http://<局域网IP>:8790/v1`）
- **API Key**：
  - 本机单机模式（未配置 Key 且未开 LAN）：可留空或填任意字符；
  - 已配置 Key 或 LAN 模式：在看板「设置」添加或复制已绑定出口的 Key。
- **模型名称**：填 `/v1/models` 列出的 **`id`（官方模型名，如 `Qwen3.8-Max`）** —— 这是唯一需要记的值；`upstream_key`（缩写 key）、人类别名（`qwen3.8-max`）与官方本地化名也全部可解析。

### Codex CLI / Claude Code (Responses API)
```bash
export OPENAI_BASE_URL="http://127.0.0.1:8790/v1"
export OPENAI_API_KEY="你在看板设置中添加并绑定的API_Key"
```
custom freeform 工具（`apply_patch`）自动降级为 function 工具出站、入站还原为 `custom_tool_call`，Codex 工具回路完整可用。

---

## 五、看板与接口一览

访问 `http://127.0.0.1:8790/` 即可使用集成看板，核心接口：

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | / | Web 用量与任务监控看板 |
| POST | /v1/chat/completions | 标准 Chat Completions 接口 |
| POST | /v1/responses | Responses API 协议接口 |
| GET | /v1/models | 模型列表（动态拉取 + 静态兜底，含能力与规格宣告） |
| GET | /tasks | 签到状态、连续天数、福利包资格与额度快照 |
| POST | /tasks/run | 触发批量每日签到与领奖 |
| POST | /tasks/travel | 批量领取 Pro 福利包 |
| GET | /scheduler | 定时调度器运行状态与排程日志 |
| POST | /scheduler/trigger | 手动立即执行后台巡检保活 |
| POST | /accounts/login/start | 发起 OAuth 设备授权 |
| POST | /accounts/import/pat | 导入 PAT 令牌 |
| POST | /accounts/checkin | 手动签到（单个/全部） |

---

## 六、开发与测试

```bash
# 离线确定性测试（294 项断言：AES-128/256 向量与官方 fixture KAT、QMC/凭证解密、
# 自定义 B64、COSY 签名、双区官方目录全字段（峰谷价/多窗口/思考档位/展示 id/解析）、
# 独占路由、签到能力运行时探测与 DISABLED 归一化、活动平台归一化、DeepSeek
# reasoning_content 回填与 flatten 保留、请求体、信封解包、custom 工具转译、
# 本机凭证扫描）
python _test_qoder.py

# 直接启动
python qoder_proxy.py --port 8790

# 客户端更新后刷新官方模型快照（解密本机客户端目录缓存 → 双区 JSON 快照，
# 并打印价格/上下文/思考档位的变化摘要；--dry-run 只看差异不写文件）
python _refresh_catalog.py

# 端到端模型库/能力清单验证（网关运行中执行；逐模型对比官方"此刻"数据：
# id/enable/峰谷价/上下文窗口/思考档位/官方介绍/禁用原因/不编造字段/低谷判定）
#   基准 = 官方动态接口优先（与桌面版选择器同源），本机目录按字段兜底
python _verify_models.py --base http://127.0.0.1:8790
#   347 项断言；退出码 0=全部一致；1=存在差异（打印逐条 FAIL 明细）；2=网关不可达
```

模块结构：

| 文件 | 职责 |
|---|---|
| `qoder_proxy.py` | 主网关：HTTP 路由、COSY 数据面、双协议转换、用量统计、看板鉴权 |
| `qoder_sign.py` | 自定义 Base64、纯库 AES-128/256 + GCM、RSA、DPAPI、QMC 解密、COSY 签名 |
| `qoder_accounts.py` | 双区账号池、OAuth 设备流、PAT、Token 生命周期、**本机凭证扫描/导入** |
| `qoder_catalog.py` | 双区官方模型快照加载（外部 JSON 优先，内嵌冻结副本兜底）、别名与独占表 |
| `qoder_catalog_intl.json` / `qoder_catalog_cn.json` | 双区官方模型快照（客户端 catalog 逐字段原样导出，`_refresh_catalog.py` 刷新） |
| `qoder_tasks.py` | 签到闭环（含 DISABLED 归一化）、Pro 福利包、批量执行、保活巡检 |
| `qoder_scheduler.py` | 整点排程调度器（09/21 签到 · 22:00 保活，签到能力运行时探测） |
| `qoder_settings.py` | 面板密码 (PBKDF2)、多 API Key 出口绑定、会话管理 |
| `qoder_fingerprint.py` | UID 稳定设备指纹派生 (derive_id) |
| `baseprompt.json` | 官方推理请求体模板 |
| `dashboard.html` | 单文件 Web 看板（本地凭证两步扫描导入 + PAT 导入） |

---

## 七、版本与更新日志 (Changelog)

完整说明见 [Releases](https://github.com/shuishuipingan/qoder2api-hub/releases)。

### v1.1.8

**新增：模型库上下文窗口 / 思考档位可改 + 客户端思考参数全识别（对齐官方 CLI 1.1.62）**
- **看板「模型库」两列直接改**：`上下文窗口`、`思考档位` 变下拉框，按 `<区>:<上游 key>` 存入 `accounts/settings.json` 的 `model_overrides`（两区独立），「跟随官方默认」即恢复；这是**默认值**——客户端请求里带了对应字段时永远以客户端为准；
- **思考预算识别**：`thinking.budget_tokens` / `thinking_budget` / `reasoning_budget_tokens`（Anthropic 风格）按官方阈值表换算档位（`0→none`、`≤1024→low`、`≤8192→medium`、`≤24576→high`、`≤49152→xhigh`、`>49152→max`）再归一化；
- **思考开关识别**：`enable_thinking: false` / `thinking.type=disabled` → `reasoning_effort=none`，并逐字段镜像官方下发 `parameters.enable_thinking`、`parameters.reasoning_budget_tokens`、`model_config.is_reasoning=false`；
- **上下文窗口识别**：请求带 `context_window`（token 数或 `"1M"` 标签）/ `context_length` 即可指定，校验后以官方字段 `parameters.context_length` 下发，越界取最接近的合法窗口并记日志；单窗口模型也给可选项（官方 `WX()` 推导 128K/200K/上限）；
- 优先级：**客户端请求 > 看板每模型默认 > 官方默认档/窗口**。

### v1.1.7

**优化：API Key 出口绑定与出口模式对齐**
- **语义明确**：未绑定出口的 Key = 跟随网关出口模式（单区 = 该区；双区 = 优先区失效自动切换）；绑定到某区的 Key 保持**严格固定、不参与失效切换**（区域独占模型仍按归属自动路由）。
- **设置页实时跟随**：出口下拉与 Key 卡片徽标直接显示当前跟随目标（如「跟随网关出口 · 双区优先国内」），文案随网关模式变化即时刷新；固定出口选项标注「不自动切换」。若绑定的区域当前没有账号，Key 卡片给出「该区域暂无账号」警示。
- **`/v1/models` 口径修正**：未绑定出口（未带 `?realm=` / `X-Realm`）的请求按网关出口模式返回——双区模式返回**两区模型并集**（每条带 `realm` / `realms` 标注，共享模型为 `both`），此前只返回优先区清单，客户端发现不了另一区的模型；非法 `?realm=` 值不再返回空清单。
- **看板统计修正**：双区模式下「N 个 Key 生效」统计全部启用 Key（此前只算优先区绑定 + 跟随，漏算另一区绑定的 Key）。
- **报错更直白**：绑定 Key 的出口没有可用账号时，503 直接说明「固定走X区、Y区仍有可用账号，改为『跟随网关出口』即可自动切换」；Key 与模型区域错配的 400 提示同步更新。

### v1.1.6

**优化：账号调度 v2（对齐主流开源网关做法）**
- **连续失败阶梯退避**（one-api「自动禁用」/ LiteLLM `cooldown` 的轻量版）：账号侧错误（401/403 凭证被拒、会话失效）连续发生时，冷却按 60s → 5min → 15min → 30min 阶梯退避，任意一次成功即清零——坏账号不会再每轮冷却后被反复选中白烧请求；上游瞬时故障（provider_error / TLS 抖动）与客户端参数错误**不进阶梯**（延续"不因上游的锅罚账号"）；单账号池不启用长阶梯，避免把唯一账号冷死。
- **least-busy 在途感知**（LiteLLM `least-busy` 思路）：每个账号维护在途请求数（响应关闭 / with 退出时恰好释放一次），同额度档内优先选**在途最少**的账号（稳定排序：同负载时保持轮询顺序）——长时间流式对话不再把新请求堆到已被占用的账号上。
- 账号视图新增 `inFlight` / `consecutiveFailures` 字段，看板与排障可见。

### v1.1.5

**新增：出口三档模式（仅国际 / 仅国内 / 双区自动切换）**
- 看板顶部出口选择器四选一：`仅国际版` / `仅国内版` / `双区 · 优先国际版` / `双区 · 优先国内版`（原两态切换按钮升级，状态继续落盘、旧配置自动兼容）；
- 双区模式：未绑定出口的请求按「优先出口 → 另一区」路由——首选区**没有可用账号**（全部停用 / 冷却中 / 凭证失效）时自动切到另一区并打日志，首选恢复后自动切回；Key 显式绑定出口、区域独占模型仍严格固定，不受模式影响。

**优化：账号调度改为「额度感知」两档轮询**
- 原有：同区内纯轮询（游标）+ 跳过冷却/禁用账号 + 会话粘性（同一对话固定账号以命中上游 prompt 缓存）；
- 现在：**额度快照已知耗尽**（`isQuotaExceeded` 或"总额度>0 且余额=0"）的账号降为后备档——一档内仍轮询均摊，主档全部不可用（冷却/停用）时后备档照常顶上，账号不会被判死；签到/额度刷新后自动回主档。

### v1.1.4

**新增：统一出站网络层 `qoder_net`（三档代理模式）**
- 项目所有出站请求统一经过 `qoder_net`：模型推理（SSE 流）、模型清单、账号/活动接口、设备授权轮询、`_verify_models.py` / `_diag_gateway.py` 辅助脚本；
- 三档模式：**跟随系统代理**（Windows 优先读 Internet Settings 注册表——即使终端里残留 `HTTP_PROXY/ALL_PROXY` 环境变量也不会被顶掉；拿不到注册表回退环境变量）、**手动指定代理**（`http://host:port`，拒绝 socks 并给出说明）、**直连**；
- 本机回环（localhost / 127.* / ::1）在任何模式下都直连；看板「设置 → 网络代理」即改即生效（写入 `accounts/settings.json`），启动日志与设置页均显示当前生效路径；环境变量 `QD_PROXY_MODE` / `QD_PROXY_URL` 优先级更高（Docker/临时调试）。
- 顺带修复：签到与福利中心「全部账号 (批量)」的「积分额度余额」此前显示**首个账号**的快照，现改为各账号**合计**（悬停显示逐账号明细）；单账号视图保持原样。

### v1.1.3

**修复**
- 活动平台的机器身份**会随时间轮换**：长缓存会拿到过期身份，导致活动列表被过滤、漏领。改为短缓存（120s）+ 签到前强制刷新 + 列表被过滤时刷新重试一次。
- 修复活动列表 404 判定的 `available` 标志（重构后误判）。

**验证**
- 多账户实测（3 账号 / 双区域）：国内版 +100 Credits（余额 594→694）、国际版账号 +100（余额 0→100）、另一国际版账号当日无可领取项——三个账号互不影响，批量签到 `ok=true`。

### v1.1.2

**修复：签到仍领不到（双区域）**
- 活动列表还依赖**真实机器身份**（`Cosy-MachineToken/Type/Code`）：官方桌面端用自带风控桥
  `resources/umid/runtime-info.exe prod --account-stdin` 取值，派生假身份会让服务端**静默过滤掉**
  「每日领取 100 Credits」这类设备定向活动。网关改为调用同一个官方二进制取真值（缓存 6h，
  `QD_NATIVE_IDENTITY=0` 可关闭），失败回退派生值并标注 `identity=derived`。
- 实测：国内版真实领取成功 **+100 Credits（余额 594 → 694）**；国际版与官方桌面端列表逐条一致。

**确认：`reasoning_effort` 按官方档位表归一化**
- 命中/`none` 原样透传；未命中取最近合法档位（同距偏向模型默认档）；有 `thinking_config` 但无档位表的模型
  不下发无效档位；没有 `thinking_config` 的路由模型（`auto`）原样透传；Chat Completions 与 Responses 两条路径都已覆盖。

### v1.1.1

**签到真正做到「能领到」（双区域）**
- **根因**：官方活动平台 `/sash/api/v1/me/campaigns` 必须携带**桌面端请求头**（`Cosy-ClientType: 10` + `Cosy-Version` + 机器头 + `UA: Qoder`）；此前网关用普通 openapi 头请求，服务端不报错但返回**空活动列表** → 看板上永远是"无活动"；
- **现在直接领取**：对 `CLAIMABLE` 的 Credits 活动 `POST /sash/api/v1/me/campaigns/{id}/claim`（逆向自官方 `growth-page/activity-iframe` 页面 JS，**官方幂等**：已领返回 `replayed=true`，不会重复发放）；任务中心显示「可领取 100 Credits —— 点『一键签到』自动领取」/「今日已领取 +100 Credits」；
- 旧 sash 签到接口降级为兜底（仅在仍开放时附一行历史状态）。

**思考档位 `reasoning_effort` 归一化（回答"Qwen3.8-Flash 传档位没反应"）**
- 字段名确认无误（官方 0.4.3 SDK 参数表即 `reasoning_effort`），但**各模型合法档位不同**，且**上游对不支持的档位静默忽略**：`qfmodel` 只认 `low/medium/xhigh`（传 `high` 无效）、`dfmodel` 只认 `low/high/max`（传 `medium/xhigh` 无效）、`qmodel` 只有开/关；
- 网关改为按官方目录的档位表归一化：未命中取最近合法档位（`dfmodel`：`medium→high`、`xhigh→max`；`qfmodel`：`high→medium`、`max→xhigh`）并记日志；无档位表的模型不再下发无效档位；`none` 始终可用于关闭思考；兼容 `reasoning.effort` 与 `thinking.effort/level`。

### v1.1.0

**适配 0.4.3 双区桌面端**
- `cosy-version` 跟随官方 0.4.3 客户端更新为 `1.1.64`（实测模型列表与推理均正常）；
- 国际版推理主机改为官方候选主选 `api1.qoder.sh`，并新增 `api2`/`api3` 自动故障切换（传输层失败即换域名，签名只覆盖 path 不受影响）；
- 双区模型目录快照随新版客户端刷新（价格倍率 / 上下文窗口 / 思考默认档 / `is_sensitive`），新增 `_refresh_catalog.py` 一键随客户端更新并打印差异；
- 修复 `model_entry` 两处计费语义缺陷：低谷价改为官方定义 `峰价 × 折扣`（旧实现把当前价当谷价）、`off_peak` 元数据在非低谷时段也会下发（旧实现整块丢失）。

**修复 issue #1（签到没效果 / 国际版也有签到活动）**
- 签到能力改为**运行时探测**（404/405/410 记不可用，6 小时 TTL 后自动重探），不再按区域硬编码；国际版接口实测 404、国内版旧活动实测 `status=DISABLED`，都会给出明确原因而不是"点了没反应"；
- 接入官方新活动平台 `GET /sash/api/v1/me/campaigns`（双区域可用），任务中心「限时活动」行呈现 `showCampaign/claimable/campaignUrl`；「每日领取 100 Credits」的领取动作在官方客户端内完成（服务端下发 JS，网关只做状态呈现，不执行远端代码）。

**修复 issue #2（DeepSeek-Flash 偶发调用失败）**
- `reasoning_content` 兼容层此前两处断点：按客户端名字前缀 `deepseek` 判定（写 `dfmodel` 时不生效）→ 改按上游 key 判定；`flatten_messages` 压平时无条件丢弃该字段（兼容层"写了但从没发出去"）→ 改按目标模型保留；
- 瞬时故障容错：同账号 1s/2s 重试、HTTP200 建流后信封投 418 时重开上游、短冷却期间"等待续上"而非误报 429。

**HTTP 帧层**
- 流式响应改用 `Transfer-Encoding: chunked` 并以 `0\r\n\r\n` 正确收尾（不再 `Connection: close`），连接可复用；
- 上游长首字延迟期间每 5 秒发送 SSE 注释心跳 `: ping`（`QD_SSE_HEARTBEAT` 可调）；
- 新增 `/ping`（及 `/healthz`、`/livez`、`/readyz`）免鉴权探活端点，另附 `_diag_gateway.py` 自检脚本（默认仅回环，`--allow-remote` 走 SSRF 校验）。

---

## 八、致谢与引用声明 (Credits & References)

本项目在协议兼容、COSY 签名与设备授权链路设计中，深度参考了开源社区现有项目的经验与逆向成果，特此致谢：

- **[mmqz/cpa-multi-plugins](https://github.com/mmqz/cpa-multi-plugins)**：
  - **Qoder 双区域合并插件**：CN/Intl 常量表、OAuth 设备授权与 PAT 交换流程、COSY 签名与自定义 Base64 编码的验证实现；
  - **每日签到与保活排程设计**（09:00/21:00 签到 · 22:00 保活）、按 token 前缀路由的刷新策略。
- **[Liki4/qodercli2api](https://github.com/Liki4/qodercli2api)**：
  - **Qoder OAuth 与推理协议逆向全记录**：设备流 PKCE 细节、`deviceToken`/`jobToken` 端点、SSE 信封与 `[DONE]`/`event:finish` 语义。
- **[Sliverkiss/workbuddy2api](https://github.com/Sliverkiss/workbuddy2api)**：
  - **设备指纹稳定派生设计 (`derive_id`)**、整点排程调度理念、指纹脱敏管线与 DeepSeek 多轮思维链回填。
- **WorkBuddy2API-Hub**：本项目的架构、看板交互与功能矩阵蓝本。

---

## 九、免责声明 (Disclaimer)

1. 本项目为非官方自托管网关，仅供技术研究、逆向协议学习与个人合法授权账号在私有环境测试使用。
2. 本项目不提供任何账号及额度。请严格遵守官方服务条款，禁止用于任何商业转售、恶意并发或违规滥用。
