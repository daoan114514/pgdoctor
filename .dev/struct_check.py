"""结构提案的验收 —— 重点不是"能提案"，是"提案进不了生效路径"。"""
import sys
sys.path.insert(0, "/home/daoan/pgdoctor")

from knowledge import structure as S
from knowledge.causal_graph import graph as G

# 干净起步
for f in (S.CANDIDATES, S.PROMOTED):
    if f.exists():
        f.unlink()

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


class St:
    def __init__(self, rc, evidence):
        self.claimed_fault_class = rc
        self.scratchpad = [{"evidence_type": e, "observation": ""}
                           for e in evidence]


class Sc:
    def __init__(self, diag):
        self.diagnosis = diag


print(f"map_symptoms 探测: {G.map_symptoms(['p99 延迟上升', 'CPU 饱和'])}")
print()

# ══ 1. 误诊的 episode 一条提案都不该产生 ══════════════════════
print("[1] 防噪声：误诊不观察")
S.observe_episode(St("missing_index", ["dead_tuple_ratio"]),
                  Sc(False), ["disk_growing"])
check("误诊 episode 零提案", S.stats()["total"] == 0, S.stats())

# truth 与 claimed 不符时同样不观察
S.observe_episode(St("missing_index", ["dead_tuple_ratio"]),
                  Sc(True), ["disk_growing"], truth="lock_contention")
check("claimed≠truth 零提案", S.stats()["total"] == 0, S.stats())

# ══ 2. 诊断正确才累计 ════════════════════════════════════════
print("\n[2] 正确诊断才累计")
before = G.stats()["edges"]
for _ in range(3):
    S.observe_episode(
        St("missing_index", ["dead_tuple_ratio", "explain_seq_scan"]),
        Sc(True), ["disk_growing"])
st_now = S.stats()
check("产生了提案", st_now["total"] > 0, st_now)
check("累计到 ready", st_now["ready"] >= 1, f"ready={st_now['ready']}")

# ══ 3. 候选绝不影响生效图 ★核心不变式★ ═══════════════════════
print("\n[3] 候选不进生效路径（核心不变式）")
G.load.cache_clear()
check("边数未变", G.stats()["edges"] == before,
      f"{before} -> {G.stats()['edges']}")
check("required 未变",
      "dead_tuple_ratio" not in G.required_evidence("missing_index"),
      G.required_evidence("missing_index"))

# ══ 4. 门槛：support 不够不许 promote ════════════════════════
print("\n[4] promote 的门槛")
weak = S._pid("CAUSES", "missing_index", "queries_blocked")
S.observe_episode(St("missing_index", []), Sc(True), ["查询挂起不返回"])
ok, msg = S.promote(weak)
check("support 不足被拒", not ok, msg)

ok, msg = S.promote("CAUSES:not_a_thing->nope")
check("不存在的提案被拒", not ok, msg)

# ══ 5. promote 之后才生效 ═══════════════════════════════════
print("\n[5] 人 promote 之后才生效")
target = S._pid("CONFIRMED_BY", "missing_index", "dead_tuple_ratio")
ok, msg = S.promote(target, by="tester")
check("够格的提案可 promote", ok, msg)
G.load.cache_clear()
check("边数 +1", G.stats()["edges"] == before + 1,
      f"{before} -> {G.stats()['edges']}")
check("已进 supporting",
      "dead_tuple_ratio" in G.supporting_evidence("missing_index"),
      G.supporting_evidence("missing_index"))

# ══ 6. 硬禁：学来的边永远不能是 required ★最重要★ ════════════
print("\n[6] 硬禁：学习不能给自己降标准")
check("required 仍未增长",
      "dead_tuple_ratio" not in G.required_evidence("missing_index"),
      G.required_evidence("missing_index"))

# 手改 promoted 文件想塞一条 required，加载层必须降级
import yaml
d = yaml.safe_load(S.PROMOTED.read_text(encoding="utf-8"))
d["confirmed_by"][0]["necessity"] = "required"
d.setdefault("refuted_by", []).append(
    {"cause": "missing_index", "evidence": "explain_plan"})
S.PROMOTED.write_text(yaml.safe_dump(d, allow_unicode=True), encoding="utf-8")
G.load.cache_clear()
check("手改 required 被加载层降级",
      "dead_tuple_ratio" not in G.required_evidence("missing_index"),
      G.required_evidence("missing_index"))
n_ref = G.stats()["by_edge"].get("REFUTED_BY", 0)
check("手塞 REFUTED_BY 被忽略", n_ref == 5, f"REFUTED_BY={n_ref}")

# ══ 7. promote 幂等 / 驳回 ══════════════════════════════════
print("\n[7] 状态机")
ok, msg = S.promote(target)
check("不能重复 promote", not ok, msg)
ok, msg = S.reject(weak, "样本太少")
check("可驳回", ok, msg)
ok, msg = S.promote(weak)
check("驳回后不能再 promote", not ok, msg)

# teardown：测试写过 promoted_edges（还手改成非法内容测拦截），
# 留在工作区会污染下一次加载，也不该进 git。
for f in (S.CANDIDATES, S.PROMOTED):
    if f.exists():
        f.unlink()
G.load.cache_clear()

print()
print("=" * 60)
print("STRUCT: PASS" if not fails else f"STRUCT: FAIL {fails}")
sys.exit(1 if fails else 0)
