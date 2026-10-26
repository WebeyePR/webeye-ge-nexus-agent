# lark-cli / gws CLI 支持能力分类整理

更新日期：2026-05-25（Asia/Shanghai）

本文件基于两类来源交叉确认：

- 本地命令实测：`lark-cli --help`、`lark-cli auth --help`、`lark-cli calendar --help`、`lark-cli im --help`、`gws --help`、`gws auth --help`、`gws workflow --help`
- 官方文档/官方仓库：
  - `lark-cli` 官方仓库 README：https://github.com/larksuite/cli/blob/main/README.md
  - 飞书开放平台文档首页：https://open.feishu.cn/document/home/index
  - `gws` 官方仓库 README：https://github.com/googleworkspace/cli
  - Google Workspace 认证总览：https://developers.google.com/workspace/guides/auth-overview

补充说明：

- 本地版本：
  - `lark-cli version 1.0.22`
  - `gws 0.22.5`
- 本机在 2026-05-25 实测时，`gws` 某些服务级帮助和 schema 会访问 Google Discovery 文档，出现 `discoveryError` 超时。因此本文对 `gws` 的“支持哪些服务/通用能力”以本地顶层帮助 + 官方 README 为主。

## 1. 两个 CLI 的能力模型

### lark-cli

`lark-cli` 是飞书官方 CLI，能力分三层：

- Shortcut 层：面向高频场景的 `+` 命令，例如 `calendar +agenda`
- API Command 层：按飞书 OpenAPI 资源模型暴露命令，例如 `calendar events get`
- Raw API 层：直接调用开放平台接口，例如 `lark-cli api GET /open-apis/...`

本地确认到的横向能力：

- 认证与凭据：`auth`、`config`、`profile`
- API 结构发现：`schema`
- 健康检查：`doctor`
- 通用请求能力：`api`
- 输出与筛选：`--format json|ndjson|table|csv|pretty`、`--jq`
- 分页：`--page-all`、`--page-size`、`--page-limit`、`--page-delay`
- 身份切换：`--as user|bot|auto`

### gws

`gws` 是 Google Workspace CLI。根据官方 README，它的命令面是从 Google Discovery Service 动态生成，并补充一批人工封装的 `+helper` 命令。

本地确认到的横向能力：

- 认证：`gws auth login|setup|status|export|logout`
- API 结构发现：`gws schema <service.resource.method>`
- 通用请求参数：`--params`、`--json`
- 文件上传下载：`--upload`、`--output`
- 输出：`--format json|table|yaml|csv`
- 分页：`--page-all`、`--page-limit`、`--page-delay`
- API 版本覆盖：`--api-version`
- 跨服务 helper：`gws workflow ...`

## 2. lark-cli 支持能力分类

### 2.1 协同办公内容类

- `docs`：文档创建、读取、更新、搜索
- `drive`：文件上传下载、权限、评论、云空间文件管理
- `wiki`：知识空间、节点管理
- `sheets`：电子表格读写、追加、查找、导出
- `slides`：幻灯片创建、读取、页面增删
- `whiteboard`：画板创建与编辑
- `base`：多维表格/数据库能力，含表、字段、记录、视图、Dashboard、Workflow、Form、角色权限

说明：

- 官方 README 额外列出 Markdown 能力；本地顶层帮助未单独显示 `markdown` 命令，当前版本更像整合在 Drive/Docs 相关能力中。若后续要精确落地某条 Markdown 命令，建议再以本机版本执行 `lark-cli help` 或查对应 release note。

### 2.2 沟通与消息类

- `im`：消息发送、回复、群聊创建与更新、消息检索、线程消息、图片/文件下载、反应等
- 本地 `lark-cli im --help` 已确认存在以下高频 helper：
  - `+messages-send`
  - `+messages-reply`
  - `+messages-search`
  - `+chat-create`
  - `+chat-search`
  - `+chat-messages-list`
  - `+threads-messages-list`
  - `+messages-resources-download`

### 2.3 日历与会议类

- `calendar`：日历、日程、参会人管理
- 本地 `lark-cli calendar --help` 已确认存在以下 helper：
  - `+agenda`
  - `+create`
  - `+update`
  - `+freebusy`
  - `+room-find`
  - `+suggestion`
  - `+rsvp`
- `vc`：视频会议、会议记录、会议纪要/录制相关信息
- `minutes`：妙记内容和元数据检索

### 2.4 组织、人事、流程类

- `contact`：用户/组织信息查询
- `attendance`：考勤记录查询
- `approval`：审批实例与审批任务管理
- `okr`：OKR 目标、关键结果、对齐关系、指标与进展
- `task`：任务、任务清单、子任务、提醒

### 2.5 基础设施与平台能力

- `auth`：登录、登出、状态、scope 校验、账号列表
- `config`：全局配置
- `profile`：配置 profile 管理
- `schema`：查看接口参数、类型、scope
- `doctor`：配置、认证、连通性检查
- `event`：实时事件消费与管理
- `api`：直接调用开放平台 API
- `update`：更新 CLI

## 3. gws 支持能力分类

### 3.1 文档与文件类

- `drive`：文件、文件夹、共享盘
- `docs`：Google Docs 文档读写
- `sheets`：Google Sheets 电子表格
- `slides`：Google Slides 演示文稿
- `forms`：Google Forms 表单
- `keep`：Google Keep 笔记

### 3.2 沟通与协作类

- `gmail`：邮件收发与管理
- `chat`：Google Chat 空间和消息
- `calendar`：日历和事件
- `meet`：Google Meet 会议
- `tasks`：Google Tasks 任务
- `people`：联系人与个人资料

### 3.3 教育与组织管理类

- `classroom`：班级、成员、课程作业
- `admin-reports`：审计日志与使用报告

### 3.4 自动化与开发类

- `script`：Google Apps Script 项目管理
- `events`：Google Workspace Events 订阅
- `workflow`：跨服务工作流 helper
- `modelarmor`：响应内容安全过滤

### 3.5 本地确认到的 gws helper 能力

本地 `gws workflow --help` 已确认：

- `+standup-report`：今日会议 + 未完成任务摘要
- `+meeting-prep`：下一个会议的准备信息
- `+email-to-task`：把邮件转为任务
- `+weekly-digest`：周摘要
- `+file-announce`：将 Drive 文件公告到 Chat

官方 README 还列出了部分服务级 helper：

- `gmail +send|+reply|+reply-all|+forward|+triage|+watch`
- `sheets +append|+read`
- `docs +write`
- `chat +send`
- `drive +upload`
- `calendar +insert|+agenda`
- `script +push`

说明：

- 这些 helper 是否在你本机全部可直接列出，取决于服务帮助是否能成功拉取对应 Discovery 文档。2026-05-25 本机访问 Google Discovery 时有超时，因此未逐项本地枚举完成。

## 4. 两者共有的“横向能力”分类

### 4.1 认证与授权

`lark-cli`：

- 应用初始化：`lark-cli config init`
- 用户授权：`lark-cli auth login`
- 用户/机器人身份切换：`--as user|bot`
- Scope 相关检查：`auth check`、`auth scopes`

`gws`：

- 本地 OAuth：`gws auth login`
- 一次性项目/OAuth 初始化：`gws auth setup`
- 凭据状态：`gws auth status`
- 凭据导出：`gws auth export`
- 凭据来源优先级：环境变量 token、credentials file、CLI 加密凭据、明文本地凭据

官方文档交叉确认：

- Google 官方认证文档明确区分 OAuth 2 client ID、service account、scope、user consent、access token 等概念，和 `gws` README 的认证模式一致。
- `lark-cli` 官方 README 明确要求先 `config init`，再 `auth login`，与本地帮助和实际使用一致。

### 4.2 API 发现与自描述

`lark-cli`：

- `schema <service.resource.method>` 查看参数、类型、scope
- `api` 直接调用飞书开放平台路径

`gws`：

- `schema <service.resource.method>` 查看 Discovery 派生 schema
- 所有服务和方法理论上都来自 Google Discovery Service

差异：

- `lark-cli` 的命令面更“预制”，高频 helper 更稳定。
- `gws` 的命令面更“动态”，覆盖广，但服务级帮助和 schema 更依赖 Discovery 拉取成功。

### 4.3 输出、分页与机器可读性

`lark-cli`：

- 输出：`json`、`ndjson`、`table`、`csv`、`pretty`
- 筛选：`--jq`
- 分页：`--page-all`、`--page-size`、`--page-limit`、`--page-delay`

`gws`：

- 输出：`json`、`table`、`yaml`、`csv`
- 分页：`--page-all`、`--page-limit`、`--page-delay`
- 上传下载：`--upload`、`--output`

### 4.4 面向 Agent/自动化的适配

`lark-cli`：

- 官方 README 强调 Agent-native 设计
- Shortcut + API Command + Raw API 三层结构
- 返回结构化结果，适合代理执行

`gws`：

- 官方 README 强调结构化 JSON 输出
- Discovery 自动生成 API 面
- 提供 workflow helper 和按服务分类的 agent skills

## 5. 使用时的准确边界

### lark-cli 更适合的场景

- 飞书高频办公流：消息、日程、会议室、文档、云盘、妙记、审批、任务
- 需要明确区分 user/bot 身份的场景
- 希望优先用预置 helper，而不是自己拼 OpenAPI 参数

### gws 更适合的场景

- Google Workspace 多产品统一入口：Drive、Gmail、Calendar、Sheets、Docs、Chat、Meet、Forms 等
- 已有 OAuth/GCP 配置，希望快速覆盖多个 Google API
- 需要 Discovery 驱动的广覆盖，而不是只做少量人工封装命令

### 当前本机实测限制

- `lark-cli`：核心命令帮助可正常读取
- `gws`：顶层帮助、认证帮助、workflow 帮助可正常读取；部分服务级帮助与 schema 在本机这次实测中访问 Google Discovery 超时，报 `discoveryError`

## 6. 结论

若按“支持功能分类”来看：

- `lark-cli` 更像飞书办公域的深度工具，业务 helper 更完整，尤其适合消息、文档、日程、任务、审批等高频协同场景
- `gws` 更像 Google Workspace 的统一 API 入口，产品覆盖很广，认证/Schema/分页/上传下载能力完整，但其服务级命令面更依赖 Google Discovery 拉取成功

若按“整理给团队看”的方式，建议把两者统一归纳为四大类：

1. 办公内容类：Docs/Drive/Sheets/Slides/Wiki/Base/Forms/Keep
2. 沟通协作类：IM/Gmail/Chat/Calendar/Meet/Tasks/People
3. 组织流程类：Approval/Attendance/OKR/Contact/Admin reports/Classroom
4. 平台与自动化类：Auth/Schema/API/Event/Workflow/Script/Model Armor
