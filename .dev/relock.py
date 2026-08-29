"""bump 过 revision 之后，重新生成实例锁。

只有在你确实有意改了实例定义、并且已经 bump 了 revision 之后才跑它。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness_lint import fingerprint, LOCK, SCEN   # noqa: E402

import yaml

lock = {}
for f in sorted(SCEN.glob("*.yaml")):
    sp = yaml.safe_load(f.read_text(encoding="utf-8"))
    lock[sp["id"]] = {"revision": sp.get("revision", 1),
                      "fingerprint": fingerprint(sp)}
LOCK.write_text(
    "# 评测集的实例锁。改了实例定义（注入参数/负载/判据）就必须 bump\n"
    "# 场景里的 revision，否则 harness_lint 报错 —— 否则以后没人分得清\n"
    "# 哪批结果对应哪个版本。改完跑 python3 .dev/relock.py 更新本文件。\n"
    + yaml.safe_dump(lock, allow_unicode=True, sort_keys=True),
    encoding="utf-8")
print(f"锁已更新: {len(lock)} 个场景")
