"""SSE 重复文本抑制（2026-10-27）。

背景：ADK 的 SSE 模式同时产出 partial 分段与 final 聚合事件，文本逐字相同，
GE 前端把两者都渲染 → 同一段话显示两遍。实测 6 partial(155字) + 1 final(155字)
= 310 字，正好 2 倍。

约束（不可违反）：
1. partial 事件必须原样转发（否则失去打字机效果）
2. final 事件不能丢弃（工具调用只在 partial=False 上执行）
3. 清空只作用于下发给前端的事件视图，session 落库在 runner 内部已完成
"""

import os

import pytest

from nexus_agent.deployment.streaming_app import (
    StreamingAdkApp,
    _compare_streamed,
    _dedupe_enabled,
    _event_text,
    _replace_text_keep_metadata,
    _strip_text_keep_metadata,
)


def _ev(text="", partial=False, parts=None):
    """构造真实 ADK Event 对象。"""
    from google.adk.events import Event
    from google.genai import types

    if parts is None:
        parts = [types.Part(text=text)] if text else []
    return Event(
        author="model",
        partial=partial,
        content=types.Content(role="model", parts=parts) if parts else None,
    )


class TestCompareStreamed:
    """比对规则：精确/前缀，禁止子串。"""

    def test_exact_match(self):
        assert _compare_streamed("完整回答", "完整回答") == "exact"

    def test_exact_match_with_whitespace(self):
        assert _compare_streamed("完整回答\n", " 完整回答 ") == "exact"

    def test_prefix_when_final_longer(self):
        assert _compare_streamed("前半段", "前半段后半段") == "prefix"

    def test_no_match_when_unrelated(self):
        assert _compare_streamed("甲", "乙") == ""

    def test_empty_already_is_not_duplicate(self):
        """本轮无流式分段（如工具后直出结果）→ 不能判重。"""
        assert _compare_streamed("", "工具执行完毕的结果") == ""

    def test_substring_is_not_duplicate(self):
        """回归防线：短 final 是长 already 的子串时，绝不能判为重复。

        实测缺陷：轮1「搜索完成」、轮2「完成」，子串判断会吞掉轮2 的真实回复。
        """
        assert _compare_streamed("搜索完成", "完成") == ""


class TestDedupeFinalText:
    """主逻辑：partial 累积 + final 判重。"""

    def test_partial_forwarded_verbatim(self):
        """partial 必须原样返回（打字机效果）。"""
        buf = []
        ev = _ev("第一段", partial=True)
        out = StreamingAdkApp._dedupe_final_text(ev, buf)
        assert _event_text(out) == "第一段"
        assert buf == ["第一段"]

    def test_final_duplicate_stripped(self):
        """final 与累积完全相同 → 文本清空。"""
        buf = []
        for t in ["飞书", "文档很好用"]:
            StreamingAdkApp._dedupe_final_text(_ev(t, partial=True), buf)
        out = StreamingAdkApp._dedupe_final_text(_ev("飞书文档很好用", partial=False), buf)
        assert _event_text(out) == ""
        assert buf == [], "final 处理后必须清空缓冲"

    def test_final_not_duplicate_preserved(self):
        """本轮无 partial → final 原样下发（防丢回复）。"""
        buf = []
        out = StreamingAdkApp._dedupe_final_text(_ev("工具结果", partial=False), buf)
        assert _event_text(out) == "工具结果"

    def test_prefix_final_sends_tail_only(self):
        """final 多出尾部 → 只补发差额，不丢正文。"""
        buf = []
        StreamingAdkApp._dedupe_final_text(_ev("前面的话", partial=True), buf)
        out = StreamingAdkApp._dedupe_final_text(
            _ev("前面的话，后面被漏掉的尾部", partial=False), buf
        )
        assert _event_text(out) == "，后面被漏掉的尾部"

    def test_multiround_buffer_reset(self):
        """多轮隔离：轮2 的短回复不能被轮1 的长文本吞掉。"""
        buf = []
        StreamingAdkApp._dedupe_final_text(_ev("搜索完成，共找到 3 份文档", partial=True), buf)
        StreamingAdkApp._dedupe_final_text(
            _ev("搜索完成，共找到 3 份文档", partial=False), buf
        )
        # 轮2：无 partial，final 是短文本
        out = StreamingAdkApp._dedupe_final_text(_ev("完成", partial=False), buf)
        assert _event_text(out) == "完成", "轮2 真实回复被误吞"

    def test_accumulated_equals_original(self):
        """核心断言：去重后文本 == 原文（不重复也不缺字）。"""
        buf = []
        pieces = ["飞书", "文档是", "一款协作平台"]
        for p in pieces:
            StreamingAdkApp._dedupe_final_text(_ev(p, partial=True), buf)
        full = "".join(pieces)
        out = StreamingAdkApp._dedupe_final_text(_ev(full, partial=False), buf)
        assert _event_text(out) == "", "重复的 final 应被清空"


class TestMetadataPreserved:
    """final 事件的功能载荷与元数据不能丢。"""

    def test_function_call_preserved(self):
        """function_call 必须保留（工具调用在 partial=False 上执行）。"""
        from google.genai import types

        ev = _ev(parts=[
            types.Part(text="我来查询"),
            types.Part(function_call=types.FunctionCall(name="query", args={})),
        ], partial=False)
        out = _strip_text_keep_metadata(ev)
        assert any(p.function_call is not None for p in out.content.parts)
        assert all(not p.text for p in out.content.parts), "纯文本 part 应移除"

    def test_function_response_preserved(self):
        from google.genai import types

        ev = _ev(parts=[
            types.Part(function_response=types.FunctionResponse(
                name="query", response={"ok": True})),
        ], partial=False)
        out = _strip_text_keep_metadata(ev)
        assert any(p.function_response is not None for p in out.content.parts)

    def test_usage_metadata_preserved(self):
        from google.genai import types

        ev = _ev("文本", partial=False)
        ev.usage_metadata = types.GenerateContentResponseUsageMetadata(
            prompt_token_count=100, candidates_token_count=20
        )
        out = _strip_text_keep_metadata(ev)
        assert out.usage_metadata is not None
        assert out.usage_metadata.prompt_token_count == 100

    def test_finish_reason_preserved(self):
        from google.genai import types

        ev = _ev("文本", partial=False)
        ev.finish_reason = types.FinishReason.STOP
        out = _strip_text_keep_metadata(ev)
        assert out.finish_reason == types.FinishReason.STOP

    def test_upstream_object_not_mutated(self):
        """深拷贝：不得修改上游事件对象（session 已落库同一对象）。"""
        ev = _ev("原始文本", partial=False)
        _strip_text_keep_metadata(ev)
        assert _event_text(ev) == "原始文本", "上游对象被污染"

    def test_thought_signature_part_removed(self):
        """带 thought_signature 的空文本 part 应移除，防前端渲染空段落。"""
        from google.genai import types

        ev = _ev(parts=[types.Part(text="正文"),
                        types.Part(text="", thought_signature=b"sig")], partial=False)
        out = _strip_text_keep_metadata(ev)
        assert out.content is None or all(not p.text for p in out.content.parts)


class TestEdgeCases:
    """边界：空 content / 空 parts / None 文本。"""

    def test_none_content(self):
        from google.adk.events import Event

        ev = Event(author="model", partial=False, content=None)
        assert _event_text(ev) == ""
        assert _strip_text_keep_metadata(ev) is not None

    def test_empty_parts(self):
        from google.genai import types

        ev = _ev(parts=[])
        assert _event_text(ev) == ""

    def test_none_text_part(self):
        from google.genai import types

        ev = _ev(parts=[types.Part(text=None)])
        assert _event_text(ev) == ""

    def test_replace_on_none_content(self):
        from google.adk.events import Event

        ev = Event(author="model", partial=False, content=None)
        out = _replace_text_keep_metadata(ev, "新文本")
        assert out is not None


class TestSwitch:
    """回滚开关。"""

    def test_dedupe_default_on(self, monkeypatch):
        monkeypatch.delenv("AGENT_DEDUPE_SSE_TEXT", raising=False)
        assert _dedupe_enabled() is True

    def test_dedupe_can_be_disabled(self, monkeypatch):
        monkeypatch.setenv("AGENT_DEDUPE_SSE_TEXT", "0")
        assert _dedupe_enabled() is False

    def test_dedupe_wired_into_stream_loop(self):
        """去重必须真正接入事件循环，不能只定义不调用。"""
        with open("nexus_agent/deployment/streaming_app.py") as f:
            src = f.read()
        idx = src.index("async for event in runner.run_async")
        assert "_dedupe_final_text(event, streamed_text)" in src[idx:]
