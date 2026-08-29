"""评测台自己的体检 —— 离线，进回归。

19 项回归全部在测 agent 和安全层，没有一项在监督评测台本身。而评测台
出问题比 agent 出问题更难发现：agent 越界有 ESC 和安全门盯着，评测台
坏了只有人工去数 JSON 才知道。实测已经吃过两次：

  · 两条证据从来没有工具产出过，long_idle_transaction 结构上无法诊断
  · 注入验证有竞态，第 3 轮静默丢掉 20 例，跑批照常打印"完成"

这里查的都是机械可判定、且一旦出错就会让结论失真的东西。
"""
import ast
import re
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from knowledge.causal_graph import graph as G
from sandbox import metrics
from sandbox.env import _load_injectors

ROOT = Path(__file__).resolve().parent.parent
SCEN = ROOT / "sandbox/scenarios"
LOCK = SCEN / ".instances.lock"


def fingerprint(sp: dict) -> str:
    """实例定义的指纹：注入参数 + 负载 + 判据。

    这三样决定"这是哪个实例"。note、difficulty 这类描述性字段不算进去 ——
    改注释不该逼人 bump 版本号。
    """
    import hashlib
    import json

    w = sp.get("workload", {}) or {}
    core = {
        "inject": sp.get("inject"),
        "concurrency": w.get("concurrency"),
        "hot_query": w.get("hot_query"),
        "canary": w.get("canary_queries"),
        "trigger": sp.get("trigger"),
        "success": sp.get("success"),
    }
    blob = json.dumps(core, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]

fails, warns = [], []


def check(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'FAIL'}  {name}   {detail}")
    if not cond:
        fails.append(name)


def warn(name, cond, detail=""):
    print(f"  {'PASS' if cond else 'WARN'}  {name}   {detail}")
    if not cond:
        warns.append(name)


specs = {}
for f in sorted(SCEN.glob("*.yaml")):
    specs[f.stem] = yaml.safe_load(f.read_text(encoding="utf-8"))
INJ = _load_injectors()
g = G.load()
ROOTS = {n for n, d in g.nodes(data=True) if d.get("kind") == "RootCause"}
EVID = {n for n, d in g.nodes(data=True) if d.get("kind") == "Evidence"}

print(f"场景 {len(specs)} 个，注入器 {len(INJ)} 个\n")

# ══ 1. 场景能跑起来 ═══════════════════════════════════════
print("[1] 每个场景都得能真的跑起来")
missing_inj = [s["id"] for s in specs.values() if s["fault_class"] not in INJ]
check("fault_class 都有注入器", not missing_inj, missing_inj)

bad_ids = [k for k, s in specs.items() if s.get("id") != k]
check("id 与文件名一致", not bad_ids, bad_ids)

# ══ 2. 判分表达式可解析 ═══════════════════════════════════
# 表达式写错不会报错，只会恒为 False —— 那样告警永不触发、成功永不达成，
# 而跑批看起来一切正常。
print("\n[2] 告警与成功判据必须可解析")
FAKE = metrics.KPI(p50_ms=1, p95_ms=2, p99_ms=3, qps=4, errors=5,
                   cpu_pct=6, samples=7)
bad_expr = []
for k, s in specs.items():
    for path, expr in (("trigger.alert", s.get("trigger", {}).get("alert")),
                       ("success.outcome",
                        s.get("success", {}).get("outcome"))):
        if not expr:
            bad_expr.append(f"{k}.{path} 缺失")
            continue
        try:
            metrics.eval_expr(expr, FAKE)
        except Exception as exc:
            bad_expr.append(f"{k}.{path}: {type(exc).__name__}")
check("表达式都能求值", not bad_expr, bad_expr[:5])

# 引用的字段必须真的存在于 KPI 上，否则永远取不到值
known = set(FAKE.as_dict())
bad_field = []
for k, s in specs.items():
    for path, expr in (("alert", s.get("trigger", {}).get("alert", "")),
                       ("outcome", s.get("success", {}).get("outcome", ""))):
        for tok in re.findall(r"\b([a-z_][a-z_0-9]*)\s*[<>=!]", expr):
            if tok not in known:
                bad_field.append(f"{k}.{path}: {tok}")
check("引用的 KPI 字段都存在", not bad_field, bad_field[:5])

# ══ 3. 判分与知识必须一致 ═════════════════════════════════
# 这一格错了最难发现：agent 修对了，判分却说没修好。
print("\n[3] acceptable_fixes 要能匹配图上该根因的修复模板")
bad_fix = []
for k, s in specs.items():
    rc = s["fault_class"]
    pats = [af["pattern"] for af in
            s.get("ground_truth", {}).get("acceptable_fixes", [])]
    tpls = [f.get("template", "") for f in G.fixes_for(rc)]
    if not pats:
        bad_fix.append(f"{k}: 没有 acceptable_fixes")
        continue
    if not tpls:
        continue                        # 图上没修复，另有 lint 管
    # 占位符要用**这个场景自己**声明的表和列去填。第一版把 {cols} 写死成
    # "user_id, status"，于是换了一个丢 created_at 索引的 eval 场景之后，
    # 探针串永远匹配不上它的正则 —— 报的是假失败。
    inj = s.get("inject", {})
    cols = ", ".join(inj.get("columns", []) or ["status"])
    tbl = inj.get("table", "orders")
    probe = " ; ".join(
        t.replace("{table}", tbl).replace("{pid}", "123")
         .replace("{cols}", cols).replace("{name}", "idx_x")
         .replace("{value}", "64MB").replace("{slot}", "s1")
         .replace("{gid}", "g1") for t in tpls)
    if not any(re.search(p, probe, flags=re.I) for p in pats):
        bad_fix.append(f"{k}: {pats} 匹配不上图上的 {tpls}")
check("判分正则与图上修复模板对得上", not bad_fix, bad_fix[:4])

print("\n[4] ground_truth 引用的名字都得在图上")
bad_ref = []
for k, s in specs.items():
    gt = s.get("ground_truth", {})
    if s["fault_class"] not in ROOTS:
        bad_ref.append(f"{k}.fault_class={s['fault_class']}")
    for e in gt.get("required_evidence", []) or []:
        if e not in EVID:
            bad_ref.append(f"{k}.required_evidence={e}")
    for c in gt.get("competing_hypotheses", []) or []:
        if c not in ROOTS:
            bad_ref.append(f"{k}.competing={c}")
check("名字都是图上的节点", not bad_ref, bad_ref[:5])

# 场景声明的必需证据要和图一致，否则 ESC 查的和场景说的不是一回事
mismatch = []
for k, s in specs.items():
    declared = set(s.get("ground_truth", {}).get("required_evidence", []) or [])
    ongraph = set(G.required_evidence(s["fault_class"]))
    if declared and declared != ongraph:
        mismatch.append(f"{k}: 场景 {sorted(declared)} vs 图 {sorted(ongraph)}")
# 硬检查而非警告：没有任何代码读场景里这个字段，所有读取方都走
# G.required_evidence()。也就是说它是纯文档 —— 而漂移了的文档比
# 没有更糟，读的人会信它。实测 stale_statistics 的场景一直写着已被
# 项目自己推翻的判据（时间戳而非偏差），而同一个文件的 note 早就
# 写对了，自相矛盾却没人发现。
check("场景与图的必需证据一致", not mismatch, mismatch[:3])

# ══ 5. 随机化真的生效 ═════════════════════════════════════
# 「参数化随机、防背答案」是设计稿里的铁律，但它曾经一行都没落地：
# 所有 params(rng) 都把 rng 拿到手就扔了。
print("\n[5] 参数化随机必须真的生效")
import random
flat = []
for k, s in specs.items():
    inj = INJ[s["fault_class"]](s)
    # 用 repr 做指纹：params 里有 list（如 columns），不可哈希
    seen = {repr(sorted(inj.params(random.Random(i)).items()))
            for i in range(8)}
    if len(seen) == 1:
        flat.append(k)
warn("换种子参数会变", not flat,
     f"随机化面为零: {flat}" if flat else "")

# ══ 6. train / eval 成对且不是同一个实例 ══════════════════
print("\n[6] train / eval 要成对，且不能是同一个实例")
by_fault = {}
for k, s in specs.items():
    by_fault.setdefault(s["fault_class"], {})[s.get("split")] = s
lonely = [f for f, d in by_fault.items() if set(d) != {"train", "eval"}]
check("每类故障都有 train 与 eval", not lonely, lonely)

same = []
for f, d in by_fault.items():
    if set(d) != {"train", "eval"}:
        continue
    # 实例定义 = 注入参数 + 负载。只比 inject 会漏掉"注入相同但负载不同"
    # 的情况，而那确实是两个实例。反过来更要紧：我曾经只改 inject 里的
    # leave_free 就让这条检查报 PASS，可那个改动把场景改废了 ——
    # lint 验证的是"不同"，验证不了"有效"，后者只有 scenario_probe 能查。
    def _inst(sp):
        w = dict(sp.get("workload", {}) or {})
        return (repr(sorted((sp.get("inject") or {}).items())),
                w.get("concurrency"), w.get("hot_query"))

    if _inst(d["train"]) == _inst(d["eval"]):
        same.append(f)
warn("train 与 eval 的注入参数不同", not same,
     f"参数完全相同: {same}" if same else "")

# ══ 7. 改了实例定义就得 bump revision ═════════════════════
# 改评测集和改 agent 不是一回事：后者可以随便迭代，前者每改一次就切断
# 一次可比性。实测踩过 —— missing_index_eval_v1 从"丢 user_id,status
# 索引"改成"丢 created_at 索引"，已经是另一个实例，revision 却还是 1，
# 之前 1500 例对应旧实例、以后的对应新实例，文件上看不出任何区别。
print("\n[7] 改了实例定义必须 bump revision")
if not LOCK.exists():
    warn("实例锁存在", False, "缺 .instances.lock，跑 python3 .dev/relock.py 生成")
else:
    lock = yaml.safe_load(LOCK.read_text(encoding="utf-8")) or {}
    drifted, unlocked = [], []
    for k, sp in specs.items():
        sid = sp["id"]
        rec = lock.get(sid)
        if not rec:
            unlocked.append(sid)
            continue
        if fingerprint(sp) != rec["fingerprint"] and \
                sp.get("revision", 1) == rec["revision"]:
            drifted.append(f"{sid}(rev {rec['revision']})")
    check("实例定义改了就 bump 了 revision", not drifted,
          f"{drifted} 变了但 revision 没动；改对了就跑 .dev/relock.py"
          if drifted else "")
    check("所有场景都在锁里", not unlocked,
          f"{unlocked} 不在锁里，跑 .dev/relock.py" if unlocked else "")

print()
print("=" * 66)
if fails:
    print(f"HARNESS LINT: FAIL {fails}")
elif warns:
    print(f"HARNESS LINT: PASS（{len(warns)} 条警告待人工判断）")
else:
    print("HARNESS LINT: PASS")
sys.exit(1 if fails else 0)
