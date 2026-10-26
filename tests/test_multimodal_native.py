"""ADK 2.x 原生多模态机制测试。

ADK 2.13 起原生支持工具返回值中携带 types.Part（框架自动提取到
FunctionResponse.parts，见 google.adk.flows.llm_flows.tools._caller）。
本测试验证本项目工具的返回格式与该原生机制完全兼容，
取代旧版 monkey-patch（callbacks.py 已删除）。
"""

import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from google.adk.flows.llm_flows.tools._caller import _extract_multimodal_parts
from google.genai import types

from nexus_agent import tools as nexus_tools


def test_native_extraction_handles_project_media_format():
    """工具返回 dict 中的 types.Part(inline_data) 应被 ADK 原生提取为 FunctionResponsePart。"""
    result = {
        "status": "success",
        "text_content": "以下是文档的 PDF 视图。",
        "pdf_media": [
            types.Part(
                inline_data=types.Blob(data=b"%PDF-1.4 test", mime_type="application/pdf")
            )
        ],
    }

    remaining, parts = _extract_multimodal_parts(result)

    assert parts is not None and len(parts) == 1
    assert parts[0].inline_data.mime_type == "application/pdf"
    # 媒体数据应从 JSON response 中移除，保持可序列化
    assert "pdf_media" not in remaining
    assert remaining["status"] == "success"


def test_native_extraction_passes_plain_results_through():
    """不含媒体的普通返回值应原样透传，不产生任何 parts。"""
    result = {"status": "success", "response": {"$ref": "File"}}

    remaining, parts = _extract_multimodal_parts(result)

    assert parts is None
    assert remaining == result


def test_native_extraction_handles_multiple_images():
    """rich_content 工具的多图返回（media 列表）应全部提取。"""
    result = {
        "status": "success",
        "text_content": "[图片1] [图片2]",
        "image_count": 2,
        "media": [
            types.Part.from_bytes(data=b"png1", mime_type="image/png"),
            types.Part.from_bytes(data=b"jpg2", mime_type="image/jpeg"),
        ],
    }

    remaining, parts = _extract_multimodal_parts(result)

    assert parts is not None and len(parts) == 2
    assert {p.inline_data.mime_type for p in parts} == {"image/png", "image/jpeg"}
    assert "media" not in remaining


def test_project_tools_no_longer_emit_legacy_key():
    """根除验证：项目源码中不得再出现旧版私有键 __multimodal_parts__。"""
    import pathlib

    src_dir = pathlib.Path(nexus_tools.__file__).parent
    offenders = []
    for py in src_dir.rglob("*.py"):
        if "__multimodal_parts__" in py.read_text(encoding="utf-8"):
            offenders.append(str(py))
    assert offenders == [], f"旧版多模态私有键残留于: {offenders}"
