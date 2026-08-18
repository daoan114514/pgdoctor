#!/usr/bin/env bash
# 起沙箱并等 init 跑完（seed 完成后才算 ready）
D="$(cd "$(dirname "$0")" && pwd)"
rm -f "$D/up.log" "$D/up.done"
cd "$D/../docker" || exit 9
{
  echo "=== SEED_ORDERS=${SEED_ORDERS:-unset} ==="
  docker compose up -d
  echo "=== waiting for init (seed) to finish ==="
  for i in $(seq 1 180); do
    if docker compose logs pg 2>/dev/null | grep -q 'database system is ready to accept connections'; then
      if docker compose logs pg 2>/dev/null | grep -q '\[seed\] done\.'; then
        echo "SEED DONE"; break
      fi
    fi
    sleep 5
  done
  docker compose logs pg 2>&1 | grep -E '\[seed\]|PENDING|DELIVERED|ERROR|FATAL' | tail -20
} > "$D/up.log" 2>&1
echo $? > "$D/up.done"
