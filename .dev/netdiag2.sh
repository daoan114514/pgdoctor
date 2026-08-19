#!/usr/bin/env bash
echo "=== api.anthropic.com 403 的具体内容（IPv4 直连）==="
curl -s -4 --max-time 15 -i https://api.anthropic.com/v1/messages 2>&1 | head -14

echo
echo "=== 其他端点（IPv4 直连）==="
for u in https://console.anthropic.com/ https://claude.ai/ https://statsig.anthropic.com/; do
  code=$(curl -s -4 -o /dev/null -w '%{http_code}' --max-time 12 "$u" 2>&1)
  echo "  ${code:-失败}  $u"
done

echo
echo "=== 解析到的 IPv4 地址 ==="
getent ahostsv4 api.anthropic.com | head -3

echo
echo "=== 是否被 CF 拦（看响应头）==="
curl -s -4 --max-time 12 -I https://api.anthropic.com/ 2>&1 | grep -iE '^(HTTP|cf-|server|x-)' | head -8
