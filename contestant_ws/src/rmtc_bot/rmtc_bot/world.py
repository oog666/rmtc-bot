"""地图记忆 + 路径规划。

关键设计：
* 只用公开感知更新地图（视野内实体），绝不读取全图或网页数据；
* 未知格按"可通行但略贵"处理，既保证能探索，又不会为了抄近路反复撞墙；
* 威胁场（敌车可达格）用软代价表示，低血量时硬性回避，避免"走一步就死"。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from typing import Dict, FrozenSet, Iterable, List, Optional, Set, Tuple

from .protocol import DIR_VEC, Cell, Observation

UNKNOWN, FREE, BLOCK = 0, 1, 2

INF = 1e9


@dataclass
class ResourceMemory:
    rid: str
    pos: Cell
    value: int
    seen_tick: int = 0


@dataclass
class EnemyTrack:
    team: str
    pos: Cell
    hp: int = 100
    defending: bool = False
    protected: bool = False
    seen_tick: int = -999
    # 通过伤害推算出的"最后确认存活"时刻；用于判断目标是否已阵亡
    hits_taken: int = 0

    def age(self, tick: int) -> int:
        return tick - self.seen_tick


class WorldMap:
    """增量式占据栅格 + 实体记忆。"""

    def __init__(self, w: int, h: int) -> None:
        self.w = w
        self.h = h
        self.cells = bytearray(w * h)
        self.temp_block: Dict[Cell, int] = {}       # 临时障碍（世界分裂）→ 到期 Tick
        self.resources: Dict[str, ResourceMemory] = {}
        self.enemies: Dict[str, EnemyTrack] = {}
        self.pickups: List[Tuple[int, int]] = []     # (价值, Tick)
        self.taken_value = 0                          # 看到"已知资源消失"= 被别人采走
        self.last_update_tick = -1

    # ---------- 基础查询 ----------
    def inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.w and 0 <= y < self.h

    def idx(self, x: int, y: int) -> int:
        return y * self.w + x

    def cell_state(self, x: int, y: int) -> int:
        return self.cells[y * self.w + x]

    def is_blocked(self, cell: Cell, tick: int) -> bool:
        x, y = cell
        if not self.inside(x, y):
            return True
        if self.cells[y * self.w + x] == BLOCK:
            return True
        exp = self.temp_block.get(cell)
        return exp is not None and tick < exp

    def mark_free(self, x: int, y: int) -> None:
        if self.inside(x, y) and self.cells[y * self.w + x] != BLOCK:
            self.cells[y * self.w + x] = FREE

    def mark_block(self, x: int, y: int) -> None:
        if self.inside(x, y):
            self.cells[y * self.w + x] = BLOCK

    def add_temp_block(self, cells: Iterable[Cell], expire_tick: int) -> None:
        for c in cells:
            if self.inside(*c):
                self.temp_block[c] = max(expire_tick, self.temp_block.get(c, 0))

    def prune_temp(self, tick: int) -> None:
        for c, exp in list(self.temp_block.items()):
            if tick >= exp:
                del self.temp_block[c]

    # ---------- 感知融合 ----------
    def update(self, obs: Observation) -> None:
        me = obs.me
        sx, sy = me.pos
        r = obs.vision
        self.last_update_tick = obs.tick

        # 阵亡期间规则不提供周围实体；若照常清理，会把"看不见"误判成"被采走"，
        # 既污染资源记忆，也把剩余价值估低（探索欲望随之崩塌）。这里直接跳过。
        if not me.alive:
            return

        # 1) 视野范围内默认畅通（障碍会作为实体单独覆盖）
        for y in range(max(0, sy - r), min(self.h, sy + r + 1)):
            for x in range(max(0, sx - r), min(self.w, sx + r + 1)):
                self.mark_free(x, y)

        seen_res_ids: Set[str] = set()
        seen_enemy_teams: Set[str] = set()
        for e in obs.entities:
            if e.kind == "obstacle":
                self.mark_block(*e.pos)
            elif e.kind in ("resource_normal", "resource_high"):
                self.mark_free(*e.pos)
                seen_res_ids.add(e.eid)
                self.resources[e.eid] = ResourceMemory(e.eid, e.pos, e.value, obs.tick)
            elif e.kind == "robot":
                if e.team == me.team:
                    continue
                self.mark_free(*e.pos)
                seen_enemy_teams.add(e.team)
                self.enemies[e.team] = EnemyTrack(
                    team=e.team, pos=e.pos, hp=e.hp if e.hp is not None else 100,
                    defending=e.defending, protected=e.protected, seen_tick=obs.tick,
                )

        # 2) 视野内"已知却没再出现"的资源 = 已被别人采集
        for rid, res in list(self.resources.items()):
            if max(abs(res.pos[0] - sx), abs(res.pos[1] - sy)) > r:
                continue
            if rid in seen_res_ids:
                continue
            if res.pos == me.pos:
                self.pickups.append((res.value, obs.tick))   # 本车踩到即收集
            else:
                self.taken_value += res.value                # 对手采走
            del self.resources[rid]

        # 3) 很久没见的敌车：保留最后位置用于"追猎记忆"，但威胁判定会按年龄衰减
        for team, trk in list(self.enemies.items()):
            if team in seen_enemy_teams:
                continue
            if trk.age(obs.tick) > 24:
                del self.enemies[team]

    # ---------- 统计 ----------
    def collected_value(self) -> int:
        return sum(v for v, _ in self.pickups)

    def collected_count(self) -> Tuple[int, int]:
        normal = sum(1 for v, _ in self.pickups if v < 20)
        high = sum(1 for v, _ in self.pickups if v >= 20)
        return normal, high

    def frontier_cells(self, lim: int = 400) -> List[Cell]:
        """返回与未知格四邻接的已知畅通格（探索前沿）。"""
        out: List[Cell] = []
        for y in range(self.h):
            base = y * self.w
            for x in range(self.w):
                if self.cells[base + x] != FREE:
                    continue
                for dx, dy in DIR_VEC.values():
                    nx, ny = x + dx, y + dy
                    if self.inside(nx, ny) and self.cells[ny * self.w + nx] == UNKNOWN:
                        out.append((x, y))
                        break
                if len(out) >= lim:
                    return out
        return out

    def unknown_ratio(self) -> float:
        total = self.w * self.h
        unknown = sum(1 for c in self.cells if c == UNKNOWN)
        return unknown / total if total else 0.0

    def info_gain(self, cell: Cell, radius: int = 4) -> int:
        """站在 cell 能新看到的未知格数量（用 9x9 覆盖近似）。"""
        x, y = cell
        gain = 0
        for yy in range(max(0, y - radius), min(self.h, y + radius + 1)):
            row = yy * self.w
            for xx in range(max(0, x - radius), min(self.w, x + radius + 1)):
                if self.cells[row + xx] == UNKNOWN:
                    gain += 1
        return gain


# ---------------------------------------------------------------------------
# 路径规划
# ---------------------------------------------------------------------------

@dataclass
class Field:
    """一次 Dijkstra 的结果，可同时评估多个候选目标。"""
    dist: List[float]
    prev: List[int]
    w: int
    h: int

    def d(self, cell: Cell) -> float:
        x, y = cell
        if not (0 <= x < self.w and 0 <= y < self.h):
            return INF
        return self.dist[y * self.w + x]

    def reachable(self, cell: Cell) -> bool:
        return self.d(cell) < INF * 0.5

    def first_step(self, start: Cell, target: Cell) -> Optional[str]:
        """返回从 start 走向 target 的第一步方向；不可达返回 None。"""
        w = self.w
        h = self.h
        sx, sy = start
        tx, ty = target
        if not (0 <= tx < w and 0 <= ty < h):
            return None
        cur = ty * w + tx
        start_idx = sy * w + sx
        if cur == start_idx:
            return None
        # 回溯到起点的前驱
        guard = 0
        while cur != start_idx:
            p = self.prev[cur]
            if p < 0:
                return None
            if p == start_idx:
                nx, ny = cur % w, cur // w
                dx, dy = nx - sx, ny - sy
                for name, (vx, vy) in DIR_VEC.items():
                    if (vx, vy) == (dx, dy):
                        return name
                return None
            cur = p
            guard += 1
            if guard > w * h:
                return None
        return None


def build_field(
    world: WorldMap,
    start: Cell,
    tick: int,
    *,
    hp: int = 100,
    danger: FrozenSet[Cell] = frozenset(),
    blocked_extra: FrozenSet[Cell] = frozenset(),
    enemies: Iterable[EnemyTrack] = (),
    unknown_cost: float = 0.075,
    enemy_cost: float = 7.0,
    threat_low: float = 0.95,
    threat_high: float = 0.22,
) -> Field:
    """Dijkstra（四邻接，单位步长）。

    代价构成：
      1.0            基础步长
      +unknown_cost  未知格（鼓励走已验证通路，但不禁止探索）
      +enemy_cost    当前被敌车占据的格
      +威胁代价      敌车下一 Tick 可能的攻击格（低血量时显著加价）
    """
    w, h = world.w, world.h
    dist = [INF] * (w * h)
    prev = [-1] * (w * h)
    sx, sy = start
    if not world.inside(sx, sy):
        return Field(dist, prev, w, h)

    # 威胁场：敌车所在行列 ±2 内的格子
    threat: Dict[Cell, float] = {}
    low = hp <= 45
    for e in enemies:
        ex, ey = e.pos
        if e.protected:
            continue
        for k in range(1, 3):
            for c in ((ex + k, ey), (ex - k, ey), (ex, ey + k), (ex, ey - k)):
                threat[c] = max(threat.get(c, 0.0), threat_low if low else threat_high)
        threat[(ex, ey)] = max(threat.get((ex, ey), 0.0), enemy_cost)

    start_idx = sy * w + sx
    dist[start_idx] = 0.0
    pq = [(0.0, start_idx)]

    while pq:
        d, cur = heapq.heappop(pq)
        if d > dist[cur] + 1e-9:
            continue
        cx, cy = cur % w, cur // w
        for dx, dy in DIR_VEC.values():
            nx, ny = cx + dx, cy + dy
            if not (0 <= nx < w and 0 <= ny < h):
                continue
            nxt = ny * w + nx
            cell = (nx, ny)
            if cell in danger or cell in blocked_extra:
                continue
            if world.is_blocked(cell, tick):
                continue
            c = 1.0
            if world.cells[nxt] == UNKNOWN:
                c += unknown_cost
            if cell in threat:
                c += threat[cell]
            nd = d + c
            if nd < dist[nxt] - 1e-9:
                dist[nxt] = nd
                prev[nxt] = cur
                heapq.heappush(pq, (nd, nxt))

    return Field(dist, prev, w, h)


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def chebyshev(a: Cell, b: Cell) -> int:
    return max(abs(a[0] - b[0]), abs(a[1] - b[1]))


def step_dir(a: Cell, b: Cell) -> Optional[str]:
    dx, dy = b[0] - a[0], b[1] - a[1]
    for name, (vx, vy) in DIR_VEC.items():
        if (vx, vy) == (dx, dy):
            return name
    return None
