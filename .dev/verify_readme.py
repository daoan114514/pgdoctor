"""校验 README 里给出的命令是不是真的能跑 —— README 说得再好，
命令跑不通就是失信。"""
import re
import subprocess
import sys
from pathlib import Path

# 用脚本自身位置推仓库根，别写死绝对路径 —— 原先写死成一台机器上的
# WSL 路径，换台机器连 README 都读不到，这个校验就等于没有。
REPO = Path(__file__).resolve().parent.parent
txt = (REPO / "README.md").read_text(encoding="utf-8")

print(f"行数: {len(txt.splitlines())}")

ok = True
print("\n=== 引用的 .dev 脚本 ===")
for f in sorted(set(re.findall(r"\.dev/[A-Za-z0-9_]+\.(?:py|sh)", txt))):
    exists = (REPO / f).exists()
    ok &= exists
    print(f"  {'OK  ' if exists else '缺失'} {f}")

print("\n=== 引用的模块路径 ===")
for m in sorted(set(re.findall(r"python3 -m ([a-z_]+(?:\.[a-z_]+)*)", txt))):
    path = REPO / (m.replace(".", "/") + ".py")
    pkg = REPO / m.replace(".", "/") / "__init__.py"
    exists = path.exists() or pkg.exists()
    ok &= exists
    print(f"  {'OK  ' if exists else '缺失'} {m}")

print("\n=== 引用的函数是否真实存在 ===")
checks = [
    ("agent.investigator", "toolset_for"),
    ("knowledge.case_store", "search"),
    ("sandbox.scoring", "score_episode"),
    ("safety.shield", "inspect_sql"),
    ("agent.esc", "check"),
]
for mod, fn in checks:
    r = subprocess.run(
        # 用当前解释器而不是字面量 python3：Windows 上 python3 是应用商店
        # 的占位程序，会让存在的函数被误报成缺失。
        [sys.executable, "-c",
         f"import sys; sys.path.insert(0,'.'); "
         f"import {mod} as m; assert hasattr(m,'{fn}'); print('ok')"],
        cwd=REPO, capture_output=True, text=True)
    good = r.returncode == 0
    ok &= good
    print(f"  {'OK  ' if good else '缺失'} {mod}.{fn}")

print("\n=== 过时内容检查 ===")
stale = [
    ("模型不如基线的归因", "已被 2/4 vs 1/4 的结果推翻"),
    ("首版覆盖 8–10 类故障", "实际只有 4 类，属于夸大"),
    ("w3_unit.py", "该脚本不存在"),
    ("w3_e2e.py", "该脚本不存在"),
    ("llm 修复后 | 2 | **1/2**", "已被完整实验取代"),
]
for needle, why in stale:
    found = needle in txt
    ok &= not found
    print(f"  {'仍存在!' if found else 'OK  '} {needle}  ({why})")

print("\n" + "=" * 60)
print("README VERIFY:", "PASS" if ok else "FAIL")
