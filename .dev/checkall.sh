#!/usr/bin/env bash
# 跑一遍离线验收脚本（不需要 API）。
cd "$(dirname "$0")/.." || exit 9
export PYTHONPATH="$PWD"
fail=0
for f in .dev/shield_check.py .dev/esc_check.py .dev/evo_check.py \
         .dev/gate_check.py .dev/check_gate_reject.py .dev/check_lock_safe.py \
         .dev/w7_check.py .dev/coverage_check.py .dev/check_rollback_markers.py; do
  [ -f "$f" ] || continue
  printf '%-32s ' "$f"
  out="$(timeout 180 python3 "$f" 2>&1)"
  code=$?
  echo "$out" | tail -1 | cut -c1-90
  if [ "$code" -ne 0 ]; then
    fail=$((fail + 1))
    echo "$out" | tail -8 | sed 's/^/     /'
  fi
done
echo "----"
echo "失败脚本数: $fail"
