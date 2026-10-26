# 打包部署指南

## 📋 打包部署前的检查清单

### 1. 环境配置检查

#### .env 文件配置
确保 `.env` 文件包含以下配置：
```env
LARK_AUTH_ID=lark-agent-oauth-id  # 必须与注册时的授权资源ID一致
LARK_DOMAIN=https://open.feishu.cn                # 飞书开放平台域名
GOOGLE_API_KEY=your-api-key                       # 本地开发使用（部署时不需要）
```

**重要提示**：
- `LARK_AUTH_ID` 必须与 Gemini Enterprise 中创建的授权资源 ID 完全一致
- `LARK_DOMAIN` 必须是完整的 URL（包含 `https://`），不能是中文或注释

#### deploy.py 配置
检查 `lark_agent/deployment/deploy.py` 中的配置：
```python
PROJECT_ID = "webeye-internal-test"        # 你的 GCP 项目 ID
LOCATION = "us-central1"                   # 部署区域
STAGING_BUCKET = "gs://adk-agent-deploy"   # GCS 存储桶（必须存在）
```

### 2. 依赖检查

确保 `pyproject.toml` 包含所有必要的依赖（ADK 2.x 升级后全部精确锁定，2026-10-26）：
```toml
dependencies = [
    "google-adk==2.13.0",
    "google-cloud-aiplatform==2.6.0",
    "google-genai==2.33.0",
    "cloudpickle==3.1.2",
    # ... 其余见 pyproject.toml（单一事实源）
]
```

部署时以项目 wheel 为安装入口。也就是说：

- 依赖应只维护在 `pyproject.toml`
- `deploy.py` 不应再维护一份手写依赖列表
- 项目 wheel 的 `METADATA` 由 `pyproject.toml` 生成，远端通过安装 wheel 获取 `lark_agent` 包和第三方依赖
- `requirements.txt` 中只能使用 wheel 文件名，不能写入本地绝对路径
- 这样可以避免本地依赖与云端运行时依赖漂移

### 3. Python 版本要求

**重要**：Agent Engine 目前支持 Python 3.10-3.14。推荐使用最新的 Python 3.14 以获得最佳性能。

---

## 🚀 打包部署流程

### 步骤 1: 构建 Wheel 包

```bash
cd /Users/apple/IdeaProjects/adk-agents
uv build --wheel --out-dir lark_agent/deployment
```

**注意**：必须在项目根目录执行，不能在子目录中执行。

### 步骤 2: 准备部署环境

```bash
# 创建并启用虚拟环境
uv sync && source .venv/bin/activate

# 安装部署所需的依赖（版本以 pyproject.toml 锁定为准，勿手写旧版本）
uv sync
```

### 步骤 3: 执行部署

```bash
cd /Users/apple/IdeaProjects/adk-agents
export PYTHONPATH=$(pwd)
# 可选：如果 Agent Engine 构建较慢，可把等待超时从默认 900 秒调大
export AGENT_ENGINE_CREATE_TIMEOUT_SECONDS=3600
cd lark_agent/deployment
python3 deploy.py
```

**部署成功后会输出**：
```
Deployment finished!
Resource Name: projects/<PROJECT_ID>/locations/<LOCATION>/reasoningEngines/<ENGINE_ID>
```

**请保存这个 Resource Name**，后续注册时需要用到。

### 关于 900 秒超时

Vertex AI Python SDK 在等待 Agent Engine 长任务完成时，默认轮询超时为 900 秒。对于依赖较多或构建较慢的 Agent，这个等待窗口可能不够，表现为：

```text
TimeoutError: Operation did not complete within the designated timeout of 900 seconds.
```

这不一定代表远端构建失败，也可能只是本地等待超时。

当前项目已支持通过环境变量调整等待时间：

```bash
export AGENT_ENGINE_CREATE_TIMEOUT_SECONDS=3600
```

如果再次出现超时，请优先检查：

- Vertex AI Agent Engine 控制台
- Cloud Logging 中对应的构建日志
- 是否已经生成新的 Reasoning Engine 资源

### 关于 GCS 上传 `dependencies.tar.gz` 超时

如果日志卡在：

```text
Writing to gs://.../dependencies.tar.gz
```

随后抛出：

```text
google.api_core.exceptions.RetryError: Timeout of 120.0s exceeded
```

说明问题发生在将 `extra_packages` 打包后的大文件上传到 GCS，而不是 Agent Engine 构建或启动本身。

当前项目的部署脚本已经对以下两层做了补丁：

- `Blob.upload_from_string(..., timeout=600)`
- `Blob.upload_from_file(..., timeout=600)`
- `DEFAULT_RETRY.with_timeout(600)`

如果网络仍不稳定，可考虑：

- 减少 `extra_packages` 体积
- 避免把不必要的大文件打进 wheel
- 在更稳定的网络环境下重新执行 `bash deploy.sh`

---

## 📝 注册到 Gemini Enterprise

### 🚀 一键注册 Agent

我们也提供了一个脚本来处理 OAuth 资源创建和 Agent 注册的全过程：

```bash
./scripts/register_to_GE.sh
```

**该脚本会自动**：
1. 从 `.env` 获取 GCP 项目、Lark 凭证和 Gemini App ID。
2. 从 `.deploy_env` 获取最新部署生成的 Reasoning Engine ID。
3. 如果不存在，则在 Gemini Enterprise 中创建对应的 OAuth 授权资源。
4. 将 Agent 注册到指定的 Gemini App/Engine 中。
5. 完成后自动将 Agent 的完整资源名称保存到 `.deploy_env`，便于后续维护。

---

## ⚠️ 常见问题


### 问题 2: 授权资源被使用
**错误**：`is used by another agent`
**解决**：
1. 查找并删除使用该授权资源的 Agent
2. 或者删除授权资源后重新创建

### 问题 3: 环境变量不匹配
**错误**：`Access token is missing`
**解决**：
1. 确保 `.env` 文件中的 `LARK_AUTH_ID` 与授权资源 ID 一致
2. 重新部署 Agent

### 问题 4: URL 格式错误
**错误**：`Invalid URL '飞书开放平台...'`
**解决**：确保 `.env` 文件中的 `LARK_DOMAIN` 是完整的 URL：`https://open.feishu.cn`

---

## 🔄 更新部署流程

如果代码有更新，需要重新部署：

1. **更新代码**
2. **重新构建**：`uv build --wheel --out-dir lark_agent/deployment`
3. **重新部署**：执行 `bash deploy.sh`
4. **重新注册**：执行 `scripts/register_to_GE.sh`，脚本会自动根据最新生成的部署 ID 进行注册
5. **删除旧版本**（可选）：可以使用 `scripts/cleanup_deployed.sh`（一键清理当前项目资源）或 `scripts/batch_delete.sh`（交互式清理）
---

## 📦 环境变量说明

### 部署时的环境变量

部署时，`deploy.py` 会从 `.env` 文件读取以下环境变量并设置到 Agent Engine：

- `LARK_AUTH_ID`: 授权资源 ID，用于从 `tool_context.state` 获取 access_token
- `LARK_DOMAIN`: 飞书 API 域名

**重要**：这些环境变量在部署时设置，修改 `.env` 后必须重新部署才能生效。

### 运行时获取 Token

代码中通过以下方式获取 access_token：
```python
access_token = tool_context.state.get(f"{LARK_AUTH_ID}")
```

Gemini Enterprise 会自动管理 OAuth 流程，将 token 存储到 `tool_context.state` 中。
