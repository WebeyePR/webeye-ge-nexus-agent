import os
import sys
import json

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.generate_auth_link import build_auth_url, build_length_warning


def test_build_auth_url_keeps_scope_colons_and_encodes_spaces():
    url = build_auth_url(
        "https://accounts.feishu.cn/open-apis/authen/v1/authorize",
        {
            "client_id": "cli_test",
            "response_type": "code",
            "scope": "bitable:app docs:document:import docx:document",
        },
    )

    assert "scope=bitable:app%20docs:document:import%20docx:document" in url
    assert "%3A" not in url
    assert "+" not in url


def test_build_length_warning_warns_for_long_auth_url():
    warning = build_length_warning("https://example.com/?" + "x" * 4100, scope_count=189)

    assert "HTTP 431" in warning
    assert "189" in warning


def test_lark_scopes_file_separates_user_and_tenant_only():
    """2026-10-30：user 只保留可用户授权项，tenant-only 移入 tenant 数组。

    背景：tenant-only 权限用户身份永远授权不到，放进 authorize URL 属无效请求
    （可能触发 20068），且对应的能力在用户态根本不可用。
    """
    scopes_path = os.path.join(os.path.dirname(__file__), "..", "scripts", "lark_scopes.json")
    with open(scopes_path, encoding="utf-8") as f:
        data = json.load(f)

    user, tenant = data["scopes"]["user"], data["scopes"]["tenant"]

    # 1) tenant-only 权限不得出现在 user
    for scope in (
        "drive:drive",
        "drive:drive:readonly",
        "drive:drive.search:readonly",
        "im:chat",
        "im:message.group_msg",
        "wiki:wiki",
        "bitable:app",
    ):
        assert scope not in user, f"{scope} 属 tenant-only，不应出现在 user 数组"
        assert scope in tenant, f"{scope} 应归档到 tenant 数组"

    # 2) 关键用户态能力仍在
    for scope in (
        "docs:document.content:read",
        "docx:document",
        "im:message",
        "search:docs:read",
        "offline_access",
    ):
        assert scope in user

    # 3) 两个数组都必须去重（原文件曾有 docx:document / im:message 重复）
    assert len(user) == len(set(user))
    assert len(tenant) == len(set(tenant))

    # 4) 明确标注已被分批清单取代
    assert data.get("supersededBy") == "docs/lark_scope_batches.json"

    # 5) user 必须是分批清单的子集；tenant-only 不得混进分批清单
    plan_path = os.path.join(os.path.dirname(__file__), "..", "docs", "lark_scope_batches.json")
    with open(plan_path, encoding="utf-8") as f:
        plan = json.load(f)
    batched = {s for b in plan["batches"] for s in b["scopes"]}
    assert set(user) <= batched
    assert not (set(tenant) & batched)
