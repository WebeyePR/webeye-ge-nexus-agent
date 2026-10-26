#!/usr/bin/env bash
# ==============================================================================
# 本地端到端冒烟测试：起 api 服务 → SSE 流式 → 工具调用 → 最终回复 全链路自检
#
# 用法:
#   ./scripts/test.local.sh              # 默认端口 8399
#   PORT=9000 ./scripts/test.local.sh
#
# 依赖: .env / ADC 凭证（gcloud auth application-default login）
# 注: ADK 2.13 的运行端点为顶层 /run 与 /run_sse（无 /apps/{app} 前缀）
# ==============================================================================
set -euo pipefail

L_SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
L_ROOT_DIR="$(dirname "$L_SCRIPT_DIR")"
cd "$L_ROOT_DIR"

PORT="${PORT:-8399}"
BASE_URL="http://127.0.0.1:${PORT}"
curl_noproxy() { curl -s --noproxy '*' "$@"; }
LOG="/tmp/adk_local_test.$$.log"
SSE_OUT="/tmp/adk_sse_test.$$.out"
PY="${PYTHON:-.venv/bin/python}"

cleanup() { [ -n "${SERVER_PID:-}" ] && kill "$SERVER_PID" 2>/dev/null || true; rm -f "$LOG"; }
trap cleanup EXIT

echo "==> [1/5] 启动本地 api 服务（端口 ${PORT}）..."
PORT="$PORT" bash "$L_SCRIPT_DIR/run.local.sh" api > "$LOG" 2>&1 &
SERVER_PID=$!

echo "==> [2/5] 等待服务就绪..."
APPS=""
for _ in $(seq 1 60); do
  APPS=$(curl_noproxy "$BASE_URL/list-apps" 2>/dev/null || true)
  [ -n "$APPS" ] && break
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "[×] 服务进程异常退出，日志尾部：" >&2; tail -20 "$LOG" >&2; exit 1
  fi
  sleep 1
done
echo "    已加载 agent: $APPS"
echo "$APPS" | grep -q nexus_agent || { echo "[×] 未发现 nexus_agent"; tail -20 "$LOG" >&2; exit 1; }

echo "==> [3/5] 创建会话并触发 SSE 流式 + 工具调用..."
SID=$(curl_noproxy -X POST "$BASE_URL/apps/nexus_agent/users/local-smoke/sessions" \
  -H 'Content-Type: application/json' -d '{"state":{}}' | "$PY" -c "import sys,json;print(json.load(sys.stdin)['id'])")
echo "    session=$SID"

curl_noproxy -N -X POST "$BASE_URL/run_sse" -H 'Content-Type: application/json' \
  -d "{\"appName\":\"nexus_agent\",\"userId\":\"local-smoke\",\"sessionId\":\"$SID\",\"newMessage\":{\"role\":\"user\",\"parts\":[{\"text\":\"请调用 discover_lark_operations 工具查询本地注册的飞书操作命令总数，然后用一句话告诉我结果\"}]},\"streaming\":true}" \
  --max-time 180 > "$SSE_OUT" 2>&1

echo "==> [4/5] 校验结果..."
"$PY" - "$SSE_OUT" <<'EOF'
import json, sys

partial, tool_calls, final = 0, 0, None
for line in open(sys.argv[1]):
    line = line.strip()
    if not line.startswith('data:'):
        continue
    try:
        ev = json.loads(line[5:])
    except Exception:
        continue
    if ev.get('partial'):
        partial += 1
    for part in (ev.get('content') or {}).get('parts', []):
        if 'functionCall' in part:
            tool_calls += 1
        if not ev.get('partial') and part.get('text'):
            final = part['text']

print(f"    partial 流式事件: {partial}")
print(f"    工具调用次数:     {tool_calls}")
print(f"    最终回复:         {(final or '(无)')[:200]}")

ok = True
if partial == 0:
    print("[×] 未捕获到流式 partial 事件（流式失效！）"); ok = False
if tool_calls == 0:
    print("[×] 未捕获到工具调用"); ok = False
if not final:
    print("[×] 未捕获到最终回复"); ok = False
sys.exit(0 if ok else 1)
EOF

echo "==> [5/5] ✅ 冒烟测试全部通过（流式 ✓ 工具调用 ✓ 最终回复 ✓）"
