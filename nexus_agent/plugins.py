"""ADK 原生 Plugin：工具调用集中审计。

基于 ADK 2.x 原生 Plugin 机制（google.adk.plugins.BasePlugin），
替代散落在各工具函数内的日志拼装，实现突变类操作的统一审计：

- before_tool_callback：记录工具调用开始（含脱敏参数摘要）
- after_tool_callback：记录调用结果状态
- on_tool_error_callback：记录调用异常

接入点：StreamingAdkApp（继承 aiplatform 2.6.0 AdkApp，原生支持
plugins 参数），随 Agent Engine 部署在 GE 调用链路上生效。
"""

import json
import logging

from google.adk.plugins import BasePlugin
from google.adk.tools import BaseTool, ToolContext

logger = logging.getLogger(__name__)

# 审计关注的突变类工具（写操作优先级最高；只记审计事件，不干预执行）
_MUTATING_TOOLS = {
    "create_lark_document",
    "update_lark_document",
    "delete_lark_document",
    "create_google_doc_with_text",
    "append_google_doc_text",
    "create_google_sheet_with_rows",
    "append_google_sheet_rows",
    "create_google_calendar_event",
    "save_ai_output_to_feishu_doc",
    "save_ai_output_to_existing_feishu_doc",
    "feishu_mcp_create_doc",
    "feishu_mcp_update_doc",
    "execute_lark_cli",
    "execute_lark_cli_flat",
    "execute_google_workspace_cli",
    "execute_google_workspace_cli_flat",
}


def _args_summary(args: dict, limit: int = 300) -> str:
    """参数摘要（截断，避免审计日志被超大 markdown 刷爆）。"""
    try:
        return json.dumps(args, ensure_ascii=False, default=str)[:limit]
    except Exception:
        return str(args)[:limit]


class ToolAuditPlugin(BasePlugin):
    """集中审计工具调用全生命周期，重点覆盖突变类写操作。"""

    def __init__(self, name: str = "tool_audit_plugin"):
        super().__init__(name=name)

    async def before_tool_callback(
        self, *, tool: BaseTool, args: dict, tool_context: ToolContext
    ):
        """工具执行前记录审计事件。必须返回 None 以不干预执行。"""
        name = getattr(tool, "name", tool.__class__.__name__)
        if name in _MUTATING_TOOLS:
            logger.info(
                "[AUDIT] tool_call_start "
                f"tool={name} user={getattr(tool_context, 'user_id', '?')} "
                f"session={getattr(tool_context, 'session_id', '?')} "
                f"args={_args_summary(args or {})}"
            )
        return None

    async def after_tool_callback(
        self,
        *,
        tool: BaseTool,
        args: dict,
        tool_context: ToolContext,
        result: dict,
    ):
        """工具执行后记录结果状态。必须返回 None 以不替换结果。"""
        name = getattr(tool, "name", tool.__class__.__name__)
        if name in _MUTATING_TOOLS:
            status = result.get("status", "?") if isinstance(result, dict) else "?"
            logger.info(
                f"[AUDIT] tool_call_end tool={name} status={status} "
                f"session={getattr(tool_context, 'session_id', '?')}"
            )
        return None

    async def on_tool_error_callback(
        self,
        *,
        tool: BaseTool,
        args: dict,
        tool_context: ToolContext,
        error: Exception,
    ):
        """工具异常时记录审计事件。返回 None 使用默认错误处理。"""
        name = getattr(tool, "name", tool.__class__.__name__)
        logger.error(
            f"[AUDIT] tool_call_error tool={name} "
            f"error={type(error).__name__}: {str(error)[:200]}"
        )
        return None
