#!/usr/bin/env bash
# 离线回归：改了安全相关的代码就跑一遍，确认旧防线没被放松。
cd "$(dirname "$0")/.." || exit 9
fail=0
for t in shield_check esc_check session_unit declare_unit w7_check coverage_check multicause_check evo_fix_check evo_check struct_check score_align_check misleading_check; do
  out=$(python3 ".dev/$t.py" 2>&1 | tail -1)
  case "$out" in
    *PASS*) printf '  PASS  %-18s %s\n' "$t" "$out" ;;
    *)      printf '  FAIL  %-18s %s\n' "$t" "$out"; fail=1 ;;
  esac
done
echo
[ "$fail" = 0 ] && echo "REGRESSION: PASS" || echo "REGRESSION: FAIL"
exit "$fail"
