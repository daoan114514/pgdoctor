"""Deterministic predicates over structured tool observations.

Predicates are the only runtime authority for evidence direction.  Human
summaries and REFUTED_BY ``when`` text are documentation, not inputs.  The
legacy adapter at the bottom exists only so pre-v2 traces remain replayable.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from agent.explanation import EvidenceTargetKind, PredicateResult


@dataclass(frozen=True)
class PredicateContext:
    target_kind: str = EvidenceTargetKind.NODE.value
    target_ids: tuple[str, ...] = ()
    collection_status: str = "OBSERVED"
    window_start: float | None = None
    window_end: float | None = None
    source_epoch: str = ""
    expected_source_epoch: str = ""


@dataclass(frozen=True)
class PredicateDecision:
    result: str
    reason: str


Predicate = Callable[[Any, PredicateContext], PredicateDecision]


def _decision(result: PredicateResult, reason: str) -> PredicateDecision:
    return PredicateDecision(result.value, reason)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _supports_if(condition: bool, *, support: str, refute: str,
                 neutral: bool = False) -> PredicateDecision:
    if condition:
        return _decision(PredicateResult.SUPPORTS, support)
    return _decision(PredicateResult.NEUTRAL if neutral else PredicateResult.REFUTES,
                     refute)


def _collected(value: Any, _ctx: PredicateContext) -> PredicateDecision:
    if isinstance(value, dict) and "legacy_collected" in value:
        present = bool(value["legacy_collected"])
    elif isinstance(value, dict) and "inventory_collected" in value:
        present = bool(value["inventory_collected"])
    else:
        present = value is not None and value != [] and value != {}
    return (_decision(PredicateResult.SUPPORTS, "structured observation collected")
            if present else
            _decision(PredicateResult.NEUTRAL, "structured observation is empty"))


def _explain_seq_scan(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    scans = [str(item) for item in value.get("scan_types", [])]
    removed = int(value.get("rows_removed_by_filter", 0) or 0)
    supports = any("Seq Scan" in item for item in scans) and removed > 10_000
    return _supports_if(
        supports,
        support=f"seq scan removed {removed} rows",
        refute="plan does not show a materially filtering sequential scan",
        neutral=True,
    )


def _explain_plan(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    indexes = value.get("indexes_used", []) or []
    if indexes:
        return _decision(PredicateResult.REFUTES,
                         f"current plan already uses indexes {indexes}")
    return _decision(PredicateResult.NEUTRAL,
                     "current plan contains no verified index use")


def _stats_freshness(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    """时间戳只能证明"从没分析过"，不能证明"统计是准的"。

    原先是对称的：有时间戳就 REFUTES 统计过期。那是反的 —— 注入器的顺序
    是先 ANALYZE 固化旧统计、再灌 40 万行倾斜数据，于是 last_analyze 存在
    且很新，而规划器对热查询谓词估 1,185 行、实际 400,000 行（337 倍）。
    项目自己早就记过这个结论（README：判别特征是偏差倍数不是时间戳；实测
    偏差 4200 倍而 last_analyze 看着是新的），但 predicate 里编码的还是
    时间戳判据。

    当时没出事，是因为图上没给它连 refuted_by 边，v1 的 _value_checked 和
    v2 的 _scoped_alternative_refutation 都以那条声明为门，所以这个 REFUTES
    被挡住了。但挡它的是**另一个文件里一条不存在的声明**：谁往 refuted_by
    里加一条看起来很合理的 stats_freshness -> stale_statistics，错误裁决
    立刻上线。判据自身必须是对的，不能靠别处兜底。

    所以改成不对称：没有时间戳 = 从没分析过，那确实支持统计过期；有时间戳
    什么也证明不了，返回 NEUTRAL。
    """
    analyzed = bool(value.get("last_analyze") or
                    value.get("last_analyze_present"))
    if not analyzed:
        return _decision(PredicateResult.SUPPORTS,
                         "table has never been analyzed")
    return _decision(
        PredicateResult.NEUTRAL,
        "an analyze timestamp only shows ANALYZE ran, not that the "
        "statistics still describe the data")


def _row_estimate(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    worst = _number(value.get("max_ratio"), 0.0)
    if not worst:
        for pair in value.get("rows_est_vs_actual", []) or []:
            if len(pair) != 2:
                continue
            estimated, actual = _number(pair[0]), _number(pair[1])
            if estimated > 0 and actual > 0:
                worst = max(worst, estimated / actual, actual / estimated)
    return _supports_if(
        worst >= 10.0,
        support=f"maximum estimate ratio {worst:.3f} is at least 10",
        refute=f"maximum estimate ratio {worst:.3f} is below 10",
    )


def _stats_range_drift(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    """统计的已知值域是否还盖得住实际数据。

    阈值 0.5% 是量出来的，不是拍的（orders 表 1200 万行，四个有直方图的列）：
        健康态 golden          840 行超范围 = 0.0070%
        统计过期注入态     401,102 行超范围 = 3.2346%
    462 倍分离，0.5% 落在中间，下有 71 倍余量、上有 6.5 倍余量。

    直方图两端本来就有采样误差，所以判据不能用"有没有超范围"，只能用占比。
    """
    pct = _number(value.get("stats_range_drift_pct"), 0.0)
    rows = _number(value.get("stats_range_drift_rows"), 0.0)
    if pct >= 0.5:
        # 下界已经越过阈值，真值必然也越过 —— 这个方向即使测不全也成立。
        return _decision(
            PredicateResult.SUPPORTS,
            f"{rows:.0f} rows ({pct:.4f}%) fall outside the value range the "
            f"statistics know about, at or above the 0.5% bar")
    missed = value.get("stats_range_incomplete") or []
    if missed:
        # 有列测不到时这个占比只是下界，拿它去 REFUTE 就是用一个偏低的数
        # 否定一个可能为真的根因 —— 正是本项目要防的静默失败。
        return _decision(
            PredicateResult.NOT_APPLICABLE,
            f"range drift is only a lower bound: {len(missed)} column(s) could "
            f"not be measured ({[item.get('column') for item in missed][:3]})")
    return _decision(
        PredicateResult.REFUTES,
        f"only {rows:.0f} rows ({pct:.4f}%) fall outside the known value "
        f"range, below the 0.5% bar")


def _seq_scan_volume(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    """窗口内热表上有没有大规模顺序扫描。

    这是 missing_index 唯一一条不经过规划器的可反证证据。它记的是实际执行
    了什么，而不是规划器打算怎么做 —— counterfactual_index 与 explain_plan
    都是规划器输出，统计过期时拿它们判缺索引是循环论证。

    实测（orders 1200 万行，同样跑 20 次热查询）：
        索引在        seq_scan=0                              -> 反证
        索引丢        seq_scan=20, 每次读满 12,000,000 行     -> 支持
        统计过期      seq_scan=0（规划器仍走索引）            -> 反证
    判据取"每次顺序扫描平均读了全表的多大比例"，而不是原始行数 —— 原始值
    随窗口长短变化，比例不随。

    阈值 0.1 而不是直觉上的 0.5：**并行扫描会把这个比例除以 worker 数**。
    Parallel Seq Scan 下每个 worker 各自给 seq_scan 计数、各读约 1/N 的表，
    所以一次"全表扫"量出来的每次平均行数是全表除以 N。实测缺索引态
    (loops=3) 连续三次采样都是 45.0% / 45.5% / 45.2%，而我最初按单进程全表扫
    定的 0.5 正好卡在上面 —— 于是它 REFUTES 了正确的根因 missing_index，
    诊断跑偏成 work_mem_spill。0.1 对故障态留 4.5 倍余量，对健康态更安全：
    健康态根本没有顺序扫描，走的是上面 seq<=0 那条分支。

    窗口内完全没有扫描活动（顺序和索引都是 0）时返回 NEUTRAL：那说明这段
    时间根本没有负载，不能据此否定任何根因。
    """
    seq = _number(value.get("seq_scan"), 0.0)
    idx = _number(value.get("idx_scan"), 0.0)
    rows = _number(value.get("seq_tup_read"), 0.0)
    total = _number(value.get("reltuples"), 0.0)
    if seq <= 0 and idx <= 0:
        return _decision(PredicateResult.NEUTRAL,
                         "no scan activity of either kind in the window")
    if seq <= 0:
        return _decision(
            PredicateResult.REFUTES,
            f"the window has {idx:.0f} index scans and no sequential scan")
    per_scan = rows / seq
    share = per_scan / total if total > 0 else 0.0
    return _supports_if(
        share >= 0.1,
        support=(f"each of {seq:.0f} sequential scans read {per_scan:,.0f} rows "
                 f"on average, {share:.1%} of the table"),
        refute=(f"sequential scans read only {per_scan:,.0f} rows on average, "
                f"{share:.1%} of the table, below the 10% bar"),
    )


def _is_blocking_record(row: Any) -> bool:
    """这一行是不是锁竞争的证据。

    观测器（observe.get_blocking_chain）除了"正在等锁"的行，还附上 idle in transaction
    会话作为稳定信号（等待者被 statement_timeout 掐掉后链会瞬间变空）。但只有持有表级锁的
    空闲事务才可能挡住别人；持锁 0 个表的空闲事务（事务里只跑过 SELECT 1）什么也挡不住。
    原来一律计数：misleading_idle_txn 里 87 个"持锁 0 个对象"的空闲事务被数成 87 条阻塞
    记录，lock_contention 被判 SUPPORTS、根因选错（2026-09-23）。这是一条 provenance 规则
    没表达出来的污染边 long_idle_transaction ⇝ lock_contention —— 长事务为真时，这条证据
    关于锁竞争的裁决出错。修在证据上，不靠加因果边去圆（CLAUDE.md 规则 2）。
    """
    if not isinstance(row, dict):
        return bool(row)
    if row.get("evidence") == "idle_in_transaction_holding_locks":
        return int(row.get("blocking_impact", 0) or 0) > 0
    return True


def _lock_chain(value: Any, _ctx: PredicateContext) -> PredicateDecision:
    chains = value.get("chains", []) if isinstance(value, dict) else value
    chains = chains or []
    blocking = [row for row in chains if _is_blocking_record(row)]
    ignored = len(chains) - len(blocking)
    return _supports_if(
        bool(blocking),
        support=f"{len(blocking)} blocking records observed"
                + (f" ({ignored} idle transactions holding no table lock ignored)" if ignored else ""),
        refute=("blocking chain is empty in the incident window" if not chains else
                f"no session is blocked: {ignored} idle transactions hold no table lock"))


def _session_wait(value: Any, _ctx: PredicateContext) -> PredicateDecision:
    rows = value if isinstance(value, list) else value.get("sessions", [])
    waits = [str(row.get("wait_event") or row.get("wait") or "")
             for row in (rows or []) if isinstance(row, dict)]
    # PostgreSQL reports heavyweight lock waits as ``Lock:<event>`` and
    # internal lightweight-latch contention as ``LWLock:<event>``.  The
    # latter is common under a scan-heavy workload and is not evidence of a
    # blocking transaction chain.
    has_lock = any(
        wait.lower() == "lock" or wait.lower().startswith("lock:")
        for wait in waits)
    return _supports_if(has_lock, support="lock wait observed",
                        refute="no lock wait observed")


def _counterfactual_index(value: dict,
                          ctx: PredicateContext) -> PredicateDecision:
    if (ctx.target_kind != EvidenceTargetKind.INTERVENTION.value or
            "create_covering_index" not in ctx.target_ids):
        return _decision(
            PredicateResult.NOT_APPLICABLE,
            "counterfactual result is scoped only to create_covering_index",
        )
    if value.get("trivial_baseline"):
        return _decision(PredicateResult.NEUTRAL,
                         "baseline cost is too small for a useful counterfactual")
    if bool(value.get("would_be_used")):
        return _decision(PredicateResult.SUPPORTS,
                         "optimizer would use this concrete index definition")
    return _decision(PredicateResult.REFUTES,
                     "optimizer would not use this concrete index definition")


def _dead_tuple_ratio(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    return _decision(
        PredicateResult.NEUTRAL,
        "dead tuple ratio does not establish physical table bloat",
    )


def _physical_bloat(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    if value.get("availability") != "AVAILABLE":
        return _decision(PredicateResult.NOT_APPLICABLE,
                         f"physical bloat measurement {value.get('availability', 'missing')}")
    if value.get("algorithm") != "pgstattuple_approx_reclaimable_pct_v1":
        return _decision(PredicateResult.NOT_APPLICABLE,
                         "unknown physical bloat algorithm")
    ratio = _number(value.get("reclaimable_pct"), -1.0)
    if ratio < 0:
        return _decision(PredicateResult.NOT_APPLICABLE,
                         "reclaimable_pct is missing")
    if ratio >= 20.0:
        return _decision(PredicateResult.SUPPORTS,
                         f"physical reclaimable ratio {ratio:.2f}% is at least 20%")
    if ratio <= 10.0:
        return _decision(PredicateResult.REFUTES,
                         f"physical reclaimable ratio {ratio:.2f}% is at most 10%")
    return _decision(PredicateResult.NEUTRAL,
                     f"physical reclaimable ratio {ratio:.2f}% is inconclusive")


def _autovacuum_health(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    enabled = value.get("autovacuum_enabled")
    running = value.get("autovacuum_running", value.get("running"))
    trigger = _number(value.get("autovacuum_trigger"), 0.0)
    dead = _number(value.get("n_dead_tup"), 0.0)
    backlog = _number(value.get("backlog_ratio"),
                      dead / max(trigger, 1.0) if trigger else 0.0)
    if enabled is None or running is None:
        return _decision(PredicateResult.NOT_APPLICABLE,
                         "autovacuum state fields are incomplete")
    starved = not bool(enabled) or (not bool(running) and backlog >= 2.0)
    return _supports_if(
        starved, support=f"autovacuum backlog ratio is {backlog:.3f}",
        refute="autovacuum is enabled without a qualifying backlog")


def _idle_in_transaction(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    count = int(value.get("idle_in_transaction", 0) or 0)
    if count >= 3:
        return _decision(PredicateResult.SUPPORTS,
                         f"{count} idle-in-transaction sessions observed")
    if count == 0:
        return _decision(PredicateResult.REFUTES,
                         "no idle-in-transaction sessions observed")
    return _decision(PredicateResult.NEUTRAL,
                     f"only {count} idle-in-transaction sessions observed")


# 连接"逼近上限"的阈值。observe.get_connection_stats 的 near_limit 与 connection_residual 共用。
CONNECTION_NEAR_LIMIT_RATIO = 0.85


def _connection_residual(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    """连接打满是不是由长时间 idle in transaction 的会话解释掉了 —— 只判"connection_exhaustion 是根"。

    misleading_idle_txn 的真路径是 long_idle_transaction -> connection_exhaustion ->
    throughput_down，connection_exhaustion 在这里是中间机制。以它为根的路径与真路径共享节点和
    边，NODE / PATH 范围的反证都会连带反证真路径上的中间节点，所以这条边在图上是 scope: ROOT，
    只关以它为根的路径（2026-09-24 缺陷报告 P1-2）。

    扣掉持续 >= 阈值秒数的 idle in transaction 会话后，连接数若已低于逼近上限的阈值，打满就由
    上游的长事务解释，connection_exhaustion 不是根 -> REFUTES。只数"持续"的：事务型应用在语句
    之间会短暂处于 idle in transaction，把它们也扣掉会反证真正的连接打满；阈值偏高只会少反证
    （规则 1 的安全方向）。没有逼近上限时这条问题不成立，交给 connection_count_v2 -> NEUTRAL。

    活库标定（.dev/fix_calibration_live.py connection|misleading，2026-09-24）：
        connection_exhaustion 故障态  97/100，idle in transaction 0（持续 >=30s 0）  扣后 97% -> SUPPORTS
        misleading_idle_txn 故障态    97/100，idle in transaction 87（持续 >=30s 87） 扣后 10% -> REFUTES
        两者修复后                    33-36/100                                      -> NEUTRAL
    0.85 两侧余量都很大；诊断时点上堆积的会话全都已满 30 秒，门槛没有偏高到漏数。
    """
    if ("idle_in_transaction_long" not in value or "used" not in value or
            not value.get("max_connections")):
        return _decision(PredicateResult.NEUTRAL,
                         "residual connection inputs are missing")
    max_conn = max(int(value["max_connections"]), 1)
    used = int(value["used"] or 0)
    long_idle = int(value["idle_in_transaction_long"] or 0)
    if used < max_conn * CONNECTION_NEAR_LIMIT_RATIO:
        return _decision(PredicateResult.NEUTRAL,
                         f"usage {used}/{max_conn} is not near the limit")
    ratio = (used - long_idle) / max_conn
    if ratio < CONNECTION_NEAR_LIMIT_RATIO:
        return _decision(
            PredicateResult.REFUTES,
            f"{long_idle} long idle-in-transaction sessions account for the exhaustion: "
            f"without them usage is {ratio:.0%} of max_connections")
    return _decision(
        PredicateResult.SUPPORTS,
        f"usage stays at {ratio:.0%} of max_connections even without "
        f"{long_idle} long idle-in-transaction sessions")


def _connection_count(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    near = bool(value.get("near_limit"))
    return _supports_if(near, support="connection usage is near the configured limit",
                        refute="connection usage is below the near-limit threshold")


def _xid_age(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    ratio = _number(value.get("wraparound_pct"), 0.0)
    return _supports_if(ratio >= 50.0,
                        support=f"XID age is {ratio:.2f}% of freeze max age",
                        refute=f"XID age is only {ratio:.2f}% of freeze max age")


def _backend_xmin(value: dict, ctx: PredicateContext) -> PredicateDecision:
    age = int(value.get("oldest_backend_xmin_age", 0) or 0)
    holders = set(value.get("xmin_holders", []) or [])
    target = set(ctx.target_ids)
    if "long_idle_transaction" in target:
        hit = age > 1_000_000 and "long_transaction" in holders
    else:
        hit = age > 1_000_000 and bool(holders)
    return _supports_if(hit, support=f"backend xmin age {age} has holders {sorted(holders)}",
                        refute="no qualifying backend xmin holder")


def _replication_slot(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    slots = value.get("slots", []) or []
    stale = []
    for slot in slots:
        if slot.get("active"):
            continue
        age = max(int(slot.get("xmin_age", 0) or 0),
                  int(slot.get("catalog_xmin_age", 0) or 0))
        retained = int(slot.get("retained_wal_bytes", 0) or 0)
        if age > 1_000_000 or retained >= 1024 * 1024 * 1024:
            stale.append(slot.get("slot_name", "?"))
    return _supports_if(bool(stale), support=f"stale inactive slots {stale}",
                        refute="no stale inactive replication slot")


def _prepared_xact(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    prepared = value.get("prepared_xacts", []) or []
    stale = [item for item in prepared
             if int(item.get("xid_age", 0) or 0) > 1_000_000 or
             int(item.get("prepared_age_s", 0) or 0) >= 3600]
    return _supports_if(bool(stale), support=f"{len(stale)} stale prepared transactions",
                        refute="no stale prepared transaction")


def _deadlock_count(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    count = int(value.get("deadlocks", 0) or 0)
    return _supports_if(count > 0, support=f"{count} deadlocks in incident window",
                        refute="deadlock delta is zero in incident window")


def _temp_file_volume(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    size = int(value.get("temp_bytes", 0) or 0)
    own = int(value.get("own_temp_bytes", 0) or 0)
    if size <= 0 and own > 0:
        # 净值是原始增量减去自家 EXPLAIN ANALYZE 外溢的**上界**，只可能偏低：
        # 为正可以支持，为 0 却可能是把负载的外溢一并扣掉了（CLAUDE.md 规则 1、6）。
        return _decision(
            PredicateResult.NEUTRAL,
            f"the agent's own EXPLAIN ANALYZE spilled up to {own} bytes in this window "
            f"and accounts for all {int(value.get('temp_bytes_raw', 0) or 0)} raw bytes; "
            "a workload spill cannot be ruled out")
    return _supports_if(size > 0, support=f"{size} temporary bytes in incident window",
                        refute="temporary byte delta is zero in incident window")


def _checkpoint_stats(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    timed = int(value.get("ckpt_timed", 0) or 0)
    requested = int(value.get("ckpt_requested", 0) or 0)
    ratio = requested / max(timed + requested, 1)
    return _supports_if(ratio >= 0.5,
                        support=f"requested checkpoint ratio is {ratio:.3f}",
                        refute=f"requested checkpoint ratio is only {ratio:.3f}")


def _disk_usage(value: dict, _ctx: PredicateContext) -> PredicateDecision:
    ratio = _number(value.get("used_pct"), 0.0)
    return _supports_if(ratio >= 85.0, support=f"filesystem usage is {ratio:.2f}%",
                        refute=f"filesystem usage is only {ratio:.2f}%")


_PREDICATES: dict[str, Predicate] = {
    "explain_seq_scan_v2": _explain_seq_scan,
    "explain_plan_v2": _explain_plan,
    "index_existence_v2": _collected,
    "stats_freshness_v2": _stats_freshness,
    "row_estimate_deviation_v2": _row_estimate,
    "stats_range_drift_v2": _stats_range_drift,
    "seq_scan_volume_v2": _seq_scan_volume,
    "lock_blocking_chain_v2": _lock_chain,
    "session_wait_profile_v2": _session_wait,
    "slow_query_ranking_v2": _collected,
    "counterfactual_index_v2": _counterfactual_index,
    "dead_tuple_ratio_v2": _dead_tuple_ratio,
    "physical_bloat_ratio_v2": _physical_bloat,
    "autovacuum_health_v2": _autovacuum_health,
    "idle_in_transaction_v2": _idle_in_transaction,
    "connection_count_v2": _connection_count,
    "xid_age_v2": _xid_age,
    "backend_xmin_age_v2": _backend_xmin,
    "replication_slot_age_v2": _replication_slot,
    "prepared_xact_age_v2": _prepared_xact,
    "deadlock_count_v2": _deadlock_count,
    "temp_file_volume_v2": _temp_file_volume,
    "connection_residual_v2": _connection_residual,
    "checkpoint_stats_v2": _checkpoint_stats,
    "disk_usage_v2": _disk_usage,
}

_WINDOW_PREDICATES = frozenset({
    "lock_blocking_chain_v2", "deadlock_count_v2", "seq_scan_volume_v2",
    "temp_file_volume_v2", "checkpoint_stats_v2",
})


# "采集即 SUPPORTS"的存在性门：只证明数据到手（ESC 的必需证据、路径的 required_supported
# 读它），不证明因果方向。方向裁决里把它们当 NEUTRAL —— 否则它们的 SUPPORTS 与真正的反证
# 混在一起，_decision_status 永远判 INCONCLUSIVE，missing_index 在任何场景都无法被反证
# （2026-09-23 跑批，ESC 因此空转到预算耗尽）。
GATE_PREDICATES = frozenset({"index_existence_v2", "slow_query_ranking_v2"})


def is_gate_predicate(predicate_id: str) -> bool:
    return str(predicate_id or "") in GATE_PREDICATES


# 累计计数器的窗口判据（证据 provenance 为 cumulative_counter）。它们量的是"窗口内计数器
# 涨了多少"，窗口越短越可能漏掉 —— 零增量只是下界。graph_lint 核对这个集合与图上
# provenance 一致。
CUMULATIVE_WINDOW_PREDICATES = frozenset({
    "deadlock_count_v2", "seq_scan_volume_v2", "temp_file_volume_v2", "checkpoint_stats_v2",
    # 存在性门，从不 REFUTES，下面的规则对它不起作用；列在这里是为了与图上 provenance 对齐
    "slow_query_ranking_v2",
})
# 否定裁决要求的最短窗口。告警本身是在 30 秒滚动窗口（sandbox.metrics.WINDOW_S）上判出来的，
# 声称"事故期间没有 X"的证据至少要看这么长；harness_lint 核对两者一致。
# 2026-09-24：MONITOR 阶段读一次计数器、几秒后 INVESTIGATE 再读一次，窗口只有 1.4-9.5 秒，
# 其间死锁数与临时文件量为 0 就判 deadlock / work_mem_spill 不成立（4 个 episode 都有）——
# 规则 1 禁止的"拿偏低的部分结果做否定裁决"。短窗口的 SUPPORTS 仍然有效（下界已越阈值）。
MIN_REFUTE_WINDOW_S = 30.0


def registered_predicates() -> frozenset[str]:
    return frozenset(_PREDICATES)


def evaluate(predicate_id: str, value: Any, *, context: PredicateContext,
             window_required: bool | None = None) -> PredicateDecision:
    """Evaluate one predicate without consulting summaries or model text."""
    if context.collection_status != "OBSERVED":
        return _decision(PredicateResult.NOT_APPLICABLE,
                         f"collection status is {context.collection_status}")
    predicate = _PREDICATES.get(predicate_id)
    if predicate is None:
        return _decision(PredicateResult.NOT_APPLICABLE,
                         f"unknown predicate {predicate_id}")
    needs_window = (predicate_id in _WINDOW_PREDICATES
                    if window_required is None else window_required)
    if needs_window:
        if (context.window_start is None or context.window_end is None or
                context.window_end < context.window_start or
                not context.source_epoch):
            return _decision(PredicateResult.NOT_APPLICABLE,
                             "incident window or source epoch is missing")
        if (context.expected_source_epoch and
                context.source_epoch != context.expected_source_epoch):
            return _decision(PredicateResult.NOT_APPLICABLE,
                             "source epoch does not match the incident window")
    if not isinstance(value, (dict, list)):
        return _decision(PredicateResult.NOT_APPLICABLE,
                         "predicate input is not a structured value")
    if isinstance(value, dict):
        value_epoch = str(value.get("source_epoch", ""))
        if value_epoch and context.source_epoch and value_epoch != context.source_epoch:
            return _decision(PredicateResult.NOT_APPLICABLE,
                             "structured value and binding source epochs differ")
    decision = predicate(value, context)
    if (decision.result == PredicateResult.REFUTES.value and
            predicate_id in CUMULATIVE_WINDOW_PREDICATES and
            context.window_start is not None and context.window_end is not None and
            context.window_end - context.window_start < MIN_REFUTE_WINDOW_S):
        span = context.window_end - context.window_start
        return _decision(
            PredicateResult.NEUTRAL,
            f"window is only {span:.1f}s (< {MIN_REFUTE_WINDOW_S:.0f}s); a zero or low "
            f"delta over it is a lower bound and cannot refute: {decision.reason}")
    return decision


def legacy_structured_value(predicate_id: str, observation: str) -> Any:
    """Parse historical summaries for v1 replay only.

    New tool calls persist ``structured_value`` and never use this adapter.
    """
    text = observation or ""
    low = text.lower()
    if predicate_id == "explain_seq_scan_v2":
        match = re.search(r"rows removed by filter=([\d,]+)", low)
        return {"scan_types": ["Seq Scan"] if "seq scan" in low else [],
                "rows_removed_by_filter": int(match.group(1).replace(",", ""))
                if match else 0}
    if predicate_id == "explain_plan_v2":
        no_index = "\u7528\u5230\u7d22\u5f15=\u65e0" in text
        return {"indexes_used": [] if no_index else ["legacy_index"]}
    if predicate_id == "index_existence_v2":
        return {"legacy_collected": bool("\u7d22\u5f15" in text or "index" in low)}
    if predicate_id == "stats_freshness_v2":
        return {"last_analyze_present": bool(re.search(
            r"last_analyze=\d{4}-\d{2}-\d{2}", low))}
    if predicate_id == "stats_range_drift_v2":
        match = re.search(r"占 ([\d.]+)%", text)
        return {"stats_range_drift_pct": _number(match.group(1)) if match else 0.0}
    if predicate_id == "row_estimate_deviation_v2":
        match = re.search(r"\u6700\u5927\u504f\u5dee ([\d.]+) \u500d", text)
        return {"max_ratio": _number(match.group(1)) if match else 0.0}
    if predicate_id == "lock_blocking_chain_v2":
        empty = "0 \u6761" in text or "\u65e0\u9501\u7b49\u5f85" in text
        return {"chains": [] if empty else [{"legacy": True}]}
    if predicate_id == "session_wait_profile_v2":
        return [{"wait_event": "Lock" if "lock" in low else ""}]
    if predicate_id in {"slow_query_ranking_v2"}:
        return {"legacy_collected": bool(text)}
    if predicate_id == "counterfactual_index_v2":
        return {"would_be_used": ("\u4f1a\u91c7\u7528=true" in low or
                                  "would_be_used': true" in low or
                                  "\u91c7\u7528=True" in text),
                "trivial_baseline": ("\u6210\u672c\u4ec5" in text or
                                     "\u4e0d\u8db3\u4ee5\u652f\u6301" in text)}
    if predicate_id == "dead_tuple_ratio_v2":
        match = re.search(r"dead_ratio=([\d.]+)", low)
        return {"dead_ratio": _number(match.group(1)) if match else 0.0}
    if predicate_id == "physical_bloat_ratio_v2":
        match = re.search(r"reclaimable_pct=([\d.]+)", low)
        return {"availability": "AVAILABLE" if match else "UNAVAILABLE",
                "algorithm": "pgstattuple_approx_reclaimable_pct_v1",
                "reclaimable_pct": _number(match.group(1)) if match else 0.0}
    if predicate_id == "autovacuum_health_v2":
        enabled = re.search(r"autovacuum_enabled=(true|false)", low)
        running = re.search(r"running=(true|false)", low)
        backlog = re.search(r"backlog=([\d.]+)", low)
        return {"autovacuum_enabled": enabled.group(1) == "true" if enabled else None,
                "autovacuum_running": running.group(1) == "true" if running else None,
                "backlog_ratio": _number(backlog.group(1)) if backlog else 0.0}
    if predicate_id == "idle_in_transaction_v2":
        match = re.search(r"idle in transaction=(\d+)", low)
        return {"idle_in_transaction": int(match.group(1)) if match else 0}
    if predicate_id == "connection_count_v2":
        return {"near_limit": "\u903c\u8fd1\u4e0a\u9650=True" in text}
    if predicate_id == "xid_age_v2":
        match = re.search(r"\u5360 freeze_max_age ([\d.]+)%", text)
        return {"wraparound_pct": _number(match.group(1)) if match else 0.0}
    if predicate_id == "backend_xmin_age_v2":
        match = re.search(r"\u6700\u8001 backend_xmin \u5e74\u9f84=([\d,]+)", text)
        holders = ["long_transaction"] if "long_transaction" in low else []
        return {"oldest_backend_xmin_age": int(match.group(1).replace(",", ""))
                if match else 0, "xmin_holders": holders}
    if predicate_id == "replication_slot_age_v2":
        match = re.search(
            r"\u590d\u5236\u69fd (\d+) \u4e2a, \u975e\u6d3b\u52a8=(\d+), .*?\u5e74\u9f84=([\d,]+), .*?\u6ede\u7559=([\d.]+) mb",
            low)
        if not match:
            return {"slots": []}
        count, inactive = int(match.group(1)), int(match.group(2))
        age = int(match.group(3).replace(",", ""))
        retained = int(_number(match.group(4)) * 1024 * 1024)
        slots = [{"slot_name": f"legacy_{index}", "active": index >= inactive,
                  "xmin_age": age if index < inactive else 0,
                  "catalog_xmin_age": 0,
                  "retained_wal_bytes": retained if index < inactive else 0}
                 for index in range(count)]
        return {"slots": slots}
    if predicate_id == "prepared_xact_age_v2":
        match = re.search(
            r"\u9884\u5907\u4e8b\u52a1 (\d+) \u4e2a, \u6700\u5927 xid \u5e74\u9f84=([\d,]+), \u6700\u957f\u6302\u8d77=([\d,]+)s", low)
        if not match:
            return {"prepared_xacts": []}
        count = int(match.group(1))
        return {"prepared_xacts": [
            {"xid_age": int(match.group(2).replace(",", "")),
             "prepared_age_s": int(match.group(3).replace(",", ""))}
            for _ in range(count)]}
    if predicate_id == "deadlock_count_v2":
        match = re.search(r"\u6b7b\u9501\u589e\u91cf=(\d+)", text)
        return {"deadlocks": int(match.group(1)) if match else 0}
    if predicate_id == "temp_file_volume_v2":
        match = re.search(r"\u5916\u6ea2\u589e\u91cf ([\d.]+) mb", low)
        return {"temp_bytes": int(_number(match.group(1)) * 1048576) if match else 0}
    if predicate_id == "checkpoint_stats_v2":
        timed = re.search(r"\u68c0\u67e5\u70b9\u5b9a\u65f6\u589e\u91cf=(\d+)", text)
        requested = re.search(r"\u8bf7\u6c42\u5f0f\u589e\u91cf=(\d+)", text)
        ratio = re.search(r"\u7a97\u53e3\u8bf7\u6c42\u5f0f\u5360\u6bd4 ([\d.]+)%", text)
        if timed and requested:
            return {"ckpt_timed": int(timed.group(1)),
                    "ckpt_requested": int(requested.group(1))}
        pct = _number(ratio.group(1)) if ratio else 0.0
        return {"ckpt_timed": int(round(100 - pct)),
                "ckpt_requested": int(round(pct))}
    if predicate_id == "disk_usage_v2":
        match = re.search(r"\u78c1\u76d8\u4f7f\u7528\u7387=([\d.]+)%", text)
        return {"used_pct": _number(match.group(1)) if match else 0.0}
    return {"legacy_collected": bool(text)}
