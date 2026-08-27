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

        # 估计与实际行数的偏差单独记一条：它是统计过期的判别特征，
        # 混在执行计划摘要里容易被忽略（实测子 agent 只看了 last_analyze
        # 时间戳就把 stale_statistics 判成 REFUTED，而同一次取证里
        # 偏差高达 4200 倍）。
        worst = 0.0
        for est, act in (d.rows_est_vs_actual or []):
            if est > 0 and act > 0:
                worst = max(worst, max(est / act, act / est))
        if worst > 0:
            self._evidence(
                "row_estimate_deviation", d.raw_ref,
                f"估计与实际行数最大偏差 {worst:.0f} 倍 "
                f"(明细 {d.rows_est_vs_actual[:3]})；"
                f"偏差 >10 倍通常意味着统计信息失真",
                bears_on=["stale_statistics"])
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

    def get_connection_stats(self) -> dict:
        self._enter("get_connection_stats")
        r = self.o.get_connection_stats()
        self._evidence(
            "connection_count", "",
            f"连接 {r['used']}/{r['max_connections']} ({r['pct']}%), "
            f"逼近上限={r['near_limit']}, "
            f"idle in transaction={r['idle_in_transaction']}, "
            f"按角色={r['by_user']}",
            bears_on=["connection_exhaustion", "long_idle_transaction"])
        return r

    def get_vacuum_horizon(self) -> dict:
        self._enter("get_vacuum_horizon")
        r = self.o.get_vacuum_horizon()
        slot_age = max([x["xmin_age"] for x in r["slots"]] or [0])
        prep_age = max([x["xid_age"] for x in r["prepared_xacts"]] or [0])
        # 四条证据一次落账：它们是同一个 xmin 视界的四个持有者，
        # 分开记会让 agent 查到第一个就收工，而真凶常常是另一个。
        self._evidence(
            "xid_age", "",
            f"XID 年龄 db={r['db_xid_age']:,} 最老表={r['oldest_table']}"
            f"({r['oldest_table_xid_age']:,}), "
            f"占 freeze_max_age {r['wraparound_pct']}%, 风险={r['at_risk']}",
            bears_on=["xid_wraparound_risk", "autovacuum_starvation"])
        self._evidence(
            "backend_xmin_age", "",
            f"最老 backend_xmin 年龄={r['oldest_backend_xmin_age']:,} "
            f"(pid={r['oldest_backend_pid']}); xmin 持有者={r['xmin_holders']}",
            bears_on=["long_idle_transaction", "autovacuum_starvation",
                      "xid_wraparound_risk"])
        self._evidence(
            "replication_slot_age", "",
            f"复制槽 {len(r['slots'])} 个, 最大 xmin 年龄={slot_age:,}; "
            f"明细={r['slots']}",
            bears_on=["stale_replication_slot"])
        self._evidence(
            "prepared_xact_age", "",
            f"预备事务 {len(r['prepared_xacts'])} 个, "
            f"最大 XID 年龄={prep_age:,}",
            bears_on=["orphaned_prepared_transaction"])
        return r

    def get_database_stats(self) -> dict:
        self._enter("get_database_stats")
        r = self.o.get_database_stats()
        self._evidence(
            "deadlock_count", "",
            f"累计死锁={r.get('deadlocks', 0)}, "
            f"回滚={r.get('xact_rollback', 0)}/"
            f"提交={r.get('xact_commit', 0)}",
            bears_on=["deadlock", "lock_contention"])
        self._evidence(
            "temp_file_volume", "",
            f"临时文件 {r.get('temp_files', 0)} 个, "
            f"外溢 {r.get('temp_mb', 0)} MB",
            bears_on=["work_mem_spill", "disk_pressure"])
        self._evidence(
            "checkpoint_stats", "",
            f"检查点 定时={r.get('ckpt_timed', 0)} "
            f"请求式={r.get('ckpt_requested', 0)} "
            f"(请求式占比 {r.get('ckpt_requested_pct', 0)}%), "
            f"写耗时={r.get('ckpt_write_time_ms', 0):.0f}ms",
            bears_on=["checkpoint_pressure"])
        return r

    def simulate_index(self, create_sql: str, test_sql: str,
                       params: dict | None = None) -> dict:
        """反事实验证：不改生产就能预先证伪一个"缺索引"的判断。"""
        self._enter("simulate_index")
        r = self.o.simulate_index(create_sql, test_sql, params)
        desc = (f"hypopg: cost {r['cost_before']:,.0f} -> "
                f"{r['cost_after']:,.0f} (降 {r['cost_reduction_pct']}%), "
                f"优化器会采用={r['would_be_used']}")
        if r.get("trivial_baseline"):
            desc += f"；{r.get('note', '')}"
        self._evidence("counterfactual_index", "", desc,
                       bears_on=["missing_index"])
        return r

    def fetch_raw(self, ref: str) -> dict:
        """按 raw_ref 回取落盘的原文。

        工具层就地萃取只返回结构化摘要，原文按 ref 落盘 —— 这是上下文
        治理的前半截。后半截一直缺着：白名单里有 fetch_raw，Toolbox 却
        没有这个方法，所以模型拿到 raw_ref 也回取不了。失败方向是安全的
        （白名单是超集，多余条目只会调不到），但"按需回取"因此一直只是
        说法，现在补实。
        """
        self._enter("fetch_raw")
        try:
            text = self.o.fetch_raw(ref)
        except Exception as exc:
            return {"ref": ref, "error": f"{type(exc).__name__}: {exc}"[:160]}
        # 回取的目的是看细节，但也不能把整份原文灌回上下文
        return {"ref": ref, "chars": len(text), "text": text[:4000],
                "truncated": len(text) > 4000}

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
        if v is Verdict.CONFIRMED and len(note.strip()) < 15:
            # 确认一个假设必须给出依据。实测出现过 verdict=CONFIRMED
            # 而 note 为空的情况，等于凭空确认，会直接把 ESC 的 D2
            # 推向 AMBIGUOUS。
            raise ValueError(
                f"确认 {name} 必须在 note 里给出证据依据（当前为空或过短）")
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

        # 声明根因比设置假设更重，门槛只能更高不能更低。
        # 必需证据必须已经在轨迹里 —— 这是 ESC 的 D1 会查的东西，
        # 在声明时就查一遍能让模型立刻拿到反馈，而不是等到 ESC 才被打回。
        from knowledge.causal_graph import graph as _G
        required = _G.required_evidence(fault_class)
        got = {e["evidence_type"] for e in self.st.scratchpad}
        missing = [r for r in required if r not in got]
        if missing:
            hints = []
            for m in missing:
                by = _G.load().nodes.get(m, {}).get("obtained_by", "相应工具")
                hints.append(f"{m}(用 {by} 取)")
            raise ValueError(
                f"不能声明 {fault_class}：缺少必需证据 {hints}。"
                f"请先取证，或改声明证据已齐备的那个根因")

        # 别人已经带着依据确认了另一个假设时，不允许无依据地再确认一个 ——
        # 两个 CONFIRMED 会把 ESC 直接推向 AMBIGUOUS
        others = [k for k, v in self.st.ledger.items()
                  if k != fault_class
                  and v.verdict == Verdict.CONFIRMED.value
                  and len(v.note.strip()) >= 15]
        if others and len(root_cause.strip()) < 20:
            raise ValueError(
                f"{others} 已被带依据地确认；要改声明 {fault_class} "
                f"必须在 root_cause 里说明为何它更能解释症状")

        self.st.claimed_fault_class = fault_class
        self.st.claimed_root_cause = root_cause
        self.st.set_verdict(fault_class, Verdict.CONFIRMED,
                            note=root_cause[:200])
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
        # 提前把 action_type 对齐到 AST 的分类，避免模型用了同义词
        # （实测它写 "analyze" 而分类器返回 "vacuum_analyze"，连续被拒两次）
        from safety import shield as _sh
        actual = _sh.classify(sql)
        if action_type != actual:
            self.st.note("agent", "proposal_type_corrected",
                         f"action_type 由 {action_type} 校正为 {actual}")
            action_type = actual
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
