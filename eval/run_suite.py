"""跑批器：在一组场景上跑一个策略，输出三率与成本。

设计上把"跑"和"分析"分开：跑批只负责产出结果 JSON 与落盘轨迹，
所有分析都在轨迹上离线做。这样一次昂贵的跑批可以支撑多次分析，
而且换个阈值重算不用再花钱。
"""
from __future__ import annotations

import argparse
import json
import time
import traceback
from dataclasses import asdict, dataclass, field
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"


@dataclass
class EpisodeOutcome:
    scenario: str
    fault_class: str
    split: str
    policy: str
    fired: bool = False
    final_phase: str = ""
    claimed: str | None = None
    diagnosis: bool = False
    outcome: bool = False
    safe_pass: bool = False
    steps: int = 0
    elapsed_s: float = 0.0
    cost_usd: float = 0.0
    esc_verdicts: list = field(default_factory=list)
    applied_sql: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    error: str = ""
    episode_id: str = ""


def _confirm(p, d):
    return True


def _settle(max_wait: float = 60.0) -> None:
    """等上一个 episode 的连接与后台事务散干净再开下一个。

    不等的话，前一个 episode 残留的连接会把本来正常的场景也拖挂
    （实测 missing_index 单跑没问题、跟在 connection_exhaustion
    后面就失败）。
    """
    from sandbox import db
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            n = db.query(
                "SELECT count(*) FROM pg_stat_activity "
                "WHERE datname = current_database()")[0][0]
            if n <= 12:
                break
        except Exception:
            pass
        time.sleep(3)
    time.sleep(3)


def run_one(scenario_path: Path, policy_name: str, use_esc: bool,
            use_cases: bool, allow_repair: bool, max_steps: int
            ) -> EpisodeOutcome:
    from agent.loop import run_episode
    from sandbox.env import DBAScenarioEnv

    spec = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    out = EpisodeOutcome(scenario=spec["id"], fault_class=spec["fault_class"],
                         split=spec.get("split", "train"), policy=policy_name)

    if policy_name == "scripted":
        from agent.policy import ScriptedPolicy
        policy = ScriptedPolicy()
    else:
        from agent.llm_policy import LLMPolicy
        policy = LLMPolicy(verbose=False, use_subagents=True, batch_size=2)

    try:
        with DBAScenarioEnv(str(scenario_path), warmup_s=15.0,
                            degrade_timeout_s=90.0, quiet=True) as env:
            obs = env.reset()
            out.fired = obs.fired
            if not obs.fired:
                # 告警没触发的 episode 是废的：把它算成"没诊断出来"会
                # 污染结论，所以显式标记而不是当成失败
                out.error = "告警未触发，episode 不可用"
                return out
            res, st = run_episode(env, obs, policy, max_steps=max_steps,
                                  allow_repair=allow_repair, confirm_cb=_confirm,
                                  quiet=True, use_esc=use_esc,
                                  use_cases=use_cases,
                                  use_cases_split="train")
            score = env.score(res.claimed_fault_class, audit=res.audit,
                              kpi=res.final_kpi, regression=res.final_regression)
            out.final_phase = res.final_phase
            out.claimed = res.claimed_fault_class
            out.diagnosis = bool(score.diagnosis)
            out.outcome = bool(score.outcome)
            out.safe_pass = bool(score.safe_pass)
            out.steps = res.steps
            out.elapsed_s = res.elapsed_s
            out.esc_verdicts = [r.verdict for r in res.esc_reports]
            out.applied_sql = res.applied_sql
            out.violations = res.violations
            out.error = res.error
            out.episode_id = res.episode_id
            out.cost_usd = round(
                sum((u.get("cost_usd") or 0.0)
                    for u in getattr(policy, "usage", [])), 4)

            # 成功的 episode 沉淀成案例（eval 场景由写入策略挡掉）
            if use_cases:
                from knowledge import case_store as cs
                cs.write_case(st, score, spec, res.applied_sql)
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        traceback.print_exc()
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--policy", default="scripted",
                    choices=["scripted", "llm"])
    ap.add_argument("--split", default="eval", choices=["train", "eval", "all"])
    ap.add_argument("--faults", default="",
                    help="逗号分隔，限定故障类；留空为全部")
    ap.add_argument("--no-esc", action="store_true")
    ap.add_argument("--no-cases", action="store_true")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--tag", default="")
    args = ap.parse_args()

    scen_dir = ROOT / "sandbox" / "scenarios"
    picks = []
    for p in sorted(scen_dir.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if args.split != "all" and d.get("split") != args.split:
            continue
        if args.faults and d["fault_class"] not in args.faults.split(","):
            continue
        picks.append(p)

    tag = args.tag or f"{args.policy}_{args.split}"
    if args.no_esc:
        tag += "_noesc"
    if args.no_cases:
        tag += "_nocases"

    print(f"跑批 {tag}: {len(picks)} 个场景 "
          f"(ESC={'off' if args.no_esc else 'on'}, "
          f"cases={'off' if args.no_cases else 'on'})")
    results = []
    t0 = time.time()
    for i, p in enumerate(picks, 1):
        print(f"[{i}/{len(picks)}] {p.stem} ...", flush=True)
        _settle()
        r = run_one(p, args.policy, not args.no_esc, not args.no_cases,
                    not args.no_repair, args.max_steps)
        results.append(r)
        print(f"    fired={r.fired} claimed={r.claimed} "
              f"D={r.diagnosis} O={r.outcome} S={r.safe_pass} "
              f"steps={r.steps} ${r.cost_usd} {r.error[:40]}", flush=True)

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{tag}.json"
    path.write_text(json.dumps(
        {"tag": tag, "policy": args.policy, "split": args.split,
         "use_esc": not args.no_esc, "use_cases": not args.no_cases,
         "elapsed_s": round(time.time() - t0, 1),
         "episodes": [asdict(r) for r in results]},
        ensure_ascii=False, indent=2), encoding="utf-8")

    usable = [r for r in results if r.fired]
    n = max(len(usable), 1)
    print(f"\n=== {tag} ===")
    print(f"可用 episode: {len(usable)}/{len(results)}")
    print(f"Diagnosis {sum(r.diagnosis for r in usable)}/{n}  "
          f"Outcome {sum(r.outcome for r in usable)}/{n}  "
          f"SafePass {sum(r.safe_pass for r in usable)}/{n}")
    print(f"成本合计 ${sum(r.cost_usd for r in results):.4f} | "
          f"用时 {round(time.time() - t0)}s")
    print(f"结果已写入 {path}")


if __name__ == "__main__":
    main()
