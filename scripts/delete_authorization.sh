#!/bin/bash

# 删除 Gemini Enterprise 授权资源
#
# 两种用法，确认策略不同：
#   1) 显式传参  → 视为明确意图，直接删除，不做二次确认（批处理链路依赖此行为）
#   2) 不传参数  → 取 .env 的 LARK_AUTH_ID（单批/回滚用资源），必须二次确认；
#                  非交互环境无法确认时直接中止，可用 --yes 显式跳过
#
# 使用方法:
#   ./delete_authorization.sh <AUTH_ID>
#   ./delete_authorization.sh              # 无参：删 .env 的 LARK_AUTH_ID，需确认
#   ./delete_authorization.sh --yes        # 无参 + 跳过确认
#   ./delete_authorization.sh --help

# 加载配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$SCRIPT_DIR/load_env.sh"

usage() {
    cat <<'EOF'
使用方法:
  ./delete_authorization.sh <AUTH_ID>   删除指定授权资源（显式传参，不做二次确认）
  ./delete_authorization.sh             删除 .env 中的 LARK_AUTH_ID，需二次确认
  ./delete_authorization.sh --yes       跳过二次确认（配合无参使用）
  ./delete_authorization.sh --help      显示本帮助

示例:
  ./delete_authorization.sh ge-nexus-feishu-1

注意:
  授权资源若仍被 Agent 引用，API 会拒绝物理删除（脚本会给出占用方并安全跳过）。
  分批模式（LARK_AUTH_IDS）下请逐个传入批次 id 删除；cleanup_deployed.sh 已自动遍历。
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
                echo "❌ 只能指定一个 AUTH_ID：已收到 '$TARGET_ARG'，又收到 '$1'"
                exit 1
            fi
            TARGET_ARG="$1"
            ;;
    esac
    shift
done

if [ -n "$TARGET_ARG" ]; then
    AUTH_ID="$TARGET_ARG"
    EXPLICIT_ARG=1
else
    AUTH_ID="$LARK_AUTH_ID"
    EXPLICIT_ARG=0
fi

# 彻底清洗变量，去除单双引号、\r 及首尾空格，防止 REST 请求路径污染
AUTH_ID=$(echo "$AUTH_ID" | tr -d "'\"" | tr -d '\r' | xargs)

if [ -z "$AUTH_ID" ]; then
    echo "❌ 未指定 AUTH_ID，且 .env 中也没有 LARK_AUTH_ID"
    echo ""
    usage
    exit 1
fi

# ---- 确认门禁 ----
# 显式传参即视为明确意图（cleanup_deployed.sh 的 while 循环依赖「传参不读 stdin」，
# 否则 read 会吃掉循环输入，导致只删第一条）；
# 只有落到「默认值」这条隐含路径时才要求人工确认。
if [ "$EXPLICIT_ARG" = "1" ]; then
    echo "ℹ️  已显式指定 AUTH_ID，跳过二次确认"
else
    echo "⚠️  未指定 AUTH_ID，将删除 .env 中 LARK_AUTH_ID 指向的授权资源："
    echo "      $AUTH_ID"
    echo "   （该默认值常用于单批/回滚场景，误删会导致已授权用户需要重新授权）"
    if [ -n "$LARK_AUTH_IDS" ]; then
        echo "   （检测到 LARK_AUTH_IDS 分批模式：本命令只删 legacy 单批，"
        echo "     其余批次请逐个传参，或直接跑 cleanup_deployed.sh 全量清理）"
    fi
    echo "   （如需跳过此确认，可显式传入 AUTH_ID，或追加 --yes）"

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
            echo "   如确认要删除，请显式传入 AUTH_ID，或追加 --yes"
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

echo "正在删除授权资源: $AUTH_ID"

response=$(curl --http1.1 -X DELETE \
  -H "Authorization: Bearer $(gcloud auth print-access-token)" \
  -H "Content-Type: application/json" \
  -H "X-Goog-User-Project: ${PROJECT_ID}" \
  "https://global-discoveryengine.googleapis.com/v1alpha/projects/${PROJECT_ID}/locations/global/authorizations/${AUTH_ID}" \
  -w "\nHTTP_STATUS:%{http_code}" \
  2>/dev/null)

http_status=$(echo "$response" | grep "HTTP_STATUS" | cut -d: -f2)
body=$(echo "$response" | sed '/HTTP_STATUS/d')

if [ "$http_status" = "200" ] || [ "$http_status" = "204" ]; then
    echo "✅ 授权资源删除成功"
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
elif [ "$http_status" = "404" ]; then
    echo "✅ 授权资源已不存在 (404)"
elif [ "$http_status" = "400" ] && echo "$body" | grep -q "Authorization is linked to a resource and cannot be deleted"; then
    linked_agent=$(echo "$body" | grep -oE "projects/839062387451/locations/[^\"]+" | head -n 1)
    if [ -z "$linked_agent" ]; then
        linked_agent=$(echo "$body" | grep -oE "projects/[^\"]+" | head -n 1)
    fi
    echo "⚠️  警告: 授权资源 [$AUTH_ID] 当前仍被活跃的 Agent 链接依赖，无法被强制物理删除！"
    echo "   🔗 占用 Agent: $linked_agent"
    echo "💡 建议: 如果您确定此 Agent 属于过期遗留，可先执行以下命令删除该 Agent，之后重新运行此脚本即可完成清理:"
    echo "   👉 bash scripts/delete_agent.sh \"$linked_agent\""
    echo "⏭️  由于资源共享中，已安全跳过此授权资源的物理删除，清理流继续运行。"
    exit 0
else
    echo "❌ 删除失败 (HTTP $http_status)"
    echo "$body" | python3 -m json.tool 2>/dev/null || echo "$body"
    # 失败必须给出非 0 退出码：否则调用方（cleanup/CI）会把「没删掉」当成功
    exit 1
fi
