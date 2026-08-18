"""轨迹存储。

工具的原始输出全部落盘，上下文里只留一个 raw_ref 引用。
这是上下文管理第一道防线的另一半：萃取后的摘要进上下文，原文按需回取。

同时它也是 ESC 的证据来源——ESC 核验的是"实际跑了什么、拿到了什么"，
读的是这里的记录，而不是 agent 的自述，所以 agent 伪造不了。
"""
from __future__ import annotations

import json
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TRACE_DIR = ROOT / "traces"


class TraceStore:
    def __init__(self, episode_id: str | None = None):
        self.episode_id = episode_id or f"ep_{int(time.time())}"
        self.dir = TRACE_DIR / self.episode_id
        self.dir.mkdir(parents=True, exist_ok=True)
        self._seq = 0

    def record(self, tool: str, args: dict, raw: str, digest: dict) -> str:
        """存一次工具调用，返回 raw_ref。"""
        self._seq += 1
        ref = f"trace://{self.episode_id}/step_{self._seq:03d}"
        (self.dir / f"step_{self._seq:03d}.json").write_text(
            json.dumps(
                {
                    "ref": ref,
                    "ts": time.time(),
                    "tool": tool,
                    "args": args,
                    "digest": digest,
                    "raw": raw,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        return ref

    def fetch_raw(self, ref: str) -> str:
        """按需回取原文。99% 的情况下用不到，但 ESC 核验证据取值时需要。"""
        step = ref.rsplit("/", 1)[-1]
        p = self.dir / f"{step}.json"
        if not p.exists():
            raise KeyError(ref)
        return json.loads(p.read_text(encoding="utf-8"))["raw"]

    def all_steps(self) -> list[dict]:
        """整个 episode 的执行轨迹 —— ESC 的输入。"""
        out = []
        for p in sorted(self.dir.glob("step_*.json")):
            out.append(json.loads(p.read_text(encoding="utf-8")))
        return out
