#!/usr/bin/env bash
# 下载当前平台专用的 CLI 二进制（仅供本地开发/真实测试）。
# 云部署仍使用 nexus_agent/bin/ 下的 Linux x86-64 版本（随 wheel 发布）。
#
# 用法: bash scripts/fetch_local_binaries.sh
#
# 说明:
# - 目标目录 nexus_agent/bin/<os>-<arch>/ 已在 .gitignore 中，不入库
# - 全部经官方 SHA-256 校验后才落位，校验失败即中止
# - 资产名以各仓库 release 实际资产为准（2026-10-27 核实）：
#     larksuite/cli      → lark-cli-<ver>-<os>-<arch>.tar.gz + checksums.txt
#     googleworkspace/cli → google-workspace-cli-<arch>-<os>-<os>.tar.gz + .sha256
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
OS="$(uname -s | tr '[:upper:]' '[:lower:]')"
ARCH_RAW="$(uname -m)"
DEST="$ROOT/nexus_agent/bin/$OS-$ARCH_RAW"

# 架构命名归一：lark 用 amd64/arm64，gws 用 x86_64/aarch64
case "$ARCH_RAW" in
    arm64)  LARK_ARCH=arm64 ;;
    x86_64) LARK_ARCH=amd64 ;;
    *) echo "不支持的架构: $ARCH_RAW" >&2; exit 1 ;;
esac

LARK_VERSION="${LARK_VERSION:-1.0.98}"
GWS_VERSION="${GWS_VERSION:-0.22.5}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

mkdir -p "$DEST"
echo "目标目录: $DEST"

verify_sha256() {
    # $1 文件  $2 期望的 sha256 十六进制串
    local actual
    if command -v sha256sum >/dev/null 2>&1; then
        actual="$(sha256sum "$1" | awk '{print $1}')"
    else
        actual="$(shasum -a 256 "$1" | awk '{print $1}')"
    fi
    if [[ "$actual" != "$2" ]]; then
        echo "✗ SHA-256 不匹配" >&2
        echo "  期望: $2" >&2
        echo "  实际: $actual" >&2
        exit 1
    fi
    echo "  SHA-256 校验通过"
}

# ---------- lark-cli ----------
LARK_ASSET="lark-cli-${LARK_VERSION}-${OS}-${LARK_ARCH}.tar.gz"
LARK_URL="https://github.com/larksuite/cli/releases/download/v${LARK_VERSION}/${LARK_ASSET}"
echo ""
echo "==> lark-cli v${LARK_VERSION} (${OS}-${LARK_ARCH})"
if [[ ! -f "$DEST/lark-cli" ]] || [[ "${FORCE:-0}" == "1" ]]; then
    curl -sSL -f -o "$TMP/$LARK_ASSET" "$LARK_URL"
    EXPECTED="$(curl -sSL -f "https://github.com/larksuite/cli/releases/download/v${LARK_VERSION}/checksums.txt" \
        | grep -F "$LARK_ASSET" | awk '{print $1}')"
    if [[ -z "$EXPECTED" ]]; then
        echo "✗ 未能从 checksums.txt 获取期望校验值" >&2; exit 1
    fi
    verify_sha256 "$TMP/$LARK_ASSET" "$EXPECTED"
    tar -xzf "$TMP/$LARK_ASSET" -C "$TMP"
    # 归档内可能是 lark-cli 或带目录前缀，统一找可执行文件
    BIN="$(find "$TMP" -maxdepth 2 -type f -name 'lark-cli' | head -1)"
    [[ -n "$BIN" ]] || { echo "✗ 归档内未找到 lark-cli" >&2; exit 1; }
    install -m 0755 "$BIN" "$DEST/lark-cli"
    echo "  ✓ $DEST/lark-cli"
else
    echo "  已存在，跳过（FORCE=1 可强制重下）"
fi

# ---------- gws (google-workspace-cli) ----------
case "$ARCH_RAW" in
    arm64)  GWS_ARCH=aarch64 ;;
    x86_64) GWS_ARCH=x86_64 ;;
esac
GWS_ASSET="google-workspace-cli-${GWS_ARCH}-apple-darwin.tar.gz"
[[ "$OS" == "linux" ]] && GWS_ASSET="google-workspace-cli-${GWS_ARCH}-unknown-linux-gnu.tar.gz"
GWS_URL="https://github.com/googleworkspace/cli/releases/download/v${GWS_VERSION}/${GWS_ASSET}"
echo ""
echo "==> gws v${GWS_VERSION} (${GWS_ARCH}-${OS})"
if [[ ! -f "$DEST/gws" ]] || [[ "${FORCE:-0}" == "1" ]]; then
    curl -sSL -f -o "$TMP/$GWS_ASSET" "$GWS_URL"
    curl -sSL -f -o "$TMP/$GWS_ASSET.sha256" "${GWS_URL}.sha256"
    # .sha256 内容形如 "<hex>  <filename>"
    EXPECTED="$(awk '{print $1}' "$TMP/$GWS_ASSET.sha256")"
    verify_sha256 "$TMP/$GWS_ASSET" "$EXPECTED"
    tar -xzf "$TMP/$GWS_ASSET" -C "$TMP"
    BIN="$(find "$TMP" -maxdepth 2 -type f -name 'gws' | head -1)"
    [[ -n "$BIN" ]] || { echo "✗ 归档内未找到 gws" >&2; exit 1; }
    install -m 0755 "$BIN" "$DEST/gws"
    echo "  ✓ $DEST/gws"
else
    echo "  已存在，跳过（FORCE=1 可强制重下）"
fi

echo ""
echo "完成。本地 CLI 真实测试现在可用了："
echo "  .venv/bin/python -c \"from nexus_agent.infrastructure.cli_client import CLIClient; print(CLIClient().bin_path)\""
