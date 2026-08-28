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
        base = int(inj.get("rows", 4_000_000))
        # 只向上抖：灌得越多分布越偏，故障只会更明显不会更弱。
        # 向下抖有把统计失真压到判据以下的风险，那就变成废场景了。
        return {"table": inj.get("table", "orders"),
                "rows": int(base * rng.uniform(1.0, 1.5))}

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
        if actual > 0 and abs(est - actual) / actual > 0.15:
            return True

        # 全表基数只差几个百分点不代表注入无效：往 1200 万行里灌 40 万
        # 只差 3.3%，但热查询谓词上的选择率估计可以差几百倍 —— 后者才是
        # 优化器选错计划的直接原因。所以真正该验的是"计划估计行数与实际
        # 行数的偏差倍数"，这也正是这类故障的判别特征。
        pred = self.spec.get("inject", {}).get("verify_predicate")
        if not pred:
            return False
        plan = db.query(
            "EXPLAIN (ANALYZE, FORMAT JSON, TIMING OFF) "
            f"SELECT count(*) FROM {t} WHERE {pred}")[0][0][0]["Plan"]

        def walk(node):
            yield node
            for child in node.get("Plans", []):
                yield from walk(child)

        worst = 0.0
        for node in walk(plan):
            loops = max(node.get("Actual Loops", 1), 1)
            est_rows = float(node.get("Plan Rows", 0)) * loops
            act_rows = float(node.get("Actual Rows", 0)) * loops
            if act_rows >= 1000:          # 小基数上的偏差没有判别意义
                worst = max(worst, act_rows / max(est_rows, 1.0))
        return worst >= 10.0


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
        base_max = int(inj.get("lock_id_max", 0))
        return {"table": inj.get("table", "orders"),
                "hold_rows": int(int(inj.get("hold_rows", 5000))
                                 * rng.uniform(1.0, 1.6)),
                # 锁住 id <= lock_id_max 的整段区间。必须覆盖热查询的
                # 取值空间，否则两者只是概率性相交，故障弱到量不出来。
                # 因此只向上抖 —— 锁得更多只会更严重。
                "lock_id_max": int(base_max * rng.uniform(1.0, 1.4))
                if base_max else 0,
                "duration_s": float(inj.get("duration_s", 600))}

    def _hold(self, params: dict) -> None:
        try:
            # 必须用普通角色持锁：superuser 的进程只有 superuser 能终止，
            # 而修复走的是 agent_rw —— 拿 superuser 持锁等于造了一个
            # agent 原理上修不好的故障。真实场景里持锁的也是业务连接。
            with db.connect(role="app", autocommit=False) as conn, \
                    conn.cursor() as cur:
                cur.execute("SELECT pg_backend_pid()")
                self._holder_pid = cur.fetchone()[0]
                if params["lock_id_max"]:
                    # 锁住整段 id 区间：对应真实场景里"批处理作业锁了一段
                    # 订单后卡住不提交"，且与热查询的取值空间完全重合。
                    cur.execute(
                        f"SELECT id FROM {params['table']} "
                        f"WHERE id <= {params['lock_id_max']} "
                        f"ORDER BY id FOR UPDATE")
                else:
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
        for _ in range(120):         # 等锁真正拿到（区间锁要扫十万行）
            time.sleep(0.25)
            if self._holder_pid:
                break
        return InjectionRecord(
            fault_class=self.fault_class, params=params,
            notes=f"后台事务 pid={self._holder_pid} 锁住 "
                  + (f"id <= {params['lock_id_max']} 的整段区间"
                     if params["lock_id_max"] else f"{params['hold_rows']} 行")
                  + "且不提交")

    def verify_injected(self, params: dict) -> bool:
        # 不能只数 ExclusiveLock：每个事务对自己的 transactionid 都持有
        # 一把 ExclusiveLock，这个条件恒为真，等于没验证。
        # 真正要确认的是那个具体的持锁者还在、事务还挂着、且行锁已拿到。
        if not self._holder_pid:
            return False
        rows = db.query(
            "SELECT a.state,"
            " count(*) FILTER (WHERE l.locktype = 'transactionid' AND l.granted)"
            " FROM pg_stat_activity a"
            " LEFT JOIN pg_locks l ON l.pid = a.pid"
            " WHERE a.pid = %s GROUP BY a.state", (self._holder_pid,))
        if not rows:
            return False                      # 持锁会话已经没了
        state, n_txlocks = rows[0]
        return state == "idle in transaction" and n_txlocks > 0

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
        # leave_free 越小故障越重，所以只向下抖，且不低于 1 ——
        # 留 0 个空位连 agent 自己的只读连接都建不了，就没法诊断了。
        base = int(inj.get("leave_free", 8))
        return {"leave_free": max(1, base - rng.randint(0, 1))}

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
            res = int(db.query("SHOW reserved_connections")[0][0])
            sres = int(db.query("SHOW superuser_reserved_connections")[0][0])
            used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
            held = len(self._conns)
            # app_user 最多只能占到 maxc - reserved - superuser_reserved，
            # 不扣这两项的话判据永远达不到，会误报"注入未生效"
            ceiling = maxc - res - sres
            return held > 10 and used >= ceiling - int(
                params.get("leave_free", 1))
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
