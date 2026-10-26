# Google Workspace CLI 接入 Backlog 与实施计划

## P0：最小可用接入

状态：已完成代码实现，待真实二进制与 OAuth 联调。

- 抽象通用 packaged CLI 执行基座。
- 新增 `GoogleWorkspaceCLIClient`。
- 新增 `GOOGLE_WORKSPACE_AUTH_ID` 配置。
- 将 `lark_agent/bin/gws` 加入 wheel package-data。
- 实现 Drive 文件搜索工具。
- 实现 Google Docs 纯文本追加工具。
- 实现 Google Sheets 范围读取工具。
- 实现 Google Sheets 行追加工具。
- 实现 Google Calendar 日程查询工具。
- 实现 Google Calendar 日程创建工具。
- 实现 Google Workspace 操作发现工具。
- 实现 Google Workspace schema 查询工具。
- 实现受控通用 `gws` 执行工具。
- 注册到 ADK Agent tools。
- 补单元测试。

验收标准：

- 无 Google token 时返回清晰授权错误。
- 有 token 时工具能构造确定的 `gws` argv。
- 通用 `gws` 执行工具不允许 shell，只接受 JSON argv。
- 通用 `gws` 写操作默认 dry-run，未经确认不得直接执行。
- CLI 执行不使用 shell。
- JSON 输出可被正常解析。

## P1：部署与真实联调

状态：部分完成。

- 从 v0.22.5 release 下载云端平台 `gws` 二进制。已完成。
- 放入 `lark_agent/bin/gws`。已完成。
- 在本地执行 `uv run pytest`。部分完成，项目根全量 pytest 会收集 `debug/lark_test.py` 并因缺少 `lark_oapi` 失败；核心测试已通过。
- 配置 Gemini Enterprise Google Workspace OAuth。
- 增加 OAuth scopes：
  - Drive metadata/read
  - Docs
  - Sheets
  - Calendar
- 在 Agent Engine 部署环境验证 `gws --version`。
- 使用真实 Google 账号验证 Drive search。
- 使用测试文档验证 Docs append。
- 使用测试表格验证 Sheets read / append。
- 使用测试日历验证 Calendar agenda / insert。

验收标准：

- 所有 P0 工具真实调用成功。
- 401/403 能明确定位到 scope 或资源权限。
- 云端不会触发 `gws auth login`。

## P2：能力扩展

状态：未开始。

- Google Docs 创建文档。
- Google Drive 文件导出。
- Google Drive 文件上传。
- Gmail 读取、草稿、发送。
- Google Chat 发送消息。
- Google Tasks 管理。
- Slides 创建和批量更新。

验收标准：

- 每个能力都先提供专用业务工具。
- 写操作默认需要明确目标资源和内容。
- 高风险操作优先创建草稿或 dry-run。

## P3：治理与通用化

状态：未开始。

- 多 CLI registry。
- 每个 CLI 独立 `CliSpec`。
- 通用审计日志。
- 写操作确认策略。
- 通用 OpenAPI/Discovery 白名单工具。
- CLI 二进制版本清单与校验。
- CLI 下载/构建自动化脚本。

验收标准：

- 新增第三方 CLI 不需要复制执行逻辑。
- 每个工具可追踪用户、目标资源、命令摘要和结果。
- 支持按环境禁用高风险工具。

## 当前建议

先完成 P1 的真实二进制和 OAuth 联调，再扩展 Gmail / Chat。原因是 Google Workspace 的主要不确定性在授权 scopes 和云端二进制，不在 Python Adapter。
