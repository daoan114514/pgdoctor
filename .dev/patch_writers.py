"""写入侧也要记下场景版本，否则新学的条目一加载就被自己作废。"""
from pathlib import Path

REPO = Path("/mnt/c/Users/86173/Documents/github/pgdoctor")
p = REPO / "knowledge/evolution.py"
s = p.read_text(encoding="utf-8")

# ── L2 playbook
s = s.replace(
    """    pb.updated_at = time.time()
    pbs[rc] = pb
    save_playbooks(pbs)""",
    """    pb.learned_under = current_revisions().get(rc, 1)
    pb.updated_at = time.time()
    pbs[rc] = pb
    save_playbooks(pbs)""")

# ── L3 先验调整
OLD_BUMP_TAIL = None
import re
m = re.search(r"def _bump\(d: GraphDelta, rc: str, amount: float\) -> None:\n(.*?)\n\n",
              s, re.S)
assert m, "_bump 未找到"
body = m.group(1)
if "learned_under" not in body:
    s = s.replace(body, body + "\n    d.learned_under[rc] = current_revisions().get(rc, 1)")
    print("evolution.py: _bump 记录场景版本")

# ── L4 查询库
m = re.search(r"(\s+)qs\.root_causes\[(\w+)\] = qs\.root_causes\.get\(\2, 0\) \+ 1", s)
if m:
    indent, var = m.group(1), m.group(2)
    s = s.replace(
        m.group(0),
        m.group(0) +
        f"{indent}qs.learned_under[{var}] = current_revisions().get({var}, 1)")
    print("evolution.py: record_queries 记录场景版本")
else:
    print("!! record_queries 里没找到 root_causes 累加，需人工确认")

p.write_text(s, encoding="utf-8")
print("done")
