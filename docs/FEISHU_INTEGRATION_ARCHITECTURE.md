# 基于 ADK 的飞书集成方案整理

## 背景

当前项目已经确定采用 Google ADK 开发，并部署到 Vertex AI Agent Engine / Gemini Enterprise（GE）。

当前仓库已经具备以下基础：

- ADK Agent 主体与部署链路已经存在
- GE OAuth 接入模式已经跑通，运行时可通过 `tool_context.state[f"{LARK_AUTH_ID}"]` 获取用户态 token
- 已实现飞书文档相关能力，包括搜索、读取、导出 PDF/Docx、多模态解析
- 仓库内同时包含飞书官方 CLI 与插件相关代码，可作为后续扩展飞书生态能力的基础

本次讨论的核心问题不是“是否接入飞书”，而是：

- 在 `ADK + GE` 的前提下，飞书能力层应优先采用什么方式接入
- `CLI`、`MCP`、`OpenAPI` 三种方式应如何分工
- 当前项目后续从“文档 Agent”扩展到“飞书生态 Agent”时，什么方案最稳妥

## 当前项目现状

从仓库实现看，当前项目本质上是一个“飞书文档 Agent”，而不是完整的“飞书生态 Agent”。

现有主要能力：

- 文档搜索：搜索飞书文档、知识库文档
- 文档读取：读取 Markdown、富文本、图片、PDF、Docx
- 文档写入：创建、更新、删除文档
- GE 集成：通过 Gemini Enterprise 托管 OAuth，工具侧读取用户态 token

相关代码位置：

- Agent 主体：[lark_agent/agent.py](/Users/apple/Project/Git/adk-agents/lark_agent/agent.py)
- 工具定义：[lark_agent/tools.py](/Users/apple/Project/Git/adk-agents/lark_agent/tools.py)
- 飞书 API / 文档处理：[lark_agent/infrastructure/lark_api_repository.py](/Users/apple/Project/Git/adk-agents/lark_agent/infrastructure/lark_api_repository.py)
- CLI 封装：[lark_agent/infrastructure/cli_client.py](/Users/apple/Project/Git/adk-agents/lark_agent/infrastructure/cli_client.py)
- 部署脚本：[lark_agent/deployment/deploy.py](/Users/apple/Project/Git/adk-agents/lark_agent/deployment/deploy.py)
- GE 注册说明：[docs/GEMINI_ENTERPRISE_REGISTRATION_GUIDE.md](/Users/apple/Project/Git/adk-agents/docs/GEMINI_ENTERPRISE_REGISTRATION_GUIDE.md)

需要注意的是，当前仓库并不是单一路线：

- 文档内容读取有直接走飞书 REST API 的实现
- 文档操作有通过 CLI 封装的实现
- 文档高保真读写也接入了飞书 MCP 网关能力
- `lib/openclaw-lark` 中同时注册了 OpenAPI 工具和 MCP 文档工具

结论是：**当前仓库已经天然是混合架构，而不是纯 CLI 或纯 MCP 架构。**

## 方案选型结论

建议采用：

**`ADK + GE` 作为主控编排层，飞书接入采用“CLI/官方能力封装为主，MCP 为辅，必要时直连 OpenAPI”的混合方案。**

不建议采用以下两种极端做法：

- 全量依赖 MCP 作为主能力层
- 全量手写 OpenAPI 适配整个飞书生态

原因如下：

- `ADK + GE` 已经确定，主问题不是协议统一，而是用户态授权、工具治理、错误控制和审计
- 飞书生态覆盖面很广，全部自己封装 OpenAPI 成本过高
- MCP 很适合做标准化工具暴露，但不适合作为当前项目唯一的主架构
- CLI/官方能力封装更适合快速扩展 IM、Calendar、Task、Drive、Wiki、Base、Sheets 等域能力

## 三种接入方式对比

| 方案 | 适配 ADK / GE | 用户态授权适配 | 飞书生态覆盖 | 文档保真 | 开发成本 | 运维复杂度 | 结论 |
|---|---|---|---|---|---|---|---|
| `ADK + CLI` | 高 | 强 | 高 | 高 | 中 | 低到中 | 推荐 |
| `ADK + MCP` | 中 | 中 | 中到高 | 高 | 中 | 中到高 | 不建议作为主路径 |
| `ADK + OpenAPI` | 高 | 强 | 中 | 中 | 高 | 中 | 适合少量特例 |
| `ADK + 混合架构` | 高 | 强 | 高 | 高 | 中 | 中 | 最推荐 |

### 1. `ADK + CLI`

适合场景：

- 快速扩展飞书生态能力
- 需要覆盖多个业务域
- 希望减少直接对接大量 OpenAPI 的工作量

优点：

- 覆盖范围广，适合 IM、Calendar、Task、Drive、Wiki、Base、Sheets、Slides 等域
- 参数与输出已做一定标准化，适合二次封装为 ADK tools
- 扩展新能力的成本相对较低
- 与当前仓库已有代码基础契合度高

缺点：

- 不能直接把 CLI 暴露给模型，仍需要一层稳定的 Adapter
- 需要处理二进制依赖、命令执行、返回值标准化

结论：

**适合作为飞书生态能力的主执行层。**

### 2. `ADK + MCP`

适合场景：

- 希望以标准工具协议暴露能力
- 文档高保真读取和写入
- 未来可能复用到多个 Agent 宿主或多个运行环境

优点：

- 协议标准化，工具边界清晰
- 在文档类能力上通常更容易保持高保真
- 对跨宿主复用更友好

缺点：

- 对当前固定宿主 `ADK + GE` 的直接收益有限
- 会引入额外的协议层和运行层复杂度
- 对 GE 用户态授权、会话态 token、审计治理并没有天然优势
- 不适合作为飞书全生态操作的唯一接入方式

结论：

**适合做局部能力层，尤其是文档高保真场景；不建议作为主集成架构。**

### 3. `ADK + OpenAPI`

适合场景：

- 只有少量核心接口需要接入
- 对返回结构、错误码、重试、超时有非常高的可控性要求
- 某些 CLI 或 MCP 能力不足，需要精确补位

优点：

- 可控性最高
- 便于实现统一的错误模型、重试和幂等控制
- 更利于性能优化和精细化治理

缺点：

- 开发成本高
- 飞书生态域很多，全部手写适配维护负担大
- 后续权限、分页、接口升级处理工作量大

结论：

**适合作为局部补充，不适合作为全量主方案。**

## 为什么不推荐“全量 MCP”

这里的结论不是“MCP 不好”，而是“不适合作为当前项目唯一的主能力层”。

原因主要有四点：

- 当前宿主已经固定为 `ADK + GE`，真正的控制面在 ADK tool schema、session state、OAuth state
- 当前核心难点是用户态身份、权限治理、幂等、错误处理、审计，而不是工具协议本身
- 如果再把核心能力都包进 MCP，会引入额外一层运行和排障复杂度
- 飞书生态操作是多业务域问题，主价值来自域能力封装，而不是协议层统一

因此，MCP 更适合作为：

- 文档高保真能力来源
- 标准化工具接口的补充能力
- 未来跨宿主复用时的能力出口

而不适合作为：

- 当前项目唯一的主执行架构

## 为什么推荐“CLI 为主，MCP 为辅”

这是当前阶段最务实的方案。

### CLI 更适合作为主执行层的原因

- 覆盖飞书生态更广
- 更适合快速扩展到 IM、Calendar、Task、Drive、Wiki、Base、Sheets 等域
- 已有官方维护能力，减少重复造轮子
- 当前仓库中已经存在 CLI 调用封装，可直接承接后续扩展

### MCP 更适合作为辅助能力层的原因

- 在文档类场景中保真度更高
- 更适合高保真 Markdown / 富文本读写
- 更适合少量标准化工具能力复用

### OpenAPI 更适合作为补充层的原因

- 当 CLI 或 MCP 在某个域不够稳定或不够细时，可以用 OpenAPI 补足
- 当需要细粒度控制错误语义、重试和返回结构时，更容易落地

## 推荐的目标架构

建议采用三层架构：

### 1. 编排层：`ADK + GE`

负责：

- 对话编排
- tool calling
- 用户会话管理
- OAuth token state 管理
- GE 内的用户身份接入

### 2. 适配层：Python Adapter Layer

负责：

- 定义 ADK tools
- 参数校验与规范化
- 统一错误模型
- 幂等与权限控制
- 审计日志
- 域能力路由到 CLI / MCP / OpenAPI

### 3. 能力层：Feishu Capability Layer

推荐分工：

- 主路径：CLI / 官方能力封装
- 文档高保真：MCP
- 少量特例：直连 OpenAPI

## 能力分工建议

### 优先走 CLI / 官方能力封装

推荐优先接入以下域：

- `im`
- `calendar`
- `task`
- `drive`
- `wiki`
- `base`
- `sheets`
- `slides`

这些域的特点是：

- API 面广
- 参数繁多
- 业务闭环多
- 自己手写 OpenAPI 适配成本高

### 优先保留 MCP

推荐保留在以下场景：

- 飞书文档高保真读取
- Markdown / 富文本保真更新
- 文档结构敏感场景
- 未来需要以标准工具协议对外复用时

### 适合直连 OpenAPI

推荐仅在以下情况使用：

- 某个能力只涉及 1 到 2 个简单接口
- 某个核心链路对错误语义和返回结构要求很高
- CLI / MCP 无法满足稳定性或功能细节要求

## 分阶段实施建议

### 第一阶段：从文档 Agent 扩展到常用协作 Agent

建议目标域：

- `docs/wiki/drive`
- `im`
- `calendar`
- `task`

原因：

- 最容易形成真实闭环
- 用户价值直接
- 适合 GE 用户态身份访问
- CLI 侧能力成熟，扩展成本低

### 第二阶段：扩展知识与协同能力

建议目标域：

- `base`
- `sheets`
- `slides`
- `contact`

原因：

- 能覆盖更多业务流程
- 适合企业内部知识与协作场景
- 需要更细的工具设计和权限边界

### 第三阶段：扩展复杂业务域

建议目标域：

- `approval`
- `mail`
- `okr`
- `attendance`

原因：

- 权限敏感度更高
- 风险更大
- 更适合在前两阶段稳定后逐步引入

## 推荐的一阶段工具清单

建议优先设计为粗粒度业务工具，而不是直接暴露过细的平台原子接口。

示例：

- `search_documents`
- `read_document`
- `create_document`
- `update_document`
- `send_message`
- `reply_message`
- `search_messages`
- `list_events`
- `create_event`
- `list_tasks`
- `update_task_status`
- `search_drive_files`

原则：

- 一个工具完成一个明确业务动作
- 避免让模型自由拼接原始 CLI 参数
- 避免直接暴露过多底层 API 概念
- 工具输入输出应保持结构稳定、错误语义明确

## 最终建议

最终建议如下：

- 保持 `ADK + GE` 作为唯一主控编排层
- 采用混合架构，而不是单一路线
- 飞书生态能力默认优先复用 CLI / 官方能力封装
- 文档高保真场景继续保留 MCP
- 对少量关键场景允许直连 OpenAPI 补位

可归纳为一句话：

**推荐方案：`ADK 主控，CLI 为主，MCP 为辅，OpenAPI 补位`。**

这是当前项目从“飞书文档 Agent”升级到“飞书生态 Agent”时，兼顾开发效率、可维护性、GE 适配性和后续扩展性的最稳妥方案。
