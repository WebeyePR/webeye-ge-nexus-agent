import sys
# 运行时重定向补丁，在包加载瞬间，建立 lark_agent 到 nexus_agent 的软重定向
try:
    import lark_agent  # noqa: F401  有意保留：旧包名兼容 shim
except ImportError:
    import nexus_agent
    sys.modules['lark_agent'] = nexus_agent

import os
import socket
import tomllib
from concurrent.futures import TimeoutError as FuturesTimeoutError

import dotenv
import vertexai
from google.api_core.future import polling
from vertexai import agent_engines

dotenv.load_dotenv()

from nexus_agent import root_agent
from nexus_agent.deployment.streaming_app import StreamingAdkApp
from nexus_agent.plugins import ToolAuditPlugin
from google.adk.plugins import ReflectAndRetryModelPlugin

PYTHONPATH = os.environ.get("PYTHONPATH", ".")

# 从环境变量读取配置
PROJECT_ID = os.getenv("PROJECT_ID")
# For other options, see https://cloud.google.com/vertex-ai/generative-ai/docs/agent-engine/overview#supported-regions
LOCATION = os.getenv("DEPLOY_LOCATION", "us-central1")
STAGING_BUCKET = os.getenv("STAGING_BUCKET", "gs://adk-agent-deploy")
CREATE_TIMEOUT_SECONDS = float(os.getenv("AGENT_ENGINE_CREATE_TIMEOUT_SECONDS", "2400"))

CURRENT_DIR = os.path.dirname(os.path.abspath(__file__))
# .whl 文件就在脚本所在目录 (由 deploy.sh 生成)
AGENT_WHL_FILE_NAME = "ge_nexus_agent-1.0.0-py3-none-any.whl"
AGENT_WHL_FILE = os.path.join(CURRENT_DIR, AGENT_WHL_FILE_NAME)

# 专门用于存储部署过程中自动生成的参数，优先使用环境变量指定的绝对路径
ROOT_DIR = os.path.dirname(os.path.dirname(CURRENT_DIR))
DEPLOY_ENV_FILE = os.getenv("DEPLOY_ENV_FILE", os.path.join(ROOT_DIR, ".deploy_env"))
PYPROJECT_FILE = os.path.join(ROOT_DIR, "pyproject.toml")


def load_project_requirements(pyproject_file: str) -> list[str]:
    """
    Read runtime dependencies from pyproject.toml so deployment has a single
    source of truth for requirements.
    """
    with open(pyproject_file, "rb") as f:
        data = tomllib.load(f)
    deps = data.get("project", {}).get("dependencies", [])
    if not deps:
        raise ValueError(f"No project.dependencies found in {pyproject_file}")
    return deps

# 增加全局网络超时时间，处理大包上传（如 lark-cli 二进制文件）
socket.setdefaulttimeout(1800)  # 设置为 10 分钟

# 尝试通过猴子补丁 (Monkey Patch) 解决 google-cloud-storage 内部上传超时问题
try:
    from google.cloud import storage
    from google.api_core import retry
    from google.cloud.storage.retry import DEFAULT_RETRY

    # 这里的 120s 往往是 google-api-core 的默认重试截止时间
    # 我们尝试覆盖 Blob 对象的默认上传行为或重试策略
    def patched_upload_from_string(self, data, *args, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = 1800  # 强制设置 10 分钟超时
        if "retry" not in kwargs or kwargs["retry"] is DEFAULT_RETRY:
            kwargs["retry"] = DEFAULT_RETRY.with_timeout(1800)
        return self._old_upload_from_string(data, *args, **kwargs)

    def patched_upload_from_file(self, file_obj, *args, **kwargs):
        if "timeout" not in kwargs:
            kwargs["timeout"] = 1800
        if "retry" not in kwargs or kwargs["retry"] is DEFAULT_RETRY:
            kwargs["retry"] = DEFAULT_RETRY.with_timeout(1800)
        return self._old_upload_from_file(file_obj, *args, **kwargs)

    if not hasattr(storage.blob.Blob, "_old_upload_from_string"):
        storage.blob.Blob._old_upload_from_string = storage.blob.Blob.upload_from_string
        storage.blob.Blob.upload_from_string = patched_upload_from_string
        print("Patched google.cloud.storage.blob.Blob.upload_from_string with 1800s timeout.")

    if not hasattr(storage.blob.Blob, "_old_upload_from_file"):
        storage.blob.Blob._old_upload_from_file = storage.blob.Blob.upload_from_file
        storage.blob.Blob.upload_from_file = patched_upload_from_file
        print("Patched google.cloud.storage.blob.Blob.upload_from_file with 1800s timeout.")

    # 针对 Retry 策略的截止时间进行补丁
    # vertex ai sdk 内部可能使用了带有默认 deadline 的 retry
    _old_retry_init = retry.Retry.__init__
    def patched_retry_init(self, *args, **kwargs):
        if "deadline" in kwargs and kwargs["deadline"] == 120.0:
            kwargs["deadline"] = 1800.0
        _old_retry_init(self, *args, **kwargs)
    retry.Retry.__init__ = patched_retry_init
    print("Patched google.api_core.retry.Retry deadline to 1800s.")
except ImportError:
    pass

# Initialize the Vertex AI SDK
vertexai.init(
    project=PROJECT_ID,
    location=LOCATION,
    staging_bucket=STAGING_BUCKET,
)

# Vertex SDK internally waits on the LRO with google.api_core.future.polling.DEFAULT_POLLING,
# which defaults to 900 seconds. Large builds can exceed that even when the remote operation
# eventually succeeds, so we extend the polling timeout here.
polling.DEFAULT_POLLING = polling.DEFAULT_POLLING.with_timeout(CREATE_TIMEOUT_SECONDS)

# 用 StreamingAdkApp 包装 Agent（在 GE 的 :streamQuery 链路上启用 SSE 流式，
# 详见 nexus_agent/deployment/streaming_app.py 模块说明）
#
# ADK 2.13 原生上下文缓存（App 级 ContextCacheConfig）：instruction ~5k tokens +
# 36 工具 schema 远超 Gemini 3 的 4096 缓存门槛，跨轮复用前缀显著降本提速；
# 缓存创建失败时官方设计为优雅降级（请求照常、不缓存）。环境变量可关。
from google.adk.apps import App
from google.adk.agents.context_cache_config import ContextCacheConfig

if os.getenv("AGENT_ENABLE_CONTEXT_CACHE", "1") == "0":
    # 回退路径：不预构建 App，保持模板默认行为（agent + app_name 显式传入）
    # 注意：不传 enable_tracing（已弃用），遥测由部署 env_vars 的
    # GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY=false 统一关闭（下方 _run_deploy）
    app = StreamingAdkApp(
        agent=root_agent,
        app_name="webeye-nexus-agent-app",
        plugins=[
            ToolAuditPlugin(),
            ReflectAndRetryModelPlugin(),
        ],
    )
else:
    _plugins = [
        ToolAuditPlugin(),  # ADK 原生插件：突变类工具调用集中审计
        ReflectAndRetryModelPlugin(),  # ADK 原生插件：模型瞬时故障自愈重试（并发安全，默认 3 次）
    ]
    _deploy_app = App(
        name="webeye-nexus-agent-app",
        root_agent=root_agent,
        plugins=_plugins,  # 预构建 App 时插件必须挂在 App 上（AdkApp 禁止重复传入）
        context_cache_config=ContextCacheConfig(min_tokens=4096),
    )
    # app_name/agent/plugins 均禁止传入（由 App 派生）
    # 注意：不传 enable_tracing（已弃用），遥测由部署 env_vars 统一关闭（下方 _run_deploy）
    app = StreamingAdkApp(
        app=_deploy_app,
    )

def _run_deploy() -> None:
    """执行真实部署。仅在本模块作为脚本运行时触发（防 import 副作用误部署）。"""

    # 打印文件大小供调试
    if os.path.exists(AGENT_WHL_FILE):
        size_mb = os.path.getsize(AGENT_WHL_FILE) / (1024 * 1024)
        print(f"Preparing to upload {AGENT_WHL_FILE} ({size_mb:.2f} MB)...")
    else:
        print(f"Error: {AGENT_WHL_FILE} not found!")
        sys.exit(1)

    print(
        f"Starting Agent Engine deploy with polling timeout {int(CREATE_TIMEOUT_SECONDS)} seconds..."
    )

    try:
        # 强制重新加载环境变量，确保 deploy.sh 加载的变量生效
        dotenv.load_dotenv(override=True)

        # Install the project wheel remotely. Its METADATA is generated from
        # pyproject.toml, so runtime dependencies still have a single source of
        # truth while the lark_agent package itself is installed before unpickling.
        os.chdir(CURRENT_DIR)
        requirements_source = [AGENT_WHL_FILE_NAME] + load_project_requirements(PYPROJECT_FILE)
        print(f"Deploying using pyproject.toml requirements: {requirements_source}")

        # 部署规格参数（治"慢"：消除冷启动排队与并发错配，官方 Optimize and scale 建议）
        # min_instances：保持实例温热，避免冷启动（官方实测 ~4.7s vs 温启动 0.4s，上限 10）
        min_instances = int(os.getenv("AGENT_MIN_INSTANCES", "1"))
        # container_concurrency：ADK 异步 Agent 官方建议设为 9 的倍数（如 36），
        # 高并发下最大延迟可从 ~60s 降至 ~7s；内存不足时调低并同时调小并发
        container_concurrency = int(os.getenv("AGENT_CONTAINER_CONCURRENCY", "36"))
        # resource_limits：默认高于官方缺省（cpu=4/mem=4Gi），为 36 并发预留余量防 OOM
        resource_limits = {
            "cpu": os.getenv("AGENT_CPU", "4"),
            "memory": os.getenv("AGENT_MEMORY", "8Gi"),
        }
        print(
            f"Deployment spec: min_instances={min_instances}, "
            f"container_concurrency={container_concurrency}, resource_limits={resource_limits}"
        )

        remote_app = agent_engines.create(
            agent_engine=app,
            requirements=requirements_source,
            extra_packages=[AGENT_WHL_FILE_NAME],
            display_name=os.getenv("AGENT_DISPLAY_NAME", "WebEye Nexus Agent"),
            min_instances=min_instances,
            container_concurrency=container_concurrency,
            resource_limits=resource_limits,
            env_vars={
                "LARK_AUTH_ID": os.getenv("LARK_AUTH_ID"),
                "LARK_DOMAIN": os.getenv("LARK_DOMAIN"),
                "LARK_CLIENT_ID": os.getenv("LARK_CLIENT_ID"),
                "GOOGLE_WORKSPACE_AUTH_ID": os.getenv("GOOGLE_WORKSPACE_AUTH_ID"),
                "GOOGLE_WORKSPACE_PROJECT_ID": os.getenv(
                    "GOOGLE_WORKSPACE_PROJECT_ID", os.getenv("PROJECT_ID")
                ),
                # 工具硬确认开关（GE 确认事件流验证未过时置 0 回退，见验证清单 F5）
                "AGENT_ENABLE_TOOL_CONFIRMATION": os.getenv(
                    "AGENT_ENABLE_TOOL_CONFIRMATION", "1"
                ),
                # 遥测/Tracing 官方开关（替代已弃用的 AdkApp enable_tracing 参数，
                # false 与原 enable_tracing=False 语义等效：Tracing 真值表
                # None+false→false）。默认关闭，需要时部署前设 env 为 true。
                "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY": os.getenv(
                    "GOOGLE_CLOUD_AGENT_ENGINE_ENABLE_TELEMETRY", "false"
                ),
                # ADK 2.13 模型通道判定（旧 GOOGLE_GENAI_USE_VERTEXAI 已弃用）：
                # 生产必须是 Vertex AI/Enterprise 模式。此处**硬编码为 1**，
                # 不读本地 .env：本地开发常设 0（AI Studio 通道），若透传会把
                # 生产引擎错误配成 AI Studio，导致无 API Key 时模型调用失败。
                # 需要临时切 AI Studio 的极端场景请直接改这个字面量再部署。
                "GOOGLE_GENAI_USE_ENTERPRISE": "1",
                # 模型装配（与 nexus_agent/agent.py 读取逻辑一致，远程运行时
                # 可配置）：主模型 + FallbackModel 跨模型故障转移链候选
                "MODEL_NAME": os.getenv("MODEL_NAME", "gemini-3.8-flash"),
                "AGENT_FALLBACK_MODELS": os.getenv(
                    "AGENT_FALLBACK_MODELS", "gemini-3.7-flash,gemini-3.6-flash"
                ),
                # 单次运行 LLM 调用上限（RunConfig.max_llm_calls 的 env 覆盖，
                # ADK 默认 500）：防失控循环的护栏，200 对工具链场景余量充足
                "ADK_MAX_LLM_CALLS": os.getenv("ADK_MAX_LLM_CALLS", "200"),
            },
        )
    except FuturesTimeoutError:
        print(
            "Deployment timed out while waiting for the Reasoning Engine LRO to finish. "
            "This often means the remote build is still running rather than a hard failure."
        )
        print(
            "Recommended actions: check Cloud Logging / Vertex AI Agent Engine console, "
            "then rerun with a larger AGENT_ENGINE_CREATE_TIMEOUT_SECONDS if needed."
        )
        raise

    print("Deployment finished!")
    print(f"Resource Name: {remote_app.resource_name}")

    # 自动回写到独立的 .deploy_env 文件
    # 不再修改手动维护的 .env 文件
    try:
        dotenv.set_key(
            DEPLOY_ENV_FILE,
            "VERTEX_REASONING_ENGINE_NAME",
            remote_app.resource_name,
            quote_mode="always",
        )
        print(f"Successfully updated VERTEX_REASONING_ENGINE_NAME in {DEPLOY_ENV_FILE}")
    except Exception as e:
        print(f"Warning: Failed to update {DEPLOY_ENV_FILE}: {e}")


if __name__ == "__main__":
    _run_deploy()
