"""三个新故障注入器：坏计划 / 锁阻塞 / 连接打满。

选这三个是因为它们的 oracle 各不相同，能真正拉开鉴别诊断的难度：
  stale_statistics    统计信息陈旧，EXPLAIN 的估计行数与实际严重偏离
  lock_contention     阻塞链非空，会话卡在 Lock 等待
  connection_exhaustion  连接数逼近上限

关键点在于它们都会引发"p99 上升"这个共同症状 —— 只看延迟分不出来，
必须做鉴别诊断。这正是 ESC 的 D2 存在的意义。
"""
from __future__ import annotations

import threading
import time

from sandbox import db
from sandbox.injectors.base import Injector, InjectionRecord


class StaleStatisticsInjector(Injector):
    """让优化器基于陈旧统计信息选坏计划。

    做法：关掉 autoanalyze，然后大量插入倾斜数据但不 ANALYZE。
    优化器仍以为 PENDING 只占 10%，实际已经变成绝大多数，
    于是选了错误的扫描方式。
    """

    fault_class = "stale_statistics"

    def params(self, rng) -> dict:
        inj = self.spec["inject"]
        return {"table": inj.get("table", "orders"),
                "rows": int(inj.get("rows", 4_000_000))}

    def inject(self, params: dict) -> InjectionRecord:
        t = params["table"]
        db.execute(f"ALTER TABLE {t} SET (autovacuum_enabled = false)")
        # 先固化一份"旧"统计信息，再灌入完全改变分布的数据
        db.execute(f"ANALYZE {t}")
        db.execute(
            f"INSERT INTO {t} (user_id, status, total, created_at) "
            f"SELECT 1 + (g % 100000), 'PENDING', 1.0, now() "
            f"FROM generate_series(1, {params['rows']}) g")
        return InjectionRecord(
            fault_class=self.fault_class, params=params,
            notes=f"{t}: 关闭 autovacuum 并灌入 {params['rows']:,} 行倾斜数据，"
                  f"统计信息未更新")

    def verify_injected(self, params: dict) -> bool:
        t = params["table"]
        rows = db.query(
            "SELECT reltuples::bigint, (SELECT count(*) FROM " + t + ") "
            "FROM pg_class WHERE relname = %s", (t,))
        if not rows:
            return False
        est, actual = rows[0]
        # 估计与实际相差 15% 以上即算注入成功。
        # 阈值不能定太高：优化器选错计划并不需要偏差多离谱，
        # 关键是 status 的分布被改变了（PENDING 从 10% 拉到 ~33%）。
        return actual > 0 and abs(est - actual) / actual > 0.15


class LockContentionInjector(Injector):
    """制造锁阻塞链。

    后台开一个事务锁住若干行然后挂着不提交，工作负载里的更新会卡住。
    这类故障的判别特征很干净：pg_locks 里有非空阻塞链，
    而缺索引类故障是没有的。
    """

    fault_class = "lock_contention"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._holder_pid: int | None = None

    def params(self, rng) -> dict:
        inj = self.spec["inject"]
        return {"table": inj.get("table", "orders"),
                "hold_rows": int(inj.get("hold_rows", 5000)),
                "duration_s": float(inj.get("duration_s", 600))}

    def _hold(self, params: dict) -> None:
        try:
            with db.connect(autocommit=False) as conn, conn.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                self._holder_pid = cur.fetchone()[0]
                cur.execute(
                    f"SELECT id FROM {params['table']} "
                    f"WHERE status = 'PENDING' "
                    f"ORDER BY id LIMIT {params['hold_rows']} FOR UPDATE")
                cur.fetchall()
                # 拿着锁不提交，直到 episode 结束
                self._stop.wait(params["duration_s"])
                conn.rollback()
        except Exception:
            pass

    def inject(self, params: dict) -> InjectionRecord:
        self._stop.clear()
        self._thread = threading.Thread(target=self._hold, args=(params,),
                                        daemon=True)
        self._thread.start()
        for _ in range(40):          # 等锁真正拿到
            time.sleep(0.25)
            if self._holder_pid:
                break
        return InjectionRecord(
            fault_class=self.fault_class, params=params,
            notes=f"后台事务 pid={self._holder_pid} 锁住 "
                  f"{params['hold_rows']} 行且不提交")

    def verify_injected(self, params: dict) -> bool:
        rows = db.query(
            "SELECT count(*) FROM pg_locks WHERE granted AND mode = %s",
            ("ExclusiveLock",))
        return bool(self._holder_pid) and rows and rows[0][0] > 0

    def cleanup(self) -> None:
        self._stop.set()
        if self._holder_pid:
            try:
                db.execute("SELECT pg_terminate_backend(%s)", (self._holder_pid,))
            except Exception:
                pass


class ConnectionExhaustionInjector(Injector):
    """把连接池占满。

    开一批 idle 连接顶住 max_connections，新连接被拒。
    判别特征：连接数逼近上限，且大量会话处于 idle 状态。
    """

    fault_class = "connection_exhaustion"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self._conns: list = []

    def params(self, rng) -> dict:
        inj = self.spec["inject"]
        return {"leave_free": int(inj.get("leave_free", 8))}

    def inject(self, params: dict) -> InjectionRecord:
        """填到真的连不上为止，而不是按公式算目标数。

        按 max_connections 减保留位算目标看着精确，实际总差几个：
        负载生成器自己的连接数在变、探针连接是瞬时的、superuser 保留位
        的行为也和直觉不同。实测按公式填到 97/100 仍然连得上。
        直接填到抛异常最稳，且天然自适应。
        """
        import psycopg

        maxc = int(db.query("SHOW max_connections")[0][0])
        opened, last_err = 0, ""
        for _ in range(maxc + 20):          # 上限兜底，避免死循环
            try:
                c = psycopg.connect(db.dsn("app"), autocommit=True)
                self._conns.append(c)
                opened += 1
            except Exception as exc:
                last_err = str(exc)[:120]
                break
        # 再退还 leave_free 个，让症状是"偶发失败"而不是"完全瘫痪" ——
        # 后者连 agent 自己的只读连接都建不了，就没法诊断了
        for _ in range(int(params.get("leave_free", 1))):
            if self._conns:
                try:
                    self._conns.pop().close()
                except Exception:
                    pass
        used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
        return InjectionRecord(
            fault_class=self.fault_class, params=params,
            notes=f"占用 {opened} 个普通用户连接直至耗尽"
                  f"(now {used}/{maxc}); 首次失败: {last_err or '未触发'}")

    def verify_injected(self, params: dict) -> bool:
        """判据是"普通用户的位子已基本占满"。

        注意不能用"完全连不上"当判据：注入时特意退还了 leave_free 个
        连接，否则 agent 自己也连不上就没法诊断了。症状要的是"偶发
        失败"而不是"彻底瘫痪"。
        """
        try:
            maxc = int(db.query("SHOW max_connections")[0][0])
            used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
            held = len(self._conns)
            return held > 10 and used >= maxc - int(params.get("leave_free", 1)) - 3
        except Exception:
            return len(self._conns) > 10

    def cleanup(self) -> None:
        for c in self._conns:
            try:
                c.close()
            except Exception:
                pass
        self._conns.clear()


REGISTRY = {
    StaleStatisticsInjector.fault_class: StaleStatisticsInjector,
    LockContentionInjector.fault_class: LockContentionInjector,
    ConnectionExhaustionInjector.fault_class: ConnectionExhaustionInjector,
}
