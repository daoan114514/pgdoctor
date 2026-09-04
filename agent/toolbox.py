"""Toolbox —— agent 能做的全部动作，且每次调用都过阶段校验。

这一层刻意与模型无关：脚本化策略和 LLM 策略用的是同一套工具，
所以"换成 LLM"不改变可用动作集合，两者的对比才有意义。
W4 起它会被包成 MCP server，阶段校验则由 PreToolUse hook 承担。

每次只读调用都会自动往台账里记一条 EvidenceRef —— 证据充分性检查
读的是这些落盘记录，不是 agent 的自述，所以取证行为无法被伪造。
"""
from __future__ import annotations

import json
import time
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from agent.episode_state import (EpisodeState, EvidenceRef, EvidenceStatus,
                                 Verdict, evidence_is_observed)
from agent.explanation import canonical_json
from agent.permissions import Role, allowed_tools
from agent.state_machine import PhaseViolation, StateMachine

if TYPE_CHECKING:
    from sandbox.observe import Observer


class Toolbox:
    def __init__(self, observer: Observer, state: EpisodeState, sm: StateMachine,
                 *, role: Role = Role.MAIN, task_context: Any = None,
                 environment_tools: set[str] | None = None,
                 hypothesis: str | None = None,
                 calls: list[str] | None = None,
                 evidence_ref_cache: dict[tuple[str, str], str] | None = None):
        self.o = observer
        self.st = state
        self.sm = sm
        self.role = role
        self.task_context = task_context
        self.environment_tools = environment_tools
        self.hypothesis = hypothesis
        self.calls = calls if calls is not None else []
        self._evidence_ref_cache = (evidence_ref_cache
                                    if evidence_ref_cache is not None else {})

    def scoped(self, *, role: Role, task_context: Any = None,
               environment_tools: set[str] | None = None,
               hypothesis: str | None = None) -> "Toolbox":
        """Create an immutable permission view sharing state and audit logs."""
        return Toolbox(
            self.o, self.st, self.sm, role=role, task_context=task_context,
            environment_tools=environment_tools, hypothesis=hypothesis,
            calls=self.calls, evidence_ref_cache=self._evidence_ref_cache)

    def _enter(self, tool: str,
               call_target: dict[str, str] | None = None) -> None:
        allowed = allowed_tools(
            self.sm.phase, self.role, self.hypothesis,
            task_context=self.task_context,
            environment_tools=self.environment_tools)
        if tool not in allowed:
            raise PhaseViolation(
                f"{self.role.value} 角色在 {self.sm.phase.value} 阶段不允许调用 "
                f"{tool}；有效工具集: {sorted(allowed) or '(无)'}")
        if self.task_context is not None and call_target:
            target_context = (self.task_context.get("target_context", {})
                              if isinstance(self.task_context, dict) else
                              getattr(self.task_context, "target_context", {}))
            for key, actual in call_target.items():
                expected = str(target_context.get(key) or "")
                if not expected:
                    continue
                normal_expected = " ".join(expected.split()).rstrip(";")
                normal_actual = " ".join(str(actual).split()).rstrip(";")
                if normal_actual != normal_expected:
                    raise PhaseViolation(
                        f"{tool} target {key} does not match the assigned "
                        "EvidenceNeed target")
        self.calls.append(tool)
        if not self.st.spend():
            raise RuntimeError("预算耗尽")

    def _evidence(self, kind: str, raw_ref: str, summary: str,
                  bears_on: list[str] | None = None,
                  status: EvidenceStatus | str = EvidenceStatus.OBSERVED,
                  structured_value: Any = None, target_kind: str = "NODE",
                  target_ids: list[str] | None = None,
                  window_start: float | None = None,
                  window_end: float | None = None,
                  source_epoch: str = "") -> EvidenceRef:
        status_value = status.value if isinstance(status, EvidenceStatus) else status
        trace = getattr(self.o, "trace", None)
        if trace is not None:
            source_key = raw_ref or f"{self.calls[-1] if self.calls else 'tool'}:{len(self.calls)}"
            cache_key = (source_key, canonical_json(structured_value))
            evidence_ref = self._evidence_ref_cache.get(cache_key)
            if evidence_ref is None:
                evidence_ref = trace.record(
                    "bind_structured_evidence",
                    {"evidence_type": kind, "source_ref": raw_ref},
                    json.dumps({"source_ref": raw_ref,
                                "structured_value": structured_value},
                               ensure_ascii=False, default=str),
                    structured_value,
                )
                self._evidence_ref_cache[cache_key] = evidence_ref
            raw_ref = evidence_ref
        ref = EvidenceRef(kind=kind, raw_ref=raw_ref, summary=summary,
                          bears_on=bears_on or [], status=status_value)
        from knowledge.causal_graph import graph as causal_graph
        predicate_id = str(causal_graph.load().nodes.get(kind, {}).get(
            "predicate_id", ""))
        self.st.note("agent", kind, summary, raw_ref, bears_on or [],
                     status=status_value, structured_value=structured_value,
                     predicate_id=predicate_id, target_kind=target_kind,
                     target_ids=target_ids, window_start=window_start,
                     window_end=window_end, source_epoch=source_epoch,
                     explanation_id=str(getattr(
                         self.task_context, "explanation_id", "") or ""),
                     explanation_revision=getattr(
                         self.task_context, "explanation_revision", None),
                     evidence_task_id=str(getattr(
                         self.task_context, "task_id", "") or ""),
                     evidence_need_ids=list(getattr(
                         self.task_context, "need_ids", ()) or ()),
                     collection_tool=(self.calls[-1] if self.calls else ""))
        return ref

    def _cumulative_delta(
            self, key: str, current: dict, counters: tuple[str, ...],
            reset_key: str, error: str = "") -> tuple[dict | None, EvidenceStatus, str]:
        """把累计计数器变成相邻两次观测之间的窗口增量。

        首次读取、统计被 reset、计数器回退、字段缺失都没有可解释的窗口，
        因此只能返回 UNKNOWN。查询错误单列为 ERROR，并且不覆盖最后一个
        正常基线，避免暂时性权限/连接错误破坏下一次差分。
        """
        if error:
            return None, EvidenceStatus.ERROR, f"观测失败: {error}"
        missing = [name for name in (*counters, reset_key) if name not in current]
        if missing or not current.get(reset_key):
            return (None, EvidenceStatus.UNKNOWN,
                    f"累计统计缺少字段 {missing or [reset_key]}，无法确认统计周期")

        now = time.time()
        snapshot = {
            "stats_reset": str(current[reset_key]),
            "captured_at": now,
            "values": {name: current[name] for name in counters},
        }
        previous = self.st.cumulative_baselines.get(key)
        self.st.cumulative_baselines[key] = snapshot
        if not previous:
            return (None, EvidenceStatus.UNKNOWN,
                    "已记录累计基线；需要在故障窗口后再次调用 get_database_stats")
        if previous.get("stats_reset") != snapshot["stats_reset"]:
            return (None, EvidenceStatus.UNKNOWN,
                    "统计在两次观测之间被重置；新读数仅作为下一窗口基线")

        old_values = previous.get("values", {})
        if any(name not in old_values for name in counters):
            return (None, EvidenceStatus.UNKNOWN,
                    "上一次累计基线字段不完整；已刷新基线")
        delta = {name: current[name] - old_values[name] for name in counters}
        negative = {name: value for name, value in delta.items() if value < 0}
        if negative:
            return (None, EvidenceStatus.UNKNOWN,
                    f"累计计数器发生回退 {negative}；无法解释该窗口")
        delta["window_s"] = max(0.0, now - float(previous.get("captured_at", now)))
        delta["window_start"] = float(previous.get("captured_at", now))
        delta["window_end"] = now
        delta["source_epoch"] = snapshot["stats_reset"]
        return delta, EvidenceStatus.OBSERVED, ""

    # ── 只读观测 ──────────────────────────────────────────
    def explain_query(self, sql: str, params: dict | None = None) -> dict:
        self._enter("explain_query", {"hot_query": sql})
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
                           bears_on=["lock_contention"],
                           status=EvidenceStatus.ERROR)
            return {"error": msg, "scan_types": [],
                    "rows_removed_by_filter": 0, "indexes_used": [],
                    "total_time_ms": 0.0, "rows_est_vs_actual": [],
                    "parallel_workers": 0, "top_nodes": [], "raw_ref": ""}
        scan = "Seq Scan" if any("Seq Scan" in s for s in d.scan_types) else (
            "Index Scan" if any("Index Scan" in s for s in d.scan_types) else "other")
        kind = "explain_seq_scan" if scan == "Seq Scan" else "explain_plan"
        structured_plan = asdict(d)
        structured_plan.pop("raw_ref", None)
        self._evidence(
            kind, d.raw_ref,
            f"{d.total_time_ms}ms, {scan}, Rows Removed by Filter="
            f"{d.rows_removed_by_filter:,}, 用到索引={d.indexes_used or '无'}",
            bears_on=["missing_index", "stale_statistics"],
            structured_value=structured_plan)

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
                # max_ratio is derived by Toolbox and is not present in the
                # observer's EXPLAIN trace digest.  Persist a separate derived
                # trace so value-digest validation remains exact.
                "row_estimate_deviation", "",
                f"估计与实际行数最大偏差 {worst:.0f} 倍 "
                f"(明细 {d.rows_est_vs_actual[:3]})；"
                f"偏差 >10 倍通常意味着统计信息失真",
                bears_on=["stale_statistics"],
                structured_value={**structured_plan, "max_ratio": worst})
        return asdict(d)

    def get_indexes(self, table: str = "orders") -> list[dict]:
        self._enter("get_indexes", {"table": table})
        rows = self.o.get_indexes(table)
        self._evidence("index_existence", "",
                       f"{table} 上的索引: {[r['name'] for r in rows]}",
                       bears_on=["missing_index"], structured_value={
                           "table": table, "indexes": rows,
                           "inventory_collected": True,
                       })
        return rows

    def get_table_stats(self, table: str = "orders") -> dict:
        self._enter("get_table_stats", {"table": table})
        s = self.o.get_table_stats(table)
        raw_ref = s.raw_ref
        structured_stats = asdict(s)
        structured_stats.pop("raw_ref", None)
        self._evidence(
            "stats_freshness", raw_ref,
            f"{table}: live={s.n_live_tup:,} dead={s.n_dead_tup:,} "
            f"dead_ratio={s.dead_ratio} last_analyze={s.last_analyze[:19] or '空'}",
            bears_on=["stale_statistics", "table_bloat"],
            structured_value=structured_stats)
        # 死元组占比要单独记一条。它和"统计新不新鲜"是两回事：图上
        # table_bloat 的必需证据是 dead_tuple_ratio，而这条以前从没有
        # 任何工具产出过 —— 数据取到了却混在 stats_freshness 的观测串里，
        # 于是 table_bloat 这个根因结构上无法被诊断，D1 永远失败。
        # 统计的已知值域还盖不盖得住实际数据。这条是 stale_statistics 唯一
        # 不经过规划器的判别证据 —— row_estimate_deviation 的偏差倍数只有
        # EXPLAIN 给得出来，而只读角色 EXPLAIN 不了写语句，写负载场景下那条
        # 竞争路径结构上关不掉（实测锁竞争场景因此空转 47 轮到预算耗尽）。
        self._evidence(
            "stats_range_drift", raw_ref,
            f"{table}: 落在统计已知值域之外 {s.stats_range_drift_rows:,} 行"
            f"（占 {s.stats_range_drift_pct}%）；"
            f"明细 {s.stats_range_columns[:3]}",
            bears_on=["stale_statistics"],
            structured_value=structured_stats)
        self._evidence(
            "dead_tuple_ratio", raw_ref,
            f"{table}: live={s.n_live_tup:,} dead={s.n_dead_tup:,} "
            f"dead_ratio={s.dead_ratio}",
            bears_on=["table_bloat", "autovacuum_starvation"],
            structured_value=structured_stats)
        backlog = s.n_dead_tup / max(s.autovacuum_trigger, 1)
        self._evidence(
            "autovacuum_health", raw_ref,
            f"{table}: autovacuum_enabled={s.autovacuum_enabled} "
            f"running={s.autovacuum_running} dead={s.n_dead_tup:,} "
            f"trigger={s.autovacuum_trigger:,} backlog={backlog:.2f} "
            f"last_autovacuum={s.last_autovacuum[:19] or '空'}",
            bears_on=["autovacuum_starvation"],
            structured_value=structured_stats)
        return asdict(s)

    def get_physical_bloat(self, table: str = "orders") -> dict:
        self._enter("get_physical_bloat", {"table": table})
        value = self.o.get_physical_bloat(table)
        structured_value = {key: item for key, item in value.items()
                            if key != "raw_ref"}
        availability = value.get("availability", "ERROR")
        status = (EvidenceStatus.OBSERVED if availability == "AVAILABLE" else
                  EvidenceStatus.UNKNOWN if availability == "UNAVAILABLE" else
                  EvidenceStatus.ERROR)
        if availability == "AVAILABLE":
            summary = (
                f"{table}: algorithm={value['algorithm']} "
                f"reclaimable_pct={value['reclaimable_pct']:.2f} "
                f"dead_tuple_pct={value['dead_tuple_percent']:.2f} "
                f"free_pct={value['free_percent']:.2f}"
            )
        else:
            summary = f"{table}: physical bloat measurement {availability}: " \
                      f"{value.get('reason', '')}"
        self._evidence(
            "physical_bloat_ratio", value.get("raw_ref", ""), summary,
            bears_on=["table_bloat"], status=status,
            structured_value=structured_value)
        return value

    def get_top_queries(self, n: int = 5) -> list[dict]:
        self._enter("get_top_queries")
        rows = self.o.get_top_queries(n)
        top = rows[0] if rows else {}
        self._evidence("slow_query_ranking", "",
                       f"最慢查询 mean={top.get('mean_ms')}ms calls={top.get('calls')} "
                       f": {str(top.get('query'))[:60]}",
                       bears_on=["missing_index", "stale_statistics"],
                       structured_value=rows)
        return rows

    def get_active_sessions(self) -> list[dict]:
        self._enter("get_active_sessions")
        rows = self.o.get_active_sessions()
        waits = [r.wait_event for r in rows if r.wait_event]
        self._evidence("session_wait_profile", "",
                       f"{len(rows)} 个异常会话，等待事件={waits or '无'}",
                       bears_on=["lock_contention", "long_idle_transaction",
                                 "deadlock"],
                       structured_value=[asdict(r) for r in rows])
        return [asdict(r) for r in rows]

    def get_blocking_chain(self) -> list[dict]:
        self._enter("get_blocking_chain")
        rows = self.o.get_blocking_chain()
        now = time.time()
        raw_ref = (self.o.raw_ref_for("get_blocking_chain")
                   if hasattr(self.o, "raw_ref_for") else "")
        self._evidence("lock_blocking_chain", raw_ref,
                       f"阻塞链 {len(rows)} 条" + (f": {rows[:2]}" if rows else "（无锁等待）"),
                       bears_on=["lock_contention"],
                       structured_value={"chains": rows},
                       target_kind="PATH",
                       window_start=self.st.started_at, window_end=now,
                       source_epoch=self.st.episode_id)
        return rows

    def get_connection_stats(self) -> dict:
        self._enter("get_connection_stats")
        r = self.o.get_connection_stats()
        raw_ref = r.get("raw_ref", "")
        structured_connection = {key: value for key, value in r.items()
                                 if key != "raw_ref"}
        self._evidence(
            "connection_count", raw_ref,
            f"连接 {r['used']}/{r['max_connections']} ({r['pct']}%), "
            f"逼近上限={r['near_limit']}, "
            f"idle in transaction={r['idle_in_transaction']}, "
            f"按角色={r['by_user']}",
            bears_on=["connection_exhaustion", "long_idle_transaction"],
            structured_value=structured_connection)
        # 挂起事务数要单独记一条。它是分开"真·连接打满"与"长事务堆积"
        # 的**唯一**判别证据（因果图上 power 给到 0.95），而这条以前从没
        # 有任何工具产出过 —— 数字取到了却只混在 connection_count 的观测
        # 串里，于是 long_idle_transaction 结构上无法被诊断。误导性告警
        # 场景的真根因正是它，也就是说那个场景在真实跑批里解不开。
        self._evidence(
            "idle_in_transaction", raw_ref,
            f"连接 {r['used']}/{r['max_connections']}, "
            f"idle in transaction={r['idle_in_transaction']}, "
            f"按状态={r['by_state']}",
            bears_on=["long_idle_transaction", "connection_exhaustion"],
            structured_value=structured_connection)
        return r

    def get_vacuum_horizon(self) -> dict:
        self._enter("get_vacuum_horizon")
        r = self.o.get_vacuum_horizon()
        raw_ref = r.get("raw_ref", "")
        structured_horizon = {key: value for key, value in r.items()
                              if key != "raw_ref"}
        inactive_slots = [x for x in r["slots"] if not x["active"]]
        slot_age = max([
            max(x["xmin_age"], x["catalog_xmin_age"])
            for x in inactive_slots
        ] or [0])
        slot_wal_mb = max([
            x["retained_wal_bytes"] / 1048576 for x in inactive_slots
        ] or [0.0])
        prep_age = max([x["xid_age"] for x in r["prepared_xacts"]] or [0])
        prep_seconds = max([
            x["prepared_age_s"] for x in r["prepared_xacts"]
        ] or [0])
        # 四条证据一次落账：它们是同一个 xmin 视界的四个持有者，
        # 分开记会让 agent 查到第一个就收工，而真凶常常是另一个。
        self._evidence(
            "xid_age", raw_ref,
            f"XID 年龄 db={r['db_xid_age']:,} 最老表={r['oldest_table']}"
            f"({r['oldest_table_xid_age']:,}), "
            f"占 freeze_max_age {r['wraparound_pct']}%, 风险={r['at_risk']}",
            bears_on=["xid_wraparound_risk", "autovacuum_starvation"],
            structured_value=structured_horizon)
        self._evidence(
            "backend_xmin_age", raw_ref,
            f"最老 backend_xmin 年龄={r['oldest_backend_xmin_age']:,} "
            f"(pid={r['oldest_backend_pid']}); xmin 持有者={r['xmin_holders']}",
            bears_on=["long_idle_transaction", "autovacuum_starvation",
                      "xid_wraparound_risk"],
            structured_value=structured_horizon)
        self._evidence(
            "replication_slot_age", raw_ref,
            f"复制槽 {len(r['slots'])} 个, 非活动={len(inactive_slots)}, "
            f"非活动槽最大 horizon 年龄={slot_age:,}, "
            f"非活动槽最大 WAL 滞留={slot_wal_mb:.1f} MB; "
            f"明细={r['slots']}",
            bears_on=["stale_replication_slot"],
            structured_value=structured_horizon)
        self._evidence(
            "prepared_xact_age", raw_ref,
            f"预备事务 {len(r['prepared_xacts'])} 个, "
            f"最大 XID 年龄={prep_age:,}, 最长挂起={prep_seconds:,}s",
            bears_on=["orphaned_prepared_transaction"],
            structured_value=structured_horizon)
        return r

    def get_database_stats(self) -> dict:
        self._enter("get_database_stats")
        r = self.o.get_database_stats()
        errors = r.get("errors", {})
        raw_ref = r.get("raw_ref", "")

        disk = r.get("disk_usage")
        if disk:
            self._evidence(
                "disk_usage", raw_ref,
                f"磁盘使用率={disk['used_pct']:.1f}%, "
                f"可用={disk['free_bytes'] / 1073741824:.1f} GB, "
                f"路径={disk['path']}",
                bears_on=["disk_pressure"], structured_value=disk)
        elif errors.get("disk_usage"):
            self._evidence(
                "disk_usage", raw_ref,
                f"磁盘使用率观测失败: {errors['disk_usage']}",
                bears_on=["disk_pressure"], status=EvidenceStatus.ERROR,
                structured_value={"error": errors["disk_usage"]})

        db_delta, db_status, db_reason = self._cumulative_delta(
            "pg_stat_database", r,
            ("deadlocks", "temp_files", "temp_bytes",
             "xact_commit", "xact_rollback"),
            "db_stats_reset", errors.get("pg_stat_database", ""))
        if db_delta is None:
            db_summary = db_reason
            self._evidence(
                "deadlock_count", raw_ref, db_summary,
                bears_on=["deadlock", "lock_contention"], status=db_status,
                structured_value=db_delta)
            self._evidence(
                "temp_file_volume", raw_ref, db_summary,
                bears_on=["work_mem_spill"], status=db_status,
                structured_value=db_delta)
        else:
            window = db_delta["window_s"]
            self._evidence(
                "deadlock_count", raw_ref,
                f"窗口 {window:.1f}s: 死锁增量={db_delta['deadlocks']}, "
                f"回滚增量={db_delta['xact_rollback']}/"
                f"提交增量={db_delta['xact_commit']}",
                bears_on=["deadlock", "lock_contention"],
                structured_value=db_delta, target_kind="PATH",
                window_start=db_delta["window_start"],
                window_end=db_delta["window_end"],
                source_epoch=db_delta["source_epoch"])
            self._evidence(
                "temp_file_volume", raw_ref,
                f"窗口 {window:.1f}s: 临时文件增量={db_delta['temp_files']} 个, "
                f"外溢增量 {db_delta['temp_bytes'] / 1048576:.1f} MB",
                bears_on=["work_mem_spill"], structured_value=db_delta,
                target_kind="PATH", window_start=db_delta["window_start"],
                window_end=db_delta["window_end"],
                source_epoch=db_delta["source_epoch"])

        ckpt_delta, ckpt_status, ckpt_reason = self._cumulative_delta(
            "checkpoint_stats", r,
            ("ckpt_timed", "ckpt_requested", "ckpt_write_time_ms",
             "ckpt_sync_time_ms"),
            "ckpt_stats_reset", errors.get("checkpoint_stats", ""))
        if ckpt_delta is None:
            self._evidence(
                "checkpoint_stats", raw_ref, ckpt_reason,
                bears_on=["checkpoint_pressure"], status=ckpt_status,
                structured_value=ckpt_delta)
        else:
            timed = ckpt_delta["ckpt_timed"]
            requested = ckpt_delta["ckpt_requested"]
            requested_pct = round(requested / max(timed + requested, 1) * 100, 1)
            self._evidence(
                "checkpoint_stats", raw_ref,
                f"窗口 {ckpt_delta['window_s']:.1f}s: 检查点定时增量={timed} "
                f"请求式增量={requested} (窗口请求式占比 {requested_pct}%), "
                f"写耗时增量={ckpt_delta['ckpt_write_time_ms']:.0f}ms",
                bears_on=["checkpoint_pressure"], structured_value=ckpt_delta,
                target_kind="PATH", window_start=ckpt_delta["window_start"],
                window_end=ckpt_delta["window_end"],
                source_epoch=ckpt_delta["source_epoch"])

        r["window_delta"] = {
            "pg_stat_database": db_delta,
            "checkpoint_stats": ckpt_delta,
        }
        r["evidence_status"] = {
            "pg_stat_database": db_status.value,
            "checkpoint_stats": ckpt_status.value,
        }
        return r

    def simulate_index(self, create_sql: str, test_sql: str,
                       params: dict | None = None) -> dict:
        """反事实验证：不改生产就能预先证伪一个"缺索引"的判断。"""
        self._enter("simulate_index", {"hot_query": test_sql})
        r = self.o.simulate_index(create_sql, test_sql, params)
        desc = (f"hypopg: cost {r['cost_before']:,.0f} -> "
                f"{r['cost_after']:,.0f} (降 {r['cost_reduction_pct']}%), "
                f"优化器会采用={r['would_be_used']}")
        if r.get("trivial_baseline"):
            desc += f"；{r.get('note', '')}"
        self._evidence(
            # create_sql/test_sql bind the observation to this concrete plan
            # but are not present in Observer's raw hypopg digest.  Give the
            # enriched value its own trace rather than reusing a mismatched
            # raw_ref.
            "counterfactual_index", "", desc,
            bears_on=["missing_index"],
            structured_value={
                **{key: value for key, value in r.items()
                   if key != "raw_ref"},
                "create_sql": create_sql,
                "test_sql": test_sql,
            },
            target_kind="INTERVENTION", target_ids=["create_covering_index"])
        return r

    def fetch_raw(self, ref: str) -> dict:
        """按 raw_ref 回取落盘的原文。

        工具层就地萃取只返回结构化摘要，原文按 ref 落盘 —— 这是上下文
        治理的前半截。后半截一直缺着：白名单里有 fetch_raw，Toolbox 却
        没有这个方法，所以模型拿到 raw_ref 也回取不了。失败方向是安全的
        （白名单是超集，多余条目只会调不到），但"按需回取"因此一直只是
        说法，现在补实。
        """
        self._enter("fetch_raw", {"raw_ref": ref})
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
        if v in (Verdict.CONFIRMED, Verdict.REFUTED) and len(note.strip()) < 15:
            # 确认与排除都必须给出依据。原来只查 CONFIRMED，于是"无脑把
            # 竞争假设全标 REFUTED"就能喂饱 ESC 的 D2 —— 那道闸数的是
            # 声明，不是依据。跑批里的受控策略正是这么通过的。
            act = "确认" if v is Verdict.CONFIRMED else "排除"
            raise ValueError(
                f"{act} {name} 必须在 note 里给出证据依据（当前为空或过短）")
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
        got = {e["evidence_type"] for e in self.st.scratchpad
               if evidence_is_observed(e)}
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

        if self.st.claimed_fault_class != fault_class:
            self.st.esc_verdict = ""
        self.st.claimed_fault_class = fault_class
        self.st.claimed_root_cause = root_cause
        self.st.set_verdict(fault_class, Verdict.CONFIRMED,
                            note=root_cause[:200])
        return f"根因已声明: {fault_class}"

    # ── 提交修复提案（不写库）────────────────────────────
    def submit_proposal(self, action_type: str, sql: str, rollback: str,
                        rationale: str = "", predicted_impact: dict | None = None,
                        selected_path_id: str = "", fix_id: str = "",
                        intervention_target: str = ""
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
        if (self.st.schema_version != 2 and action_type != actual):
            self.st.note("agent", "proposal_type_corrected",
                         f"action_type 由 {action_type} 校正为 {actual}")
            action_type = actual
        uses_v2_plan = (self.st.schema_version == 2 and
                        self.st.explanation_graph is not None)
        if uses_v2_plan:
            if not (selected_path_id and fix_id and intervention_target):
                raise ValueError(
                    "v2 proposal must explicitly select path_id, fix_id, and "
                    "intervention_target")
            from agent.explanation_runtime import create_intervention_plan
            plan = create_intervention_plan(
                self.st, action_type=action_type, sql=sql, rollback=rollback,
                rationale=rationale, selected_path_id=selected_path_id,
                fix_id=fix_id, intervention_target=intervention_target)
            root_cause = plan.intervention_target
            resolved_fix_id = plan.fix_id
        else:
            root_cause = self.st.claimed_fault_class or ""
            if not root_cause:
                raise ValueError("必须先声明根因，才能提交修复提案")
            from knowledge.causal_graph import graph as _graph
            matching = [f for f in _graph.fixes_for(root_cause)
                        if f.get("action_type") == action_type]
            if not matching:
                raise ValueError(
                    f"因果图没有为 {root_cause} 声明 {action_type} 修复；"
                    "不能提交与根因无关的动作")
            if len(matching) != 1:
                raise ValueError(
                    f"{root_cause} 有多个 {action_type} 修复节点，无法唯一绑定")
            fix = matching[0]
            if fix.get("execution") == "escalate_only":
                raise ValueError(
                    f"修复 {fix['fix']} 只能升级人工，不能进入执行提案")
            resolved_fix_id = fix["fix"]
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
        }
        if uses_v2_plan:
            # Only model intent is persisted here.  GATE reconstructs all
            # trusted causal fields and evidence references from EpisodeState.
            self.st.proposal.update({
                "selected_path_id": plan.selected_path_id,
                "fix_id": plan.fix_id,
                "intervention_target": plan.intervention_target,
            })
        else:
            self.st.proposal.update({
                "predicted_impact": predicted_impact or {},
                "root_cause": root_cause,
                "fix_id": resolved_fix_id,
                "esc_verdict": self.st.esc_verdict,
                "partial_explanation": self.st.partial_fix_suspected,
                "evidence_refs": list(dict.fromkeys(
                    e["raw_ref"] for e in self.st.scratchpad
                    if e.get("raw_ref") and evidence_is_observed(e)
                    and root_cause in (e.get("bears_on") or [])))[-10:],
            })
        self.st.note("agent", "remediation_proposal",
                     f"{action_type}: {sql[:90]}")
        return f"提案已提交，等待安全门裁决: {action_type}"
