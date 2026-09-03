"""Independent acceptance checks for v2 structure proposal governance."""
from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from knowledge import structure
from knowledge.causal_graph import graph as G


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


tmp = Path(tempfile.mkdtemp(prefix="pgdoctor_structure_v2_"))
old_learned = structure.LEARNED
old_candidates = structure.CANDIDATES
old_promoted = structure.PROMOTED
try:
    structure.LEARNED = tmp / "learned"
    structure.CANDIDATES = structure.LEARNED / "candidate_edges.yaml"
    structure.PROMOTED = tmp / "promoted_edges.yaml"
    structure.LEARNED.mkdir(parents=True)
    structure.CANDIDATES.write_text(yaml.safe_dump({
        "proposals": [{
            "kind": "CAUSES", "src": "missing_index",
            "dst": "disk_growing", "support": 999,
            "status": "ready_for_review"}],
    }), encoding="utf-8")
    G.load.cache_clear()
    baseline_version = G.graph_version()

    print("[1] Co-occurrence and v1 candidates are never live structure")
    state = EpisodeState("structure_cooccurrence", "controlled")
    for index in range(20):
        state.note("fixture", "session_wait_profile",
                   f"unrelated observation {index}")
    touched = structure.observe_episode_v2(state)
    check("high-frequency co-occurrence creates no proposal",
          touched == [] and structure.load_candidates_v2() == {})
    check("v1 candidate file is ignored by graph loader",
          G.graph_version() == baseline_version and
          not G.load().has_edge("missing_index", "disk_growing",
                                key="CAUSES"))
    fixed_by_blocked = False
    try:
        structure.propose_v2(
            kind="FIXED_BY", src="missing_index",
            dst="create_covering_index", episode_id="e",
            scenario_id="s")
    except ValueError:
        fixed_by_blocked = True
    check("FIXED_BY cannot be machine-proposed", fixed_by_blocked)

    print("\n[2] CAUSES requires temporal, cross-scenario evidence")
    proposal = structure.propose_v2(
        kind="CAUSES", src="missing_index", dst="disk_growing",
        episode_id="cause_0", scenario_id="scenario_a",
        temporal_order=False, reduces_orphan_symptom=True)
    check("co-occurrence-like observation stays proposed",
          proposal.status == "proposed" and not proposal.ready)
    proposal = structure.propose_v2(
        kind="CAUSES", src="missing_index", dst="disk_growing",
        episode_id="cause_1", scenario_id="scenario_b",
        temporal_order=True, reduces_orphan_symptom=True)
    proposal = structure.propose_v2(
        kind="CAUSES", src="missing_index", dst="disk_growing",
        episode_id="cause_2", scenario_id="scenario_a",
        temporal_order=True, reduces_orphan_symptom=True)
    check("missing temporal order in one episode prevents readiness",
          proposal.status == "proposed" and not proposal.ready)
    proposal = structure.propose_v2(
        kind="CAUSES", src="missing_index", dst="disk_growing",
        episode_id="cause_0", scenario_id="scenario_a",
        temporal_order=True, reduces_orphan_symptom=True)
    check("independent temporal cross-scenario evidence becomes review-ready",
          proposal.status == "ready_for_review" and proposal.ready)
    check("ready proposal still does not enter the graph",
          G.graph_version() == baseline_version and
          not G.load().has_edge("missing_index", "disk_growing",
                                key="CAUSES"))

    print("\n[3] Explicit approval is the live-graph boundary")
    promoted_too_early, _ = structure.promote_v2(
        proposal.proposal_id, by="reviewer")
    check("ready proposal cannot skip approval", not promoted_too_early)
    approved, _ = structure.approve_v2(
        proposal.proposal_id, by="dba-reviewer", likelihood=0.4)
    G.load.cache_clear()
    check("approved proposal enters the promoted overlay",
          approved and G.graph_version() != baseline_version and
          G.load().has_edge("missing_index", "disk_growing", key="CAUSES"))
    promoted, _ = structure.promote_v2(
        proposal.proposal_id, by="dba-reviewer")
    check("approved proposal can be marked promoted",
          promoted and
          structure.load_candidates_v2()[proposal.proposal_id].status ==
          "promoted")

    print("\n[4] Counterexamples quarantine readiness")
    counter = None
    for index in range(3):
        counter = structure.propose_v2(
            kind="CAUSES", src="stale_statistics", dst="disk_growing",
            episode_id=f"counter_{index}",
            scenario_id=f"scenario_{index % 2}", temporal_order=True,
            reduces_orphan_symptom=True,
            known_counterexample=index == 2)
    check("known counterexample prevents ready_for_review",
          counter is not None and not counter.ready and
          counter.status == "proposed")
finally:
    structure.LEARNED = old_learned
    structure.CANDIDATES = old_candidates
    structure.PROMOTED = old_promoted
    G.load.cache_clear()
    shutil.rmtree(tmp, ignore_errors=True)

print("\n" + "=" * 80)
print("STRUCTURE V2:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
