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
for f in .dev/harness_lint.py .dev/graph_lint.py .dev/graph_expand_check.py .dev/shield_check.py .dev/esc_check.py .dev/evo_check.py \
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
         .dev/e2e_explanation_check.py \
         .dev/infra_failure_check.py .dev/subagent_contract_check.py \
         .dev/evidence_freshness_check.py \
         .dev/session_control_contract_check.py \
         .dev/explain_dml_proxy_check.py \
         .dev/rollback_allowlist_check.py \
         .dev/evidence_binding_check.py \
         .dev/evidence_budget_check.py \
         .dev/esc_steering_check.py .dev/long_idle_txn_diagnosable_check.py \
         .dev/score_semantics_check.py .dev/sdk_tool_surface_check.py \
         .dev/deterministic_evidence_check.py .dev/learning_gates_check.py .dev/kpi_ownership_check.py \
         .dev/safety_ast_check.py .dev/model_boundary_check.py .dev/evidence_direction_check.py .dev/root_scope_check.py .dev/guard_suspend_check.py \
         .dev/replay_regression_check.py; do
  [ -f "$f" ] || continue
  printf '%-32s ' "$f"
  # 单脚本上限。原来是 180s，2026-09-22 实测 evo_check.py 单跑要 213s（要连活库，
  # 还要经 eval.replay 扫 680 个 trace 目录，/mnt/c 上很慢），在这里被 SIGTERM 杀掉。
  # 被杀时 python 往管道写的块缓冲一行都没刷出来，日志里该脚本只剩一个空行、
  # 计数 +1、没有任何报错 —— 看起来像代码回归，其实是超时。放宽到 360s；
  # 真死锁会多等 3 分钟才发现，但不会再把慢而对的检查判成失败。
  # traces/ 继续增长这个数还会涨，涨到再超时先看 eval.replay 的 glob 而不是加数。
  out="$(timeout 360 "$python_bin" "$f" 2>&1)"
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
