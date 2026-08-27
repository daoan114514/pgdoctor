"""误导性告警场景的离线验收。

不验"能注入"（那要起库），验的是**这个陷阱在知识层与判分层真的设住了**：
表象指向 connection_exhaustion，真根因 long_idle_transaction 可达，
区分只靠一条证据，且顺着表象走会被判负。
"""
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.esc import _supports
from knowledge.causal_graph import graph as G
from sandbox.env import _load_injectors
from sandbox.scoring import _diagnosis_strict

fails = []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


SPEC = yaml.safe_load(
    (Path(__file__).resolve().parent.parent /
     "sandbox/scenarios/misleading_idle_txn_eval_v1.yaml"
     ).read_text(encoding="utf-8"))
TRUTH = "long_idle_transaction"
TRAP = "connection_exhaustion"

print("[1] 场景本身")
check("fault_class 是真根因而非表象", SPEC["fault_class"] == TRUTH)
check("显式标注了被伪装成什么", SPEC.get("misleading_as") == TRAP,
      SPEC.get("misleading_as"))
check("陷阱在竞争假设里", TRAP in SPEC["ground_truth"]["competing_hypotheses"])
check("注入器已注册", TRUTH in _load_injectors())

print("\n[2] 因果图：真根因要从表象症状够得到")
cands = G.candidate_causes(["conn_near_limit"], top_k=5)
names = [c["root_cause"] for c in cands]
check("表象根因排第一（陷阱确实诱人）", names and names[0] == TRAP, names)
hit = next((c for c in cands if c["root_cause"] == TRUTH), None)
check("真根因可达", hit is not None, f"候选={names}")
if hit:
    check("且不在第一跳（正是向量检索够不到的那种）", hit["hops"] >= 1,
          f"{hit['hops']} 跳: {hit['path']}")

print("\n[3] 区分只靠一条证据")
disc = G.best_discriminator([TRAP, TRUTH])
check("最优判别证据是 idle_in_transaction",
      disc and disc["evidence"] == "idle_in_transaction", disc)
check("由 get_connection_stats 取",
      disc and disc.get("obtained_by") == "get_connection_stats")
req = G.required_evidence(TRUTH)
check("该证据是真根因的必需项", "idle_in_transaction" in req, req)
check("场景声明的必需证据与图一致",
      set(SPEC["ground_truth"]["required_evidence"]) == set(req),
      f"场景={SPEC['ground_truth']['required_evidence']} 图={req}")

print("\n[4] ESC 的取值判据：拿到证据还不够，取值得对")
hi = "连接 95/100 (95%), 逼近上限=True, idle in transaction=17, 按角色={}"
lo = "连接 95/100 (95%), 逼近上限=True, idle in transaction=0, 按角色={}"
check("idle_in_transaction=17 支持真根因",
      _supports("idle_in_transaction", hi, TRUTH) is True)
check("idle_in_transaction=0 不支持真根因（这条原来恒为真）",
      _supports("idle_in_transaction", lo, TRUTH) is False)

print("\n[5] 判分：顺着表象走要被判负")
# 落进陷阱：确认了 connection_exhaustion，没碰真根因
trapped = {TRAP: {"verdict": "CONFIRMED"}}
ok, det = _diagnosis_strict(SPEC, TRAP, trapped)
check("误诊成表象 -> 严格诊断判负", ok is False,
      f"critical_ok={det['critical_ok']}")

# 只声称真根因但没排除陷阱：F1 不够
half = {TRUTH: {"verdict": "CONFIRMED"}}
ok, det = _diagnosis_strict(SPEC, TRUTH, half)
check("答对但没排除陷阱 -> 仍判负", ok is False,
      f"F1={det['f1']} < {det['threshold']}")

# 排除了陷阱才算真做了鉴别诊断
full = {TRUTH: {"verdict": "CONFIRMED"},
        TRAP: {"verdict": "REFUTED"},
        "lock_contention": {"verdict": "REFUTED"}}
ok, det = _diagnosis_strict(SPEC, TRUTH, full)
check("答对且排除了陷阱 -> 通过", ok is True, f"F1={det['f1']}")

print("\n[6] 正解与错解的修复方向不同")
fixes = [f["fix"] for f in G.fixes_for(TRUTH)]
check("真根因的修复是终止会话", "terminate_blocker" in fixes, fixes)

print()
print("=" * 60)
print("MISLEADING: PASS" if not fails else f"MISLEADING: FAIL {fails}")
sys.exit(1 if fails else 0)
