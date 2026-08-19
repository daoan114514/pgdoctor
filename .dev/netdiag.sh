#!/usr/bin/env bash
GW=$(ip route | awk '/^default/ {print $3; exit}')
echo "WSL 网关(即 Windows 主机): $GW"

echo "=== 网关 7890 端口是否可达 ==="
if timeout 5 bash -c "cat < /dev/null > /dev/tcp/$GW/7890" 2>/dev/null; then
  echo "  可达 —— 代理已开放局域网"
  REACHABLE=1
else
  echo "  不可达 —— 代理只监听 127.0.0.1，未开放局域网"
  REACHABLE=0
fi

if [ "$REACHABLE" = "1" ]; then
  echo "=== 经代理访问测试 ==="
  for t in https://api.anthropic.com/v1/messages https://console.anthropic.com; do
    code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 -x "http://$GW:7890" "$t" 2>&1)
    echo "  $t -> ${code:-失败}"
  done
fi

echo "=== 直连（对照）==="
code=$(curl -s -o /dev/null -w '%{http_code}' --max-time 8 -4 https://api.anthropic.com/v1/messages 2>&1)
echo "  强制 IPv4 直连 -> ${code:-失败}"

echo "=== WSL IPv6 路由 ==="
ip -6 route show default 2>/dev/null | head -2 || echo "  (无默认 IPv6 路由)"

echo "GW=$GW"
