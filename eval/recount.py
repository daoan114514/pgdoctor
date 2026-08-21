"""按正确口径重算历史跑批：额度耗尽作废的 episode 不计入三率。

这类重算正是轨迹落盘的意义 —— 口径改了不必重新花钱跑。
"""
import json
from pathlib import Path

R = Path("/home/daoan/pgdoctor/eval/results")
DEAD_HINTS = ("modelunavailable", "error result: success")


def classify(e):
    low = (e.get("error") or "").lower()
    if not e.get("fired"):
        return "告警未触发"
    if any(h in low for h in DEAD_HINTS):
        return "模型不可用"
    if "故障注入未生效" in (e.get("error") or ""):
        return "注入未生效"
    return "有效"


for tag in ("scripted_eval", "llm_eval", "llm_eval_fixed", "llm_eval_v3"):
    p = R / f"{tag}.json"
    if not p.exists():
        continue
    d = json.loads(p.read_text(encoding="utf-8"))
    eps = d["episodes"]
    groups = {}
    for e in eps:
        groups.setdefault(classify(e), []).append(e)
    valid = groups.get("有效", [])
    n = max(len(valid), 1)
    print("=" * 76)
    print(f"{tag}   ({d['policy']}, 共 {len(eps)} 个 episode)")
    for k, v in groups.items():
        if k != "有效":
            print(f"  作废[{k}]: {[x['fault_class'] for x in v]}")
    print(f"  有效 {len(valid)} 个: "
          f"Diagnosis {sum(x['diagnosis'] for x in valid)}/{n}  "
          f"Outcome {sum(x['outcome'] for x in valid)}/{n}  "
          f"SafePass {sum(x['safe_pass'] for x in valid)}/{n}")
    print(f"  成本 ${sum(x['cost_usd'] for x in eps):.2f}")
    for x in valid:
        print(f"    {x['fault_class']:<24} 声称={str(x['claimed']):<22} "
              f"D={'Y' if x['diagnosis'] else '.'} "
              f"O={'Y' if x['outcome'] else '.'} "
              f"S={'Y' if x['safe_pass'] else '.'} "
              f"步数={x['steps']}")
    print()


def merge(policy: str, tags: list[str] | None = None) -> dict:
    """跨跑批合并：每个场景取最新的一次有效结果。

    Pro 额度一个窗口只够跑两三个 episode，一次完整实验必然被切成
    几段。不能合并的话就永远拿不到完整的对照表。
    """
    files = sorted(R.glob("*.json"), key=lambda q: q.stat().st_mtime)
    best: dict[str, dict] = {}
    for f in files:
        if tags and f.stem not in tags:
            continue
        d = json.loads(f.read_text(encoding="utf-8"))
        if d.get("policy") != policy:
            continue
        for e in d["episodes"]:
            if classify(e) == "有效":
                best[e["scenario"]] = e | {"_from": f.stem}
    return best


if __name__ == "__main__":
    import sys as _sys

    if len(_sys.argv) > 1 and _sys.argv[1] == "merge":
        print()
        print("=" * 76)
        print("跨跑批合并（每个场景取最新的有效结果）")
        for pol in ("scripted", "llm"):
            m = merge(pol)
            if not m:
                continue
            n = max(len(m), 1)
            print(f"\n{pol}: {len(m)} 个场景有有效结果")
            for sc, e in sorted(m.items()):
                print(f"  {e['fault_class']:<24} 声称={str(e['claimed']):<22} "
                      f"D={'Y' if e['diagnosis'] else '.'} "
                      f"O={'Y' if e['outcome'] else '.'} "
                      f"S={'Y' if e['safe_pass'] else '.'} "
                      f"步数={e['steps']:<4} [{e['_from']}]")
            print(f"  合计  Diagnosis {sum(x['diagnosis'] for x in m.values())}/{n}"
                  f"  Outcome {sum(x['outcome'] for x in m.values())}/{n}"
                  f"  SafePass {sum(x['safe_pass'] for x in m.values())}/{n}")
