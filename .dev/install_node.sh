#!/usr/bin/env bash
# 在用户目录装 node + Claude Code CLI，不需要 sudo，全程走国内镜像。
set -uo pipefail
D="$(cd "$(dirname "$0")" && pwd)"
LOG="$D/node.log"
: > "$LOG"
exec > >(tee -a "$LOG") 2>&1

MIRROR="https://npmmirror.com/mirrors/node"
PREFIX="$HOME/.local/node"

echo "=== 查最新 LTS 版本 ==="
VER=$(curl -s --max-time 30 "$MIRROR/index.json" \
      | python3 -c "
import json,sys
d=json.load(sys.stdin)
lts=[x for x in d if x.get('lts')]
print(lts[0]['version'] if lts else '')
" 2>/dev/null)
if [ -z "$VER" ]; then
  echo '索引获取失败，回落到已知 LTS'
  VER="v22.20.0"
fi
echo "版本: $VER"

TARBALL="node-${VER}-linux-x64.tar.xz"
echo "=== 下载 $TARBALL ==="
curl -fL --max-time 600 -o "/tmp/$TARBALL" "$MIRROR/${VER}/${TARBALL}" || {
  echo "下载失败"; exit 1; }
ls -lh "/tmp/$TARBALL"

echo "=== 解压到 $PREFIX ==="
rm -rf "$PREFIX"; mkdir -p "$PREFIX"
tar -xJf "/tmp/$TARBALL" -C "$PREFIX" --strip-components=1
rm -f "/tmp/$TARBALL"

export PATH="$PREFIX/bin:$PATH"
echo "node: $(node --version)  npm: $(npm --version)"

echo "=== 配置 npm 国内源 ==="
npm config set registry https://registry.npmmirror.com

echo "=== 安装 Claude Code CLI ==="
npm install -g @anthropic-ai/claude-code 2>&1 | tail -8

echo "=== 写入 PATH（~/.bashrc）==="
LINE='export PATH="$HOME/.local/node/bin:$PATH"'
grep -qF "$LINE" "$HOME/.bashrc" 2>/dev/null || echo "$LINE" >> "$HOME/.bashrc"

echo "=== 结果 ==="
which node npm claude 2>&1
claude --version 2>&1 | head -2
