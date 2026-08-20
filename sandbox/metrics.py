"""KPI 采集：延迟来自负载生成器的滚动窗口，CPU 来自容器。

判分器要判 Outcome（修复有没有效），依据必须是外部可测的真实指标，
而不是 agent 自己汇报的"我觉得好了"。
"""
from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
METRICS_PATH = ROOT / "traces" / "workload_metrics.json"
CONTAINER = "pgdoctor-pg"


@dataclass
class KPI:
    p50_ms: float
    p95_ms: float
    p99_ms: float
    qps: float
    errors: int
    cpu_pct: float
    samples: int
    stale: bool = False          # 指标文件太旧 -> 负载生成器没在跑

    def as_dict(self) -> dict:
        return {
            "p50_ms": self.p50_ms, "p95_ms": self.p95_ms, "p99_ms": self.p99_ms,
            "qps": self.qps, "errors": self.errors, "cpu_pct": self.cpu_pct,
            "samples": self.samples, "stale": self.stale,
        }


def container_cpu_pct(container: str = CONTAINER) -> float:
    """容器 CPU 占用。docker stats 的百分比是相对单核累加的，可能 >100。"""
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container],
            capture_output=True, text=True, timeout=20,
        )
        return float(out.stdout.strip().rstrip("%") or 0.0)
    except Exception:
        return -1.0


def read_workload(max_age_s: float = 15.0, kind: str = "hot") -> dict:
    """读负载生成器落盘的滚动指标。"""
    if not METRICS_PATH.exists():
        return {}
    try:
        d = json.loads(METRICS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    age = time.time() - float(d.get("ts", 0))
    q = d.get("by_query", {}).get(kind, {})
    q["_stale"] = age > max_age_s
    return q


def collect(kind: str = "hot", include_all_errors: bool = True) -> KPI:
    w = read_workload(kind=kind)
    # 错误要跨查询类型聚合：连接池打满时热查询走的是常驻连接、完全正常，
    # 错误全部出在新建连接探针上。只看一类就会漏掉整类故障。
    err = int(w.get("errors", 0))
    if include_all_errors:
        try:
            import json as _json
            d = _json.loads(METRICS_PATH.read_text(encoding="utf-8"))
            err = sum(int(v.get("errors", 0))
                      for v in d.get("by_query", {}).values())
        except Exception:
            pass
    return KPI(
        p50_ms=float(w.get("p50_ms", 0.0)),
        p95_ms=float(w.get("p95_ms", 0.0)),
        p99_ms=float(w.get("p99_ms", 0.0)),
        qps=float(w.get("qps", 0.0)),
        errors=err,
        cpu_pct=container_cpu_pct(),
        samples=int(w.get("n", 0)),
        stale=bool(w.get("_stale", True)),
    )


def eval_expr(expr: str, kpi: KPI) -> bool:
    """判定 success.outcome / trigger.alert 这类表达式。

    只支持 `<字段> <比较符> <数值>` 用 AND/OR 连接的形式 —— 故意做得很窄，
    场景 DSL 是数据不是代码，不该有能力执行任意表达式。
    """
    import re

    vals = kpi.as_dict()
    tokens = re.split(r"\s+(AND|OR)\s+", expr.strip(), flags=re.I)
    result: bool | None = None
    op: str | None = None
    for tok in tokens:
        if tok.upper() in ("AND", "OR"):
            op = tok.upper()
            continue
        m = re.match(r"^\s*(\w+)\s*(<=|>=|<|>|==)\s*([\d.]+)\s*$", tok)
        if not m:
            raise ValueError(f"不支持的条件表达式: {tok!r}")
        field, cmp_, num = m.group(1), m.group(2), float(m.group(3))
        if field not in vals:
            raise ValueError(f"未知指标字段: {field}")
        v = float(vals[field])
        cur = {"<": v < num, ">": v > num, "<=": v <= num,
               ">=": v >= num, "==": v == num}[cmp_]
        if result is None:
            result = cur
        elif op == "AND":
            result = result and cur
        else:
            result = result or cur
    return bool(result)
