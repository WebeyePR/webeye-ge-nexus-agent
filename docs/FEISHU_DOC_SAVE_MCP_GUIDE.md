# 飞书云文档写入能力说明

## 目标

当前项目已经支持基于飞书官方 MCP 网关创建飞书云文档，并将 AI 生成的 Markdown 内容写入新文档或已有文档。

当前能力重点覆盖：

- 创建新的飞书云文档
- 更新已有飞书云文档
- 将 AI 输出结果写入文档正文
- 支持对 create/update 返回的 `task_id` 进行轮询
- 返回结构化结果给 Agent，包括 `doc_id`、`doc_url`、`message`
- 自动去除与文档标题重复的开头一级标题，避免标题重复

## 能力入口

### 1. 底层 MCP 工具

适合直接调试或需要更细粒度控制时使用：

- `feishu_mcp_create_doc`
- `feishu_mcp_update_doc`
- `feishu_mcp_fetch_doc`

### 2. 业务级保存工具

推荐 Agent 优先使用：

- `save_ai_output_to_feishu_doc`
- `save_ai_output_to_existing_feishu_doc`
- `wait_for_feishu_doc_create_task`
- `wait_for_feishu_doc_update_task`

该工具位于：

- [lark_agent/tools.py](/Users/apple/Project/Git/adk-agents/lark_agent/tools.py)

它是对 `feishu_mcp_create_doc` 的高层封装，主要做了两件事：

- 自动清洗 AI 输出 Markdown
- 统一返回创建结果，便于 Agent 直接回复用户

## 推荐调用方式

当用户提出以下意图时，应优先调用 `save_ai_output_to_feishu_doc`：

- “把这份总结写到飞书文档”
- “把刚才的内容保存到飞书”
- “导出成飞书文档”
- “帮我生成一份飞书文档”

当用户明确要求更新已有文档时，优先调用：

- `save_ai_output_to_existing_feishu_doc`

## `save_ai_output_to_feishu_doc` 输入

| 字段 | 必填 | 说明 |
|---|---|---|
| `title` | 是 | 飞书文档标题 |
| `markdown_content` | 是 | 要写入文档的 Markdown 正文 |
| `folder_token` | 否 | 创建到指定文件夹 |
| `wiki_node` | 否 | 创建到指定知识库节点 |
| `wiki_space` | 否 | 创建到指定知识空间 |

说明：

- `title` 作为飞书文档标题单独传入
- `markdown_content` 只保留正文，不建议再以 `# 标题` 开头重复写一遍
- 如果 AI 输出正文开头恰好是与 `title` 相同的一级标题，系统会自动去除

## 返回结果

工具成功时，会返回统一结构：

```json
{
  "status": "success",
  "title": "周报",
  "doc_id": "doxcn123456789",
  "doc_url": "https://www.feishu.cn/docx/doxcn123456789",
  "message": "文档创建成功",
  "summary": "Created document '周报' at https://www.feishu.cn/docx/doxcn123456789"
}
```

失败时返回：

```json
{
  "status": "error",
  "message": "具体错误信息"
}
```

更新已有文档成功时，返回示例：

```json
{
  "status": "success",
  "doc_id": "doxcn123456789",
  "mode": "append",
  "message": "文档更新成功",
  "summary": "Updated document 'doxcn123456789' with mode 'append'"
}
```

异步任务轮询结果示例：

```json
{
  "status": "success",
  "completed": true,
  "poll_attempts": 2,
  "doc_id": "doxcn123456789",
  "message": "done",
  "summary": "Create task 'task_123' completed after 2 poll(s)."
}
```

## 示例

### 示例 1：保存普通总结

输入：

```json
{
  "title": "项目周报",
  "markdown_content": "## 本周进展\n\n- 完成 MCP 文档写入\n- 补充 Agent 工具封装\n\n## 下周计划\n\n- 增加更新已有文档能力"
}
```

### 示例 2：AI 输出中含重复标题

输入：

```json
{
  "title": "项目周报",
  "markdown_content": "# 项目周报\n\n## 本周进展\n\n- 完成接口联调"
}
```

处理结果：

- 飞书文档标题使用 `项目周报`
- 正文中的重复 `# 项目周报` 会被自动删除
- 写入正文从 `## 本周进展` 开始

### 示例 3：追加到已有文档

输入：

```json
{
  "doc_id": "doxcn123456789",
  "markdown_content": "## 本轮更新\n\n- 已补充 MCP 写入闭环",
  "mode": "append"
}
```

### 示例 4：替换指定章节

输入：

```json
{
  "doc_id": "doxcn123456789",
  "markdown_content": "## 下周计划\n\n- 完成更新工具封装\n- 增加异步轮询",
  "mode": "replace_range",
  "selection_by_title": "## 下周计划"
}
```

### 示例 5：轮询更新任务

当前一次更新操作如果返回：

```json
{
  "status": "success",
  "task_id": "task_456",
  "message": "文档更新已提交异步处理，请使用 task_id 查询状态"
}
```

则后续应调用轮询工具：

```json
{
  "task_id": "task_456",
  "max_polls": 10,
  "poll_interval_seconds": 1
}
```

## 本地验证方式

### 1. 运行测试

```bash
pytest -q tests/test_feishu_mcp_tools.py
```

当前已覆盖：

- MCP `content` 解包
- JSON 文本解析为结构化结果
- 重复标题清洗
- 业务级保存工具的返回结构
- 业务级更新工具的返回结构
- create/update 异步任务轮询

### 2. 本地启动 Agent

```bash
uv run python -m lark_agent.main
```

或：

```bash
uv run adk web
```

### 3. 推荐测试提示词

```text
请帮我生成一份项目周报，并直接写入飞书文档，标题叫“项目周报”
```

```text
把刚才的总结保存成飞书文档，标题叫“需求评审纪要”
```

```text
把刚才这段内容追加到这个飞书文档里：<doc_id 或 URL>
```

```text
如果写入返回 task_id，就继续轮询，直到完成后再把结果告诉我
```

## 相关实现文件

- Agent 指令与工具注册：[lark_agent/agent.py](/Users/apple/Project/Git/adk-agents/lark_agent/agent.py)
- 工具实现：[lark_agent/tools.py](/Users/apple/Project/Git/adk-agents/lark_agent/tools.py)
- MCP 调用与结果解包：[lark_agent/infrastructure/lark_api_repository.py](/Users/apple/Project/Git/adk-agents/lark_agent/infrastructure/lark_api_repository.py)
- 测试：[tests/test_feishu_mcp_tools.py](/Users/apple/Project/Git/adk-agents/tests/test_feishu_mcp_tools.py)

## 当前边界

当前这条能力只覆盖：

- 新建文档并写入正文
- 更新已有文档正文

还未进一步抽象的能力包括：

- 长文档分段写入封装
- 保存后自动回填到会话状态中的“当前文档”
- 更复杂的知识库/目录组织策略

## 当前建议

现阶段推荐先用这条能力做最小闭环：

- 用户提出生成内容请求
- Agent 生成 Markdown
- 新建场景调用 `save_ai_output_to_feishu_doc`
- 更新场景调用 `save_ai_output_to_existing_feishu_doc`
- 返回飞书文档链接或更新结果

这已经足够支撑“将 AI 输出结果直接落到飞书文档”的第一阶段目标。
