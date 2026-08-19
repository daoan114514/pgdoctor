#!/usr/bin/env bash
# 带完整环境跑一个 python 脚本：node/CLI 在 PATH 上，代理变量就位。
# 用法: .dev/pyenv.sh <脚本> [参数...]
export PATH="$HOME/.local/node/bin:$PATH"
GW=$(ip route | awk '/^default/ {print $3; exit}')
export http_proxy="http://$GW:7890"
export https_proxy="$http_proxy"
export HTTP_PROXY="$http_proxy"
export HTTPS_PROXY="$http_proxy"
export no_proxy="localhost,127.0.0.1,::1,172.17.0.0/16,$GW"
cd "$(dirname "$0")/.." || exit 9
exec python3 "$@"
