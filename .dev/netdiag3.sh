#!/usr/bin/env bash
echo "=== 403 的完整响应体（正常与否的关键）==="
curl -s -4 --max-time 15 https://api.anthropic.com/v1/messages 2>&1 | head -3
echo

echo "=== IPv4 vs IPv6 对照 ==="
c4=$(curl -s -4 -o /dev/null -w '%{http_code} %{time_total}s' --max-time 12 https://console.anthropic.com/ 2>&1)
echo "  IPv4: ${c4:-失败}"
c6=$(curl -s -6 -o /dev/null -w '%{http_code} %{time_total}s' --max-time 12 https://console.anthropic.com/ 2>&1)
echo "  IPv6: ${c6:-失败}"

echo
echo "=== 默认（不指定协议族）==="
cd_=$(curl -s -o /dev/null -w '%{http_code} %{time_total}s' --max-time 12 https://console.anthropic.com/ 2>&1)
echo "  默认: ${cd_:-失败}"

echo
echo "=== node 的 DNS 行为 ==="
export PATH="$HOME/.local/node/bin:$PATH"
node -e "
const dns=require('dns');
dns.lookup('console.anthropic.com',{all:true},(e,a)=>{
  console.log('  默认 lookup:', e? String(e) : JSON.stringify(a));
});
" 2>&1 | head -3

echo "=== node 强制 ipv4first ==="
NODE_OPTIONS=--dns-result-order=ipv4first node -e "
const https=require('https');
const r=https.get('https://console.anthropic.com/',res=>{
  console.log('  状态码:',res.statusCode); res.destroy();
});
r.on('error',e=>console.log('  错误:',e.code||String(e)));
r.setTimeout(12000,()=>{console.log('  超时');r.destroy();});
" 2>&1 | head -3

echo "=== node 默认（对照）==="
node -e "
const https=require('https');
const r=https.get('https://console.anthropic.com/',res=>{
  console.log('  状态码:',res.statusCode); res.destroy();
});
r.on('error',e=>console.log('  错误:',e.code||String(e)));
r.setTimeout(12000,()=>{console.log('  超时');r.destroy();});
" 2>&1 | head -3
