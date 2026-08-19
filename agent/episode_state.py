"""EpisodeState —— 持久真相源。

核心不变式：上下文是可丢弃的缓存，这个对象才是真相。
任何系统正确性依赖的东西（当前阶段、假设台账、证据引用、回滚句柄、
基线 KPI）都不许只存在于上下文里，否则一次压缩就可能让 agent 失忆、
重复调查、甚至重复执行已经失败过的修复。

它落盘保存，所以进程崩溃后也能恢复。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Verdict(str, Enum):
    UNTESTED = "UNTESTED"
    CONFIRMED = "CONFIRMED"
    REFUTED = "REFUTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    # 比只读证据强得多的反证：真修了，没用。
    # 它的存在是为了堵死"忘了修复失败过 -> 重新推导出同一个根因 -> 再修一次"
    # 这个无限重试循环。
    REFUTED_BY_REMEDIATION = "REFUTED_BY_REMEDIATION"


@dataclass
class EvidenceRef:
    """指向轨迹里一次真实的工具调用。ESC 核验的是这个，不是 agent 的自述。"""
    kind: str                 # 证据类型，来自受控词表
    raw_ref: str              # trace://episode/step_NNN
    summary: str
    bears_on: list[str] = field(default_factory=list)   # 这条线索关系到哪些假设


@dataclass
class HypothesisEntry:
    verdict: str = Verdict.UNTESTED.value
    evidence: list[EvidenceRef] = field(default_factory=list)
    note: str = ""


@dataclass
class RemediationAttempt:
    """失败的修复尝试。知识单调增长的载体 —— 数据库回滚，但这条记录不回滚。"""
    root_cause: str
    sql: str
    predicted: dict
    actual: dict
    verdict: str
    rolled_back: bool = False
    inference: str = ""


@dataclass
class EpisodeState:
    episode_id: str
    scenario_id: str
    phase: str = "MONITOR"
    alert: str = ""
    baseline_kpi: dict = field(default_factory=dict)
    current_kpi: dict = field(default_factory=dict)
    symptoms: list[str] = field(default_factory=list)
    ledger: dict[str, HypothesisEntry] = field(default_factory=dict)
    scratchpad: list[dict] = field(default_factory=list)     # append-only
    attempts: list[RemediationAttempt] = field(default_factory=list)
    undo_refs: list[str] = field(default_factory=list)
    directives: list[str] = field(default_factory=list)      # ESC 给的定向取证指令
    claimed_root_cause: str | None = None
    claimed_fault_class: str | None = None
    budget: dict = field(default_factory=lambda: {"steps": 0, "max_steps": 40})
    started_at: float = field(default_factory=time.time)
    finished: bool = False
    outcome_note: str = ""

    # ── 台账 ──────────────────────────────────────────────
    def ensure_hypotheses(self, names: list[str]) -> None:
        for n in names:
            self.ledger.setdefault(n, HypothesisEntry())

    def set_verdict(self, name: str, verdict: Verdict | str,
                    evidence: list[EvidenceRef] | None = None,
                    note: str = "") -> None:
        e = self.ledger.setdefault(name, HypothesisEntry())
        e.verdict = verdict.value if isinstance(verdict, Verdict) else verdict
        if evidence:
            e.evidence.extend(evidence)
        if note:
            e.note = note

    def confirmed(self) -> list[str]:
        return [k for k, v in self.ledger.items()
                if v.verdict == Verdict.CONFIRMED.value]

    def refuted(self) -> list[str]:
        return [k for k, v in self.ledger.items()
                if v.verdict in (Verdict.REFUTED.value,
                                 Verdict.REFUTED_BY_REMEDIATION.value)]

    def untested(self) -> list[str]:
        return [k for k, v in self.ledger.items()
                if v.verdict == Verdict.UNTESTED.value]

    # ── 便签（append-only）───────────────────────────────
    def note(self, author: str, kind: str, observation: str,
             raw_ref: str = "", bears_on: list[str] | None = None) -> None:
        self.scratchpad.append({
            "seq": len(self.scratchpad) + 1,
            "ts": time.time(),
            "author": author,
            "evidence_type": kind,
            "observation": observation,
            "raw_ref": raw_ref,
            "bears_on": bears_on or [],
        })

    # ── 失败尝试：知识不回滚 ──────────────────────────────
    def record_attempt(self, attempt: RemediationAttempt) -> None:
        self.attempts.append(attempt)
        self.set_verdict(attempt.root_cause, Verdict.REFUTED_BY_REMEDIATION,
                         note=attempt.inference)

    def already_failed(self, root_cause: str) -> bool:
        return any(a.root_cause == root_cause for a in self.attempts)

    def spend(self, n: int = 1) -> bool:
        self.budget["steps"] += n
        return self.budget["steps"] < self.budget["max_steps"]

    def exhausted(self) -> bool:
        return self.budget["steps"] >= self.budget["max_steps"]

    # ── 持久化 ────────────────────────────────────────────
    def path(self) -> Path:
        d = ROOT / "traces" / self.episode_id
        d.mkdir(parents=True, exist_ok=True)
        return d / "episode_state.json"

    def save(self) -> None:
        self.path().write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8")

    @classmethod
    def load(cls, episode_id: str) -> "EpisodeState":
        p = ROOT / "traces" / episode_id / "episode_state.json"
        d = json.loads(p.read_text(encoding="utf-8"))
        st = cls(episode_id=d["episode_id"], scenario_id=d["scenario_id"])
        for k, v in d.items():
            if k == "ledger":
                st.ledger = {
                    name: HypothesisEntry(
                        verdict=e["verdict"],
                        evidence=[EvidenceRef(**r) for r in e["evidence"]],
                        note=e.get("note", ""))
                    for name, e in v.items()}
            elif k == "attempts":
                st.attempts = [RemediationAttempt(**a) for a in v]
            elif hasattr(st, k):
                setattr(st, k, v)
        return st

    # ── 上下文投影 ────────────────────────────────────────
    def render_context(self) -> str:
        """把状态投影成一段紧凑的上下文。

        回退时不恢复旧上下文，而是丢弃后从这里重建 —— 重建比恢复更好：
        没有失败尝试的噪声、token 更省，而且重试次数不会让上下文线性膨胀。
        """
        L = []
        L.append(f"# 事故 {self.episode_id}")
        L.append(f"告警: {self.alert}")
        if self.baseline_kpi:
            b, c = self.baseline_kpi, self.current_kpi
            L.append(f"KPI: p99 {b.get('p99_ms')}ms -> {c.get('p99_ms')}ms | "
                     f"cpu {b.get('cpu_pct')}% -> {c.get('cpu_pct')}%")
        if self.symptoms:
            L.append("症状: " + ", ".join(self.symptoms))

        if self.ledger:
            L.append("\n## 假设台账")
            for name, e in self.ledger.items():
                ev = f" [{len(e.evidence)} 条证据]" if e.evidence else ""
                nt = f" — {e.note}" if e.note else ""
                L.append(f"- {name}: {e.verdict}{ev}{nt}")

        if self.attempts:
            L.append("\n## 已尝试过的修复（不要重复）")
            for a in self.attempts:
                L.append(f"- {a.root_cause}: {a.sql[:70]} -> {a.verdict}"
                         f"{' (已回滚)' if a.rolled_back else ''}")
                if a.inference:
                    L.append(f"  推断: {a.inference}")

        if self.scratchpad:
            L.append("\n## 证据便签")
            for e in self.scratchpad[-12:]:
                bo = f" ->{','.join(e['bears_on'])}" if e["bears_on"] else ""
                L.append(f"- [{e['evidence_type']}]{bo} {e['observation'][:110]}")

        if self.directives:
            L.append("\n## 待补取证（ESC 判定证据不足）")
            for d in self.directives:
                L.append(f"- {d}")

        L.append(f"\n阶段: {self.phase} | 步数 {self.budget['steps']}/"
                 f"{self.budget['max_steps']}")
        return "\n".join(L)
