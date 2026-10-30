# 脚本工具说明

本目录包含用于管理和部署 Lark Document Agent 的脚本工具。

## 🚀 本地运行与测试（ADK 官方 CLI）

ADK（google-adk 2.13 自带 CLI）原生支持本地真实运行，无需部署到云端：

### `run.local.sh` — 本地启动
```bash
./scripts/run.local.sh          # 默认 adk web：Dev UI 浏览器调试（http://127.0.0.1:8000/dev-ui/）
./scripts/run.local.sh api      # 纯 API 服务（可 curl 顶层 /run_sse 验证流式）
./scripts/run.local.sh run      # 终端 CLI 交互模式
PORT=8080 ./scripts/run.local.sh web   # 自定义端口
```

- 默认强制 **Vertex AI**（与 Gemini Enterprise 线上行为对齐），走本机 ADC 凭证
  （首次需 `gcloud auth application-default login`）；`LOCAL_USE_VERTEX=0` 可退回 .env 的 AI Studio Key
- `adk web / api_server` 端点无鉴权，仅限本地开发调试，勿暴露公网

### `test.local.sh` — 一键端到端冒烟测试
```bash
./scripts/test.local.sh
```
自动完成：起 api 服务 → 创建会话 → SSE 流式请求 → 触发工具调用（discover_lark_operations）→
校验 partial 流式事件 / 工具调用 / 最终回复三项全绿后退出。
注：ADK 2.13 的运行端点为顶层 `/run` 与 `/run_sse`（无 `/apps/{app}` 前缀）。

---

## 📋 脚本列表

### 1. `check_agent_status.sh`
**功能**：检查 Gemini Enterprise 中的 Agent 状态和授权配置

**使用方法**：
```bash
./scripts/check_agent_status.sh
```

**输出**：
- 所有 Agent 列表
- 所有授权资源列表
- 特定授权资源详情

---

### 2. `list_all_resources.sh`
**功能**：列出所有已部署的 Agent 和授权资源（格式化输出）

**使用方法**：
```bash
./scripts/list_all_resources.sh
```

**输出**：
- 格式化的 Agent 列表（显示名称、状态、Reasoning Engine）
- 格式化的授权资源列表

---

### 3. `batch_delete.sh`
**功能**：批量删除 Agent 和授权资源的交互式工具

**使用方法**：
```bash
./scripts/batch_delete.sh
```

**功能选项**：
1. 删除 Agent
2. 删除授权资源
3. 删除未使用的授权资源
4. 删除所有 Lark Document Agent（保留其他）
5. 退出

---

### 4. `delete_agent.sh`
**功能**：删除指定的 Agent

**使用方法**：
```bash
./scripts/delete_agent.sh <AGENT_NAME>   # 显式传参：直接删，不二次确认
./scripts/delete_agent.sh --yes          # 无参：删 .deploy_env 的当前部署，跳过确认
./scripts/delete_agent.sh                # 无参：删当前部署，需交互确认
./scripts/delete_agent.sh --help         # 帮助
```

**确认策略（2026-10-30 起）**：

| 用法 | 二次确认 | 说明 |
|---|---|---|
| 传 `<AGENT_NAME>` | ❌ 不确认 | 传参即明确意图。`cleanup_deployed.sh` 等批处理链路依赖「传参不读 stdin」 |
| 无参 + `--yes` | ❌ 不确认 | 显式声明要删默认值 |
| 无参（无 `--yes`） | ✅ 确认 | 无参会落到 `.deploy_env` 的 `GE_AGENT_RESOURCE_NAME`，是「删线上当前部署」的隐含路径 |

- **非交互环境（无 tty）无法确认时直接中止，一次 API 都不发**（退出码 1），避免 `./delete_agent.sh`
  在 CI/管道里静默删掉线上 Agent
- 退出码：`0` = 删除成功或已不存在（404 幂等）；`1` = 参数缺失 / 无法确认 / API 删除失败

**示例**：
```bash
./scripts/delete_agent.sh projects/839062387451/locations/global/collections/default_collection/engines/webeye-agentspace-app_1742521319182/assistants/default_assistant/agents/123456789
```

**提示**：Agent NAME 可以从 `check_agent_status.sh` 或 `list_all_resources.sh` 的输出中获取

---

### 5. `delete_authorization.sh`
**功能**：删除指定的授权资源

**使用方法**：
```bash
./scripts/delete_authorization.sh <AUTH_ID>   # 显式传参：直接删，不二次确认
./scripts/delete_authorization.sh --yes       # 无参：删 .env 的 LARK_AUTH_ID，跳过确认
./scripts/delete_authorization.sh             # 无参：删 LARK_AUTH_ID，需交互确认
./scripts/delete_authorization.sh --help      # 帮助
```

确认策略与 `delete_agent.sh` 完全一致（传参不确认 / 无参必确认 / 无 tty 中止 / 失败退出码 1）。
无参时若检测到 `LARK_AUTH_IDS` 分批模式，会额外提示「本命令只删 legacy 单批」。

**示例**：
```bash
./scripts/delete_authorization.sh ge-nexus-feishu-1
```

**被 Agent 占用时**：API 会拒绝物理删除，脚本打印占用方并**安全跳过**（退出码 0，不是失败），
同时给出下一步命令 `bash scripts/delete_agent.sh "<占用方>"`。

**注意**：分批授权（4 批）请逐个传参删除，或直接跑 `cleanup_deployed.sh`（已自动遍历去重）。

---

### 6. `delete_reasoning_engine.sh`
**功能**：删除指定的 Vertex AI Reasoning Engine

**使用方法**：
```bash
./scripts/delete_reasoning_engine.sh <ENGINE_NAME>
```

**特点**：
- 优先尝试使用 `gcloud`
- 如果 `gcloud` 命令不可用，自动回退到直接调用 REST API
- 自动处理 404 等已删除状态
- ⚠️ **无参时会直接删 `.deploy_env` 的 `VERTEX_REASONING_ENGINE_NAME`（当前线上引擎），
  且「没有」二次确认、`?force=true` 不可逆** —— 手工调用前先确认那个值是要删的引擎
  （与 `delete_agent.sh` / `delete_authorization.sh` 的确认门禁不一致，见文末注意事项表）

---

### 7. `register_to_GE.sh`
**功能**：创建授权资源并注册 Agent 到 Gemini Enterprise（自动从环境变量加载配置）

**使用方法**：
```bash
./scripts/register_to_GE.sh
```

**重要**：
1. 注册前需要先完成部署，确保 `.deploy_env` 中已有 `VERTEX_REASONING_ENGINE_NAME`
2. 脚本会自动加载 `.env` 和 `.deploy_env` 中的配置
3. 如果授权资源已存在，会返回 409 错误（可以忽略）
4. 如果 Agent 已存在，建议先使用 `batch_delete.sh` 删除旧 Agent
5. **分批授权（2026-10-30）**：当 `.env` 配了 `LARK_AUTH_IDS` 时，脚本走分批模式——
   先调用 `lark_auth_batches.py --apply` 确保各批资源就绪（幂等，已存在的**不会修改**），
   再绑定全部批次 + GWS。任一批次缺失/不符合预期即中止注册，避免绑出残缺授权。
   未配 `LARK_AUTH_IDS` 时仍走原来的单批逻辑（向后兼容）。

---

### 8. `cleanup_deployed.sh`
**功能**：一键彻底删除所有远程资源（Agent、授权资源、Reasoning Engine）

**使用方法**：
```bash
./scripts/cleanup_deployed.sh
```

**特点**：
- 自动读取 `.env` 和 `.deploy_env`
- 依次调用 `delete_agent.sh`、`delete_authorization.sh` 和 `delete_reasoning_engine.sh`
- 子脚本全部以**显式传参**方式调用 → 不会弹确认、不会读 stdin（`while … <<<` 循环才不会漏项）
- **分批授权（2026-10-30）**：会逐个删除 `LARK_AUTH_IDS` 里的**每一批** + 旧单批
  `LARK_AUTH_ID` + `GOOGLE_WORKSPACE_AUTH_ID`（自动去重），避免卸载后残留 4 批资源
- 可选清空本地部署记录文件（这一步是脚本内**唯一**的交互提示，非交互环境自动跳过）

---

### 9. `lark_auth_batches.py`
**功能**：飞书分批授权资源的检查 / 创建 / 资源名录（幂等，默认只读）

**使用方法**：
```bash
python3 scripts/lark_auth_batches.py --check        # 只检查（默认），有偏差退出码 2
python3 scripts/lark_auth_batches.py --apply        # 创建缺失批次；已存在的绝不修改
python3 scripts/lark_auth_batches.py --print-names  # 输出资源全名，供脚本组装绑定
```

**行为边界（重要）**：
- 批次定义来自 `docs/lark_scope_batches.json`，作用对象是 `.env` 的 `LARK_AUTH_IDS`
- **不提供自动修改既有资源**：GE 的 `GET` 返回 `clientSecret: <redacted>`，回写会把密钥写坏；
  有偏差只报告，人工处理
- 构造授权 URL 时手工拼 `%20`（不能用 `urlencode`，它把空格编成 `+`，飞书按字面量解析会失效）

---

### 10. `check_ge_auth_binding.sh`
**功能**：校验 GE Agent 的 `toolAuthorizations` 是否覆盖配置要求

**使用方法**：
```bash
bash scripts/check_ge_auth_binding.sh
```

**为什么需要**：部署只更新 engine 绑定，**不会**改动 `authorizationConfig`。
「引擎支持 4 批」和「agent 绑了 4 批」是两件独立的事；漏绑会让用户授权了却读不到
token。`deploy.sh` 在更新部署后会调用它（只告警、不自动改授权配置）。

---

### 11. `plan_lark_scope_batches.py`
**功能**：一键生成/校验飞书分批授权清单（`docs/lark_scope_batches.json`）

**使用方法**：
```bash
python3 scripts/plan_lark_scope_batches.py --check        # 默认：拉 app.scopes 比对现有清单
python3 scripts/plan_lark_scope_batches.py --write        # 重算并写回（先备份 .bak）
python3 scripts/plan_lark_scope_batches.py --from-cache app_scopes.json --check   # 离线
```

**特点**：
- 数据源是飞书 `app.scopes`，只取 `token_types` 含 `user` 的权限（tenant-only 用户态拿不到）
- 按场景聚类分 4 批；未归类的域并入最后一批并**打印**，不静默丢弃
- 单批 >45 **报错不落盘**：静默拆批会改名，与线上 `ge-nexus-feishu-N` 资源错位
- 比较按「批名 → scope 集合」，不比顺序（API 返回顺序可能变化）
- 已实测能**完整复现**当前线上 173 项清单（rc=0）

**新增能力时的顺序**：飞书后台开通权限 → `--check` → `--write` →
若批次数/批名变化则同步 `.env` 的 `LARK_AUTH_IDS` → `lark_auth_batches.py --apply`
→ `check_ge_auth_binding.sh`。

---

## 🔧 配置说明

所有脚本都通过 `scripts/load_env.sh` 自动加载以下配置文件：
- `.env`: 包含 `PROJECT_ID`, `GE_APP_ID`, `LARK_CLIENT_ID` 等基础配置
- `.deploy_env`: 包含部署后生成的 `VERTEX_REASONING_ENGINE_NAME` 和 `GE_AGENT_RESOURCE_NAME`

不再需要在脚本中硬编码这些 ID。

### 环境后缀（`ENV_SUFFIX`）约定 —— 2026-10-30 修

`load_env.sh` **只认 `ENV_SUFFIX` 变量**，不再读取 `source` 时的 `$1`。

- 需要切环境：`export ENV_SUFFIX=dev`（或按脚本自己的第 1 个参数传入，脚本内部会转成该变量）
  → 读 `.env-dev` / `.deploy_env-dev`；`deploy.sh`、`cleanup_deployed.sh` 本来就是这样做的，能力不变
- **为什么改**：`source` 时 `$1` 是**调用方脚本**的位置参数。旧实现里
  `delete_authorization.sh ge-nexus-feishu-1` 会把 `ge-nexus-feishu-1` 当环境后缀 →
  去找 `.env-ge-nexus-feishu-1`（不存在）→ `PROJECT_ID` 为空 →
  删除请求 URL 变成 `projects//locations/global/authorizations/...` →
  API 返回 `400 RESOURCE_PROJECT_INVALID`。实测确认，属真缺陷（同类受害者：
  `delete_agent.sh`、`delete_reasoning_engine.sh`、`run.local.sh` 的 `web/api/run`）
- 后缀对应的配置文件不存在时，**加载阶段就在 stderr 告警**，不再静默空配置

---

## 📝 使用流程示例

### 场景 1: 检查当前状态
```bash
./scripts/check_agent_status.sh
```

### 场景 2: 更新部署后重新注册
```bash
# 1. 查看当前 Agent
./scripts/list_all_resources.sh

# 2. 删除旧 Agent（如果需要）
./scripts/delete_agent.sh <OLD_AGENT_NAME>

# 3. 注册新 Agent
./scripts/register_to_GE.sh
```

### 场景 3: 清理所有资源
```bash
# 方式 A：使用交互式批量删除工具
./scripts/batch_delete.sh

# 方式 B：一键清理当前项目已部署的所有远程资源
./scripts/cleanup_deployed.sh
```

---

## ⚠️ 注意事项

1. **授权资源依赖**：删除授权资源前，必须先删除所有使用该授权资源的 Agent
2. **API 延迟**：删除操作后可能需要等待几秒钟，API 才会更新状态
3. **权限要求**：所有脚本都需要 `gcloud auth print-access-token` 有权限访问 Gemini Enterprise API
4. **破坏性脚本的确认策略一览**：

   | 脚本 | 传参 | 无参 | 非交互 |
   |---|---|---|---|
   | `delete_agent.sh` | 直接删 | **确认**（删 `.deploy_env` 当前部署） | 中止，退出码 1 |
   | `delete_authorization.sh` | 直接删 | **确认**（删 `.env` 的 `LARK_AUTH_ID`） | 中止，退出码 1 |
   | `delete_reasoning_engine.sh` | 直接删 | ⚠️ **不确认**（删 `VERTEX_REASONING_ENGINE_NAME` 当前引擎，`?force=true`） | 直接执行 |
   | `cleanup_deployed.sh` | —（无位置参数） | 逐个传参调用子脚本，全程不弹确认 | 可全自动 |
   | `batch_delete.sh` | 交互式菜单 | 菜单内自带确认 | 不适合非交互 |

   `delete_reasoning_engine.sh` 是三个删除脚本里唯一「无参即删当前引擎且不确认」的，
   手工调用前务必先看 `.deploy_env` 里的 `VERTEX_REASONING_ENGINE_NAME` 是否为要删的那一个。

