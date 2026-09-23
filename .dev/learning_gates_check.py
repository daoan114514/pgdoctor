#!/usr/bin/env python3
"""学习层的门（架构评审第 8 条）：作废判据只写一份、停机不学习、L1 可召回条件来自
episode 自身的信任证据、失效标记不跨故障类。"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import loop as loop_mod  # noqa: E402
from agent.failure_class import episode_unusable_by_infra  # noqa: E402
from knowledge import case_store, evolution  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


print("[1] 作废判据只有一份、两处都读它")
audit_dead = [{"event": "evidence_need_unavailable", "reason": "You've hit your session limit"}]
audit_live = audit_dead + [{"event": "tool_learning_observation", "collection_status": "OBSERVED"}]
check(episode_unusable_by_infra(audit_dead), "有停机且零 OBSERVED -> 作废")
check(not episode_unusable_by_infra(audit_live), "有停机但有 OBSERVED -> 计分（单向）")
check(not episode_unusable_by_infra([]), "无停机 -> 不作废")
check("episode_unusable_by_infra(" in inspect.getsource(loop_mod._post_episode_learning), "loop 的学习写回读它")
check("episode_unusable_by_infra(" in (ROOT / "eval/run_suite.py").read_text(encoding="utf-8"), "run_suite 的分母读它")
check('"SKIPPED_INFRA"' in inspect.getsource(loop_mod._post_episode_learning), "停机作废的 episode 不写任何学习层")

print("[2] L1 可召回条件")
csrc = inspect.getsource(case_store.write_case_v2)
check('_reason == "verified positive explanation"' in csrc, "SUFFICIENT ESC + VERIFIED 尝试即可召回（不再只看 spec.source_refs）")
check("training_eligible=_eligible" in csrc and "source_refs=_source_refs" in csrc, "training_eligible / source_refs 由推导值决定")
check('f"trace://{st.episode_id}/episode_state"' in csrc, "来源指向本 episode 的 trace")

print("[3] 失效标记不再按本次场景 revision 清掉其它故障类")
esrc = inspect.getsource(evolution.learn_v2)
check("scenario_revision=" not in esrc.split("mark_v2_stale(")[1].split(")")[0], "learn_v2 调 mark_v2_stale 不传 scenario_revision")
check("def _is_stale" in inspect.getsource(evolution), "读取时的 _is_stale 仍按根因逐个判场景 revision")

print()
if fails:
    print(f"LEARNING GATES: FAIL（{len(fails)}/{checks}）")
    raise SystemExit(1)
print(f"LEARNING GATES: PASS（{checks} 项）")
