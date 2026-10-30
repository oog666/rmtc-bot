"""三种比赛策略的公共基类。

设计要点：
1. **记忆共享**：策略只读 `brain.world` / `brain.events`，切换策略不清空地图、资源、
   敌车记忆（由 Brain 持有，见 `Brain.set_strategy`）。
2. **事件只做约束**：事件由 LLM 解析成时间/坐标约束后，各策略只决定"要不要去、要不要躲"，
   不在策略里解析任何公告文本。
3. **防循环**：所有策略共用一个 `LoopGuard`：检测"原地抖动/两格往返/追不到还追"，
   必要时强制改走远点或拉黑目标，避免反复无效动作。

三种策略只在"目标怎么选、要不要打"上不同，底层走路、防御、事件避让完全复用。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from ..combat import attack_options, choose_attack
from ..config import Params
from ..protocol import Cell
from ..world import INF, chebyshev

INF_ = INF


# ---------------------------------------------------------------------------
# 防循环守卫（三种策略共享一个实例）
# ---------------------------------------------------------------------------
@dataclass
class LoopGuard:
    """记录最近轨迹与"无效目标"，把反复横跳扼杀在策略层。"""

    positions: deque = field(default_factory=lambda: deque(maxlen=14))
    last_pos: Optional[Cell] = None
    same_pos: int = 0
    cell_ban: Dict[Cell, int] = field(default_factory=dict)
    team_ban: Dict[str, int] = field(default_factory=dict)
    escape_cell: Optional[Cell] = None
    escape_until: int = -1
    reason: str = ""

    def reset(self) -> None:
        self.positions.clear()
        self.last_pos = None
        self.same_pos = 0
        self.cell_ban.clear()
        self.team_ban.clear()
        self.escape_cell = None
        self.escape_until = -1
        self.reason = ""

    def observe(self, pos: Cell, tick: int) -> None:
        self.positions.append(pos)
        if pos == self.last_pos:
            self.same_pos += 1
        else:
            self.same_pos = 0
            self.last_pos = pos

    def oscillating(self) -> bool:
        """最近 12 Tick 恰好只在 2 个格子之间来回 → 判定为往返抖动。

        只有 1 个格子是"站住不动"，由 Brain 的强制挪窝兜底处理；
        这里排除了它，避免把"堡垒驻留/防御"误判成抖动。
        """
        if len(self.positions) < 10:
            return False
        return len(set(self.positions)) == 2

    def ban_cell(self, cell: Cell, tick: int, ticks: int = 25) -> None:
        self.cell_ban[cell] = tick + ticks

    def banned_cell(self, cell: Cell, tick: int) -> bool:
        return self.cell_ban.get(cell, -1) > tick

    def ban_team(self, team: str, tick: int, ticks: int = 25) -> None:
        self.team_ban[team] = tick + ticks

    def banned_team(self, team: str, tick: int) -> bool:
        return self.team_ban.get(team, -1) > tick

    def prune(self, tick: int) -> None:
        for c, until in list(self.cell_ban.items()):
            if until <= tick:
                del self.cell_ban[c]
        for t, until in list(self.team_ban.items()):
            if until <= tick:
                del self.team_ban[t]


# ---------------------------------------------------------------------------
# 策略基类
# ---------------------------------------------------------------------------
class Strategy:
    """所有策略的统一接口：`decide(brain) -> (action, direction|None)`。"""

    name = "base"
    # 规划器给"敌车攻击线"的额外代价：越大越怕贴脸
    threat_low = 0.95
    threat_high = 0.22

    def __init__(self, params: Params) -> None:
        self.p = params
        self._track: Dict[str, Dict[str, Any]] = {}

    def reset(self) -> None:
        """换场次时清空策略内部状态（不动共享记忆）。"""
        self._track.clear()

    def decide(self, b) -> Tuple[str, Optional[str]]:
        raise NotImplementedError

    # ---------------- 通用：攻击 ----------------
    def attack_action(self, b, purity: bool, buff: bool = False):
        me = b.obs.me
        opts = attack_options(me.pos, b.visible_enemies(), purity=purity, unrivaled=buff)
        opt = choose_attack(opts, my_hp=me.hp, protected=me.protected)
        if opt is None:
            return None
        return ("attack", opt.direction)

    # ---------------- 通用：威胁判断 ----------------
    @staticmethod
    def in_enemy_line(pos: Cell, enemies) -> bool:
        """该格是否处于某个敌车的攻击线（同行/同列 1–2 格）。

        出生保护的敌车不能攻击，因此不计入威胁。
        """
        for e in enemies:
            if e.protected:
                continue
            dx = abs(e.pos[0] - pos[0])
            dy = abs(e.pos[1] - pos[1])
            if (dx == 0 and 1 <= dy <= 2) or (dy == 0 and 1 <= dx <= 2):
                return True
        return False

    def threatened(self, b) -> bool:
        return self.in_enemy_line(b.obs.me.pos, b.visible_enemies())

    # ---------------- 通用：目标评估 ----------------
    def resources(self, b, max_dist: Optional[float] = None,
                  value_bias: float = 1.0) -> List[Tuple[float, Cell, int]]:
        """按"分/步"给已知资源排序，过滤被拉黑与不可达的。"""
        out: List[Tuple[float, Cell, int]] = []
        ppv = b.point_per_value()
        for res in b.world.resources.values():
            if b.guard.banned_cell(res.pos, b.tick):
                continue
            d = b.field.d(res.pos)
            if d >= INF_ * 0.5:
                continue
            if max_dist is not None and d > max_dist:
                continue
            u = ppv * res.value * value_bias / (0.45 * d + 0.3)
            out.append((u, res.pos, res.value))
        out.sort(key=lambda x: x[0], reverse=True)
        return out

    def reach_target(self, b) -> Optional[Tuple[float, Cell]]:
        """未完成的"到达坐标"任务，按路程折算分值。"""
        completed = set(b.obs.completed_ids)
        for m in b.obs.missions:
            if m.get("id") in completed or m.get("type") != "reach":
                continue
            pos = m.get("position")
            if not (isinstance(pos, list) and len(pos) == 2):
                continue
            cell = (int(pos[0]), int(pos[1]))
            d = b.field.d(cell)
            if d >= INF_ * 0.5:
                continue
            u = self.p.mission_score / (0.45 * d + 0.25)
            if d <= 1:
                u *= 1.8
            return (u, cell)
        return None

    def heal_target(self, b, hp_threshold: int) -> Optional[Tuple[float, Cell]]:
        """低血量时去天使点；事件坐标由 LLM 给出，过远则放弃。"""
        me = b.obs.me
        if me.hp >= hp_threshold:
            return None
        best: Optional[Tuple[float, Cell]] = None
        for p in b.events.angel_points(b.tick):
            d = b.field.d(p)
            if d >= INF_ * 0.5 or d > 14:
                continue
            u = 0.09 * (100 - me.hp) / (d + 2.0)
            if best is None or u > best[0]:
                best = (u, p)
        return best

    def fortress_target(self, b, max_dist: int) -> Optional[Cell]:
        """天下无双堡垒：只有需要 AoE 刷伤害的策略才去。"""
        f = b.events.fortress(b.tick)
        if f is None:
            return None
        rect, left, need = f
        if left <= need + 2:
            return None
        center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
        d = b.field.d(center)
        if d >= INF_ * 0.5 or d > max_dist:
            return None
        return center

    def pepper_trap(self, b) -> Optional[Cell]:
        """火爆辣椒：敌人正好在十字线上时，踩点触发火线（交点自身免疫）。"""
        if b.obs.me.hp <= 55:
            return None
        for p in b.events.pepper_points(b.tick):
            d = b.field.d(p)
            if d >= INF_ * 0.5 or d > 3:
                continue
            for e in b.visible_enemies():
                if e.pos == p:
                    continue
                if e.pos[0] == p[0] or e.pos[1] == p[1]:
                    return p
        return None

    # ---------------- 通用：目标进度跟踪（防无效追逐） ----------------
    def track_progress(self, b, key: str, dist: float, cell: Optional[Cell] = None,
                       patience: int = 5, ban_ticks: int = 22) -> None:
        """同一目标连续 `patience` Tick 距离没有下降，就判定为"走不到"，拉黑一段时间。"""
        st = self._track.get(key)
        if st is None or b.tick - st["tick"] > patience + 2:
            self._track[key] = {"dist": dist, "tick": b.tick}
            return
        if dist <= st["dist"] - 0.5:
            st["dist"] = dist
            st["tick"] = b.tick
            return
        if b.tick - st["tick"] >= patience:
            if cell is not None:
                b.guard.ban_cell(cell, b.tick, ban_ticks)
            self._track.pop(key, None)

    def clear_progress(self, key: str) -> None:
        self._track.pop(key, None)

    # ---------------- 通用：移动 ----------------
    def move(self, b, cell: Optional[Cell], avoid_threat: bool = False):
        if cell is None:
            return None
        return b.move_to(cell, avoid_threat=avoid_threat)

    def explore(self, b, scale: float = 1.55, offset: float = 3.6):
        cell = b.best_explore(scale, offset)
        return b.move_to(cell) if cell is not None else None

    def flee_cell(self, b, enemies, max_step: int = 6,
                  resource_bonus: float = 0.0) -> Optional[Cell]:
        """挑一个"离敌人最远、又不至于走不到"的落脚点。"""
        best: Optional[Cell] = None
        best_score = -INF_
        for y in range(b.obs.map_h):
            for x in range(b.obs.map_w):
                c = (x, y)
                d = b.field.d(c)
                if d >= INF_ * 0.5 or d > max_step:
                    continue
                if b.world.is_blocked(c, b.tick):
                    continue
                gap = min((chebyshev(c, e.pos) for e in enemies), default=8)
                score = min(gap, 7) * 1.4 - 0.22 * d
                if resource_bonus and c in {r.pos for r in b.world.resources.values()}:
                    score += resource_bonus
                if score > best_score:
                    best_score = score
                    best = c
        return best

    def patrol_target(self, b) -> Optional[Cell]:
        """没有敌人也没有资源时的巡逻点：优先地图中心，其次最近前沿。"""
        f = b.best_explore(1.2, 3.6)
        if f is not None:
            return f
        me = b.obs.me.pos
        center = (b.obs.map_w // 2, b.obs.map_h // 2)
        if b.field.d(center) < INF_ * 0.5 and center != me:
            return center
        for c in ((1, 1), (b.obs.map_w - 2, 1), (1, b.obs.map_h - 2),
                  (b.obs.map_w - 2, b.obs.map_h - 2)):
            if c != me and b.field.d(c) < INF_ * 0.5:
                return c
        return None
