#!/usr/bin/env python3
"""基础设施失败判据的语料回放检查。

CLAUDE.md 硬规则 5：阈值/词表必须用真实观测值标定，构造值只能验逻辑分支、
验不了刻度。所以这个检查不写夹具，直接回放 traces/ 里真实 episode 的
`evidence_need_unavailable.reason` 字符串，断言分类结果和人工核对的数目一致。

标定语料（2026-09-21 那轮 eval 跑批，人工逐条核对过）：

  ep_connection_exhaustion_eval_v1_1789974597  63 条不可得 = 18 停机 + 45 max_turns
  ep_misleading_idle_txn_eval_v1_1789982550    29 条不可得 = 29 停机 + 0  max_turns
  ep_lock_contention_eval_v1_1789980308        91 条不可得 = 0  停机 + 91 agent 侧

第三个是负对照，也是最重要的一个：它是同一轮跑批里唯一没被停机碰过的 episode，
如果判据开始在它身上报停机，说明词表已经宽到会吃掉真实的诊断失败 —— 那是
比原 bug 更严重的方向（把失败从分母里删掉，三率凭空变好）。

语料不在了（traces 被清理）就跳过对应断言并打印，不假装通过。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent.failure_class import (infra_rows_of, is_infra_failure,  # noqa: E402
                                 observed_subagent_count)

# episode 目录名 -> (停机条数, 不可得总条数, 子 agent OBSERVED 条数, 应否作废)
# 第四列就是 run_suite 的作废门：停机>0 且 OBSERVED==0。三个 episode 恰好覆盖
# 三种情形：全灭作废 / 部分停机仍计分但要标记 / 干净。
CORPUS = {
    "ep_connection_exhaustion_eval_v1_1789974597": (18, 63, 23, False),
    "ep_misleading_idle_txn_eval_v1_1789982550": (29, 29, 0, True),
    "ep_lock_contention_eval_v1_1789980308": (0, 91, 8, False),
}

# 必须认成停机的真实原话（都能在 trace 或历史事故记录里找到出处）
MUST_INFRA = [
    "ResultError: Claude Code returned an error result: You've hit your "
    "session limit · resets 9:40pm (Asia/Shanghai) (exit code: 1)",
    "ResultError: Claude Code returned an error result: error result: success",
    "ModelUnavailable: 模型调用不可用（额度/限流/认证/网络）",
    "Failed to authenticate: OAuth session expired and could not be refreshed",
    "API Error: 429 rate_limit_error",
    "Your credit balance is too low",
]

# 必须**不**认成停机的：agent 能力/预算/环境不可得。
# 认错方向就是把真实诊断失败从分母里删掉。
MUST_NOT_INFRA = [
    "ResultError: Claude Code returned an error result: Reached maximum "
    "number of turns (12) (exit code: 1)",
    "subagent did not call report_evidence",
    "invalid EvidenceReport: report need_id does not match assigned task",
    "missing EvidenceReport for needs: ['need_abc']",
    "Budget exhausted - RuntimeError: 预算耗尽",
    "collection status UNKNOWN: pg_stat_statements 未安装",
    "missing_index: branch discriminator",
    "distinguish a major competing path",
    "permission denied for table orders",
    # 旧词表里 "429" 是裸子串，会命中任何含这三个数字的文本；
    # "exceeded" 和 "Budget exhausted" 只差一个词。两条都删掉了，这里钉住。
    "seq_tup_read 4290 行，未超过阈值",
    "row estimate exceeded the planner limit by 258x",
]

fails: list[str] = []
checks = 0


def check(cond: bool, label: str) -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)


print("[1] 必须认成停机的原话")
for text in MUST_INFRA:
    ok = is_infra_failure(text=text)
    check(ok, f"漏判停机: {text[:70]}")
    print(f"    {'OK ' if ok else 'FAIL'} {text[:78]}")

print("[2] 必须不认成停机的原话（认错方向会把真失败移出分母）")
for text in MUST_NOT_INFRA:
    ok = not is_infra_failure(text=text)
    check(ok, f"误判成停机: {text[:70]}")
    print(f"    {'OK ' if ok else 'FAIL'} {text[:78]}")

print("[3] 真实 trace 语料回放（分类 + 作废门）")
for stem, (want_infra, want_total, want_obs, want_dead) in CORPUS.items():
    state = ROOT / "traces" / stem / "episode_state.json"
    if not state.exists():
        print(f"    SKIP {stem}（trace 已不在，不计入断言）")
        continue
    audit = json.loads(state.read_text(encoding="utf-8")).get(
        "evidence_task_audit", [])
    total = sum(1 for x in audit
                if x.get("event") == "evidence_need_unavailable")
    infra = len(infra_rows_of(audit))
    obs = observed_subagent_count(audit)
    # 与 eval/run_suite.py::run_one 里的作废门逐字一致；改了一处必须改另一处
    dead = bool(infra) and obs == 0
    ok = (infra, total, obs, dead) == (want_infra, want_total, want_obs, want_dead)
    check(ok, f"{stem} 期望 停机{want_infra}/{want_total} OBSERVED={want_obs} "
              f"作废={want_dead}，实得 {infra}/{total} OBSERVED={obs} 作废={dead}")
    print(f"    {'OK ' if ok else 'FAIL'} {stem}: 停机 {infra}/{total} "
          f"OBSERVED={obs} -> {'作废' if dead else '计分'}"
          f"（期望 {want_infra}/{want_total} OBSERVED={want_obs} "
          f"{'作废' if want_dead else '计分'}）")

print("[4] 类型化字段优先于字符串（混合批次里 max_turns 不能被整批认成停机）")


class _FakeResultError(Exception):
    def __init__(self, msg, subtype=None, status=None):
        super().__init__(msg)
        self.subtype = subtype
        self.api_error_status = status


# 报错原文含 "session limit"，但 subtype 说是 max_turns -> 必须判 agent 侧
mixed = _FakeResultError("hit your session limit", subtype="error_max_turns")
check(not is_infra_failure(mixed), "subtype=error_max_turns 被误判成停机")
print(f"    {'OK ' if not is_infra_failure(mixed) else 'FAIL'} "
      "subtype=error_max_turns 优先于字符串")

# 400 是我们自己把请求拼错了，必须留在分母里
bad_req = _FakeResultError("invalid_request_error", status=400)
check(not is_infra_failure(bad_req), "HTTP 400 被误判成停机")
print(f"    {'OK ' if not is_infra_failure(bad_req) else 'FAIL'} "
      "HTTP 400 不算停机（是我们自己的 bug，要让它被看见）")

for status in (429, 529, 503):
    e = _FakeResultError("whatever", status=status)
    check(is_infra_failure(e), f"HTTP {status} 漏判")
    print(f"    {'OK ' if is_infra_failure(e) else 'FAIL'} HTTP {status} 算停机")

print("[5] 认不出来必须返回 False（单向：宁可把停机留在分母里）")
for text in ("", "some unrecognised failure", "Traceback (most recent call last)"):
    ok = not is_infra_failure(text=text)
    check(ok, f"未知文本被认成停机: {text[:40]}")
    print(f"    {'OK ' if ok else 'FAIL'} {text[:50]!r}")

print()
if fails:
    print(f"INFRA_FAILURE_CHECK: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"INFRA_FAILURE_CHECK: PASS（{checks} 项）")
