"""P0 candidate recall and evidence-contract acceptance checks.

These fixtures exercise production thresholds without filling a disk, producing
1GB of WAL, or waiting an hour.  They are diagnosis contracts, not performance
episodes, so eval/run_suite.py does not discover this nested directory.
"""
from __future__ import annotations

import sys
import random
from pathlib import Path
from unittest.mock import patch

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import esc
from agent.episode_state import EpisodeState, EvidenceStatus, Verdict
from agent.loop import run_episode
from agent.policy import Policy
from agent.state_machine import Phase
from knowledge.causal_graph import graph as G
from sandbox.env import _load_injectors

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "sandbox/scenarios/p0"
EXPECTED = {
    "autovacuum_starvation",
    "disk_pressure",
    "orphaned_prepared_transaction",
    "stale_replication_slot",
}
fails: list[str] = []


def check(name: str, condition: bool, detail="") -> None:
    print(f"  {'PASS' if condition else 'FAIL'}  {name}   {detail}")
    if not condition:
        fails.append(name)


specs = [yaml.safe_load(p.read_text(encoding="utf-8"))
         for p in sorted(SCENARIOS.glob("*.yaml"))]
found = {s.get("fault_class") for s in specs}

print("[1] P0 场景清单与图元数据")
check("四个 P0 场景齐全", found == EXPECTED, sorted(found))
graph_p0 = {n for n, d in G.load().nodes(data=True)
            if d.get("kind") == "RootCause" and d.get("severity") == "P0"}
check("图上的 P0 与场景一一对应", graph_p0 == EXPECTED, sorted(graph_p0))
check("场景明确区分真实状态与指标夹层",
      all(s.get("environment", {}).get("realism") in {
          "live_state", "provider_fixture", "live_object_with_age_fixture",
          "live_object_with_retention_fixture"} for s in specs))
registry = _load_injectors()
check("四个 P0 场景均有注册注入器", EXPECTED <= set(registry),
      sorted(EXPECTED - set(registry)))

print("\n[2] 正式假设生成：基础召回扩大，相关 P0 强制保留")
forced = set()
for s in specs:
    symptoms = s["symptoms"]
    base = G.candidate_causes(symptoms, top_k=6)
    recalled = G.recall_candidates(symptoms, base_top_k=6)
    base_names = [c["root_cause"] for c in base]
    core_names = [c["root_cause"] for c in recalled
                  if not c["forced_by_risk"]]
    names = [c["root_cause"] for c in recalled]
    forced |= {c["root_cause"] for c in recalled if c["forced_by_risk"]}
    check(f"{s['fault_class']} 从症状可召回", s["fault_class"] in names, names)
    check(f"{s['fault_class']} 保持基础排序", core_names == base_names,
          core_names)
check("至少一个低先验 P0 确由风险规则补入", bool(forced), sorted(forced))
cpu_only = G.recall_candidates(["cpu_saturated"], base_top_k=6)
check("不可达 P0 不会被无条件塞入",
      not any(c.get("severity") == "P0" for c in cpu_only),
      [c["root_cause"] for c in cpu_only])


class _StageEnv:
    episode_id = "p0_hypothesis_stage"
    spec = {
        "id": "p0_hypothesis_stage",
        "workload": {"hot_query": "SELECT 1"},
    }

    @staticmethod
    def observe():
        return None


class _StageObservation:
    alert = "p99_ms > 300"
    healthy_kpi = {"p99_ms": 100, "cpu_pct": 20, "errors": 0}
    current_kpi = {"p99_ms": 400, "cpu_pct": 20, "errors": 0}


class _StagePolicy(Policy):
    name = "p0-stage-probe"

    def __init__(self):
        self.before_hypothesize = True
        self.seen: list[str] = []

    def run_phase(self, phase, tb, st, ctx):
        if phase is Phase.MONITOR:
            self.before_hypothesize = (
                st.explanation_graph is None and not st.hypothesis_candidates)
            return Phase.OBSERVE
        if phase is Phase.OBSERVE:
            return Phase.HYPOTHESIZE
        if phase is Phase.HYPOTHESIZE:
            explanation = st.explanation_graph
            self.seen = list(dict.fromkeys(
                path.root_node_id for path in
                (explanation.candidate_paths if explanation else [])))
            projection = ctx.get("explanation", {})
            check("路径在 HYPOTHESIZE 阶段生成并持久化",
                  self.before_hypothesize and
                  explanation is not None and
                  projection.get("explanation_id") == explanation.explanation_id and
                  projection.get("revision") == explanation.revision and
                  self.seen == st.hypothesis_candidates and bool(self.seen))
            return Phase.ESCALATE
        raise AssertionError(f"unexpected phase: {phase}")


stage_policy = _StagePolicy()
with patch.object(EpisodeState, "save", lambda self: None):
    run_episode(_StageEnv(), _StageObservation(), stage_policy,
                use_esc=False, use_cases=False, quiet=True)
check("阶段级候选包含相关 P0",
      any(G.severity_of(c) == "P0" for c in stage_policy.seen),
      stage_policy.seen)

print("\n[3] 每个场景的健康值反证、故障值确认，UNKNOWN 不放行")
for s in specs:
    rc = s["fault_class"]
    ev = s["evidence"]
    healthy = esc._supports(ev["type"], ev["healthy"], rc)
    fault = esc._supports(ev["type"], ev["fault"], rc)
    check(f"{rc} 健康值不支持", healthy is False)
    check(f"{rc} 故障值支持", fault is True)

    st = EpisodeState(episode_id=f"p0_{rc}", scenario_id=s["id"])
    st.symptoms = list(s["symptoms"])
    st.hypothesis_candidates = [rc]
    st.claimed_fault_class = rc
    st.set_verdict(rc, Verdict.CONFIRMED, note=f"P0 contract confirms {rc}")
    st.note("fixture", ev["type"], ev["fault"], bears_on=[rc])
    report = esc.check(st)
    d1 = next(d for d in report.dims if d.name == "D1")
    check(f"{rc} 直接证据通过 ESC D1", d1.passed, d1.detail)

    unknown = EpisodeState(episode_id=f"p0_unknown_{rc}", scenario_id=s["id"])
    unknown.hypothesis_candidates = [rc]
    unknown.claimed_fault_class = rc
    unknown.set_verdict(rc, Verdict.CONFIRMED, note="unavailable fixture evidence")
    unknown.note("fixture", ev["type"], ev["fault"], bears_on=[rc],
                 status=EvidenceStatus.UNKNOWN)
    report = esc.check(unknown)
    d1 = next(d for d in report.dims if d.name == "D1")
    check(f"{rc} UNKNOWN 不通过 D1", not d1.passed, d1.detail)

print("\n[4] ESC 只消费候选，并对候选中的 P0 单独设门")
st = EpisodeState(episode_id="p0_d2_layer", scenario_id="p0_contract")
st.hypothesis_candidates = [
    "missing_index", "stale_statistics", "autovacuum_starvation"]
st.claimed_fault_class = "missing_index"
st.set_verdict("missing_index", Verdict.CONFIRMED,
               note="Seq Scan removes millions of rows and covering index is absent")
st.set_verdict("stale_statistics", Verdict.REFUTED,
               note="estimate and actual row counts differ by only one times")
st.note("fixture", "explain_seq_scan",
        "Seq Scan, Rows Removed by Filter=12,000,000")
st.note("fixture", "index_existence", "orders 上的索引: ['orders_pkey']")
st.note("fixture", "row_estimate_deviation", "估计与实际行数最大偏差 1 倍")
report = esc.check(st)
d2 = next(d for d in report.dims if d.name == "D2")
check("普通排除率已达标但 P0 未取证时 D2 仍拒绝", not d2.passed, d2.detail)

auto = next(s for s in specs if s["fault_class"] == "autovacuum_starvation")
st.set_verdict("autovacuum_starvation", Verdict.REFUTED,
               note="autovacuum is enabled and backlog is below its trigger")
st.note("fixture", auto["evidence"]["type"], auto["evidence"]["healthy"])
report = esc.check(st)
d2 = next(d for d in report.dims if d.name == "D2")
check("P0 有反证后 D2 分层门通过", d2.passed, d2.detail)

if "--live" in sys.argv:
    print("\n[5] WSL/PostgreSQL 轻量实物注入与幂等清理")
    for s in specs:
        rc = s["fault_class"]
        injector = registry[rc](s)
        params = injector.params(random.Random(0))
        semantic_only = bool(getattr(injector, "semantic_only", False))
        try:
            injector.cleanup()
            record = injector.inject(params)
            check(f"{rc} 注入器返回正确 ground truth",
                  record.fault_class == rc, record.notes)
            check(f"{rc} 注入后 oracle 成立",
                  injector.verify_injected(params), record.notes)
        finally:
            injector.cleanup()
            injector.cleanup()
        if not semantic_only:
            check(f"{rc} cleanup 后实物状态消失",
                  not injector.verify_injected(params))
        else:
            check(f"{rc} 使用 provider fixture，不写真实磁盘", True)

print("\n" + "=" * 68)
print("P0 RECALL: PASS" if not fails else f"P0 RECALL: FAIL {fails}")
sys.exit(1 if fails else 0)
