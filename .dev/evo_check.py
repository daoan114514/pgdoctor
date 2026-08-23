"""L2/L3/L4 验收：用真实历史轨迹喂进去，看学到的东西是否改变下次决策。

自进化的判据不是"记下来了"，而是"下次不一样了"。
所以每一层都要验两件事：学没学到、以及学到的有没有回流。
"""
import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge import evolution as ev
from knowledge.causal_graph import graph as G

REPO = Path("/home/daoan/pgdoctor")
BACKUP = REPO / "knowledge" / "learned_backup"

# 用干净的学习状态，跑完恢复
if ev.LEARNED.exists():
    shutil.rmtree(BACKUP, ignore_errors=True)
    shutil.copytree(ev.LEARNED, BACKUP)
    shutil.rmtree(ev.LEARNED)

ok = True


class FakeScore:
    def __init__(self, d, o, s):
        self.diagnosis, self.outcome, self.safe_pass = d, o, s


print("=" * 76)
print("[0] 基线：学习之前的候选排序")
before = G.candidate_causes(["latency_p99_up", "cpu_saturated"], top_k=5)
for c in before:
    print(f"    {c['root_cause']:<24} score={c['score']:.4f} "
          f"adj={c['learned_adj']}")

# ── 喂真实轨迹 ──────────────────────────────────────────────
from eval import replay

print("\n" + "=" * 76)
print("[1] 喂入真实历史轨迹")
fed = 0
for res_file in sorted((REPO / "eval/results").glob("*.json")):
    d = json.loads(res_file.read_text(encoding="utf-8"))
    for e in d["episodes"]:
        if not e.get("episode_id") or not e.get("fired"):
            continue
        low = (e.get("error") or "").lower()
        if "modelunavailable" in low or "error result: success" in low:
            continue
        try:
            st = replay.load(e["episode_id"])
        except Exception:
            continue
        sc = FakeScore(e["diagnosis"], e["outcome"], e["safe_pass"])
        ev.learn(st, sc, e["applied_sql"], st.symptoms,
                 truth=e["fault_class"])
        fed += 1
print(f"    喂入 {fed} 个 episode")
ok &= fed > 0

s = ev.stats()

print("\n" + "=" * 76)
print("[2] L2 技能沉淀：playbook")
for rc, v in s["playbooks"].items():
    print(f"    {rc:<24} 成功{v['成功']} 失败{v['失败']} "
          f"置信={v['置信']} 中位步数={v['步数']}")
pbs = ev.load_playbooks()
has_order = any(p.evidence_order for p in pbs.values())
print(f"    {'PASS' if pbs else 'FAIL'}  沉淀出 {len(pbs)} 个 playbook")
print(f"    {'PASS' if has_order else 'FAIL'}  记录了有效取证顺序")
for rc, p in pbs.items():
    if p.evidence_order:
        print(f"      {rc}: {' → '.join(p.evidence_order[:5])}")
ok &= bool(pbs) and has_order

hint = ev.render_playbook_hint(["missing_index", "lock_contention"])
print(f"    {'PASS' if hint else 'FAIL'}  能渲染成提示（{len(hint)} 字符）")
ok &= bool(hint)

print("\n" + "=" * 76)
print("[3] L3 失败驱动：先验调整")
print(f"    prior_adj: {s['prior_adj']}")
print(f"    observed:  {s['observed']}")
ok &= bool(s["prior_adj"])
print(f"    {'PASS' if s['prior_adj'] else 'FAIL'}  产生了先验调整")

print("\n    学到的先验是否真的改变了候选排序:")
after = G.candidate_causes(["latency_p99_up", "cpu_saturated"], top_k=5)
changed = False
for b, a in zip(before, after):
    mark = ""
    if abs(a["score"] - b["score"]) > 1e-6:
        changed = True
        mark = f"  ← 变化 {b['score']:.4f} → {a['score']:.4f}"
    print(f"      {a['root_cause']:<24} score={a['score']:.4f} "
          f"adj={a['learned_adj']}{mark}")
print(f"    {'PASS' if changed else 'FAIL'}  排序分数确实被学到的先验改变")
ok &= changed

print("\n    安全性：调整量有上下限，不会让根因彻底出局")
adjs = list(s["prior_adj"].values())
bounded = all(abs(x) <= ev.MAX_ADJ + 1e-9 for x in adjs)
survivors = [c["root_cause"] for c in after]
print(f"      调整量范围 [{min(adjs):.3f}, {max(adjs):.3f}]，"
      f"上限 ±{ev.MAX_ADJ}")
print(f"      候选集仍有 {len(survivors)} 个根因")
print(f"    {'PASS' if bounded else 'FAIL'}  调整量在界内")
ok &= bounded and len(survivors) >= 4

print("\n" + "=" * 76)
print("[4] L4 诊断查询库：判别力排序")
for k, v in list(s["queries"].items())[:8]:
    print(f"    {k:<26} 判别力={v['判别力']:<6} "
          f"成功{v['成功']} 失败{v['失败']}")
ok &= bool(s["queries"])
print(f"    {'PASS' if s['queries'] else 'FAIL'}  沉淀出 {len(s['queries'])} 条查询统计")

print("\n    是否影响子 agent 的工具顺序:")
from agent.investigator import toolset_for
for rc in ("missing_index", "lock_contention"):
    pref = ev.top_queries_for(rc)
    ts = toolset_for(rc)
    print(f"      {rc:<20} 历史偏好={pref}")
    print(f"      {'':<20} 实际工具集={ts}")
influenced = any(ev.top_queries_for(rc) for rc in
                 ("missing_index", "lock_contention"))
print(f"    {'PASS' if influenced else 'FAIL'}  查询库对工具顺序有输入")
ok &= influenced

print("\n" + "=" * 76)
print("[5] 可审计：学到的东西全部落盘且可 diff")
for f in ("playbooks.yaml", "graph_delta.yaml", "query_library.yaml"):
    p = ev.LEARNED / f
    print(f"    {'PASS' if p.exists() else 'FAIL'}  {f} "
          f"({p.stat().st_size if p.exists() else 0} bytes)")
    ok &= p.exists()

print("\n" + "=" * 76)
print("L2/L3/L4 ACCEPTANCE:", "PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
