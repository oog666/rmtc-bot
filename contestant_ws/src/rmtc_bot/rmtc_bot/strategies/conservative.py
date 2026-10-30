"""保守型：优先拉开距离，在躲避过程中搜资源。

行为约束（按需求）：
* 不主动接敌，也不主动攻击；
* 敌车进入警戒半径就换一个"离敌人更远、还能顺手捡资源"的落脚点；
* 被贴脸且无路可退时 DEFEND（把 20 伤害降到 10）；
* 资源评分会额外偏好"离敌人远"的那一侧。

计分目标：以存活和资源为主，通常能拿到资源 20~30 分 + 任务 10~15 分。
适合开局被针对、或对手明显更强时保底。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..protocol import Cell
from ..world import chebyshev
from .base import Strategy


class ConservativeStrategy(Strategy):
    name = "conservative"

    def reset(self) -> None:
        super().reset()
        self._flee_since = -1
    threat_low = 2.40          # 非常怕贴脸
    threat_high = 1.10

    def decide(self, b) -> Tuple[str, Optional[str]]:
        me = b.obs.me
        if not me.alive:
            return ("wait", None)

        enemies = b.visible_enemies()
        nearest = min((chebyshev(me.pos, e.pos) for e in enemies), default=99)
        pan = b.events.pan_active(b.tick)

        # 1) 易伤期被贴脸：移动会再次获得易伤，站住防御更划算
        if pan and nearest <= 5 and self.in_enemy_line(me.pos, enemies):
            return ("defend", None)

        # 2) 被逼近才跑：2 格内必跑；警戒半径内且血不多也跑；否则继续采集（资源评分偏向远离敌人）
        danger = (nearest <= 2
                  or me.hp <= 45
                  or (nearest <= self.p.conservative_flee_radius
                      and me.hp <= self.p.conservative_alert_hp))
        if danger:
            if self._flee_since < 0:
                self._flee_since = b.tick
            cell = self.flee_cell(b, enemies, max_step=5, resource_bonus=0.9)
            if cell is not None:
                mv = self.move(b, cell)
                if mv is not None:
                    return mv
            if self.in_enemy_line(me.pos, enemies):
                return ("defend", None)
        else:
            self._flee_since = -1

        # 3) 已经被贴脸但没跑掉：防御
        if nearest <= 2 and self.in_enemy_line(me.pos, enemies):
            return ("defend", None)

        # 4) 低血找天使点（保守型阈值更高，满血前都愿意去）
        heal = self.heal_target(b, self.p.conservative_heal_hp)

        # 5) 资源：偏好"离敌人远"的那一侧
        cands: List[Tuple[float, str, Cell]] = []
        for u, cell, _value in self.resources(b, value_bias=1.0)[:12]:
            gap = min((chebyshev(cell, e.pos) for e in enemies), default=8)
            cands.append((u * (1.0 + 0.06 * min(gap, 8)), "resource", cell))
        reach = self.reach_target(b)
        if reach is not None:
            d_enemy = min((chebyshev(reach[1], e.pos) for e in enemies), default=8)
            if d_enemy >= 3:                      # 任务点太靠近敌人就先不去
                cands.append((reach[0], "reach", reach[1]))
        if heal is not None:
            cands.append((heal[0], "heal", heal[1]))
        for cell, u in b.explore_candidates(1.25, 3.8)[:8]:
            gap = min((chebyshev(cell, e.pos) for e in enemies), default=8)
            if gap >= 3 and not b.guard.banned_cell(cell, b.tick):
                cands.append((u, "explore", cell))

        if not cands:
            mv = self.explore(b, 1.25, 3.8)
            if mv is None:
                mv = self.move(b, self.patrol_target(b))
            return mv or ("wait", None)

        cands.sort(key=lambda x: x[0], reverse=True)
        _, kind, cell = cands[0]
        if kind in ("resource", "reach"):
            self.track_progress(b, f"{kind}:{cell}", b.field.d(cell), cell)

        mv = self.move(b, cell)
        if mv is not None:
            return mv
        for _u, kind2, cell2 in cands[1:6]:
            mv = self.move(b, cell2)
            if mv is not None:
                if kind2 in ("resource", "reach"):
                    self.track_progress(b, f"{kind2}:{cell2}", b.field.d(cell2), cell2)
                return mv
        return ("wait", None)
