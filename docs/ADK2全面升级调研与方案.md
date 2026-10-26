# ADK 2.x 全面升级调研与方案

> 调研日期：2026-10-26。核心结论均经**三级来源核实**：官方文档原文抓取（adk.dev / PyPI）、ADK 2.13.0 与 aiplatform 2.6.0 官方 wheel 逐文件解包实证、本项目源码逐行对照。

---

## 一、版本矩阵与依赖冲突链（wheel 实证）

| 包 | 当前锁定 | 最新版 | 关键约束（实测 METADATA） |
|---|---|---|---|
| google-adk | 1.24.1 | **2.13.0**（2026-10-23） | Python ≥3.11；`google-genai>=2.30,<3` |
| google-cloud-aiplatform | 1.136.0 | **2.6.0**（2026-10-20） | `google-genai<3,>=1.66`；adk extra 允许 `google-adk<3.0,>=1.27` |
| google-genai | 1.62.0 | — | **冲突核心**（见下） |
| cloudpickle | 3.1.2 | — | 随 aiplatform 2.x 联动检查 |

**决定性冲突**：aiplatform 1.136.0 要求 `google-genai<2.0,>=1.59`，ADK 2.13.0 要求 `>=2.30,<3` —— 两者**互斥**。因此：

> **全面升级 = ADK 2.13.0 + aiplatform ≥2.2 同步升级，无法只升其一。**
> 依据：ADK 2.13.0 官方部署命令（`cli_deploy.py:59`）自身声明 `'google-cloud-aiplatform[adk,agent_engines]>=2.2,<3'`，aiplatform 2.6.0 满足且与 genai 2.x 兼容。

---

## 二、官方破坏性变更清单（来源：[adk.dev/2.0](https://adk.dev/2.0/)，原文已抓取）

官方口径：*"ADK 2.0 is designed to be compatible with agents developed with ADK 1.x releases. However, there are a few breaking changes."* 核心架构变化：**Workflow Runtime**（图执行引擎，Agent/Tool/Function 成为 graph 节点）。

| # | 变更 | 本项目影响 |
|---|---|---|
| ① | Event schema 新增 `node_info`/`output` 字段；自定义 BaseSessionService 固定列表结构需迁移（JSON blob 存储免改） | **低**：项目仅用 InMemorySessionService，无自定义存储。已实证 2.13.0 `Event` 保留原字段（event.py:105-125） |
| ② | `BaseAgent` 改继承 `BaseNode`；`_run_async_impl()`/`generate_content()` 覆盖被**静默忽略**，需迁移到 Before/AfterAgentCallback | **无**：项目未子类化 BaseAgent |
| ③ | 禁止 `session.events.append()` / `enqueue_event`，必须 yield | **无**：项目未使用 |
| ④ | 宽泛 `except Exception:` 会**禁用框架自动重试**；`except BaseException` 会误捕 `NodeInterruptedError` | **中**：`tools.py` Phase VII 防灾大量 try-except 弹性降级——不会崩溃，但会失去 ADK 2.0 自动重试能力；建议逐步收窄关键工具的异常捕获范围 |
| ⑤ | Python ≥3.11 | **满足**：venv 为 3.14.2 |

## 三、本项目 API 使用面逐一核对（2.13.0 wheel 实证）

| 使用点 | 位置 | 2.13.0 状态 | 动作 |
|---|---|---|---|
| `Agent`（LlmAgent）`model/name/description/instruction/tools` | agent.py:109 | ✅ 字段保留（llm_agent.py:301/319/397） | 无需改 |
| `root_agent.mode` 兼容 hack | agent.py:138 | ⚠️ 2.13.0 新增真字段 `mode: LlmAgentMode`（llm_agent.py:410），赋模型名字符串会触发 **pydantic 校验失败** | **必须移除该 hack** |
| `Runner.run_async` | main.py:32 | ✅ 签名保留，新增可选 `yield_user_message`/`abort_signal`（向后兼容） | 无需改 |
| `InMemorySessionService.create_session` | main.py:14 | ✅ async 方法保留 | 无需改 |
| `ToolContext` | tools.py:7 | ✅ 正常导出 | 无需改 |
| `RunConfig`/`StreamingMode` | streaming_app.py:33 | ✅ 路径不变（run_config.py:84，`streaming_mode` 默认 NONE 语义未变） | 无需改 |
| `Event` 构造 + `is_final_response()` | callbacks.py:98、main.py:34 | ✅ 保留（新增 output/node_info） | 无需改 |
| **monkey-patch `__build_response_event`** | callbacks.py:41 | ❌ **函数已移除**（functions.py 缩为 135 行兼容 shim，仅剩注释引用；新实现为单下划线 `_build_response_event`，`tools/_caller.py:317`，签名相同） | **必须重构**（见第四节） |

## 四、多模态能力迁移（本次调研最重要发现）

项目的 `__multimodal_parts__` monkey-patch 是为弥补 1.x 不支持工具返回多媒体。**ADK 2.13.0 已原生支持**：

- `_extract_multimodal_parts`（`tools/_caller.py:223`）自动扫描工具返回值（含嵌套 dict/list）中的 `types.Part`（`inline_data` 字节流或 `file_data` URI），转为 `FunctionResponsePart` 传给模型；
- 原文：*"A tool that produces an image, audio clip or document returns a part holding the raw bytes or a uri instead."*

**迁移方案**：删除 `callbacks.py` 整个 patch 机制；`tools.py` 三处打标点（约 2007-2014、2079-2086、2165-2218 行）由塞 `__multimodal_parts__` 私有键改为直接在返回 dict 中放 `types.Part(inline_data=...)` 对象。净删除代码 > 新增代码。

## 五、部署链路核对（aiplatform 2.6.0 wheel 实证）

1. **`vertexai.agent_engines` 模块保留**（8 文件），`create()` 完整保留 `min_instances/container_concurrency/resource_limits`（_agent_engines.py:393-396）——现有 `deploy.py` 架构兼容。
2. **模板 `streaming_agent_run_with_events` 仍在**（templates/adk.py:1496），但已新增 `labels → RunConfig` 透传与 `state_delta` 支持；**SSE 仍默认不开**（0 处 streaming_mode）→ 现有 `StreamingAdkApp` 需对齐 2.6.0 模板重写（保留 labels/state_delta 逻辑 + 注入 SSE）。
3. 模板 15 处 google.adk 懒导入路径在 2.13.0 **全部存在**（逐一验证）。
4. ADK 2.13.0 官方部署路径 `adk deploy agent_engine` 已重构为 agentplatform 源文件打包（`requirements_file`/`adk_app` 参数弃用，新增 `agent_engine_config_file`/`worker_pool`/GE `trigger_sources`/`session_service_uri` 等）——目标态架构，建议二期迁移。

## 六、升级实施路径

### 方案 A（推荐：渐进升级，保留现有部署架构）

| 步骤 | 内容 | 风险 |
|---|---|---|
| 1 | `pyproject.toml`：`google-adk==2.13.0` + `google-cloud-aiplatform==2.6.0` + `google-genai>=2.30,<3` + cloudpickle 对齐 → `uv lock && uv sync` | 依赖树重解析（langgraph/langchain 无 genai 依赖，无冲突） |
| 2 | 删除 `callbacks.py` 多模态 patch；`tools.py` 三处改返回 `types.Part` | 多模态行为回归需专项测试 |
| 3 | 移除 `agent.py` 的 `root_agent.mode` hack | 低 |
| 4 | `streaming_app.py` 对齐 2.6.0 模板重写（保留 labels/state_delta + 注入 SSE），同步更新测试 | 中：模板重写需逐行 diff |
| 5 | 全量 pytest + `uv build` + 真实部署 + GE 界面实测（流式/多模态/文档读写三线验证） | — |
| 6 | 审查 `tools.py` 防灾 except 范围（变更 ④），关键路径收窄异常类型 | 长期项 |

**回滚**：单 commit 粒度 + `.deploy_env` 指向旧 RE 实例即可切回。

### 方案 B（二期目标态）

迁移到 `adk deploy agent_engine` 官方链路：配置文件化（`agent_engine_config.json`）、官方维护、GE trigger 集成、托管 session/memory/artifact 服务（`vertex_ai_session_service` 等）。建议方案 A 稳定运行后单独立项。

## 七、验证清单（实施时逐项打勾）

> **✅ 已于 2026-10-26 按"方案 A"完成实施，结果如下（第八节）。**

- [x] 全量 pytest 通过（**83 passed 零回归**，含多模态原生机制 4 项 + 流式对齐 5 项新测试）
- [x] `uv build` wheel 成功且包含全部模块（并根除了 stale `build/` 目录导致的旧 `callbacks.py` 残留）
- [x] 真实部署成功（4m1s），REST 实查实例规格生效：minInstances=1 / containerConcurrency=36 / cpu4·mem8Gi
- [x] GE 原样调用（`:streamQuery`）实测：**6 个 partial 增量 + 1 个最终事件**，SSE 流式在 ADK 2.13 下正常（解析层级注意事项已验证有效）
- [x] 工具调用链路实测：`discover_lark_operations` 真实执行并返回正确结果（ADK 2.13 工具循环无回归）
- [ ] GE 界面实测：流式渲染、图片问答、文档写入三线（需人工在 GE 界面确认，自动化无法覆盖）
- [ ] 长会话/多轮会话语义正常（session 获取/回退逻辑）

## 八、实施结果（2026-10-26 完成）

### 8.1 版本落点（全部精确锁定）

| 包 | 升级前 | 升级后 | 说明 |
|---|---|---|---|
| google-adk | 1.24.1 | **2.13.0** | ADK 2.x 大版本 |
| google-cloud-aiplatform | 1.136.0 | **2.6.0** | genai 约束交集的必选组合 |
| google-genai | 1.62.0 | **2.33.0** | adk 2.13 要求 >=2.30,<3 |
| langchain-google-vertexai | 3.2.2 | 3.2.5 | 同步升级 |
| langgraph | 1.0.7 | 1.2.17 | 满足 adk 2.13 的 >=1.0.10 |
| cloudpickle | 3.1.2 | 3.1.2（不变） | 官方最佳实践锁定项 |

### 8.2 代码迁移结果

| 迁移项 | 结果 |
|---|---|
| `callbacks.py` 多模态 monkey-patch | **整体删除**（目标函数在 2.13 已被移除；框架原生 `_extract_multimodal_parts` 接管） |
| `tools.py` 三处工具 | 改返回原生 `types.Part(inline_data=...)`，JSON response 保持可序列化 |
| `agent.py` mode hack | 移除（2.13 真 pydantic 字段 `mode: LlmAgentMode`，实测 root_agent 正常加载） |
| `streaming_app.py` | 对齐 2.6.0 模板逐行重写：保留 authorizations→temp: state_delta、labels 透传、`_save_artifacts`；SSE 注入点不变；回滚开关保留 |

### 8.3 真实部署验证

- 新引擎 `projects/839062387451/locations/us-central1/reasoningEngines/3781091020427493376`，GE 注册 state=ENABLED，旧引擎已清理
- 云端 requirements 与本地锁定一致（adk==2.13.0 / aiplatform==2.6.0 / genai==2.33.0，部署日志确认）
- 流式：6 partial + 1 final，首个 partial 延迟 6.89s（含模型推理），块间隔 ~100ms
- 工具循环：`discover_lark_operations` 服务端真实执行，返回正确

### 8.4 Git 提交

- `bfa1d8a` build(deps): 全面升级至 ADK 2.x 并精确锁定版本
- `7975ff0` refactor(multimodal): 迁移至 ADK 2.x 原生多模态，删除 monkey-patch
- `2bb0c68` feat(streaming): StreamingAdkApp 对齐 aiplatform 2.6.0 模板重写
- `（本次提交）` docs: 实施结果回填

### 8.5 回滚

单 commit 粒度回滚代码 + `.deploy_env` 指向旧 RE 即可切回；或运行期设置 `AGENT_ENABLE_SSE_STREAMING=0` 仅关闭流式。

## 引用来源

| # | 来源 | 说明 |
|---|---|---|
| 1 | https://adk.dev/2.0/ | 官方 ADK 2.0 介绍与破坏性变更（原文抓取） |
| 2 | https://pypi.org/project/google-adk/ （JSON API） | 2.13.0 元数据：Python≥3.11、发布日期 |
| 3 | `google_adk-2.13.0` wheel 解包 | 全部 API 路径/签名/依赖约束实证 |
| 4 | `google_cloud_aiplatform-2.6.0` wheel 解包 | genai 约束、agent_engines 保留、模板 SSE 行为实证 |
| 5 | 本地 `aiplatform-1.136.0` METADATA | genai<2.0 冲突源实证 |
| 6 | `google/adk/cli/cli_deploy.py`（2.13.0） | aiplatform>=2.2 官方部署要求、to_agent_engine 重构 |
| 7 | docs/非流式与慢问题解决方案.md | 前期调研与流式改造基础 |
