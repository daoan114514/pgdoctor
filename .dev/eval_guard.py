#!/usr/bin/env python3
"""LLM 跑批守护：一个场景一次 run_suite 调用，跑完立刻落盘，自己看门、自己等额度。

为什么需要它（三条都是实测栽出来的）：
1. run_suite 原来只在整批结束时写结果文件（现在每个 episode 后也写，但守护仍按场景切分，
   一个 fault_class 一次调用、各自独立 --tag guard_<fault>），被杀最多损失当前这个。
2. 只看进程在不在不够：SDK 的异步清理 bug 会让子进程停在 do_epoll_wait 一个 token 都不发。
   看门狗按"最新 step 文件 / episode_state.json 是否还在增长"判活（取证轮进行中只写后者）。
3. 等额度的时间会把 golden 时间锚拖过上限，每个场景开跑前都单独查锚。

只认自己产出的 guard_*.json：eval/results/ 下有 8 月的旧 llm 结果，扫全部会把它们误当成已完成。
infra_failures>0 或 unusable 的结果不算已完成，会重跑。

用法（在 WSL 里）：bash .dev/run_guard.sh   或   python3 .dev/eval_guard.py
环境变量：GUARD_MAX_STEPS（默认 30）、GUARD_SPLIT（默认 eval）。
"""
import fcntl
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "eval" / "results"
LOGDIR = ROOT / "traces" / "eval_guard"
LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = LOGDIR / "guard.log"
# 锁文件放 Linux 侧 /tmp（ext4），不放 /mnt/c：9P 上的文件锁语义不可靠
LOCK_PATH = Path("/tmp/pgdoctor_eval_guard.lock")
MAX_STEPS = int(os.getenv("GUARD_MAX_STEPS", "30"))
SPLIT = os.getenv("GUARD_SPLIT", "eval")
DRIFT_REANCHOR_H = 4.0
QUOTA_WAIT_S = 600
STALL_MIN = 15            # 最新 step / 状态文件超过这么久没动 -> 判卡死
EPISODE_CAP_S = 90 * 60   # 单场景硬上限；真死锁由 STALL_MIN 负责，这只是"慢而不死"的兜底
MAX_WALL_S = 20 * 3600
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

_LOCK_FH = None


def log(msg: str) -> None:
    line = f"[{time.strftime('%F %T')}] {msg}"
    print(line, flush=True)
    with LOG.open("a", encoding="utf-8") as f:
        f.write(line + "\n")


def acquire_single_instance() -> bool:
    """同一时刻只允许一个守护：两个会互相 reset golden，结果全部作废且不报错。"""
    global _LOCK_FH
    _LOCK_FH = open(LOCK_PATH, "a+")
    try:
        fcntl.flock(_LOCK_FH, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    _LOCK_FH.seek(0)
    _LOCK_FH.truncate()
    _LOCK_FH.write(str(os.getpid()))
    _LOCK_FH.flush()
    return True


def targets() -> dict:
    import yaml
    out = {}
    for p in sorted((ROOT / "sandbox" / "scenarios").glob("*.yaml")):
        d = yaml.safe_load(p.read_text(encoding="utf-8"))
        if d.get("split") != SPLIT or str(d.get("status") or "") == "excluded":
            continue
        out[d["fault_class"]] = p.stem
    return out


def usable_episodes(stems: set) -> dict:
    """每个目标场景最新的一条可用 episode（口径与 run_suite 分母一致 + infra_failures==0）。"""
    found = {}
    for f in RESULTS.glob("guard_*.json"):
        try:
            d = json.loads(f.read_text(encoding="utf-8"))
        except Exception:
            continue
        if d.get("policy") != "llm":
            continue
        mt = f.stat().st_mtime
        for e in d.get("episodes", []):
            if e.get("scenario") not in stems:
                continue
            if (e.get("unusable") or not e.get("fired")
                    or int(e.get("infra_failures") or 0) > 0):
                continue
            prev = found.get(e["scenario"])
            if prev is None or mt > prev[0]:
                found[e["scenario"]] = (mt, e)
    return {k: v[1] for k, v in found.items()}


def _newest_episode_dir(stem=None):
    pattern = f"ep_{stem}_*" if stem else "ep_*"
    best, best_mt = None, 0.0
    for d in (ROOT / "traces").glob(pattern):
        try:
            mt = d.stat().st_mtime
        except OSError:
            continue
        if mt > best_mt and d.is_dir():
            best, best_mt = d, mt
    return best


def newest_activity_age_min(stem=None):
    """最新 episode 里 step 文件与 episode_state.json 中最新者的年龄（分钟）。"""
    d = _newest_episode_dir(stem)
    if d is None:
        return None
    newest = 0.0
    for p in list(d.glob("step_*.json")) + [d / "episode_state.json"]:
        try:
            newest = max(newest, p.stat().st_mtime)
        except OSError:
            continue
    return None if newest == 0.0 else (time.time() - newest) / 60.0


def _pids(pattern: str) -> list[int]:
    r = subprocess.run(["pgrep", "-f", pattern], capture_output=True, text=True)
    return [int(x) for x in r.stdout.split()] if r.returncode == 0 else []


def _kill_tree(proc=None) -> None:
    """杀 run_suite、claude 子 agent 和孤儿负载。run_suite 被 SIGKILL 时 env.close()
    不会执行，sandbox.workload 会变成孤儿继续打库（实测活了 5 小时）。"""
    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    for pat in ("eval[.]run_suite", "_bundled/claud[e]", "sandbox[.]workload"):
        for pid in _pids(pat):
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
    if proc is not None:
        try:
            proc.wait(timeout=30)
        except Exception:
            pass
    time.sleep(3)


def kill_stale_runs() -> None:
    if not _pids("eval[.]run_suite"):
        for pid in _pids("sandbox[.]workload"):
            log(f"  清掉孤儿负载 {pid}")
            os.kill(pid, signal.SIGKILL)
        return
    while _pids("eval[.]run_suite"):
        age = newest_activity_age_min()
        if age is None or age >= STALL_MIN:
            log(f"  已有跑批停滞 {age if age is None else round(age)} 分钟，判定卡死，清理")
            _kill_tree()
            return
        log(f"  有跑批在跑且仍在推进（最新活动 {age:.0f} 分钟前），等它")
        time.sleep(60)
    log("  已有跑批自行结束")


def drift_h() -> float:
    from sandbox.env import anchor_drift_h
    return float(anchor_drift_h() or 0.0)


def reanchor() -> bool:
    log("  重锚 ...")
    r = subprocess.run([sys.executable, ".dev/reanchor_time.py", "--force"],
                       capture_output=True, text=True)
    for t in (r.stdout or "").strip().splitlines()[-2:]:
        log("    " + t)
    return r.returncode == 0


def model_ok() -> bool:
    """探针放子进程跑：同一进程里第二次 asyncio.run 会撞 SDK 的 aclose() 状态，误报不可用。"""
    r = subprocess.run(
        [sys.executable, "-c",
         "from eval.run_suite import _model_reachable; raise SystemExit(0 if _model_reachable() else 1)"],
        capture_output=True, text=True, timeout=300)
    return r.returncode == 0


def run_one(fault: str, stem: str) -> None:
    tag = f"guard_{fault}"
    log(f"  >>> {stem}  (--faults {fault} --tag {tag})")
    cmd = [sys.executable, "-m", "eval.run_suite", "--policy", "llm",
           "--split", SPLIT, "--faults", fault, "--tag", tag,
           "--max-steps", str(MAX_STEPS)]
    out = LOGDIR / f"{tag}.txt"
    t_start = time.time()
    with out.open("w", encoding="utf-8") as f:
        proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
        while proc.poll() is None:
            time.sleep(60)
            elapsed = time.time() - t_start
            if elapsed > EPISODE_CAP_S:
                log(f"    看门狗：单场景超过 {EPISODE_CAP_S // 60} 分钟上限，清理")
                _kill_tree(proc)
                break
            if elapsed / 60.0 < STALL_MIN:
                continue
            try:
                age = newest_activity_age_min(stem)
            except Exception as exc:
                log(f"    看门狗检查出错（本轮跳过）: {type(exc).__name__}: {exc}")
                continue
            if age is not None and age >= STALL_MIN:
                log(f"    看门狗：{age:.0f} 分钟没有任何活动，判定死锁，清理")
                _kill_tree(proc)
                break
    for line in out.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.strip().startswith("fired=") or line.startswith("成本合计"):
            log("    " + line.strip())


def merge(eps: dict, out_name: str = "llm_eval_guarded") -> Path:
    from eval.metrics_v2 import aggregate_episode_metrics
    ordered = [eps[k] for k in sorted(eps)]
    usable = [e for e in ordered if e.get("fired") and not e.get("unusable")]
    path = RESULTS / f"{out_name}.json"
    path.write_text(json.dumps({
        "tag": out_name, "policy": "llm", "split": SPLIT,
        "use_esc": True, "use_cases": True,
        "learned_layers": ["l1", "l2", "l3", "l4"],
        "note": f"由 .dev/eval_guard.py 合并（单场景独立跑批），max_steps={MAX_STEPS}",
        "metrics_v2": aggregate_episode_metrics(usable),
        "episodes": ordered,
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"已合并 -> {path}")
    return path


def main() -> int:
    t0 = time.time()
    if not acquire_single_instance():
        log(f"已有另一个守护实例持有 {LOCK_PATH}，本实例退出")
        return 5
    tgt = targets()
    stems = set(tgt.values())
    log("=" * 60)
    log(f"守护启动 pid={os.getpid()} | split={SPLIT} | 目标 {len(stems)} 个场景 | max_steps={MAX_STEPS}")
    kill_stale_runs()
    fails: dict = {}
    while True:
        if time.time() - t0 > MAX_WALL_S:
            log("超过 20 小时上限，退出")
            return 3
        have = usable_episodes(stems)
        missing = {fc: st for fc, st in tgt.items() if st not in have}
        log(f"已有可用 {len(have)}/{len(stems)}" + (" -> " + ", ".join(sorted(have)) if have else ""))
        if not missing:
            log("全部场景都已有可用 episode")
            merge(have)
            return 0
        log(f"待补 {len(missing)} 个: {sorted(missing.values())}")
        fault, stem = sorted(missing.items(), key=lambda kv: (fails.get(kv[0], 0), kv[0]))[0]
        d = drift_h()
        log(f"时间锚漂移 {d:.2f}h")
        if d > DRIFT_REANCHOR_H and not reanchor():
            log("重锚失败，退出")
            return 4
        waited = 0
        while not model_ok():
            if time.time() - t0 > MAX_WALL_S:
                log("等额度期间超时，退出")
                return 3
            waited += QUOTA_WAIT_S
            log(f"  模型不可用（额度/限流/登录），{QUOTA_WAIT_S // 60} 分钟后再探（累计 {waited // 60} 分钟）")
            time.sleep(QUOTA_WAIT_S)
        if waited:
            log(f"  额度恢复（等了 {waited // 60} 分钟），回到开头重新查锚")
            continue
        before = len(have)
        run_one(fault, stem)
        if len(usable_episodes(stems)) <= before:
            fails[fault] = fails.get(fault, 0) + 1
            log(f"  {stem} 没拿到可用结果（累计失败 {fails[fault]} 次），排到后面，先试别的场景")
            time.sleep(60)
        else:
            fails.pop(fault, None)


if __name__ == "__main__":
    raise SystemExit(main())
