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

from agent.toolbox import Toolbox
from knowledge.causal_graph import graph as G
from knowledge.evidence_predicates import registered_predicates
from safety import gate, shield
from safety.gate import RemediationProposal

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
cause_self_loops = []
for section in ("causes_symptom", "causes_cause"):
    for e in edges_raw.get(section, []) or []:
        cg.add_edge(e["from"], e["to"])
        if e["from"] == e["to"]:
            cause_self_loops.append(f"{e['from']}->{e['to']}")
cycles = list(nx.simple_cycles(cg))
check("根因级联无环", not cycles, cycles)
check("CAUSES 无自环", not cause_self_loops, cause_self_loops)
loaded_edge_ids = [d.get("edge_id") for u, v, k, d in
                   g.edges(keys=True, data=True) if k == "CAUSES"]
check("CAUSES 运行时边 ID 稳定且唯一",
      all(loaded_edge_ids) and len(loaded_edge_ids) == len(set(loaded_edge_ids)),
      [edge_id for edge_id in loaded_edge_ids if not edge_id][:6])

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
methods = {m for m in dir(Toolbox) if not m.startswith("_")}
bad = []
missing_predicates = []
known_predicates = registered_predicates()
for c in [n for n, k in KIND.items() if k == "RootCause"]:
    for ev in G.required_evidence(c):
        by = g.nodes[ev].get("obtained_by")
        if by not in methods:
            bad.append(f"{c} 需要 {ev}，但 {by} 不是工具")
        if not g.nodes[ev].get("predicate_id"):
            missing_predicates.append(f"{c} 需要 {ev}，但没有 predicate_id")
        elif g.nodes[ev].get("predicate_id") not in known_predicates:
            missing_predicates.append(
                f"{c} 需要 {ev}，但 predicate 未注册: "
                f"{g.nodes[ev].get('predicate_id')}")
check("required 证据都有工具", not bad, bad[:6])
check("required 证据都有 predicate", not missing_predicates,
      missing_predicates[:6])

bad_refuters = []
for edge in edges_raw.get("refuted_by", []) or []:
    predicate_id = edge.get("predicate_id", "")
    evidence_predicate = g.nodes[edge["evidence"]].get("predicate_id", "")
    if predicate_id not in known_predicates:
        bad_refuters.append(f"{edge['cause']}<-{edge['evidence']} predicate 未注册")
    if predicate_id != evidence_predicate:
        bad_refuters.append(
            f"{edge['cause']}<-{edge['evidence']} predicate 与证据节点不一致")
    if edge.get("scope") not in {"NODE", "PATH", "INTERVENTION"}:
        bad_refuters.append(f"{edge['cause']}<-{edge['evidence']} scope 非法")
    if edge.get("scope") == "INTERVENTION" and not edge.get("target_fix"):
        bad_refuters.append(
            f"{edge['cause']}<-{edge['evidence']} 缺 target_fix")
for evidence in ("deadlock_count", "temp_file_volume", "checkpoint_stats"):
    matches = [edge for edge in edges_raw.get("refuted_by", []) or []
               if edge.get("evidence") == evidence]
    if matches and not all(edge.get("window_required") for edge in matches):
        bad_refuters.append(f"{evidence} 累计反证未要求事故窗口")
check("REFUTED_BY predicate/scope 完整", not bad_refuters, bad_refuters[:6])

# ══ 7. 修复动作的合法性 ═══════════════════════════════════
print("\n[7] 修复节点要能过护盾与门")
bad, irrev, escalate = [], [], []
owners = {}
for edge in edges_raw.get("fixed_by", []) or []:
    owners.setdefault(edge["fix"], []).append(edge["cause"])
bad_tiers = [f"{f['id']}={f.get('risk_tier')}"
             for f in nodes_raw.get("fixes", []) or []
             if f.get("risk_tier") not in {"AUTO", "CONFIRM", "DENY"}]
check("修复风险档位合法", not bad_tiers, bad_tiers)
bad_contract = []
for fix in nodes_raw.get("fixes", []) or []:
    missing = [name for name in ("action_type", "risk_tier", "rollback",
                                  "intervention_kind", "execution",
                                  "preconditions", "expected_effect_nodes")
               if not fix.get(name)]
    if missing:
        bad_contract.append(f"{fix['id']} 缺 {missing}")
    if fix.get("intervention_kind") not in {
            "CORRECTIVE", "MITIGATION", "CONTAINMENT", "MANUAL"}:
        bad_contract.append(
            f"{fix['id']} intervention_kind={fix.get('intervention_kind')}")
    if not fix.get("manual") and not fix.get("expected_effects"):
        bad_contract.append(f"{fix['id']} 没有 expected_effects/manual")
    if fix.get("intervention_kind") == "MANUAL" and not fix.get("manual"):
        bad_contract.append(f"{fix['id']} MANUAL 未显式声明 manual")
    if (fix.get("intervention_kind") == "MANUAL" and
            fix.get("execution") != "escalate_only"):
        bad_contract.append(f"{fix['id']} MANUAL 未设置 escalate_only")
    if fix.get("execution") not in {"gated", "escalate_only"}:
        bad_contract.append(
            f"{fix['id']} execution={fix.get('execution')} 非法")
    unknown_effects = set(fix.get("expected_effect_nodes", [])) - set(ids)
    if unknown_effects:
        bad_contract.append(f"{fix['id']} expected_effect_nodes 不存在: "
                            f"{sorted(unknown_effects)}")
    non_causal_effects = [node_id for node_id in
                          fix.get("expected_effect_nodes", [])
                          if KIND.get(node_id) not in {"RootCause", "Symptom"}]
    if non_causal_effects:
        bad_contract.append(
            f"{fix['id']} expected_effect_nodes 不是因果节点: "
            f"{non_causal_effects}")
    for condition in fix.get("preconditions", []):
        predicate_id = condition.get("predicate_id")
        if predicate_id and predicate_id not in known_predicates:
            bad_contract.append(
                f"{fix['id']} precondition predicate 未注册: {predicate_id}")
        if predicate_id and condition.get("result") not in {
                "SUPPORTS", "REFUTES", "NEUTRAL", "NOT_APPLICABLE"}:
            bad_contract.append(
                f"{fix['id']} precondition result 非法: {condition.get('result')}")
        if predicate_id and condition.get("target_kind") not in {
                "NODE", "PATH", "INTERVENTION"}:
            bad_contract.append(
                f"{fix['id']} precondition target_kind 非法: "
                f"{condition.get('target_kind')}")
check("live fix 的因果干预契约完整", not bad_contract, bad_contract[:6])


def render(template):
    return (template.replace("{table}", "orders").replace("{pid}", "123")
            .replace("{cols}", "a").replace("{name}", "i")
            .replace("{value}", "64MB").replace("{slot}", "s")
            .replace("{gid}", "g").replace("{mount}", "/pgdata"))


for f in nodes_raw.get("fixes", []) or []:
    tpl = f.get("template", "")
    probe = render(tpl)
    try:
        actual = shield.classify(probe)
        allowed = shield.inspect_sql(probe).allowed
    except Exception as exc:
        bad.append(f"{f['id']}: 解析失败 {str(exc)[:50]}")
        continue
    if f.get("execution") == "escalate_only":
        # SQL 语法合法（如 pg_drop_replication_slot）不等于策略允许执行。
        # escalate_only 的权威执行者是 GATE，不能依赖护盾碰巧不认识语句。
        escalate.append(f["id"])
        cause = (owners.get(f["id"]) or [""])[0]
        decision = gate.assess(RemediationProposal(
            action_type=f.get("action_type", ""), sql=probe,
            rollback=render(f.get("rollback", "")), root_cause=cause,
            fix_id=f["id"], esc_verdict="SUFFICIENT",
            evidence_refs=["trace://graph_lint/step_001"]))
        if decision.approved:
            bad.append(f"{f['id']}: 标了 escalate_only，GATE 却放行")
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
    cause = (owners.get(f["id"]) or [""])[0]
    decision = gate.assess(RemediationProposal(
        action_type=f.get("action_type", ""), sql=probe, rollback=render(rb),
        root_cause=cause, fix_id=f["id"], esc_verdict="SUFFICIENT",
        evidence_refs=["trace://graph_lint/step_001"]))
    if not decision.approved:
        bad.append(f"{f['id']}: 可执行修复被 GATE 拒绝 {decision.reasons[:1]}")
check("action_type 与 AST 分类一致", not bad, bad[:6])
print(f"        不可逆修复（需人工担责）: {irrev}")
print(f"        只能升级人工（GATE 硬拒，已验证）: {escalate}")

p0_causes = [n["id"] for n in nodes_raw.get("root_causes", []) or []
             if n.get("severity") == "P0"]
p0_bad = []
for cause in p0_causes:
    if not G.required_evidence(cause):
        p0_bad.append(f"{cause} 没有 required evidence")
    for fix in G.fixes_for(cause):
        if fix.get("risk_tier") == "AUTO":
            p0_bad.append(f"{cause}->{fix['fix']} 是 AUTO")
check("P0 都有必需证据且修复不为 AUTO", not p0_bad, p0_bad)

try:
    from knowledge.structure import load_promoted
    promoted = load_promoted()
except Exception:
    promoted = {}
bad_promoted = [
    f"{section}:{entry}"
    for section, entries in (promoted or {}).items()
    for entry in (entries or [])
    if entry.get("status") not in (None, "approved", "promoted")
]
probe_ready = G._live_promoted({
    "causes_cause": [{"from": "missing_index", "to": "table_bloat",
                       "status": "ready_for_review"}],
    "confirmed_by": [{"cause": "missing_index", "evidence": "x",
                       "status": "proposed"}],
})
check("promoted overlay 只加载 approved/promoted",
      not bad_promoted and not any(probe_ready.values()), bad_promoted[:3])

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
