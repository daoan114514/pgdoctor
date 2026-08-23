"""lock_contention 诊断正确、零修复，为何 Safe Pass 失分？"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

d = json.loads(Path("/home/daoan/pgdoctor/eval/results/llm_two_fixed.json")
               .read_text(encoding="utf-8"))
e = next(x for x in d["episodes"] if x["fault_class"] == "lock_contention")

print("episode:", e["episode_id"])
print("已执行的修复:", e["applied_sql"] or "(无)")
print("阶段违规:", e["violations"] or "(无)")
print()

from eval import replay
st = replay.load(e["episode_id"])
print("undo 记录:", st.undo_refs or "(无)")
print("失败尝试:", len(st.attempts))
print("outcome_note:", st.outcome_note[:200])
print()

# 用当前的判分逻辑复算，看违规项到底是什么
from sandbox.scoring import RegressionResult, score_episode
from sandbox import metrics
import yaml

spec = yaml.safe_load(Path(
    "/home/daoan/pgdoctor/sandbox/scenarios/lock_contention_eval_v1.yaml"
).read_text(encoding="utf-8"))

kpi = metrics.KPI(p50_ms=5, p95_ms=20, p99_ms=1500, qps=50, errors=200,
                  cpu_pct=40, samples=300)
reg_fail = RegressionResult(passed=False,
                            latency_regressions=["canary_0: 0.4ms -> 5000ms"])
s = score_episode(spec, "lock_contention", [], kpi, reg_fail)
print("复算（零修复 + 回归失败）:")
print("  SafePass:", s.safe_pass)
print("  违规项:", s.details.get("safe_violations"))
print("  回归备注:", s.details.get("regression_note", "")[:100])
