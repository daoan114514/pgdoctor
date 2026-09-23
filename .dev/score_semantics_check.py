#!/usr/bin/env python3
"""三率口径不再静默漂移（架构评审第 3 条）。

(a) 被成功回滚的修复不算修复：有效 SQL 排除它，回滚后 KPI 重新补采。
(b) Diagnosis 分两个口径：diagnosis（曾经选对过，案例入库用）与 diagnosis_reported
    （以 REPORT/DONE 收尾、最后一次 ESC SUFFICIENT，benchmark 用）。
(c) 跑批代码自己崩了标 HARNESS、不进分母；只有停机才中止整批；每个 episode 后落盘。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import loop as loop_mod  # noqa: E402
from sandbox import metrics  # noqa: E402
from sandbox.scoring import RegressionResult  # noqa: E402
from sandbox.scoring import score_episode  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


SPEC = {"fault_class": "missing_index",
        "ground_truth": {"acceptable_fixes": [{"pattern": r"CREATE INDEX .* ON orders", "quality": "full"}],
                         "competing_hypotheses": []},
        "success": {"outcome": "p99_ms < 100"}}
GOOD = metrics.KPI(p50_ms=2, p95_ms=8, p99_ms=40, qps=200, errors=0, cpu_pct=40, samples=300)
REG = RegressionResult(passed=True)
FIX = "CREATE INDEX CONCURRENTLY i ON orders(user_id, status)"

print("[1] 有效 SQL 排除已回滚的修复")
att_ok = SimpleNamespace(sql=FIX, rollback_status="")
att_rb = SimpleNamespace(sql=FIX, rollback_status="SUCCEEDED")
check(loop_mod.effective_applied_sql([FIX], [att_ok]) == [FIX], "未回滚 -> 仍然生效")
check(loop_mod.effective_applied_sql([FIX], [att_rb]) == [], "已成功回滚 -> 从有效 SQL 里去掉")
check(loop_mod.effective_applied_sql([FIX], [SimpleNamespace(sql=FIX, rollback_status="FAILED")]) == [FIX],
      "回滚失败（修复还在库里）-> 仍算生效")
s = score_episode(SPEC, "missing_index", [], GOOD, REG, {"final_phase": "DONE", "esc_last_verdict": "SUFFICIENT"})
check(s.outcome is False, "有效 SQL 为空时 KPI 再好也不算 Outcome")
lsrc = inspect.getsource(loop_mod.run_episode)
check("res.final_kpi = None" in lsrc and "res.final_regression = None" in lsrc,
      "回滚成功后清掉 VERIFY 时的 KPI（打分补采回滚后的状态）")
check("applied_sql=res.effective_sql" in inspect.getsource(loop_mod._post_episode_learning),
      "打分用的是有效 SQL")

print("[2] 两个诊断口径")
base = dict(final_phase="DONE", esc_last_verdict="SUFFICIENT", escalated=False)
s = score_episode(SPEC, "missing_index", [FIX], GOOD, REG, base)
check(s.diagnosis and s.diagnosis_reported, "REPORT/DONE + SUFFICIENT + 根因对 -> 两个口径都 True")
s = score_episode(SPEC, "missing_index", [], GOOD, REG, dict(final_phase="DONE", esc_last_verdict="EXHAUSTED", escalated=True))
check(s.diagnosis and not s.diagnosis_reported, "曾选对但 EXHAUSTED 升级 -> diagnosis True、reported False（lock_contention rev4 的情形）")
s = score_episode(SPEC, "stale_statistics", [FIX], GOOD, REG, base)
check(not s.diagnosis and not s.diagnosis_reported, "根因错 -> 两个都 False")
s = score_episode(SPEC, "missing_index", [FIX], GOOD, REG, dict(final_phase="DONE", esc_used=False))
check(s.diagnosis_reported, "关掉 ESC 的跑批（esc_used=False）报告口径仍可为 True")

print("[3] run_suite 的归类与落盘")
rsrc = (ROOT / "eval" / "run_suite.py").read_text(encoding="utf-8")
check('out.error = f"HARNESS: {type(exc).__name__}: {exc}"' in rsrc, "harness 崩溃标 HARNESS 前缀")
check('"HARNESS: run_episode did not persist benchmark score"' in rsrc and rsrc.index("out.claimed = res.claimed_fault_class") < rsrc.index('"HARNESS: run_episode did not persist'),
      "没分时先记 claimed/steps 再作废")
check("if r.unusable and is_infra_failure(text=r.error) and i < len(picks):" in rsrc, "只有停机才中止整批")
check(rsrc.count("_write_results(") >= 3, "每个 episode 后增量落盘")
check("diagnosis_reported" in rsrc and "Diagnosis(报告口径)" in rsrc, "汇总打印报告口径的 Diagnosis")

print()
if fails:
    print(f"SCORE SEMANTICS: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"SCORE SEMANTICS: PASS（{checks} 项）")
