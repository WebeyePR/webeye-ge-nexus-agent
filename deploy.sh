#!/bin/bash

# ==============================================================================
# Lark Agent 一键部署脚本 (Using UV and Google Cloud CLI)
# ==============================================================================
# 两种运行模式（参照 kerun_ai_web/scripts/deploy-gcp.sh 的职责边界）：
#   bash deploy.sh                 只读校验（默认）——检查环境/配置/API/二进制/
#                                  构建工具/凭证，输出就绪度报告，不做任何变更
#   bash deploy.sh deploy [suffix] 真实部署（可选环境后缀 .env-<suffix>）
#
# 兼容旧用法：首个参数不是 validate/deploy 时按环境后缀处理并执行真实部署，
# 避免既有工作流（bash deploy.sh <suffix>）失效。
# ==============================================================================

# 设置错误即停止
set -e

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 1. 解析模式参数
MODE="validate"
ENV_SUFFIX=""
case "${1:-}" in
    deploy)   MODE="deploy";     ENV_SUFFIX="${2:-}" ;;
    validate) MODE="validate";   ENV_SUFFIX="${2:-}" ;;
    -h|--help|help) MODE="help" ;;
    "")       MODE="validate" ;;
    *)        MODE="deploy";     ENV_SUFFIX="$1" ;;   # 旧用法：首参即环境后缀
esac

if [ "$MODE" = "help" ]; then
    echo "WebEye Nexus Agent 部署脚本"
    echo ""
    echo "用法："
    echo "  bash deploy.sh                  只读校验（默认，不部署，输出就绪度报告）"
    echo "  bash deploy.sh validate         同上"
    echo "  bash deploy.sh deploy           真实部署（幂等）"
    echo "  bash deploy.sh deploy staging   用 .env-staging 部署"
    echo "  bash deploy.sh staging          旧用法等价于 deploy staging"
    echo ""
    echo "校验模式检查项：环境配置文件、必填变量、gcloud/uv 工具、ADC 凭证、"
    echo "所需 API、CLI 二进制、GCS staging bucket、GE 应用可达性。"
    exit 0
fi

TARGET_ENV=".env"
if [ -n "$ENV_SUFFIX" ]; then
    TARGET_ENV=".env-${ENV_SUFFIX}"
fi

if [ ! -f "$TARGET_ENV" ]; then
    echo -e "${RED}错误: 未找到 ${TARGET_ENV} 配置文件。请创建并填写配置。${NC}"
    exit 1
fi

# 设置部署目录
DEPLOY_DIR=nexus_agent/deployment
# 设置 Python 路径
export PYTHONPATH=$(pwd)

echo "--- 加载配置文件 ---"
source scripts/load_env.sh "$ENV_SUFFIX"

echo -e "\n=================================================================="
echo -e "🔮  ${GREEN}WebEye Nexus Agent 部署配置看板${NC}"
echo -e "=================================================================="
echo -e "- 环境后缀 (ENV_SUFFIX):  ${BLUE}${ENV_SUFFIX:-无 (使用默认环境 .env)}${NC}"
echo -e "- 基础配置文件 (.env):     ${YELLOW}${BASE_ENV_FILE}${NC}"
echo -e "- 状态部署文件 (.deploy_env): ${YELLOW}${DEPLOY_ENV_FILE}${NC}"
echo -e "- 目标 GCP 项目 (PROJECT_ID): ${GREEN}${PROJECT_ID}${NC}"
echo -e "- 目标智能体名称 (DISPLAY_NAME): ${GREEN}${AGENT_DISPLAY_NAME}${NC}"
echo -e "- 飞书授权 ID (LARK_AUTH_ID): ${GREEN}${LARK_AUTH_ID}${NC}"
if [ -n "$GOOGLE_WORKSPACE_AUTH_ID" ]; then
echo -e "- GWS 授权 ID (GWS_AUTH_ID): ${GREEN}${GOOGLE_WORKSPACE_AUTH_ID}${NC}"
fi
if [ -n "$GE_AGENT_RESOURCE_NAME" ]; then
echo -e "- 预计部署类型:            ${YELLOW}更新部署 (Update Agent Engine)${NC}"
echo -e "- 已注册 GE Agent 资源:     ${YELLOW}${GE_AGENT_RESOURCE_NAME}${NC}"
else
echo -e "- 预计部署类型:            ${GREEN}首次部署 (Register New Agent)${NC}"
fi
echo -e "=================================================================="
if [ "$MODE" = "validate" ]; then
    echo -e "模式: ${BLUE}只读校验${NC}（不会产生任何云端变更）"
else
    echo -e "模式: ${RED}真实部署${NC}"
    echo -e "请在 5 秒内确认以上信息是否正确，按 ${RED}Ctrl+C${NC} 可安全取消部署..."
    for i in {5..1}; do
        echo -ne "倒计时: ${YELLOW}$i${NC} 秒...\r"
        sleep 1
    done
fi
echo -e "\n==================================================================\n"

# ------------------------------------------------------------------------------
# 只读校验（MODE=validate）：把所有前置条件检查完，输出就绪度报告后退出。
# 关键纪律：本段只做 describe/list/get，绝不 enable/create/add-iam-policy-binding。
# ------------------------------------------------------------------------------
if [ "$MODE" = "validate" ]; then
    VALIDATE_ERRORS=0
    VALIDATE_WARNS=0
    v_ok()   { echo -e "  ${GREEN}✓${NC} $1"; }
    v_warn() { echo -e "  ${YELLOW}!${NC} $1"; VALIDATE_WARNS=$((VALIDATE_WARNS+1)); }
    v_fail() { echo -e "  ${RED}✗${NC} $1"; VALIDATE_ERRORS=$((VALIDATE_ERRORS+1)); }

    echo -e "${BLUE}[1/7] 工具链${NC}"
    if command -v gcloud >/dev/null 2>&1; then
        v_ok "gcloud: $(gcloud version 2>/dev/null | head -1)"
    else
        v_fail "gcloud 未安装"
    fi
    if command -v uv >/dev/null 2>&1; then
        v_ok "uv: $(uv --version 2>/dev/null)"
    else
        v_fail "uv 未安装（https://docs.astral.sh/uv/）"
    fi

    echo -e "\n${BLUE}[2/7] 必填配置项${NC}"
    for var_name in PROJECT_ID LOCATION STAGING_BUCKET AGENT_DISPLAY_NAME GE_APP_ID GE_APP_LOCATION LARK_AUTH_ID LARK_CLIENT_ID; do
        var_val="${!var_name}"
        if [ -n "$var_val" ]; then
            v_ok "${var_name}"
        else
            v_fail "${var_name} 未设置"
        fi
    done
    if [ -n "$GOOGLE_WORKSPACE_AUTH_ID" ]; then
        v_ok "GOOGLE_WORKSPACE_AUTH_ID"
    else
        v_warn "GOOGLE_WORKSPACE_AUTH_ID 未设置（GWS 工具无法完成授权）"
    fi

    echo -e "\n${BLUE}[3/7] GCP 项目可达性${NC}"
    if gcloud projects describe "$PROJECT_ID" --format='value(projectId)' >/dev/null 2>&1; then
        v_ok "项目可访问: $PROJECT_ID"
    else
        v_fail "无法访问项目 $PROJECT_ID（检查凭证与权限）"
    fi

    echo -e "\n${BLUE}[4/7] ADC 凭证${NC}"
    if gcloud auth application-default print-access-token >/dev/null 2>&1; then
        v_ok "ADC 有效"
    else
        v_fail "ADC 无效，请运行 gcloud auth application-default login"
    fi

    echo -e "\n${BLUE}[5/7] 所需 API${NC}"
    ENABLED_APIS="$(gcloud services list --enabled --project "$PROJECT_ID" \
        --format='value(config.name)' 2>/dev/null || true)"
    for api in aiplatform.googleapis.com storage.googleapis.com \
               discoveryengine.googleapis.com iam.googleapis.com; do
        if grep -Fxq "$api" <<<"$ENABLED_APIS"; then
            v_ok "$api"
        else
            v_fail "$api 未启用（deploy 前需启用）"
        fi
    done

    echo -e "\n${BLUE}[6/7] CLI 二进制${NC}"
    if [ -f "nexus_agent/bin/lark-cli" ]; then
        size_mb=$(( $(wc -c < "nexus_agent/bin/lark-cli") / 1024 / 1024 ))
        v_ok "lark-cli（${size_mb}MB）"
    else
        v_fail "nexus_agent/bin/lark-cli 缺失（deploy 时会尝试构建）"
    fi
    if [ -f "nexus_agent/bin/gws" ]; then
        size_mb=$(( $(wc -c < "nexus_agent/bin/gws") / 1024 / 1024 ))
        v_ok "gws（${size_mb}MB）"
    else
        v_fail "nexus_agent/bin/gws 缺失（需从 googleworkspace/cli release 下载）"
    fi

    echo -e "\n${BLUE}[7/7] 存储与 GE 应用${NC}"
    if gcloud storage buckets describe "$STAGING_BUCKET" \
        --project "$PROJECT_ID" --format='value(name)' >/dev/null 2>&1; then
        v_ok "staging bucket 存在: $STAGING_BUCKET"
    else
        v_fail "staging bucket 不可访问: $STAGING_BUCKET"
    fi
    if [ -n "$GE_APP_ID" ]; then
        # 只读探测 GE 应用（失败不算致命：GE 侧权限可能独立于部署权限）
        if gcloud alpha discovery-engine engines describe "$GE_APP_ID" \
            --project "$PROJECT_ID" --location "$GE_APP_LOCATION" >/dev/null 2>&1; then
            v_ok "GE 应用可访问: $GE_APP_ID"
        else
            v_warn "GE 应用探测未通过（可能是权限或 gcloud alpha 组件缺失，不阻塞部署）"
        fi
    fi

    echo ""
    echo -e "=================================================================="
    if [ "$VALIDATE_ERRORS" -eq 0 ]; then
        echo -e "${GREEN}校验通过${NC}：0 错误，${VALIDATE_WARNS} 警告。未执行任何变更。"
        echo -e "确认后执行：${BLUE}bash deploy.sh deploy${NC}"
        exit 0
    else
        echo -e "${RED}校验未通过${NC}：${VALIDATE_ERRORS} 错误，${VALIDATE_WARNS} 警告。"
        echo -e "请先解决上述错误项再部署。"
        exit 1
    fi
fi

# 保存旧的 Reasoning Engine 资源名称以供后续清理
PREVIOUS_REASONING_ENGINE="$VERTEX_REASONING_ENGINE_NAME"

echo "--- 初始化项目 ---"
bash scripts/init.sh

echo "--- 检查必要工具和配置 ---"
# 检查必要工具
command -v gcloud >/dev/null 2>&1 || { echo "错误: 未安装 gcloud CLI。"; exit 1; }
command -v uv >/dev/null 2>&1 || { echo "错误: 未安装 uv。建议访问 https://docs.astral.sh/uv/ 安装。"; exit 1; }

if [ -z "$PROJECT_ID" ]; then
    echo "错误: 未设置 PROJECT_ID。请在 .env 文件中配置。"
    exit 1
fi

if [ -z "$LOCATION" ]; then
    echo "错误: 未设置 LOCATION。请在 .env 文件中配置。"
    exit 1
fi

if [ -z "$STAGING_BUCKET" ]; then
    echo "错误: 未设置 STAGING_BUCKET。请在 .env 文件中配置。"
    exit 1
fi

if [ -z "$AGENT_DISPLAY_NAME" ]; then
    echo "错误: 未设置 AGENT_DISPLAY_NAME。请在 .env 文件中配置。"
    exit 1
fi

if [ -z "$GE_APP_ID" ]; then
    echo "错误: 未设置 GE_APP_ID。请在 .env 文件中配置。"
    exit 1
fi

if [ -z "$GE_APP_LOCATION" ]; then
    echo "错误: 未设置 GE_APP_LOCATION。请在 .env 文件中配置。"
    exit 1
fi

echo "--- [0/4] 正在检查二进制文件(Lark CLI) ---"
if [ ! -f "nexus_agent/bin/lark-cli" ]; then
    echo "未发现 CLI 二进制文件，正在开始构建..."
    bash scripts/build_cli.sh
else
    echo "CLI 二进制文件已存在，跳过构建。"
fi

echo "--- [0/4] 正在检查二进制文件(Google Workspace CLI) ---"
if [ ! -f "nexus_agent/bin/gws" ]; then
    echo "错误: 未发现 Google Workspace CLI 二进制文件: nexus_agent/bin/gws"
    echo "请从 googleworkspace/cli release 下载 Linux amd64 版本并放入 nexus_agent/bin/gws。"
    exit 1
fi

echo "--- [1/4] 正在使用 UV 打包应用 ---"
# 清理旧的构建文件
rm -rf dist/ build/ *.egg-info
rm -f $DEPLOY_DIR/ge_nexus_agent-1.0.0-py3-none-any.whl
# uv build 会自动处理构建依赖
uv build --wheel --out-dir $DEPLOY_DIR
# 同步本地 venv（--no-editable 规避 uv 0.12.17 与 setuptools editable_wheel 的 EEXIST bug）
uv sync --no-editable

echo "--- [2/4] 正在部署到 Vertex AI Reasoning Engine ---"
# 在根目录运行部署脚本，确保模块导入 and PYTHONPATH 正确
# --no-sync：跳过 uv 的 editable 重建（同上 EEXIST bug），直接使用上一步同步好的 venv
uv run --no-sync python nexus_agent/deployment/deploy.py

# 重新加载部署生成的变量
source scripts/load_env.sh "$ENV_SUFFIX"
if [ -z "$GE_AGENT_RESOURCE_NAME" ] && [ ! -f "$DEPLOY_ENV_FILE" ]; then
    echo "错误: 部署失败，未能生成 $DEPLOY_ENV_FILE。"
    exit 1
fi

echo "--- [3/4] 正在同步到 Gemini Enterprise ---"
if [ -z "$GE_AGENT_RESOURCE_NAME" ]; then
    echo "识别到首次部署，正在执行注册流程..."
    bash "scripts/register_to_GE.sh"
else
    echo "识别到更新部署，正在更新 Agent 引擎..."
    bash "scripts/update_agent_GE.sh"
fi

# 重新加载部署生成的变量 (包含刚刚生成的 GE_AGENT_RESOURCE_NAME)
source scripts/load_env.sh "$ENV_SUFFIX"

# 分批授权一致性校验（2026-10-30）：
# 部署只更新 engine 绑定，不会改 authorizationConfig。若 agent 上绑的批次与
# LARK_AUTH_IDS 不一致，用户会「授权了却读不到 token」。此处只告警、不自动改，
# 避免部署流程擅自变更授权配置（config.sh 未失败中断，用 || 兜住 set -e）。
if [ -n "$LARK_AUTH_IDS" ] && [ -n "$GE_AGENT_RESOURCE_NAME" ]; then
    echo "--- 校验 Agent 授权绑定与 LARK_AUTH_IDS 是否一致 ---"
    bash scripts/check_ge_auth_binding.sh "$ENV_SUFFIX" || \
        echo "⚠️  警告: 授权绑定与 LARK_AUTH_IDS 不一致（见上方缺失项）。未自动修改，请人工修复后再验证授权。"
fi

# 清理旧的 Reasoning Engine 资源 (避免云端资源累积和持续计费)
if [ -n "$PREVIOUS_REASONING_ENGINE" ] && [ "$PREVIOUS_REASONING_ENGINE" != "$VERTEX_REASONING_ENGINE_NAME" ]; then
    echo "--- 正在清理历史 Reasoning Engine 资源 ---"
    echo "检测到旧的 Reasoning Engine: $PREVIOUS_REASONING_ENGINE"
    echo "正在安全删除旧实例以节省云端资源..."
    
    # 调用项目中已有的删除脚本，并允许忽略失败，确保部署主流程能顺利完成
    bash scripts/delete_reasoning_engine.sh "$PREVIOUS_REASONING_ENGINE" || echo "⚠️ 警告: 删除旧 Reasoning Engine 失败，您后续可以手动运行: bash scripts/delete_reasoning_engine.sh $PREVIOUS_REASONING_ENGINE 进行清理。"
fi

echo "--- [4/4] 部署完成！ ---"
echo "Reasoning Engine ID: $VERTEX_REASONING_ENGINE_NAME"
if [ -n "$GE_AGENT_RESOURCE_NAME" ]; then
    echo "Agent Resource Name: $GE_AGENT_RESOURCE_NAME"
    
    # 拼接控制台 URL
    AGENT_ID=$(basename "$GE_AGENT_RESOURCE_NAME")
    RE_ID=$(basename "$VERTEX_REASONING_ENGINE_NAME")
    # 提取 Reasoning Engine 的区域 (通常是 us-central1 等)
    RE_REGION=$(echo "$VERTEX_REASONING_ENGINE_NAME" | cut -d/ -f4)
    echo ""
    
    echo -e "\n--- 控制台快捷访问地址 ---"
    # 使用正确的变量和格式
    echo "Gemini Enterprise App: https://console.cloud.google.com/gemini-enterprise/locations/${GE_APP_LOCATION}/engines/${GE_APP_ID}/overview/dashboard?project=${PROJECT_ID}"
    echo ""
    # Reasoning Engine (Vertex AI)
    echo "Reasoning Engine (Vertex AI): https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/${RE_REGION}/agent-engines/${RE_ID}/dashboard?project=${PROJECT_ID}"
    echo -e "\n您可以点击上方链接前往 Gemini Enterprise 控制台体验部署的 Agent。"
else
    echo -e "\n警告: 未检测到 GE_AGENT_RESOURCE_NAME。这可能意味着 Agent 注册失败，或者环境变量未正确同步。"
    echo "请检查 .deploy_env 文件并确保 Agent 资源已正确创建。"
    
    # 即使 Agent 注册有问题，通常 Reasoning Engine 已经好了，可以单独打出它的链接作为备选
    if [ -n "$VERTEX_REASONING_ENGINE_NAME" ]; then
        RE_ID=$(basename "$VERTEX_REASONING_ENGINE_NAME")
        RE_REGION=$(echo "$VERTEX_REASONING_ENGINE_NAME" | cut -d/ -f4)
        echo -e "\n--- Reasoning Engine (Vertex AI) 快捷访问地址 ---"
        echo "https://console.cloud.google.com/vertex-ai/agents/agent-engines/locations/${RE_REGION}/agent-engines/${RE_ID}/dashboard?project=${PROJECT_ID}"
    fi
fi
echo ""
