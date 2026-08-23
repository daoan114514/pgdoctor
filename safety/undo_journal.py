"""回滚日志 —— WAL 式"先写后做"。

执行之前先把回滚记录落盘并 fsync，之后才动数据库。这样即使 agent
进程当场崩掉、上下文彻底不存在，journal 里那条 APPLIED 记录还在，
重启后扫一遍就知道有个变更需要撤销。

回滚能力绝不依赖上下文：撤销由这里驱动，且执行者是安全门而非 agent
（agent 全程只有只读连接）。
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JOURNAL = ROOT / "traces" / "undo_journal.jsonl"


class UndoStatus(str, Enum):
    PENDING = "PENDING"        # 已落盘，尚未执行
    APPLIED = "APPLIED"        # 已执行，未撤销
    REVERTED = "REVERTED"      # 已撤销
    FAILED = "FAILED"          # 正向执行失败
    UNDO_FAILED = "UNDO_FAILED"  # 撤销失败 —— 最危险，必须人工介入


@dataclass
class UndoRecord:
    undo_id: str
    episode_id: str
    action_type: str
    forward_sql: str
    undo_sql: str
    status: str = UndoStatus.PENDING.value
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    error: str = ""


def _append_line(rec: dict) -> None:
    JOURNAL.parent.mkdir(parents=True, exist_ok=True)
    with open(JOURNAL, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        f.flush()
        os.fsync(f.fileno())   # 落盘之后才允许继续，否则崩溃会丢记录


# 显式声明"这个动作撤不回来"的标记，不是可执行 SQL。
# 终止会话、重载配置这类动作本质上不可撤销，强求一条回滚语句只会
# 逼出假的，反而制造"以为能回滚"的错觉。
IRREVERSIBLE = "IRREVERSIBLE"


def is_irreversible(undo_sql: str) -> bool:
    return (undo_sql or "").strip().upper() == IRREVERSIBLE


def make_idempotent(undo_sql: str) -> str:
    """撤销语句必须幂等，否则崩溃恢复时重复执行会二次出错。

    注意 IF EXISTS 必须放在 CONCURRENTLY 之后：
        DROP INDEX CONCURRENTLY IF EXISTS x      正确
        DROP INDEX IF EXISTS CONCURRENTLY x      语法错误
    最初用 split 拼接就踩了这个顺序，导致回滚语句本身非法、撤销失败。
    """
    s = undo_sql.strip().rstrip(";")
    if is_irreversible(s):
        return IRREVERSIBLE          # 原样保留，执行层会跳过
    if "IF EXISTS" in s.upper():
        return s
    m = re.match(r"^(DROP\s+INDEX)(\s+CONCURRENTLY)?\s+(.+)$", s,
                 re.IGNORECASE | re.DOTALL)
    if m:
        return f"{m.group(1)}{m.group(2) or ''} IF EXISTS {m.group(3)}"
    return s


def append(episode_id: str, action_type: str, forward_sql: str,
           undo_sql: str) -> UndoRecord:
    rec = UndoRecord(
        undo_id=f"undo_{int(time.time() * 1000)}",
        episode_id=episode_id,
        action_type=action_type,
        forward_sql=forward_sql,
        undo_sql=make_idempotent(undo_sql),
    )
    _append_line(asdict(rec))
    return rec


def mark(undo_id: str, status: UndoStatus, error: str = "") -> None:
    _append_line({"undo_id": undo_id, "status": status.value,
                  "updated_at": time.time(), "error": error, "_update": True})


def _replay() -> dict[str, dict]:
    """journal 是 append-only 的，回放得到每条记录的最终状态。"""
    if not JOURNAL.exists():
        return {}
    out: dict[str, dict] = {}
    for line in JOURNAL.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            d = json.loads(line)
        except Exception:
            continue
        uid = d.get("undo_id")
        if not uid:
            continue
        if d.get("_update"):
            if uid in out:
                out[uid].update({k: v for k, v in d.items() if k != "_update"})
        else:
            out[uid] = d
    return out


def get(undo_id: str) -> dict | None:
    return _replay().get(undo_id)


def unreverted() -> list[dict]:
    """已执行且未撤销的变更。

    注意：成功的修复本来就应该停在 APPLIED —— 它不是问题。
    崩溃恢复要看的是 needs_attention()。
    """
    return [r for r in _replay().values()
            if r.get("status") == UndoStatus.APPLIED.value]


def needs_attention() -> list[dict]:
    """真正需要人工过目的记录。

    PENDING     已落盘但执行结果未知（多半是执行中崩了）
    UNDO_FAILED 撤销失败 —— 最危险，库里留着一个撤不掉的变更

    最初把 APPLIED 也算进来，结果一次成功的修复就被当成遗留问题。
    不自动撤销任何记录：人可能已经手工处理过，盲目撤销会雪上加霜。
    """
    return [r for r in _replay().values()
            if r.get("status") in (UndoStatus.PENDING.value,
                                   UndoStatus.UNDO_FAILED.value)]


def episode_records(episode_id: str) -> list[dict]:
    return [r for r in _replay().values() if r.get("episode_id") == episode_id]
