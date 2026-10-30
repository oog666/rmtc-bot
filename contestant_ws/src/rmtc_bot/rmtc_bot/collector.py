"""第一阶段：100 Tick 内的在线定向采集。

本质是一个"边走边发现"的定向越野（Orienteering）问题：
* 资源位置未知，只能靠 9×9 视野发现；
* 每个资源 10 或 20 价值，总价值 200 → 40 分，因此 1 点价值 = 0.2 分；
* 时间只有 100 Tick，移动 1 格/Tick，所以决策单位统一为"分/步"。

效用函数：
    资源：  u = 0.2 × value / (dist + 1.6)
    探索：  u = 0.2 × 密度 × 新发现格数 / (dist + 3.4)

探索项让机器人在"没有已知资源"时朝信息量最大的前沿走，而不是原地打转；
目标锁定 + 切换阈值抑制两个几乎等价的候选之间反复横跳。
"""

from __future__ import annotations

from typing import Optional, Tuple

from .config import Params
from .world import INF


class Collector:
    def __init__(self, params: Params) -> None:
        self.p = params

    def decide(self, b) -> Tuple[str, Optional[str]]:
        obs = b.obs
        remaining = b.remaining_ticks()
        ppv = b.point_per_value()

        # ---------- 1. 资源候选 ----------
        cands = []  # (utility, kind, cell, ref)
        for rid, res in b.world.resources.items():
            d = b.field.d(res.pos)
            if d >= INF * 0.5:
                continue
            if d > remaining:            # 时间不够，放弃
                continue
            u = ppv * res.value / (self.p.s1_detour_k * d + self.p.s1_dist_offset)
            cands.append((u, "resource", res.pos, rid))

        # ---------- 2. 探索候选 ----------
        exp = b.explore_candidates(1.0, self.p.s1_explore_offset)
        if exp:
            cell, u = exp[0]
            cands.append((u * self.p.s1_explore_scale * self.p.s1_explore_risk,
                          "explore", cell, None))

        if not cands:
            # 地图已探明且无资源：原地等待（此时已无分可拿）
            return ("wait", None)

        cands.sort(key=lambda t: t[0], reverse=True)
        best = cands[0]

        # ---------- 3. 目标锁定（抑制抖动）----------
        if b.lock_cell is not None and b.tick <= b.lock_until:
            for c in cands:
                if c[2] == b.lock_cell:
                    if best[0] < b.lock_score * self.p.s1_switch_margin:
                        best = c
                    break

        b.lock_cell = best[2]
        b.lock_kind = best[1]
        b.lock_score = best[0]
        b.lock_until = b.tick + self.p.s1_lock_ticks

        # ---------- 4. 执行 ----------
        if best[2] == obs.me.pos:
            return ("wait", None)
        move = b.move_to(best[2])
        if move is not None:
            return move

        # 选不出可走方向时，退化为探索
        cell = b.best_explore(1.0, self.p.s1_explore_offset)
        if cell is not None:
            move = b.move_to(cell)
            if move is not None:
                return move
        return ("wait", None)
