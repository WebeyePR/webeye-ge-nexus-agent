#!/bin/bash

# 删除 Gemini Enterprise Agent
#
# 两种用法，确认策略不同：
#   1) 显式传参  → 视为明确意图，直接删除，不做二次确认（批处理链路依赖此行为）
#   2) 不传参数  → 取 .deploy_env 的当前部署（线上 Agent），必须二次确认；
#                  非交互环境无法确认时直接中止，可用 --yes 显式跳过
#
# 使用方法:
#   ./delete_agent.sh <AGENT_NAME>
#   ./delete_agent.sh              # 无参：删当前部署，需确认
#   ./delete_agent.sh --yes        # 无参 + 跳过确认
#   ./delete_agent.sh --help

# 加载配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load_env.sh"

usage() {
    cat <<'EOF'
使用方法:
  ./delete_agent.sh <AGENT_NAME>     删除指定 Agent（显式传参，不做二次确认）
  ./delete_agent.sh                  删除 .deploy_env 中的当前部署（线上 Agent），需二次确认
  ./delete_agent.sh --yes            跳过二次确认（配合无参使用）
  ./delete_agent.sh --help           显示本帮助

示例:
  ./delete_agent.sh projects/839062387451/locations/global/collections/default_collection/engines/webeye-agentspace-app_1742521319182/assistants/default_assistant/agents/123456789

提示:
  Agent NAME 可以从 check_agent_status.sh 或 list_all_resources.sh 的输出中获取
EOF
}

# ---- 参数解析：显式传参 / 无参默认值 / --yes / --help ----
ASSUME_YES=0
TARGET_ARG=""

while [ $# -gt 0 ]; do
    case "$1" in
        -y|--yes)
            ASSUME_YES=1
            ;;
        -h|--help)
            usage
            exit 0
            ;;
        -*)
            echo "❌ 未知参数: $1"
            echo ""
            usage
            exit 1
            ;;
        *)
            if [ -n "$TARGET_ARG" ]; then
                echo "❌ 只能指定一个 Agent：已收到 '$TARGET_ARG'，又收到 '$1'"
                exit 1
            fi
            TARGET_ARG="$1"
            ;;
    esac
    shift
done

if [ -n "$TARGET_ARG" ]; then
    AGENT_NAME="$TARGET_ARG"
    EXPLICIT_ARG=1
else
    AGENT_NAME="$GE_AGENT_RESOURCE_NAME"
    EXPLICIT_ARG=0
fi

# 彻底清洗变量，去除单双引号、\r 及首尾空格，防止 REST 请求路径污染
AGENT_NAME=$(echo "$AGENT_NAME" | tr -d "'\"" | tr -d '\r' | xargs)

if [ -z "$AGENT_NAME" ]; then
    echo "❌ 未指定 Agent，且 .deploy_env 中也没有 GE_AGENT_RESOURCE_NAME"
    echo ""
    usage
    exit 1
fi

# ---- 确认门禁 ----
# 显式传参即视为明确意图（cleanup_deployed.sh 等批处理链路依赖「传参不阻塞」）；
# 只有落到「默认值」这条隐含路径时才要求人工确认。
if [ "$EXPLICIT_ARG" = "1" ]; then
    echo "ℹ️  已显式指定 Agent，跳过二次确认"
else
    echo "⚠️  未指定 Agent，将删除 .deploy_env 中记录的当前部署："
    echo "      $AGENT_NAME"
    echo "   （如需跳过此确认，可显式传入 Agent 名，或追加 --yes）"

    if [ "$ASSUME_YES" = "1" ]; then
        echo "⚡ 已指定 --yes，跳过二次确认"
    else
        confirm=""
        # 先探测可用输入源，再决定是否打印提示：
        # 否则非交互环境会先输出「确认删除?」却没人能回答，日志有误导性。
        if [ -t 0 ]; then
            printf "确认删除? (y/N): " >&2
            read -r confirm || confirm=""
        elif { true > /dev/tty; } 2>/dev/null; then
            # stdin 不是终端但存在控制终端（如被管道调用），从 /dev/tty 读，避免吃掉管道数据
            printf "确认删除? (y/N): " >&2
            read -r confirm < /dev/tty || confirm=""
        else
            echo "❌ 非交互环境无法二次确认，已中止（未执行任何删除）"
            echo "   如确认要删除，请显式传入 Agent 名，或追加 --yes"
            exit 1
        fi
        case "$confirm" in
            y|Y|yes|YES) ;;
            *)
                echo "已取消"
                exit 0
                ;;
        esac
    fi
fi

echo "正在删除 Agent: $AGENT_NAME"

response=$(curl --http1.1 -X DELETE \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://${GE_APP_LOCATION:-global}-discoveryengine.googleapis.com/v1alpha/${AGENT_NAME}" \
  -w "\nHTTP_STATUS:%{http_code}" \
  2>/dev/null)

http_status=$(echo "$response" | grep "HTTP_STATUS" | cut -d: -f2)
body=$(echo "$response" | sed '/HTTP_STATUS/d')

if [ "$http_status" = "200" ] || [ "$http_status" = "204" ]; then
    echo "✅ Agent 删除成功"
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
elif [ "$http_status" = "404" ]; then
    echo "✅ Agent 已不存在 (404)"
else
    echo "❌ 删除失败 (HTTP $http_status)"
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
    # 失败必须给出非 0 退出码：否则调用方（cleanup/CI）会把「没删掉」当成功
    exit 1
fi
