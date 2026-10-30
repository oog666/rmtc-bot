"""贪婪型：只抢资源、只做任务，绝不主动攻击。

行为约束（按需求）：
* 不使用 ATTACK；
* 唯一的战斗动作是 DEFEND，且只在"已经处于敌方攻击线"或"下一步就要踏进攻击线"
  且血量不足以硬吃一次攻击时使用；
* 其余时间全部用于资源与任务；没有目标时探索。

计分上限：资源 30 + 任务 15 = 45 分（放弃 15 分对抗分），因此适合第一阶段、
以及"对手很强、先保证不死不亏"的对局。
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from ..protocol import Cell
from ..world import chebyshev
from .base import Strategy


class GreedyStrategy(Strategy):
    name = "greedy"

    def reset(self) -> None:
        super().reset()
        self._last_defend = -99
    threat_low = 1.80          # 明显绕开敌方攻击线
    threat_high = 0.55

    def decide(self, b) -> Tuple[str, Optional[str]]:
        me = b.obs.me
        if not me.alive:
            return ("wait", None)

        enemies = b.visible_enemies()
        in_line = self.in_enemy_line(me.pos, enemies)
        nearest = min((chebyshev(me.pos, e.pos) for e in enemies), default=99)

        # 1) 被贴脸且残血：防一 Tick 把 20 降到 10，下一 Tick 必须脱离（不能站桩到死）
        if in_line and me.hp <= 45:
            if b.tick - self._last_defend > 1:
                self._last_defend = b.tick
                return ("defend", None)
            cell = self.flee_cell(b, enemies, max_step=5)
            mv = self.move(b, cell)
            if mv is not None:
                return mv
            return ("defend", None)

        # 2) 血量危险且敌人很近：脱离
        if me.hp <= 35 and nearest <= 3:
            cell = self.flee_cell(b, enemies, max_step=5)
            mv = self.move(b, cell)
            if mv is not None:
                return mv

        # 3) 已在攻击线但血还够：不站桩，边撤边找下一个资源
        if in_line and me.hp <= 70:
            cell = self.flee_cell(b, enemies, max_step=4)
            mv = self.move(b, cell)
            if mv is not None:
                return mv

        # 2) 事件驱动：低血去天使点（坐标由 LLM 给出）
        heal = self.heal_target(b, self.p.greedy_heal_hp)

        # 3) 目标清单：任务点 / 资源 / 探索
        cands: List[Tuple[float, str, Cell]] = []
        reach = self.reach_target(b)
        if reach is not None:
            cands.append((reach[0], "reach", reach[1]))
        for u, cell, _value in self.resources(b, value_bias=1.15)[:12]:
            gap = min((chebyshev(cell, e.pos) for e in enemies), default=8)
            cands.append((u * (1.0 + 0.07 * min(gap, 6)), "resource", cell))
        if heal is not None:
            cands.append((heal[0], "heal", heal[1]))
        for cell, u in b.explore_candidates(1.55, 3.6)[:8]:
            if not b.guard.banned_cell(cell, b.tick):
                cands.append((u, "explore", cell))

        if not cands:
            mv = self.explore(b)
            if mv is None:
                mv = self.move(b, self.patrol_target(b))
            return mv or ("wait", None)

        cands.sort(key=lambda x: x[0], reverse=True)
        _, kind, cell = cands[0]

        # 4) 目标进度跟踪：连续几个 Tick 走不近就拉黑，避免来回无效移动
        dist = b.field.d(cell)
        if kind in ("resource", "reach"):
            self.track_progress(b, f"{kind}:{cell}", dist, cell)

        # 5) 移动；血量不高时避免踏入敌方攻击线
        mv = self.move(b, cell, avoid_threat=me.hp <= 60)
        if mv is not None:
            return mv

        # 6) 走不到就换下一个候选，避免原地卡死
        for _u, kind2, cell2 in cands[1:6]:
            mv = self.move(b, cell2, avoid_threat=me.hp <= 60)
            if mv is not None:
                if kind2 in ("resource", "reach"):
                    self.track_progress(b, f"{kind2}:{cell2}", b.field.d(cell2), cell2)
                return mv
        return ("wait", None)
