#!/usr/bin/env python3
"""把一批跑批结果（eval/results/guard_*.json 或指定文件）连同 trace 汇总成缺陷报告的素材。

用法：python3 .dev/defect_report.py [结果文件...] > eval/results/defect_report_raw.md
每个 episode 一节：真值 vs 声明、三率、ESC 裁决序列与最后一次失败的维度、路径状态、
证据绑定/合并/不可得、门裁决与干预、metrics_v2 要点。分析与归因由人在这上面写。
"""
from __future__ import annotations

import collections
import glob
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def load_state(episode_id: str) -> dict:
    p = ROOT / "traces" / episode_id / "episode_state.json"
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def fmt_pct(m, key):
    v = (m or {}).get(key)
    if isinstance(v, dict) and "value" in v:
        return "-" if v["value"] is None else f"{v['value']:.2f} ({v.get('numerator')}/{v.get('denominator')})"
    return str(v) if v is not None else "-"


def section(ep: dict) -> list[str]:
    out: list[str] = []
    st = load_state(ep.get("episode_id", ""))
    truth = ep.get("fault_class")
    out.append(f"## {ep.get('scenario')}  ·  真值 `{truth}`  ·  声明 `{ep.get('claimed')}`")
    out.append("")
    out.append(f"- episode: `{ep.get('episode_id')}` | 终态 {ep.get('final_phase')} | steps {ep.get('steps')} | "
               f"{ep.get('elapsed_s', 0):.0f}s | ${ep.get('cost_usd')} | infra {ep.get('infra_failures')} | "
               f"harness_error `{ep.get('harness_error') or ''}`")
    out.append(f"- D(报告口径) **{ep.get('diagnosis_reported')}** | 曾选对 {ep.get('diagnosis')} | 严格 {ep.get('diagnosis_strict')} | "
               f"O **{ep.get('outcome')}** | S **{ep.get('safe_pass')}** | 无损 {ep.get('non_destructive')}")
    out.append(f"- outcome_note: {ep.get('outcome_note') or '-'}")
    out.append(f"- ESC 序列: {' → '.join(ep.get('esc_verdicts') or []) or '-'}")
    reports = st.get("esc_reports") or []
    if reports:
        last = reports[-1]
        failed = [d for d in last.get("dimensions", []) if not d.get("passed")]
        out.append(f"- 最后一次 ESC（{last.get('verdict')}）失败维度: " +
                   ("; ".join(f"{d['name']}: {d.get('detail')}" for d in failed) if failed else "无"))
        if last.get("unresolved_competing_path_ids"):
            out.append(f"  - 未消解的竞争路径: {len(last['unresolved_competing_path_ids'])}")
    eg = st.get("explanation_graph") or {}
    paths = eg.get("candidate_paths") or []
    if isinstance(paths, dict):
        paths = list(paths.values())
    selected = set(eg.get("selected_path_ids") or [])
    if paths:
        out.append("- 路径:")
        for p in paths:
            mark = " **[选中]**" if p.get("path_id") in selected else ""
            out.append(f"  - `{' -> '.join(p.get('node_ids', []))}` {p.get('status')}{mark}")
    bindings = list((eg.get("evidence_bindings") or {}).values())
    by_res = collections.Counter(b.get("predicate_result") for b in bindings)
    out.append(f"- 证据绑定 {len(bindings)}: {dict(by_res)}")
    audit = st.get("evidence_task_audit") or []
    obs = collections.Counter((a.get("executor"), a.get("collection_status")) for a in audit if a.get("event") == "tool_learning_observation")
    out.append(f"- 取证观测（执行者, 状态）: {dict(obs)}")
    acc = sum(len(a.get("accepted") or []) for a in audit if a.get("event") == "evidence_merge")
    rej = collections.Counter(r.split(": ", 1)[-1][:50] for a in audit if a.get("event") == "evidence_merge" for r in (a.get("rejected") or []))
    out.append(f"- 合并: 接受 {acc} | 拒绝 {sum(rej.values())} {dict(rej) if rej else ''}")
    unav = collections.Counter(f"{a.get('source')}: {str(a.get('reason'))[:60]}" for a in audit if a.get("event") == "evidence_need_unavailable")
    if unav:
        out.append("- 不可得（来源: 原因 × 次数）:")
        for k, n in unav.most_common(6):
            out.append(f"  - {k} × {n}")
    subagent_tools = collections.Counter(a.get("tool") for a in audit if a.get("event") == "tool_learning_observation" and a.get("executor") == "subagent")
    out.append(f"- 子 agent 用到的工具: {dict(subagent_tools)}")
    gd = ep.get("gate_decisions") or []
    if gd:
        out.append("- 门裁决:")
        for g in gd[:6]:
            out.append(f"  - {g.get('tier')} approved={g.get('approved')} `{str(g.get('sql'))[:80]}` — {'; '.join((g.get('reasons') or [])[:2])}")
    else:
        out.append("- 门裁决: 无提案")
    attempts = st.get("intervention_attempts") or []
    if attempts:
        out.append("- 干预:")
        for a in attempts:
            out.append(f"  - {a.get('fix_id')} exec={a.get('execution_status')} outcome={a.get('outcome')} rollback={a.get('rollback_status')} `{str(a.get('sql'))[:70]}`")
    sb = ep.get("shield_blocked") or []
    if sb:
        out.append(f"- 护盾拦下: {len(sb)} 次: {[str(x)[:60] for x in sb[:3]]}")
    m = ep.get("metrics_v2") or {}
    out.append(f"- metrics_v2: root_recall@1 {fmt_pct(m, 'root_recall_at_k') if not isinstance(m.get('root_recall_at_k'), dict) else fmt_pct(m['root_recall_at_k'], '1')}"
               f" | @3 {fmt_pct(m.get('root_recall_at_k'), '3')} | required_evidence_completion {fmt_pct(m, 'required_evidence_completion')}"
               f" | unknown_error_rate {fmt_pct(m, 'unknown_error_rate')} | tool_calls {m.get('tool_calls')} | esc_over_conservative {m.get('esc_over_conservative')}")
    dirs = st.get("directives") or []
    if dirs:
        out.append("- 最后的 ESC 指令: " + " | ".join(str(x)[:90] for x in dirs[-3:]))
    out.append("")
    return out


def main() -> None:
    files = [Path(a) for a in sys.argv[1:]] or sorted(
        Path(p) for p in glob.glob(str(ROOT / "eval" / "results" / "guard_*.json")))
    episodes: list[dict] = []
    harness = {}
    for f in files:
        d = json.loads(f.read_text(encoding="utf-8"))
        harness = d.get("harness") or harness
        for e in d.get("episodes", []):
            e["_file"] = f.name
            episodes.append(e)
    episodes.sort(key=lambda e: e.get("scenario", ""))
    print("# 跑批缺陷报告素材")
    print()
    print(f"harness: commit `{harness.get('git_commit')}` dirty={harness.get('git_dirty')} | graph `{harness.get('graph_version')}` | "
          f"SDK {harness.get('sdk_version')} / CLI {harness.get('cli_version')} | max_steps {harness.get('default_max_steps')}")
    print()
    print("| 场景 | 真值 | 声明 | D报告 | D曾选对 | O | S | steps | $ | infra | 终态 |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for e in episodes:
        print(f"| {e.get('scenario')} | {e.get('fault_class')} | {e.get('claimed')} | {e.get('diagnosis_reported')} | {e.get('diagnosis')} | "
              f"{e.get('outcome')} | {e.get('safe_pass')} | {e.get('steps')} | {e.get('cost_usd')} | {e.get('infra_failures')} | {e.get('final_phase')} |")
    usable = [e for e in episodes if e.get("fired") and not e.get("unusable")]
    n = max(len(usable), 1)
    print()
    print(f"可用 {len(usable)}/{len(episodes)} | D(报告) {sum(bool(e.get('diagnosis_reported')) for e in usable)}/{n} | "
          f"曾选对 {sum(bool(e.get('diagnosis')) for e in usable)}/{n} | O {sum(bool(e.get('outcome')) for e in usable)}/{n} | "
          f"S {sum(bool(e.get('safe_pass')) for e in usable)}/{n} | 成本 ${sum(e.get('cost_usd') or 0 for e in episodes):.2f}")
    print()
    for e in episodes:
        print("\n".join(section(e)))


if __name__ == "__main__":
    main()
