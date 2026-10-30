"""侵略型：主动找架打，资源只捡"嘴边"的。

行为约束（按需求）：
* 攻击优先：只要进入攻击线且这一下不亏，立刻 ATTACK；
* 主动接近视野内敌车，优先打残血、能斩杀的目标；
* 资源只在 3 格内顺路捡，任务点只在 5 格内顺路做；
* 血量过低先脱离，不做自杀式换血。

计分目标：对抗 15 分打满（D≥500）+ 顺路资源/任务，是三种策略里上限最高的一种。
"""

from __future__ import annotations

from typing import Optional, Tuple

from ..combat import attack_cells
from ..protocol import Cell
from ..world import INF, chebyshev
from .base import Strategy


class AggressiveStrategy(Strategy):
    name = "aggressive"
    threat_low = 0.55          # 不太怕贴脸
    threat_high = 0.10

    def reset(self) -> None:
        super().reset()
        self._chase = None       # (team, start_tick, damage_at_start)
        self._regroup_until = -1

    def decide(self, b) -> Tuple[str, Optional[str]]:
        me = b.obs.me
        if not me.alive:
            return ("wait", None)

        enemies = b.visible_enemies()
        purity = b.events.purity_active(b.tick)
        buff = b.buff_until > b.tick

        # 1) 能打就打
        act = self.attack_action(b, purity, buff)
        if act is not None:
            self._chase = None
            return act

        # 2) 残血脱离：退到安全处并进入 6 Tick 的"重整期"
        if me.hp <= self.p.aggressive_retreat_hp and enemies:
            self._regroup_until = b.tick + 6
            cell = self.flee_cell(b, enemies, max_step=5)
            mv = self.move(b, cell)
            if mv is not None:
                return mv

        # 重整期内不主动接敌，优先回血/捡资源
        if b.tick < self._regroup_until and me.hp < 60:
            heal = self.heal_target(b, self.p.angel_hp_threshold)
            if heal is not None:
                mv = self.move(b, heal[1])
                if mv is not None:
                    return mv
            res = self.resources(b, max_dist=4.0)
            if res:
                mv = self.move(b, res[0][1])
                if mv is not None:
                    return mv

        # 3) 主动接近目标（对抗分已满 / 多打一 / 残血 / 出生保护时，停止主动追猎）
        capped = b.obs.effective_damage >= self.p.combat_cap_damage
        multi = len(enemies) >= 2 and me.hp < 55
        weak = me.hp <= self.p.engage_hp
        target = None if (multi or capped or weak or me.protected) else self._pick_enemy(b)
        if target is not None:
            self._note_chase(b, target.team)
            cell = self._attack_cell(b, target.pos) or self._approach_cell(b, target.pos)
            mv = self.move(b, cell)
            if mv is not None:
                return mv
            b.guard.ban_team(target.team, b.tick, 25)   # 够不到，先放弃
            self._chase = None

        # 3.5) 血量偏低且附近有天使点：先回血再打（事件坐标驱动）
        if me.hp <= 50:
            heal = self.heal_target(b, self.p.angel_hp_threshold)
            if heal is not None:
                mv = self.move(b, heal[1])
                if mv is not None:
                    return mv

        # 4) 天下无双：AoE 是刷伤害最快的途径
        fort = self.fortress_target(b, self.p.fortress_max_dist + 2)
        if fort is not None:
            mv = self.move(b, fort)
            if mv is not None:
                return mv

        # 5) 辣椒陷阱：敌人正好在十字线上时踩点
        trap = self.pepper_trap(b)
        if trap is not None:
            mv = self.move(b, trap)
            if mv is not None:
                return mv

        # 6) 资源：对抗分已满后可以走更远（顺路 → 主动）
        res = self.resources(b, max_dist=7.0 if capped else 4.0)
        if res:
            mv = self.move(b, res[0][1])
            if mv is not None:
                return mv

        # 7) 顺路任务（5 格内）
        reach = self.reach_target(b)
        if reach is not None and b.field.d(reach[1]) <= (12 if capped else 6):
            mv = self.move(b, reach[1])
            if mv is not None:
                return mv

        # 8) 没敌人：去最后已知位置/中心/前沿巡逻（失败就换下一个，绝不原地等）
        for cell in (self._memory_target(b), self.patrol_target(b)):
            if cell is None or cell == me.pos:
                continue
            mv = self.move(b, cell)
            if mv is not None:
                return mv
        return ("wait", None)

    # ------------------------------------------------------------------
    def _pick_enemy(self, b):
        me = b.obs.me
        best = None
        best_u = -1.0
        for e in b.visible_enemies():
            if e.protected or b.guard.banned_team(e.team, b.tick):
                continue
            d = chebyshev(e.pos, me.pos)
            if d > self.p.chase_max_dist:
                continue
            hp = max(1, min(100, e.hp))
            # 目标价值：伤害分 + 击杀奖励，摊到"赶路 + 击杀所需 Tick"
            u = (0.03 * hp + 1.8) / (0.55 * d + hp / 20.0 + 1.0)
            if e.hp <= self.p.kill_secure_hp:
                u *= 1.35
            if me.hp <= self.p.engage_hp and e.hp > self.p.kill_secure_hp:
                continue          # 残血时只打能斩杀的，不做无谓换血
            if u > best_u:
                best_u = u
                best = e
        return best

    def _note_chase(self, b, team: str) -> None:
        """追了 15 Tick 还没造成伤害 → 判定为"追不到"，拉黑一段时间。"""
        now = b.tick
        dmg = b.obs.effective_damage
        if self._chase is None or self._chase[0] != team:
            self._chase = (team, now, dmg)
            return
        if dmg > self._chase[2] + 0.5:
            self._chase = (team, now, dmg)
            return
        if now - self._chase[1] > 15:
            b.guard.ban_team(team, now, 25)
            self._chase = None

    def _attack_cell(self, b, enemy_pos: Cell) -> Optional[Cell]:
        best = None
        best_d = INF
        for c in attack_cells(enemy_pos):
            if c == b.obs.me.pos or b.world.is_blocked(c, b.tick):
                continue
            if b.guard.banned_cell(c, b.tick):
                continue
            d = b.field.d(c)
            if d < best_d:
                best_d = d
                best = c
        return best

    def _approach_cell(self, b, enemy_pos: Cell) -> Optional[Cell]:
        ex, ey = enemy_pos
        best = None
        best_d = INF
        for c in ((ex + 1, ey), (ex - 1, ey), (ex, ey + 1), (ex, ey - 1)):
            if c == b.obs.me.pos or b.world.is_blocked(c, b.tick):
                continue
            d = b.field.d(c)
            if d < best_d:
                best_d = d
                best = c
        return best

    def _memory_target(self, b) -> Optional[Cell]:
        """视野里没人时，去"最后见过的敌人位置"看看（不超过 15 Tick 的记忆）。"""
        best = None
        best_age = 99
        for team, trk in b.world.enemies.items():
            if b.guard.banned_team(team, b.tick):
                continue
            age = b.obs.tick - trk.seen_tick
            if trk.pos == b.obs.me.pos or b.world.is_blocked(trk.pos, b.tick):
                continue
            if age <= 15 and age < best_age:
                best_age = age
                best = trk.pos
        return best
