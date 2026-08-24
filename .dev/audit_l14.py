"""核实 L1-L4 自进化各层的实现情况 —— 看代码，不看印象。"""
import subprocess
from pathlib import Path

REPO = Path("/home/daoan/pgdoctor")


def has(pattern, *paths):
    """在指定文件或目录里找符号，返回命中的路径。

    原来只处理文件：传 "knowledge/" 这种目录时 exists() 为真，
    read_text() 直接抛 IsADirectoryError，L4 整段审计从没跑完过。
    """
    hits = []
    for p in paths:
        f = REPO / p
        if not f.exists():
            continue
        files = sorted(f.rglob("*.py")) + sorted(f.rglob("*.yaml")) \
            if f.is_dir() else [f]
        for one in files:
            try:
                if pattern in one.read_text(encoding="utf-8"):
                    hits.append(str(one.relative_to(REPO)))
                    break
            except (OSError, UnicodeDecodeError):
                continue
    return hits


print("=" * 76)
print("L1  案例记忆检索：解过的事故存成 case，下次告警检索命中")
print("=" * 76)
for label, pat, files in [
    ("案例写入", "def write_case", ["knowledge/case_store.py"]),
    ("混合检索", "def search", ["knowledge/case_store.py"]),
    ("先验注入主循环", "case_prior", ["agent/loop.py"]),
    ("先验进提示", 'ctx.get("case_prior"', ["agent/llm_policy.py"]),
    ("效用追踪/隔离", "def record_reuse", ["knowledge/case_store.py"]),
    ("跑批后写入案例", "cs.write_case", ["eval/run_suite.py"]),
]:
    h = has(pat, *files)
    print(f"  {'已实现' if h else '缺失  '}  {label:<20} {h or ''}")

print()
print("=" * 76)
print("L2  技能沉淀：把成功的诊断路径固化成可复用 playbook")
print("=" * 76)
for label, pat, files in [
    ("Playbook 数据结构", "class Playbook", ["knowledge/evolution.py"]),
    ("只从成功 episode 沉淀", "def sediment_playbook",
     ["knowledge/evolution.py"]),
    ("步数用真中位数", "steps_samples", ["knowledge/evolution.py"]),
    ("压成提示", "def render_playbook_hint", ["knowledge/evolution.py"]),
    ("提示进主循环", "playbook_hint", ["agent/loop.py"]),
    ("提示进模型上下文", "playbook_hint", ["agent/llm_policy.py"]),
    ("落盘可 diff", "def save_playbooks", ["knowledge/evolution.py"]),
]:
    h = has(pat, *files)
    print(f"  {'已实现' if h else '缺失  '}  {label:<20} {h or ''}")

print()
print("=" * 76)
print("L3  失败驱动：从失败反思，更新假设生成的先验与检查清单")
print("=" * 76)
for label, pat, files in [
    ("失败尝试被记录", "def record_attempt", ["agent/episode_state.py"]),
    ("失败进案例库(负例)", "failed_attempts", ["knowledge/case_store.py"]),
    ("失败归因可区分", "counts_against_root_cause",
     ["agent/episode_state.py", "knowledge/evolution.py"]),
    ("从结局更新先验", "def learn_from_episode", ["knowledge/evolution.py"]),
    ("已知真值时定向修正", "def learn_truth", ["knowledge/evolution.py"]),
    ("先验回流到候选排序", "def _learned_adj",
     ["knowledge/causal_graph/graph.py"]),
    ("边权重回流到候选排序", "def _learned_likelihood_adj",
     ["knowledge/causal_graph/graph.py"]),
    ("调整量有相对上限", "MAX_REL_ADJ", ["knowledge/evolution.py"]),
    ("overlay 不改种子图", "def save_delta", ["knowledge/evolution.py"]),
]:
    h = has(pat, *files)
    print(f"  {'已实现' if h else '缺失  '}  {label:<20} {h or ''}")

print()
print("=" * 76)
print("L4  诊断查询库：沉淀有效的诊断 SQL，越用越会查")
print("=" * 76)
for label, pat, files in [
    ("QueryStat 数据结构", "class QueryStat", ["knowledge/evolution.py"]),
    ("统计每种证据的表现", "def record_queries", ["knowledge/evolution.py"]),
    ("按根因算判别力", "def power_for", ["knowledge/evolution.py"]),
    ("归因只算图上相关证据", "def _evidence_of", ["knowledge/evolution.py"]),
    ("回流到子 agent 工具顺序", "top_queries_for", ["agent/investigator.py"]),
    ("跑批后回流", "ev.learn", ["eval/run_suite.py"]),
    ("落盘可 diff", "def save_queries", ["knowledge/evolution.py"]),
]:
    h = has(pat, *files)
    print(f"  {'已实现' if h else '缺失  '}  {label:<20} {h or ''}")
print()
print("有意不做（不是缺口）：")
print("  · 回写必需证据边 —— 学习只影响假设的生成与排序，绝不放松证据")
print("    要求。让 L3 去改 required 边等于让系统学会给自己降标准。")
print("  · 直接改种子图 —— 学到的东西存成 overlay，才分得清哪些是人写的")
print("    ground truth、哪些是学来的，出问题也能单独回滚。")

print()
print("=" * 76)
print("当前案例库实际内容")
print("=" * 76)
r = subprocess.run(
    ["python3", "-c",
     "import sys; sys.path.insert(0,'.'); "
     "from knowledge import case_store as cs; print(cs.library_stats())"],
    cwd=REPO, capture_output=True, text=True)
print(" ", (r.stdout or r.stderr).strip()[:300])
