#!/bin/bash
# ==============================================================================
# 校验 GE Agent 的授权绑定是否覆盖配置要求（分批授权一致性检查）
#
# 用法: bash scripts/check_ge_auth_binding.sh [env_suffix]
#
# 背景（2026-10-30）：部署只更新 adkAgentDefinition 的 engine 绑定，**不会**
# 改动 authorizationConfig。因此「引擎支持 4 批」与「agent 绑了 4 批」是两件
# 独立的事：漏绑会让用户授权了却读不到对应 token，表现为「授权成功但仍未授权」。
#
# 退出码: 0 = 一致；1 = 有缺失/多余（便于 CI 或人工判断）
# ==============================================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load_env.sh" "$1"

if [ -z "$GE_AGENT_RESOURCE_NAME" ]; then
    echo "⏭️  跳过授权绑定校验：未找到 GE_AGENT_RESOURCE_NAME（首次注册前正常）"
    exit 0
fi

TOKEN=$(gcloud auth print-access-token 2>/dev/null)
if [ -z "$TOKEN" ]; then
    echo "⚠️  无法获取 access token，跳过校验"
    exit 0
fi

AGENT_JSON=$(curl --http1.1 -s -X GET \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  "https://${GE_APP_LOCATION:-global}-discoveryengine.googleapis.com/v1alpha/${GE_AGENT_RESOURCE_NAME}")

AGENT_JSON="$AGENT_JSON" \
LARK_AUTH_IDS="$LARK_AUTH_IDS" \
LARK_AUTH_ID="$LARK_AUTH_ID" \
GOOGLE_WORKSPACE_AUTH_ID="$GOOGLE_WORKSPACE_AUTH_ID" \
python3 - <<'PY'
import json
import os
import sys

raw = os.environ.get("AGENT_JSON", "")
try:
    data = json.loads(raw)
except json.JSONDecodeError:
    print("⚠️  agent 详情解析失败，跳过校验:", raw[:160])
    sys.exit(0)

if data.get("error"):
    print("⚠️  查询 agent 失败:", str(data["error"].get("message"))[:160])
    sys.exit(0)

bound = {
    ref.split("/")[-1]
    for ref in (data.get("authorizationConfig") or {}).get("toolAuthorizations") or []
}
expected: list[str] = []
for item in (os.environ.get("LARK_AUTH_IDS") or "").split(","):
    item = item.strip()
    if item and item not in expected:
        expected.append(item)
legacy = (os.environ.get("LARK_AUTH_ID") or "").strip()
if legacy and legacy not in expected and len(expected) == 0:
    # 仅单批模式才把旧 id 计入期望；分批模式下旧 id 属回滚通道，不要求绑定
    expected.append(legacy)
gws = (os.environ.get("GOOGLE_WORKSPACE_AUTH_ID") or "").strip()
if gws and gws not in expected:
    expected.append(gws)

missing = [i for i in expected if i not in bound]
extra = sorted(bound - set(expected))

print(f"  已绑定 ({len(bound)}): {', '.join(sorted(bound)) or '无'}")
print(f"  配置期望 ({len(expected)}): {', '.join(expected) or '无'}")
if missing:
    print(f"  ★ 缺失: {missing}")
if extra:
    print(f"  · 多余（可能为历史遗留）: {extra}")

if missing:
    print("  → 修复: 用 authorizationConfig.toolAuthorizations 的 updateMask PATCH agent，"
          "把缺失项补进去；或重跑 bash scripts/register_to_GE.sh（分批模式会自动绑全）。")
    sys.exit(1)

print("结论: ✓ 授权绑定与配置一致")
sys.exit(0)
PY