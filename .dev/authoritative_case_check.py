"""Validate the 100 source-grounded replay cases and reviewed L1 split."""
from __future__ import annotations

import collections
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from eval.build_authoritative_cases import ROOTS, build
from knowledge import case_store
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import PredicateContext, evaluate


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<60} {detail}")


dataset_path = ROOT / "eval" / "authoritative_cases_v2.yaml"
source_path = ROOT / "eval" / "authoritative_sources.yaml"
document = yaml.safe_load(dataset_path.read_text(encoding="utf-8"))
sources = yaml.safe_load(source_path.read_text(encoding="utf-8"))["sources"]
cases = document["cases"]

print("[1] Dataset size, split, and root coverage")
splits = collections.Counter(item["split"] for item in cases)
roots = collections.Counter(item["fault_class"] for item in cases)
check("dataset contains exactly 100 unique cases",
      len(cases) == len({item["case_id"] for item in cases}) == 100)
check("train/eval is exactly 70/30", splits == {"train": 70, "eval": 30}, splits)
check("all 14 live graph fault classes are covered", set(roots) == set(ROOTS), roots)
check("every fault has at least two held-out examples",
      all(sum(item["split"] == "eval" and item["fault_class"] == root
              for item in cases) >= 2 for root in ROOTS))
check("checked-in dataset is reproducible from the generator",
      document == build())

print("\n[2] Source and graph provenance")
check("source catalog uses authoritative HTTPS pages",
      all(value["url"].startswith("https://") and value.get("publisher")
          for value in sources.values()))
check("every case carries a resolvable source and fidelity notice",
      all(item.get("fidelity") == "source_grounded_controlled_replay" and
          item.get("source_grounding") and item.get("source_refs") and
          all(ref.get("source_id") in sources and ref.get("url") ==
              sources[ref["source_id"]]["url"] for ref in item["source_refs"])
          for item in cases))
check("all expected paths use the current graph version",
      document["generated_from_graph_version"] == G.graph_version() and
      all(len(item["expected"]["edge_ids"]) >= 1 for item in cases))

print("\n[3] Structured predicate quality")
bad_bindings = []
bad_selected = []
bad_p0 = []
allowed_buckets = {"up_20x+", "up_5x", "up_2x", "down", "normal"}
for item in cases:
    by_target_type = {}
    for binding in item["decisive_evidence_bindings"]:
        context = PredicateContext(
            target_kind="NODE",
            target_ids=tuple(binding["target_node_ids"]),
            collection_status=binding["status"],
            window_start=binding.get("window_start"),
            window_end=binding.get("window_end"),
            source_epoch=binding.get("source_epoch", ""),
        )
        actual = evaluate(binding["predicate_id"], binding["structured_value"],
                          context=context).result
        if actual != binding["predicate_result"]:
            bad_bindings.append((item["case_id"], binding["binding_id"], actual))
        by_target_type[(binding["target_node_ids"][0],
                        binding["evidence_type"])] = binding
    for cause in item["expected"]["node_ids"][:-1]:
        for evidence_type in G.required_evidence(cause):
            binding = by_target_type.get((cause, evidence_type))
            if binding is None or binding["predicate_result"] != "SUPPORTS":
                bad_selected.append((item["case_id"], cause, evidence_type))
    for cause, obligation in item["p0_expectations"].items():
        if obligation["status"] not in {"SUPPORTED", "REFUTED"}:
            bad_p0.append((item["case_id"], cause, obligation["status"]))

check("stored predicate directions recompute exactly", not bad_bindings,
      bad_bindings[:3])
check("every selected path cause has supporting required evidence",
      not bad_selected, bad_selected[:3])
check("every reachable P0 is individually resolved", not bad_p0, bad_p0[:3])
check("fingerprints only use runtime-reachable metric buckets",
      all(set(item["fingerprint"]["metric_deltas"].values()) <= allowed_buckets
          for item in cases))
check("each root has multiple fingerprint variants",
      all(len({str(item["fingerprint"]["metric_deltas"])
               for item in cases if item["fault_class"] == root}) >= 3
          for root in ROOTS))

print("\n[4] L1 contamination and retrieval quality gates")
l1 = case_store.load_cases_v2()
eligible = [case for case in l1 if case.training_eligible and case.status == "active"]
legacy = [case for case in l1 if case.case_id.startswith("fixture_")]
check("L1 has exactly the 70 reviewed train seeds", len(eligible) == 70,
      len(eligible))
check("no eval ID entered the learned case store",
      all(case.split == "train" and "_06" not in case.case_id and
          "_07" not in case.case_id and "_08" not in case.case_id
          for case in eligible))
check("old empty-evidence fixtures remain audited but quarantined",
      len(legacy) == 2 and all(case.status == "quarantined" and
                               not case.training_eligible for case in legacy))
check("eligible L1 seeds have source, evidence, selected path, and review",
      all(case.source_refs and case.decisive_evidence_bindings and
          case.selected_path_ids and case.review_status == "approved" and
          case.evidence_quality == "source_grounded_structured_replay"
          for case in eligible))

print("\n" + "=" * 78)
print("AUTHORITATIVE CASE DATASET:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
