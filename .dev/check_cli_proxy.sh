#!/usr/bin/env bash
export PATH="$HOME/.local/node/bin:$PATH"
GW=$(ip route | awk '/^default/ {print $3; exit}')
export http_proxy="http://$GW:7890"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"

echo "node 版本: $(node --version)"
echo

echo "=== node fetch + NODE_USE_ENV_PROXY=1 ==="
NODE_USE_ENV_PROXY=1 node -e "
fetch('https://api.anthropic.com/v1/messages')
  .then(r=>r.text().then(t=>console.log('  状态',r.status,'|',t.replace(/\s+/g,' ').slice(0,70))))
  .catch(e=>console.log('  错误:',e.cause?.code||e.message));
" 2>&1 | head -3

echo
echo "=== node undici ProxyAgent（显式走代理，验证链路可用）==="
node -e "
const {ProxyAgent, setGlobalDispatcher} = require('undici');
setGlobalDispatcher(new ProxyAgent(process.env.HTTPS_PROXY));
fetch('https://api.anthropic.com/v1/messages')
  .then(r=>r.text().then(t=>console.log('  状态',r.status,'|',t.replace(/\s+/g,' ').slice(0,70))))
  .catch(e=>console.log('  错误:',e.cause?.code||e.message));
" 2>&1 | head -3

echo
echo "=== claude CLI 里与代理/网络相关的选项 ==="
timeout 30 claude --help 2>&1 | grep -iE "proxy|doctor|diagnos" | head -6
echo
echo "=== claude doctor（若存在）==="
timeout 60 claude doctor 2>&1 | head -12
