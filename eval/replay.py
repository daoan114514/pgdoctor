"""轨迹重放 —— 让离线实验不必重新调模型。

单个 LLM episode 约 $0.35~0.5。W8 的规模曲线实验要在 held-out 集上跑
几十个 episode，直接跑的话额度撑不住。

但很多分析根本不需要重新调模型：episode 的执行轨迹（跑了哪些查询、
拿到什么返回、台账怎么演变）已经完整落盘，ESC 的判定、判分的复核、
以及"换一组阈值会怎样"这类消融，都可以在轨迹上离线重算。

这也让实验可复现：同一份轨迹重跑一百遍结果一样，而重新调模型每次
都会有随机性。
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from agent import esc as esc_mod
from agent.episode_state import EpisodeState

ROOT = Path(__file__).resolve().parent.parent
TRACES = ROOT / "traces"


@dataclass
class ReplayResult:
    episode_id: str
    scenario_id: str
    claimed_fault_class: str | None
    esc_verdict: str = ""
    esc_dims: dict = field(default_factory=dict)
    esc_directives: list = field(default_factory=list)
    steps: int = 0
    evidence_types: list = field(default_factory=list)
    ledger: dict = field(default_factory=dict)
    attempts: int = 0


def list_episodes() -> list[str]:
    if not TRACES.exists():
        return []
    return sorted(p.parent.name for p in TRACES.glob("ep_*/episode_state.json"))


def load(episode_id: str) -> EpisodeState:
    return EpisodeState.load(episode_id)


def replay_esc(episode_id: str, candidates: list[str] | None = None,
               min_refute_ratio: float = 0.5) -> ReplayResult:
    """在已落盘的轨迹上重跑证据充分性检查。

    不调用模型、不碰数据库 —— 纯粹拿当时实际取到的证据重新判一次。
    调阈值做敏感性分析时尤其有用。
    """
    st = load(episode_id)
    rep = esc_mod.check(st, candidates=candidates,
                        min_refute_ratio=min_refute_ratio)
    return ReplayResult(
        episode_id=episode_id,
        scenario_id=st.scenario_id,
        claimed_fault_class=st.claimed_fault_class,
        esc_verdict=rep.verdict,
        esc_dims={d.name: d.passed for d in rep.dims},
        esc_directives=rep.directives,
        steps=st.budget.get("steps", 0),
        evidence_types=sorted({e["evidence_type"] for e in st.scratchpad}),
        ledger={k: v.verdict for k, v in st.ledger.items()},
        attempts=len(st.attempts),
    )


def replay_all(candidates: list[str] | None = None,
               min_refute_ratio: float = 0.5) -> list[ReplayResult]:
    out = []
    for eid in list_episodes():
        try:
            out.append(replay_esc(eid, candidates, min_refute_ratio))
        except Exception:
            continue
    return out


def sensitivity(ratios: list[float] | None = None) -> dict:
    """ESC 排除率阈值的敏感性分析 —— 纯离线，零成本。

    只统计裁决分布，**不与 ground truth 比对**。这个区别曾经代价很大：
    D2 因为症状没归一到图节点 id 而无条件通过（44/44），这个函数照样
    能跑，跑出来的是一条完全平坦的曲线 —— 而"所有阈值结果都一样"读起来
    像个无聊结论，不像故障信号。那道闸因此一直没通电也没人发现。

    要区分"阈值没被压到"和"阈值压到了但拦错了人"，必须把裁决和真实
    根因对起来算四个格子（放行且对／放行但错／拦截但对／拦截且错）。
    那个在 .dev/threshold_ablation.py，它才是能证伪的那个。
    """
    ratios = ratios or [0.0, 0.34, 0.5, 0.67, 1.0]
    out: dict[str, dict] = {}
    for r in ratios:
        res = replay_all(min_refute_ratio=r)
        counts: dict[str, int] = {}
        for x in res:
            counts[x.esc_verdict] = counts.get(x.esc_verdict, 0) + 1
        out[f"{r:.2f}"] = {"n": len(res), **counts}
    return out


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "sensitivity":
        print(json.dumps(sensitivity(), ensure_ascii=False, indent=2))
    else:
        for r in replay_all():
            print(f"{r.episode_id[:46]:<46} {str(r.claimed_fault_class):<16} "
                  f"{r.esc_verdict:<14} 证据 {len(r.evidence_types)} 类")
