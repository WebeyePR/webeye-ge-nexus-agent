# Gemini Enterprise 流式与响应延迟问题 · 核查报告

> **核查日期**：2026-10-26
> **核查对象**：webeye-ge-nexus-agent（google-adk==1.24.1，部署于 Vertex AI Agent Engine / Agent Runtime，注册到 Gemini Enterprise）
> **核查方法**：官方文档逐条原文核对（WebFetch 抓取页面正文）+ GitHub 官方仓库 issue 核对 + 本地部署环境源码静态验证
> **所有外部链接均于 2026-10-26 访问确认有效**

---

## 一、结论摘要

| # | 用户观察现象 | 核查结论 | 置信度 |
|---|---|---|---|
| 1 | 回答非流式、全部完成后一次性返回 | **非 GE 不支持，也非 ADK 版本 bug**。根因是部署模板 AdkApp 的 `streaming_agent_run_with_events` 方法调用 `runner.run_async()` 时未启用 `StreamingMode.SSE`（默认 `NONE` = 每轮一个完整事件），而 GE 恰好固定通过该方法调用 Agent | 高（源码级证据） |
| 2 | 无思考过程 | **产品级限制**：ADK 事件流默认不携带 thought 内容；GE 前端不为注册的 ADK Agent 渲染 thinking/中间步骤面板。目前无官方配置开关 | 中高 |
| 3 | 响应慢 | **七类因素叠加**（详见第四节 D1–D8）：① 冷启动（官方实测 4.7s vs 温启动 0.4s）② 并发配置未调优（默认 9，建议 9 的倍数）③ 编排链路工具往返放大 ④ 端点容量争用 ⑤ 依赖版本未全固定 ⑥ 模型 thinking 默认开启（项目未配置 thinking_level）⑦ 大 Prompt 重复发送 + 会话历史全量加载。本项目 `deploy.py` 未做任何扩缩容/并发配置 | 高 |
| 4 | 是否需要升级 ADK 版本 | **升级非必需**：ADK 1.24.1 已完整支持 `RunConfig`/`StreamingMode`（本地源码验证）；最新版模板同样未启用 SSE，升级解决不了流式问题 | 高（源码级证据） |
| 5 | 若不想改 Agent 侧，有无其他出路 | **有**：架构替代路径——把能力封装为自定义 MCP 服务器直连 GE 注册（GE 原生流式、无 Agent Runtime 冷启动/并发问题），见第六节方案 E | 中高（官方文档确认能力，适配成本需评估） |

---

## 二、问题一：非流式 —— 完整证据链

### C1. Gemini Enterprise 以流式 API 调用 Agent（官方文档）

Agent Runtime 运行时契约定义了流式端点 `/api/stream_reasoning_engine`，响应为 ndjson 逐行 JSON 块；`reasoningEngines/streamQuery` REST API 与 Playground 均要求 `stream_query` 类方法（`api_mode: "stream"`）。

- 来源（官方）：[Agent Platform runtime contract](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract)（最后更新 2026-10-22）
- 官方流式调用示例（`run_sse` 端点 + `"streaming": true`）：[Use an agent](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-an-agent)（最后更新 2026-10-23）

**结论：GE 平台侧支持流式调用，"GE 不支持流式"不成立。**

### C2. GE 实际调用 ADK Agent 的固定方法是 `streaming_agent_run_with_events`（实测请求日志）

adk-go issue #770 中，开发者抓取到 GE 调用 Agent Engine 的真实请求体：

```json
{
  "class_method": "streaming_agent_run_with_events",
  "input": { "request_json": "{ message / session_id / user_id }" }
}
```

- 来源：[google/adk-go#770](https://github.com/google/adk-go/issues/770)（含完整请求日志，2026-04）
- 佐证 1：[Google 开发者论坛](https://discuss.google.dev/t/agent-returned-an-error-400-when-registering-to-google-enterprise/369919)（2026-06）——Agent Studio 部署的容器缺少该方法时，GE 调用直接报 `InvocationMethodNotFoundError: User-specified method streaming_agent_run_with_events not found`，反向证实 GE 固定调用此方法
- 佐证 2：[google/agents-cli#80](https://github.com/google/agents-cli#80)（2026-08 提交，2026-09-01 官方确认修复）——原文明确写道："**stream_query is the contract the Vertex AI Console Playground and Gemini Enterprise (ADK registration) both use**"（较新的 agents-cli 部署路径中该方法名映射为 `stream_query`，本质同为流式契约）
- 本地佐证：项目 `.venv` 中 ADK 1.24.1 的 `google/adk/cli/cli_deploy.py:389` 将 `streaming_agent_run_with_events` 注册为部署操作，docstring 原文注明 *"primarily meant for invocation from AgentSpace"*

**边界澄清（官方文档）**：Gemini Enterprise 的程序化调用 API `streamAssist`（StreamAssist）**明确不支持 ADK 与 A2A agent**——官方限制条款原文："通过 `streamAssist` 不支持注册到 Gemini Enterprise 应用的 A2A 或 ADK 智能体。"即 ADK agent 在 GE 中的调用只能走 Agent Runtime 的流式调用链（C2 所述路径），两条通道互不混淆。

- 来源（官方）：[使用 StreamAssist API 调用特定代理](https://docs.cloud.google.com/gemini/enterprise/docs/invoke-agent-streamassist)（最后更新 2026-10-24，"限制"一节）

### C3. 根因：模板实现未启用 SSE（本地源码验证 ⭐ 最关键证据）

**本项目实际部署的模板源码**（`.venv/lib/python3.14/site-packages/vertexai/agent_engines/templates/adk.py:1143-1217`）：

```python
async def streaming_agent_run_with_events(self, request_json: str):
    ...
    async for event in runner.run_async(
        user_id=request.user_id,
        session_id=session.id,
        new_message=message_for_agent,
    ):          # ← 未传 run_config
```

**最新版验证**：下载 PyPI 最新 `google-cloud-aiplatform 2.6.0` wheel 检查（`vertexai/agent_engines/templates/adk.py:1496` 起），该方法仅在存在 `labels` 时构造 `RunConfig(labels=...)`，**从不设置 `streaming_mode`** —— 最新版行为相同，说明这是模板长期现状，非本项目引入的问题。

### C4. SSE 与默认行为语义（官方 ADK 文档）

- **`StreamingMode.NONE`（默认）**："The runner returns one complete response per turn."（每轮返回一个完整响应）
- **`StreamingMode.SSE`**："The runner yields partial events as the LLM generates, enabling typewriter-style UIs and real-time chat displays."
- 启用方式：`RunConfig(streaming_mode=StreamingMode.SSE)` 传入 `runner.run_async(..., run_config=config)`

- 来源（官方）：[ADK Runtime Configuration](https://adk.dev/runtime/runconfig)（`google.github.io/adk-docs` 301 重定向至 adk.dev，为 ADK 官方文档站）

**C3 + C4 合并推导：GE 收到的事件流是"每轮一个完整事件"→ 用户看到一次性返回。证据链闭环。**

### C5. GE 前端具备渲染流式 chunk 的能力（间接证据，需实测）

[google/adk-python#7395](https://github.com/google/adk-python/issues/7395)（2026-10，官方成员已在 issue 中确认 bug）中，adk-samples 的 `long-horizon-harness` 明确指出 **Gemini Enterprise 属于"会把每个 artifact part（流式 chunk）逐一拼接渲染"的客户端**，因此存在流式内容与最终事件重复显示的已知 bug。修复 PR：[#7396](https://github.com/google/adk-python/pull/7396)（**截至 2026-10-26 核查日状态为 open，尚未合并**）。

⚠️ **精确性说明**：该 issue 的场景是 A2A 协议路径，而非 Agent Runtime 的 `streamQuery` 路径。它能证明 GE 前端具备渲染流式 chunk 的能力，但开启 SSE 后 GE 界面的实际渲染效果**必须实测确认**（是否存在重复显示）。官方无明确文档描述 `streamQuery` 路径下 GE 前端对 partial 事件的处理策略。

---

## 三、问题二：无思考过程

| 事实 | 依据 |
|---|---|
| ADK 事件流默认不含 Gemini thinking 内容 | 官方 RunConfig 文档中 thought 相关默认关闭（`include_thoughts_from_other_agents`: "Disabled by default"，[adk.dev/runtime/runconfig](https://adk.dev/runtime/runconfig)） |
| GE 前端不为注册 ADK Agent 提供 thinking/中间步骤展示面板 | 未找到任何官方文档描述该能力；GE 聊天界面对 Agent Runtime Agent 仅展示最终回复（结合 C2/C3 的调用链，中间事件不构成思考面板）。**此为产品现状推断 + 缺证，非官方声明** |
| 对比：Vertex AI Console Playground 可见完整事件流/轨迹 | [Use an agent](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-an-agent)：启用 OpenTelemetry trace 后可查看 Trace/Event/State |

**结论：想获得"思考过程"展示，现阶段在 GE 聊天界面无官方开关；只能靠流式 partial 事件改善"感知等待"，或自建前端消费完整事件流。**

---

## 四、问题三：响应慢 —— 官方数据 + 本项目现状核对

### D1. 冷启动（官方实测数据）

> "向具有默认 `min_instances=1` 的智能体发送 300 个并发请求：**冷启动（首次运行）平均延迟约 4.7 秒；温启动（立即第二次运行）平均延迟约 0.4 秒**。超过 4 秒的开销几乎完全是由于启动新实例造成的。"（注：官方中文页术语为"温启动"）

官方缓解方案：调高 `min_instances`；对脉冲流量场景设为匹配基线负载。

**取值范围（官方 REST 参考文档权威确认）**：`minInstances` 默认 1，范围 **[0, 10]**；`maxInstances` 默认 100，范围 [1, 1000]（启用 VPC-SC 或 PSC-I 时为 [1, 100]）。

- 来源（官方）：[Optimize and scale Agent Runtime performance](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/optimize-and-scale)（最后更新 2026-10-22）
- 来源（官方，字段权威定义）：[REST Resource: projects.locations.reasoningEngines — DeploymentSpec](https://docs.cloud.google.com/vertex-ai/docs/reference/rest/v1beta1/projects.locations.reasoningEngines)（`minInstances`: "Defaults to 1. Range: [0, 10]."）

**本项目现状**：`nexus_agent/deployment/deploy.py:144-158` 的 `agent_engines.create()` 调用**未设置 `min_instances`** → 承受默认冷启动惩罚。

### D2. 并发模型错配（官方实测数据）

> 默认 `container_concurrency` 面向同步代码。官方 REST 参考明确：`containerConcurrency` **默认值为 9**，"Recommended value: 2 * cpu + 1"；Optimize and scale 页进一步说明 9 代表每容器并行运行的 agent 进程数，单进程并发 = `container_concurrency / 9`。"对于异步代理（例如基于 ADK 的代理），请先将 `container_concurrency` 设置为 **9 的倍数（例如 36）** 作为起步值。"官方压测：300 并发下，默认配置最大延迟飙升至 **60 秒**；设为 36 后降至约 **7 秒**。过高有 OOM 风险。
>
> `resource_limits` 官方默认 `{"cpu": "4", "memory": "4Gi"}`；CPU 仅支持 '1'/'2'/'4'/'6'/'8'，内存支持 '1Gi'–'32Gi'。

- 来源：[Optimize and scale](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/optimize-and-scale)、[REST Reference — DeploymentSpec](https://docs.cloud.google.com/vertex-ai/docs/reference/rest/v1beta1/projects.locations.reasoningEngines)（`containerConcurrency`: "Recommended value: 2 * cpu + 1. Defaults to 9."）

**本项目现状**：`deploy.py` **未设置 `container_concurrency` 与 `resource_limits`**。

### D3. 编排链路放大延迟（官方最佳实践）

> "尽量减少顺序多智能体跃点和冗余总结：每增加一次子智能体交接或工具往返，就会增加该轮对话的模型推理延迟。避免使用深层顺序智能体链，并使用 `AgentTool(..., skip_summarization=True)` 封装子智能体。"

- 来源（官方）：[Register and manage an ADK agent in Gemini Enterprise](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-adk-agent)（最后更新 2026-10-24）

**本项目现状**（2026-10-26 代码实查）：341 条命令注册表（`lark_registry/commands.json` 精确 308 条 + `gws_registry/commands.json` 精确 33 条，合计 341）+ `discover → get_spec → execute` 三步自省工具链（`tools.py:1400` `discover_lark_operations`、`tools.py:449` `discover_google_workspace_operations`，Prompt 中亦引导模型按三步法调用，见 `agent.py:88`）+ 每次命令执行均通过 `subprocess.run` spawn `lark-cli`/`gws` 子进程（`infrastructure/cli_client.py:115`，默认超时 120s）。单轮对话可能包含多次模型推理 + 多次工具往返。这与官方"减少工具往返"的建议存在结构性张力，是延迟的重要放大项。

### D4. 模型端点区域争用（官方建议 + ⚠️ 需知悉的权衡）

> "当 ADK 智能体在 Vertex AI 上调用 Gemini 模型时，请使用 **global 端点**（或预配吞吐量）来减少区域容量争用，并针对外部工具调用配置明确的连接超时和有界重试限制，以免缓慢的下游依赖项导致级联轮次超时。"

- 来源（官方）：[Register and manage an ADK agent](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-adk-agent)（最后更新 2026-10-24）

**⚠️ 官方同时提示的权衡**（易被忽略）：

> "使用全域端點時，Google 可以從所用模型支援的任何區域處理及提供要求，**在某些情況下，這可能會導致延遲時間較長**。全域端點有助於提高整體可用性，並減少錯誤。"

即 global 端点的核心收益是**可用性与减少 429/503 容量类错误**（容量限额按"每模型 × 每区域 × 每分钟"计），单次请求延迟反而可能更长。官方 Vertex AI 端点文档同样表述为"improve overall availability while reducing resource exhausted (429) errors"。

- 来源（官方）：[合作夥伴模型定價 — 區域、全域和多區域端點](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/use-partner-models)（繁中版原文核对）
- 来源（官方）：[Deployments and endpoints](https://cloud.google.com/vertex-ai/generative-ai/docs/models/gemini-supervised-tuning-region-settings)（"Global endpoints cover the entire world and provide higher availability and reliability than single regions"；"can improve overall availability while reducing resource exhausted (429) errors"）

**本项目现状**：`.env` 中 `DEPLOY_LOCATION="us-central1"`（区域端点）。注意：Agent Runtime 的部署位置需与 GE App 位置兼容（同页位置兼容表：GE App 为 `global` 时，Runtime 可在任意受支持区域；GE App 为 `us`/`eu` 时有对应约束）。
**精确表述**：改用 global 端点主要解决"区域容量争用导致的排队/429/503 重试型延迟"，并非缩短单次推理延迟；若当前延迟主要来自容量争用则有效，若纯粹是模型推理耗时则收益有限。

### D5. 版本一致性（官方最佳实践）

> "请在部署 requirements 列表中固定与本地构建环境使用的 `google-adk`、`google-cloud-aiplatform` 和 `cloudpickle` **完全相同的版本**，以避免反序列化开销和初始化延迟。"

**本项目现状**：`pyproject.toml` 中 `google-adk==1.24.1` 已固定 ✅；但 `google-cloud-aiplatform>=1.135.0` 为**开放下限** ⚠️——云端构建时会拉取最新版，与本地 `.venv` 实际版本可能不一致，既违反官方建议，也会使下文流式改造面临模板版本漂移风险。**建议固定 `google-cloud-aiplatform` 与 `cloudpickle` 的精确版本。**

### D6. 模型思考（thinking）默认开启（官方文档 + 本项目未配置）

> 思考功能在受支持的 Gemini 模型中**默认启用**。Gemini 3 模型使用 `thinking_level` 参数（可选 `MINIMAL`/`LOW`/`MEDIUM`/`HIGH`），"此参数设置了离散推理层级，以便您**在延迟时间和推理深度之间进行优化**"。
> ⚠️ `thinking_budget` 参数已针对 Gemini 3 及更高版本弃用——在 Gemini 3 模型上使用它"可能会导致间歇性 `400: INVALID_ARGUMENT` 错误"。

- 来源（官方）：[思考型（Thinking）— Gemini Enterprise Agent Platform](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/capabilities/thinking)（2026-10-26 抓取核对）
- 延迟量化佐证（官方 BigQuery 文档转述）："By setting the thinking_budget to 0, you can **reduce the model's response latency**…For Gemini 3.0 and later models, the thinking_level parameter replaces the thinking_budget…Setting a lower thinking level prioritizes latency"：[AI.GENERATE function](https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/bigqueryml-syntax-ai-generate)

**本项目现状**：`nexus_agent/` 全目录 grep 核实**未配置任何 `thinking`/`GenerateContentConfig` 参数** → 模型按默认档位思考。本项目使用 `gemini-3-flash-preview`（Gemini 3 系列），每轮工具决策与最终作答都会包含默认档位的思考 token 耗时；若追求响应速度，可在模型配置中调低 `thinking_level`（注意：不要使用已弃用的 `thinking_budget`）。

### D7. 上下文缓存与 Prompt 结构（官方文档）

> 上下文缓存"permet de réduire le coût et la **latence** des requêtes adressées à Gemini qui contiennent du contenu répété"（降低含重复内容请求的成本与延迟）。**隐式缓存默认开启**，缓存 token 享受 90% 折扣，缓存前缀最长保留 24 小时；提升命中率的官方建议："Placez les contenus volumineux et courants au début de votre requête"（将大体积、高复用内容放在请求开头）。

- 来源（官方）：[Context caching overview — Gemini Enterprise Agent Platform](https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview)（2026-10-26 抓取核对）
- 支持模型清单中包含 **Gemini 3 Flash preview**（本项目所用模型，位于 in-memory 缓存支持列表）

**本项目现状**：系统 Prompt 体量较大（`agent.py` 内嵌完整工具使用规范与三步法指引，每轮重复发送）。隐式缓存已默认可用，但命中率取决于前缀稳定性——Prompt 中的动态内容（时间戳、随机排序等）应避免置于前缀位置。

### D8. 会话历史加载（官方 ADK 文档）

> ADK `RunConfig.get_session_config`："Limits which events are fetched when loading a session. Use `num_recent_events` or `after_timestamp` to **avoid loading the full event history on every invocation**."（避免每次调用加载完整会话历史）

- 来源（官方）：[ADK Runtime Configuration](https://adk.dev/runtime/runconfig)

**本项目现状**：未对会话加载做裁剪（默认全量加载）。长会话场景下，每轮需先加载全部历史事件再推理，历史越长单轮延迟越高。可评估在 `RunConfig` 中设置 `num_recent_events`（该项与方案 B 的流式改造共用同一 `RunConfig` 注入点）。

---

## 五、版本问题专项核查

| 检查项 | 结果 | 证据 |
|---|---|---|
| ADK 1.24.1 是否支持 `RunConfig`/`StreamingMode.SSE` | ✅ 支持 | 本地源码：`.venv/.../google/adk/agents/run_config.py:52-70`（`StreamingMode` 枚举）、`:220`（`streaming_mode` 字段，默认 `NONE`） |
| ADK 1.24.1 部署模板是否含 `streaming_agent_run_with_events` | ✅ 存在 | `.venv/.../vertexai/agent_engines/templates/adk.py:1143` |
| 最新版模板是否已修复（启用 SSE） | ❌ 未修复 | PyPI 最新 `google-cloud-aiplatform 2.6.0` wheel：`RunConfig` 仅用于 labels，从不设置 `streaming_mode` |
| 当前最新 ADK 版本 | 2.13.0（2026-10-26 PyPI 实查） | `pip index versions google-adk` |
| 升级 ADK 能否解决流式 | ❌ 不能（改造点在部署模板层） | 见 C3/C4 |
| 升级 ADK 是否另有价值 | ✅ 2.x 生态更成熟（agents-cli 部署路径 2026-09 修复了 stream_query 空响应 bug） | [google/agents-cli#80](https://github.com/google/agents-cli/issues/80) |

---

## 六、改进方案（按优先级）

### 方案 A：部署参数调优（治"慢"，零代码风险，纯配置）

修改 `deploy.py` 的 `agent_engines.create()` 调用，增加部署规格参数：

- `min_instances`：按基线并发设定（≥1，官方上限 10）
- `container_concurrency`：ADK 异步 Agent 起步建议 36（9 的倍数）
- `resource_limits`：按官方建议配置 CPU/内存，防 OOM
- 模型调用改 global 端点；固定 `google-cloud-aiplatform`、`cloudpickle` 精确版本

依据：[Optimize and scale](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/optimize-and-scale)、[Register and manage an ADK agent](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-adk-agent)

### 方案 B：启用 token 级流式（治"非流式"，需实测验证）

在 `deploy.py` 中子类化 `AdkApp`，重写 `streaming_agent_run_with_events`（复制模板原实现，仅在 `runner.run_async(...)` 处追加 `run_config=RunConfig(streaming_mode=StreamingMode.SSE)`），替换 `deploy.py:115` 的 `agent_engines.AdkApp(...)` 后重新部署。

**风险与对策**：
1. 重写代码必须与云端实际安装的模板版本匹配 → 配合方案 A 固定 `google-cloud-aiplatform` 版本
2. GE 前端对 partial 事件的渲染未经官方文档确认，存在重复显示先例（C5）→ 上线前在 GE 界面用长回答实测
3. 非官方默认路径 → ADK/aiplatform 升级时需回归测试

### 方案 C：编排层降延迟（中长期）

- 审查高频路径，用 `registry.py` 精确匹配替代 `discover → get_spec` 的多轮自省
- 子 Agent 交接处使用 `skip_summarization=True`
- 减少单轮对话内的顺序工具往返

依据：[Register and manage an ADK agent](https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-adk-agent) 性能最佳实践节

### 兜底方案 D：A2A + 自定义服务

若 B 实测不通过且流式为硬需求，改走 A2A 协议部署到 Cloud Run，完全掌控 SSE 行为。工程量最大，仅在前述方案失效时考虑。

### 方案 E：架构替代 —— 自定义 MCP 服务器直连 GE（绕开 Agent Runtime）

核心思路：把本项目 lark-cli / gws 能力封装为一个 MCP 服务器（StreamableHTTP 传输）部署到 Cloud Run，在 GE 后台直接注册——Standard/Plus 版走 *Create data store → Custom MCP Server* 填表流程，Business 版走 *Connected apps → Add MCP Server*，注册环节零代码。此后工具编排由 GE 原生完成：GE 自带聊天界面与模型，**天然流式**，不存在模板未启用 SSE 的问题，也没有 Agent Runtime 的冷启动与并发调优问题（同时治"非流式"和"慢"）。

**官方限制**（来源 [22]，2026-10-26 原文核对）：
- 仅支持 StreamableHTTP 传输，不支持旧版 SSE 传输
- 公网/私有 MCP 服务器均须使用公开可信 CA 签名的 TLS 证书（不支持自签名）
- 单个数据存储启用的操作一次最多 **100 个**（本项目 341 条命令需按域拆分或精选高频命令）
- 导入的工具默认禁用，需在 Actions 页手动启用；调用默认需用户确认，只读工具可用 `readOnlyHint` 注解跳过
- Cloud Run 仅 `.run.app` 默认域名自动附带 ID 令牌认证，自定义域名不会

**代价**：失去 ADK 层的多 Agent 编排、会话管理与命令自省 Prompt 逻辑，改由 GE 模型直接编排工具；需新开发并运维一个 MCP 服务器（如 FastMCP）。

**方案 D vs E 选择指引**：

| | D：A2A + 自定义服务 | E：MCP 直连 GE |
|---|---|---|
| 保留现有 ADK Agent 逻辑 | ✅ 完整保留 | ❌ 编排移交 GE 模型 |
| 流式 | 完全掌控 SSE | GE 原生流式 |
| 工程量 | 大 | 中（开发一个 MCP 服务器 + 后台注册） |
| 适合场景 | 必须保留 ADK 智能体且流式为硬需求 | 可接受 GE 直接编排工具的轻量形态 |

> 飞书文档《[Gemini Enterprise 自定义插件与连接器方案](https://webeye.feishu.cn/docx/WH67dRhlTooqymxnNdZcOQrBnYf)》（来源 [25]）另全景覆盖预置连接器、自定义数据连接器、Workflow Builder 等与本问题无直接关系的接入方式，此处不重复；其结论"注册自定义 MCP 服务器零代码"及各项限制已抽官方页面原文核对一致（[22]–[24]）。

---

## 七、引用来源清单

**官方文档（Google Cloud / ADK）**

| 编号 | 标题 | 链接 | 更新时间 |
|---|---|---|---|
| [1] | Agent Platform runtime contract | https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/runtime-contract | 2026-10-22 |
| [2] | Use an agent（含 run_sse 流式调用示例、Playground 支持说明） | https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/use-an-agent | 2026-10-23 |
| [3] | Optimize and scale Agent Runtime performance（冷启动/并发官方压测数据） | https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/runtime/optimize-and-scale | 2026-10-22 |
| [4] | Register and manage an ADK agent（GE 侧性能最佳实践、位置兼容表） | https://docs.cloud.google.com/gemini/enterprise/docs/register-and-manage-an-adk-agent | 2026-10-24 |
| [5] | ADK Runtime Configuration（RunConfig/StreamingMode 语义） | https://adk.dev/runtime/runconfig | 官方文档站 |
| [15] | REST Resource: projects.locations.reasoningEngines（DeploymentSpec 字段权威定义：minInstances/maxInstances/containerConcurrency/resource_limits 默认值与取值范围） | https://docs.cloud.google.com/vertex-ai/docs/reference/rest/v1beta1/projects.locations.reasoningEngines | 官方 REST 参考 |
| [16] | 使用 StreamAssist API 调用特定代理（限制条款：streamAssist 不支持 ADK/A2A agent） | https://docs.cloud.google.com/gemini/enterprise/docs/invoke-agent-streamassist | 2026-10-24 |
| [17] | 合作伙伴模型定价 — 区域、全域和多区域端点（global 端点"某些情况下可能导致延迟较长"的官方原文） | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/partner-models/use-partner-models | 官方文档 |
| [18] | Deployments and endpoints（global 端点定位：可用性与减少 429） | https://cloud.google.com/vertex-ai/generative-ai/docs/models/gemini-supervised-tuning-region-settings | 官方文档 |
| [19] | 思考型 Thinking（thinking 默认启用；Gemini 3 用 thinking_level；thinking_budget 已弃用） | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/maas/capabilities/thinking | 官方文档 |
| [20] | Context caching overview（隐式缓存默认开启、降低重复内容延迟、命中率建议） | https://docs.cloud.google.com/gemini-enterprise-agent-platform/models/context-cache/context-cache-overview | 官方文档 |
| [21] | AI.GENERATE function（thinking_budget=0 降低延迟、thinking_level 低档优先延迟的转述） | https://docs.cloud.google.com/bigquery/docs/reference/standard-sql/bigqueryml-syntax-ai-generate | 官方文档 |

**官方仓库 Issue / 论坛（行为实证）**

| 编号 | 内容 | 链接 | 日期/状态 |
|---|---|---|---|
| [6] | GE 调用 `streaming_agent_run_with_events` 的实测请求日志 | https://github.com/google/adk-go/issues/770 | 2026-04，开放 |
| [7] | 开发者论坛：GE 400 错误证实 GE 固定调用该方法 | https://discuss.google.dev/t/agent-returned-an-error-400-when-registering-to-google-enterprise/369919 | 2026-06 |
| [8] | agents-cli：Console Playground 与 GE (ADK 注册) 均使用 stream_query 契约；空响应 bug 已于 2026-09-01 官方确认修复 | https://github.com/google/agents-cli/issues/80 | 已关闭 |
| [9] | GE 会逐一拼接渲染流式 chunk（含重复显示 bug 及修复 PR #7396） | https://github.com/google/adk-python/issues/7395 | 2026-10，官方确认 |

**GE 自定义 MCP 连接器（方案 E 来源）**

| 编号 | 标题 | 链接 | 更新时间 |
|---|---|---|---|
| [22] | Set up your custom MCP server data store（StreamableHTTP 限制、可信 CA TLS、100 操作上限等官方原文） | https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server | 2026-10-24 更新，2026-10-26 原文核对 |
| [23] | Business 版自定义 MCP 服务器连接教程（Add MCP Server 图文流程） | https://support.google.com/g/answer/17106276 | 2026-10-26 核对有效（Pre-GA） |
| [24] | Dev's guide to MCP apps in Gemini（官方博客，7 步开发部署） | https://cloud.google.com/blog/topics/developers-practitioners/devs-guide-to-mcp-apps-in-gemini | 2026-10-20 发布 |
| [25] | 内部文档：《Gemini Enterprise 自定义插件与连接器方案》（飞书，预置连接器/数据连接器/Workflow Builder 全景） | https://webeye.feishu.cn/docx/WH67dRhlTooqymxnNdZcOQrBnYf | 2026-10-26 核查 |

**本地源码证据（本项目环境实查）**

| 编号 | 位置 |
|---|---|
| [10] | `.venv/lib/python3.14/site-packages/vertexai/agent_engines/templates/adk.py:1143-1217` —— 部署模板未传 `run_config` |
| [11] | PyPI `google-cloud-aiplatform 2.6.0` wheel 内 `vertexai/agent_engines/templates/adk.py:1496` —— 最新版行为相同 |
| [12] | `.venv/lib/python3.14/site-packages/google/adk/agents/run_config.py:52-70, 220` —— ADK 1.24.1 支持 StreamingMode |
| [13] | `nexus_agent/deployment/deploy.py:115-158` —— AdkApp 封装与 `agent_engines.create()` 现有参数（无 min_instances/concurrency/resource_limits） |
| [14] | `pyproject.toml:8` —— `google-adk==1.24.1` 已固定；`:9` `google-cloud-aiplatform>=1.135.0` 未固定 |

---

## 八、核查过程中的修正记录

相对上一轮口头结论，本次逐条核查后修正/收紧了以下表述：

1. ~~"热启动 0.4s"~~ → 官方中文页术语为"**温启动**"（warm start）。
2. ~~"container_concurrency 默认值为 9（出自 optimize 页）"~~ → Optimize and scale 页**未给出默认数值**；默认值 9 及"建议 2×cpu+1"出自官方 **REST 参考文档 DeploymentSpec**（来源 [15]），optimize 页仅描述默认面向同步代码、9 = 每容器并行 agent 进程数。
3. ~~"GE 会渲染流式 chunk"~~ → 收紧为**间接证据**：issue #9 的场景是 A2A 路径而非 `streamQuery` 路径，仅能证明 GE 前端具备该能力，Agent Runtime 路径下的实际渲染效果需实测。
4. ~~"min_instances 最大值 10（出自 optimize-and-scale 英文页）"~~ → 该页当前版本**已无此句**（改为链接 REST 参考）；上限 10 最终由官方 REST 参考确认：`minInstances` "Defaults to 1. Range: [0, 10]"（来源 [15]）。
5. ~~"修复 PR #7396 已跟进修复"~~ → PR #7396 截至核查日（2026-10-26）状态为 **open、尚未合并**；仅 issue 本身获官方成员确认。
6. **新增边界澄清**：GE 的 StreamAssist API 明确不支持 ADK/A2A agent（来源 [16]），ADK agent 只能走 Agent Runtime 流式调用链——进一步收窄了 C1/C2 的适用范围，排除"换用 StreamAssist API"这一无效路径。
7. **"慢"成因专项补核（2026-10-26 第三轮）**：原四个因素全部复核通过，并新增三个此前遗漏的官方延迟因素——D6 思考默认开启（项目未配置 `thinking_level`）、D7 上下文缓存与 Prompt 结构、D8 会话历史全量加载；同时对 D4 补充官方权衡提示：**global 端点的收益是可用性与减少 429/503，某些情况下单次延迟反而更长**，不宜简单理解为"改 global 就变快"。
8. **编排链路项目侧事实精确化**：341 条（308+33）经 JSON 实际解析确认；`subprocess.run` 子进程调用定位于 `cli_client.py:115`；三步法工具定位于 `tools.py:1400/449`、Prompt 引导见 `agent.py:88`。

---

## 九、核查覆盖度说明

- 报告全部 25 项来源均已逐一在线抓取原文核对（其中 [10]–[14] 及 D3/D6/D7 的项目侧事实为本地源码/配置/JSON 实查，无外部链接；[23]–[25] 于 2026-10-26 合入方案 E 时核对，关键限制条款已对照官方页面 [22] 原文逐条确认）。
- 官方文档均核对页面"最后更新时间"（2026-10-22 至 2026-10-24 之间，均为近期有效版本）。
- 唯一无法在线复核的声明：GE 前端在 `streamQuery` 路径下对 partial 事件的最终渲染行为——官方无文档，已明确标注为"需实测"（见 C5）。
