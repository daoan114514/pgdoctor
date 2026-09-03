"""按官方文档扩图后的验收。

扩图最容易破的是两条自定的约束，两条都在这里钉住：
  1. Evidence 节点必须有工具真能产出 —— 否则 ESC 只能退化成看模型自述
  2. 新证据必须有取值判据 —— 否则走默认分支恒返回 True，"调过工具就算
     取证"，正是刚给 idle_in_transaction 修掉的那个 bug
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.esc import _supports
from agent.state_machine import ALLOWED_TOOLS, Phase
from agent.toolbox import Toolbox
from knowledge.causal_graph import graph as G
from knowledge.evolution import TOOL_OF

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


g = G.load()
st = G.stats()
ROOTS = [n for n, d in g.nodes(data=True) if d.get("kind") == "RootCause"]
EVID = [n for n, d in g.nodes(data=True) if d.get("kind") == "Evidence"]

print(f"图: {st}\n")

print("[1] 每个根因都得可确认、可修复")
no_ev = [n for n in ROOTS
         if not G.required_evidence(n) and not G.supporting_evidence(n)]
check("没有'无法确认'的根因", not no_ev, no_ev)
no_fix = [n for n in ROOTS if not G.fixes_for(n)]
check("没有'诊断出来也不知道怎么修'的根因", not no_fix, no_fix)

print("\n[2] 每个证据节点都得有工具真能产出")
tb_methods = {m for m in dir(Toolbox) if not m.startswith("_")}
bad = []
for e in EVID:
    by = g.nodes[e].get("obtained_by")
    if not by:
        bad.append(f"{e}(未标 obtained_by)")
    elif by not in tb_methods:
        bad.append(f"{e}(工具 {by} 不存在)")
check("证据都挂到了真实工具上", not bad, bad)

orphan = [e for e in EVID if not list(g.in_edges(e)) and not list(g.out_edges(e))]
check("没有孤点证据", not orphan, orphan)

print("\n[3] 新工具进了 agent 的动作空间")
for t in ("get_vacuum_horizon", "get_database_stats"):
    check(f"{t} 在 OBSERVE 白名单里", t in ALLOWED_TOOLS[Phase.OBSERVE])
    check(f"{t} 在 GATE 阶段不可用（写区仍是空集）",
          t not in ALLOWED_TOOLS[Phase.GATE])

print("\n[4] L4 认得新证据（否则判别力统计会漏掉它们）")
missing = [e for e in EVID if e not in TOOL_OF]
check("TOOL_OF 覆盖所有证据类型", not missing, missing)

print("\n[5] 取值判据：拿到证据不等于证据支持结论")
CASES = [
    ("autovacuum_health",
     "orders: autovacuum_enabled=True running=False dead=100 trigger=1,000 "
     "backlog=0.10 last_autovacuum=2026-09-01",
     "autovacuum_starvation", False),
    ("autovacuum_health",
     "orders: autovacuum_enabled=False running=False dead=0 trigger=1,000 "
     "backlog=0.00 last_autovacuum=空",
     "autovacuum_starvation", True),
    ("autovacuum_health",
     "orders: autovacuum_enabled=True running=False dead=3,000 trigger=1,000 "
     "backlog=3.00 last_autovacuum=2026-08-01",
     "autovacuum_starvation", True),
    ("autovacuum_health",
     "orders: autovacuum_enabled=True running=True dead=3,000 trigger=1,000 "
     "backlog=3.00 last_autovacuum=2026-08-01",
     "autovacuum_starvation", False),
    ("xid_age", "占 freeze_max_age 0.4%, 风险=False", "xid_wraparound_risk", False),
    ("xid_age", "占 freeze_max_age 87.0%, 风险=True", "xid_wraparound_risk", True),
    ("deadlock_count", "窗口 30.0s: 死锁增量=0, 回滚增量=0/提交增量=88",
     "deadlock", False),
    ("deadlock_count", "窗口 30.0s: 死锁增量=3, 回滚增量=2/提交增量=88",
     "deadlock", True),
    ("temp_file_volume", "窗口 30.0s: 临时文件增量=0 个, 外溢增量 0.0 MB",
     "work_mem_spill", False),
    ("temp_file_volume", "窗口 30.0s: 临时文件增量=9 个, 外溢增量 512.0 MB",
     "work_mem_spill", True),
    ("replication_slot_age",
     "复制槽 0 个, 非活动=0, 非活动槽最大 horizon 年龄=0, "
     "非活动槽最大 WAL 滞留=0.0 MB; 明细=[]",
     "stale_replication_slot", False),
    ("replication_slot_age",
     "复制槽 1 个, 非活动=0, 非活动槽最大 horizon 年龄=0, "
     "非活动槽最大 WAL 滞留=0.0 MB; 明细=[{'active': True}]",
     "stale_replication_slot", False),
    ("replication_slot_age",
     "复制槽 1 个, 非活动=1, 非活动槽最大 horizon 年龄=50,000, "
     "非活动槽最大 WAL 滞留=10.0 MB; 明细=[]",
     "stale_replication_slot", False),
    ("replication_slot_age",
     "复制槽 1 个, 非活动=1, 非活动槽最大 horizon 年龄=5,000,000, "
     "非活动槽最大 WAL 滞留=10.0 MB; 明细=[]",
     "stale_replication_slot", True),
    ("replication_slot_age",
     "复制槽 1 个, 非活动=1, 非活动槽最大 horizon 年龄=0, "
     "非活动槽最大 WAL 滞留=2048.0 MB; 明细=[]",
     "stale_replication_slot", True),
    ("prepared_xact_age", "预备事务 0 个, 最大 XID 年龄=0, 最长挂起=0s",
     "orphaned_prepared_transaction", False),
    ("prepared_xact_age", "预备事务 1 个, 最大 XID 年龄=10, 最长挂起=30s",
     "orphaned_prepared_transaction", False),
    ("prepared_xact_age",
     "预备事务 1 个, 最大 XID 年龄=5,000,000, 最长挂起=30s",
     "orphaned_prepared_transaction", True),
    ("prepared_xact_age", "预备事务 1 个, 最大 XID 年龄=10, 最长挂起=7,200s",
     "orphaned_prepared_transaction", True),
    ("disk_usage", "磁盘使用率=42.0%, 可用=100.0 GB, 路径=/var/lib/postgresql",
     "disk_pressure", False),
    ("disk_usage", "磁盘使用率=92.0%, 可用=8.0 GB, 路径=/var/lib/postgresql",
     "disk_pressure", True),
    ("checkpoint_stats", "窗口 30.0s: 检查点定时增量=47 请求式增量=2 "
     "(窗口请求式占比 4.1%), 写耗时增量=1ms",
     "checkpoint_pressure", False),
    ("checkpoint_stats", "窗口 30.0s: 检查点定时增量=47 请求式增量=352 "
     "(窗口请求式占比 88.2%), 写耗时增量=1ms",
     "checkpoint_pressure", True),
]
for ev, obs, rc, want in CASES:
    got = _supports(ev, obs, rc)
    check(f"{ev} -> {rc} 期望 {want}", got is want, obs[:46])

# 逐个确认没有漏网的"恒为真"
NEW_EV = ["autovacuum_health", "xid_age", "replication_slot_age",
          "prepared_xact_age", "disk_usage", "deadlock_count",
          "temp_file_volume", "checkpoint_stats"]
for ev in NEW_EV:
    owners = [c for c in ROOTS if ev in G.required_evidence(c)]
    if not owners:
        continue
    zero = {"autovacuum_health":
            "autovacuum_enabled=True running=False backlog=0.0",
            "xid_age": "占 freeze_max_age 0.0%",
            "replication_slot_age":
            "复制槽 0 个, 非活动=0, 非活动槽最大 horizon 年龄=0, "
            "非活动槽最大 WAL 滞留=0.0 MB",
            "prepared_xact_age":
            "预备事务 0 个, 最大 XID 年龄=0, 最长挂起=0s",
            "disk_usage": "磁盘使用率=0.0%",
            "deadlock_count": "窗口 30.0s: 死锁增量=0",
            "temp_file_volume": "窗口 30.0s: 外溢增量 0.0 MB",
            "checkpoint_stats": "窗口 30.0s: 窗口请求式占比 0.0%"}[ev]
    check(f"{ev} 空值不支持 {owners[0]}",
          _supports(ev, zero, owners[0]) is False)

print("\n[6] 手册出处要记在图上")
nodes_raw = yaml.safe_load(
    (Path(__file__).resolve().parent.parent /
     "knowledge/causal_graph/nodes.yaml").read_text(encoding="utf-8"))
sourced = sum(1 for grp in nodes_raw.values() if isinstance(grp, list)
              for n in grp if n.get("source", "").startswith("pgdoc:"))
check("新增节点带了 pgdoc 出处", sourced >= 15, f"{sourced} 个节点有出处")

print("\n[7] 新根因要能从症状够得到")
for sym, want in [("disk_growing", "stale_replication_slot"),
                  ("throughput_down", "deadlock"),
                  ("latency_p99_up", "work_mem_spill")]:
    names = [c["root_cause"] for c in G.candidate_causes([sym], top_k=12)]
    check(f"{sym} 能反查到 {want}", want in names, names[:6])

print("\n[8] 相关性不能伪装成根因级联")
check("table_bloat 不会凭空制造 missing_index",
      not g.has_edge("table_bloat", "missing_index", key="CAUSES"))
check("普通 lock_contention 不会升级成 deadlock",
      not g.has_edge("lock_contention", "deadlock", key="CAUSES"))
check("autovacuum 使用直接健康证据",
      G.required_evidence("autovacuum_starvation") == ["autovacuum_health"])
check("disk_pressure 使用真实文件系统水位",
      G.required_evidence("disk_pressure") == ["disk_usage"])
check("临时文件外溢不等于磁盘空间不足",
      "temp_file_volume" not in G.supporting_evidence("disk_pressure"))

print()
print("=" * 60)
print("GRAPH EXPAND: PASS" if not fails else f"GRAPH EXPAND: FAIL {fails}")
sys.exit(1 if fails else 0)
