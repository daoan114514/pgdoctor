"""Acceptance checks for the v2 causal-path runtime."""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge.causal_graph import graph as G


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<48} {detail}")


print("[1] Central config and deterministic graph identity")
cfg = G.DEFAULT_PATH_RECALL
check("default path config matches the contract",
      (cfg.max_hops, cfg.ordinary_path_budget,
       cfg.max_paths_per_root_symptom, cfg.exploration_path_budget,
       cfg.p0_max_paths_per_cause) == (4, 12, 3, 2, 20))
version = G.graph_version()
check("graph_version is stable and content-addressed",
      version == G.graph_version() and version.startswith("graph_"), version)

print("\n[2] Multi-hop enumeration and dynamic path roles")
paths = G.enumerate_causal_paths(["disk_growing"], use_learned=False)
structures = {tuple(path.node_ids): path for path in paths}
long_idle = (
    "long_idle_transaction", "autovacuum_starvation", "table_bloat",
    "disk_pressure", "disk_growing",
)
hidden_slot = (
    "stale_replication_slot", "autovacuum_starvation", "table_bloat",
    "disk_pressure", "disk_growing",
)
check("four-hop upstream cause remains recallable", long_idle in structures)
check("hidden P0 upstream cause remains recallable", hidden_slot in structures)
check("every path is simple and upstream-to-symptom",
      all(len(path.node_ids) == len(path.edge_ids) + 1 and
          len(path.node_ids) == len(set(path.node_ids)) and
          path.root_node_id == path.node_ids[0] and
          path.observed_symptom_id == path.node_ids[-1]
          for path in paths))
auto_root = structures[("autovacuum_starvation", "table_bloat",
                        "disk_growing")]
slot_path = structures[("stale_replication_slot", "autovacuum_starvation",
                        "table_bloat", "disk_growing")]
check("one node can be root on a shorter path",
      auto_root.node_roles["autovacuum_starvation"] == "ROOT_CAUSE")
check("the same node is mechanism on a longer path",
      slot_path.node_roles["autovacuum_starvation"] == "MECHANISM")
again = G.enumerate_causal_paths(["disk_growing"], use_learned=False)
check("path IDs replay exactly",
      {path.path_id for path in paths} == {path.path_id for path in again})

print("\n[3] Ordinary budget, branch diversity, and exploration")
small = G.PathRecallConfig(
    max_hops=4, ordinary_path_budget=6,
    max_paths_per_root_symptom=2, exploration_path_budget=2,
    p0_max_paths_per_cause=20)
small_paths = G.enumerate_causal_paths(
    ["latency_p99_up"], config=small, max_hops=small.max_hops,
    use_learned=False)
ordinary = [path for path in small_paths
            if G.severity_of(path.root_node_id) != "P0"]
branches = {(path.observed_symptom_id, path.node_ids[-2]) for path in ordinary}
exploration = [path for path in ordinary if "exploration" in path.source]
check("ordinary paths respect their own budget", len(ordinary) <= 6, len(ordinary))
check("same symptom retains distinct nearest branches", len(branches) >= 3, branches)
check("exploration quota retains low-prior structures",
      len(exploration) >= 2, [path.root_node_id for path in exploration])
check("per-root/symptom structural cap is enforced",
      all(sum(1 for other in ordinary
              if other.root_node_id == path.root_node_id and
              other.observed_symptom_id == path.observed_symptom_id) <= 2
          for path in ordinary))

print("\n[4] Explainable scoring and learned ablation")
required_components = {
    "manual_causes_likelihood", "manual_root_prior",
    "learned_root_prior_adjustment", "l1_path_template_adjustment",
    "l3_edge_adjustment", "l3_path_adjustment", "symptom_coverage_reward",
    "hop_penalty", "redundancy_penalty", "total",
}
check("every score retains all explainable components",
      all(required_components.issubset(path.score_components) for path in paths))
check("use_learned=False zeroes all learned channels",
      all(path.score_components["learned_root_prior_adjustment"] == 0 and
          path.score_components["l1_path_template_adjustment"] == 0 and
          path.score_components["l3_edge_adjustment"] == 0 and
          path.score_components["l3_path_adjustment"] == 0
          for path in paths))
known = auto_root.path_id
boosted = G.enumerate_causal_paths(
    ["disk_growing"], use_learned=True,
    case_path_scores={known: 999.0, "path_unknown": 999.0},
    learned_path_scores={known: 999.0})
boost = next(path for path in boosted if path.path_id == known)
manual = boost.score_components["manual_causes_likelihood"]
edge_overlay = {
    f"{source}->{target}": 999.0
    for source, target in zip(auto_root.node_ids, auto_root.node_ids[1:])
}
with patch.object(G, "_learned_likelihood_adj", return_value=edge_overlay):
    edge_boosted = G.enumerate_causal_paths(["disk_growing"], use_learned=True)
edge_boost = next(path for path in edge_boosted if path.path_id == known)
check("L1/L3 adjustments are capped relative to manual weight",
      boost.score_components["l1_path_template_adjustment"] <= manual * 0.25 and
      abs(edge_boost.score_components["l3_edge_adjustment"]) <= manual * 0.5 and
      boost.score_components["l3_path_adjustment"] <= manual * 0.25)
graph_nodes = set(G.load().nodes)
check("unknown case-template nodes never enter live paths",
      all(set(path.node_ids).issubset(graph_nodes) for path in boosted))

print("\n[5] Merge, frontier, needs, and bounded path queries")
explanation = G.merge_paths(
    paths, episode_id="path_recall_fixture",
    observed_symptoms=["disk_growing", "unmapped_signal"])
check("merge preserves explicit unmapped symptoms",
      explanation.unexplained_symptoms == ["unmapped_signal"])
check("merge keeps shared mechanism paths distinct",
      auto_root.path_id in explanation.path_map() and
      slot_path.path_id in explanation.path_map())
frontier = G.path_frontier(explanation)
check("frontier starts nearest the observed region",
      bool(frontier) and frontier[0]["distance_from_observed"] == 0,
      frontier[:2])
needs = G.evidence_needs(explanation)
check("frontier produces typed evidence needs",
      bool(needs) and all(need.predicate_id and need.candidate_tools for need in needs))
check("required needs sort ahead of optional needs",
      not needs or needs[0].required)

alternatives = G.alternatives_for(slot_path.path_id, explanation)
check("alternatives share the concrete observed symptom",
      bool(alternatives) and all(
          path.observed_symptom_id == slot_path.observed_symptom_id
          for path in alternatives))
options = G.intervention_options(auto_root.path_id, explanation)
check("interventions are restricted to nodes on the path",
      bool(options) and all(option["target_node_id"] in auto_root.node_ids
                            for option in options),
      [(option["target_node_id"], option["fix"]) for option in options])
downstream = G.downstream_on_path(
    slot_path.path_id, "autovacuum_starvation", explanation)
check("downstream query is bounded to the selected path",
      downstream == ["table_bloat", "disk_growing"] and
      "connection_exhaustion" not in downstream, downstream)
outside_rejected = False
try:
    G.downstream_on_path(slot_path.path_id, "connection_exhaustion", explanation)
except ValueError:
    outside_rejected = True
check("path-external downstream targets are rejected", outside_rejected)

print("\n[6] Controlled cascade and branch fixtures")
prepared = G.enumerate_causal_paths(
    ["disk_growing"], use_learned=False)
prepared_structures = {tuple(path.node_ids) for path in prepared
                       if path.root_node_id ==
                       "orphaned_prepared_transaction"}
check("prepared transaction retains the autovacuum branch",
      ("orphaned_prepared_transaction", "autovacuum_starvation",
       "table_bloat", "disk_growing") in prepared_structures)
check("prepared transaction also retains the xid-risk branch",
      ("orphaned_prepared_transaction", "xid_wraparound_risk",
       "disk_growing") in prepared_structures)
lock_shape = G.enumerate_causal_paths(
    ["queries_blocked"], use_learned=False)
lock_roots = {path.root_node_id for path in lock_shape}
check("same lock symptom preserves deadlock and contention branches",
      {"deadlock", "lock_contention"} <= lock_roots, lock_roots)
latency_roots = {path.root_node_id for path in G.enumerate_causal_paths(
    ["latency_p99_up"], use_learned=False)}
check("same latency symptom preserves index/stats/spill branches",
      {"missing_index", "stale_statistics", "work_mem_spill"} <=
      latency_roots, latency_roots)
unreachable = G.enumerate_causal_paths(
    ["not_a_live_graph_symptom"], use_learned=False)
check("unreachable or unknown symptoms recall no path", unreachable == [])

print("\n[7] Runtime ignores unapproved promoted entries")
filtered = G._live_promoted({
    "causes_cause": [
        {"from": "missing_index", "to": "table_bloat", "status": "proposed"},
        {"from": "stale_statistics", "to": "table_bloat",
         "status": "ready_for_review"},
        {"from": "long_idle_transaction", "to": "table_bloat",
         "status": "approved"},
    ]
})
check("only approved/promoted overlay entries survive",
      len(filtered["causes_cause"]) == 1 and
      filtered["causes_cause"][0]["status"] == "approved")

print("\n" + "=" * 72)
print("PATH RECALL:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
