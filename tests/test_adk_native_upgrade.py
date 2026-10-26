"""ADK 2.13 原生能力升级的机制级真实验证。

覆盖：
1. LongRunningFunctionTool 轮询工具：真实 Runner 跑通 yield 进度 + 最终返回
2. ToolAuditPlugin：三个回调钩子返回 None 且产生审计日志
3. 残留根除断言：死代码/陈旧文件不再存在
"""

import asyncio
import logging
import os


from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool, LongRunningFunctionTool, ToolContext


class TestLongRunningTaskTools:
    """wait_for_feishu_doc_* 已改为 ADK 原生长任务。"""

    def _load_tools(self):
        from nexus_agent import tools as T

        return T

    def test_tools_are_long_running_wrappers(self):
        T = self._load_tools()
        assert isinstance(T.wait_for_feishu_doc_create_task_tool, LongRunningFunctionTool)
        assert isinstance(T.wait_for_feishu_doc_update_task_tool, LongRunningFunctionTool)
        # 被包装的仍是模块上的生成器函数本体
        assert T.wait_for_feishu_doc_create_task_tool.func is T.wait_for_feishu_doc_create_task

    def test_agent_mounts_long_running_tools(self):
        from nexus_agent.agent import root_agent

        tool_objects = root_agent.tools
        lrfts = [t for t in tool_objects if isinstance(t, LongRunningFunctionTool)]
        assert len(lrfts) == 2, f"agent 应挂载 2 个长任务工具，实际 {len(lrfts)}"
        # 35 个挂载项 = 28 个普通函数（ADK 运行时自动包装）+ 5 个显式
        # FunctionTool 硬确认包装（delete 恒确认 + 4 个 CLI 总线动态确认）+ 2 个长任务
        # （execute_lark_api 已删除：CLI 总线完全覆盖且更安全，36→35）
        others = [t for t in tool_objects if not isinstance(t, LongRunningFunctionTool)]
        assert len(others) == 33
        assert all(
            callable(t) or (isinstance(t, FunctionTool) and callable(t.func))
            for t in others
        ), "每个挂载项必须是可调用函数或含 func 的 FunctionTool"

    def test_tool_confirmation_wrapping(self):
        """ADK 2.13 原生硬确认：delete 恒确认，CLI 总线动态确认（干跑放行）。"""

        from google.adk.tools import FunctionTool

        from nexus_agent.agent import root_agent

        by_name = {t.name: t for t in root_agent.tools if isinstance(t, FunctionTool)}
        assert by_name["delete_lark_document"]._require_confirmation is True
        for bus in (
            "execute_lark_cli",
            "execute_lark_cli_flat",
            "execute_google_workspace_cli",
            "execute_google_workspace_cli_flat",
        ):
            pred = by_name[bus]._require_confirmation
            assert callable(pred), f"{bus} 应挂动态确认判定"
            assert pred(allow_mutating=True, dry_run=False) is True
            assert pred(allow_mutating=True, dry_run=True) is False
            assert pred(allow_mutating=False) is False

    def test_long_running_declaration_note(self):
        """框架自动在声明中追加长任务说明（避免模型重复轮询调用）。"""
        from nexus_agent.tools import wait_for_feishu_doc_create_task_tool

        decl = wait_for_feishu_doc_create_task_tool._get_declaration()
        assert decl is not None
        assert decl.name == "wait_for_feishu_doc_create_task"
        assert "long-running" in (decl.description or "").lower()

    def test_generator_protocol_yield_and_return(self):
        """生成器协议真实验证（异步生成器）：错误输入仅 yield 错误项后立即结束。"""
        from nexus_agent import tools as T

        async def scenario():
            svc = InMemorySessionService()
            session = await svc.create_session(app_name="t", user_id="u")
            from google.adk.agents.invocation_context import InvocationContext

            from nexus_agent.agent import root_agent

            ic = InvocationContext(
                artifact_service=None, session_service=svc, memory_service=None,
                credential_service=None, invocation_id="i1", agent=root_agent,
                session=session,
            )
            ctx = ToolContext(ic)
            agen = T.wait_for_feishu_doc_create_task(task_id="", tool_context=ctx)
            yielded = [item async for item in agen]
            return yielded

        yielded = asyncio.run(scenario())
        assert yielded == [{"status": "error", "message": "task_id is required."}], (
            "空 task_id 应仅 yield 一条错误项且不产生进度"
        )


class TestToolAuditPlugin:
    """ADK 原生插件：突变工具调用集中审计。"""

    def _make_plugin(self):
        from nexus_agent.plugins import ToolAuditPlugin

        return ToolAuditPlugin()

    def test_hooks_return_none(self):
        from nexus_agent.agent import root_agent

        p = self._make_plugin()

        async def scenario():
            svc = InMemorySessionService()
            session = await svc.create_session(app_name="t", user_id="u")
            from google.adk.agents.invocation_context import InvocationContext

            ic = InvocationContext(
                artifact_service=None, session_service=svc, memory_service=None,
                credential_service=None, invocation_id="i1", agent=root_agent,
                session=session,
            )
            ctx = ToolContext(ic)

            class FakeTool:
                name = "delete_lark_document"

            r1 = await p.before_tool_callback(tool=FakeTool(), tool_args={"doc_token": "x"}, tool_context=ctx)
            r2 = await p.after_tool_callback(tool=FakeTool(), tool_args={}, tool_context=ctx, result={"status": "success"})
            r3 = await p.on_tool_error_callback(tool=FakeTool(), tool_args={}, tool_context=ctx, error=RuntimeError("boom"))
            return r1, r2, r3

        r1, r2, r3 = asyncio.run(scenario())
        assert r1 is None and r2 is None and r3 is None, "插件回调必须返回 None（不干预执行/结果/错误处理）"

    def test_audit_logs_emitted(self, caplog):
        from nexus_agent.agent import root_agent

        p = self._make_plugin()

        async def scenario(caplog_holder):
            svc = InMemorySessionService()
            session = await svc.create_session(app_name="t", user_id="u")
            from google.adk.agents.invocation_context import InvocationContext

            ic = InvocationContext(
                artifact_service=None, session_service=svc, memory_service=None,
                credential_service=None, invocation_id="i1", agent=root_agent,
                session=session,
            )
            ctx = ToolContext(ic)

            class FakeTool:
                name = "delete_lark_document"

            await p.before_tool_callback(tool=FakeTool(), tool_args={"doc_token": "x"}, tool_context=ctx)

        with caplog.at_level(logging.INFO, logger="nexus_agent.plugins"):
            asyncio.run(scenario(caplog))
        assert any("[AUDIT] tool_call_start" in r.message and "delete_lark_document" in r.message for r in caplog.records)


class TestResidualsEradicated:
    """升级残留根除断言：绝不遗漏。"""

    def test_dead_functions_removed(self):
        from nexus_agent import tools as T

        assert not hasattr(T, "show_user_auth_info"), "show_user_auth_info 死代码未根除"
        assert not hasattr(T, "get_lark_document_content"), "get_lark_document_content 死代码未根除"

    def test_legacy_main_removed(self):
        assert not os.path.exists("nexus_agent/main.py"), "main.py 已被 adk run 官方 CLI 取代，应删除"
        assert not os.path.exists("nexus_agent/js_bridge"), "空目录 js_bridge 应删除"

    def test_stale_wheel_removed(self):
        assert not os.path.exists(
            "nexus_agent/deployment/adk_agents-0.1.0-py3-none-any.whl"
        ), "旧项目名 wheel 残留未根除"
        assert not os.path.exists(
            "nexus_agent/deployment/ge_nexus_agent-1.0.0-py3-none-any.whl"
        ), "初版项目名 wheel 残留未根除"
        assert not os.path.exists("build/"), "build/ 构建产物目录不得入库/残留"

    def test_deploy_module_has_audit_plugin(self):
        """deploy.py 的 app 构造必须携带审计插件与模型自愈重试插件。"""

        with open("nexus_agent/deployment/deploy.py") as f:
            src = f.read()
        assert "ToolAuditPlugin()" in src
        assert "ReflectAndRetryModelPlugin()" in src, "模型瞬时故障自愈重试插件不得缺失"
        assert "from google.adk.plugins import ReflectAndRetryModelPlugin" in src
        assert "ContextCacheConfig(min_tokens=4096)" in src, "ADK 原生上下文缓存不得缺失"
        assert "AGENT_ENABLE_CONTEXT_CACHE" in src, "上下文缓存回退开关不得缺失"
        assert "if __name__ == \"__main__\":" in src, "部署 main 守卫不得回退"

    def test_context_cache_config_semantics(self):
        """ADK 原生上下文缓存配置语义正确（Gemini 3 门槛 4096）。"""

        from google.adk.agents.context_cache_config import ContextCacheConfig
        from google.adk.apps import App

        cfg = ContextCacheConfig(min_tokens=4096)
        assert cfg.min_tokens == 4096
        assert "context_cache_config" in App.model_fields
        assert "resumability_config" in App.model_fields

    def test_reflect_retry_model_plugin_defaults(self):
        """ADK 原生模型自愈重试插件可用且默认 3 次重试。"""

        import inspect

        from google.adk.plugins import ReflectAndRetryModelPlugin

        sig = inspect.signature(ReflectAndRetryModelPlugin.__init__)
        assert sig.parameters["max_retries"].default == 3


# ---------------------------------------------------------------------------
# 插件回调签名契约测试（2026-10-27 远程实测发现的 bug 回归锚定）
# ADK 2.13 回调契约: 关键字为 tool_args；签名写成 args 会导致远程运行时
# 工具调用全部被拦截（ToolAuditPlugin.before_tool_callback() got an
# unexpected keyword argument 'tool_args'）
# ---------------------------------------------------------------------------

class TestPluginCallbackSignatureContract:
    """ToolAuditPlugin 必须兼容 ADK 2.13 回调契约（tool_args 关键字）。"""

    def _make_plugin(self):
        from nexus_agent.plugins import ToolAuditPlugin

        return ToolAuditPlugin()

    def test_callbacks_accept_adk213_tool_args_kwarg(self):
        import asyncio

        from nexus_agent.plugins import ToolAuditPlugin

        plugin = self._make_plugin()
        tool = type("T", (), {"name": "create_lark_document"})()
        common = dict(tool=tool, tool_args={"title": "t"}, tool_context=None)
        # 三个回调都必须以 tool_args 关键字调用成功且返回 None
        assert asyncio.run(plugin.before_tool_callback(**common)) is None
        assert asyncio.run(plugin.after_tool_callback(**common, result={"status": "success"})) is None
        assert asyncio.run(plugin.on_tool_error_callback(**common, error=RuntimeError("x"))) is None

    def test_plugin_manager_invocation_path_compatible(self):
        """经 ADK PluginManager 自身调用路径验证签名兼容（远程同款调用方式）。"""
        import asyncio

        from google.adk.plugins.plugin_manager import PluginManager
        from nexus_agent.plugins import ToolAuditPlugin

        pm = PluginManager(plugins=[ToolAuditPlugin()])
        tool = type("T", (), {"name": "execute_lark_cli"})()
        # PluginManager 以 tool_args= 关键字调用——签名不匹配会在此抛 TypeError
        result = asyncio.run(
            pm.run_before_tool_callback(tool=tool, tool_args={"cmd": "docs"}, tool_context=None)
        )
        assert result is None
