"""Toolbox —— agent 能做的全部动作，且每次调用都过阶段校验。

这一层刻意与模型无关：脚本化策略和 LLM 策略用的是同一套工具，
所以"换成 LLM"不改变可用动作集合，两者的对比才有意义。
W4 起它会被包成 MCP server，阶段校验则由 PreToolUse hook 承担。

每次只读调用都会自动往台账里记一条 EvidenceRef —— 证据充分性检查
读的是这些落盘记录，不是 agent 的自述，所以取证行为无法被伪造。
"""
from __future__ import annotations

from dataclasses import asdict

from agent.episode_state import EpisodeState, EvidenceRef, Verdict
from agent.state_machine import StateMachine
from sandbox.observe import Observer


class Toolbox:
    def __init__(self, observer: Observer, state: EpisodeState, sm: StateMachine):
        self.o = observer
        self.st = state
        self.sm = sm
        self.calls: list[str] = []

    def _enter(self, tool: str) -> None:
        self.sm.assert_tool(tool)
        self.calls.append(tool)
        if not self.st.spend():
            raise RuntimeError("预算耗尽")

    def _evidence(self, kind: str, raw_ref: str, summary: str,
                  bears_on: list[str] | None = None) -> EvidenceRef:
        ref = EvidenceRef(kind=kind, raw_ref=raw_ref, summary=summary,
                          bears_on=bears_on or [])
        self.st.note("agent", kind, summary, raw_ref, bears_on or [])
        return ref

    # ── 只读观测 ──────────────────────────────────────────
    def explain_query(self, sql: str, params: dict | None = None) -> dict:
        self._enter("explain_query")
        try:
            d = self.o.explain_query(sql, params)
        except Exception as exc:
            # 只读连接 EXPLAIN 不了写操作。这不是 bug 而是权限隔离的
            # 必然结果 —— 把原因如实返回，让 agent 换用 pg_locks 之类
            # 的取证手段，而不是让 episode 崩掉。
            msg = f"{type(exc).__name__}: {exc}"[:200]
            self._evidence("explain_unavailable", "",
                           f"无法取执行计划（{msg}）；若为写操作，"
                           f"只读连接无权 EXPLAIN，请改用锁与会话视图取证",
                           bears_on=["lock_contention"])
            return {"error": msg, "scan_types": [],
                    "rows_removed_by_filter": 0, "indexes_used": [],
                    "total_time_ms": 0.0, "rows_est_vs_actual": [],
                    "parallel_workers": 0, "top_nodes": [], "raw_ref": ""}
        scan = "Seq Scan" if any("Seq Scan" in s for s in d.scan_types) else (
            "Index Scan" if any("Index Scan" in s for s in d.scan_types) else "other")
        kind = "explain_seq_scan" if scan == "Seq Scan" else "explain_plan"
        self._evidence(
            kind, d.raw_ref,
            f"{d.total_time_ms}ms, {scan}, Rows Removed by Filter="
            f"{d.rows_removed_by_filter:,}, 用到索引={d.indexes_used or '无'}",
            bears_on=["missing_index", "stale_statistics"])
        return asdict(d)

    def get_indexes(self, table: str = "orders") -> list[dict]:
        self._enter("get_indexes")
        rows = self.o.get_indexes(table)
        self._evidence("index_existence", "",
                       f"{table} 上的索引: {[r['name'] for r in rows]}",
                       bears_on=["missing_index"])
        return rows

    def get_table_stats(self, table: str = "orders") -> dict:
        self._enter("get_table_stats")
        s = self.o.get_table_stats(table)
        self._evidence(
            "stats_freshness", "",
            f"{table}: live={s.n_live_tup:,} dead={s.n_dead_tup:,} "
            f"dead_ratio={s.dead_ratio} last_analyze={s.last_analyze[:19] or '空'}",
            bears_on=["stale_statistics", "table_bloat"])
        return asdict(s)

    def get_top_queries(self, n: int = 5) -> list[dict]:
        self._enter("get_top_queries")
        rows = self.o.get_top_queries(n)
        top = rows[0] if rows else {}
        self._evidence("slow_query_ranking", "",
                       f"最慢查询 mean={top.get('mean_ms')}ms calls={top.get('calls')} "
                       f": {str(top.get('query'))[:60]}",
                       bears_on=["missing_index", "stale_statistics"])
        return rows

    def get_active_sessions(self) -> list[dict]:
        self._enter("get_active_sessions")
        rows = self.o.get_active_sessions()
        waits = [r.wait_event for r in rows if r.wait_event]
        self._evidence("session_wait_profile", "",
                       f"{len(rows)} 个异常会话，等待事件={waits or '无'}",
                       bears_on=["lock_contention"])
        return [asdict(r) for r in rows]

    def get_blocking_chain(self) -> list[dict]:
        self._enter("get_blocking_chain")
        rows = self.o.get_blocking_chain()
        self._evidence("lock_blocking_chain", "",
                       f"阻塞链 {len(rows)} 条" + (f": {rows[:2]}" if rows else "（无锁等待）"),
                       bears_on=["lock_contention"])
        return rows

    def simulate_index(self, create_sql: str, test_sql: str,
                       params: dict | None = None) -> dict:
        """反事实验证：不改生产就能预先证伪一个"缺索引"的判断。"""
        self._enter("simulate_index")
        r = self.o.simulate_index(create_sql, test_sql, params)
        self._evidence(
            "counterfactual_index", "",
            f"hypopg: cost {r['cost_before']:,.0f} -> {r['cost_after']:,.0f} "
            f"(降 {r['cost_reduction_pct']}%), 优化器会采用={r['would_be_used']}",
            bears_on=["missing_index"])
        return r

    # ── 推理（内部动作，不碰数据库）────────────────────────
    def note_evidence(self, kind: str, observation: str,
                      bears_on: list[str] | None = None) -> str:
        self._enter("note_evidence")
        self.st.note("agent", kind, observation, "", bears_on or [])
        return "recorded"

    def set_hypothesis(self, name: str, verdict: str, note: str = "") -> str:
        self._enter("set_hypothesis")
        v = Verdict(verdict)
        if v is Verdict.REFUTED_BY_REMEDIATION:
            raise ValueError("该裁决只能由修复失败产生，不能由 agent 直接声明")
        cur = self.st.ledger.get(name)
        if cur and cur.verdict == Verdict.REFUTED_BY_REMEDIATION.value:
            # 否则重新调查一轮就能把修复反证覆盖掉，无限重试循环又回来了
            raise ValueError(
                f"{name} 已被修复反证，不能用只读证据翻案；"
                f"若确有新证据请换用其他假设")
        self.st.set_verdict(name, v, note=note)
        return f"{name} = {v.value}"

    def declare_root_cause(self, fault_class: str, root_cause: str) -> str:
        self._enter("declare_root_cause")
        if self.st.already_failed(fault_class):
            raise ValueError(
                f"{fault_class} 此前修复失败并已被反证；除非有新证据，否则不能重提")
        self.st.claimed_fault_class = fault_class
        self.st.claimed_root_cause = root_cause
        self.st.set_verdict(fault_class, Verdict.CONFIRMED)
        return f"根因已声明: {fault_class}"

    # ── 提交修复提案（不写库）────────────────────────────
    def submit_proposal(self, action_type: str, sql: str, rollback: str,
                        rationale: str = "", predicted_impact: dict | None = None
                        ) -> str:
        """把修复意图交给安全门。这里不执行任何东西。

        要求类型化而非裸 SQL：门要在结构上做防伪校验（声称建索引却夹带
        DROP 的提案会被 AST 拆穿），裸字符串没法做这件事。
        """
        self._enter("submit_proposal")
        if not rollback or not rollback.strip():
            raise ValueError("提案必须带回滚语句，否则无法保证可撤销")
        if self.st.claimed_fault_class and self.st.already_failed(
                self.st.claimed_fault_class):
            raise ValueError("该根因此前修复失败并已被反证，不能重复提交")
        if self.st.tried_fix(sql):
            raise ValueError("这条修复已经试过且失败，换一个方案")
        self.st.proposal = {
            "action_type": action_type,
            "sql": sql,
            "rollback": rollback,
            "rationale": rationale,
            "predicted_impact": predicted_impact or {},
            "evidence_refs": [e["raw_ref"] for e in self.st.scratchpad
                              if e.get("raw_ref")][-5:],
        }
        self.st.note("agent", "remediation_proposal",
                     f"{action_type}: {sql[:90]}")
        return f"提案已提交，等待安全门裁决: {action_type}"
