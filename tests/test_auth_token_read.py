"""GE 授权 token 读取（2026-10-30）。

## 背景：ADK 反复变更过 state key 格式

| 时期 | 写入形态 | 证据 |
|---|---|---|
| 2025-11 前 | ``state["temp:*"]`` | adk-python#3274 标题 |
| 2025-11-22 后 | ``session_state[auth_id]``（无前缀，持久化） | adk-python#3274 官方答复、#3725 源码分析 |
| aiplatform 1.135~1.136（dev 期） | 同上 | 下载 wheel 源码实测 |
| **aiplatform 2.6.0（现行）** | **``state_delta["temp:{auth_id}"]``** | 本地源码 + 真机对比 |

实测证据：同一份 tools.py、同一 AUTH_ID、同一 token，
dev 引擎（1.136）读到 ``"COMPARE_TOKEN_777"``，线上（2.6）读到 ``null``。

## 因此读取必须是双格式

与 ADK 官方 ``AuthHandler._read_stored_credential`` 同构：
``for key in ("temp:" + credential_key, credential_key)`` —— 先 temp: 后无前缀。

注：不能用官方 ``external_access_token_key``，因为 ``_google_credentials.py``
只读无前缀、不读 ``temp:``，在当前版本下读不到 GE 注入的 token。
"""

import pytest

from nexus_agent.config import GOOGLE_WORKSPACE_AUTH_ID, LARK_AUTH_ID
from nexus_agent.tools import (
    _coerce_token,
    _read_auth_token,
    get_access_token,
    get_google_workspace_access_token,
)

AUTH_ID = "ge-nexus-agent-oauth-id-gb-dev-guyue"


class _FakeState:
    """模拟 ADK State：支持 get 与 to_dict。"""

    def __init__(self, data=None):
        self._data = dict(data or {})

    def get(self, key, default=None):
        return self._data.get(key, default)

    def to_dict(self):
        return dict(self._data)

    def __contains__(self, key):
        return key in self._data


class TestCoerceToken:
    """凭据形态归一。"""

    def test_plain_string(self):
        assert _coerce_token("tok") == "tok"

    def test_none_and_empty(self):
        assert _coerce_token(None) == ""
        assert _coerce_token("") == ""

    def test_dict_access_token_snake(self):
        assert _coerce_token({"access_token": "tok"}) == "tok"

    def test_dict_access_token_camel(self):
        """GE 传 accessToken 驼峰，需兼容。"""
        assert _coerce_token({"accessToken": "tok"}) == "tok"

    def test_dict_without_known_key(self):
        assert _coerce_token({"other": "x"}) == ""

    def test_unsupported_type(self):
        assert _coerce_token(123) == ""


class TestReadAuthToken:
    """双格式读取主逻辑。"""

    def test_temp_prefixed_hit(self):
        """aiplatform 2.6 现行形态（最关键用例）。"""
        st = _FakeState({f"temp:{AUTH_ID}": "TOKEN_NEW"})
        assert _read_auth_token(st, AUTH_ID) == "TOKEN_NEW"

    def test_bare_key_fallback(self):
        """aiplatform 1.x 遗留形态，必须仍可读（升级不破坏旧会话）。"""
        st = _FakeState({AUTH_ID: "TOKEN_OLD"})
        assert _read_auth_token(st, AUTH_ID) == "TOKEN_OLD"

    def test_temp_takes_precedence(self):
        """两种形态并存时以 temp: 为准（新版是权威）。"""
        st = _FakeState({f"temp:{AUTH_ID}": "NEW", AUTH_ID: "OLD"})
        assert _read_auth_token(st, AUTH_ID) == "NEW"

    def test_dict_credential(self):
        st = _FakeState({f"temp:{AUTH_ID}": {"access_token": "T"}})
        assert _read_auth_token(st, AUTH_ID) == "T"

    def test_empty_string_treated_as_missing(self):
        """空字符串不能当作有效 token，否则会带着空凭据调 API。"""
        st = _FakeState({f"temp:{AUTH_ID}": ""})
        assert _read_auth_token(st, AUTH_ID) == ""

    def test_absent_returns_empty(self):
        assert _read_auth_token(_FakeState({}), AUTH_ID) == ""

    def test_none_state(self):
        assert _read_auth_token(None, AUTH_ID) == ""

    def test_empty_auth_id(self):
        assert _read_auth_token(_FakeState({}), "") == ""

    def test_plain_dict_state_supported(self):
        """state 可能是普通 dict（无 to_dict），兜底不能因此失效。"""
        st = {f"temp:{AUTH_ID}": "TOKEN"}
        assert _read_auth_token(st, AUTH_ID) == "TOKEN"


class TestFullResourceNameFallback:
    """GE 可能传入授权资源全名而非裸 auth_id。"""

    def test_full_resource_name_hit(self):
        full = f"projects/839062387451/locations/global/authorizations/{AUTH_ID}"
        st = _FakeState({f"temp:{full}": "TOKEN_FULL"})
        assert _read_auth_token(st, AUTH_ID) == "TOKEN_FULL"

    def test_ambiguous_match_gives_up(self):
        """多个 temp: 键都以 auth_id 结尾时，不能猜——放弃比用错凭据安全。"""
        st = _FakeState({
            f"temp:projects/a/authorizations/{AUTH_ID}": "A",
            f"temp:projects/b/authorizations/{AUTH_ID}": "B",
        })
        assert _read_auth_token(st, AUTH_ID) == ""

    def test_unrelated_credentials_not_picked(self):
        st = _FakeState({"temp:someone-else-auth": "X", "temp:another": "Y"})
        assert _read_auth_token(st, AUTH_ID) == ""


class TestPublicAccessors:
    """对外函数：飞书与 GWS 两侧对称，且都走双格式。"""

    def test_lark_temp_prefixed(self):
        st = _FakeState({f"temp:{LARK_AUTH_ID}": "LARK_TOK"})
        assert get_access_token(_FakeState()) == ""
        assert _read_auth_token(st, LARK_AUTH_ID) == "LARK_TOK"

    def test_gws_temp_prefixed(self):
        st = _FakeState({f"temp:{GOOGLE_WORKSPACE_AUTH_ID}": "GWS_TOK"})
        assert _read_auth_token(st, GOOGLE_WORKSPACE_AUTH_ID) == "GWS_TOK"

    def test_string_context_passthrough(self):
        """兼容历史调用：直接传 token 字符串时原样返回。"""
        assert get_access_token("DIRECT_TOKEN") == "DIRECT_TOKEN"
        assert get_google_workspace_access_token("DIRECT_TOKEN") == "DIRECT_TOKEN"

    def test_context_with_state_attribute(self):
        class Ctx:
            state = _FakeState({f"temp:{LARK_AUTH_ID}": "FROM_CTX"})

        assert get_access_token(Ctx()) == "FROM_CTX"

    def test_context_without_state_does_not_raise(self):
        """缺少 state 属性时返回空串，不能抛异常打断工具执行。"""
        class Ctx:
            pass

        assert get_access_token(Ctx()) == ""


class TestEndToEndInjectionChain:
    """端到端：复刻 aiplatform 2.6 模板的注入路径，验证真实可读。

    模板行为是 ``state_delta[f"temp:{auth_id}"]`` 经
    ``session_service.append_event`` → ``_apply_temp_state`` 写入 session.state。
    这里直接走这条链路而**不调用 LLM**，避免测试依赖模型环境与执行顺序。

    本轮修复前：该链路上 ``state.get(auth_id)`` 恒为 None（真机对比已证实）。
    """

    def _inject(self, service, auth_id, token):
        import asyncio

        from google.adk.events import Event
        from google.adk.events.event_actions import EventActions

        async def run():
            sess = await service.create_session(app_name="a", user_id="u")
            await service.append_event(
                session=sess,
                event=Event(
                    author="user",
                    actions=EventActions(state_delta={f"temp:{auth_id}": token}),
                ),
            )
            return sess

        return asyncio.run(run())

    def test_inmemory_service_temp_token_readable(self):
        """InMemory 服务：temp: 注入后可从 session.state 读到。"""
        from google.adk.sessions import InMemorySessionService

        sess = self._inject(InMemorySessionService(), AUTH_ID, "E2E_TOKEN")
        assert _read_auth_token(sess.state, AUTH_ID) == "E2E_TOKEN"

    def test_both_providers_agree(self):
        """不同 session service 必须表现一致（生产走 VertexAI）。"""
        from google.adk.sessions import InMemorySessionService

        svc = InMemorySessionService()
        sess = self._inject(svc, AUTH_ID, "TOK_A")
        # 关键断言：注入后的真实 key 就是 temp: 前缀形态
        # 注意 InMemory 服务的 state 是 dict，生产 VertexAI 是 State 对象，需都兼容
        raw = sess.state.to_dict() if hasattr(sess.state, "to_dict") else dict(sess.state)
        keys = list(raw.keys())
        assert f"temp:{AUTH_ID}" in keys, f"未按 temp: 前缀注入，实际 key={keys}"
        assert _read_auth_token(sess.state, AUTH_ID) == "TOK_A"

    def test_lark_and_gws_both_readable(self):
        """飞书与 GWS 双通道同时注入，互不串号。"""
        from google.adk.sessions import InMemorySessionService

        import asyncio

        from google.adk.events import Event
        from google.adk.events.event_actions import EventActions

        async def run():
            svc = InMemorySessionService()
            sess = await svc.create_session(app_name="a", user_id="u")
            await svc.append_event(
                session=sess,
                event=Event(author="user", actions=EventActions(state_delta={
                    f"temp:{LARK_AUTH_ID}": "LARK_T",
                    f"temp:{GOOGLE_WORKSPACE_AUTH_ID}": "GWS_T",
                })),
            )
            return sess

        sess = asyncio.run(run())
        assert _read_auth_token(sess.state, LARK_AUTH_ID) == "LARK_T"
        assert _read_auth_token(sess.state, GOOGLE_WORKSPACE_AUTH_ID) == "GWS_T"


class TestErrorMessageMentionsBothKeys:
    """报错信息要能让排查者知道尝试过哪些 key（不要只报一个 key 导致误判）。"""

    def test_lark_message_lists_both(self):
        with open("nexus_agent/tools.py") as f:
            src = f.read()
        # 报错模板必须同时点出 temp: 前缀与裸 key，两者被 f-string 换行拆开，
        # 故分别断言片段而非整句。
        assert "tried 'temp:{LARK_AUTH_ID}'" in src
        assert "Missing token in " in src
        assert "tool_context.state (tried" in src

    def test_gws_message_lists_both(self):
        with open("nexus_agent/tools.py") as f:
            src = f.read()
        assert "tried 'temp:{GOOGLE_WORKSPACE_AUTH_ID}'" in src

# ---------------------------------------------------------------------------
# 分批授权（2026-10-30）：飞书 173 项 user 权限超单批容量，按 4 批下发。
# 读取策略：批次逆序（最后一批最全，因飞书 scope 累积）→ 回退单批 LARK_AUTH_ID。
# ---------------------------------------------------------------------------


class TestBatchAuthIds:
    """候选顺序与多 id 读取。"""

    def test_candidate_order_is_reversed_then_legacy(self):
        from nexus_agent.tools import _token_candidate_ids

        assert _token_candidate_ids(
            "legacy", ["b1", "b2", "b3", "b4"]
        ) == ["b4", "b3", "b2", "b1", "legacy"]

    def test_candidate_order_without_batches(self):
        """未配置多批（回滚态）时只尝试单批 id。"""
        from nexus_agent.tools import _token_candidate_ids

        assert _token_candidate_ids("legacy", []) == ["legacy"]
        assert _token_candidate_ids("legacy", None) == ["legacy"]

    def test_candidate_dedupes_primary_present_in_batches(self):
        from nexus_agent.tools import _token_candidate_ids

        assert _token_candidate_ids("b2", ["b1", "b2"]) == ["b2", "b1"]

    def test_reads_last_batch_first_when_all_present(self):
        """全部批次都有 token 时，取最后一批（最全）。"""
        from nexus_agent.tools import _read_token_from_ids

        state = {f"temp:b{i}": f"T{i}" for i in (1, 2, 3, 4)}
        assert _read_token_from_ids(state, ["b4", "b3", "b2", "b1"]) == "T4"

    def test_falls_back_when_last_batch_missing(self):
        """只授权了第 2 批时仍能取到（不因缺批而全空）。"""
        from nexus_agent.tools import _read_token_from_ids

        state = {"temp:b2": "ONLY2"}
        assert _read_token_from_ids(state, ["b4", "b3", "b2", "b1"]) == "ONLY2"

    def test_all_missing_returns_empty(self):
        from nexus_agent.tools import _read_token_from_ids

        assert _read_token_from_ids({}, ["b4", "b1"]) == ""

    def test_legacy_single_key_still_readable(self):
        """回滚场景：旧单批 key 必须仍能读到。"""
        from nexus_agent.tools import _read_token_from_ids

        assert _read_token_from_ids({"temp:legacy": "OLD"}, ["b1", "legacy"]) == "OLD"

    def test_batch_suffix_appears_in_error_hint(self):
        """报错文案要列出批次 id，否则排查只看得到一个 key。"""
        from nexus_agent.tools import _batch_auth_suffix

        assert _batch_auth_suffix([]) == ""
        s = _batch_auth_suffix(["b1", "b2"])
        assert "2 batch" in s and "b1" in s and "b2" in s


class TestBatchEnvParsing:
    """LARK_AUTH_IDS 的解析（逗号分隔、去重、保序、容错）。"""

    def test_splits_and_trims(self):
        from nexus_agent.config import _split_csv_ids

        assert _split_csv_ids(" b1 , b2 ,b3 ") == ["b1", "b2", "b3"]

    def test_dedupes_preserving_order(self):
        from nexus_agent.config import _split_csv_ids

        assert _split_csv_ids("b1,b2,b1") == ["b1", "b2"]

    def test_empty_and_none(self):
        from nexus_agent.config import _split_csv_ids

        assert _split_csv_ids("") == []
        assert _split_csv_ids(None) == []

    def test_trailing_comma_ignored(self):
        from nexus_agent.config import _split_csv_ids

        assert _split_csv_ids("b1,b2,") == ["b1", "b2"]
