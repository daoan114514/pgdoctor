#!/usr/bin/env bash
# 跑一遍离线验收脚本（不需要 API）。
cd "$(dirname "$0")/.." || exit 9
export PYTHONPATH="$PWD"
export PYTHONUTF8=1
export PYTHONIOENCODING=utf-8
# WSL does not forward ordinary environment variables to Windows executables
# unless they are named in WSLENV.  The fallback interpreter below is a
# Windows Python on this workstation, so carry the encoding controls across.
export WSLENV="${WSLENV:+$WSLENV:}PYTHONUTF8:PYTHONIOENCODING"
python_bin="${PGDOCTOR_PYTHON:-python3}"
if ! "$python_bin" -c 'import networkx, pglast, psycopg' >/dev/null 2>&1; then
  if command -v python.exe >/dev/null 2>&1 &&
     python.exe -c 'import networkx, pglast, psycopg' >/dev/null 2>&1; then
    python_bin="python.exe"
  fi
fi
fail=0
for f in .dev/graph_lint.py .dev/graph_expand_check.py .dev/shield_check.py .dev/esc_check.py .dev/evo_check.py \
         .dev/gate_check.py .dev/check_gate_reject.py .dev/check_lock_safe.py \
         .dev/w7_check.py .dev/coverage_check.py .dev/check_rollback_markers.py \
         .dev/cumulative_evidence_check.py .dev/p0_recall_check.py \
         .dev/p0_gate_check.py .dev/explanation_model_check.py \
         .dev/path_recall_check.py .dev/p0_obligation_check.py \
         .dev/causal_semantics_check.py .dev/mape_k_v2_check.py \
         .dev/tool_planner_v2_check.py .dev/esc_v2_check.py \
         .dev/causal_gate_v2_check.py .dev/verify_rollback_v2_check.py \
         .dev/learning_v2_check.py .dev/evidence_predicate_check.py \
         .dev/dynamic_tool_planner_check.py \
         .dev/subagent_path_task_check.py .dev/esc_explanation_check.py \
         .dev/causal_gate_context_check.py .dev/causal_verify_check.py \
         .dev/terminal_done_check.py .dev/evolution_v2_check.py \
         .dev/automatic_learning_writeback_check.py \
         .dev/authoritative_case_check.py \
         .dev/structure_v2_check.py .dev/eval_metrics_v2_check.py \
         .dev/e2e_explanation_check.py; do
  [ -f "$f" ] || continue
  printf '%-32s ' "$f"
  out="$(timeout 180 "$python_bin" "$f" 2>&1)"
  code=$?
  echo "$out" | tail -1 | cut -c1-90
  if [ "$code" -ne 0 ]; then
    fail=$((fail + 1))
    echo "$out" | tail -8 | sed 's/^/     /'
  fi
done
echo "----"
echo "失败脚本数: $fail"
exit "$fail"
