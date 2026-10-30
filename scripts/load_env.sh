#!/bin/bash
# ==============================================================================
# 自动加载项目环境变量 (.env 和 .deploy_env)
# ==============================================================================

# 重置部署脚本动态管理的内存生成变量，杜绝当前 Shell 终端进程的环境变量残留污染。
# 确保每次加载皆以物理配置文件 (.deploy_env) 为绝对的唯一真相来源。
unset VERTEX_REASONING_ENGINE_NAME
unset GE_AGENT_RESOURCE_NAME

# 获取脚本所在目录的绝对路径
L_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# 获取项目根目录 (假设此脚本在 scripts/ 目录下)
L_ROOT_DIR="$(dirname "$L_SCRIPT_DIR")"

# 环境后缀**只认 ENV_SUFFIX 变量**（调用方先 export），不再读取 $1。
#
# 为什么不再读 $1（2026-10-30 实测缺陷）：
#   `source` 时 $1 是**调用方脚本**的位置参数，不是 load_env.sh 的参数。于是
#     delete_authorization.sh ge-nexus-feishu-1
#   会让 $1=ge-nexus-feishu-1 被当成环境后缀 → 去找 .env-ge-nexus-feishu-1（不存在）
#   → PROJECT_ID 为空 → 删除 URL 变成 projects//locations/global/authorizations/... 
#   → API 返回 400 RESOURCE_PROJECT_INVALID（看着像「删不掉 / 已不存在」）。
#   同类受害者：delete_agent.sh（Agent 名）、delete_reasoning_engine.sh（引擎名）、
#   run.local.sh（web/api/run 被当后缀）。
#   需要切环境的脚本（deploy.sh / cleanup_deployed.sh 等）都在 source 前先设 ENV_SUFFIX，
#   因此改为只读该变量不损失任何能力；`source ... "$ENV_SUFFIX"` 的旧写法传参会被忽略，向后兼容。
ENV_SUFFIX="${ENV_SUFFIX:-}"

BASE_ENV_NAME=".env"
DEPLOY_ENV_NAME=".deploy_env"

if [ -n "$ENV_SUFFIX" ]; then
    BASE_ENV_NAME=".env-${ENV_SUFFIX}"
    DEPLOY_ENV_NAME=".deploy_env-${ENV_SUFFIX}"
fi

# 只吃后缀变量时，最怕「后缀拼错/不存在」变成静默空配置（PROJECT_ID 为空只会在
# 真正发请求时暴露成 400）。这里显式告警，把问题前移到加载阶段。
if [ -n "$ENV_SUFFIX" ] && [ ! -f "$L_ROOT_DIR/$BASE_ENV_NAME" ]; then
    echo "⚠️  环境后缀 '$ENV_SUFFIX' 的配置文件不存在：$L_ROOT_DIR/$BASE_ENV_NAME（后续可能缺关键变量）" >&2
fi

# 导出当前的物理配置文件绝对路径，供 Python 和其他脚本使用，确保全链路读写同一物理文件
export BASE_ENV_FILE="$L_ROOT_DIR/$BASE_ENV_NAME"
export DEPLOY_ENV_FILE="$L_ROOT_DIR/$DEPLOY_ENV_NAME"
export ENV_SUFFIX

# 如果当前不在根目录，且根目录存在相应配置文件，则加载
# 使用 set -a 确保所有加载的变量都会被自动 export

# 1. 加载基础配置
if [ -f "$BASE_ENV_FILE" ]; then
    set -a
    source "$BASE_ENV_FILE"
    set +a
fi

# 2. 加载部署生成配置
if [ -f "$DEPLOY_ENV_FILE" ]; then
    set -a
    source "$DEPLOY_ENV_FILE"
    set +a
fi

# 如果还是没有加载到必要变量，尝试直接在当前目录查找 (以防 CWD 变化且不匹配 L_ROOT_DIR)
if [ -z "$PROJECT_ID" ]; then
    if [ -f "$BASE_ENV_NAME" ]; then
        set -a
        source "$BASE_ENV_NAME"
        set +a
    fi
    if [ -f "$DEPLOY_ENV_NAME" ]; then
        set -a
        source "$DEPLOY_ENV_NAME"
        set +a
    fi
fi
