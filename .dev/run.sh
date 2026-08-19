#!/usr/bin/env bash
# 脱离式跑一个项目内脚本。用法: .dev/run.sh <脚本路径> [日志名]
#
# 为什么要这个：WSL 里长任务会随调用进程被杀，必须 setsid 脱离；
# 而直接在命令行里拼 `echo $? > x.done` 会被外层 shell 提前展开成字面量，
# 导致退出码永远是 0。写成脚本文件才能拿到真实退出码。
D="$(cd "$(dirname "$0")" && pwd)"
SCRIPT="$1"; NAME="${2:-$(basename "$SCRIPT" .py)}"
rm -f "$D/$NAME.log" "$D/$NAME.done"
cd "$D/.." || exit 9
setsid bash -c "python3 '$SCRIPT' > '$D/$NAME.log' 2>&1; echo \$? > '$D/$NAME.done'" \
  < /dev/null > /dev/null 2>&1 &
sleep 2
echo "started: $SCRIPT -> .dev/$NAME.log"
