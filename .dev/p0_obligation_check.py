"""Acceptance checks for path-level P0 obligations."""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.explanation import ObligationStatus
from knowledge.causal_graph import graph as G


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<48} {detail}")


P0 = {
    "autovacuum_starvation", "disk_pressure",
    "stale_replication_slot", "orphaned_prepared_transaction",
}

print("[1] Reachability creates explicit obligations")
cfg = G.PathRecallConfig(
    max_hops=4, ordinary_path_budget=1,
    max_paths_per_root_symptom=1, exploration_path_budget=0,
    p0_max_paths_per_cause=20)
explanation = G.recall_explanation(
    ["disk_growing"], episode_id="p0_obligation_fixture",
    config=cfg, use_learned=False)
check("all four reachable P0 causes have obligations",
      set(explanation.p0_obligations) == P0,
      set(explanation.p0_obligations))
ordinary = [path for path in explanation.candidate_paths
            if G.severity_of(path.root_node_id) != "P0"]
p0_paths = [path for path in explanation.candidate_paths
            if G.severity_of(path.root_node_id) == "P0"]
check("ordinary budget remains one", len(ordinary) == 1, len(ordinary))
check("P0 paths are retained outside the ordinary budget",
      len(p0_paths) > cfg.ordinary_path_budget, len(p0_paths))
check("every obligation carries required evidence",
      all(obligation.required_evidence_types
          for obligation in explanation.p0_obligations.values()))

print("\n[2] Unreachable P0 causes are not injected")
latency = G.recall_explanation(
    ["latency_p99_up"], episode_id="p0_unreachable_fixture",
    config=cfg, use_learned=False)
check("disk_pressure is unreachable from latency",
      "disk_pressure" not in latency.p0_obligations,
      set(latency.p0_obligations))
check("no unrelated P0 path is injected",
      all(path.observed_symptom_id == "latency_p99_up"
          for path in latency.candidate_paths))

print("\n[3] Per-cause caps preserve obligations and mark truncation")
tiny = G.PathRecallConfig(
    max_hops=4, ordinary_path_budget=1,
    max_paths_per_root_symptom=1, exploration_path_budget=0,
    p0_max_paths_per_cause=1)
truncated_graph = G.recall_explanation(
    ["disk_growing"], episode_id="p0_truncated_fixture",
    config=tiny, use_learned=False)
check("all reachable P0 obligations survive a tiny cap",
      set(truncated_graph.p0_obligations) == P0)
truncated = {cause for cause, obligation in
             truncated_graph.p0_obligations.items() if obligation.truncated}
check("causes with additional paths are explicitly truncated",
      {"autovacuum_starvation", "stale_replication_slot",
       "orphaned_prepared_transaction"}.issubset(truncated), truncated)
check("truncated obligations cannot become resolved",
      all(not obligation.resolved for obligation in
          truncated_graph.p0_obligations.values() if obligation.truncated))

shallow = G.PathRecallConfig(
    max_hops=1, ordinary_path_budget=1,
    max_paths_per_root_symptom=1, exploration_path_budget=0,
    p0_max_paths_per_cause=1)
shallow_graph = G.recall_explanation(
    ["disk_growing"], episode_id="p0_depth_fixture",
    config=shallow, use_learned=False)
check("P0 beyond max_hops remains an explicit obligation",
      set(shallow_graph.p0_obligations) == P0 and
      shallow_graph.p0_obligations["orphaned_prepared_transaction"].truncated,
      set(shallow_graph.p0_obligations))

print("\n[4] Resolution is per obligation, never an average")
disk = truncated_graph.p0_obligations["disk_pressure"]
truncated_graph.resolve_p0(
    "disk_pressure", ObligationStatus.REFUTED,
    reason="current structured disk-usage evidence is healthy")
check("one non-truncated P0 can be resolved", disk.resolved)
open_others = [cause for cause, obligation in
               truncated_graph.p0_obligations.items() if not obligation.resolved]
check("resolving one P0 leaves every other obligation open",
      set(open_others) == P0 - {"disk_pressure"}, open_others)
needs = G.evidence_needs(truncated_graph)
p0_need_causes = {need.target_ids[0] for need in needs
                  if need.target_kind == "P0"}
check("required/P0 needs bypass ordinary frontier pressure",
      P0 - {"disk_pressure"} <= p0_need_causes, p0_need_causes)

print("\n[5] P0 inclusion does not depend on learned score")
adversarial = G.recall_explanation(
    ["disk_growing"], episode_id="p0_learned_fixture",
    config=tiny, use_learned=True,
    case_path_scores={path.path_id: -999.0 for path in p0_paths},
    learned_path_scores={path.path_id: -999.0 for path in p0_paths})
check("all P0 obligations remain under adversarial ranking",
      set(adversarial.p0_obligations) == P0)

print("\n" + "=" * 72)
print("P0 OBLIGATION:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
