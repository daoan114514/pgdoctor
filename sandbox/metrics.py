"""KPI 采集：延迟来自负载生成器的滚动窗口，CPU 来自容器。

判分器要判 Outcome（修复有没有效），依据必须是外部可测的真实指标，
而不是 agent 自己汇报的"我觉得好了"。
"""
from __future__ import annotations

import json
import os
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
    """PostgreSQL CPU 占用；优先容器，原生/WSL 服务回退到进程采样。

    两种口径都是按单核累加，因此并行查询时可以超过 100%。只在数据库
    位于本机时做进程回退；远程库不能拿客户端机器的 CPU 冒充服务端指标。
    """
    try:
        out = subprocess.run(
            ["docker", "stats", "--no-stream", "--format", "{{.CPUPerc}}", container],
            capture_output=True, text=True, timeout=20,
        )
        value = out.stdout.strip().rstrip("%")
        if out.returncode == 0 and value:
            return float(value)
    except Exception:
        pass

    try:
        from sandbox import db
        if db.PG_HOST not in {"localhost", "127.0.0.1", "::1"}:
            return -1.0
        import psutil

        wanted = os.getenv("PGDOCTOR_POSTGRES_PROCESS", "postgres")
        processes = []
        for proc in psutil.process_iter(["name"]):
            try:
                if proc.info.get("name") == wanted:
                    proc.cpu_percent(None)
                    processes.append(proc)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        if not processes:
            return -1.0
        time.sleep(0.2)
        total = 0.0
        for proc in processes:
            try:
                total += proc.cpu_percent(None)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                continue
        return round(total, 1)
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


# 负载生成器写指标用的滚动窗口长度。env 要用它判断"窗口是否已经被故障期的样本填满"。
WINDOW_S = 30.0


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


def baseline_refs(baseline: "KPI | dict | None") -> dict:
    """把一组健康基线 KPI 摊成 `healthy_<字段>` 引用。

    加 healthy_ 前缀而不是直接复用字段名，是为了让判据一眼看得出比的是
    基线还是当前值 —— 两者同名的话，`cpu_pct < cpu_pct` 这种写法会既
    合法又毫无意义。
    """
    if baseline is None:
        return {}
    raw = baseline.as_dict() if hasattr(baseline, "as_dict") else dict(baseline)
    return {f"healthy_{k}": float(v) for k, v in raw.items()
            if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _threshold(rhs: str, refs: dict) -> float:
    """解析比较符右边：一个数，或者"倍数 × 某个健康基线"。

    只放开这一种乘法，不做通用表达式求值 —— 场景 DSL 仍然是数据。
    """
    import re

    rhs = rhs.strip()
    if re.fullmatch(r"[\d.]+", rhs):
        return float(rhs)
    m = (re.fullmatch(r"([\d.]+)\s*\*\s*(\w+)", rhs) or
         re.fullmatch(r"(\w+)\s*\*\s*([\d.]+)", rhs))
    if not m:
        raise ValueError(f"不支持的阈值写法: {rhs!r}")
    left, right = m.group(1), m.group(2)
    if _is_number(left):
        name, factor = right, left
    else:
        name, factor = left, right
    if name not in refs:
        # 分开报：没传基线和字段名写错是两种完全不同的错，混成一句会让
        # 排查从"谁没传参"变成"判据是不是写错了"。
        raise ValueError(
            f"判据引用了健康基线 {name}，但本次求值没有提供基线"
            if not refs else f"未知的健康基线字段: {name}")
    return float(factor) * refs[name]


def _is_number(tok: str) -> bool:
    try:
        float(tok)
    except ValueError:
        return False
    return True


def eval_expr(expr: str, kpi: KPI,
              baseline: "KPI | dict | None" = None) -> bool:
    """判定 success.outcome / trigger.alert 这类表达式。

    只支持 `<字段> <比较符> <数值>`，或 `<字段> <比较符> <倍数> * healthy_<字段>`，
    用 AND/OR 连接 —— 故意做得很窄，场景 DSL 是数据不是代码，不该有能力
    执行任意表达式。

    右边允许乘一个健康基线，是因为绝对阈值不可移植：`cpu_pct < 100` 在
    参考机上健康态实测 38%，在 18 核开发机上健康态实测 72–123%，于是
    正确的修复被判成"KPI 未恢复"、连正确的索引一起回滚掉。基线取的是
    本 episode 注入前实测的那一组，不是场景里写死的 `baseline.healthy_*`，
    换机器会自动跟着走。
    """
    import re

    vals = kpi.as_dict()
    refs = baseline_refs(baseline)
    tokens = re.split(r"\s+(AND|OR)\s+", expr.strip(), flags=re.I)
    result: bool | None = None
    op: str | None = None
    for tok in tokens:
        if tok.upper() in ("AND", "OR"):
            op = tok.upper()
            continue
        m = re.match(r"^\s*(\w+)\s*(<=|>=|<|>|==)\s*(.+?)\s*$", tok)
        if not m:
            raise ValueError(f"不支持的条件表达式: {tok!r}")
        field, cmp_ = m.group(1), m.group(2)
        if field not in vals:
            raise ValueError(f"未知指标字段: {field}")
        num = _threshold(m.group(3), refs)
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
