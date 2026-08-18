"""缺索引故障。

健康基线带 idx_orders_status_created；注入动作就是 DROP 掉它。
热查询于是从"索引扫描取前 20 条"退化为"全表扫 1200 万行 + 排序"。

选这个做第一个注入器，是因为它的 oracle 最干净：
EXPLAIN 会直接给出 Seq Scan 和 Rows Removed by Filter，无解释空间。
"""
from __future__ import annotations

from sandbox import db
from sandbox.injectors.base import Injector, InjectionRecord


class MissingIndexInjector(Injector):
    fault_class = "missing_index"

    def params(self, rng) -> dict:
        inj = self.spec["inject"]
        return {
            "index": inj["index"],
            "table": inj.get("table", "orders"),
            "columns": inj.get("columns", ["status"]),
        }

    def inject(self, params: dict) -> InjectionRecord:
        idx = params["index"]
        db.execute(f'DROP INDEX IF EXISTS "{idx}"')
        # 丢索引后统计信息本身仍是新鲜的 —— 这点很重要：
        # 它让 stale_statistics 这个竞争假设可以被 agent 干净地排除，
        # 从而 ESC 的 D2 鉴别诊断有真东西可做。
        db.execute(f'ANALYZE {params["table"]}')
        return InjectionRecord(
            fault_class=self.fault_class,
            params=params,
            notes=f"dropped {idx} on {params['table']}({','.join(params['columns'])})",
        )

    def verify_injected(self, params: dict) -> bool:
        rows = db.query(
            "SELECT 1 FROM pg_indexes WHERE tablename = %s AND indexname = %s",
            (params["table"], params["index"]),
        )
        return not rows


REGISTRY = {MissingIndexInjector.fault_class: MissingIndexInjector}
