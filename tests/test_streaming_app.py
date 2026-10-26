"""StreamingAdkApp 单元测试（对齐 google-cloud-aiplatform==2.6.0 模板）。

验证要点：
1. GE 调用链路（streaming_agent_run_with_events）恒注入 StreamingMode.SSE；
2. 模板 2.6.0 新增逻辑完整保留：authorizations → temp: state_delta、
   labels 透传到 RunConfig（且与 SSE 共存）；
3. 回滚开关 AGENT_ENABLE_SSE_STREAMING=0 时委托模板默认行为（无 SSE）；
4. 会话获取、消息透传、事件产出与模板语义一致。

风格说明：与既有测试保持一致，使用 asyncio.run() 驱动协程，
不引入 pytest-asyncio 等新依赖。
"""

import asyncio
import json
from types import SimpleNamespace

from google.adk.agents.run_config import RunConfig, StreamingMode

from nexus_agent.deployement.streaming_app import StreamingAdkApp


def _make_request_json(user_id="u1", session_id="s1", extra=None):
    """构造与 GE :streamQuery 请求结构一致的 payload。"""
    payload = {
        "userId": user_id,
        "sessionId": session_id,
        "message": {"role": "user", "parts": [{"text": "你好"}]},
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload)


class _FakeRunner:
    """捕获 run_async 入参的假 Runner。"""

    def __init__(self):
        self.captured_kwargs = None
        self._events = [SimpleNamespace(text="事件A"), SimpleNamespace(text="事件B")]

    async def run_async(self, **kwargs):
        self.captured_kwargs = kwargs
        for event in self._events:
            yield event


class _FakeSessionService:
    """返回既有会话的假 SessionService（删除操作视为异常路径）。"""

    def __init__(self):
        self.get_called = False

    async def get_session(self, **kwargs):
        self.get_called = True
        return SimpleNamespace(id=kwargs["session_id"])

    async def delete_session(self, **kwargs):
        raise AssertionError("既有会话不应被删除")


def _make_app(runner, session_service):
    """构造跳过 __init__（避免 vertexai 初始化）的 StreamingAdkApp 实例。"""
    app = object.__new__(StreamingAdkApp)
    app._tmpl_attrs = {
        "app": None,
        "app_name": "test-app",
        "runner": runner,
        "session_service": session_service,
        "artifact_service": object(),  # 仅透传；无 artifacts 时 _save_artifacts 为空操作
    }

    async def _fake_convert(self, **kwargs):
        return kwargs["events"][0]

    app._convert_response_events = _fake_convert.__get__(app, StreamingAdkApp)
    app._tracing_enabled = lambda: False
    app._telemetry_enabled = lambda: False
    return app


def test_sse_run_config_injected():
    """默认行为：runner.run_async 必须收到 StreamingMode.SSE 配置。"""
    runner, session_service = _FakeRunner(), _FakeSessionService()
    app = _make_app(runner, session_service)

    events = asyncio.run(
        _collect_all(app.streaming_agent_run_with_events(_make_request_json()))
    )

    assert runner.captured_kwargs is not None
    run_config = runner.captured_kwargs["run_config"]
    assert isinstance(run_config, RunConfig)
    assert run_config.streaming_mode == StreamingMode.SSE
    # 无 labels 时不应携带 labels（模板语义）
    assert run_config.labels is None
    # 无 authorizations 时 state_delta 为 None（模板语义）
    assert runner.captured_kwargs["state_delta"] is None
    assert session_service.get_called is True
    assert runner.captured_kwargs["user_id"] == "u1"
    assert runner.captured_kwargs["session_id"] == "s1"
    assert runner.captured_kwargs["new_message"].parts[0].text == "你好"
    assert len(events) == 2  # 事件逐一产出（转换后）


def test_labels_and_authorizations_forwarded_with_sse():
    """模板 2.6.0 新增逻辑：labels 透传进 RunConfig 且与 SSE 共存；
    authorizations 转为 temp: state_delta（OAuth 用户态令牌）。"""
    runner, session_service = _FakeRunner(), _FakeSessionService()
    app = _make_app(runner, session_service)

    payload = _make_request_json(
        extra={
            "labels": {"env": "test"},
            "authorizations": {"auth-id-1": {"access_token": "tok-abc"}},
        }
    )
    asyncio.run(_collect_all(app.streaming_agent_run_with_events(payload)))

    run_config = runner.captured_kwargs["run_config"]
    assert isinstance(run_config, RunConfig)
    assert run_config.streaming_mode == StreamingMode.SSE
    assert run_config.labels == {"env": "test"}
    assert runner.captured_kwargs["state_delta"] == {"temp:auth-id-1": "tok-abc"}


def test_kill_switch_falls_back_to_template(monkeypatch):
    """回滚开关：AGENT_ENABLE_SSE_STREAMING=0 时委托模板默认行为（run_config 无 SSE）。"""
    monkeypatch.setenv("AGENT_ENABLE_SSE_STREAMING", "0")
    runner, session_service = _FakeRunner(), _FakeSessionService()
    app = _make_app(runner, session_service)

    events = asyncio.run(
        _collect_all(app.streaming_agent_run_with_events(_make_request_json()))
    )

    # 模板 2.6.0 默认：无 labels 时 run_config=None（即不启用 SSE）
    assert runner.captured_kwargs["run_config"] is None
    assert len(events) == 2


def test_missing_session_raises_runtime_error():
    """会话初始化失败时应抛出 RuntimeError（与模板语义一致）。"""

    class _FailingSessionService:
        async def get_session(self, **kwargs):
            return None

        async def create_session(self, **kwargs):
            return None

    app = _make_app(_FakeRunner(), _FailingSessionService())
    try:
        asyncio.run(
            _collect_all(app.streaming_agent_run_with_events(
                _make_request_json(session_id="missing")))
        )
        raised = False
    except RuntimeError:
        raised = True
    assert raised is True


def test_subclass_covers_ge_entrypoint():
    """子类必须覆盖 GE 固定调用的入口方法，且仍为 AdkApp 子类。"""
    from vertexai.agent_engines import AdkApp

    assert issubclass(StreamingAdkApp, AdkApp)
    assert "streaming_agent_run_with_events" in StreamingAdkApp.__dict__


async def _collect_all(agen):
    return [event async for event in agen]
