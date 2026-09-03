"""DBAScenarioEnv —— 沙箱的对外接口。

一个 episode 的生命周期：
    reset()   回滚到 golden -> 起负载 -> 采健康基线 -> 注入故障 -> 等告警
    observe() 交出只读观测工具（agent 的眼睛）
    verify()  外部可测的 KPI + 回归套件
    score()   Diagnosis / Outcome / Safe Pass

注意基线必须在注入之前采：回归套件比的是"修复后 vs 故障前"，
如果在故障态采基线，等于把故障本身当成了正常水位。
"""
from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sandbox import db, metrics, snapshot
from sandbox.observe import Observer
from sandbox.scoring import EpisodeScore, RegressionSuite, score_episode
from sandbox.traces import TraceStore

ROOT = Path(__file__).resolve().parent.parent
INJECTORS: dict = {}


def _load_injectors() -> dict:
    global INJECTORS
    if not INJECTORS:
        from sandbox.injectors.misleading import REGISTRY as R3
        from sandbox.injectors.missing_index import REGISTRY as R1
        from sandbox.injectors.more import REGISTRY as R2
        from sandbox.injectors.p0 import REGISTRY as R4
        INJECTORS = {**R1, **R2, **R3, **R4}
    return INJECTORS


@dataclass
class Observation:
    alert: str
    fired: bool
    healthy_kpi: dict
    current_kpi: dict
    episode_id: str
    scenario_id: str
    notes: list[str] = field(default_factory=list)


class DBAScenarioEnv:
    def __init__(self, scenario: str, warmup_s: float = 20.0,
                 degrade_timeout_s: float = 90.0, quiet: bool = False):
        p = Path(scenario)
        if not p.is_absolute():
            p = ROOT / p
        self.spec = yaml.safe_load(p.read_text(encoding="utf-8"))
        self.scenario_path = p
        self.warmup_s = warmup_s
        self.degrade_timeout_s = degrade_timeout_s
        self.quiet = quiet

        self.episode_id = f"ep_{self.spec['id']}_{int(time.time())}"
        self.trace = TraceStore(self.episode_id)
        self.observer = Observer(self.trace)
        self.suite = RegressionSuite(self.spec["workload"].get("canary_queries", []))
        self._wl: subprocess.Popen | None = None
        self.healthy_kpi: metrics.KPI | None = None
        self.injection = None
        self.applied_sql: list[str] = []

    # ── 负载生成器 ────────────────────────────────────────────
    def _start_workload(self) -> None:
        self._stop_workload()
        self._wl = subprocess.Popen(
            [sys.executable, "-m", "sandbox.workload",
             "--scenario", str(self.scenario_path)],
            cwd=str(ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )

    def _stop_workload(self) -> None:
        if self._wl and self._wl.poll() is None:
            self._wl.terminate()
            try:
                self._wl.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._wl.kill()
        self._wl = None

    def _log(self, msg: str) -> None:
        if not self.quiet:
            print(msg, flush=True)

    # ── 生命周期 ──────────────────────────────────────────────
    def reset(self, seed: int = 0, verify_timeout_s: float = 60.0) -> Observation:
        import random

        notes = []
        self._log("[env] 回滚到 golden ...")
        self._stop_workload()
        snapshot.reset()

        self._log("[env] 启动负载生成器 ...")
        self._start_workload()
        time.sleep(self.warmup_s)

        self._log("[env] 采集健康基线（必须在注入之前）...")
        self.suite.capture_baseline()
        self.healthy_kpi = metrics.collect()
        if self.healthy_kpi.stale:
            notes.append("警告：负载指标过期，健康基线可能不可靠")
        self._log(f"       p50={self.healthy_kpi.p50_ms}ms "
                  f"p99={self.healthy_kpi.p99_ms}ms cpu={self.healthy_kpi.cpu_pct}%")

        fault_class = self.spec["fault_class"]
        injector_cls = _load_injectors().get(fault_class)
        if injector_cls is None:
            raise KeyError(f"没有注册 {fault_class} 的注入器")
        injector = injector_cls(self.spec)
        params = injector.params(random.Random(seed))
        # 注入器给出的探针 uid 透给策略，让热查询每轮打在不同的行段上
        self.probe_uid = params.get("probe_uid")
        self._injector = injector
        self.injection = injector.inject(params)
        # 轮询而不是查一次。注入不是瞬时完成的：锁竞争要扫十几万行才拿
        # 到行锁、统计过期要灌几十万行、连接打满要建近百条连接，而
        # verify_injected 判的是"到位没有"。掐一个时间点问一次，赶上慢的
        # 那次就误报"注入未生效" —— 实测第 3 轮因此丢掉 20 例，同种子
        # 重跑却是好的。偶发丢数据比直接失败更危险，它是静默的。
        deadline = time.time() + verify_timeout_s
        ok = injector.verify_injected(params)
        while not ok and time.time() < deadline:
            time.sleep(1.0)
            ok = injector.verify_injected(params)
        if not ok:
            raise RuntimeError(f"故障注入未生效: {fault_class}")
        self._log(f"[env] 已注入: {self.injection.notes}")

        injected_at = time.time()
        alert_expr = self.spec["trigger"]["alert"]
        fired, cur = self._wait_for_alert(alert_expr)
        if not fired:
            notes.append(f"告警未在 {self.degrade_timeout_s}s 内触发；该 episode 不可用")

        if fired:
            # 告警响了不等于 KPI 窗口已经反映故障。健康态吞吐往往远高于
            # 故障态：锁竞争注入后每条热查询都要阻塞 5 秒才超时，而健康态
            # 是每秒五百多次 —— 30 秒滚动窗口里绝大多数样本仍是注入前的，
            # p99 被稀释到看不出异常（实测 1.7 万个样本里只有 6 个超时，
            # 占 0.035%，p99 显示 14ms 像是没事）。
            # 等窗口被故障期样本填满再测，故障态 KPI 才和修复后的 KPI 可比，
            # 否则 Outcome 判据是拿两个不同口径的数在比。
            settle = metrics.WINDOW_S - (time.time() - injected_at)
            if settle > 0:
                self._log(f"[env] 等待 {settle:.0f}s 让指标窗口填满故障期样本 ...")
                time.sleep(settle)
            cur = metrics.collect()

        self._log(f"[env] 告警({alert_expr}) 触发={fired} "
                  f"p99={cur.p99_ms}ms errors={cur.errors} cpu={cur.cpu_pct}%")

        return Observation(
            alert=alert_expr, fired=fired,
            healthy_kpi=self.healthy_kpi.as_dict(), current_kpi=cur.as_dict(),
            episode_id=self.episode_id, scenario_id=self.spec["id"], notes=notes,
        )

    def _wait_for_alert(self, expr: str) -> tuple[bool, metrics.KPI]:
        deadline = time.time() + self.degrade_timeout_s
        cur = metrics.collect()
        while time.time() < deadline:
            cur = metrics.collect()
            if not cur.stale and metrics.eval_expr(
                    expr, cur, baseline=self.healthy_kpi):
                return True, cur
            time.sleep(5)
        return False, cur

    def observe(self) -> Observer:
        return self.observer

    def apply_sql(self, sql: str) -> None:
        """W2 阶段的临时通道：直接以 agent_rw 执行修复。

        W4 起这条路会被关掉，所有写操作必须经 remediation_server 的
        护盾与安全门，agent 拿不到 agent_rw 凭据。
        """
        db.execute(sql, role="rw")
        self.applied_sql.append(sql)

    def verify(self, settle_s: float = 0.0) -> tuple[metrics.KPI, object]:
        """修复之后重测。

        必须等满一个完整的指标窗口。窗口是 30 秒的滚动样本，等不满的话
        里面仍混着故障期的样本 —— p99 是分位数，只要还有超过 1% 的样本
        是故障期那些 5 秒超时，p99 就依然是 5000ms，于是正确的修复被判成
        "KPI 未恢复"并被回滚掉。这是注入侧那个问题（告警触发 ≠ 窗口已反映
        故障）的镜像。

        实测人工执行正解：锁竞争 +15s errors=18 不达标、+35s errors=0 达标；
        统计过期 +15s p99=1076ms 不达标、+35s p99=599ms 达标。
        """
        # 传进来的值只能加码不能减码：调用方想多等可以，想少等不行。
        time.sleep(max(settle_s, metrics.WINDOW_S + 5.0))
        kpi = metrics.collect()
        reg = self.suite.check()
        return kpi, reg

    def score(self, claimed_fault_class: str | None,
              audit: dict | None = None,
              kpi=None, regression=None,
              ledger: dict | None = None) -> EpisodeScore:
        if kpi is None or regression is None:
            kpi, regression = self.verify()
        return score_episode(self.spec, claimed_fault_class, self.applied_sql,
                             kpi, regression, audit, ledger,
                             baseline=self.healthy_kpi)

    def close(self) -> None:
        self._stop_workload()
        # 有些注入器会留下后台事务或占用的连接，不清会拖垮后续 episode
        inj = getattr(self, "_injector", None)
        if inj is not None and hasattr(inj, "cleanup"):
            try:
                inj.cleanup()
            except Exception:
                pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
