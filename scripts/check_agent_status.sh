#!/bin/bash

# 检查 Gemini Enterprise Agent 状态和授权配置
# 使用方法: ./check_agent_status.sh

# 加载配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load_env.sh"

# 默认值
GE_APP_LOCATION="${GE_APP_LOCATION:-global}"

echo "=== 1. 查询 Agent 列表 ==="
curl --http1.1 -X GET \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://${GE_APP_LOCATION}-discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_ID}/locations/${GE_APP_LOCATION}/collections/default_collection/engines/${GE_APP_ID}/assistants/default_assistant/agents" \
  | jq '.'

echo ""
echo "=== 2. 查询授权资源列表 ==="
curl --http1.1 -X GET \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://${GE_APP_LOCATION}-discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_ID}/locations/${GE_APP_LOCATION}/authorizations" \
  | jq '.'

echo ""
echo "=== 3. 查询特定授权资源详情 ==="
# 分批授权（2026-10-30）：逐个检查 LARK_AUTH_IDS 每批 + 旧单批 + GWS。
# 只查单条会漏掉「4 批中某一批缺失」这类问题。
STATUS_PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format="value(projectNumber)" 2>/dev/null)
STATUS_PROJECT_REF="${STATUS_PROJECT_NUMBER:-$PROJECT_ID}"

STATUS_AUTH_IDS=""
[ -n "$LARK_AUTH_IDS" ] && STATUS_AUTH_IDS="$LARK_AUTH_IDS"
[ -n "$LARK_AUTH_ID" ] && STATUS_AUTH_IDS="${STATUS_AUTH_IDS:+$STATUS_AUTH_IDS,}$LARK_AUTH_ID"
[ -n "$GOOGLE_WORKSPACE_AUTH_ID" ] && STATUS_AUTH_IDS="${STATUS_AUTH_IDS:+$STATUS_AUTH_IDS,}$GOOGLE_WORKSPACE_AUTH_ID"

for auth_id in $(echo "$STATUS_AUTH_IDS" | tr ',' ' '); do
  [ -z "$auth_id" ] && continue
  echo "--- $auth_id ---"
  curl --http1.1 -s -X GET \
    -H "Authorization: Bearer $(gcloud auth print-access-token)" \
    -H "Content-Type: application/json" \
    -H "X-Goog-User-Project: ${PROJECT_ID}" \
    "https://${GE_APP_LOCATION}-discoveryengine.googleapis.com/v1alpha/projects/${STATUS_PROJECT_REF}/locations/${GE_APP_LOCATION}/authorizations/${auth_id}" \
    | jq '.'
done

