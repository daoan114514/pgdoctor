"""主循环 —— 把状态机、工具面、策略接起来。

循环本身不做任何领域判断：它只负责推进阶段、校验转移合法性、
记账与落盘。所有"该做什么"的决定都在策略里，所有"能不能做"的
决定都在状态机里。这个分工是架构 C 的全部意义。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field

from agent.episode_state import EpisodeState
from agent.policy import Policy
from agent.state_machine import Phase, PhaseViolation, StateMachine
from agent.toolbox import Toolbox
from sandbox.env import DBAScenarioEnv


@dataclass
class RunResult:
    episode_id: str
    final_phase: str
    claimed_fault_class: str | None
    claimed_root_cause: str | None
    steps: int
    tool_calls: list[str] = field(default_factory=list)
    transitions: list[tuple] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    elapsed_s: float = 0.0
    error: str = ""


def run_diagnosis(env: DBAScenarioEnv, obs, policy: Policy,
                  max_steps: int = 40, allow_repair: bool = False,
                  quiet: bool = False) -> tuple[RunResult, EpisodeState]:
    st = EpisodeState(episode_id=env.episode_id, scenario_id=env.spec["id"])
    st.alert = obs.alert
    st.baseline_kpi = obs.healthy_kpi
    st.current_kpi = obs.current_kpi
    st.budget["max_steps"] = max_steps
    st.symptoms = _symptoms(obs)
    st.save()

    sm = StateMachine(st, allow_repair=allow_repair)
    tb = Toolbox(env.observe(), st, sm)
    ctx = {
        # 告警里带上慢查询本身 —— 真实场景里 APM 会告诉你哪条查询在拖慢，
        # 让 agent 从零去猜"哪条查询有问题"不是这个项目要解决的问题。
        "hot_query": " ".join(env.spec["workload"]["hot_query"].split()),
        "symptoms": st.symptoms,
    }

    t0 = time.time()
    res = RunResult(episode_id=st.episode_id, final_phase=st.phase,
                    claimed_fault_class=None, claimed_root_cause=None, steps=0)

    try:
        while not sm.terminal():
            if st.exhausted():
                st.outcome_note = "步数预算耗尽"
                sm.goto(Phase.ESCALATE, "budget exhausted")
                break
            cur = sm.phase
            nxt = policy.run_phase(cur, tb, st, ctx)
            if not quiet:
                print(f"  {cur.value:<12} -> {nxt.value:<12} "
                      f"(累计 {st.budget['steps']} 步)")
            sm.goto(nxt, f"policy={policy.name}")
            st.save()
    except PhaseViolation as exc:
        # 状态机拦下了越界动作。这不是崩溃，是设计生效了。
        res.violations.append(str(exc))
        st.outcome_note = f"阶段违规: {exc}"
        try:
            sm.goto(Phase.ESCALATE, "phase violation")
        except PhaseViolation:
            pass
    except Exception as exc:
        res.error = f"{type(exc).__name__}: {exc}"
        st.outcome_note = res.error

    st.finished = True
    st.save()

    res.final_phase = st.phase
    res.claimed_fault_class = st.claimed_fault_class
    res.claimed_root_cause = st.claimed_root_cause
    res.steps = st.budget["steps"]
    res.tool_calls = tb.calls
    res.transitions = sm.history
    res.elapsed_s = round(time.time() - t0, 1)
    return res, st


def _symptoms(obs) -> list[str]:
    s = []
    b, c = obs.healthy_kpi, obs.current_kpi
    if c.get("p99_ms", 0) > b.get("p99_ms", 0) * 3:
        s.append(f"p99 上升 {c['p99_ms'] / max(b.get('p99_ms', 1), 1):.0f}x")
    if c.get("cpu_pct", 0) > b.get("cpu_pct", 0) * 2:
        s.append(f"CPU 上升 {c['cpu_pct'] / max(b.get('cpu_pct', 1), 1):.0f}x")
    if c.get("errors", 0) > 0:
        s.append(f"错误 {c['errors']}")
    return s
