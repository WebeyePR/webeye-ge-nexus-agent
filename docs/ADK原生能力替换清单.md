# ADK 2.13 原生能力替换审计清单（逐文件，无一遗漏）

> 审计基线：google-adk==2.13.0（2026-10-22 发布）｜ 审计日期：2026-10-26
> 原则：能被 ADK 原生能力等价替换的**一律替换**；有明确风险依据的**不采纳并留档**。

## 一、逐文件审计映射

### nexus_agent/tools.py（2896 行 → 全函数盘点）
| 代码位置 | 原实现 | ADK 2.13 原生能力 | 决策 |
|---------|--------|------------------|------|
| `wait_for_feishu_doc_create_task` / `wait_for_feishu_doc_update_task` | 同步 sleep 轮询，用户无感知、模型需反复重试 | **LongRunningFunctionTool**（原生长任务：yield 进度流式推送、框架托管生命周期、声明自动追加"勿重复调用"提示） | ✅ **已替换**（本轮实施） |
| 工具返回 `types.Part`（PDF/Word/图片工具） | — | 2.13 原生多模态提取 `_extract_multimodal_parts` | ✅ 已采用（上一轮实施，monkey-patch 已根除） |
| `show_user_auth_info` | 死代码（未挂载） | — | ✅ **已删除**（本轮） |
| `get_lark_document_content` | 死代码（未挂载，被 `get_lark_document_markdown` 取代） | — | ✅ **已删除**（本轮） |
| 35 个业务工具函数 | 纯函数 + `ToolContext` 注入 | ADK 原生支持（运行时自动包装 FunctionTool） | ✅ 保持（即原生用法） |
| `feishu_mcp_create/update/fetch_doc` + `call_feishu_mcp_tool` | 手写 MCP JSON-RPC（mcp.feishu.cn 网关） | **MCPToolset**（StreamableHTTPConnectionParams + header_provider） | ❌ 不采纳，理由见二.1 |
| `execute_lark_cli*` / `execute_google_workspace_cli*` 总线 | CLI 二进制 + registry | **OpenAPIToolset**（spec 驱动） | ❌ 不采纳，理由见二.2 |
| 突变操作双层安全 | 提示词两阶段确认 + CLI `dry_run`/`allow_mutating` | **FunctionTool(require_confirmation=True)** / `tool_context.request_confirmation`（HITL） | ❌ 不采纳，理由见二.3 |
| `_robust_clean_json` / `_parse_*_args` | LLM JSON 容错解析 | 2.13 已在模型适配层流式解析函数调用参数 | 保持（作为防御层仍有价值，删除无收益） |

### nexus_agent/agent.py
| 代码位置 | 决策 |
|---------|------|
| 单 `LlmAgent` + 36 挂载项（34 函数 + 2 长任务） | ✅ 符合原生形态；`mode` 已是 2.13 真字段（上一轮已修） |
| import 期端点环境变量设定 | ✅ 保留（远程容器同样依赖） |

### nexus_agent/main.py（45 行 CLI REPL）
| 决策 |
|------|
| ✅ **已删除**（本轮）。功能被官方 `adk run` 完全取代（本项目 scripts/run.local.sh run 模式即官方 CLI） |

### nexus_agent/plugins.py（新建）
| 决策 |
|------|
| ✅ **新增**：`ToolAuditPlugin`（继承原生 `google.adk.plugins.BasePlugin`），集中审计 16 个突变类工具的调用开始/结果/异常，接入部署链路（AdkApp 原生 `plugins` 参数，存储于 `_tmpl_attrs`，Agent Engine pickle 兼容） |

### nexus_agent/deployement/
| 代码位置 | 决策 |
|---------|------|
| `deploy.py` 模块级即部署 | ✅ 上一轮已加 `main` 守卫；本轮接入审计插件 |
| `adk_agents-0.1.0-py3-none-any.whl`（旧项目名陈旧 wheel） | ✅ **已删除**（本轮根除） |
| `adk deploy agent_engine` 官方 CLI 部署 | ❌ 不采纳：无法携带 StreamingAdkApp 子类与 plugins 定制，保留受守卫保护的 deploy.py |
| `streaming_app.py` | ✅ 与 aiplatform 2.6.0 模板逐行对齐 + SSE 注入（核心资产，保持） |

### 目录结构
| 位置 | 决策 |
|------|------|
| `nexus_agent/js_bridge/`（空目录） | ✅ **已删除**（本轮根除） |
| 目录层级重排（如 tools.py 拆分） | ❌ 不做：2896 行单文件是唯一大文件，拆分属纯风格收益，反而增加 import 图谱与 pickle 路径风险（Agent Engine 按 `nexus_agent.tools` 路径 pickle），违背"最稳"目标 |

### 基础设施（infrastructure/、*_registry/）
| 位置 | 决策 |
|------|------|
| `lark_api_repository.py`（30+ 函数） | ✅ 保持：手写 HTTP 精确控制 UAT/Bearer/预览流/导出任务，无等价 ADK 原生件 |
| `cli_client.py`（PackagedCLIClient 隔离 home 执行） | ✅ 保持：安全执行器无原生替代 |
| `lark_registry`（308 命令）/ `gws_registry`（33 命令） | ✅ 保持：云端实战验证 |

## 二、不采纳决策与依据（重要，防止后人重复踩坑）

1. **MCPToolset 不替换手写 MCP JSON-RPC**：业务语义（UAT/TAT 头选择、token 门控、markdown 规范化、task_id 轮询、结果归一）必须保留；MCPToolset 只解决传输层，纯替换零用户收益，反引入 MCP 握手/会话生命周期/Agent Engine pickle 兼容三重风险。
2. **OpenAPIToolset 不重写 CLI 总线**：lark/gws CLI 总线 + 本地 registry 已在生产验证；重写需维护 spec、改变全部工具面、GE 提示词联动重写——风险收益完全倒挂。
3. **Tool Confirmation（HITL）不启用**：`require_confirmation`/`request_confirmation` 依赖可恢复会话（`ResumabilityConfig(is_resumable=True)`，属 Runner/App 层能力）；GE 经 `:streamQuery` 单请求链路无恢复通道，启用即挂起。现有"提示词两阶段确认 + CLI dry_run"双层安全在 GE 场景是**唯一正确解**。
4. **Workflow Runtime / Task API / AgentTool / RemoteA2aAgent**：单体高能力 Agent + GE 单入口架构，无多智能体编排需求。
5. **MongoDB Session/Memory、Live 音频（barge-in）、Classifiers、SkillToolset、ModelConsultTool**：无对应场景；部分为实验特性。
6. **OTel GenAI v2（2.13 breaking）**：本项目 Agent Engine 链路 `enable_tracing=False`，不受影响；如需旧 schema 设 `ADK_TELEMETRY_SCHEMA_VERSION_OPT_IN=otel_semconv_1_36`。

## 三、实施与验证记录（2026-10-26 本轮）

- **测试**：新增 `tests/test_adk_native_upgrade.py`（10 项）+ 适配 2 项旧轮询测试（生成器协议），**全量 93 passed**
- **冒烟**：`./scripts/test.local.sh` 全绿（28 partial 流式 + 工具调用 + 最终回复）
- **部署路径**：StreamingAdkApp + ToolAuditPlugin 实例化验证通过，wheel 已重建同步
- **净变化**：删除 2 个死函数 + main.py + 空目录 + 陈旧 wheel；新增 plugins.py 与 2 个长任务工具
- **线上生效**：需重新部署（`bash deploy.sh`）后 GE 侧生效；本地已全验证

## 四、二次防遗漏审计（2026-10-26 23:10 补充）

首轮替换后进行全仓二次扫描（ruff F401/F811/F841 + 残留模式 grep + 依赖树核对），补齐以下项：

| # | 发现 | 处置 | 验证 |
|---|------|------|------|
| 1 | `langchain_google_vertexai==3.2.5`、`langgraph==1.2.17` 为 pyproject 直接依赖，但生产代码/测试**零引用**（langgraph 仅是 ADK 可选 extra） | 从依赖移除，部署包瘦身、冷启动加速 | uv sync 后 langgraph 导入失败（已移除）、93 测试全绿、冒烟 26 partial 全绿、wheel METADATA 已精简 |
| 2 | `cloudpickle` 无直接 import，但是 Agent Engine 序列化链路组件（aiplatform agent-engines extra 依赖） | **保留锁定**（"最稳"原则，本地/云端版本漂移风险大于瘦身收益） | — |
| 3 | 上轮重构遗留未用导入（tests 3 文件 9 处）、`deploy.py` 未用异常变量、`__init__.py` re-export 未声明 | ruff 全量清零；`__all__` 显式导出；`lark_agent` shim 加 noqa 注明意图 | `ruff check --select F401,F811,F841` 仅剩 2 处有意 shim（已标注） |
| 4 | **uv 0.12.17 与 setuptools editable_wheel 存在 EEXIST 缓存冲突 bug**（uv sync / uv run 触发 editable 重建即失败），会导致 deploy.sh 与本地脚本全线卡死 | `run.local.sh`/`deploy.sh` 全部 `uv run` 改为 `--no-sync`；`deploy.sh` 增加 `uv sync --no-editable` 步骤 | 冒烟脚本修复后全绿；`uv build --wheel` 正常；deploy.sh 链路确定性恢复 |
| 5 | `agent.py`（217 行）逐行复查：无手写 generate_content_config / planner / callback 等可原生化点 | 无需改动 | — |
| 6 | `infrastructure/`、`config/` 无手写 BaseSessionService/BaseArtifactService 等重复实现 | 无需改动 | — |
| 7 | 全仓 `__build_response_event` / `__multimodal_parts__` / monkey-patch 模式扫描 | 零残留 | grep 确认 |

**二次审计结论**：首轮清单覆盖完整，无漏审文件；本轮净效果 = 依赖精简 2 项 + 构建链路修复 + 静态检查清零。
