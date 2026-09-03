"""Lightweight injectors for P0 diagnosis-contract scenarios.

The database objects are real where that is cheap and reversible. Magnitudes
that would be destructive or slow (85% disk usage, 1GB retained WAL, one hour
of prepared-transaction age) stay in the scenario's observation fixture.
"""
from __future__ import annotations

import re

from psycopg import sql

from sandbox import db
from sandbox.injectors.base import Injector, InjectionRecord


def _name(value: str, kind: str) -> str:
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,62}", value):
        raise ValueError(f"invalid {kind}: {value!r}")
    return value


class AutovacuumStarvationInjector(Injector):
    fault_class = "autovacuum_starvation"

    def __init__(self, spec: dict):
        super().__init__(spec)
        self._has_original = False
        self._original: bool | None = None

    def params(self, rng) -> dict:
        table = self.spec.get("inject", {}).get("table", "orders")
        return {"table": _name(table, "table")}

    def inject(self, params: dict) -> InjectionRecord:
        table = params["table"]
        rows = db.query(
            "SELECT option_value::boolean FROM pg_class c "
            "CROSS JOIN LATERAL pg_options_to_table(c.reloptions) "
            "WHERE c.oid=%s::regclass AND option_name='autovacuum_enabled'",
            (table,))
        self._original = bool(rows[0][0]) if rows else None
        self._has_original = True
        db.execute(sql.SQL("ALTER TABLE {} SET (autovacuum_enabled = false)")
                   .format(sql.Identifier(table)))
        return InjectionRecord(self.fault_class, params,
                               f"disabled autovacuum on {table}")

    def verify_injected(self, params: dict) -> bool:
        rows = db.query(
            "SELECT coalesce((SELECT option_value::boolean "
            "FROM pg_options_to_table(reloptions) "
            "WHERE option_name='autovacuum_enabled'), true) "
            "FROM pg_class WHERE oid=%s::regclass", (params["table"],))
        return bool(rows) and rows[0][0] is False

    def cleanup(self) -> None:
        if not self._has_original:
            return
        table = _name(self.spec.get("inject", {}).get("table", "orders"), "table")
        if self._original is None:
            statement = sql.SQL("ALTER TABLE {} RESET (autovacuum_enabled)").format(
                sql.Identifier(table))
        else:
            statement = sql.SQL(
                "ALTER TABLE {} SET (autovacuum_enabled = {})").format(
                    sql.Identifier(table), sql.SQL("true" if self._original else "false"))
        db.execute(statement)
        self._has_original = False


class StaleReplicationSlotInjector(Injector):
    fault_class = "stale_replication_slot"

    def params(self, rng) -> dict:
        slot = self.spec.get("inject", {}).get("slot", "pgdoctor_p0_slot")
        return {"slot": _name(slot, "slot")}

    def inject(self, params: dict) -> InjectionRecord:
        self.cleanup()
        db.execute("SELECT pg_create_physical_replication_slot(%s)",
                   (params["slot"],))
        return InjectionRecord(self.fault_class, params,
                               f"created inactive physical slot {params['slot']}")

    def verify_injected(self, params: dict) -> bool:
        rows = db.query(
            "SELECT active FROM pg_replication_slots WHERE slot_name=%s",
            (params["slot"],))
        return bool(rows) and rows[0][0] is False

    def cleanup(self) -> None:
        slot = _name(self.spec.get("inject", {}).get("slot", "pgdoctor_p0_slot"),
                     "slot")
        if db.query("SELECT 1 FROM pg_replication_slots WHERE slot_name=%s", (slot,)):
            db.execute("SELECT pg_drop_replication_slot(%s)", (slot,))


class OrphanedPreparedTransactionInjector(Injector):
    fault_class = "orphaned_prepared_transaction"

    def params(self, rng) -> dict:
        gid = self.spec.get("inject", {}).get("gid", "pgdoctor_p0_prepared")
        return {"gid": _name(gid, "prepared transaction GID")}

    def inject(self, params: dict) -> InjectionRecord:
        max_prepared = int(db.query("SHOW max_prepared_transactions")[0][0])
        if max_prepared <= 0:
            raise RuntimeError(
                "max_prepared_transactions=0; set it above zero and restart PostgreSQL")
        self.cleanup()
        with db.connect(autocommit=False) as conn, conn.cursor() as cur:
            # Force XID assignment without changing application data.
            cur.execute("SELECT txid_current()")
            cur.execute(sql.SQL("PREPARE TRANSACTION {}")
                        .format(sql.Literal(params["gid"])))
        return InjectionRecord(self.fault_class, params,
                               f"prepared transaction {params['gid']}")

    def verify_injected(self, params: dict) -> bool:
        return bool(db.query("SELECT 1 FROM pg_prepared_xacts WHERE gid=%s",
                             (params["gid"],)))

    def cleanup(self) -> None:
        gid = _name(self.spec.get("inject", {}).get("gid", "pgdoctor_p0_prepared"),
                    "prepared transaction GID")
        if db.query("SELECT 1 FROM pg_prepared_xacts WHERE gid=%s", (gid,)):
            db.execute(sql.SQL("ROLLBACK PREPARED {}")
                       .format(sql.Literal(gid)))


class DiskPressureInjector(Injector):
    """Register a provider fixture without consuming host disk."""

    fault_class = "disk_pressure"
    semantic_only = True

    def params(self, rng) -> dict:
        used = self.spec.get("inject", {}).get("fixture_used_pct", 92.0)
        return {"used_pct": float(used)}

    def inject(self, params: dict) -> InjectionRecord:
        return InjectionRecord(
            self.fault_class, params,
            f"disk provider fixture reports {params['used_pct']:.1f}% used")

    def verify_injected(self, params: dict) -> bool:
        return params["used_pct"] >= 85.0

    def cleanup(self) -> None:
        return None


REGISTRY = {
    AutovacuumStarvationInjector.fault_class: AutovacuumStarvationInjector,
    DiskPressureInjector.fault_class: DiskPressureInjector,
    OrphanedPreparedTransactionInjector.fault_class:
        OrphanedPreparedTransactionInjector,
    StaleReplicationSlotInjector.fault_class: StaleReplicationSlotInjector,
}
