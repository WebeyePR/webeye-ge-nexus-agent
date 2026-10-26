# Google Workspace CLI 接入基准线

## 时间

2026-05-20

## 已知材料

- Release：`googleworkspace/cli` v0.22.5
- 本地源码：`lib/gws-cli-v0.22.5/`
- 官方 Workspace 开发入门：https://developers.google.com/workspace/guides/get-started?hl=zh-cn

## 当前项目基准

项目已成功接入飞书 CLI：

- 二进制随 Python wheel 打包。
- 工具调用时通过环境变量注入用户态 access token。
- 每次执行使用隔离 `HOME`。
- Python Adapter 统一解析 JSON 输出和错误。
- Agent 只调用业务工具，不直接拼接 shell 命令。

Google Workspace CLI 接入应复用上述原则。

## gws v0.22.5 能力基准

`gws` 具备以下特性：

- Rust CLI。
- 支持预编译二进制分发。
- 通过 Google Discovery Service 动态生成命令。
- 默认输出 JSON。
- 支持 `--params`、`--json`、`--fields`、`--page-all`、`--page-limit`。
- 支持 helper commands，例如 `docs +write`、`sheets +append`、`calendar +insert`。

## gws 认证基准

认证来源优先级：

1. `GOOGLE_WORKSPACE_CLI_TOKEN`
2. `GOOGLE_WORKSPACE_CLI_CREDENTIALS_FILE`
3. `~/.config/gws/credentials.enc`
4. `~/.config/gws/credentials.json`
5. ADC：`GOOGLE_APPLICATION_CREDENTIALS` 或 well-known ADC path

本项目采用第 1 种方式：

```text
ADK/GE OAuth access token -> GOOGLE_WORKSPACE_CLI_TOKEN
```

原因：

- 无需交互登录。
- 无需本地 keyring。
- 无需持久化 refresh token。
- 符合当前飞书 CLI 的用户态授权模式。

## 必须满足的上线条件

- `lark_agent/bin/gws` 使用云端平台匹配二进制。
- `GOOGLE_WORKSPACE_AUTH_ID` 与 GE OAuth 配置一致。
- OAuth scopes 覆盖启用工具所需权限。
- 工具层不暴露任意 shell。
- 写操作通过专用工具或受控通用 `gws` 执行工具暴露；通用工具默认 dry-run。
- 401/403 错误向用户透出，便于判断 token 过期、scope 缺失或资源无权限。

## 已完成基准

- 通用 `PackagedCLIClient`。
- `GoogleWorkspaceCLIClient`。
- package-data 增加 `bin/gws`。
- 已放入 v0.22.5 Linux x86_64 `lark_agent/bin/gws` 二进制。
- 部署和本地运行统一解析 `lark_agent/bin/<name>`，不再使用项目根目录 `bin/` 或 PATH 覆盖。
- Google Workspace token 读取。
- 首批 Drive / Docs / Sheets / Calendar 工具。
- 通用探索与执行工具：`discover_google_workspace_operations`、`get_google_workspace_operation_schema`、`execute_google_workspace_cli`。
- Agent 工具注册与系统提示更新。
- 单元测试覆盖命令构造、缺失授权、token 注入和 JSON 解析。

## 未完成基准

- 尚未配置 Gemini Enterprise Google Workspace OAuth。
- 尚未进行真实 Google API 联调。
- 尚未实现 Gmail / Chat / Drive 导出等高风险或高复杂度能力。
- 尚未接入写操作审计和显式二次确认。
