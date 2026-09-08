"""验证 stale_statistics 的核心前提：ANALYZE 之后执行计划真的翻转了吗？

场景的 note 写着「统计过期时优化器低估匹配行数而选 Nested Loop，对 30 万行
做 30 万次索引查找；ANALYZE 后改用 Hash Join。修复的可观测证据是计划类型
翻转，不只是延迟下降」。但实测延迟只降 2.5-3 倍，故障态 p99 的低尾（654.9ms）
和修复后的高尾（526.8ms）几乎贴上 —— 如果计划确实翻了却只快 3 倍，说明
这个场景的 Outcome 判据天然就难做；如果压根没翻，那是另一回事。
两种情况的处理方式完全不同，所以要先看清楚。
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from sandbox import db
from sandbox.env import DBAScenarioEnv

SC = sys.argv[1] if len(sys.argv) > 1 else "stale_statistics_eval_v1"
spec = yaml.safe_load(
    Path(f"sandbox/scenarios/{SC}.yaml").read_text(encoding="utf-8"))
HOT = " ".join(spec["workload"]["hot_query"].split())


def explain() -> dict:
    row = db.query(
        f"EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF, BUFFERS) {HOT}",
        (None,) if "%(uid)s" in HOT else None)
    return row[0][0][0]


def walk(node):
    yield node
    for child in node.get("Plans", []):
        yield from walk(child)


def describe(tag: str, plan: dict) -> None:
    root = plan["Plan"]
    print(f"\n  --- {tag} ---")
    print(f"  总耗时 {plan.get('Execution Time', 0):.1f}ms")
    for node in walk(root):
        loops = max(node.get("Actual Loops", 1), 1)
        est = float(node.get("Plan Rows", 0)) * loops
        act = float(node.get("Actual Rows", 0)) * loops
        ratio = act / max(est, 1.0)
        print(f"    {node['Node Type']:<26} 估计={est:>12,.0f} "
              f"实际={act:>12,.0f}  偏差={ratio:>8.1f}x")


with DBAScenarioEnv(f"sandbox/scenarios/{SC}.yaml", warmup_s=10.0,
                    degrade_timeout_s=140.0, quiet=True) as env:
    obs = env.reset(seed=1)
    print(f"告警触发={obs.fired}  故障 p99={obs.current_kpi.get('p99_ms')}ms")
    before = explain()
    describe("ANALYZE 之前（统计过期）", before)
    env.apply_sql("ANALYZE orders")
    after = explain()
    describe("ANALYZE 之后", after)

    b = {n["Node Type"] for n in walk(before["Plan"])}
    a = {n["Node Type"] for n in walk(after["Plan"])}
    print(f"\n  计划节点 变化: 只在修复前 {sorted(b - a)} | "
          f"只在修复后 {sorted(a - b)}")
    print(f"  执行时间 {before.get('Execution Time', 0):.1f}ms -> "
          f"{after.get('Execution Time', 0):.1f}ms "
          f"({before.get('Execution Time', 1) / max(after.get('Execution Time', 1), 1e-9):.1f}x)")
