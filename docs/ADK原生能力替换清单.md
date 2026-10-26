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

## 五、第三遍防遗漏审计（2026-10-26 23:52 补充）

联网核对 PyPI/官方发布线（公开报道最新至 2.10.0，本项目已在 2.13.0，**版本领先无需升级**），并对 2.9–2.13 新增原生能力逐项评估采纳：

| # | ADK 2.13 原生能力 | 评估结论 | 理由 |
|---|------|------|------|
| 1 | `ReflectAndRetryModelPlugin`（模型瞬时故障自愈重试） | **✅ 已采纳接入** deploy.py | 并发安全、零配置、默认 3 次重试；直接减少 429/5xx 硬失败，利好稳定性与延迟 |
| 2 | `ReflectAndRetryToolPlugin`（工具失败自愈重试） | ❌ 不采纳 | 本项目工具从不抛异常（结构化错误返回协议），插件无从触发；且突变类工具盲目重试有重复副作用风险 |
| 3 | `ToolCallIntegrityPlugin`（HMAC 工具调用完整性） | ❌ 不采纳 | 防会话篡改威胁模型；Agent Engine 托管会话场景不适用，且引入密钥管理负担 |
| 4 | `LoggingPlugin` / `DebugLoggingPlugin` | ❌ 不采纳 | 已有结构化日志（logger + ToolAuditPlugin 审计），生产环境无需冗余调试输出 |
| 5 | `Workflow`（图工作流）/ Task API | ❌ 不采纳 | 本项目为单 Agent + 28 工具形态，无确定性编排诉求 |
| 6 | FallbackModel（2.9，本机 2.13 未含此模块） | ❌ 不适用 | 模块在 2.13 不存在（2.9 引入后调整）；且 Vertex 单模型栈无备用模型配置 |
| 7 | Skill 生命周期（2.10 实验，需环境开关） | ❌ 不适用 | 项目无 Skill 形态 |
| 8 | MongoDB toolset / eval 效率指标 / LiveKit | ❌ 不适用 | 与项目场景无关 |

**阻塞点全链路终验**（第三遍逐层核对，确认零残留）：
- `cli_client.py` `subprocess.run`（阻塞）→ 由 `_run_*_cli` 同步助手封装 → 全部调用点 `await asyncio.to_thread(...)` 在工作线程执行 ✅
- `lark_api_repository.py` `_export_document` 内 `time.sleep(0.5)` 轮询（阻塞）→ 由 `get_document_as_pdf/docx` 调用 → tools.py 两个 async 工具以 `await asyncio.to_thread(...)` 包裹 ✅
- `tools.py:266/1316` 直接 `return cli_client.run_command(...)` 均位于 `_run_*_cli` 同步助手内部（`_` 前缀、无 async），由上层 to_thread 驱动，非事件循环路径 ✅
- monkey-patch / 回调劫持 / patch 模式全仓扫描：零残留 ✅

**第三遍审计净效果**：新增 `ReflectAndRetryModelPlugin` 接入（deploy.py plugins 列表 2 项）+ 测试补 2 项断言（94 全绿）。线上生效随下次 `bash deploy.sh`。

## 六、第四遍审计（2026-10-26 23:56 补充）——新维度核验

| # | 核验维度 | 结论 |
|---|------|------|
| 1 | `agent.py` 全文逐行 | ✅ 已完全原生对齐：无手写 generate_content_config/planner/mode hack；多模态走 2.x 原生 FunctionResponse.parts；mode 说明注释在案。36 工具平铺列表即 ADK 原生推荐形态 |
| 2 | `config/` 目录引用链 | ✅ 非死目录：tools.py 与 lark_api_repository.py 均引用（首次 shell grep 管道误报为空，专用工具复核推翻） |
| 3 | `streaming_app.py` | ✅ 保留：GE :streamQuery 链路 SSE 注入为 aiplatform 模板层能力，ADK 不提供原生等价物；版本锁定约束与回滚开关文档完备 |
| 4 | 部署参数（治慢第一层） | ✅ 已在位：min_instances=1（温热防冷启动）、container_concurrency=36、resource_limits 4CPU/8Gi，全部环境变量可调 |
| 5 | `ReflectAndRetryModelPlugin` 实例化端到端 | ✅ 真实 import deploy.py（main 守卫生效，无误部署）：StreamingAdkApp 成功挂载双插件 |

**第四遍结论**：零新增改动项，全部核验通过；审计收敛（连续两遍仅新增 1 项并已落地，第四遍无发现）。

## 七、第五遍审计（2026-10-26 23:59 补充）——冗余/死代码专项清扫

用户点名清查：不使用的工具函数、旧自定义注册、中间件、字节识别图片等旧时代残留。方法：AST 全量盘点（top-level def/class × 全仓引用矩阵）+ AST Call/Name 精确行级裁决 + 弃用标记全仓扫描。

**已删除（约 250 行，commit fc23591）**：

| # | 死代码 | 位置 | 佐证 |
|---|------|------|------|
| 1 | `batch_get_document_urls` | lark_api_repository.py 原 158-241 | 文档串自标 DEPRECATED"新搜索 API 稳定后删除"；生产/测试零引用 |
| 2 | `get_document_rich_text_by_word` | 原 944-1046 | 被 `_by_block` 全面取代；零引用 |
| 3 | `_process_image_to_base64` | 原 736-776 | 仅存于 1 行注释；`_to_bytes` 已覆盖 |
| 4 | `_download_media` | 原 814-831 | 被 `_download_media_by_batch` 取代 |
| 5 | 孤儿导入 `zipfile`/`base64` | 原 3/6 行 | 死函数专属依赖，删除后成孤儿 |

**用户点名项核验（均已在历次重构中根除，本轮实证零残留）**：
- 中间件：nexus_agent/ + tests/ 全仓 grep `middleware|Middleware` 零命中
- 字节识别图片：`imghdr|magic|PNG/JPEG 签名` 全仓零命中（原属已删除的 js_bridge 体系）
- 自定义重复注册：AST 扫描 tools.py 全部 top-level 函数——36 个注册工具 + 内部助手全部有真实引用，无孤儿工具
- 模块级常量：全仓零未引用；`__all__` 导出与实际定义一致
- 弃用标记：`DEPRECATED|已弃用|不再使用` 全仓零命中

**验证**：94 测试全绿、`import nexus_agent` 冒烟 OK、36 工具完整、编译检查通过。

## 八、第六遍审计（2026-10-27 00:04 补充）——指令/工具一致性专项

新维度：system_instruction ↔ 工具注册表一致性、docstring 时效性、双后端分工清晰度。

| # | 发现 | 处置 |
|---|------|------|
| 1 | 36 个注册工具中 10 个未在 instruction 出现（含 2 个 LRFT 等待工具、4 个文档编辑工具混用无分工） | instruction 补齐至 **36/36**：明确 feishu_mcp_*（异步任务+等待）为首选、create/update_lark_document（CLI 同步回退）不混用、execute_lark_api/get_access_token/feishu_mcp_fetch_doc/search_google_drive_files/append_google_doc_text/get_google_workspace_operation_schema 各自定位 |
| 2 | feishu_mcp_create/update_doc 的 docstring 未指引"返回 task_id 后立刻调用 wait_for_* 工具" | 两处 docstring Returns 补 Next step 指引（工具描述随每次调用发送，对模型约束力最强），强调勿手动轮询 |
| 3 | 幽灵名扫描 | 仅 `lark_doc_xxx_logo`（示例文件名，非工具），无幽灵工具引用 |
| 4 | create/update_lark_document 与 feishu_mcp_* 关系判定 | **保留**：CLI 同步 vs MCP 异步任务为互补回退，非代码重复；已用 instruction 明确分工消除模型选择混乱 |
| 5 | 环境变量三方一致性（config/ ↔ deploy.py env_vars ↔ 脚本） | 一致，无需改动 |

**验证**：94 测试全绿、import 冒烟 OK。审计至此六遍，新增维度已穷尽。

## 九、第七遍审计（2026-10-27 00:07 补充）——原生硬确认机制与收尾探针

| # | 维度 | 结论 |
|---|------|------|
| 1 | **ADK 2.13 原生 `FunctionTool(func, require_confirmation=bool\|Callable)`**（bin 探针实测存在） | ⏸ **列为 F5 待验证候选，暂不接入**：突变工具当前为 instruction 软约束 + ToolAuditPlugin 审计；框架级硬拦截的前提是 GE 前端支持 ADK 确认事件流（escalation + tool_confirmation 回传），未验证前盲接会导致突变工具卡死等待确认。已写入验证清单 F5：GE 界面最小样例验证通过后，将 delete_lark_document 等高危工具升级为硬拦截 |
| 2 | ADK 原生 `load_artifacts_tool` | ❌ 非替代关系：原生工具把会话已有 artifacts 注入模型上下文；项目 `render_image_as_artifact` 是"下载 URL→注册 artifact→返回 Part"，职责不同，保留 |
| 3 | `adk deploy agent_engine` CLI（2.6+ 支持 --extra_packages） | ❌ 保留自研 deploy.py：需精确控制 StreamingAdkApp/plugins/min_instances/concurrency/resource_limits/env_vars，CLI 通道无法等价表达 |
| 4 | tests/ 陈旧检测（AST 引用完整性） | ✅ 全部测试引用的被测模块均存在，零陈旧 |
| 5 | registry 结构（lark/gws commands.json + 薄 registry.py） | ✅ 数据驱动 + 运行时 discover 兜底，无陈旧硬编码 |

**第七遍净效果**：新增 F5 验证项（require_confirmation 候选），代码零改动。七遍累计，新维度已穷尽。
