# 远程生产引擎回归测试

对已部署的 Agent Engine 做**真实调用**回归，不是 mock、不是本地模拟。每次部署后跑一轮，确认已验证能力没有被破坏。

## 运行

```bash
# 前置：ADC 有效（无需密钥）
gcloud auth application-default login

# 全量（12 用例，约 8-12 分钟，含工具链慢用例）
.venv/bin/python tests/remote/remote_regression.py

# 快速（6 用例，约 2-3 分钟，部署后冒烟用）
.venv/bin/python tests/remote/remote_regression.py --quick

# 指向其他引擎
ENGINE_NAME=projects/<p>/locations/<l>/reasoningEngines/<id> \
  .venv/bin/python tests/remote/remote_regression.py
```

退出码：全过为 0，任一失败为 1（可直接接 CI）。

## 用例清单

| 用例 | 验证内容 |
|---|---|
| 引擎元信息 | 引擎存活、方法注册表可读 |
| 基础对话 | 模型响应正常、指令遵循 |
| get_access_token | 工具真实执行（非编造） |
| 多轮会话记忆 | 同会话上下文保持 |
| 错误韧性 | 无效 command_id 优雅降级不崩溃 |
| GE SSE 通道 | streaming_agent_run_with_events 真实 token 级流式 |
| Lark 工具链 | discover→spec 三步法（慢，约 100s） |
| GWS 工具链 | Google Workspace discover→spec（慢，约 130s） |
| CLI 真实执行 | execute_lark_cli 命中真实 CLI 二进制 |
| 多工具串联 | 单次对话串联 3 步工具调用 |
| 高危工具确认门控 | delete_lark_document 触发确认而非直接删除 |
| 并发双会话隔离 | 双线程并发，暗号不串扰 |

## 三个必须遵守的调用规范

这三条都是 2026-10-27 真实踩坑后确认的，改代码时勿绕过：

**1. `create_session` 响应包裹在 `output` 层**

```python
sid = re_query("create_session", {"user_id": u})["output"]["id"]
```

取错层（如取 `response` 或根部 `id`）会得到 `None`，后续每轮隐式新建会话，表现为"多轮记忆失败"的假性缺陷。

**2. SSE 通道的 `message` 必须是 Content 结构化字典**

```python
"message": {"role": "user", "parts": [{"text": "..."}]}   # 正确
"message": "..."                                          # 触发 code 498
```

根因在 ADK 部署模板：`types.Content(**request.message)` 要求 mapping。错误信息为 `Content() argument after ** must be a mapping, not str`。

**3. 超时必须 ≥540s**

gemini-3.8-flash 在工具链场景下单次响应可达 2 分钟以上。客户端超时过短会把正常的慢响应误判为失败。

## 与其他测试层的关系

| 层 | 位置 | 覆盖 |
|---|---|---|
| 单元/契约 | `tests/*.py` | 本地代码逻辑，秒级，无需网络 |
| 远程回归 | 本目录 | **真实部署产物**，分钟级，需 ADC |

单元测试全绿不代表远程可用（历史教训：插件回调签名 bug 在本地 `InMemoryRunner(agent=...)` 路径完全测不出，只有远程真实调用或挂 App 插件的本地 E2E 才暴露）。两层都要跑。
