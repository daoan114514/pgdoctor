#!/usr/bin/env bash
# 守护入口（在 WSL 里跑）：代理与 PATH 显式导出，不依赖 ~/.bashrc（非交互 shell 会早退）。
# 后台启动：setsid nohup bash .dev/run_guard.sh >/dev/null 2>&1 < /dev/null &
R="$(cd "$(dirname "$0")/.." && pwd)"
cd "$R" || exit 1
GW=$(ip route | awk '/^default/ {print $3; exit}')
PROXY="${PGDOCTOR_PROXY:-http://$GW:7890}"
export http_proxy="$PROXY" https_proxy="$PROXY" HTTP_PROXY="$PROXY" HTTPS_PROXY="$PROXY"
export no_proxy="localhost,127.0.0.1,::1,172.17.0.0/16,$GW"
export PATH="$HOME/.local/node/bin:$PATH"
export PYTHONPATH="$R" PYTHONUTF8=1 PYTHONIOENCODING=utf-8

# 计费方式：有 ~/.config/pgdoctor/anthropic.env（内容为 export ANTHROPIC_API_KEY=...）就走 API key，
# 没有就走 claude.ai 订阅额度。密钥只放家目录、权限 600，不进仓库、不打印。
ENV_FILE="$HOME/.config/pgdoctor/anthropic.env"
[ -f "$ENV_FILE" ] && . "$ENV_FILE"
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then MODE="API key"; else MODE="claude.ai 订阅额度"; fi
mkdir -p "$R/traces/eval_guard"
echo "[$(date '+%F %T')] run_guard: 计费方式 = $MODE" >> "$R/traces/eval_guard/guard.log"
exec python3 "$R/.dev/eval_guard.py"
