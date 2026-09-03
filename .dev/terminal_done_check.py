"""Independent acceptance checks that REPORT/ESCALATE persist and reach DONE."""
from __future__ import annotations

import shutil
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.episode_state import EpisodeState
from agent.loop import run_episode
from agent.policy import Policy
from agent.state_machine import Phase, StateMachine
from sandbox.traces import TRACE_DIR


ok = True


def check(label: str, condition: bool, detail="") -> None:
    global ok
    ok &= bool(condition)
    print(f"  {'PASS' if condition else 'FAIL'}  {label:<56} {detail}")


class FinalPolicy(Policy):
    name = "controlled_finalizer"

    def __init__(self, final_phase):
        self.final_phase = final_phase

    def run_phase(self, phase, _toolbox, _state, _ctx):
        return {
            Phase.MONITOR: Phase.OBSERVE,
            Phase.OBSERVE: Phase.HYPOTHESIZE,
            Phase.HYPOTHESIZE: Phase.INVESTIGATE,
            Phase.INVESTIGATE: Phase.DIAGNOSE,
            Phase.DIAGNOSE: self.final_phase,
        }[phase]


class Env:
    def __init__(self, episode_id):
        self.episode_id = episode_id
        self.spec = {
            "id": "controlled_terminal", "revision": 2,
            "workload": {"hot_query": "SELECT 1"},
        }

    @staticmethod
    def observe():
        return object()


print("[1] DONE is the only terminal state")
state = EpisodeState("terminal_unit", "controlled", phase=Phase.REPORT.value)
machine = StateMachine(state)
check("REPORT is not terminal", not machine.terminal())
state.phase = Phase.ESCALATE.value
check("ESCALATE is not terminal", not machine.terminal())
state.phase = Phase.DONE.value
check("DONE is terminal", machine.terminal())

episode_ids = []
try:
    print("\n[2] Both finalization routes execute and persist")
    for requested in (Phase.REPORT, Phase.ESCALATE):
        episode_id = f"terminal_{requested.value.lower()}_{uuid.uuid4().hex}"
        episode_ids.append(episode_id)
        obs = SimpleNamespace(
            alert="p99 latency alert",
            healthy_kpi={"p99_ms": 10.0, "cpu_pct": 10.0, "errors": 0},
            current_kpi={"p99_ms": 100.0, "cpu_pct": 10.0, "errors": 0})
        result, final_state = run_episode(
            Env(episode_id), obs, FinalPolicy(requested), max_steps=20,
            allow_repair=False, quiet=True, use_esc=False,
            use_cases=False, use_learned=False)
        loaded = EpisodeState.load(episode_id)
        transitions = [(source, target) for source, target, _ in
                       result.transitions]
        check(f"{requested.value} transitions to DONE",
              (requested.value, "DONE") in transitions and
              result.final_phase == "DONE")
        check(f"{requested.value} persists finished/final report",
              final_state.finished and loaded.finished and
              loaded.phase == "DONE" and bool(loaded.final_report))
finally:
    for episode_id in episode_ids:
        shutil.rmtree(TRACE_DIR / episode_id, ignore_errors=True)

print("\n" + "=" * 78)
print("TERMINAL DONE:", "PASS" if ok else "FAIL")
raise SystemExit(0 if ok else 1)
