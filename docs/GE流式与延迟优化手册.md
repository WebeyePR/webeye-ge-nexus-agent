# GE 非流式 / 响应慢 / 回复重复 —— 完整经验与优化手册

> **文档性质**：本项目（webeye-ge-nexus-agent，35 工具 / ADK 2.13 / GE 插件型 Agent）历轮实测经验的**唯一权威汇总**。
> **数据来源**：全部为本项目真机实测，非文档推断。每项结论标注了测量方法与样本量。
> **最后核实**：2026-10-27，生产引擎 `2972905993547022336`。
> **前置文档**：`GE流式与延迟问题核查报告.md`（问题根因证据链）、`非流式与慢问题解决方案.md`（初版方案，部分结论已被本文覆盖）。

---

## 0. 一页速查

| 问题 | 是否解决 | 真正根因 | 处置 |
|---|---|---|---|
| 回复非流式（一次性返回） | ✅ 已解决 | aiplatform 官方模板 `AdkApp` 从不注入 `StreamingMode.SSE` | 子类化 `AdkApp` 重写方法，注入 SSE（`AGENT_ENABLE_SSE_STREAMING` 可回退） |
| 回复慢 | ✅ 已压到代码极限 | ① thinking token ② 缓存每 10 轮重建 ③ `min_instances=0` 冷启动 ④ 模型侧尾延迟 | thinking=LOW + cache_intervals=50 + （可选）min_instances=1 |
| 回复重复显示两遍 | ⚠️ **不能由服务端修复** | GE 前端把 `partial=True` 分片与 `partial=False` 全文**都追加渲染** | 要么接受，要么关 SSE（`=0`）换无重复 |
| 偶发 40~130s 超长延迟 | ⚠️ 部分可控 | 配额限流 429 后的指数退避；冷启动长尾 | 提配额；避免密集压测自我触发限流 |

**延迟基线（当前生产配置，热态）**：中位 3.8~4.3s，P90 9.5s，P95 20s。

---

## 1. 非流式问题

### 1.1 根因（源码级证据）

`vertexai/agent_engines/templates/adk.py` 的 `streaming_agent_run_with_events` 里：

```python
run_config = None
if request.labels:
    from google.adk.agents.run_config import RunConfig
    if "labels" in RunConfig.model_fields:
        run_config = RunConfig(labels=request.labels)   # 只在有 labels 时才构造
async for event in runner.run_async(..., run_config=run_config)
```

`RunConfig` 默认 `streaming_mode = StreamingMode.NONE`，而 GE 固定通过这个方法调用 Agent（整条调用链只有这一个入口）。因此**不注入 SSE 就必然一次性返回**。

**这不是 SDK 版本问题**。三版本模板核对（`streaming_mode` 出现次数）：

| 模板来源 | `streaming_mode` / `StreamingMode` 出现次数 | 核对状态 |
|---|---|---|
| aiplatform 1.136.0 | 0 | 历史会话实测（wheel 已不在本机） |
| aiplatform 1.165.1（1.x 最新） | 0 | 历史会话实测（wheel 已不在本机） |
| aiplatform 2.6.0（当前锁定） | **0** | ✅ 当前环境可复验 |

> 复验命令：`grep -c "streaming_mode\|StreamingMode" .venv/lib/python3.14/site-packages/vertexai/agent_engines/templates/adk.py` → 输出 0。
> 1.x 两版为历史结论，本机已无对应 wheel，引用时请知悉其证据等级低于 2.6.0。

→ **升级 SDK 永远解决不了非流式**，必须自己注入。

### 1.2 解决方案（已实施）

`nexus_agent/deployment/streaming_app.py` 继承 `AdkApp`，重写 `streaming_agent_run_with_events`，与官方模板**逐行对齐**，唯一差异是：

```python
run_config_kwargs = {"streaming_mode": StreamingMode.SSE}
if request.labels and "labels" in RunConfig.model_fields:
    run_config_kwargs["labels"] = request.labels   # 保留 labels 透传
run_config = RunConfig(**run_config_kwargs)
```

**三条纪律**：
1. **升级 aiplatform 前必须先 diff 模板**——这个方法是抄模板的，模板变了必须同步。
2. **保留回退开关** `AGENT_ENABLE_SSE_STREAMING=0`，置 0 时完全走 `super()` 原逻辑。
3. 不要在服务端过滤事件（见 §3 重复问题，任何过滤都会破坏 ADK 契约）。

### 1.3 验证方法（真机）

```bash
# 判据：事件数 > 1 且存在 partial=True，且相邻事件间隔 ~100-300ms
.venv/bin/python tests/remote/remote_regression.py --quick   # 含 GE SSE 通道用例
```

实测通过样本（2026-10-27）：

```
[10.6s] partial=True len=31 '飞书文档的协作能力可概括为以下三点：...'
[10.7s] partial=True len=42 ' **多维融合与极速协同**：...'
[10.9s] partial=True len=38 '...'
...
[11.9s] partial=False len=246 (完整文本)
```

**解析注意**：GE 通道的事件**整行包裹在 `{"events": [...]}` 内**，`stream_query` 通道则事件直接在行内。早期解析脚本漏了信封，导致"partial 满值但文本为空"的假性失败。

---

## 2. 响应慢问题

### 2.1 延迟构成实测分解

> ⚠️ 以下仅采用**本项目（35 工具 / 14.4k 字符指令）**的实测值。

| 环节 | 实测值 | 说明与验证方式 |
|---|---|---|
| GE 基础设施固定成本 | **0.48s** | 同 prompt 经引擎 4.14s vs 直连 3.66s（各 4 次中位） |
| prompt 体积影响 | **可忽略** | 11k token prompt 2.6s vs 小 prompt 4.3s（反更快，属噪声） |
| 会话创建开销 | **1.5~1.6s** | 4 次实测；但复用/新建会话对总延迟无差异（5.62 vs 5.72s） |
| thinking 推理 | **0~10s+** | **最大变量，见 2.2** |
| 生成长度 | 0.3~3s | 输出越长越慢 |

**静态开销实测**（本项目）：

```
static_instruction: 14,450 字符 ≈ 4,816 tokens
35 个工具 schema :   9,599 字符 ≈ 3,199 tokens
单轮固定开销     :            ≈ 8,016 tokens
```

但见上表——这 8k tokens 对延迟影响可忽略（prefill 成本远低于输出成本），**不要为"精简 prompt"做优化**。

### 2.2 thinking token 是首要变量（已优化）

**直连模型对照**（同 prompt `用一个词回答：好`，各 3 次取中位）：

| thinking 档位 | 延迟中位 | thinking tokens |
|---|---|---|
| 默认（未设） | 13.1s | 474 |
| `LOW` | **7.3s** | 未产生/极少 |
| `HIGH` | 24.9s | 828 |

> 复现补充（2026-10-27 再测）：默认 23.7s（thoughts=375）、LOW 2.1s。
> **单次数值波动很大**（默认档见过 13.1s / 23.7s / 8.1s），但**方向性稳定**：
> LOW 始终显著快于默认档。不要引用单次数值作为绝对值，看趋势。

**Agent 级严格 A/B**（6 任务 × 2 轮，含三步法工具链、写操作）：

| 档位 | 正确率 | 总耗时 |
|---|---|---|
| 默认 | 10/12 | 92.1s / 64.3s |
| `LOW` | **12/12** | **30.1s / 35.4s** |

→ LOW 档**快 45%~67%，且正确率更高**（默认档在写操作题型上有 2 次根本没调工具）。

**远程实测对照**（连续 5 轮）：

| 场景 | 默认档 | LOW 档 |
|---|---|---|
| 简单问答首事件中位 | 10.8s | **6.0s** |
| 工具调用整体 | 36.0s | **19.4s**（首响应 22.8→6.1s） |
| 基础对话回归 | 41.4s | **5.8s** |
| 多轮记忆回归 | 332s | **15.8s** |

**配置**：`AGENT_THINKING_LEVEL=LOW`（deploy.py 默认值）。可选 `MINIMAL/LOW/MEDIUM/HIGH`；非法值会被忽略而非报错（防部署后模型调用崩溃）。

> ⚠️ LOW 是**权衡而非真理**。若场景偏重复杂推理（多步合规校验、长链推演），用 `MEDIUM` 重新部署即可。

### 2.3 上下文缓存每 10 轮重建（已优化）

**实测证据**（连续 15 轮观察 `cache_metadata.invocations_used`）：

```
默认 cache_intervals=10：
  第2-11轮  inv=1→10,  fingerprint=423a8f83
  第12轮    inv=1,     fingerprint=7c04e67d   ← 缓存重建了
  刷新点延迟 7.9~15.2s，稳态仅 2.6~3.3s

改为 50 后：
  第11轮 inv=10 → 继续增长到 inv=14，fingerprint 全程恒定
  不再周期性重建
```

**缓存本身是生效的**：第 2/3 轮 `cached_content_token_count` 达 10729/10766，接近全命中。

**配置**：`AGENT_CACHE_INTERVALS=50`（ADK 合法区间 [1,100]，`ttl_seconds` 默认 1800s 足够覆盖）。

### 2.4 冷启动（`min_instances=0` 的固有代价）

**5 次独立采样**（每次空闲 240s 后测首请求）：

```
5.0 / 5.6 / 8.4 / 23.1 / 48.8s     中位 8.4s
```

另有一次空闲 120s 后 **130.8s**。

**关键认知**：这是**长尾分布，不是固定值**。曾只测到 130.8s 一次就差点误报为"必然 130s"，后续 5 次采样推翻了该结论。

**补充观测**：空闲 300s 时冷启动仅 9.2s / 5.6s——短的闲置期（<5min）实例可能尚未真正回收。

| 配置 | 首句体验 | 费用 |
|---|---|---|
| `min_instances=0`（当前） | 连续对话只有第一句慢；**偶发对话（间隔 >5min）每次都要等冷启动** | 闲置不计实例费 |
| `min_instances=1` | 始终温热 | 常驻实例费用 |

→ 面向真实用户且体验优先时，建议改回 `1`。

### 2.5 60s+ 超长延迟：配额限流，不是代码

**识别方法**（三条独立证据）：

1. 容器日志中请求到达后长时间才出现 `Sending out request`（期间在 429 退避）
2. 日志里出现 `429 RESOURCE_EXHAUSTED`
3. **本机直连模型**（完全绕过 GE）同样返回 429 → 证明是项目级配额

**自我干扰陷阱**：压测本身会触发限流。429 后 SDK 指数退避（默认 attempts=5、exp_base=2、max_delay=60），会把单次请求放大成数十秒甚至更长的尖峰。**密集测量时数据完全不可信**。

> 本项目实测极端值：空闲 120s 后首个请求出现 **130.8s**（冷启动叠加退避）。
> 该值不可复现为常态——5 次采样中位仅 8.4s，见 §2.4。

**纪律**：测延迟前静默 3~5 分钟；一次只测一组；发现 429 立即停止并等待。

**出路**：申请提升配额（`generate_content` 的 QPM/TPM），而非改代码。

### 2.6 已排查但**不成立**的假设（避免重复劳动）

| 假设 | 实测结论 |
|---|---|
| prompt 太大导致慢 | ❌ 11k token prompt 反而比小 prompt 快（2.6s vs 4.3s） |
| 会话复用能省时间 | ❌ 复用 5.62s vs 新建 5.72s，无差异 |
| `create_session` 是瓶颈 | ❌ 稳定 1.5~1.6s，且 GE 侧开销可忽略 |
| 引擎层本身慢 | ❌ 额外开销仅 0.48s |
| 静态指令 14,450 字符臃肿 | ❌ 图片章节占 28.2% 但是业务决策树（`download_images` 判定）；且实测 **11k token prompt 反而比小 prompt 快**（2.6s vs 4.3s），说明 prefill 不是瓶颈 |
| 35 工具冗余（`_flat` 变体） | ❌ 是为避免 JSON 转义的结构化变体，标注"优先推荐" |
| `cache_intervals` 是抖动主因 | ❌ 20 轮采样异常出现在第 2/6/19 轮，非 10 的倍数——**假说被自己数据否定** |

> 最后一条特别重要：我最初把缓存刷新当成主要抖动源，但采样数据不支持。缓存刷新只解释**周期性**开销，**15% 的随机抖动是模型侧尾延迟**（直连也有 7.6x 波动，1.3~10.0s）。

---

## 3. 回复重复显示两遍

### 3.1 完整证据链（5 轮实测）

**证据 1：事件内容等长**

```
partial=True  ×7  → 分片累加 = 182 字符
partial=False ×1  → 完整全文 = 182 字符（内容完全相同）
```

**证据 2：事件 id 全部共享（关键）**

```
partial=True  id=8f4cdca9-6af8…  ×7
partial=False id=8f4cdca9-6af8…  ×1
事件数=6，不同 id 数=1
```

这是 ADK 官方定义的**正确形态**——本地流式的分片与收尾响应共享一个 id，客户端应**按 id 替换**而非追加。

**证据 3：会话历史干净**

`get_session` 仅 1 条 `partial=False`（102 字符），partial 事件不入库（`runners.py` 明确 `if output_event.partial is not True: append_event`）。

→ **翻历史记录不会重复**，重复只发生在实时渲染阶段。

**证据 4：非流式通道无此问题**

`stream_query` 仅 1 个事件、`partial=None`、85 字符。

### 3.2 根因

**ADK 官方文档**（adk.dev/live/events）原文：

> `partial=True`：only the new text since the last event
> `partial=False`：the full merged text for this segment
> ADK accumulates the chunks for you, so a `partial=False` event already holds the sum of the preceding `partial=True` chunks

**ADK issue #7395** 明确点名：

> adk-samples' `long-horizon-harness` notes that clients concatenating every artifact part (**it names Gemini Enterprise**) render the reply twice.

→ **GE 前端把「流式分片」与「最终全文」都追加渲染了**。这是 GE 的渲染行为，服务端无法干预。

### 3.3 三个可选处置

| 方案 | 效果 | 代价 | 评价 |
|---|---|---|---|
| **A. 保持流式** | 有打字机效果 | 回复显示两遍 | 需向 GE 反馈该问题 |
| **B. 关 SSE**（`AGENT_ENABLE_SSE_STREAMING=0`） | **无重复** | 失去逐字效果，等全文生成完才显示 | 总耗时不变，仅首屏感知变化 |
| C. 服务端不发 final 事件 | 理论无重复 | **破坏 ADK 契约** | ❌ **禁止**：final 是权威全文且负责落库与多轮上下文，砍掉会丢历史 |

**决策建议**：企业场景下"同一段话显示两遍"比"没有逐字效果"更伤专业观感，且延迟已优化到 3~8s，倾向 B。但这是体验权衡，需业务判断。

### 3.4 排查时的认知陷阱

ADK issue #7395 讨论的是 **A2A 远程**路径（分片与 final **id 不同**，客户端无法按 id 替换）。我们的是**本地流式**（id 共享）。两者现象相似但成因不同，**不要拿 A2A 的结论套本地**，也不要按 A2A 的方案去改本服务端。

---

## 4. 完整配置清单（当前生产）

引擎 `2972905993547022336` 实读值：

| 环境变量 | 值 | 作用 |
|---|---|---|
| `MODEL_NAME` | `gemini-3.8-flash` | 主模型 |
| `AGENT_FALLBACK_MODELS` | `gemini-3.7-flash,gemini-3.6-flash` | 跨模型降级链（429/500/502/503/504 触发） |
| `AGENT_THINKING_LEVEL` | `LOW` | 思考深度 |
| `AGENT_CACHE_INTERVALS` | `50`（代码默认） | 缓存复用次数 |
| `ADK_MAX_LLM_CALLS` | `200` | 单次运行模型调用上限 |
| `AGENT_ENABLE_SSE_STREAMING` | 未设（默认 1） | 流式开关，置 0 回退非流式 |
| `AGENT_ENABLE_TOOL_CONFIRMATION` | `1` | 高危工具硬确认 |
| `GOOGLE_GENAI_USE_ENTERPRISE` | `1` | 生产必须 Vertex；**deploy.py 硬编码，不读本地 .env** |
| `GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY` | `false` | 遥测关闭 |

部署规格：`min_instances=0`、`container_concurrency=36`、`cpu=4`、`memory=8Gi`。

> `GOOGLE_GENAI_USE_ENTERPRISE` 必须硬编码：本地开发常设 0（AI Studio 通道），若透传会把生产引擎错配成 AI Studio。

---

## 5. 验证工具与方法论

### 5.1 工具

| 脚本 | 用途 |
|---|---|
| `tests/remote/remote_regression.py` | 12 用例真实回归（`--quick` 6 用例冒烟） |
| `tests/remote/long_run_sample.py` | 长时采样，自动分离冷启动/稳态，输出 P50-P99 |

```bash
# 部署后冒烟
ENGINE_NAME=projects/.../reasoningEngines/<id> \
  .venv/bin/python tests/remote/remote_regression.py --quick

# 长时采样（含冷启动观测）
.venv/bin/python tests/remote/long_run_sample.py --rounds 40 --idle 180
```

### 5.2 三条测量纪律（都是踩过坑换来的）

1. **测延迟前先静默 3~5 分钟**——压测自身会触发 429，导致数据完全失真。
2. **区分冷启动与稳态**——首轮数据不能代表常态，`long_run_sample.py` 已内置分段统计。
3. **单次测量不能当结论**——冷启动我测到过 130.8s，5 次采样后中位数只有 8.4s。**任何延迟结论都要有样本量和分布**。

### 5.3 日志排查注意

- **必须按 `reasoning_engine_id` 过滤**：同项目下可能有多个引擎（含他人引擎），曾把别的引擎的 `gemini-3.5-flash` 日志误认为我们的配置错误。
- **日志有写入延迟**：刚发生的请求可能查不到，需放宽时间窗或稍后重查。
- `RuntimeError: Event loop is closed`（`AsyncClient.aclose()`）是**请求完成后的清理噪音**，不影响响应，不要当故障排查。

---

## 6. 结论：代码侧优化已到极限

当前实测（热态，40 轮长时采样）：**中位 4.26s、P90 9.47s、P95 20.13s、P99 52.31s**；
cache_intervals 调优后的 20 轮采样达到 **20/20 正常、中位 3.8s**。

**剩余不可优化的部分**：

| 项 | 量级 | 能否优化 |
|---|---|---|
| GE 基础设施固定成本 | ~0.48s | ❌ 平台成本 |
| thinking 推理 | 已压到 LOW，仍占主要部分 | ⚠️ 更低档（MINIMAL）可能损质量，需 A/B |
| 模型侧尾延迟 | 15% 请求 >15s，直连同样存在 | ❌ 模型特性 |
| 冷启动 | 中位 8.4s，长尾可达 130s | ⚠️ 改 `min_instances=1` |
| 配额限流 | 突发可达 60s+ | ⚠️ 需提配额 |

**再往下优化只有三条路**：

1. **提配额**（`generate_content` 的 QPM/TPM）—— 解决 429 长尾
2. **改架构**：MCP 直连 GE 绕过 Agent Runtime —— 消除冷启动与实例开销，但需重构
3. **接受现状** —— 当前 3~4s 中位对工具型 Agent 属合理范围

---

## 7. 操作速查（遇到问题时按此顺序排查）

### 7.1 「感觉慢」时的排查顺序

```
① 先看是不是冷启动？
   空闲 >5min 后第一次问 → 中位 8.4s，长尾可达 130s，属 min_instances=0 代价
   连续问第 2 句起 → 3~8s 属正常
   ↓ 不是冷启动
② 再看是不是 429 限流？
   查引擎日志：gcloud logging read '... AND textPayload:"429"' --freshness=30m
   有 429 → 等 3~5 分钟后单次重测；持续出现则需提配额
   ↓ 也没有
③ 检查 thinking 配置是否生效
   远程 env 应含 AGENT_THINKING_LEVEL=LOW
   ↓ 也正常
④ 检查是否缓存频繁重建
   连续观察 cache_metadata.invocations_used，若很快归零 → 确认 AGENT_CACHE_INTERVALS=50
   ↓ 都正常
⑤ 那就是模型侧尾延迟（15% 请求 >15s，直连同现象）→ 代码侧无解
```

### 7.2 常用命令

```bash
# 查引擎真实配置（确认 env 是否生效）
TOKEN=$(gcloud auth application-default print-access-token)
curl -s -H "Authorization: Bearer $TOKEN" \
  "https://us-central1-aiplatform.googleapis.com/v1/projects/839062387451/locations/us-central1/reasoningEngines/<ID>" \
  | python3 -c "import json,sys; d=json.load(sys.stdin); \
    [print(e['name'],'=',e.get('value')) for e in d['spec']['deploymentSpec']['env']]"

# 部署后冒烟（6 用例）
ENGINE_NAME=projects/839062387451/locations/us-central1/reasoningEngines/<ID> \
  .venv/bin/python tests/remote/remote_regression.py --quick

# 长时采样（含冷启动观测）
.venv/bin/python tests/remote/long_run_sample.py --rounds 40 --idle 180

# 切配置重新部署
AGENT_THINKING_LEVEL=MEDIUM AGENT_MIN_INSTANCES=1 bash deploy.sh deploy
```

### 7.3 改配置前必读

| 想改什么 | 风险 |
|---|---|
| `AGENT_ENABLE_SSE_STREAMING=0` | 消除重复，但失去打字效果（这是唯一能解重复的开关） |
| `min_instances=1` | 消除冷启动，但产生常驻实例费 |
| `AGENT_THINKING_LEVEL` 调到 MEDIUM/HIGH | 复杂推理可能更准，但延迟显著上升（HIGH 实测 24.9s） |
| 精简静态指令 / 删工具 | ❌ 已实测无效（prefill 非瓶颈），且有功能损失风险 |
| 服务端过滤 final 事件 | ❌ **禁止**，破坏会话历史与多轮上下文 |

---

## 8. 版本与归属提醒（避免混淆）

本仓库 `docs/` 下有三份相关文档，定位不同：

| 文档 | 性质 | 时效 |
|---|---|---|
| `GE流式与延迟问题核查报告.md` | 问题**根因证据链**（官方文档 + issue 逐条核对） | 2026-10-26，结论仍有效 |
| `非流式与慢问题解决方案.md` | **初版方案**（部分结论已被实施后的实测覆盖） | 2026-10-26，参考价值有限 |
| **本文** | **实测经验与最终配置（权威）** | 2026-10-27 |

> ⚠️ **注意本机存在并行项目** `webeye-ge-nexus-agent-new`（又称 Nexus Agent Next，13 工具、用 `AGENT_THINKING_BUDGET`、指令 842 tok）。两者的延迟数据**不可混用**：本项目是 35 工具、`AGENT_THINKING_LEVEL`、指令 14,450 字符。历史上曾出现把该项目的实测数据误记入本项目记录的情况，引用数据前请先核对工具数与变量名。
