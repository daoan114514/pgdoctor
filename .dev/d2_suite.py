"""压出 D2 响应曲线的跑批：10 个场景 × 2 个目标 × 5 个鉴别深度 = 100 例。

设计上是**被试内**而不是被试间：每个场景注入一次真实故障（约 105s），
然后在同一个故障态上跑多个策略变体（每个约 2s）。这样做不是为了省时间
（虽然确实从 3 小时压到 15 分钟），是因为它才是对的实验设计 —— 要量的
自变量是"鉴别诊断做到什么程度"，环境必须固定住。

代价要说清楚：同一场景内的样本共享同一个数据库状态，所以这 100 例
**不是 100 次独立事故**，而是 10 个真实故障态 × 10 种策略行为。用来量
ESC 对鉴别深度的响应是充分的；用来估"真实世界误诊率"则不行。

证据与 KPI 全部来自真实注入的故障，只有策略是受控的。零模型调用。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import yaml

from agent import esc
from agent.depth_policy import DifferentialDepthPolicy
from agent.loop import run_episode
from sandbox.env import DBAScenarioEnv

ROOT = Path(__file__).resolve().parent.parent
# 第几轮。同一批场景换种子重跑，注入参数与排除对象都会变 ——
# 这是"每轮不一样"的来源，也是防背答案那条铁律要求的。
RUN_SEED = int(sys.argv[1]) if len(sys.argv) > 1 else 0
OUT = ROOT / (f"eval/results/d2_suite_r{RUN_SEED}.json" if RUN_SEED
              else "eval/results/d2_suite.json")

# 每个场景配一个"陷阱"：最容易被误认成的那个根因。
# 这是这批数据的核心 —— 只有落进陷阱的样本才可能出现"D1 过但结论错"。
TRAPS = {
    "missing_index": "stale_statistics",
    "stale_statistics": "missing_index",
    "lock_contention": "missing_index",
    "connection_exhaustion": "lock_contention",
    "long_idle_transaction": "connection_exhaustion",
}
DEPTHS = [0, 1, 2, 3, None]          # None = 全排除

rows = []
t_start = time.time()

scenarios = sorted(ROOT.glob("sandbox/scenarios/*.yaml"))
print(f"第 {RUN_SEED} 轮 —— 场景 {len(scenarios)} 个 × 目标 2 × "
      f"深度 {len(DEPTHS)} = {len(scenarios) * 2 * len(DEPTHS)} 例\n")

for i, sf in enumerate(scenarios, 1):
    spec = yaml.safe_load(sf.read_text(encoding="utf-8"))
    truth = spec["fault_class"]
    trap = TRAPS.get(truth, "missing_index")
    t0 = time.time()
    print(f"[{i}/{len(scenarios)}] {spec['id']}  真根因={truth} 陷阱={trap}")
    try:
        with DBAScenarioEnv(str(sf), warmup_s=8.0, degrade_timeout_s=75.0,
                            quiet=True) as env:
            obs = env.reset(seed=RUN_SEED)
            print(f"        注入完成 {time.time() - t0:.0f}s  告警={obs.fired}")
            for target, kind in ((truth, "correct"), (trap, "trapped")):
                for k in DEPTHS:
                    pol = DifferentialDepthPolicy(
                        target, refute_k=k,
                        seed=RUN_SEED * 1000 + i * 10 + (0 if kind == "correct"
                                                         else 1))
                    try:
                        res, st = run_episode(
                            env, obs, pol, max_steps=30, allow_repair=False,
                            quiet=True, use_cases=False, use_esc=False)
                    except Exception as exc:
                        rows.append({"scenario": spec["id"], "truth": truth,
                                     "target": target, "kind": kind,
                                     "depth": k,
                                     "error": f"{type(exc).__name__}: {exc}"[:160]})
                        continue
                    rep = esc.check(st)
                    dims = {d.name: d.passed for d in rep.dims}
                    # esc.check 在根因未声明时提前返回，dims 是空的。
                    # 用 next(...) 取 D2 会抛 StopIteration，而它的 str()
                    # 恰好是空字符串 —— 上一版 6 个场景就是这么失败得
                    # 毫无线索的。
                    d2 = next((d for d in rep.dims if d.name == "D2"), None)
                    rows.append({
                        "run": RUN_SEED,
                        "scenario": spec["id"], "truth": truth,
                        "target": target, "kind": kind, "depth": k,
                        "claimed": st.claimed_fault_class,
                        "correct": st.claimed_fault_class == truth,
                        "verdict": rep.verdict,
                        "passed": rep.verdict == esc.ESCVerdict.SUFFICIENT.value,
                        "dims": dims,
                        "d2_detail": d2.detail if d2 else "",
                        # 声明被证据门拒绝：这不是错误，是门在正常工作 ——
                        # 这一格连 D1 都没到就被挡住了，本身就是结果
                        "declare_blocked": not st.claimed_fault_class,
                        "n_evidence": len({e["evidence_type"]
                                           for e in st.scratchpad
                                           if e.get("status", "OBSERVED")
                                           == "OBSERVED"}),
                    })
            print(f"        跑完 10 个变体，累计 {time.time() - t0:.0f}s")
    except Exception as exc:
        print(f"        场景失败: {type(exc).__name__}: {exc}"[:160])

OUT.parent.mkdir(parents=True, exist_ok=True)
OUT.write_text(json.dumps({"n": len(rows), "rows": rows},
                          ensure_ascii=False, indent=1), encoding="utf-8")
# 少产出必须大声失败。第 3 轮曾只产出 80 例（两个场景注入超时），而跑批
# 照常打印"完成"、正常退出 —— 评测台自己出问题时没有任何东西在监督它，
# 而少掉的那 20 例会给整轮统计带上说不清来源的偏差。
expect = len(scenarios) * 2 * len(DEPTHS)
if len(rows) != expect:
    from collections import Counter as _C
    per = _C(r["scenario"] for r in rows)
    short = {s["id"]: per.get(s["id"], 0) for s in scenarios
             if per.get(s["id"], 0) != 2 * len(DEPTHS)}
    print(f"\n产出不足：{len(rows)}/{expect} 例，缺口场景 {short}")
    Path(OUT).write_text(json.dumps({"rows": rows, "incomplete": True,
                                     "expected": expect, "short": short},
                                    ensure_ascii=False, indent=1),
                         encoding="utf-8")
    sys.exit(2)

print(f"\n共 {len(rows)} 例，耗时 {time.time() - t_start:.0f}s -> {OUT}")
