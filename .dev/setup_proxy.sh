#!/usr/bin/env bash
# 让 WSL 走 Windows 主机上的代理。
#
# 背景：Anthropic 对国内出口 IP 返回 403 "Request not allowed"。
# Windows 侧浏览器能用是因为走了 127.0.0.1:7890 的代理，而 WSL2 是独立
# NAT 网络，默认不继承 Windows 的代理设置，所以 CLI 登录会 ECONNRESET。
#
# 前提：代理客户端必须开启"允许局域网连接"(Allow LAN)，否则它只监听
# 127.0.0.1，WSL 通过网关地址够不着。

set -uo pipefail
PORT="${1:-7890}"
GW=$(ip route | awk '/^default/ {print $3; exit}')
PROXY="http://$GW:$PORT"

echo "网关(Windows 主机): $GW"
echo "代理地址: $PROXY"

echo "=== 连通性检查 ==="
if ! timeout 5 bash -c "cat < /dev/null > /dev/tcp/$GW/$PORT" 2>/dev/null; then
  echo "  ✗ $GW:$PORT 不可达"
  echo "    请在代理客户端里开启 [允许局域网连接 / Allow LAN] 后重试"
  exit 1
fi
echo "  ✓ 端口可达"

echo "=== 经代理访问 Anthropic ==="
body=$(curl -s --max-time 20 -x "$PROXY" https://api.anthropic.com/v1/messages 2>&1)
echo "  响应: $(echo "$body" | tr -d '\n' | head -c 120)"
if echo "$body" | grep -q "Request not allowed"; then
  echo "  ✗ 仍被地区封锁 —— 代理节点可能也在受限区域，换个节点再试"
  exit 2
fi

echo "=== 写入 ~/.bashrc ==="
MARK="# --- pgdoctor proxy ---"
sed -i "/$MARK/,+5d" "$HOME/.bashrc" 2>/dev/null
cat >> "$HOME/.bashrc" <<EOF
$MARK
export http_proxy="$PROXY"
export https_proxy="$PROXY"
export HTTP_PROXY="$PROXY"
export HTTPS_PROXY="$PROXY"
export no_proxy="localhost,127.0.0.1,::1,172.17.0.0/16,$GW"
EOF
echo "  已写入（新开的 shell 自动生效）"

echo
echo "完成。现在可以在 WSL 里登录："
echo "  source ~/.bashrc && claude auth login"
