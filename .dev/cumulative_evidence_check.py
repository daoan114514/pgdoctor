"""累计统计窗口差分与 UNKNOWN/ERROR 语义的离线回归测试。"""
from __future__ import annotations

import copy
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent import episode_state as episode_state_module
from agent import esc
from agent.episode_state import (EpisodeState, EvidenceStatus,
                                 evidence_is_observed)
from agent.state_machine import StateMachine
from agent.toolbox import Toolbox


DB_COUNTERS = {
    "deadlocks": 10,
    "temp_files": 4,
    "temp_bytes": 2 * 1048576,
    "xact_commit": 100,
    "xact_rollback": 2,
    "db_stats_reset": "2026-08-31T08:00:00+08:00",
}
CKPT_COUNTERS = {
    "ckpt_timed": 20,
    "ckpt_requested": 5,
    "ckpt_write_time_ms": 1000.0,
    "ckpt_sync_time_ms": 100.0,
    "ckpt_stats_reset": "2026-08-31T08:00:00+08:00",
}


class FakeObserver:
    def __init__(self, responses: list[dict]):
        self.responses = [copy.deepcopy(r) for r in responses]

    def get_database_stats(self) -> dict:
        return self.responses.pop(0)


def sample(**changes) -> dict:
    out = {**DB_COUNTERS, **CKPT_COUNTERS, "raw_ref": "trace://unit/db"}
    out.update(changes)
    return out


def statuses(st: EpisodeState) -> list[str]:
    return [e["status"] for e in st.scratchpad[-3:]]


failures: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  {mark}  {name}   {detail}")
    if not condition:
        failures.append(name)


responses = [
    sample(),
    sample(deadlocks=12, temp_files=7, temp_bytes=8 * 1048576,
           xact_commit=120, xact_rollback=3, ckpt_timed=21,
           ckpt_requested=8, ckpt_write_time_ms=1400.0,
           ckpt_sync_time_ms=120.0),
    sample(deadlocks=12, temp_files=7, temp_bytes=8 * 1048576,
           xact_commit=120, xact_rollback=3, ckpt_timed=21,
           ckpt_requested=8, ckpt_write_time_ms=1400.0,
           ckpt_sync_time_ms=120.0),
    sample(deadlocks=1, temp_files=1, temp_bytes=1048576,
           xact_commit=3, xact_rollback=0, ckpt_timed=1,
           ckpt_requested=1, ckpt_write_time_ms=20.0,
           ckpt_sync_time_ms=2.0,
           db_stats_reset="2026-08-31T09:00:00+08:00",
           ckpt_stats_reset="2026-08-31T09:00:00+08:00"),
    sample(deadlocks=0, temp_files=0, temp_bytes=0,
           xact_commit=1, xact_rollback=0, ckpt_timed=0,
           ckpt_requested=0, ckpt_write_time_ms=10.0,
           ckpt_sync_time_ms=1.0,
           db_stats_reset="2026-08-31T09:00:00+08:00",
           ckpt_stats_reset="2026-08-31T09:00:00+08:00"),
    {"errors": {"pg_stat_database": "permission denied",
                "checkpoint_stats": "connection lost"},
     "raw_ref": "trace://unit/error"},
]

st = EpisodeState(episode_id="cumulative_unit", scenario_id="x")
st.budget["max_steps"] = 30
tb = Toolbox(FakeObserver(responses), st, StateMachine(st))

print("[1] 首次读取只有累计基线，不能冒充当前事故证据")
first = tb.get_database_stats()
check("三类证据均为 UNKNOWN",
      statuses(st) == [EvidenceStatus.UNKNOWN.value] * 3)
check("两组基线写入 EpisodeState",
      set(st.cumulative_baselines) == {"pg_stat_database", "checkpoint_stats"})
old_root = episode_state_module.ROOT
try:
    with tempfile.TemporaryDirectory() as tmp:
        episode_state_module.ROOT = Path(tmp)
        st.save()
        restored = EpisodeState.load(st.episode_id)
finally:
    episode_state_module.ROOT = old_root
check("累计基线可随 EpisodeState 落盘恢复",
      restored.cumulative_baselines == st.cumulative_baselines)
check("无 status 的旧证据仍按 OBSERVED 读取",
      evidence_is_observed({"evidence_type": "legacy"}))
try:
    tb.declare_root_cause("deadlock", "故障窗口内发生了新增死锁")
    rejected = False
except ValueError:
    rejected = True
check("UNKNOWN 不能通过根因声明门槛", rejected)
st.claimed_fault_class = "deadlock"
report = esc.check(st, ["deadlock"])
d1 = next(d for d in report.dims if d.name == "D1")
check("ESC D1 把 UNKNOWN 判为不足", not d1.passed and "未知/错误 1" in d1.detail)
check("ESC 给出再次采集指令",
      any("再次调用 get_database_stats" in d for d in report.directives))
check("首次返回不含伪造的窗口增量", first["window_delta"]["pg_stat_database"] is None)

print("\n[2] 同一统计周期的第二次读取产生可用窗口增量")
second = tb.get_database_stats()
db_delta = second["window_delta"]["pg_stat_database"]
ckpt_delta = second["window_delta"]["checkpoint_stats"]
check("三类证据均为 OBSERVED",
      statuses(st) == [EvidenceStatus.OBSERVED.value] * 3)
check("数据库累计值差分正确",
      db_delta is not None and db_delta["deadlocks"] == 2
      and db_delta["temp_files"] == 3
      and db_delta["temp_bytes"] == 6 * 1048576)
check("检查点累计值差分正确",
      ckpt_delta is not None and ckpt_delta["ckpt_timed"] == 1
      and ckpt_delta["ckpt_requested"] == 3)
window_entries = st.scratchpad[-3:]
check("累计证据绑定同一窗口与 source epoch",
      all(entry.get("window_start") is not None and
          entry.get("window_end") is not None and
          entry.get("source_epoch") and
          entry.get("structured_value", {}).get("source_epoch") ==
          entry.get("source_epoch")
          for entry in window_entries))
check("正增量支持对应根因",
      esc._supports("deadlock_count", st.scratchpad[-3]["observation"], "deadlock")
      and esc._supports("temp_file_volume", st.scratchpad[-2]["observation"],
                        "work_mem_spill")
      and esc._supports("checkpoint_stats", st.scratchpad[-1]["observation"],
                        "checkpoint_pressure"))

print("\n[3] 合法的零增量是 OBSERVED，但不支持当前根因")
tb.get_database_stats()
check("零增量仍为 OBSERVED",
      statuses(st) == [EvidenceStatus.OBSERVED.value] * 3)
check("零增量不支持三类根因",
      not esc._supports("deadlock_count", st.scratchpad[-3]["observation"], "deadlock")
      and not esc._supports("temp_file_volume", st.scratchpad[-2]["observation"],
                            "work_mem_spill")
      and not esc._supports("checkpoint_stats", st.scratchpad[-1]["observation"],
                            "checkpoint_pressure"))

print("\n[4] reset 与计数器回退都重新建立基线并返回 UNKNOWN")
tb.get_database_stats()
check("stats_reset 改变时为 UNKNOWN",
      statuses(st) == [EvidenceStatus.UNKNOWN.value] * 3)
tb.get_database_stats()
check("同一 reset 周期内计数器回退时为 UNKNOWN",
      statuses(st) == [EvidenceStatus.UNKNOWN.value] * 3)

print("\n[5] 查询错误为 ERROR，且不覆盖最后一个正常基线")
baseline_before_error = copy.deepcopy(st.cumulative_baselines)
tb.get_database_stats()
check("三类证据均为 ERROR",
      statuses(st) == [EvidenceStatus.ERROR.value] * 3)
check("ERROR 不覆盖累计基线", st.cumulative_baselines == baseline_before_error)

print("\n[6] 旧累计总量文本不能再支持当前事故根因")
check("累计死锁旧格式失效",
      not esc._supports("deadlock_count", "累计死锁=99", "deadlock"))
check("累计外溢旧格式失效",
      not esc._supports("temp_file_volume", "外溢 512.0 MB", "work_mem_spill"))
check("累计检查点占比旧格式失效",
      not esc._supports("checkpoint_stats", "请求式占比 88.2%",
                        "checkpoint_pressure"))

print("\n" + "=" * 72)
print("CUMULATIVE EVIDENCE:", "PASS" if not failures else f"FAIL {failures}")
sys.exit(1 if failures else 0)
