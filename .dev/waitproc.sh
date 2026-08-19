#!/usr/bin/env bash
# 轮询进程退出，而不是依赖 .done 文件（启动器里的 $? 会被外层 shell 提前展开）
pat="$1"; limit="${2:-60}"; i=0
while [ $i -lt "$limit" ]; do
  if ! ps -eo cmd --no-headers | grep -F -- "$pat" | grep -qv grep; then
    echo "PROCESS EXITED"; exit 0
  fi
  sleep 10; i=$((i+1))
done
echo "STILL RUNNING after $((limit*10))s"
