"""注入器基类。

计划里的三条铁律，在这里落成接口约束：
  1. 可快照回滚  -> 一切改动都能被 snapshot.reset() 抹掉
  2. 参数化随机  -> params() 每次产生不同实例，防 agent 背答案
  3. 自带 ground truth -> fault_class + 期望证据，供判分器机械比对
"""
from __future__ import annotations

import abc
from dataclasses import dataclass, field
from typing import Any


@dataclass
class InjectionRecord:
    """注入了什么 —— 判分器据此核对 agent 的诊断是否命中。"""

    fault_class: str
    params: dict[str, Any] = field(default_factory=dict)
    notes: str = ""


class Injector(abc.ABC):
    fault_class: str = "unknown"

    def __init__(self, spec: dict):
        self.spec = spec

    @abc.abstractmethod
    def params(self, rng) -> dict:
        """本次实例的随机化参数。"""

    @abc.abstractmethod
    def inject(self, params: dict) -> InjectionRecord:
        """把健康库变成故障库。"""

    @abc.abstractmethod
    def verify_injected(self, params: dict) -> bool:
        """确认故障确实生效 —— 没生效的 episode 是废的，必须能检出。"""
