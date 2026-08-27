"""因果图体检。

扩图之后最怕的不是漏，是**加错**：一条方向反了或类型串了的边不会报错，
只会安静地把所有相关诊断带偏，而且很难追溯。所以这里查的是结构层面
能机械判定的东西，不依赖任何人的判断。
"""
import sys
from collections import Counter
from pathlib import Path

import networkx as nx
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge.causal_graph import graph as G

HERE = Path(__file__).resolve().parent.parent / "knowledge/causal_graph"
fails, warns = [], []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


def warn(name, cond, detail=""):
    if not cond:
        print(f"  WARN  {name}   {detail}")
        warns.append(name)
    else:
        print(f"  PASS  {name}   {detail}")


g = G.load()
nodes_raw = yaml.safe_load((HERE / "nodes.yaml").read_text(encoding="utf-8"))
edges_raw = yaml.safe_load((HERE / "edges.yaml").read_text(encoding="utf-8"))
KIND = {n: d.get("kind") for n, d in g.nodes(data=True)}

print(f"图: {G.stats()}\n")

# ══ 1. 悬空引用 ═══════════════════════════════════════════
print("[1] 边的两端都必须是已声明的节点")
declared = set()
for grp in nodes_raw.values():
    if isinstance(grp, list):
        declared |= {n["id"] for n in grp}
dangling = []
for key, spec in (("causes_symptom", ("from", "to")),
                  ("causes_cause", ("from", "to")),
                  ("confirmed_by", ("cause", "evidence")),
                  ("refuted_by", ("cause", "evidence")),
                  ("fixed_by", ("cause", "fix"))):
    for e in edges_raw.get(key, []) or []:
        for f in spec:
            if e.get(f) not in declared:
                dangling.append(f"{key}.{f}={e.get(f)}")
for e in edges_raw.get("discriminates", []) or []:
    if e["evidence"] not in declared:
        dangling.append(f"discriminates.evidence={e['evidence']}")
    for c in e.get("separates", []):
        if c not in declared:
            dangling.append(f"discriminates.separates={c}")
check("无悬空引用", not dangling, dangling[:6])

# ══ 2. 边的类型必须对 ═════════════════════════════════════
print("\n[2] 每种边只能连特定类型的节点")
bad = []
for e in edges_raw.get("causes_symptom", []) or []:
    if KIND.get(e["from"]) != "RootCause" or KIND.get(e["to"]) != "Symptom":
        bad.append(f"causes_symptom {e['from']}->{e['to']}")
for e in edges_raw.get("causes_cause", []) or []:
    if KIND.get(e["from"]) != "RootCause" or KIND.get(e["to"]) != "RootCause":
        bad.append(f"causes_cause {e['from']}->{e['to']}")
for e in edges_raw.get("confirmed_by", []) or []:
    if KIND.get(e["cause"]) != "RootCause" or KIND.get(e["evidence"]) != "Evidence":
        bad.append(f"confirmed_by {e['cause']}->{e['evidence']}")
for e in edges_raw.get("refuted_by", []) or []:
    if KIND.get(e["cause"]) != "RootCause" or KIND.get(e["evidence"]) != "Evidence":
        bad.append(f"refuted_by {e['cause']}->{e['evidence']}")
for e in edges_raw.get("fixed_by", []) or []:
    if KIND.get(e["cause"]) != "RootCause" or KIND.get(e["fix"]) != "Fix":
        bad.append(f"fixed_by {e['cause']}->{e['fix']}")
for e in edges_raw.get("discriminates", []) or []:
    if KIND.get(e["evidence"]) != "Evidence":
        bad.append(f"discriminates evidence={e['evidence']}")
    for c in e.get("separates", []):
        if KIND.get(c) != "RootCause":
            bad.append(f"discriminates separates={c}")
check("边的端点类型正确", not bad, bad[:6])

# ══ 3. CAUSES 不能成环 ════════════════════════════════════
print("\n[3] 根因级联不能成环")
cg = nx.DiGraph()
for e in edges_raw.get("causes_cause", []) or []:
    cg.add_edge(e["from"], e["to"])
cycles = list(nx.simple_cycles(cg))
check("根因级联无环", not cycles, cycles)

# ══ 4. 概率值合法 ═════════════════════════════════════════
print("\n[4] 权重取值")
bad = [f"{e.get('from')}->{e.get('to')}={e.get('likelihood')}"
       for key in ("causes_symptom", "causes_cause")
       for e in edges_raw.get(key, []) or []
       if not (0.0 < float(e.get("likelihood", 0.5)) <= 1.0)]
check("likelihood 落在 (0,1]", not bad, bad[:6])
bad = [f"{e['evidence']}={e.get('power')}"
       for e in edges_raw.get("discriminates", []) or []
       if not (0.0 < float(e.get("power", 0.5)) <= 1.0)]
check("power 落在 (0,1]", not bad, bad[:6])
bad = [f"{n['id']}={n.get('prior')}" for n in nodes_raw.get("root_causes", [])
       if not (0.0 < float(n.get("prior", 0.1)) <= 1.0)]
check("prior 落在 (0,1]", not bad, bad[:6])

# ══ 5. 重复边 ═════════════════════════════════════════════
print("\n[5] 重复定义")
dups = []
for key, spec in (("causes_symptom", ("from", "to")),
                  ("causes_cause", ("from", "to")),
                  ("confirmed_by", ("cause", "evidence")),
                  ("refuted_by", ("cause", "evidence")),
                  ("fixed_by", ("cause", "fix"))):
    c = Counter(tuple(e[f] for f in spec) for e in edges_raw.get(key, []) or [])
    dups += [f"{key} {k}×{v}" for k, v in c.items() if v > 1]
ids = [n["id"] for grp in nodes_raw.values() if isinstance(grp, list)
       for n in grp]
dup_nodes = [k for k, v in Counter(ids).items() if v > 1]
check("无重复边", not dups, dups[:6])
check("无重复节点 id", not dup_nodes, dup_nodes)

# ══ 6. 必需证据的可得性 ═══════════════════════════════════
print("\n[6] required 证据必须真能取到")
from agent.toolbox import Toolbox
methods = {m for m in dir(Toolbox) if not m.startswith("_")}
bad = []
for c in [n for n, k in KIND.items() if k == "RootCause"]:
    for ev in G.required_evidence(c):
        by = g.nodes[ev].get("obtained_by")
        if by not in methods:
            bad.append(f"{c} 需要 {ev}，但 {by} 不是工具")
check("required 证据都有工具", not bad, bad[:6])

# ══ 7. 修复动作的合法性 ═══════════════════════════════════
print("\n[7] 修复节点要能过护盾与门")
from safety import shield
bad, irrev, escalate = [], [], []
for f in nodes_raw.get("fixes", []) or []:
    tpl = f.get("template", "")
    probe = (tpl.replace("{table}", "orders").replace("{pid}", "123")
                .replace("{cols}", "a").replace("{name}", "i")
                .replace("{value}", "64MB").replace("{slot}", "s")
                .replace("{gid}", "g"))
    try:
        actual = shield.classify(probe)
        allowed = shield.inspect_sql(probe).allowed
    except Exception as exc:
        bad.append(f"{f['id']}: 解析失败 {str(exc)[:50]}")
        continue
    if f.get("execution") == "escalate_only":
        # 标了只能升级人工的，反过来断言护盾**确实**拦得住 ——
        # 否则这个标记就只是一句注释，agent 照样能提交。
        escalate.append(f["id"])
        if allowed:
            bad.append(f"{f['id']}: 标了 escalate_only，护盾却放行")
        continue
    if not allowed:
        bad.append(f"{f['id']}: 护盾拦下但没标 escalate_only")
    if actual != f.get("action_type"):
        bad.append(f"{f['id']}: 声明 {f.get('action_type')} 实为 {actual}")
    rb = f.get("rollback", "")
    if not rb:
        bad.append(f"{f['id']}: 没有回滚")
    if rb == "IRREVERSIBLE":
        irrev.append(f["id"])
check("action_type 与 AST 分类一致", not bad, bad[:6])
print(f"        不可逆修复（需人工担责）: {irrev}")
print(f"        只能升级人工（护盾硬拦，已验证）: {escalate}")

# ══ 8. 语义体检（只报警告，交人判断）═══════════════════════
print("\n[8] 语义体检")
both = []
for e in edges_raw.get("confirmed_by", []) or []:
    for r in edges_raw.get("refuted_by", []) or []:
        if e["cause"] == r["cause"] and e["evidence"] == r["evidence"]:
            both.append(f"{e['cause']}<-{e['evidence']}")
print(f"        同一证据既确认又反证（取值决定方向，合法）: {len(both)} 对")

no_sym = [c for c, k in KIND.items() if k == "RootCause"
          and not G.symptoms_of(c)]
warn("每个根因都至少解释一个症状", not no_sym,
     f"只经级联现身、自身不直接产生症状: {no_sym}" if no_sym else "")

unreach = []
symptoms = [n for n, k in KIND.items() if k == "Symptom"]
reach = set()
for s in symptoms:
    reach |= {c["root_cause"] for c in G.candidate_causes([s], top_k=99)}
unreach = [c for c, k in KIND.items() if k == "RootCause" and c not in reach]
check("每个根因都能从某个症状反查到", not unreach, unreach)

print()
print("=" * 62)
if fails:
    print(f"GRAPH LINT: FAIL {fails}")
elif warns:
    print(f"GRAPH LINT: PASS（{len(warns)} 条警告待人工判断）")
else:
    print("GRAPH LINT: PASS")
sys.exit(1 if fails else 0)
