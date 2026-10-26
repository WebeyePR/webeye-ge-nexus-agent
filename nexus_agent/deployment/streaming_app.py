"""StreamingAdkApp：为 Gemini Enterprise 的 :streamQuery 调用链启用 token 级 SSE 流式。

## 背景

Gemini Enterprise 对注册在 Agent Runtime 上的 ADK Agent，固定通过
``streaming_agent_run_with_events`` 方法发起 ``:streamQuery`` 调用。
官方部署模板 AdkApp 的该方法在调用 ``runner.run_async()`` 时仅在存在
labels 时构造 RunConfig（且从不设置 ``StreamingMode.SSE``，默认
``StreamingMode.NONE`` 每轮只回一个完整事件），导致 GE 侧表现为
"整轮完成后一次性返回"（非流式）。

## 方案

子类化 AdkApp，完整复制 google-cloud-aiplatform==2.6.0 模板实现
（源码位置：``vertexai/agent_engines/templates/adk.py`` 的
``streaming_agent_run_with_events``，1496-1620 行），仅在构造
``run_config`` 处注入 ``streaming_mode=StreamingMode.SSE``，
并保留模板新增的 labels 透传 / authorizations state_delta /
_save_artifacts 逻辑。

## ⚠️ 版本约束（重要）

google-cloud-aiplatform 已在 pyproject.toml 中精确锁定为 ==2.6.0。
**升级该包前必须先 diff 模板源码与本文件**，确认实现无漂移后再同步更新，
否则会导致 GE 调用链路失败。详见 docs/ADK2全面升级调研与方案.md 与
docs/非流式与慢问题解决方案.md。

## 回滚开关

设置环境变量 ``AGENT_ENABLE_SSE_STREAMING=0`` 并重新部署，
可回退到模板默认行为（非流式），无需回滚代码。
"""

import json
import os

from google.adk.agents.run_config import RunConfig, StreamingMode
from google.genai import types
from google.genai.errors import ClientError
from vertexai.agent_engines import AdkApp
from vertexai.agent_engines.templates.adk import (
    _Authorization,
    _StreamRunRequest,
    _force_flush_otel,
)

__all__ = ["StreamingAdkApp"]


class StreamingAdkApp(AdkApp):
    """在 GE 固定调用入口 ``streaming_agent_run_with_events`` 中启用 SSE 流式。"""

    async def streaming_agent_run_with_events(self, request_json: str):
        """以 SSE 流式方式处理 GE 的 :streamQuery 请求。

        实现复制自 aiplatform==2.6.0 模板（同签名同语义），唯一差异为
        ``run_config`` 恒以 ``streaming_mode=StreamingMode.SSE`` 构造
        （模板仅在存在 labels 时构造且不含 SSE）。

        Args:
            request_json (str):
                Required. GE 传入的一轮对话上下文（JSON 字符串）。
        """
        # 运维回滚开关：置 0 时完全退回模板默认行为（非流式）
        if os.getenv("AGENT_ENABLE_SSE_STREAMING", "1") != "1":
            async for event in super().streaming_agent_run_with_events(request_json):
                yield event
            return

        # ---- 以下逻辑与 aiplatform==2.6.0 模板逐行对齐 ----
        request = _StreamRunRequest(**json.loads(request_json))
        if not any(
            self._tmpl_attrs.get(service)
            for service in (
                "in_memory_runner",
                "runner",
                "in_memory_artifact_service",
                "artifact_service",
                "in_memory_session_service",
                "session_service",
                "in_memory_memory_service",
                "memory_service",
            )
        ):
            self.set_up()

        # Forward the user's OAuth access tokens as ephemeral `temp:` state.
        # ADK exposes `temp:` keys to the agent for the duration of the
        # invocation but trims them before the session is written to durable
        # storage, so the tokens are never persisted.
        state_delta = None
        if request.authorizations:
            state_delta = {}
            for auth_id, auth in request.authorizations.items():
                auth = _Authorization(**auth)
                state_delta[f"temp:{auth_id}"] = auth.access_token

        # Try to get the session, if it doesn't exist, create a new one.
        if request.session_id:
            session_service = self._tmpl_attrs.get("session_service")
            artifact_service = self._tmpl_attrs.get("artifact_service")
            runner = self._tmpl_attrs.get("runner")
            session = None
            try:
                session = await session_service.get_session(
                    app_name=self._app_name(),
                    user_id=request.user_id,
                    session_id=request.session_id,
                )
                if session:
                    await self._save_artifacts(
                        session_id=request.session_id,
                        artifact_service=artifact_service,
                        request=request,
                    )
            except ClientError:
                pass
            if not session:
                #  Fall back to create session if the session is not found.
                #  Specifying session_id on creation is not supported,
                #  so session id will be regenerated.
                session = await self._init_session(
                    session_service=session_service,
                    artifact_service=artifact_service,
                    request=request,
                )
        else:
            # Not providing a session ID will create a new in-memory session.
            session_service = self._tmpl_attrs.get("in_memory_session_service")
            artifact_service = self._tmpl_attrs.get("in_memory_artifact_service")
            runner = self._tmpl_attrs.get("in_memory_runner")
            session = await self._init_session(
                session_service=session_service,
                artifact_service=artifact_service,
                request=request,
            )
        if not session:
            raise RuntimeError("Session initialization failed.")

        # Run the agent
        message_for_agent = types.Content(**request.message)

        # ===== 与模板的唯一差异：恒注入 SSE 流式 =====
        # 模板原逻辑：仅当存在 labels 且 RunConfig 支持 labels 字段时才
        # 构造 RunConfig(labels=...)，无 SSE。此处改为恒以 SSE 构造，
        # 并在 RunConfig 支持时保留 labels 透传（计费/归因）。
        run_config_kwargs = {"streaming_mode": StreamingMode.SSE}
        if request.labels and "labels" in RunConfig.model_fields:
            run_config_kwargs["labels"] = request.labels
        run_config = RunConfig(**run_config_kwargs)

        try:
            async for event in runner.run_async(
                user_id=request.user_id,
                session_id=session.id,
                new_message=message_for_agent,
                state_delta=state_delta,
                run_config=run_config,
            ):
                converted_event = await self._convert_response_events(
                    user_id=request.user_id,
                    session_id=session.id,
                    events=[event],
                    artifact_service=artifact_service,
                )
                yield converted_event
        finally:
            if session and not request.session_id:
                await session_service.delete_session(
                    app_name=self._app_name(),
                    user_id=request.user_id,
                    session_id=session.id,
                )
            # Avoid telemetry data loss having to do with CPU throttling on instance turndown
            _ = await _force_flush_otel(
                tracing_enabled=self._tracing_enabled(),
                logging_enabled=bool(self._telemetry_enabled()),
            )
