"""场景改版则相关知识失效。

沙箱侧的 bug 修好后发现 knowledge/learned/playbooks.yaml 里
lock_contention 是 fail_count=2 / success_count=0 —— 系统学到了
"锁竞争修不好"，而那两次失败根本不是 agent 的问题，是负载生成器把
成功的 UPDATE 记成了失败。自进化系统会忠实地把环境的 bug 学进去。

只手工清一次不解决问题：场景以后还会改。做成机制 —— 每个场景带
revision，学到的条目记下自己是在哪一版下学的，版本对不上就作废。
这也正是 M8 里"版本失效（stale）"那条记忆治理规则，只是原先只对
案例库生效，没覆盖 L2/L3/L4。
"""
from pathlib import Path

import yaml

REPO = Path("/mnt/c/Users/86173/Documents/github/pgdoctor")

# ── 1. 场景标注 revision：这一轮重建过的两类 +1 ──────────────────
BUMPED = {"lock_contention", "stale_statistics"}
for f in sorted((REPO / "sandbox/scenarios").glob("*.yaml")):
    d = yaml.safe_load(f.read_text(encoding="utf-8"))
    if "revision" not in d:
        d["revision"] = 2 if d.get("fault_class") in BUMPED else 1
        # revision 放在 fault_class 后面更好读
        out = {}
        for k, v in d.items():
            if k != "revision":
                out[k] = v
            if k == "fault_class":
                out["revision"] = d["revision"]
        f.write_text(yaml.safe_dump(out, allow_unicode=True, sort_keys=False),
                     encoding="utf-8")
        print(f"  {f.name}: revision={d['revision']}")

# ── 2. evolution 支持按 revision 作废 ───────────────────────────
p = REPO / "knowledge/evolution.py"
s = p.read_text(encoding="utf-8")

HELPER = '''
def current_revisions() -> dict[str, int]:
    """每个故障类当前的场景版本号。

    场景语义变了（判据、热查询、注入方式），在旧版本下学到的东西就不
    再成立。踩过的坑：负载生成器有 bug 时，系统忠实地学到了"锁竞争修
    不好"（fail_count=2 / success_count=0）—— 自进化会把环境的 bug 一并
    学进去，而且学得很认真。
    """
    revs: dict[str, int] = {}
    for f in (ROOT / "sandbox" / "scenarios").glob("*.yaml"):
        try:
            d = yaml.safe_load(f.read_text(encoding="utf-8")) or {}
        except Exception:
            continue
        fc = d.get("fault_class")
        if fc:
            revs[fc] = max(revs.get(fc, 1), int(d.get("revision", 1)))
    return revs


def _is_stale(root_cause: str, learned_under: int) -> bool:
    return learned_under < current_revisions().get(root_cause, 1)


'''
s = s.replace("\ndef _pb_path() -> Path:", HELPER + "\ndef _pb_path() -> Path:")

# Playbook 记下学习时的场景版本
s = s.replace(
    """    success_count: int = 0
    fail_count: int = 0
    updated_at: float = field(default_factory=time.time)""",
    """    success_count: int = 0
    fail_count: int = 0
    # 在哪一版场景下学到的。场景改版后这条就不再成立，见 current_revisions()
    learned_under: int = 1
    updated_at: float = field(default_factory=time.time)""")

s = s.replace(
    '''    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {k: Playbook(**v) for k, v in raw.items()}''',
    '''    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {}
    for k, v in raw.items():
        pb = Playbook(**v)
        if _is_stale(pb.root_cause, pb.learned_under):
            continue          # 场景已改版，这条经验作废而不是继续拿来用
        out[k] = pb
    return out''')

# QueryStat 同理
s = s.replace(
    """    used_in_success: int = 0
    used_in_failure: int = 0
    root_causes: dict = field(default_factory=dict)   # 它帮着确认了哪些根因""",
    """    used_in_success: int = 0
    used_in_failure: int = 0
    root_causes: dict = field(default_factory=dict)   # 它帮着确认了哪些根因
    # 各根因上的统计分别是在哪一版场景下攒的
    learned_under: dict = field(default_factory=dict)""")

s = s.replace(
    '''    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    return {k: QueryStat(**v) for k, v in raw.items()}''',
    '''    raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    out = {}
    for k, v in raw.items():
        qs = QueryStat(**v)
        # 只摘掉已改版根因上的计数，工具本身的历史不必整条丢弃
        for rc in [r for r in qs.root_causes
                   if _is_stale(r, int(qs.learned_under.get(r, 1)))]:
            qs.root_causes.pop(rc, None)
            qs.learned_under.pop(rc, None)
        out[k] = qs
    return out''')

# GraphDelta：先验调整按根因作废
s = s.replace(
    """    observed: dict = field(default_factory=dict)         # root_cause: {hit, miss}
    updated_at: float = field(default_factory=time.time)""",
    """    observed: dict = field(default_factory=dict)         # root_cause: {hit, miss}
    learned_under: dict = field(default_factory=dict)    # root_cause: 场景版本
    updated_at: float = field(default_factory=time.time)""")

s = s.replace(
    '''    return GraphDelta(**(yaml.safe_load(p.read_text(encoding="utf-8")) or {}))''',
    '''    d = GraphDelta(**(yaml.safe_load(p.read_text(encoding="utf-8")) or {}))
    for rc in [r for r in d.prior_adj
               if _is_stale(r, int(d.learned_under.get(r, 1)))]:
        # 先验调整是在已作废的场景下学的，退回手工种子图的值
        d.prior_adj.pop(rc, None)
        d.observed.pop(rc, None)
        d.learned_under.pop(rc, None)
    return d''')

p.write_text(s, encoding="utf-8")
print("evolution.py: L2/L3/L4 支持按场景版本作废")
