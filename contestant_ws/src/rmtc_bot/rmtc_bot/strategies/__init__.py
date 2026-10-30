"""策略注册表：把 decision_type 映射到具体实现。

对外只暴露 `build_strategy(name, params)`，Brain 不关心具体类。
新增策略时：实现 `Strategy` 子类 → 在这里注册 → 在 `config.DECISION_TYPES` 里登记。
"""

from __future__ import annotations

from typing import Tuple

from ..battle import Battle as BalancedStrategy
from ..config import Params
from .aggressive import AggressiveStrategy
from .base import LoopGuard, Strategy
from .conservative import ConservativeStrategy
from .greedy import GreedyStrategy

# 现场允许切换的枚举（与 config.DECISION_TYPES 保持一致）
STRATEGY_TYPES: Tuple[str, ...] = ("greedy", "aggressive", "conservative", "balanced")

# 兼容旧命名，避免历史配置文件失效
ALIASES = {"collect": "greedy", "defensive": "conservative", "battle": "balanced"}


def build_strategy(name: str, params: Params) -> Strategy:
    key = ALIASES.get(name, name)
    if key == "greedy":
        return GreedyStrategy(params)
    if key == "aggressive":
        return AggressiveStrategy(params)
    if key == "conservative":
        return ConservativeStrategy(params)
    return BalancedStrategy(params)


__all__ = ["Strategy", "LoopGuard", "build_strategy", "STRATEGY_TYPES"]
