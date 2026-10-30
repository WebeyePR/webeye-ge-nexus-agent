"""分批清单生成器（scripts/plan_lark_scope_batches.py）的回归测试。

关键目标：保证「分组规则」能离线复现已上线的 173 项清单 —— 这正是之前的真实缺口
（只有结果文件、没有生成规则）。这些用例全部不联网。
"""

import json
import os
import sys

import pytest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from scripts.plan_lark_scope_batches import (
    build_batches,
    diff_plans,
    signature,
    user_scopes_from,
)

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
PLAN_PATH = os.path.join(ROOT, "docs", "lark_scope_batches.json")


def _load_plan():
    with open(PLAN_PATH, encoding="utf-8") as f:
        return json.load(f)


def test_grouping_reproduces_committed_plan():
    """用已上线清单里的 scope 反推分组，必须得到完全相同的批次与归属。"""
    plan = _load_plan()
    scopes = [s for b in plan["batches"] for s in b["scopes"]]

    generated, leftovers = build_batches(scopes, plan["limit"])

    assert leftovers == [], "不应有未归类的 scope"
    assert signature(generated) == signature(plan["batches"]), "分组结果与已上线清单不一致"
    assert [b["count"] for b in generated] == [b["count"] for b in plan["batches"]]


def test_offline_access_goes_to_first_batch():
    plan = _load_plan()
    scopes = [s for b in plan["batches"] for s in b["scopes"]]
    generated, _ = build_batches(scopes, plan["limit"])
    assert "offline_access" in generated[0]["scopes"]
    for batch in generated[1:]:
        assert "offline_access" not in batch["scopes"]


def test_overflow_raises_instead_of_silently_splitting():
    """超出单批上限必须报错：静默拆批会改名，与线上 authorization 资源错位。"""
    scopes = [f"base:fake{i:02d}:read" for i in range(46)]
    with pytest.raises(ValueError) as exc:
        build_batches(scopes, 45)
    assert "超出上限" in str(exc.value)


def test_unknown_domain_merged_into_last_batch_and_reported():
    """未在 DOMAIN_GROUPS 里的域不得静默丢弃。"""
    scopes = ["docs:document:import", "brandnew:thing:read"]
    generated, leftovers = build_batches(scopes, 45)
    assert leftovers == ["brandnew:thing:read"]
    assert "brandnew:thing:read" in generated[-1]["scopes"]


def test_user_scopes_from_filters_tenant_only_and_dedupes():
    app_scopes = [
        {"scope": "a:one:read", "token_types": ["user"]},
        {"scope": "b:two:read", "token_types": ["tenant"]},
        {"scope": "c:three:read", "token_types": ["user", "tenant"]},
        {"scope": "a:one:read", "token_types": ["user"]},  # 重复
        {"scope": "", "token_types": ["user"]},
    ]
    assert user_scopes_from(app_scopes) == ["a:one:read", "c:three:read"]


def test_diff_plans_detects_added_and_removed_scopes():
    old = [{"name": "ge-nexus-feishu-1", "label": "x", "count": 2, "scopes": ["a", "b"]}]
    new = [{"name": "ge-nexus-feishu-1", "label": "x", "count": 2, "scopes": ["b", "c"]}]
    problems = diff_plans(old, new)
    assert len(problems) == 1
    assert "+['c']" in problems[0] and "-['a']" in problems[0]


def test_diff_plans_reports_batch_name_drift():
    old = [{"name": "ge-nexus-feishu-1", "label": "x", "count": 1, "scopes": ["a"]}]
    new = [{"name": "ge-nexus-feishu-9", "label": "x", "count": 1, "scopes": ["a"]}]
    problems = diff_plans(old, new)
    assert any("ge-nexus-feishu-9: 新增批次" in p for p in problems)
    assert any("ge-nexus-feishu-1: 生成结果中缺失" in p for p in problems)