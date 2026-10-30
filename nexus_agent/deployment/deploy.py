import os
import sys

# --- 源码树路径注入（必须在任何 nexus_agent 导入之前）------------------------
# 脚本方式运行（python nexus_agent/deployment/deploy.py）时 sys.path[0] 是
# deployment 目录，项目根不在其中；此时 import nexus_agent 会命中 .venv 里
# 由 `uv sync --no-editable` 生成的陈旧安装副本，导致部署上传旧代码且无报错。
# 实测：旧副本的 model 是 str，源码是 FallbackModel 链——降级能力静默消失。
# 因此这里先算项目根（__file__ 上溯两级）并置于 sys.path 首位，再做导入。
_ROOT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if not sys.path or sys.path[0] != _ROOT_DIR:
    sys.path.insert(0, _ROOT_DIR)

# 运行时重定向补丁，在包加载瞬间，建立 lark_agent 到 nexus_agent 的软重定向
try:
    import lark_agent  # noqa: F401  有意保留：旧包名兼容 shim
except ImportError:
    import nexus_agent
    sys.modules['lark_agent'] = nexus_agent

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
# ROOT_DIR 已在上方（导入区）定义，此处复用，避免两处定义漂移
ROOT_DIR = _ROOT_DIR
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


def ensure_project_root_on_path() -> None:
    """把项目根置于 sys.path 首位，保证脚本方式运行也能解析到源码树。

    2026-10-27 实测事故路径：`python nexus_agent/deployment/deploy.py` 的
    sys.path[0] 是 nexus_agent/deployment，项目根不在路径中。此时
    `import nexus_agent` 会解析到 .venv/…/site-packages 下的**陈旧安装副本**
    （deploy.sh 的 `uv sync --no-editable` 会生成该副本）。实测差异：
    源码 model 是 FallbackModel 链，旧副本 model 退化为 str——部署出去
    FallbackModel 静默消失，且全程零报错。

    幂等：已在首位则不重复插入。
    """
    root = ROOT_DIR
    if not sys.path or sys.path[0] != root:
        # 先移除历史位置，避免重复累积
        sys.path[:] = [p for p in sys.path if p != root]
        sys.path.insert(0, root)


def verify_source_tree() -> None:
    """部署前守卫：拦截「导入到 site-packages 陈旧副本」的部署。

    不拦的后果是静默的——远程容器拿到旧代码，功能缺失但无任何报错。
    """
    import nexus_agent

    pkg_file = os.path.realpath(nexus_agent.__file__)
    expected = os.path.join(ROOT_DIR, "nexus_agent")
    if "site-packages" in pkg_file or "dist-packages" in pkg_file:
        raise RuntimeError(
            "部署中止：nexus_agent 解析到了已安装副本，而非项目源码树\n"
            f"  实际路径: {pkg_file}\n"
            f"  期望根目录: {expected}\n"
            "  处置：删除 .venv/lib/python*/site-packages/nexus_agent 与 "
            "ge_nexus_agent-*.dist-info（陈旧副本），或改用 "
            "`uv run --no-sync python nexus_agent/deployment/deploy.py`。"
        )
    print(f"Source tree verified: {pkg_file}")


def verify_serialization(app) -> None:
    """部署前守卫：扫描序列化产物，拦截远程必然失败的动态符号。

    cloudpickle 对「模块中按名取不到的函数」会退化为 STACK_GLOBAL 名字引用，
    远程进程若无该名字即反序列化失败。2026-10-27 实测本项目当前产物健康
    （55 个符号全部可达），此守卫为防回归而设，非修既有 bug。
    """
    import pickletools

    import cloudpickle

    blob = cloudpickle.dumps(app)
    bad: set[str] = set()
    pending: list[str] = []
    for op, arg, _ in pickletools.genops(blob):
        if op.name in ("SHORT_BINUNICODE", "BINUNICODE") and isinstance(arg, str):
            pending.append(arg)
            if len(pending) > 2:
                pending.pop(0)
        elif op.name == "STACK_GLOBAL" and len(pending) >= 2:
            module, name = pending[-2], pending[-1]
            # 按名解析的内部实现符号（_xxx_impl）是历史事故特征
            if name.startswith("_") and "_impl" in name:
                bad.add(f"{module}.{name}")
    if bad:
        raise RuntimeError(
            "部署中止：序列化产物含按名解析的动态符号，远程必然反序列化失败\n"
            f"  可疑符号: {sorted(bad)}\n"
            "  处置：确认工具函数在模块级有公开绑定（不要只包装内部实现名）。"
        )
    print(f"Serialization verified: {len(blob) / 1024:.1f} KB, symbols OK")

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
        # cache_intervals=50：默认 10 意味着每 10 次调用重建一次 10.7k token
        # 的缓存。2026-10-27 实测（连续 15 轮观察 cache_metadata）：第 11 轮
        # inv 达上限 10，第 12 轮 fingerprint 变更、inv 归零 → 确实周期性重建，
        # 重建轮次延迟明显高于稳态（稳态 2.6~3.3s vs 刷新点 7.9~15.2s）。
        # 提到 50 让刷新频率降 80%；ttl_seconds 默认 1800s 足够覆盖。
        context_cache_config=ContextCacheConfig(
            min_tokens=4096,
            cache_intervals=int(os.getenv("AGENT_CACHE_INTERVALS", "50")),
        ),
    )
    # app_name/agent/plugins 均禁止传入（由 App 派生）
    # 注意：不传 enable_tracing（已弃用），遥测由部署 env_vars 统一关闭（下方 _run_deploy）
    app = StreamingAdkApp(
        app=_deploy_app,
    )

def _run_deploy() -> None:
    """执行真实部署。仅在本模块作为脚本运行时触发（防 import 副作用误部署）。"""

    # 部署前守卫（2026-10-27）：源码树来源 + 序列化产物。
    # 二者都对应「本地全绿、远程静默失败」类事故，必须在真正上传前拦截。
    ensure_project_root_on_path()
    verify_source_tree()
    verify_serialization(app)

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
        # min_instances：默认 0（scale-to-zero，闲置不产生实例费用）。
        # 代价（2026-10-27 实测，须知晓）：
        #   - 空闲一段时间后首个请求触发冷启动，实测出现过 26s 首响应、
        #     甚至 167s 的工具链静默（模型在等首个 token）；
        #   - 预热后回落到 3~8s。
        # 若某场景无法接受冷启动，部署前设 AGENT_MIN_INSTANCES=1 恢复常驻
        # （官方实测冷启动 ~4.7s vs 温启动 0.4s，上限 10）。
        min_instances = int(os.getenv("AGENT_MIN_INSTANCES", "0"))
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

        # env_vars 先构造再过滤空值：proto 校验拒绝空字符串，而
        # AGENT_THINKING_LEVEL 等可选开关默认就是空（语义为"不设置"）。
        _env_vars = {
            "LARK_AUTH_ID": os.getenv("LARK_AUTH_ID"),
            # 分批授权（逗号分隔，顺序 = GE toolAuthorizations 的 consent 顺序）。
            # 为空则不注入 → 代码回退单批 LARK_AUTH_ID（未改造/回滚场景）。
            "LARK_AUTH_IDS": os.getenv("LARK_AUTH_IDS"),
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
            # 思考深度（延迟主导变量）。2026-10-27 实测：LOW 档在本项目
            # 6 任务 × 2 轮 A/B 中 12/12 正确且快 45%~67%。留空 = 用模型
                # 思考深度（延迟主导变量）。2026-10-27 真实 A/B 结论：
                # Agent 级 6 任务 × 2 轮 → LOW 12/12 正确、默认档 10/12；
                # 远程实测 LOW 首事件中位 6.0s vs 默认 10.8s，SSE 回归 6/6。
                # 默认 LOW；需更高推理深度时设 MINIMAL/MEDIUM/HIGH 覆盖。
                "AGENT_THINKING_LEVEL": os.getenv("AGENT_THINKING_LEVEL", "LOW"),
        }
        _env_vars = {k: v for k, v in _env_vars.items() if v}

        remote_app = agent_engines.create(
            agent_engine=app,
            requirements=requirements_source,
            extra_packages=[AGENT_WHL_FILE_NAME],
            display_name=os.getenv("AGENT_DISPLAY_NAME", "WebEye Nexus Agent"),
            min_instances=min_instances,
            container_concurrency=container_concurrency,
            resource_limits=resource_limits,
            env_vars=_env_vars,
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
