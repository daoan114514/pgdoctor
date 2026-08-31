"""拿真实轨迹重新喂一遍学习管线，验证 L3 的修复是否真的起作用。

改了代码不等于有用。修复的主张是"正确诊断也写边权，而边权能推动排序"，
那就得拿真实数据喂一遍，看 likelihood_adj 是否真的长出来、候选排序是否
真的改善。

用 392 条真实轨迹（不是受控脚本跑批的产物）—— 它们的症状与诊断结论是
真实 episode 留下的。
"""
import re
import shutil
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

ROOT = Path(__file__).resolve().parent.parent
SCEN = ROOT / "sandbox/scenarios"
LEARNED = ROOT / "knowledge/learned"

# ── 备份现有学习状态 ───────────────────────────────────────
bak = LEARNED.parent / "learned_backup"
if bak.exists():
    shutil.rmtree(bak)
shutil.copytree(LEARNED, bak)
print(f"现有学习状态已备份到 {bak.name}/")

for f in ("graph_delta.yaml", "playbooks.yaml", "query_library.yaml"):
    t = LEARNED / f
    if t.exists():
        t.unlink()
print("学习状态已清空，从零重学\n")

from eval import replay                                    # noqa: E402
from knowledge import evolution as ev                      # noqa: E402
from knowledge.causal_graph import graph as G              # noqa: E402


def truth_of(eid):
    m = re.match(r"^ep_(.+)_\d{6,}$", eid)
    if not m:
        return None
    f = SCEN / f"{m.group(1)}.yaml"
    if not f.exists():
        return None
    return yaml.safe_load(f.read_text(encoding="utf-8"))["fault_class"]


class Score:
    def __init__(self, diag):
        self.diagnosis = diag
        self.outcome = False
        self.safe_pass = False


n = hit = 0
for d in sorted((ROOT / "traces").iterdir()):
    if not d.is_dir() or not d.name.startswith("ep_"):
        continue
    truth = truth_of(d.name)
    if not truth:
        continue
    try:
        st = replay.load(d.name)
    except Exception:
        continue
    if not st.claimed_fault_class or not st.symptoms:
        continue
    correct = (st.claimed_fault_class == truth)
    hit += correct
    n += 1
    try:
        ev.learn(st, Score(correct), [], st.symptoms, truth=truth)
    except Exception as exc:
        print(f"  跳过 {d.name[:40]}: {type(exc).__name__}: {exc}")

print(f"喂入 {n} 条轨迹（诊断正确 {hit}，误诊 {n - hit}）\n")

delta = ev.load_delta()
print(f"prior_adj      {len(delta.prior_adj)} 条: {delta.prior_adj}")
print(f"likelihood_adj {len(delta.likelihood_adj)} 条")
for k, v in sorted(delta.likelihood_adj.items(), key=lambda x: -abs(x[1]))[:10]:
    print(f"    {k:<44} {v:+.3f}")
