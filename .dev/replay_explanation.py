#!/usr/bin/env python3
"""离线重放：用一个真实 episode 的观测（scratchpad），在**当前**因果图与判据下重建解释、
绑定证据、选路径、跑 ESC —— 改图或改判据之后先在这里看效果，不花额度、不碰活库。

用法：python3 .dev/replay_explanation.py <episode_id> [<期望选中的根因>]
时间钉在原 episode 的最后观测时刻（证据新鲜度按当时算）；只读原 trace，不写任何东西。
观测按"主 agent 取得"重放（去掉原任务/解释标签），因为原任务绑的是旧的解释 revision。
"""
from __future__ import annotations

import copy
import json
import sys
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import explanation_runtime as xr  # noqa: E402
from agent.episode_state import EpisodeState  # noqa: E402
from agent.esc import check_explanation  # noqa: E402


def replay(episode_id: str) -> dict:
    raw = json.loads((ROOT / "traces" / episode_id / "episode_state.json").read_text(encoding="utf-8"))
    # 只取第一次执行干预之前的观测：之后的观测反映的是修复后的库（规则 6：动作制造证据），
    # 拿它重放诊断会把"已修好"当成"没发生过"。
    started = [float(a.get("created_at") or 0) for a in raw.get("intervention_attempts") or []
               if a.get("execution_status") == "SUCCEEDED"]
    cutoff = min(started) if started else float("inf")
    raw["scratchpad"] = [e for e in raw.get("scratchpad", []) if float(e.get("ts") or 0) < cutoff]
    at = max([float(e.get("ts") or 0) for e in raw.get("scratchpad", [])] or [time.time()]) + 1.0
    with mock.patch("time.time", return_value=at):
        st = EpisodeState(episode_id, raw.get("scenario_id", "replay"))
        st.alert = raw.get("alert", "")
        st.symptoms = list(raw.get("symptoms") or [])
        st.observed_symptom_ids = list(raw.get("observed_symptom_ids") or [])
        st.incident_window = copy.deepcopy(raw.get("incident_window") or {})
        st.budget = dict(raw.get("budget") or st.budget)
        entries = copy.deepcopy(raw.get("scratchpad") or [])
        for entry in entries:
            for key in ("explanation_id", "evidence_task_id"):
                entry[key] = ""
            entry["explanation_revision"] = None
            entry["evidence_need_ids"] = []
        st.scratchpad = entries
        xr.recall_explanation(st, use_learned=False)
        for _ in range(6):
            added = xr.bind_evidence(st)
            xr.select_minimal_explanation(st)
            if not added:
                break
        report = check_explanation(st, persist=False)
    exp = st.explanation_graph
    return {
        "paths": [(" -> ".join(p.node_ids), p.status, p.path_id in exp.selected_path_ids)
                  for p in exp.candidate_paths],
        "selected_roots": exp.derive_selected_root_causes(),
        "verdict": report["verdict"],
        "failed": [(d["name"], d.get("detail")) for d in report["dimensions"] if not d.get("passed")],
        "claimed": st.claimed_fault_class,
    }


if __name__ == "__main__":
    out = replay(sys.argv[1])
    for text, status, selected in out["paths"]:
        print(f"  {'*' if selected else ' '} {status:12} {text}")
    print("selected roots:", out["selected_roots"], "| claimed:", out["claimed"])
    print("ESC:", out["verdict"], out["failed"])
    if len(sys.argv) > 2:
        ok = out["claimed"] == sys.argv[2] and out["verdict"] == "SUFFICIENT"
        print("REPLAY:", "PASS" if ok else "FAIL", f"(期望 {sys.argv[2]} + SUFFICIENT)")
        raise SystemExit(0 if ok else 1)
