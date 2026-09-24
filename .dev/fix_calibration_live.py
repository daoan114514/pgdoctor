#!/usr/bin/env python3
"""活库标定修复能否真的修好（CLAUDE.md 规则 5：阈值与可修性必须用真实观测验证）。

用法：python3 .dev/fix_calibration_live.py connection|stale
  connection  注入 connection_exhaustion，记健康/故障态连接使用率；按 agent 会走的路径取
              idle 客户端 pid（observer.get_active_sessions(include_idle=True)，排除系统/
              诊断连接），gate._recheck_sessions 复核后以 rw 执行多 pid 终止，隔 30/60s 各测一次。
  stale       注入 stale_statistics，记故障态 p50；以 rw 执行 ANALYZE，隔 30/60/120/180s 测 p50，
              并在修复前后各取一次热查询的执行计划，看是否换了计划。
只动沙箱库；结束时 env.close() 清理注入与负载。不经过 agent，也不写 undo journal。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from sandbox import db, metrics  # noqa: E402
from sandbox.env import DBAScenarioEnv  # noqa: E402


def kpi_line(label: str, kpi) -> str:
    d = kpi.as_dict()
    return (f"{label:<18} p50={d['p50_ms']:>8.1f}ms p99={d['p99_ms']:>8.1f}ms qps={d['qps']:>7.1f} "
            f"errors={d['errors']:>4} cpu={d['cpu_pct']:>6.1f}% conn={d['connection_usage_ratio']:.3f}")


def connection() -> bool:
    from safety import gate, shield
    from safety.gate import RemediationProposal
    path = ROOT / "sandbox" / "scenarios" / "connection_exhaustion_eval_v1.yaml"
    with DBAScenarioEnv(str(path), warmup_s=15.0, degrade_timeout_s=90.0, quiet=True) as env:
        obs = env.reset()
        print("fired:", obs.fired)
        print(kpi_line("healthy", env.healthy_kpi))
        fault, _ = env.verify(settle_s=0)
        print(kpi_line("fault", fault))
        rows = env.observe().get_active_sessions(include_idle=True)
        pids = [r.pid for r in rows if r.state == "idle" and not r.is_system_or_diagnostic
                and not r.is_current_diagnostic_connection][:shield.MAX_SESSION_TARGETS]
        sql = "SELECT " + ", ".join(f"pg_terminate_backend({pid})" for pid in pids)
        print(f"idle client pids observed: {len(pids)}")
        p = RemediationProposal(action_type="session_control", sql=sql, rollback="IRREVERSIBLE",
                                root_cause="connection_exhaustion", fix_id="terminate_idle_backend")
        print("assess:", gate.assess(p).tier, "| recheck:", gate._recheck_sessions(p))
        db.execute(sql, role="rw")
        ok = False
        for wait in (30, 30):
            time.sleep(wait)
            kpi, _ = env.verify(settle_s=0)
            passed = metrics.eval_expr(env.spec["success"]["outcome"], kpi, baseline=env.healthy_kpi)
            print(kpi_line(f"after +{wait}s", kpi), "| success:", passed)
            ok = passed
        return ok


def misleading() -> bool:
    """misleading_idle_txn：终止已观测的 idle in transaction 客户端会话（最多 64 个）。"""
    from safety import gate, shield
    from safety.gate import RemediationProposal
    path = ROOT / "sandbox" / "scenarios" / "misleading_idle_txn_eval_v1.yaml"
    with DBAScenarioEnv(str(path), warmup_s=15.0, degrade_timeout_s=90.0, quiet=True) as env:
        obs = env.reset()
        print("fired:", obs.fired)
        print(kpi_line("healthy", env.healthy_kpi))
        fault, _ = env.verify(settle_s=0)
        print(kpi_line("fault", fault))
        rows = env.observe().get_active_sessions(include_idle=True)
        pids = [r.pid for r in rows if r.state == "idle in transaction"
                and not r.is_system_or_diagnostic and not r.is_current_diagnostic_connection
                ][:shield.MAX_SESSION_TARGETS]
        print(f"idle-in-transaction client pids observed: {len(pids)}")
        sql = "SELECT " + ", ".join(f"pg_terminate_backend({pid})" for pid in pids)
        p = RemediationProposal(action_type="session_control", sql=sql, rollback="IRREVERSIBLE",
                                root_cause="long_idle_transaction", fix_id="terminate_idle_transaction")
        print("assess:", gate.assess(p).tier, "| recheck:", gate._recheck_sessions(p))
        db.execute(sql, role="rw")
        ok = False
        for wait in (30, 30):
            time.sleep(wait)
            kpi, _ = env.verify(settle_s=0)
            ok = metrics.eval_expr(env.spec["success"]["outcome"], kpi, baseline=env.healthy_kpi,
                                   fault=getattr(env, "fault_kpi", None))
            print(kpi_line(f"after +{wait}s", kpi), "| success:", ok)
        return ok


def stale(fix: bool = True) -> bool:
    """fix=False 是对照组：不做任何修复，看故障态 p50 自己会不会降下来（判据的假阳性风险）。"""
    path = ROOT / "sandbox" / "scenarios" / "stale_statistics_eval_v1.yaml"
    with DBAScenarioEnv(str(path), warmup_s=15.0, degrade_timeout_s=90.0, quiet=True) as env:
        obs = env.reset()
        print("fired:", obs.fired)
        print(kpi_line("healthy", env.healthy_kpi))
        alert_p50 = float(obs.current_kpi["p50_ms"])
        print(f"{'at alert':<18} p50={alert_p50:>8.1f}ms")
        fault, _ = env.verify(settle_s=0)
        print(kpi_line("fault", fault), f"| ratio to alert {fault.p50_ms / alert_p50:.2f}")
        if not fix:
            for wait in (30, 30, 60, 60):
                time.sleep(wait)
                kpi, _ = env.verify(settle_s=0)
                print(kpi_line(f"no-fix +{wait}s", kpi), f"| ratio to alert {kpi.p50_ms / alert_p50:.2f}")
            return False
        hot = " ".join(env.spec["workload"]["hot_query"].split())
        before = env.observe().explain_query(hot)
        print("plan before:", before.scan_types[:3], before.total_time_ms, "ms est/act", before.rows_est_vs_actual[:2])
        t0 = time.time()
        db.execute("ANALYZE orders", role="rw")
        print(f"ANALYZE took {time.time() - t0:.1f}s")
        after = env.observe().explain_query(hot)
        print("plan after: ", after.scan_types[:3], after.total_time_ms, "ms est/act", after.rows_est_vs_actual[:2])
        passed = False
        for wait in (30, 30, 60, 60):
            time.sleep(wait)
            kpi, _ = env.verify(settle_s=0)
            passed = metrics.eval_expr(env.spec["success"]["outcome"], kpi, baseline=env.healthy_kpi)
            print(kpi_line(f"after +{wait}s", kpi), "| success:", passed,
                  f"| ratio to alert {kpi.p50_ms / alert_p50:.2f}")
        return passed


if __name__ == "__main__":
    which = sys.argv[1] if len(sys.argv) > 1 else "connection"
    ok = {"connection": connection, "stale": stale,
          "stale_nofix": lambda: stale(fix=False), "misleading": misleading}[which]()
    print("FIX CALIBRATION", which, ":", "RECOVERED" if ok else "NOT RECOVERED")
