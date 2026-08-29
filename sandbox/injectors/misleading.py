"""误导性告警注入器。

DBA-Bench 里 misleading alerts 是单独一类（10 个场景，8 个标 Hard），
考的是"告警指向的东西不是真根因"。这类场景对本项目特别重要 —— ESC
的全部意义就是拦住"顺着表象往下编"这个动作，而只有在表象具备误导性
的场景里，这个能力才量得出来。

设计原则：表面症状必须与被误认的那个根因**完全一致**，判别只能靠一条
需要多走一步才拿得到的证据。否则就不是误导，只是难。
"""
from __future__ import annotations

from sandbox import db
from sandbox.injectors.base import Injector, InjectionRecord


class IdleTransactionPileupInjector(Injector):
    """一批未提交的事务占满连接槽 —— 伪装成连接打满。

    表面症状与 ConnectionExhaustionInjector 一模一样：连接数逼近上限、
    新连接被拒、吞吐下降。告警照着念就是"连接池满了"。

    但真根因是长事务：这些会话处于 **idle in transaction** 而不是 idle。
    区别不只是名字 —— 它们还握着旧快照挡住 vacuum，且调大 max_connections
    这类"针对连接打满"的修复对它们完全无效，过一会儿照样占满。

    判别点只有一个：pg_stat_activity 里的 state。这正是因果图上那条
    idle_in_transaction 判别边 power 给到 0.95 的原因 —— 它是唯一的
    区分证据，漏了就只能靠猜。

    正解是终止这些挂着的事务（terminate_blocker），不是加连接数。
    """

    fault_class = "long_idle_transaction"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self._conns: list = []

    def params(self, rng) -> dict:
        inj = self.spec["inject"]
        # 不抖 leave_free。这个场景在这条轴上栽过两次：设成 6 时告警连续
        # 两轮不触发；改成 2 之后，同一个值有的种子出错有的不出错 ——
        # 它是刀刃参数，出不出错取决于时序而非取值。随机化它等于往评测里
        # 注入不确定性。区分实例改用场景里的并发度。
        return {"leave_free": int(inj.get("leave_free", 8)),
                "min_idle_txn": int(inj.get("min_idle_txn", 10))}

    def inject(self, params: dict) -> InjectionRecord:
        """填到连不上为止，但每条连接都停在 idle in transaction。

        和 ConnectionExhaustionInjector 一样按"填到抛异常"而不是按公式
        算目标数 —— 那边的注释解释了为什么公式算不准。

        事务里只跑 SELECT 1：既进入了 idle in transaction 状态，又不持有
        任何行锁。持了锁就变成 lock_contention 了，那是另一类故障，混在
        一起这个场景就不再是"干净的误导"。
        """
        import psycopg

        maxc = int(db.query("SHOW max_connections")[0][0])
        opened, last_err = 0, ""
        for _ in range(maxc + 20):
            try:
                c = psycopg.connect(db.dsn("app"), autocommit=False)
                with c.cursor() as cur:
                    cur.execute("SELECT 1")      # 进入事务，但不拿任何锁
                self._conns.append(c)
                opened += 1
            except Exception as exc:
                last_err = str(exc)[:120]
                break
        # 退还几个，让症状是"偶发失败"而非"完全瘫痪" —— 后者连 agent
        # 自己的只读连接都建不了，就没法诊断了
        for _ in range(int(params.get("leave_free", 1))):
            if self._conns:
                try:
                    self._conns.pop().close()
                except Exception:
                    pass

        used = db.query("SELECT count(*) FROM pg_stat_activity")[0][0]
        n_idle_txn = db.query(
            "SELECT count(*) FROM pg_stat_activity "
            "WHERE state = 'idle in transaction'")[0][0]
        return InjectionRecord(
            fault_class=self.fault_class, params=params,
            notes=f"{opened} 个未提交事务占住连接槽 (now {used}/{maxc}, "
                  f"其中 idle in transaction {n_idle_txn}); "
                  f"首次失败: {last_err or '未触发'}")

    def verify_injected(self, params: dict) -> bool:
        """判据是"确实有一批挂着的事务"，不是"连接满了"。

        只验连接数会让这个注入器和 connection_exhaustion 无法区分 ——
        而两者可区分正是这个场景存在的全部理由。
        """
        try:
            n = db.query(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE state = 'idle in transaction'")[0][0]
            return int(n) >= int(params.get("min_idle_txn", 10))
        except Exception:
            return len(self._conns) >= int(params.get("min_idle_txn", 10))

    def cleanup(self) -> None:
        for c in self._conns:
            try:
                c.rollback()
            except Exception:
                pass
            try:
                c.close()
            except Exception:
                pass
        self._conns.clear()


REGISTRY = {
    IdleTransactionPileupInjector.fault_class: IdleTransactionPileupInjector,
}
