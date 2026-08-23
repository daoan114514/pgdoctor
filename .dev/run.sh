#!/usr/bin/env bash
# 脱离式跑一个项目内脚本。用法: .dev/run.sh <脚本路径> [日志名]
#
# 为什么要这个：
#   1. WSL 里长任务会随调用进程被杀，必须 setsid 脱离
#   2. 直接在命令行拼 `echo $? > x.done` 会被外层 shell 提前展开成字面量，
#      退出码恒为 0（踩过）。写成脚本文件才拿得到真实退出码
#   3. PYTHONPATH 指向项目根，.dev/ 下的脚本 import agent/sandbox 才找得到
#      —— Python 只会把脚本自身所在目录加进 sys.path
D="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$D/.." && pwd)"
#   4. 第一个参数允许带命令行参数（如 "eval/run_suite.py --split eval"），
#      所以下面展开 $SCRIPT 时不能加引号 —— 加了会被当成一个含空格的
#      文件名，报 "No such file or directory"（踩过）
SCRIPT="$1"
NAME="${2:-$(basename "${SCRIPT%% *}" .py)}"

rm -f "$D/$NAME.log" "$D/$NAME.done"
cd "$ROOT" || exit 9

PYTHONPATH="$ROOT" setsid bash -c \
  "python3 $SCRIPT > '$D/$NAME.log' 2>&1; echo \$? > '$D/$NAME.done'" \
  < /dev/null > /dev/null 2>&1 &

sleep 2
echo "started: $SCRIPT -> .dev/$NAME.log"
