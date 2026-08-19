#!/usr/bin/env bash
export PATH="$HOME/.local/node/bin:$PATH"
GW=$(ip route | awk '/^default/ {print $3; exit}')
export http_proxy="http://$GW:7890"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"

echo "代理: $https_proxy"
echo
echo "=== curl 经代理（对照，应为 405）==="
curl -s --max-time 20 https://api.anthropic.com/v1/messages | tr -d '\n' | head -c 100
echo
echo
echo "=== node 原生 https（默认不读 proxy 变量，预期被封）==="
node -e "
const https=require('https');
const r=https.get('https://api.anthropic.com/v1/messages',res=>{
  let b='';res.on('data',d=>b+=d);
  res.on('end',()=>console.log('  状态',res.statusCode,'|',b.slice(0,80)));
});
r.on('error',e=>console.log('  错误:',e.code||String(e)));
r.setTimeout(20000,()=>{console.log('  超时');r.destroy();});
" 2>&1 | head -3

echo
echo "=== claude CLI 是否能出网 ==="
timeout 60 claude auth status 2>&1 | head -6
