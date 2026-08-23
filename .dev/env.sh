#!/usr/bin/env bash
# 跑任何需要调模型的东西之前，必须先 source 这个文件。
#
# 为什么单独抽出来：run.sh 原先直接调 python3，没有代理变量也没有
# node/CLI 的 PATH。国内直连 api.anthropic.com 会拿到
# "403 Request not allowed"，而 SDK 把它包成一条 is_error 的
# ResultMessage —— 看起来和额度耗尽一模一样。我因此花了好几轮
# 去查"额度"，真正的原因只是启动脚本没配代理。
#
# WSL 是 NAT 模式，用不了 Windows 侧的 localhost 代理，必须走网关地址。
export PATH="$HOME/.local/node/bin:$PATH"
GW=$(ip route | awk '/^default/ {print $3; exit}')
PROXY_PORT="${PGDOCTOR_PROXY_PORT:-7890}"
export http_proxy="http://$GW:$PROXY_PORT"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"
export no_proxy="localhost,127.0.0.1,::1,172.17.0.0/16,$GW"
