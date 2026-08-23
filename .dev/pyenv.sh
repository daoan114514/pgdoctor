#!/usr/bin/env bash
# 带完整环境跑一个 python 脚本：node/CLI 在 PATH 上，代理变量就位。
# 用法: .dev/pyenv.sh <脚本> [参数...]
D="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
. "$D/env.sh"
cd "$D/.." || exit 9
exec python3 "$@"
