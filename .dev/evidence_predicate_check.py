"""Independent acceptance checks for structured, scoped evidence predicates."""
from __future__ import annotations

import json
import shutil
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.explanation import EvidenceBinding
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import PredicateContext, evaluate
from sandbox.traces import TRACE_DIR, TraceStore


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<54} {detail}")


episode_id = f"predicate_v2_{uuid.uuid4().hex}"
try:
    print("[1] Structured direction and summary isolation")
    ctx = PredicateContext(target_kind="NODE", target_ids=("stale_statistics",))
    support = evaluate(
        "row_estimate_deviation_v2", {"rows_est_vs_actual": [[10, 1000]]},
        context=ctx)
    refute = evaluate(
        "row_estimate_deviation_v2", {"rows_est_vs_actual": [[100, 105]]},
        context=ctx)
    check("structured values deterministically support/refute",
          support.result == "SUPPORTS" and refute.result == "REFUTES")
    repeated = evaluate(
        "row_estimate_deviation_v2", {"rows_est_vs_actual": [[100, 105]]},
        context=ctx)
    check("natural-language claims are not predicate inputs",
          repeated == refute and "SUPPORTED" not in repeated.reason)
    heavyweight_lock = evaluate(
        "session_wait_profile_v2",
        [{"wait_event": "Lock:transactionid"}],
        context=PredicateContext(target_ids=("lock_contention",)))
    normalized_lock = evaluate(
        "session_wait_profile_v2", [{"wait_event": "Lock"}],
        context=PredicateContext(target_ids=("lock_contention",)))
    lightweight_lock = evaluate(
        "session_wait_profile_v2",
        [{"wait_event": "LWLock:BufferMapping"}],
        context=PredicateContext(target_ids=("lock_contention",)))
    check("LWLock is not misclassified as a blocking transaction lock",
          heavyweight_lock.result == normalized_lock.result == "SUPPORTS" and
          lightweight_lock.result == "REFUTES")

    print("\n[2] Scope does not cascade from intervention to node")
    value = {"would_be_used": False, "trivial_baseline": False,
             "create_sql": "CREATE INDEX ON orders(total)"}
    intervention = evaluate(
        "counterfactual_index_v2", value,
        context=PredicateContext(
            target_kind="INTERVENTION",
            target_ids=("create_covering_index",)))
    node = evaluate(
        "counterfactual_index_v2", value,
        context=PredicateContext(
            target_kind="NODE", target_ids=("missing_index",)))
    check("bad index definition refutes only the intervention",
          intervention.result == "REFUTES" and
          node.result == "NOT_APPLICABLE")
    dead_tuple = evaluate(
        "dead_tuple_ratio_v2", {"dead_ratio": 0.0},
        context=PredicateContext(target_ids=("table_bloat",)))
    physical_missing = evaluate(
        "physical_bloat_ratio_v2", {"availability": "UNAVAILABLE"},
        context=PredicateContext(target_ids=("table_bloat",)))
    check("dead tuples and unavailable extension do not refute bloat",
          dead_tuple.result == "NEUTRAL" and
          physical_missing.result == "NOT_APPLICABLE")

    print("\n[3] Collection status, window, and source epoch")
    for status in ("UNKNOWN", "ERROR"):
        decision = evaluate(
            "disk_usage_v2", {"used_pct": 99},
            context=PredicateContext(collection_status=status))
        check(f"{status} never supports or refutes",
              decision.result == "NOT_APPLICABLE")
    no_window = evaluate(
        "deadlock_count_v2", {"deadlocks": 0},
        context=PredicateContext())
    wrong_epoch = evaluate(
        "deadlock_count_v2", {"deadlocks": 0, "source_epoch": "epoch-b"},
        context=PredicateContext(
            window_start=10, window_end=20, source_epoch="epoch-b",
            expected_source_epoch="epoch-a"))
    valid_window = evaluate(
        "deadlock_count_v2", {"deadlocks": 0, "source_epoch": "epoch-a"},
        context=PredicateContext(
            window_start=10, window_end=20, source_epoch="epoch-a",
            expected_source_epoch="epoch-a"))
    check("window predicates reject missing/mismatched epochs",
          no_window.result == wrong_epoch.result == "NOT_APPLICABLE")
    check("same-window cumulative delta can refute",
          valid_window.result == "REFUTES")

    print("\n[4] Stable binding identity and raw_ref deduplication")
    store = TraceStore(episode_id)
    structured = {"used_pct": 92.0}
    raw_ref = store.record("get_database_stats", {}, json.dumps(structured),
                           structured)
    binding = EvidenceBinding.create(
        episode_id=episode_id, raw_ref=raw_ref, evidence_type="disk_usage",
        status="OBSERVED", observed_at=20, predicate_id="disk_usage_v2",
        predicate_result="SUPPORTS", structured_value=structured,
        target_node_ids=["disk_pressure"], window_start=10, window_end=20,
        source_epoch="epoch-a")
    duplicate = EvidenceBinding.from_dict(binding.to_dict())
    path = next(path for path in G.enumerate_causal_paths(
        ["disk_growing"], use_learned=False)
        if path.root_node_id == "disk_pressure")
    explanation = G.merge_paths(
        [path], episode_id=episode_id, observed_symptoms=["disk_growing"])
    first = explanation.add_evidence_binding(binding)
    second = explanation.add_evidence_binding(duplicate)
    check("raw_ref/predicate/target yields a stable binding_id",
          binding.binding_id == duplicate.binding_id)
    check("the same scoped raw_ref is counted once",
          first and not second and len(explanation.evidence_bindings) == 1)
    check("raw_ref belongs to the episode and trace exists",
          binding.validate_raw_ref())
finally:
    shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 76)
print("EVIDENCE PREDICATES:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
