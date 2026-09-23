#!/usr/bin/env python3
"""模型值不再直接进裁决、评分、学习数据与停机判定（2026-09-23 模型边界审计第一类）。

钉住的规则：
  v2 下 set_hypothesis / declare_root_cause 在三处都不可用（不注册、hook 拒、Toolbox 抛）；
  声明的根因必须是图上的 RootCause，假设名必须在候选集合内；打分前重算投影；
  采集状态从 scratchpad 条目推导，模型自报值只进审计；
  停机标记在异常还在手里时算出，模型的 limitations 不进词表；max_turns 不重试不抛；
  "真 SUFFICIENT" 只有一个判据，--no-esc 的旁路报告不冒充；打分后的后处理崩溃不作废 episode。
"""
from __future__ import annotations

import inspect
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agent import failure_class as fc  # noqa: E402
from agent import llm_policy as lp  # noqa: E402
from agent import orchestrator as orch  # noqa: E402
from agent import loop as loop_mod  # noqa: E402
from agent.esc import esc_is_genuinely_sufficient, esc_verdict_label  # noqa: E402
from agent.permissions import Role  # noqa: E402
from agent.state_machine import Phase  # noqa: E402
from agent.toolbox import PhaseViolation, Toolbox  # noqa: E402

fails: list[str] = []
checks = 0


def check(cond: bool, label: str, detail="") -> None:
    global checks
    checks += 1
    if not cond:
        fails.append(label)
    print(f"    {'OK ' if cond else 'FAIL'} {label}" + (f"  {detail}" if not cond else ""))


def raises(fn, exc_type):
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


print("[1] v2 下裁决类工具在三处读取点都不可用")
tb_v2 = SimpleNamespace(st=SimpleNamespace(schema_version=2))
tb_v1 = SimpleNamespace(st=SimpleNamespace(schema_version=1))
names_v2 = {getattr(t, "name", "") for t in lp._model_tools(tb_v2)}
names_v1 = {getattr(t, "name", "") for t in lp._model_tools(tb_v1)}
check(not (names_v2 & lp.V2_MODEL_FORBIDDEN), "v2：裁决工具不注册到 MCP server", sorted(names_v2 & lp.V2_MODEL_FORBIDDEN))
check({"set_hypothesis", "declare_root_cause"} <= names_v1, "v1：裁决工具仍注册")
check(lp._forbidden_for(tb_v2) == lp.V2_MODEL_FORBIDDEN and not lp._forbidden_for(tb_v1), "_forbidden_for 按 schema_version")
ask_src = inspect.getsource(lp.LLMPolicy._ask)
check("extra_denied=_forbidden_for(tb)" in ask_src, "hook 收到同一份禁用集合")


def make_tb(schema_version, candidates=()):
    tb = Toolbox.__new__(Toolbox)
    tb.task_context = None
    tb.target_context = None
    tb.role = Role.MAIN
    tb.hypothesis = None
    tb.environment_tools = None
    tb.sm = SimpleNamespace(phase=Phase.INVESTIGATE)
    tb.calls = []
    tb.st = SimpleNamespace(spend=lambda: True, schema_version=schema_version,
                            hypothesis_candidates=list(candidates), ledger={},
                            already_failed=lambda fc_: False)
    return tb


tb = make_tb(2, ["missing_index"])
check(raises(lambda: tb.set_hypothesis("missing_index", "CONFIRMED", "x" * 30), PhaseViolation), "v2：Toolbox.set_hypothesis 抛 PhaseViolation")
check(raises(lambda: tb.declare_root_cause("missing_index", "y" * 30), PhaseViolation), "v2：Toolbox.declare_root_cause 抛 PhaseViolation")
tb = make_tb(1, ["missing_index", "stale_statistics"])
check(raises(lambda: tb.set_hypothesis("bogus", "CONFIRMED", "x" * 30), ValueError), "v1：假设名不在候选集合 -> ValueError")
check(raises(lambda: tb.declare_root_cause("MISSING_INDEX", "y" * 30), ValueError), "v1：非图上根因（大小写错）-> ValueError")
check(raises(lambda: tb.declare_root_cause("latency_p99_up", "y" * 30), ValueError), "v1：症状节点当根因 -> ValueError")
loop_src = inspect.getsource(loop_mod)
i = loop_src.find("xr.sync_v1_projection(st)\n        st.save()")
j = loop_src.find("res.claimed_fault_class = st.claimed_fault_class")
check(0 < i < j, "loop 在读 claimed_fault_class 打分前重算投影")

print("[2] 采集状态从 scratchpad 条目推导，不读模型自报值")
need_a = SimpleNamespace(evidence_type="explain_plan")
need_b = SimpleNamespace(evidence_type="row_estimate_deviation")
task = SimpleNamespace(task_id="task_1", need_ids=["na", "nb"])
st = SimpleNamespace(scratchpad=[
    {"evidence_task_id": "task_1", "raw_ref": "trace://e/step_001", "evidence_type": "explain_plan", "status": "OBSERVED", "summary": "plan"},
    {"evidence_task_id": "task_2", "raw_ref": "trace://e/step_002", "evidence_type": "row_estimate_deviation", "status": "OBSERVED"},
])
facts = orch._need_facts_from_scratchpad(st, task, {"na": need_a, "nb": need_b})
check(facts["na"][0] == "OBSERVED", "有 OBSERVED 条目 -> OBSERVED")
check(facts["nb"][0] == "UNKNOWN", "工具跑了但没产出该证据类型 -> UNKNOWN（别的任务的条目不算）")
st2 = SimpleNamespace(scratchpad=[{"evidence_task_id": "task_1", "raw_ref": "r", "evidence_type": "explain_plan", "status": "UNKNOWN"}])
check(orch._need_facts_from_scratchpad(st2, task, {"na": need_a, "nb": need_b})["na"][0] == "UNKNOWN", "只有 UNKNOWN 条目 -> UNKNOWN")
st3 = SimpleNamespace(scratchpad=[])
check(orch._need_facts_from_scratchpad(st3, task, {"na": need_a})["na"][0] is None, "没有任何条目 -> None（工具没产出）")
mu = inspect.getsource(orch._mark_unavailable)
check("_need_facts_from_scratchpad" in mu and "report.collection_status !=" not in mu, "_mark_unavailable 不再读 report.collection_status")
check('"reported_limitations"' in mu and 'row["infra"]' in mu, "审计行带 source/infra，模型局限另存 reported_limitations")
rl = inspect.getsource(orch._record_tool_learning_observations)
check("_need_facts_from_scratchpad" in rl and '"reported_status": reported_status' in rl, "学习观测的状态取系统事实，模型值另存")
check("accepted_ids" in rl, "accepted 要求报告真的被合并层接受")

print("[3] 停机判定读类型化标记；模型 limitations 不进词表；max_turns 不重试")
rows = [
    {"event": "evidence_need_unavailable", "infra": True, "reason": "x"},
    {"event": "evidence_need_unavailable", "infra": False, "reason": "You've hit your session limit"},
    {"event": "evidence_need_unavailable", "source": "collection_status", "reason": "collection status UNKNOWN: server overloaded"},
    {"event": "evidence_need_unavailable", "source": "planner", "reason": "rate limit"},
    {"event": "evidence_need_unavailable", "reason": "You've hit your session limit · resets 2:20pm"},
    {"event": "evidence_need_unavailable", "source": "task_error", "reason": "session limit"},
]
got = fc.infra_rows_of(rows)
check(got == [rows[0], rows[4], rows[5]], "只有 infra=True、无标记的旧行、task_error 且命中词表的行算停机", [rows.index(r) for r in got])
check(fc.is_agent_terminal(SimpleNamespace(subtype="error_max_turns")) and not fc.is_infra_failure(SimpleNamespace(subtype="error_max_turns")), "max_turns 是 agent 侧终止，不是停机")
check(not fc.is_agent_terminal(SimpleNamespace(api_error_status=529)), "529 不是 agent 侧终止")
from agent import investigator as inv  # noqa: E402
inv_src = inspect.getsource(inv.investigate_task)
check("result.infra = is_infra_failure(exc)" in inv_src and "result.agent_terminal = is_agent_terminal(exc)" in inv_src, "investigator 在异常还在手里时算出类型化标记")
check(ask_src.index("is_agent_terminal(exc)") < ask_src.index("asyncio.sleep(8)"), "_ask 对 agent 侧终止不重试、不抛（在重试之前分支）")

print("[4] 真 SUFFICIENT 只有一个判据")
check(not esc_is_genuinely_sufficient({"verdict": "SUFFICIENT", "bypassed": True}), "旁路报告不算 SUFFICIENT")
check(esc_is_genuinely_sufficient({"verdict": "SUFFICIENT"}), "真实 SUFFICIENT")
check(esc_verdict_label({"verdict": "SUFFICIENT", "bypassed": True}) == "BYPASSED", "标签显示 BYPASSED")
from knowledge import case_store, evolution  # noqa: E402
import eval.run_suite as rs  # noqa: E402
check("esc_is_genuinely_sufficient" in inspect.getsource(case_store._latest_sufficient_esc), "case_store 走同一判据")
check("esc_is_genuinely_sufficient" in inspect.getsource(evolution._update_l3_v2), "evolution L3 走同一判据")
check("esc_mod.esc_verdict_label(r)" in loop_src, "loop 的 esc_last_verdict 走标签")
check("esc_verdict_label" in inspect.getsource(rs._esc_verdict_of), "run_suite 的 esc_verdicts 走标签")

print("[5] 打分之后的后处理崩溃不作废 episode")
ro = inspect.getsource(rs.run_one)
check("scored = True" in ro and "if scored:" in ro and "out.harness_error" in ro, "run_one 锁定三率后只记 harness_error")
check(ro.index("scored = True") < ro.index("compute_episode_metrics"), "锁定点在 metrics_v2 之前")
check("harness_error" in {f.name for f in rs.EpisodeOutcome.__dataclass_fields__.values()}, "EpisodeOutcome 有 harness_error 字段")

print("[6] 封闭集合参数在 schema 里是 enum，不是裸 str；SDK 边界")
stub = SimpleNamespace(st=SimpleNamespace(schema_version=1, hypothesis_candidates=["missing_index"]))
by_name = {getattr(t, "name", ""): t for t in lp._build_tools(stub)}
ps = by_name["submit_proposal"].input_schema["properties"]
check(set(ps["action_type"].get("enum") or []) == set(lp._fix_action_types()) and ps["action_type"]["enum"],
      "submit_proposal.action_type 的 enum 来自因果图修复节点的动作类型", ps["action_type"])
check(by_name["set_hypothesis"].input_schema["properties"]["verdict"].get("enum") == ["CONFIRMED", "REFUTED", "INCONCLUSIVE"],
      "set_hypothesis.verdict 是 enum")
check(by_name["set_hypothesis"].input_schema["properties"]["name"].get("enum") == ["missing_index"], "set_hypothesis.name 的 enum 是候选假设")
check("missing_index" in (by_name["declare_root_cause"].input_schema["properties"]["fault_class"].get("enum") or []),
      "declare_root_cause.fault_class 的 enum 是图上根因")
CLOSED = {"action_type", "verdict", "fault_class", "collection_status", "need_id", "tool", "selected_path_id", "fix_id", "intervention_target"}
bare = []
for name, tdef in by_name.items():
    schema = tdef.input_schema
    if isinstance(schema, dict) and "properties" not in schema:
        bare += [f"{name}.{k}" for k, v in schema.items() if k in CLOSED and v is str]
check(not bare, "主 agent 工具里没有封闭集合参数被声明成裸 str", bare)
from agent import investigator as inv2  # noqa: E402
itools = {getattr(t, "name", ""): t for t in inv2._tools_for(SimpleNamespace(), {}, task=SimpleNamespace(need_ids=["n1"], selected_tools=["explain_query"]))}
iprops = itools["report_evidence"].input_schema["properties"]
check(iprops["need_id"].get("enum") == ["n1"] and iprops["tool"].get("enum") == ["explain_query"] and iprops["collection_status"].get("enum"),
      "子 agent report_evidence 的 need_id/tool/collection_status 都是 enum")
for label, src in (("llm_policy._ask", ask_src), ("investigator", inspect.getsource(inv2)),
                   ("run_suite", inspect.getsource(rs))):
    check("setting_sources=[]" in src and "setting_sources=None" not in src, f"{label} 用 setting_sources=[]（隔离模式）")
req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
check("claude-agent-sdk==" in req, "requirements 钉死 SDK 版本")
ident = rs._harness_identity()
check(bool(ident.get("sdk_version")) and bool(ident.get("cli_version")), "harness 身份记录 SDK/CLI 版本", ident)
wrap_src = inspect.getsource(lp._build_tools)
check("tool_result_text(r, 4000)" in wrap_src, "主 agent 工具结果经 tool_result_text（截断可见、列表按条截）")
from agent.toolbox import tool_result_text as _trt  # noqa: E402
import json as _json  # noqa: E402
_big = [{"pid": i, "state": "idle", "note": "x" * 120} for i in range(60)]
_out = _json.loads(_trt(_big, 2000))
check(_out.get("result_truncated") and isinstance(_out.get("result"), list) and _out["result"] and _out["total"] == 60 and _out["shown"] == len(_out["result"]),
      "列表结果按条截断后仍是完整 JSON", str(_out)[:80])
check(_trt({"a": 1}, 4000) == '{"a": 1}', "不超长时原样返回")

print()
if fails:
    print(f"MODEL BOUNDARY: FAIL（{len(fails)}/{checks}）")
    for f in fails:
        print("  - " + f)
    raise SystemExit(1)
print(f"MODEL BOUNDARY: PASS（{checks} 项）")
