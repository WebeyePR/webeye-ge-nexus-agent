#!/bin/bash
# ==============================================================================
# 完全清理部署资源脚本
# 功能：根据本地环境变量记录，彻底删除远程 Agent、授权资源和 Reasoning Engine
# ==============================================================================

# 获取脚本所在目录
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(dirname "$SCRIPT_DIR")"

# 1. 支持指定环境后缀，优先通过参数传递
ENV_SUFFIX="${1:-$ENV_SUFFIX}"
if [ -f "$SCRIPT_DIR/load_env.sh" ]; then
    source "$SCRIPT_DIR/load_env.sh" "$ENV_SUFFIX"
else
    echo "❌ 错误: 找不到 $SCRIPT_DIR/load_env.sh"
    exit 1
fi

echo "=========================================="
echo "🛡️  正在准备清理远程部署资源..."
echo "=========================================="

# 检查必要的基础变量
if [ -z "$PROJECT_ID" ]; then
    echo "❌ 错误: PROJECT_ID 未设置，请检查 .env 文件"
    exit 1
fi

TOKEN=$(gcloud auth print-access-token)
if [ -z "$TOKEN" ]; then
    echo "❌ 错误: 无法获取 gcloud access token，请先运行 gcloud auth login"
    exit 1
fi

DEPLOY_ENV_BASENAME=$(basename "${DEPLOY_ENV_FILE:-.deploy_env}")

# 2. 删除 Gemini Enterprise Agent
if [ -n "$GE_AGENT_RESOURCE_NAME" ]; then
    bash "$SCRIPT_DIR/delete_agent.sh" "$GE_AGENT_RESOURCE_NAME"
else
    echo "⏭️  跳过 Agent 删除: 未在 $DEPLOY_ENV_BASENAME 中找到 GE_AGENT_RESOURCE_NAME"
fi

# 3. 删除授权资源 (Authorization)
#    分批授权（2026-10-30）：需逐个删除 LARK_AUTH_IDS 里的每一批 + 旧单批 +
#    GWS，否则卸载后 4 批资源会残留（脏资源，重建时易与旧资源混淆）。
CLEANUP_AUTH_IDS=""
[ -n "$LARK_AUTH_IDS" ] && CLEANUP_AUTH_IDS="$LARK_AUTH_IDS"
[ -n "$LARK_AUTH_ID" ] && CLEANUP_AUTH_IDS="${CLEANUP_AUTH_IDS:+$CLEANUP_AUTH_IDS,}$LARK_AUTH_ID"
[ -n "$GOOGLE_WORKSPACE_AUTH_ID" ] && CLEANUP_AUTH_IDS="${CLEANUP_AUTH_IDS:+$CLEANUP_AUTH_IDS,}$GOOGLE_WORKSPACE_AUTH_ID"

AUTH_ID_LIST=$(echo "$CLEANUP_AUTH_IDS" | tr ',' '\n' | sed 's/^[[:space:]]*//; s/[[:space:]]*$//' | awk 'NF && !seen[$0]++')

if [ -z "$AUTH_ID_LIST" ]; then
    echo "⏭️  跳过授权资源删除: 未在 $BASE_ENV_FILE 中找到任何 AUTH_ID"
else
    echo "🗑️  待删除授权资源（$(echo "$AUTH_ID_LIST" | wc -l | tr -d ' ') 条）:"
    echo "$AUTH_ID_LIST" | sed 's/^/   - /'
    while IFS= read -r auth_id; do
        [ -z "$auth_id" ] && continue
        # 注意：如果授权资源被其他 Agent 使用，脚本内部会返回错误响应并安全跳过
        bash "$SCRIPT_DIR/delete_authorization.sh" "$auth_id"
    done <<< "$AUTH_ID_LIST"
fi

# 4. 删除 Vertex AI Reasoning Engine
if [ -n "$VERTEX_REASONING_ENGINE_NAME" ]; then
    bash "$SCRIPT_DIR/delete_reasoning_engine.sh" "$VERTEX_REASONING_ENGINE_NAME"
else
    echo "⏭️  跳过 Reasoning Engine 删除: 未在 $DEPLOY_ENV_BASENAME 中找到 VERTEX_REASONING_ENGINE_NAME"
fi

# 5. 清理本地部署记录
echo "------------------------------------------"
read -p "是否同步清理本地 $DEPLOY_ENV_BASENAME 记录? (y/n) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    if [ -f "$DEPLOY_ENV_FILE" ]; then
        # 仅保留空文件或删除特定键值，这里采取清空策略但保留文件
        > "$DEPLOY_ENV_FILE"
        echo "✨ $DEPLOY_ENV_BASENAME 已清空"
    fi
fi

echo "=========================================="
echo "🎉 清理流程结束"
echo "=========================================="
