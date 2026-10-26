#!/usr/bin/env bash
# ==============================================================================
# 本地真实运行/测试启动脚本（基于 ADK 官方 CLI，google-adk>=2.13 自带）
#
# 用法:
#   ./scripts/run.local.sh              # 默认启动 adk web（Dev UI，浏览器调试）
#   ./scripts/run.local.sh web          # 同上
#   ./scripts/run.local.sh api          # 纯 API 服务（无 UI，可 curl /run_sse 验证流式）
#   ./scripts/run.local.sh run          # 终端 CLI 交互模式
#   PORT=8080 ./scripts/run.local.sh    # 自定义端口
#
# 说明:
# - 默认强制 Vertex AI（与 Gemini Enterprise 线上行为对齐），走本机 ADC 凭证
#   （gcloud auth application-default login）。如需走 .env 里的 AI Studio
#   API Key，可设置 LOCAL_USE_VERTEX=0。
# - adk web / api_server 仅供本地开发调试，端点无鉴权，勿暴露公网。
# ==============================================================================
set -euo pipefail

L_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
L_ROOT_DIR="$(dirname "$L_SCRIPT_DIR")"
cd "$L_ROOT_DIR"

MODE="${1:-web}"
PORT="${PORT:-8000}"
AGENTS_DIR="${AGENTS_DIR:-nexus_agent}"

case "$MODE" in
  web|api|run) ;;
  *) echo "未知模式: ${MODE}（支持 web / api / run）" >&2; exit 1 ;;
esac

# 1. 加载项目环境变量（.env / .deploy_env）
# load_env.sh 内部直接引用 PROJECT_ID / ENV_SUFFIX，先初始化以兼容 set -u
export PROJECT_ID="${PROJECT_ID:-}"
export ENV_SUFFIX="${ENV_SUFFIX:-}"
# shellcheck disable=SC1091
source "$L_SCRIPT_DIR/load_env.sh"

# 2. 与生产对齐：默认走 Vertex AI（可用 LOCAL_USE_VERTEX=0 关闭，退回 .env 配置）
if [ "${LOCAL_USE_VERTEX:-1}" = "1" ]; then
  export GOOGLE_GENAI_USE_VERTEXAI=1
  export GOOGLE_CLOUD_PROJECT="${GOOGLE_CLOUD_PROJECT:-$PROJECT_ID}"
  export GOOGLE_CLOUD_LOCATION=global
  # Vertex 模式下清掉 AI Studio Key，避免 genai client 优先选到 API Key 通道
  unset GOOGLE_API_KEY || true
  # 预检 ADC 凭证，给出可操作的提示
  if [ ! -f "$HOME/.config/gcloud/application_default_credentials.json" ]; then
    echo "[!] 未检测到 ADC 凭证，请先执行: gcloud auth application-default login" >&2
    exit 1
  fi
fi

# 3. 模型名兜底（与部署保持一致，可通过 env 覆盖）
export MODEL_NAME="${MODEL_NAME:-gemini-3.5-flash}"

# 4. 非交互环境（CI/后台/管道）下先固化遥测询问，避免 ADK CLI 首次启动交互挂起
if [ ! -t 0 ]; then
  uv run adk telemetry disable >/dev/null 2>&1 || true
fi

# 5. 启动
echo "==> 模式: adk $MODE | agent: $AGENTS_DIR | model: $MODEL_NAME | port: $PORT"
case "$MODE" in
  web)
    exec uv run adk web "$AGENTS_DIR" --host 127.0.0.1 --port "$PORT" --log_level info
    ;;
  api)
    exec uv run adk api_server "$AGENTS_DIR" --host 127.0.0.1 --port "$PORT" --log_level info
    ;;
  run)
    exec uv run adk run "$AGENTS_DIR"
    ;;
esac
