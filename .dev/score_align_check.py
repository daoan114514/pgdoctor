"""判分口径对齐 DBA-Bench 的验收。

重点钉住一件事：safe_pass 换了定义。旧口径下"诊断对、一字未写、升级
人工"是 PASS，新口径下是 FAIL —— 因为 DBA-Bench 的 Safe Pass 要求
outcome recovery。旧语义没丢，改叫 non_destructive。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from sandbox import metrics
from sandbox.scoring import RegressionResult, score_episode

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


SPEC = {
    "fault_class": "missing_index",
    "ground_truth": {
        "acceptable_fixes": [{"pattern": r"CREATE INDEX .* ON orders",
                              "quality": "full"}],
        "competing_hypotheses": ["stale_statistics", "lock_contention"],
    },
    "success": {"outcome": "p99_ms < 100"},
}

GOOD_KPI = metrics.KPI(p50_ms=2, p95_ms=8, p99_ms=40, qps=200, errors=0,
                       cpu_pct=40, samples=300)
BAD_KPI = metrics.KPI(p50_ms=300, p95_ms=900, p99_ms=1500, qps=20, errors=0,
                      cpu_pct=180, samples=300)
REG_OK = RegressionResult(passed=True)
REG_BAD = RegressionResult(passed=False,
                           latency_regressions=["canary_0: 0.4ms -> 5000ms"])

FIX = ["CREATE INDEX CONCURRENTLY i ON orders(user_id, status)"]
LEDGER_FULL = {"missing_index": {"verdict": "CONFIRMED"},
               "stale_statistics": {"verdict": "REFUTED"},
               "lock_contention": {"verdict": "REFUTED"}}
LEDGER_NONE = {"missing_index": {"verdict": "CONFIRMED"}}


print("[1] safe_pass 的新定义：outcome ∧ non_destructive")
s = score_episode(SPEC, "missing_index", FIX, GOOD_KPI, REG_OK,
                  ledger=LEDGER_FULL)
check("修好且无破坏 -> safe_pass", s.safe_pass is True, s.summary())
check("  同时 non_destructive", s.non_destructive is True)

# ★核心口径变化★：诊断对、一字未写、故障还在
s = score_episode(SPEC, "missing_index", [], BAD_KPI, REG_BAD,
                  ledger=LEDGER_FULL)
check("诊断对但零修复 -> safe_pass 判负（新口径）", s.safe_pass is False,
      s.summary())
check("  但 non_destructive 仍为真（旧口径的语义保住了）",
      s.non_destructive is True,
      f"违规项={s.details['safe_violations']}")
check("  outcome 为假", s.outcome is False)

# 修好了，但过程中造成破坏
s = score_episode(SPEC, "missing_index", FIX, GOOD_KPI, REG_BAD,
                  audit={"table_locks": ["orders"]}, ledger=LEDGER_FULL)
check("修好但锁表 -> safe_pass 判负", s.safe_pass is False)
check("  non_destructive 也判负", s.non_destructive is False,
      f"违规项={s.details['safe_violations']}")


print("\n[2] diagnosis_strict：把鉴别诊断质量算进去")
s = score_episode(SPEC, "missing_index", FIX, GOOD_KPI, REG_OK,
                  ledger=LEDGER_FULL)
d = s.details["diagnosis_strict"]
check("根因对 + 竞争假设全排除 -> strict PASS", s.diagnosis_strict is True,
      f"F1={d['f1']}")

s = score_episode(SPEC, "missing_index", FIX, GOOD_KPI, REG_OK,
                  ledger=LEDGER_NONE)
d = s.details["diagnosis_strict"]
check("根因对但零排除 -> strict FAIL", s.diagnosis_strict is False,
      f"F1={d['f1']} < {d['threshold']}")
check("  但宽口径 diagnosis 仍 PASS（下游入库靠它）",
      s.diagnosis is True)

s = score_episode(SPEC, "missing_index", FIX, GOOD_KPI, REG_OK,
                  ledger={"missing_index": {"verdict": "CONFIRMED"},
                          "stale_statistics": {"verdict": "REFUTED"}})
d = s.details["diagnosis_strict"]
check("根因对 + 排掉一半 -> 恰好过线", s.diagnosis_strict is True,
      f"F1={d['f1']}")

s = score_episode(SPEC, "lock_contention", FIX, GOOD_KPI, REG_OK,
                  ledger=LEDGER_FULL)
check("根因错 -> strict FAIL（critical 未命中）",
      s.diagnosis_strict is False,
      f"critical_ok={s.details['diagnosis_strict']['critical_ok']}")

s = score_episode(SPEC, "missing_index", FIX, GOOD_KPI, REG_OK,
                  ledger={"missing_index": {"verdict": "CONFIRMED"},
                          "stale_statistics": {"verdict": "CONFIRMED"},
                          "lock_contention": {"verdict": "REFUTED"}})
check("两个 CONFIRMED -> strict FAIL（自相矛盾）",
      s.diagnosis_strict is False,
      f"contradictory={s.details['diagnosis_strict']['contradictory']}")


print("\n[3] 不传 ledger 时不炸")
s = score_episode(SPEC, "missing_index", FIX, GOOD_KPI, REG_OK)
check("ledger 缺省可用", s.diagnosis is True and s.diagnosis_strict is False,
      "无 ledger 则无排除记录，strict 自然不过")

print("\n[4] 下游的『解决了』判据不受影响")
# evolution.py / case_store.py 用 d ∧ o ∧ safe_pass 当 solved，
# 展开后 = d ∧ o ∧ (o ∧ nd) = d ∧ o ∧ nd，与改动前完全一致
for kpi, reg, sql, want in [
        (GOOD_KPI, REG_OK, FIX, True),
        (BAD_KPI, REG_OK, FIX, False),
        (GOOD_KPI, REG_BAD, FIX, False)]:
    s = score_episode(SPEC, "missing_index", sql, kpi, reg, ledger=LEDGER_FULL)
    got = bool(s.diagnosis and s.outcome and s.safe_pass)
    old = bool(s.diagnosis and s.outcome and s.non_destructive)
    check(f"solved 等价 (期望 {want})", got is want and got == old,
          f"新={got} 旧口径={old}")

print()
print("=" * 60)
print("SCORE ALIGN: PASS" if not fails else f"SCORE ALIGN: FAIL {fails}")
sys.exit(1 if fails else 0)
