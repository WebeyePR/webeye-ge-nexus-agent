# Gemini Enterprise 自定义插件与连接器调研报告（已核实）

> 核查时间：2026-10-26
> 核查方式：逐链接 WebFetch 实际访问验证，全部 5 个链接确认有效且内容与结论一致
> 资料来源：Google Cloud 官方文档 / 官方博客 / 官方 Codelab / Google 帮助中心

## 一、核心结论

Gemini Enterprise **支持自定义插件和连接器**，官方提供两条主要自定义集成路径：

1. **自定义 MCP 服务器**（对应"自定义插件"）：将符合 Model Context Protocol 规范的工具系统直接接入，让 Agent 调用企业私有工具与业务逻辑。
2. **自定义数据连接器**（对应"自定义数据源连接"）：接入标准连接器库之外的外部数据源，通过 Discovery Engine API 实现提取 → 转换 → 同步的数据流水线。

## 二、路径一：自定义 MCP 服务器

### 能力说明
- 将公共工具及符合 MCP 规范的第三方系统、内部旧系统直接集成到 Gemini Enterprise。
- MCP 服务器暴露的"工具（Tools）"在 Gemini Enterprise 中称为"操作（Actions）"，导入后默认禁用，需手动启用。
- 所有操作调用默认需用户确认；只读工具可通过 `readOnlyHint: True` 注解跳过确认。

### 关键限制（官方文档明确列出）
- 传输协议：仅支持 **StreamableHTTP**，不支持旧版 SSE。
- TLS 证书：公共和私有 MCP 服务器都必须使用**公开可信 CA 签名的证书**，不支持自签名证书。
- 操作数量：单个数据存储区启用的操作一次**最多 100 个**。
- 不支持连接到现有数据存储区，必须新建并注册。
- VPC Service Controls：出站流量通过组织政策（`allowedEgressFqdns`、`allowedDataSources`）控制，需显式放行 `custom_mcp` 数据源。
- 默认受组织政策约束 `discoveryengine.managed.disableCustomMcpServerConnector`，需先覆盖该策略。

### 版本适用范围
- 完整文档适用：**Standard 版、Plus 版、随用随付版、一线员工版**。
- **Business 版**：帮助中心另有"Set up your custom MCP server connection"子页面，证实 Business 版同样支持添加自定义 MCP 服务器（经管理后台操作，无需走 Cloud Console 数据存储区流程）。

### 自建部署要点（7 步流程，源自官方开发者指南）
1. 启用所需 API（Cloud Run、Discovery Engine、Agent Platform、Organization Policy）。
2. 部署 MCP 服务器容器到 Cloud Run（暴露 `POST /mcp` 端点），参数建议 `--no-allow-unauthenticated --no-cpu-throttling --min-instances=1`。
3. 向 Discovery Engine 服务代理授予 `roles/run.invoker`（第一层认证：Cloud Run IAM）。
4. 创建 OAuth 2.0 Web 客户端，重定向 URI 为 `https://vertexaisearch.cloud.google.com/oauth-redirect`（第二层认证：应用内 OAuth）。
5. 覆盖组织政策约束后，在 Gemini Enterprise → Data stores → Create data store → **Custom MCP Server** 注册（填 MCP URL、Authorization URL、Token URL、Client ID/Secret、Scopes，建议勾选 PKCE）。
6. 在数据存储区 Actions 页面点击 "Reload custom actions" 导入工具并启用操作。
7. 将数据存储区连接到应用（Apps → Connected data stores；注意：MCP 数据存储区走"Add data store"，不要用"Add Agent"，后者用于外部子 Agent）。

## 三、路径二：自定义数据连接器

### 能力说明
- 用于集成标准连接器库之外的外部数据源，使组织独特数据可被自然语言搜索（由 Gemini 与 Google 搜索智能驱动）。
- 连接器直接与 **Discovery Engine API** 交互，将源数据转换为标准化 JSON 文档格式（内容、元数据、ACL）并写入数据存储区。

### 工作流程
- **提取（Fetch）**：通过 API、数据库或文件格式从外部系统拉取文档、元数据和权限。
- **转换（Transform）**：转换为 Discovery Engine 文档格式，分配全局唯一 ID；访问控制可选纯 ACL（AclInfo，适合邮箱身份）或身份映射（Identity Mapping，适合外部身份系统）。
- **同步（Sync）**：上传至数据存储区并通过定时作业保持更新。

### 同步模式
- 增量模式（Incremental upsert）：适合流式、实时更新数据。
- 全量模式（基于 GCS 暂存的全面同步，官方推荐）：支持全量替换与增量插入/更新/删除。
- 支持混合架构：快变数据走增量 upsert，定期全量更新走全面同步。

### 版本适用范围
- 适用 Standard、Plus、随用随付、一线员工版；不适用 Business 版。

## 四、版本差异与预置连接器

| 版本 | 自定义能力 | 预置连接器 |
|------|-----------|-----------|
| Business 版（business.gemini.google） | 管理后台 Settings & help → Manage team → Connectors 启用；支持添加自定义 MCP 服务器 | 官方帮助页列出：Google 连接器 5 个（Calendar、Chat、Drive、Gmail、Sites）+ 第三方约 50 个（Salesforce、Jira、Confluence、Slack、Notion、GitHub、Figma、Microsoft 全家桶等） |
| Standard / Plus / 随用随付版 | 完整支持自定义 MCP 服务器数据存储区 + 自定义连接器（Cloud Console 流程） | 目录更广，第三方资料（Zapier 博客，2026-10）称全目录超 100 个，含 Zapier 桥接 9000+ 应用 |

注意事项：
- "100+ 连接器"说法源自第三方 Zapier 博客；Google 官方 Business 版帮助页未给出总数，严谨表述以上表为准。
- Business 版文件上传/下载上限一般为 50MB（Outlook 为 25MB）。
- HubSpot 使用 Google 托管 OAuth 应用；Outlook 需有效 Microsoft 365 许可证；Monday 需先安装其市场中的 Google Integration；部分连接器仅支持只读操作。

## 五、官方教程链接（全部已于 2026-10-26 实测有效）

| # | 资源 | 链接 | 状态 |
|---|------|------|------|
| 1 | 设置自定义 MCP 服务器数据存储区（官方文档） | https://docs.cloud.google.com/gemini/enterprise/docs/connectors/custom-mcp-server/set-up-custom-mcp-server | ✅ 有效，2026-10-24 更新 |
| 2 | MCP Apps 开发者指南（官方博客，含 7 步部署） | https://cloud.google.com/blog/topics/developers-practitioners/devs-guide-to-mcp-apps-in-gemini | ✅ 有效，2026-10-20 发布 |
| 3 | Google Workspace MCP Codelab（官方实操教程） | https://codelabs.developers.google.com/google-workspace-ge | ✅ 有效 |
| 4 | 自定义连接器概览（官方文档） | https://docs.cloud.google.com/gemini/enterprise/docs/custom-connector | ✅ 有效，2026-10-24 更新 |
| 5 | Business 版连接器帮助（Google 帮助中心） | https://support.google.com/g/answer/16550932 | ✅ 有效，2026-10-15 更新 |

补充说明：
- 链接 1、4 也可通过 `cloud.google.com/gemini/enterprise/docs/...` 访问（会 302 重定向到 docs.cloud.google.com），中文版可加 `?hl=zh-cn` 或访问 docs.cloud.google.cn 镜像。
- 链接 3 前提条件：加入 Google Workspace Developer Preview Program、拥有 Organization Policy Administrator 角色、Gemini Enterprise Standard/Plus 版（可 30 天试用）。

## 六、快速决策建议

- 只想快速接入第三方 SaaS（Salesforce、Jira、Slack 等）→ Business 版管理后台直接开启预置连接器，无需开发。
- 需要接入内部旧系统或自研工具 → 自定义 MCP 服务器（推荐用 FastMCP + Cloud Run，可参考官方博客与 Codelab）。
- 需要让企业私有文档可被搜索 → 自定义数据连接器（Discovery Engine API 流水线）。
- 目标应用不在目录且不想自建 → 用预置 Zapier 连接器桥接 9000+ 应用。
