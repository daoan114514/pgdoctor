"""核实 L1-L4 自进化各层的实现情况 —— 看代码，不看印象。"""
import subprocess
from pathlib import Path

REPO = Path("/home/daoan/pgdoctor")


def has(pattern, *paths):
    """在指定文件里找符号，返回命中的文件名。"""
    hits = []
    for p in paths:
        f = REPO / p
        if f.exists() and pattern in f.read_text(encoding="utf-8"):
            hits.append(p)
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
    ("playbook 数据结构", "playbook", ["knowledge/case_store.py",
                                       "agent/loop.py", "knowledge/skills.py"]),
    ("取证路径已记录", "investigation_path", ["knowledge/case_store.py"]),
    ("路径被复用", "investigation_path", ["agent/loop.py",
                                          "agent/llm_policy.py"]),
]:
    h = has(pat, *files)
    print(f"  {'已实现' if h else '缺失  '}  {label:<20} {h or ''}")
print(f"  knowledge/skills.py 存在: {(REPO / 'knowledge/skills.py').exists()}")

print()
print("=" * 76)
print("L3  失败驱动：从失败反思，更新假设生成的先验与检查清单")
print("=" * 76)
for label, pat, files in [
    ("失败尝试被记录", "record_attempt", ["agent/episode_state.py"]),
    ("失败进案例库(负例)", "failed_attempts", ["knowledge/case_store.py"]),
    ("回写因果图 likelihood", "update_likelihood", [
        "knowledge/causal_graph/graph.py", "agent/loop.py"]),
    ("回写必需证据边", "add_required_evidence", [
        "knowledge/causal_graph/graph.py"]),
    ("图可写回磁盘", "def save", ["knowledge/causal_graph/graph.py"]),
]:
    h = has(pat, *files)
    print(f"  {'已实现' if h else '缺失  '}  {label:<20} {h or ''}")

print()
print("=" * 76)
print("L4  诊断查询库：沉淀有效的诊断 SQL，越用越会查")
print("=" * 76)
for label, pat, files in [
    ("DiagnosticQuery 节点", "DiagnosticQuery", [
        "knowledge/causal_graph/nodes.yaml",
        "knowledge/causal_graph/graph.py"]),
    ("查询库存储", "query_library", ["knowledge/"]),
    ("有效查询被沉淀", "record_query", ["knowledge/", "agent/"]),
]:
    h = has(pat, *files)
    print(f"  {'已实现' if h else '缺失  '}  {label:<20} {h or ''}")
print(f"  knowledge/query_library.py 存在: "
      f"{(REPO / 'knowledge/query_library.py').exists()}")

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
