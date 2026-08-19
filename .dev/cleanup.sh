#!/usr/bin/env bash
for p in $(pgrep -f "pgdoctor/_run" 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
for p in $(pgrep -f "sandbox.workload" 2>/dev/null); do kill -9 "$p" 2>/dev/null; done
sleep 1
echo "残留进程: $(pgrep -cf 'pgdoctor/_run|sandbox.workload' 2>/dev/null || echo 0)"
rm -f /home/daoan/pgdoctor/_run.py /home/daoan/pgdoctor/.dev/w2env.done
