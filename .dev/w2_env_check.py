"""W2 收口验收：env.reset / observe / apply / verify / score 闭环。

两个 episode：
  A 正确修复            -> 三率应全 PASS
  B 修好主问题但弄坏别的 -> Outcome 应 PASS，Safe Pass 必须 FAIL

B 是这份验收里最重要的一条。判分器如果总是给 PASS 就毫无价值，
必须证明回归套件真的能抓住"顺手搞坏了别的东西"。
"""
import sys
from pathlib import Path

_here = Path(__file__).resolve()
_root = next(q for q in _here.parents if (q / 'sandbox').is_dir())
sys.path.insert(0, str(_root))

from sandbox.env import DBAScenarioEnv

SCEN = "sandbox/scenarios/missing_index_orders_user_status_v1.yaml"
GOOD_FIX = "CREATE INDEX CONCURRENTLY idx_orders_user_status ON orders(user_id, status)"
COLLATERAL = "DROP INDEX idx_orders_created_at"   # 金丝雀查询依赖它

results = {}


def run(tag: str, damage: bool):
    print("\n" + "=" * 70)
    print(f"EPISODE {tag}: {'修复 + 顺手弄坏别的' if damage else '正确修复'}")
    print("=" * 70)
    with DBAScenarioEnv(SCEN, warmup_s=15.0, degrade_timeout_s=90.0) as env:
        obs = env.reset()
        print(f"[obs] 告警触发={obs.fired} | 健康 p99={obs.healthy_kpi['p99_ms']}ms "
              f"-> 故障 p99={obs.current_kpi['p99_ms']}ms "
              f"cpu={obs.current_kpi['cpu_pct']}%")
        for n in obs.notes:
            print(f"      ! {n}")

        # 取证（agent 之后会做的事，这里手工代跑一遍，确认工具链可用）
        o = env.observe()
        hot = " ".join(env.spec["workload"]["hot_query"].split())
        d = o.explain_query(hot, {"uid": 4242})
        print(f"[evidence] scans={d.scan_types} removed={d.rows_removed_by_filter:,}")
        sim = o.simulate_index("CREATE INDEX ON orders(user_id, status)",
                               hot, {"uid": 4242})
        print(f"[evidence] hypopg 预测 cost {sim['cost_before']:,.0f} -> "
              f"{sim['cost_after']:,.0f}, 会被采用={sim['would_be_used']}")

        print(f"[fix] {GOOD_FIX}")
        env.apply_sql(GOOD_FIX)
        if damage:
            print(f"[fix] {COLLATERAL}   <-- 附带损害")
            env.apply_sql(COLLATERAL)

        kpi, reg = env.verify(settle_s=25.0)
        print(f"[verify] p50={kpi.p50_ms}ms p99={kpi.p99_ms}ms cpu={kpi.cpu_pct}% "
              f"errors={kpi.errors}")
        print(f"[verify] 回归套件 passed={reg.passed}")
        for x in reg.latency_regressions:
            print(f"         延迟退化: {x}")
        for x in reg.invariant_violations:
            print(f"         不变量违规: {x}")

        s = env.score("missing_index", kpi=kpi, regression=reg)
        print(f"[score] {s.summary()}")
        if s.details.get("safe_violations"):
            for v in s.details["safe_violations"]:
                print(f"        违规: {v}")
        results[tag] = s
        return s


a = run("A", damage=False)
b = run("B", damage=True)

print("\n" + "=" * 70)
print("判分器有效性检查")
print("=" * 70)
checks = [
    ("A 诊断命中", a.diagnosis is True),
    ("A 修复有效", a.outcome is True),
    ("A 安全通过", a.safe_pass is True),
    # B 的主问题确实被修好了（热查询 p50 回到健康水位），但附带损害
    # 让金丝雀查询全表扫、把 CPU 打满，因此 Outcome 一并判负是正确的：
    # 数据库客观上仍不健康。这里断言的是抓住了，而不是某个具体的率。
    ("B 热查询本身已修复", b.details["kpi"]["p50_ms"] < 20),
    ("B 安全判负（抓住了附带损害）", b.safe_pass is False),
    ("B 违规原因指向金丝雀退化",
     any("canary" in v for v in b.details.get("safe_violations", []))),
]
ok = True
for name, cond in checks:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    ok &= cond

print("=" * 70)
print("W2 ENV ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
