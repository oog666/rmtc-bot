"""战斗评估：把"能不能打、打谁、打哪边"量化成分数。

规则要点（§6.2 / §8.5 / §8.8）：
* 普通攻击沿指定方向命中 1–2 格内**最近**的存活敌车，只命中一个，障碍不阻挡；
* 防御目标 20→10；出生保护目标 0；天下无双为 35/7/0 且命中 5×5 内全部敌人；
* 「洁身自好」期间攻击正在防御的目标：目标不掉血，攻击者自伤 20；
* 击杀奖励 60 点有效伤害，因此"补刀"价值远高于普通磨血。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

from .protocol import DIR_VEC, DIRS, Cell, Entity


@dataclass
class AttackOption:
    direction: str
    target: Entity
    dist: int
    damage: int          # 对目标的名义伤害
    lethal: bool
    reflect: int         # 自身受到的反伤
    score: float         # 折算成"比赛分"的收益
    aoe: bool = False


def nearest_in_line(pos: Cell, direction: str, enemies: Sequence[Entity]) -> Optional[Tuple[Entity, int]]:
    """沿 direction 找最近的敌车（1–2 格）。返回 (实体, 距离)。"""
    vx, vy = DIR_VEC[direction]
    for k in (1, 2):
        cell = (pos[0] + vx * k, pos[1] + vy * k)
        for e in enemies:
            if e.pos == cell:
                return e, k
    return None


def _damage_against(target: Entity, purity: bool, aoe: bool) -> Tuple[int, int]:
    """返回 (对目标伤害, 攻击者反伤)。"""
    if target.protected:
        return 0, 0
    if purity and target.defending:
        return 0, 20
    if aoe:
        # 规则 §8.5：天下无双对非防御 50 HP、防御 10 HP、出生保护 0
        return (10 if target.defending else 50), 0
    return (10 if target.defending else 20), 0


def attack_options(
    pos: Cell,
    enemies: Sequence[Entity],
    *,
    purity: bool = False,
    unrivaled: bool = False,
    damage_point: float = 0.03,   # 1 HP 有效伤害 = 0.03 分（15/500）
    kill_bonus: float = 1.8,      # 60 点击杀奖励 = 1.8 分
    reflect_penalty: float = 0.06,
) -> List[AttackOption]:
    """列出当前可执行的攻击方案，按收益从高到低排序。"""
    out: List[AttackOption] = []

    if unrivaled:
        # 天下无双：5×5 内所有敌人，方向合法即可（方向不限制目标集合）
        hits = [e for e in enemies if max(abs(e.pos[0] - pos[0]), abs(e.pos[1] - pos[1])) <= 2]
        if hits:
            total_dmg = 0
            score = 0.0
            any_lethal = False
            for e in hits:
                dmg, _ = _damage_against(e, purity, aoe=True)
                total_dmg += dmg
                score += dmg * damage_point
                if dmg > 0 and e.hp <= dmg:
                    score += kill_bonus
                    any_lethal = True
            out.append(AttackOption("up", hits[0], 1, total_dmg, any_lethal, 0, score, aoe=True))
        return out

    for d in DIRS:
        hit = nearest_in_line(pos, d, enemies)
        if hit is None:
            continue
        target, dist = hit
        dmg, reflect = _damage_against(target, purity, aoe=False)
        if dmg <= 0 and reflect <= 0:
            continue
        lethal = dmg > 0 and target.hp <= dmg
        score = dmg * damage_point + (kill_bonus if lethal else 0.0) - reflect * reflect_penalty
        out.append(AttackOption(d, target, dist, dmg, lethal, reflect, score))

    out.sort(key=lambda o: o.score, reverse=True)
    return out


def choose_attack(
    options: Sequence[AttackOption],
    *,
    my_hp: int,
    protected: bool,
    min_score: float = 0.02,
) -> Optional[AttackOption]:
    """给攻击加"保命闸门"：残血时不送，除非这一下能斩杀。"""
    if protected:
        return None
    for opt in options:
        if opt.score < min_score:
            continue
        if opt.reflect >= my_hp:
            continue
        if my_hp <= 20 and not opt.lethal:
            continue
        return opt
    return None


def aligned_threats(pos: Cell, enemies: Sequence[Entity], max_dist: int = 2) -> List[Entity]:
    """当前处于敌方攻击线（同行/同列 1–2 格）上的敌人——用于防御判断。"""
    out = []
    for e in enemies:
        dx, dy = e.pos[0] - pos[0], e.pos[1] - pos[1]
        if dx == 0 and 1 <= abs(dy) <= max_dist:
            out.append(e)
        elif dy == 0 and 1 <= abs(dx) <= max_dist:
            out.append(e)
    return out


def attack_cells(enemy_pos: Cell) -> List[Cell]:
    """能攻击到该敌人的站位（同行/同列 1–2 格，去掉敌人自身格）。"""
    ex, ey = enemy_pos
    return [
        (ex + 1, ey), (ex + 2, ey), (ex - 1, ey), (ex - 2, ey),
        (ex, ey + 1), (ex, ey + 2), (ex, ey - 1), (ex, ey - 2),
    ]


def threat_cells(positions: Iterable[Cell]) -> set:
    """敌人下一 Tick 可能覆盖的攻击格（用于贴近时的站位风险估计）。"""
    out = set()
    for px, py in positions:
        for d in DIRS:
            vx, vy = DIR_VEC[d]
            out.add((px + vx, py + vy))
            out.add((px + vx * 2, py + vy * 2))
    return out
