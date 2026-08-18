#!/usr/bin/env bash
# 脱离式构建：WSL 里长任务会随调用进程被杀，且 /tmp 会被清空，
# 所以脚本与日志都放项目内，用 setsid 脱离，靠 .done 文件判完成。
D="$(cd "$(dirname "$0")" && pwd)"
rm -f "$D/build.log" "$D/build.done"
cd "$D/../docker" || exit 9
docker compose build --progress plain > "$D/build.log" 2>&1
echo $? > "$D/build.done"
