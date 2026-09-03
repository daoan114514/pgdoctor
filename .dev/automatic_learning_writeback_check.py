"""Acceptance checks for run_episode-owned v2 scoring and learning writes."""
from __future__ import annotations

import shutil
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.loop import run_episode
from agent.policy import Policy
from agent.state_machine import Phase
from knowledge import case_store, evolution
from sandbox.traces import TRACE_DIR


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<58} {detail}")


@dataclass
class Score:
    diagnosis: bool = True
    outcome: bool = False
    safe_pass: bool = False
    non_destructive: bool = True
    diagnosis_strict: bool = False
    details: dict = None


class LearningPolicy(Policy):
    name = "automatic-learning-fixture"

    def run_phase(self, phase, _toolbox, state, _ctx):
        if not state.evidence_task_audit:
            context = {
                "frontier_signature": "frontier_fixture",
                "evidence_state_signature": "evidence_fixture",
                "p0_signature": "p0_fixture",
                "capability_signature": "capability_fixture",
                "evidence_need_signature": "need_fixture",
                "graph_version": "graph_fixture",
                "scenario_revision": 2,
                "tool_schema_version": evolution.V2_TOOL_SCHEMA_VERSION,
            }
            state.evidence_task_audit.append({
                "event": "tool_learning_observation",
                "observation_id": f"observation_{state.episode_id}",
                "tool": "get_blocking_chain",
                "learning_context": context,
                "collection_status": "OBSERVED",
                "changed_statuses": 1,
                "pruned_paths": 1,
                "required_fulfilled": True,
                "changed_next_decision": True,
                "cost": 0.1,
                "latency_s": 0.1,
                "covered_need_count": 1,
                "entropy_gain": 0.8,
                "posterior_change": 0.4,
                "duplicate_calls": 0,
            })
        return Phase.ESCALATE


class Env:
    def __init__(self, episode_id: str, split: str):
        self.episode_id = episode_id
        self.spec = {
            "id": f"automatic_learning_{split}",
            "split": split,
            "revision": 2,
            "workload": {"hot_query": "SELECT 1"},
        }
        self.score_calls = 0

    @staticmethod
    def observe():
        return object()

    def score(self, *_args, **_kwargs):
        self.score_calls += 1
        return Score(details={"fixture": True})


class Observation:
    alert = "p99 latency alert"
    healthy_kpi = {"p99_ms": 10.0, "cpu_pct": 10.0, "errors": 0}
    current_kpi = {"p99_ms": 100.0, "cpu_pct": 10.0, "errors": 0}


tmp = Path(tempfile.mkdtemp(prefix="pgdoctor_auto_learning_"))
old_evolution = evolution.LEARNED
old_cases_dir = case_store.LEARNED_V2
old_cases_file = case_store.CASES_V2
episode_ids: list[str] = []
try:
    evolution.LEARNED = tmp / "learned"
    case_store.LEARNED_V2 = evolution.LEARNED / "v2"
    case_store.CASES_V2 = case_store.LEARNED_V2 / "cases.yaml"

    print("[1] train episode writes from run_episode")
    train_id = f"automatic_train_{uuid.uuid4().hex}"
    episode_ids.append(train_id)
    train_env = Env(train_id, "train")
    train_result, train_state = run_episode(
        train_env, Observation(), LearningPolicy(), quiet=True,
        use_cases=False, learned_layers={"l2", "l4"})
    check("run_episode scores exactly once", train_env.score_calls == 1)
    check("benchmark score is returned and persisted",
          train_result.benchmark_score.get("diagnosis") is True and
          train_state.benchmark_score == train_result.benchmark_score)
    check("enabled L2/L4 updates are written automatically",
          train_result.learning_result.get("l2") == 1 and
          train_result.learning_result.get("l4") == 1,
          train_result.learning_result)
    loaded = EpisodeState.load(train_id)
    check("learning result survives state round-trip",
          loaded.learning_result == train_state.learning_result)

    print("\n[2] eval and disabled runs cannot mutate learning")
    before_eval = {
        path.name: path.read_bytes()
        for path in (evolution.LEARNED / "v2").glob("*.yaml")
    }
    eval_id = f"automatic_eval_{uuid.uuid4().hex}"
    episode_ids.append(eval_id)
    eval_result, _ = run_episode(
        Env(eval_id, "eval"), Observation(), LearningPolicy(), quiet=True,
        use_cases=False, learned_layers={"l2", "l4"})
    after_eval = {
        path.name: path.read_bytes()
        for path in (evolution.LEARNED / "v2").glob("*.yaml")
    }
    check("eval episode is scored but writes no learning",
          eval_result.learning_result.get("written") is False and
          before_eval == after_eval, eval_result.learning_result)

    off_id = f"automatic_off_{uuid.uuid4().hex}"
    episode_ids.append(off_id)
    off_result, _ = run_episode(
        Env(off_id, "train"), Observation(), LearningPolicy(), quiet=True,
        use_cases=False, use_learned=False,
        learned_layers={"l2", "l4"})
    after_off = {
        path.name: path.read_bytes()
        for path in (evolution.LEARNED / "v2").glob("*.yaml")
    }
    check("learned=False is a zero-write ablation",
          off_result.learning_result.get("enabled_layers") == [] and
          after_eval == after_off)
finally:
    evolution.LEARNED = old_evolution
    case_store.LEARNED_V2 = old_cases_dir
    case_store.CASES_V2 = old_cases_file
    shutil.rmtree(tmp, ignore_errors=True)
    for episode_id in episode_ids:
        shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 78)
print("AUTOMATIC LEARNING WRITEBACK:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
