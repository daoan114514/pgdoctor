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


def _ledger_of(st, res=None) -> dict:
    """取假设台账，供严格诊断算 F1。

    拿不到就返回空 —— 那种情况下没有排除记录，diagnosis_strict 自然
    不过，不会把"缺数据"误报成"做得好"。
    """
    for src in (st, res):
        led = getattr(src, "ledger", None) if src is not None else None
        if isinstance(led, dict):
            return led
    return {}


def _esc_verdict_of(report) -> str:
    """取一份 ESC 报告的裁决，不关心报告是什么形状。

    v2 的 check_explanation 返回 dict，v1 的 check 返回 ESCReport 数据类，
    两种都会进 res.esc_reports。原先这里写死 .verdict，v2 上每个 episode
    都抛 AttributeError —— 而它抛在 run_one 中段，把后面的 metrics_v2、
    applied_sql、gate_decisions、shield_blocked 整段吞掉，还会用这个
    AttributeError 覆盖掉 episode 真正的 error。三率因为在它之前赋值才
    幸存，所以跑批表面看起来是好的。
    """
    if isinstance(report, dict):
        return str(report.get("verdict") or "")
    return str(getattr(report, "verdict", "") or "")


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
    # 严格诊断（把鉴别诊断质量算进去）与"没造成破坏"。后者是本项目
    # 原先的 safe_pass 语义 —— 改名是因为 DBA-Bench 的 Safe Pass 要求
    # 故障真被修好，两者不是一个指标，混用会让对比失去意义。
    diagnosis_strict: bool = False
    non_destructive: bool = False
    outcome: bool = False
    safe_pass: bool = False
    steps: int = 0
    elapsed_s: float = 0.0
    cost_usd: float = 0.0
    esc_verdicts: list = field(default_factory=list)
    # 门的裁决与终局说明。"修复为什么没发生"是这一环最关键的信息，
    # 之前没记，只能翻 traces 才查得到（查了两轮才发现是 rollback 字段）
    gate_decisions: list = field(default_factory=list)
    # 提出过但被护盾挡住的灾难动作。不判 Safe Pass 失败（伤害没落到
    # 库里），但必须报出来 —— 只报"没出事"不报"伸手几次"，等于拿护盾
    # 的功劳掩盖模型的鲁莽。
    shield_blocked: list = field(default_factory=list)
    outcome_note: str = ""
    applied_sql: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    error: str = ""
    episode_id: str = ""
    # 模型调不通导致的作废，与"没诊断出来"必须分开统计
    unusable: bool = False
    learned: dict = field(default_factory=dict)
    learned_layers: list[str] = field(default_factory=list)
    metrics_v2: dict = field(default_factory=dict)


def _confirm(p, d):
    return True


def _already_valid(policy: str) -> set[str]:
    """历史跑批里已经拿到有效结果的场景。

    额度有限时要能跨多个窗口拼出一次完整实验，所以先跑还没结果的。
    """
    import json as _json
    done: set[str] = set()
    if not RESULTS.exists():
        return done
    for f in RESULTS.glob("*.json"):
        try:
            d = _json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("policy") != policy:
            continue
        for e in d.get("episodes", []):
            low = (e.get("error") or "").lower()
            dead = ("modelunavailable" in low
                    or "error result: success" in low
                    or not e.get("fired"))
            if not dead:
                done.add(e["scenario"])
    return done


def _model_reachable() -> bool:
    """跑批前的探针：花几分钱确认模型可用，胜过烧半小时产出一堆废数据。"""
    import asyncio

    from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query

    async def probe() -> bool:
        import os
        env = {k: os.environ[k] for k in
               ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                "no_proxy") if k in os.environ}
        opts = ClaudeAgentOptions(
            model=os.getenv("PGDOCTOR_MODEL", "claude-sonnet-4-5"),
            system_prompt="只回一个数字。", max_turns=1,
            permission_mode="bypassPermissions", setting_sources=None,
            env=env)
        from agent.llm_policy import _UNAVAILABLE_HINTS
        try:
            async for m in query(prompt="回复 1", options=opts):
                if isinstance(m, ResultMessage):
                    # 只要收到 ResultMessage 就算可用是错的：额度耗尽时
                    # 返回的正是一条 ResultMessage，is_error=True、内容是
                    # "error result: success"。探针必须和跑批用同一套判据，
                    # 否则它防不住它本该防的东西。
                    blob = f"{getattr(m, 'subtype', '')} {getattr(m, 'result', '')}"
                    if getattr(m, "is_error", False) or \
                            any(h in blob for h in _UNAVAILABLE_HINTS):
                        print(f"  探针拿到错误结果: {blob[:120]}")
                        return False
                    return True
        except Exception as exc:
            print(f"  探针失败: {str(exc)[:120]}")
            return False
        return False    # 一条 ResultMessage 都没收到，同样不算可用

    print("探测模型可用性 ...", flush=True)
    okp = asyncio.run(probe())
    print(f"  模型可用: {okp}")
    return okp


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
            use_cases: bool, allow_repair: bool, max_steps: int,
            learned_layers: set[str] | None = None,
            ) -> EpisodeOutcome:
    from agent.loop import run_episode
    from sandbox.env import DBAScenarioEnv

    spec = yaml.safe_load(scenario_path.read_text(encoding="utf-8"))
    out = EpisodeOutcome(scenario=spec["id"], fault_class=spec["fault_class"],
                         split=spec.get("split", "train"), policy=policy_name)
    active_layers = ({"l1", "l2", "l3", "l4"}
                     if learned_layers is None else set(learned_layers))
    out.learned_layers = sorted(active_layers)

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
                                  use_cases_split="train",
                                  use_learned=bool(active_layers),
                                  learned_layers=active_layers)
            # run_episode owns scoring and learning finalization.  Repeating it
            # here used to sample a second KPI window and write L1-L4 twice.
            score = res.benchmark_score
            if not score:
                raise RuntimeError("run_episode did not persist benchmark score")
            out.final_phase = res.final_phase
            out.claimed = res.claimed_fault_class
            out.diagnosis = bool(score.get("diagnosis"))
            out.diagnosis_strict = bool(score.get("diagnosis_strict"))
            out.non_destructive = bool(score.get("non_destructive"))
            out.outcome = bool(score.get("outcome"))
            out.safe_pass = bool(score.get("safe_pass"))
            out.steps = res.steps
            out.elapsed_s = res.elapsed_s
            out.esc_verdicts = [_esc_verdict_of(r) for r in res.esc_reports]
            out.applied_sql = res.applied_sql
            out.violations = res.violations
            out.error = res.error
            out.episode_id = res.episode_id
            from eval.metrics_v2 import compute_episode_metrics
            out.metrics_v2 = compute_episode_metrics(
                st, spec, gate_decisions=res.gate_decisions)
            # run_episode 会把异常吞进 res.error，所以要在这里判，
            # 否则额度耗尽的 episode 会被当成"模型没诊断出来"计入分母
            low = (res.error or "").lower()
            if "modelunavailable" in low or "error result: success" in low:
                out.unusable = True
            out.cost_usd = round(
                sum((u.get("cost_usd") or 0.0)
                    for u in getattr(policy, "usage", [])), 4)

            out.learned = dict(res.learning_result)
            out.gate_decisions = res.gate_decisions
            out.outcome_note = st.outcome_note or ""
            out.shield_blocked = list(
                (res.audit or {}).get("shield_blocked") or [])
    except Exception as exc:
        out.error = f"{type(exc).__name__}: {exc}"
        if type(exc).__name__ == "ModelUnavailable" or \
                "error result: success" in str(exc).lower():
            out.unusable = True
        else:
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
    ap.add_argument("--no-learned", action="store_true",
                    help="disable all L1-L4 online consumers")
    ap.add_argument(
        "--learned-layers", default="l1,l2,l3,l4",
        help="comma-separated online learning layers; each of l1,l2,l3,l4 "
             "can be ablated independently")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--max-steps", type=int, default=60)
    ap.add_argument("--tag", default="")
    ap.add_argument("--order", default="name",
                    choices=["name", "reverse", "pending"],
                    help="pending=优先跑历史结果里还没有效数据的场景")
    args = ap.parse_args()
    learned_layers = {
        value.strip().lower() for value in args.learned_layers.split(",")
        if value.strip()
    }
    invalid_layers = learned_layers - {"l1", "l2", "l3", "l4"}
    if invalid_layers:
        ap.error(f"unknown learned layers: {sorted(invalid_layers)}")
    if args.no_learned:
        learned_layers.clear()
    if args.no_cases:
        learned_layers.discard("l1")

    scen_dir = ROOT / "sandbox" / "scenarios"
    picks = []
    for p in sorted(scen_dir.glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if args.split != "all" and d.get("split") != args.split:
            continue
        if args.faults and d["fault_class"] not in args.faults.split(","):
            continue
        picks.append(p)

    if args.order == "reverse":
        picks.reverse()
    elif args.order == "pending":
        done = _already_valid(args.policy)
        picks.sort(key=lambda q: (q.stem in done, q.stem))
        if done:
            print(f"已有有效结果的场景排到最后: {sorted(done)}")

    tag = args.tag or f"{args.policy}_{args.split}"
    if args.no_esc:
        tag += "_noesc"
    if args.no_cases:
        tag += "_nocases"
    if learned_layers != {"l1", "l2", "l3", "l4"}:
        tag += "_layers_" + ("-".join(sorted(learned_layers)) or "off")

    # 跑批前先探一次模型是否可用，避免烧掉几十分钟才发现额度没了
    if args.policy == "llm":
        if not _model_reachable():
            print("模型当前不可用（额度或限流），跑批中止 —— "
                  "现在跑只会产出一堆作废的 episode")
            raise SystemExit(2)

    print(f"跑批 {tag}: {len(picks)} 个场景 "
          f"(ESC={'off' if args.no_esc else 'on'}, "
          f"cases={'off' if args.no_cases else 'on'})")
    results = []
    t0 = time.time()
    for i, p in enumerate(picks, 1):
        print(f"[{i}/{len(picks)}] {p.stem} ...", flush=True)
        _settle()
        r = run_one(p, args.policy, not args.no_esc, not args.no_cases,
                    not args.no_repair, args.max_steps,
                    learned_layers=learned_layers)
        results.append(r)
        print(f"    fired={r.fired} claimed={r.claimed} "
              f"D={r.diagnosis}(严{r.diagnosis_strict}) O={r.outcome} "
              f"S={r.safe_pass}(无损{r.non_destructive}) "
              f"steps={r.steps} ${r.cost_usd} {r.error[:40]}", flush=True)
        if r.unusable and i < len(picks):
            # 撞到额度墙就整批中止。继续往下跑毫无意义：每个场景都要先花
            # 两分钟重建沙箱、灌数据、等告警，然后必然撞上同一堵墙。
            print(f"!! 模型不可用，剩余 {len(picks) - i} 个场景不再尝试",
                  flush=True)
            break

    RESULTS.mkdir(parents=True, exist_ok=True)
    path = RESULTS / f"{tag}.json"
    serialized = [asdict(r) for r in results]
    usable_serialized = [asdict(r) for r in results
                         if r.fired and not r.unusable]
    from eval.metrics_v2 import aggregate_episode_metrics
    aggregate_v2 = aggregate_episode_metrics(usable_serialized)
    path.write_text(json.dumps(
        {"tag": tag, "policy": args.policy, "split": args.split,
         "use_esc": not args.no_esc, "use_cases": not args.no_cases,
         "learned_layers": sorted(learned_layers),
         "elapsed_s": round(time.time() - t0, 1),
         "metrics_v2": aggregate_v2,
         "episodes": serialized},
        ensure_ascii=False, indent=2), encoding="utf-8")

    usable = [r for r in results if r.fired and not r.unusable]
    dead = [r for r in results if r.unusable]
    n = max(len(usable), 1)
    print(f"\n=== {tag} ===")
    print(f"可用 episode: {len(usable)}/{len(results)}")
    if dead:
        print(f"!! {len(dead)} 个 episode 因模型调不通作废（额度/限流），"
              f"不计入三率：{[r.fault_class for r in dead]}")
        print("   这类失败不是模型能力问题，混进统计会让整轮实验失真")
    if dead:
        print(f"   （三率仅基于 {len(usable)} 个有效 episode 计算）")
    print(f"Diagnosis {sum(r.diagnosis for r in usable)}/{n}  "
          f"[严格 {sum(r.diagnosis_strict for r in usable)}/{n}]  "
          f"Outcome {sum(r.outcome for r in usable)}/{n}  "
          f"SafePass {sum(r.safe_pass for r in usable)}/{n}  "
          f"[无损 {sum(r.non_destructive for r in usable)}/{n}]  "
          f"危险动作提出 {sum(len(r.shield_blocked) for r in usable)} 次"
          f"（均被护盾拦下）")
    print(f"成本合计 ${sum(r.cost_usd for r in results):.4f} | "
          f"用时 {round(time.time() - t0)}s")
    p0 = aggregate_v2["p0_obligation_recall"]
    path_r = aggregate_v2["path_recall_at_k"].get("12", {})
    print("v2 P0 obligation recall "
          f"{p0.get('numerator', 0)}/{p0.get('denominator', 0)} | "
          "path recall@12 "
          f"{path_r.get('numerator', 0)}/{path_r.get('denominator', 0)} | "
          "GATE context bypass "
          f"{aggregate_v2['gate_context_bypass_count']}")
    print(f"结果已写入 {path}")


if __name__ == "__main__":
    main()
