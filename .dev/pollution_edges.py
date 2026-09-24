#!/usr/bin/env python3
"""列出因果图推导出的全部污染边 A ⇝ B（CLAUDE.md 规则 2）：A 为真时，某条关于 B 的证据
（与 B 有 CONFIRMED_BY / REFUTED_BY 关系）的裁决不可信。只由证据的 provenance 查
provenance_rules 推出，不手写。另列每个根因的可反证证据及其是否干净（接地覆盖）。

用法：python3 .dev/pollution_edges.py [--json]
改图前后各跑一次、diff 输出，就能看出改动有没有引入新的污染边。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from knowledge.causal_graph import graph as G  # noqa: E402


def pollution() -> dict:
    g = G.load()
    roots = sorted(n for n, d in g.nodes(data=True) if d.get("kind") == "RootCause")
    edges: list[tuple[str, str, str, str]] = []
    grounding: dict[str, list[str]] = {}
    for b in roots:
        for _u, e, key, data in g.out_edges(b, keys=True, data=True):
            if key not in ("CONFIRMED_BY", "REFUTED_BY"):
                continue
            for a in sorted(G.invalidators_of(e) - {b}):
                edges.append((a, b, e, key))
        clean = []
        for item in G.refuting_evidence(b):
            e = item["evidence"]
            tag = "clean" if not (G.invalidators_of(e) - {b}) else "polluted"
            clean.append(f"{e}[{item.get('scope')}]:{tag}")
        grounding[b] = clean
    return {"edges": sorted(set(edges)), "grounding": grounding,
            "ungrounded": G.ungrounded_root_causes(),
            "causes_cause": sorted((u, v) for u, v, k in g.edges(keys=True)
                                   if k == "CAUSES" and u in roots and v in roots)}


if __name__ == "__main__":
    out = pollution()
    if "--json" in sys.argv:
        print(json.dumps(out, ensure_ascii=False, indent=1, default=list))
        raise SystemExit(0)
    print(f"污染边 {len(out['edges'])} 条（A ⇝ B via 证据, 关系）:")
    for a, b, e, key in out["edges"]:
        print(f"  {a} ⇝ {b}  via {e} ({key})")
    print("根因→根因因果边:", out["causes_cause"])
    print("接地缺口:", out["ungrounded"] or "无")
    print("各根因的可反证证据:")
    for b, items in out["grounding"].items():
        print(f"  {b}: {items}")
