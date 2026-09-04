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
    fresh = bool(value.get("last_analyze") or value.get("last_analyze_present"))
    return (_decision(PredicateResult.REFUTES, "analyze timestamp is present")
            if fresh else
            _decision(PredicateResult.SUPPORTS, "analyze timestamp is absent"))


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


def _lock_chain(value: Any, _ctx: PredicateContext) -> PredicateDecision:
    chains = value.get("chains", []) if isinstance(value, dict) else value
    chains = chains or []
    return _supports_if(
        bool(chains), support=f"{len(chains)} blocking records observed",
        refute="blocking chain is empty in the incident window")


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
    "checkpoint_stats_v2": _checkpoint_stats,
    "disk_usage_v2": _disk_usage,
}

_WINDOW_PREDICATES = frozenset({
    "lock_blocking_chain_v2", "deadlock_count_v2",
    "temp_file_volume_v2", "checkpoint_stats_v2",
})


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
    return predicate(value, context)


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
