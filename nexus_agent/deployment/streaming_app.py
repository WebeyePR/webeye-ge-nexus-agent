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

## SSE 的「文本重复两遍」（2026-10-27 实测确认并修复）

ADK 的 SSE 模式**同时**产出两类事件，文本内容相同：

- ``partial=True``：流式分段（打字机效果所需）
- ``partial=False``：聚合事件，文本与所有分段累加后**逐字相同**

GE 前端把两者都当正文渲染 → 同一段话显示两遍。实测（本引擎）：
``6 个 partial 累加 155 字 + 1 个 final 155 字 = 310 字``，正好 2 倍。

修复：见 ``_dedupe_final_text`` —— final 文本与「本轮已流式输出」完全相同时
清空其文本，但**完整保留元数据**（usage_metadata / finish_reason / actions /
function_call）。三条硬约束决定了不能直接丢弃 final 事件：

1. 工具调用只在 ``partial=False`` 事件上执行
   （``google/adk/flows/llm_flows/base_llm_flow.py``：partial 的 function_call
   只是碎片，丢弃 final 会导致工具失效）
2. final 携带计费与状态元数据，以及 ``requested_auth_configs`` 授权请求
3. session 落库发生在 runner 内部（``_process_and_append_event`` 先 append
   再 yield），因此这里的清空只影响**下发给前端的事件视图**，不影响会话历史

**实测去重不影响流式**：partial 事件原样转发、零缓冲；去重只作用于已经到达
末尾的 final 事件，因此不增加任何等待。

## 回滚开关

- ``AGENT_ENABLE_SSE_STREAMING=0``：回退模板默认行为（非流式）
- ``AGENT_DEDUPE_SSE_TEXT=0``：关闭去重（恢复 GE 原生两遍，便于对照）
"""

import copy
import json
import os
from typing import Any

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


def _dedupe_enabled() -> bool:
    """重复文本抑制开关（默认开）。置 0 可恢复两遍行为用于对照。"""
    return os.getenv("AGENT_DEDUPE_SSE_TEXT", "1") == "1"


def _event_text(event: Any) -> str:
    """取事件 content 里的纯文本（忽略 thought_signature 等空 part）。"""
    content = getattr(event, "content", None)
    if content is None:
        return ""
    return "".join(
        (p.text or "") for p in (getattr(content, "parts", None) or [])
        if getattr(p, "text", None)
    )


def _compare_streamed(already: str, final: str) -> str:
    """比对「本轮已流式输出的文本」与「final 聚合文本」。

    - ``exact``：完全一致 → final 是纯重复，可清空文本
    - ``prefix``：final 以已输出内容开头且更长 → 分段漏了尾部，需补发 tail
    - ``""``：不重复 → final 原样下发（例如此轮无流式分段）

    ⚠️ 刻意**不使用** ``final in already`` 子串判断：多轮场景下 final 常比
    上一轮短（轮1「搜索完成」、轮2「完成」），子串判断会把真实短回复误判
    为重复而整段吞掉。实测该缺陷会导致「执行了工具却看不到回复」。
    """
    a, f = already.strip(), final.strip()
    if not a or not f:
        return ""
    if a == f:
        return "exact"
    if f.startswith(a):
        return "prefix"
    return ""


def _strip_text_keep_metadata(event: Any) -> Any:
    """清空事件文本但保留全部功能载荷与元数据（深拷贝，不污染上游对象）。

    移除纯文本 part（含 thought_signature 空 part，避免前端渲染空段落），
    保留 function_call / function_response / inline_data part 以及
    usage_metadata / finish_reason / actions / author / id 等字段。
    """
    try:
        stripped = event.model_copy(deep=True)
    except Exception:
        return event
    content = getattr(stripped, "content", None)
    if content is None:
        return stripped
    parts = list(getattr(content, "parts", None) or [])
    kept = [
        p for p in parts
        if not (
            getattr(p, "function_call", None) is None
            and getattr(p, "function_response", None) is None
            and getattr(p, "inline_data", None) is None
        )
    ]
    try:
        if kept:
            content.parts = kept
        else:
            stripped.content = None
    except Exception:
        return event
    return stripped


def _replace_text_keep_metadata(event: Any, new_text: str) -> Any:
    """把事件文本替换为 new_text，保留函数调用等非文本 part 与元数据。

    用于 final 比已流式内容多出尾部时只补发差额，避免正文缺字。
    """
    try:
        replaced = event.model_copy(deep=True)
    except Exception:
        return event
    content = getattr(replaced, "content", None)
    if content is None or not new_text:
        return replaced
    parts = list(getattr(content, "parts", None) or [])
    out: list[Any] = []
    text_placed = False
    for part in parts:
        is_text_only = (
            getattr(part, "function_call", None) is None
            and getattr(part, "function_response", None) is None
            and getattr(part, "inline_data", None) is None
        )
        if is_text_only:
            if not text_placed:
                out.append(types.Part(text=new_text))
                text_placed = True
            continue
        out.append(part)
    if not text_placed:
        out.insert(0, types.Part(text=new_text))
    try:
        content.parts = out
    except Exception:
        return event
    return replaced


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

        # SSE 会同时产出 partial 分段与 final 聚合事件（文本相同），GE 前端
        # 会把两者都渲染 → 同一段话显示两遍。此处按轮累积已输出文本用于比对。
        dedupe = _dedupe_enabled()
        streamed_text: list[str] = []

        try:
            async for event in runner.run_async(
                user_id=request.user_id,
                session_id=session.id,
                new_message=message_for_agent,
                state_delta=state_delta,
                run_config=run_config,
            ):
                event = self._dedupe_final_text(event, streamed_text) if dedupe else event
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

    @staticmethod
    def _dedupe_final_text(event: Any, streamed_text: list[str]) -> Any:
        """消除 final 聚合事件与流式分段的重复文本。

        partial 分段**原样返回**（打字机效果不受影响，零缓冲）；只在
        ``partial=False`` 的聚合事件上比对文本：

        - 完全重复 → 清空文本、保留元数据
        - final 多出尾部 → 只补发尾部差额，避免正文缺字
        - 不重复（本轮无分段，如工具后直出结果）→ 原样下发

        每轮结束即重置缓冲，否则下一轮的短回复会被本轮长文本误判为重复。
        """
        if bool(getattr(event, "partial", False)):
            text = _event_text(event)
            if text:
                streamed_text.append(text)
            return event

        text = _event_text(event)
        if not text:
            streamed_text.clear()
            return event

        already = "".join(streamed_text)
        kind = _compare_streamed(already, text)
        try:
            if kind == "exact":
                stripped = _strip_text_keep_metadata(event)
            elif kind == "prefix":
                stripped = _replace_text_keep_metadata(
                    event, text.strip()[len(already.strip()):]
                )
            else:
                stripped = event
        finally:
            streamed_text.clear()
        return stripped
