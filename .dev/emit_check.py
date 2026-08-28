"""图上声明的证据类型，有没有工具真的产出。

graph_lint 里已有一条"required 证据都有工具"，但它查的是 obtained_by
是不是 Toolbox 的方法名 —— 方法存在，不等于那个方法真的产出这条证据。

实测漏网：table_bloat 的必需证据是 dead_tuple_ratio，图上写着由
get_table_stats 取。方法确实存在，lint 因此 PASS；可 get_table_stats
只产出 stats_freshness，dead_tuple_ratio 从来没有任何工具产出过。
也就是说 table_bloat 这个根因**结构上无法被诊断**，D1 永远失败。

这是同一类错的又一次：一个名字在 A 处存在，就假定它在 B 处也存在。
"""
import ast
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from knowledge.causal_graph import graph as G


def emitted_types() -> dict[str, set[str]]:
    """从 toolbox.py 里静态抽出「哪个工具产出哪些 evidence_type」。

    静态抽而不是跑一遍：跑一遍要起库，而且只能覆盖到实际走到的分支。
    """
    src = (ROOT / "agent/toolbox.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef):
            continue
        types = set()
        for sub in ast.walk(node):
            if not isinstance(sub, ast.Call):
                continue
            fn = sub.func
            name = getattr(fn, "attr", None) or getattr(fn, "id", None)
            if name != "_evidence" or not sub.args:
                continue
            a0 = sub.args[0]
            if isinstance(a0, ast.Constant) and isinstance(a0.value, str):
                types.add(a0.value)
            elif isinstance(a0, ast.Name):
                # kind = "explain_seq_scan" if ... else "explain_plan"
                for s2 in ast.walk(node):
                    if (isinstance(s2, ast.Assign)
                            and any(getattr(t, "id", None) == a0.id
                                    for t in s2.targets)):
                        for c in ast.walk(s2.value):
                            if isinstance(c, ast.Constant) and isinstance(
                                    c.value, str):
                                types.add(c.value)
        if types:
            out[node.name] = types
    return out


fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


emit = emitted_types()
all_emitted = set().union(*emit.values()) if emit else set()

print("工具实际产出的证据类型:")
for tool in sorted(emit):
    print(f"  {tool:<24} {sorted(emit[tool])}")

g = G.load()
declared = {n for n, d in g.nodes(data=True) if d.get("kind") == "Evidence"}

print("\n[1] 图上声明的证据，必须真有工具产出")
orphan = sorted(declared - all_emitted)
check("没有无人产出的证据节点", not orphan, orphan)

print("\n[2] obtained_by 必须指向真的产出它的那个工具")
wrong = []
for ev in sorted(declared & all_emitted):
    by = g.nodes[ev].get("obtained_by")
    if by and ev not in emit.get(by, set()):
        actual = [t for t, s in emit.items() if ev in s]
        wrong.append(f"{ev}: 图说 {by}，实际由 {actual}")
check("obtained_by 与实际产出一致", not wrong, wrong)

print("\n[3] 每个根因的必需证据都取得到")
bad = []
for rc in [n for n, d in g.nodes(data=True) if d.get("kind") == "RootCause"]:
    miss = [e for e in G.required_evidence(rc) if e not in all_emitted]
    if miss:
        bad.append(f"{rc} 需要 {miss}，但没有工具产出")
check("没有结构上无法诊断的根因", not bad, bad)

print("\n[4] 工具产出的证据，图上都得认识")
unknown = sorted(all_emitted - declared)
print(f"      工具产出但图上没有节点的: {unknown or '（无）'}")
print("      （这类不算错：过程性标注不必进图，但值得知道有哪些）")

print()
print("=" * 66)
print("EMIT CHECK: PASS" if not fails else f"EMIT CHECK: FAIL {fails}")
sys.exit(1 if fails else 0)
