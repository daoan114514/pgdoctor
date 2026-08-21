"""覆盖修复的离线验证：新工具能否取到判别性证据、候选集是否覆盖到位。

不花模型额度 —— 直接调工具层与因果图。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg

from agent.investigator import toolset_for
from agent.state_machine import ALLOWED_TOOLS, Phase
from knowledge.causal_graph import graph as G
from sandbox import db
from sandbox.observe import Observer
from sandbox.traces import TraceStore

ok = True
print("=" * 74)
print("[1] 新工具在正常状态下的输出")
o = Observer(TraceStore("coverage_check"))
r = o.get_connection_stats()
print(f"  连接 {r['used']}/{r['max_connections']} ({r['pct']}%) "
      f"逼近上限={r['near_limit']}")
print(f"  按角色 {r['by_user']}")
c1 = r["max_connections"] > 0 and not r["near_limit"]
print(f"  {'PASS' if c1 else 'FAIL'}  正常态判定为未逼近上限")
ok &= c1

print("\n[2] 池子占满时能否识别")
held = []
for _ in range(200):
    try:
        held.append(psycopg.connect(db.dsn("app"), autocommit=True))
    except Exception:
        break
r2 = o.get_connection_stats()
print(f"  连接 {r2['used']}/{r2['max_connections']} ({r2['pct']}%) "
      f"逼近上限={r2['near_limit']}")
print(f"  按角色 {r2['by_user']}")
c2 = r2["near_limit"] and r2["by_user"].get("app_user", 0) > 50
print(f"  {'PASS' if c2 else 'FAIL'}  能识别出连接打满且指出占用者")
ok &= c2
for c in held:
    try:
        c.close()
    except Exception:
        pass
print(f"  已释放 {len(held)} 个连接")

print("\n[3] 工具集由因果图推导（加故障类型只改图）")
for h in ("connection_exhaustion", "lock_contention", "long_idle_transaction"):
    ts = toolset_for(h)
    req = G.required_evidence(h)
    covered = all(
        any(G.load().nodes.get(e, {}).get("obtained_by") == t for t in ts)
        for e in req)
    print(f"  {h:<24} 工具={ts}")
    print(f"  {'':<24} 必需证据={req} 覆盖={covered}")
    ok &= covered

print("\n[4] 新工具在状态机允许集内")
in_allow = "get_connection_stats" in ALLOWED_TOOLS[Phase.INVESTIGATE]
print(f"  {'PASS' if in_allow else 'FAIL'}  INVESTIGATE 允许 get_connection_stats")
ok &= in_allow

print("\n[5] 候选集覆盖：p99 上升时连接打满能否进候选")
cands = [c["root_cause"] for c in
         G.candidate_causes(["latency_p99_up", "cpu_saturated"], top_k=6)]
print(f"  候选根因: {cands}")
c5 = "connection_exhaustion" in cands
print(f"  {'PASS' if c5 else 'FAIL'}  connection_exhaustion 在候选内")
ok &= c5

print("\n" + "=" * 74)
print("COVERAGE FIX:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
