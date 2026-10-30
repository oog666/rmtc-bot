"""RMTC 离线规则仿真器（仅用于开发验证，不参与比赛）。

严格按规则 §5.2 的结算顺序实现：
  1. 效果到期 / 事件激活（爆炸、幻术、世界分裂在移动前判定）
  2. 事件致死
  3. 固化动作、设置防御
  4. 按移动前位置汇总攻击 / 反伤 / 伤害修饰，统一扣血，分配实际伤害与击杀奖励
  5. 按战斗后存活车辆统一移动（争格全失败）
  6. 入点事件 / 辣椒火线
  7. 堡垒驻留与回血
  8. 死亡扣 60 有效伤害
  9. 收集资源 / 检查任务 / 复活倒计时

用法：
  python3 tools/sim.py --stage 1 --matches 20
  python3 tools/sim.py --stage 2 --matches 20 --preset practice
  python3 tools/sim.py --stage 2 --matches 1 --verbose
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import random
import sys
from collections import deque
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Set, Tuple

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "contestant_ws", "src", "rmtc_bot"))

from rmtc_bot.brain import Brain              # noqa: E402
from rmtc_bot.config import Params           # noqa: E402
from rmtc_bot.protocol import DIR_VEC, OPPOSITE, Cell  # noqa: E402

DIRS = ("up", "down", "left", "right")
INF = 10 ** 9

# 官方练习包 config/stage1.json 与 stage2.json 的公共地图（18×18）
PRACTICE_MAP = {
    "width": 18, "height": 18,
    "obstacles": [(6, 3), (6, 4), (11, 13), (11, 14), (3, 11), (14, 6)],
    "resources": [
        ((2, 1), 10), ((15, 1), 10), ((1, 15), 10), ((16, 15), 10),
        ((4, 2), 10), ((13, 2), 10), ((2, 13), 10), ((15, 13), 10),
        ((4, 8), 10), ((13, 9), 10), ((8, 4), 10), ((9, 13), 10),
        ((5, 5), 20), ((12, 5), 20), ((5, 12), 20), ((12, 12), 20),
    ],
    "spawns": [(1, 1), (16, 1), (1, 16), (16, 16)],
    "reach": (9, 9),
}


@dataclass
class Resource:
    rid: str
    pos: Cell
    value: int


@dataclass(eq=False)
class Robot:
    team: str
    pos: Cell
    spawn: Cell
    hp: int = 100
    alive: bool = True
    defending: bool = False
    protected_until: int = -1
    respawn_at: int = -1
    illusion_until: int = -1
    vulnerable_until: int = -1
    buff_until: int = -1
    heal_until: int = -1
    heal_rate: int = 0
    fortress_stay: int = 0
    action: Optional[dict] = None
    last_action: Optional[dict] = None
    forced_wait_tick: int = -1

    collected_value: int = 0
    normals: int = 0
    highs: int = 0
    damage: float = 0.0
    deaths: int = 0
    mission_done: Set[str] = field(default_factory=set)

    def protected(self, tick: int) -> bool:
        return tick < self.protected_until


@dataclass
class EventPlan:
    kind: str
    announce: int
    start: int
    end: int
    data: dict
    announced: bool = False


class Sim:
    def __init__(
        self,
        stage: int = 2,
        seed: int = 1,
        size: Optional[Tuple[int, int]] = None,
        obstacles: Optional[Sequence[Cell]] = None,
        resources: Optional[Sequence[Tuple[Cell, int]]] = None,
        spawns: Optional[Sequence[Cell]] = None,
        events: bool = True,
        vision: int = 4,
        reach: Optional[Cell] = None,
        session: str = "sim-session",
    ) -> None:
        self.rng = random.Random(seed)
        self.stage = stage
        w, h = size or ((20, 20) if stage == 1 else (24, 24))
        self.w, self.h = w, h
        self.vision = vision
        self.tick = 0
        self.total_ticks = 100 if stage == 1 else 500
        self.session = session
        self.obstacles: Set[Cell] = set(obstacles or [])
        self.temp_obstacles: Dict[Cell, int] = {}

        self.robots: List[Robot] = []
        if spawns is None:
            spawns = [(1, 1)] if stage == 1 else [
                (1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2)]
        for i, sp in enumerate(spawns):
            self.robots.append(Robot(team="ABCD"[i], pos=sp, spawn=sp))

        if resources is None:
            resources = self._gen_resources(12, 4)
        self.resources: List[Resource] = [Resource(f"r{i}", p, v) for i, (p, v) in enumerate(resources)]

        self.reach = reach or (w // 2, h // 2)
        self.mission_text = (
            f"获得至少两个普通资源；获得至少一个高价值资源；比赛结束前进入坐标 "
            f"[{self.reach[0]}, {self.reach[1]}]。"
            if stage == 2 else "练习采集：自主收集资源。"
        )
        self.missions = ([
            {"id": "collect_two_normal", "type": "normal_resources", "target": 2},
            {"id": "collect_high_value", "type": "high_resources", "target": 1},
            {"id": "reach_target", "type": "reach", "target": 1, "position": list(self.reach)},
        ] if stage == 2 else [])

        self.events: List[EventPlan] = []
        if events and stage == 2:
            self._gen_events()

        self.milestones: Dict[str, Dict[int, int]] = {}
        self.announcements: List[str] = []
        self.deaths_this_tick: List[Robot] = []
        self.log: List[str] = []
        self.total_attacks = 0

    # ---------------- 地图 ----------------
    def _gen_resources(self, n_normal: int, n_high: int) -> List[Tuple[Cell, int]]:
        used: Set[Cell] = set(self.obstacles) | {r.pos for r in self.robots}
        reserved = set()
        for sx, sy in [(r.spawn) for r in self.robots]:
            reserved |= {(sx + dx, sy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        out: List[Tuple[Cell, int]] = []
        for value, count in ((10, n_normal), (20, n_high)):
            for _ in range(count):
                for _try in range(800):
                    p = (self.rng.randrange(self.w), self.rng.randrange(self.h))
                    if p in used or p in reserved:
                        continue
                    used.add(p)
                    out.append((p, value))
                    break
        return out

    def _gen_events(self) -> None:
        w, h = self.w, self.h
        cx, cy = w // 2, h // 2
        # 规则：正式图（≥22×22）堡垒固定在 [11,11]–[13,13]（中心 [12,12]）
        fx, fy = (12, 12) if w >= 22 and h >= 22 else (cx, cy)
        plan = [
            ("explosion", 10, dict(damage=80, rect=(4, 4, 8, 8))),
            ("illusion", 40, dict(effect=50, rect=(7, 3, 11, 7))),
            ("angel", 70, dict(points=[(4, 11), (19, 11)], duration=20, effect=10, heal=10)),
            ("unrivaled", 100, dict(center=(fx, fy), size=3, duration=50, stay=10)),
            ("world_split", 130, dict(point=(7, 7), duration=30)),
            ("pepper", 160, dict(points=[(6, 7), (18, 16)], duration=30, damage=50)),
            ("purity", 190, dict(duration=20, reflect=20)),
            ("pan", 220, dict(duration=30, window=20, vuln=30, mult=2)),
        ]
        for kind, ann, data in plan:
            if kind == "explosion":
                start, end = ann + 5, ann + 6
            elif kind == "illusion":
                start, end = ann + 5, ann + 6 + data["effect"]
            elif kind in ("world_split", "pepper", "purity"):
                start, end = ann + 5, ann + 5 + data["duration"]
            elif kind == "pan":
                start, end = ann + 5, ann + 5 + data["vuln"]
            elif kind == "unrivaled":
                start, end = ann + 5, ann + 5 + data["duration"]
            else:
                start, end = ann + 5, ann + 5 + data["duration"]
            self.events.append(EventPlan(kind=kind, announce=ann, start=start, end=end, data=data))

    # ---------------- 观察 ----------------
    def _rel(self, dx: int, dy: int) -> str:
        rows = [["0"] * 9 for _ in range(9)]
        rows[4][4] = "1"
        if (dx, dy) != (0, 0):
            rows[dy + 4][dx + 4] = "2"
        return "\n".join("".join(r) for r in rows)

    def observation(self, r: Robot) -> str:
        ents = []
        if r.alive:
            for o in self.robots:
                if o is r or not o.alive:
                    continue
                if max(abs(o.pos[0] - r.pos[0]), abs(o.pos[1] - r.pos[1])) <= self.vision:
                    ents.append({
                        "type": "robot", "id": f"robot_{o.team}", "team": o.team, "robot_id": o.team,
                        "position": self._rel(o.pos[0] - r.pos[0], o.pos[1] - r.pos[1]),
                        "hp": o.hp, "defending": o.defending, "protected": o.protected(self.tick),
                    })
            for res in self.resources:
                if max(abs(res.pos[0] - r.pos[0]), abs(res.pos[1] - r.pos[1])) <= self.vision:
                    ents.append({
                        "type": "resource_high" if res.value >= 20 else "resource_normal",
                        "id": res.rid, "value": res.value,
                        "position": self._rel(res.pos[0] - r.pos[0], res.pos[1] - r.pos[1]),
                    })
            for (x, y) in list(self.obstacles) + [c for c, exp in self.temp_obstacles.items() if exp > self.tick]:
                if max(abs(x - r.pos[0]), abs(y - r.pos[1])) <= self.vision:
                    ents.append({"type": "obstacle",
                                 "position": self._rel(x - r.pos[0], y - r.pos[1])})

        obj = {
            "tick": self.tick,
            "session_id": self.session,
            "vision_radius": self.vision,
            "self": {
                "team": r.team, "robot_id": r.team, "position": list(r.pos),
                "hp": r.hp, "alive": r.alive, "defending": r.defending,
                "respawn_in": max(0, r.respawn_at - self.tick) if not r.alive else 0,
                "protected": r.protected(self.tick),
                "last_action": r.last_action,
            },
            "visible_entities": ents,
            "score": {
                "resource_value": r.collected_value,
                "effective_damage": round(r.damage, 6),
                "mission_completed": len(r.mission_done),
                "mission_completed_ids": sorted(r.mission_done),
                "invalid_actions": 0,
            },
            "mission_text": self.mission_text,
            "missions": self.missions,
            "map": {"width": self.w, "height": self.h},
            "event_text": "\n".join(self.announcements[-6:]),
        }
        if not r.alive:
            obj["visible_entities"] = []
        return json.dumps(obj, ensure_ascii=False)

    # ---------------- 公告 ----------------
    def _announce(self) -> None:
        for e in self.events:
            if e.announced or self.tick < e.announce:
                continue
            e.announced = True
            d = e.data
            left = e.start - self.tick
            if e.kind == "explosion":
                x1, y1, x2, y2 = d["rect"]
                text = (f"随机事件预告：{left} Tick 后（Tick {e.start}），左上角 [{x1},{y1}]、"
                        f"右下角 [{x2},{y2}] 的矩形区域将发生爆炸，范围内所有存活战车扣除 "
                        f"{d['damage']} HP；出生保护免疫。")
            elif e.kind == "illusion":
                x1, y1, x2, y2 = d["rect"]
                text = (f"随机事件预告：Tick {e.start}（{left} Tick 后），矩形 [{x1},{y1}] 至 "
                        f"[{x2},{y2}] 内战车将中幻术，接下来 {d['effect']} Tick 移动上下、左右均颠倒；复活不解除。")
            elif e.kind == "angel":
                text = (f"随机事件预告：{left} Tick 后（Tick {e.start}），天使恩泽将在 {d['points']} "
                        f"出现并存在 {d['duration']} Tick。进入点位可获得持续 {d['effect']} Tick、"
                        f"每 Tick 回血 {d['heal']} HP 的增益，上限 100 HP；离开再进入可以刷新。")
            elif e.kind == "unrivaled":
                text = (f"随机事件预告：Tick {e.start}（{left} Tick 后），地图中央 {d['center']} 将出现 "
                        f"{d['size']}×{d['size']} 堡垒并存在 {d['duration']} Tick。连续停留 {d['stay']} Tick 将获得"
                        f"「天下无双」：ATTACK 命中自身周围 5×5 内所有敌人，普通伤害 50 HP、防御伤害 10 HP；"
                        f"增益于堡垒消失时结束。")
            elif e.kind == "world_split":
                text = (f"随机事件预告：Tick {e.start}（{left} Tick 后），以 {d['point']} 为交点的整行整列将"
                        f"生成十字障碍，存在 {d['duration']} Tick。障碍生成时十字线上的战车直接死亡并立即回出生点重生。")
            elif e.kind == "pepper":
                text = (f"随机事件预告：{left} Tick 后（Tick {e.start}），火爆辣椒点 {d['points']} 出现并存在 "
                        f"{d['duration']} Tick。每次进入点位会触发 1 Tick 的整行整列十字火线，交点自身除外，"
                        f"线上战车扣除 {d['damage']} HP。")
            elif e.kind == "purity":
                text = (f"随机事件预告：Tick {e.start}（{left} Tick 后）起，全场进入持续 {d['duration']} Tick 的"
                        f"「洁身自好」：攻击正在防御的敌人会让攻击者自身受到 {d['reflect']} HP 伤害，并且无法伤害该目标。")
            else:
                text = (f"随机事件预告：Tick {e.start}（{left} Tick 后）起，接下来的 {d['window']} Tick 中，"
                        f"成功移动的战车将获得易伤，受到的战斗伤害乘以 {d['mult']}；易伤统一在 Tick "
                        f"{e.start + d['vuln']} 结束。")
            self.announcements.append(text)
            self.log.append(f"[公告 t={self.tick}] {e.kind}")

    # ---------------- 事件激活 ----------------
    def _activate_events(self) -> None:
        t = self.tick
        for e in self.events:
            if e.start != t:
                continue
            d = e.data
            if e.kind == "explosion":
                x1, y1, x2, y2 = d["rect"]
                for r in self.robots:
                    if not r.alive or r.protected(t):
                        continue
                    if x1 <= r.pos[0] <= x2 and y1 <= r.pos[1] <= y2:
                        self._env_damage(r, d["damage"], t)
            elif e.kind == "world_split":
                px, py = d["point"]
                for r in self.robots:
                    if r.alive and (r.pos[0] == px or r.pos[1] == py):
                        self._kill(r, t, [], reason="world_split")
                        self._respawn(r, t, immediate=True)
                        r.forced_wait_tick = t
                for x in range(self.w):
                    self.temp_obstacles[(x, py)] = e.end
                for y in range(self.h):
                    self.temp_obstacles[(px, y)] = e.end
            elif e.kind == "illusion":
                x1, y1, x2, y2 = d["rect"]
                for r in self.robots:
                    if r.alive and x1 <= r.pos[0] <= x2 and y1 <= r.pos[1] <= y2:
                        r.illusion_until = max(r.illusion_until, t + d["effect"])
        self.temp_obstacles = {c: exp for c, exp in self.temp_obstacles.items() if exp > t}

    # ---------------- 伤害 ----------------
    def _env_damage(self, r: Robot, amount: int, t: int) -> None:
        if not r.alive:
            return
        r.hp -= min(amount, r.hp)
        if r.hp <= 0:
            self._kill(r, t, [], reason="env")

    def _kill(self, r: Robot, t: int, credit: List[Robot], reason: str = "combat") -> None:
        if not r.alive:
            return
        r.hp = 0
        r.alive = False
        r.defending = False
        r.deaths += 1
        r.respawn_at = t + 4
        r.fortress_stay = 0
        self.deaths_this_tick.append(r)
        teams = {c.team: c for c in credit if c.team != r.team and c.alive}
        if teams and reason in ("combat", "pepper", "purity"):
            share = 60.0 / len(teams)
            for c in teams.values():
                c.damage += share

    def _respawn(self, r: Robot, t: int, immediate: bool = False) -> None:
        r.alive = True
        r.hp = 100
        r.pos = r.spawn
        r.protected_until = t + 2 if not immediate else t + 2
        r.respawn_at = -1
        r.last_action = {"tick": t, "action": "wait", "moved": False}

    # ---------------- 主循环 ----------------
    def run(self, controllers, verbose: bool = False) -> dict:
        for t in range(self.total_ticks):
            self.tick = t
            self.deaths_this_tick = []
            self._announce()
            self._activate_events()

            # 3) 固化动作
            for i, r in enumerate(self.robots):
                if not r.alive:
                    r.action = {"action": "wait"}
                    r.defending = False
                    continue
                if r.forced_wait_tick == t:
                    r.action = {"action": "wait"}
                    r.forced_wait_tick = -1
                    r.defending = False
                    continue
                act = controllers[i](self, r) or {"action": "wait"}
                r.action = act
                r.defending = act.get("action") == "defend"

            purity = any(e.kind == "purity" and e.start <= t < e.end for e in self.events)

            # 4) 战斗：victim -> [(source, nominal, is_reflect, is_aoe)]
            table: Dict[Robot, List[Tuple[Robot, int, bool, bool]]] = {}
            for r in self.robots:
                if not r.alive or r.action.get("action") != "attack" or r.protected(t):
                    continue
                aoe = r.buff_until > t
                hits: List[Tuple[Robot, int]] = []
                if aoe:
                    for o in self.robots:
                        if o is r or not o.alive:
                            continue
                        if max(abs(o.pos[0] - r.pos[0]), abs(o.pos[1] - r.pos[1])) <= 2:
                            hits.append((o, 1))
                else:
                    d = r.action.get("direction")
                    if d not in DIR_VEC:
                        continue
                    vx, vy = DIR_VEC[d]
                    for k in (1, 2):
                        c = (r.pos[0] + vx * k, r.pos[1] + vy * k)
                        hit = next((o for o in self.robots if o.alive and o is not r and o.pos == c), None)
                        if hit:
                            hits.append((hit, k))
                            break
                for target, _ in hits:
                    if target.protected(t):
                        continue
                    if purity and target.defending:
                        table.setdefault(r, []).append((r, 20, True, aoe))
                        continue
                    base = (10 if target.defending else 50) if aoe else (10 if target.defending else 20)
                    if target.vulnerable_until > t:
                        base *= 2
                    table.setdefault(target, []).append((r, base, False, aoe))
                    self.total_attacks += 1

            for victim, hits in table.items():
                total = sum(h[1] for h in hits)
                if total <= 0 or victim.hp <= 0:
                    continue
                real_total = min(total, victim.hp)
                for src, nominal, is_reflect, _aoe in hits:
                    if not is_reflect and src is not victim:
                        src.damage += real_total * nominal / total
                victim.hp -= real_total
                if victim.hp <= 0:
                    credit = [src for src, nominal, is_reflect, _aoe in hits
                              if nominal > 0 and src is not victim]
                    self._kill(victim, t, credit, reason="purity" if any(h[2] for h in hits) else "combat")

            # 5) 移动
            intents: Dict[Robot, Tuple[Cell, str]] = {}
            occupied = {r.pos for r in self.robots if r.alive}
            for r in self.robots:
                if not r.alive:
                    continue
                if r.action.get("action") != "move":
                    r.last_action = {"tick": t, "action": r.action.get("action", "wait"), "moved": False}
                    continue
                d = r.action.get("direction")
                if d not in DIR_VEC:
                    r.last_action = {"tick": t, "action": "move", "moved": False, "error": "E_SCHEMA"}
                    continue
                if r.illusion_until > t:
                    d = OPPOSITE[d]
                vx, vy = DIR_VEC[d]
                target = (r.pos[0] + vx, r.pos[1] + vy)
                if not (0 <= target[0] < self.w and 0 <= target[1] < self.h) \
                        or target in self.obstacles \
                        or self.temp_obstacles.get(target, 0) > t \
                        or target in occupied:
                    r.last_action = {"tick": t, "action": "move", "moved": False,
                                     "effective_direction": d, "error": "E_BLOCKED"}
                    continue
                intents[r] = (target, d)

            counts: Dict[Cell, int] = {}
            for target, _d in intents.values():
                counts[target] = counts.get(target, 0) + 1
            for r, (target, d) in intents.items():
                if counts[target] > 1:
                    r.last_action = {"tick": t, "action": "move", "moved": False,
                                     "effective_direction": d, "error": "E_BLOCKED"}
                    continue
                r.pos = target
                r.last_action = {"tick": t, "action": "move", "moved": True,
                                 "effective_direction": d}
                self._on_enter(r, t)

            # 7) 堡垒驻留
            for e in self.events:
                if e.kind != "unrivaled" or not (e.start <= t < e.end):
                    continue
                cx, cy = e.data["center"]
                for r in self.robots:
                    if not r.alive:
                        continue
                    inside = abs(r.pos[0] - cx) <= 1 and abs(r.pos[1] - cy) <= 1
                    if not inside:
                        r.fortress_stay = 0
                        continue
                    # 规则 §8.5："移动后位于堡垒内"才累计，原地 WAIT/攻击不计数
                    moved = (r.last_action or {}).get("action") == "move" and \
                            bool((r.last_action or {}).get("moved"))
                    if moved:
                        r.fortress_stay += 1
                        if r.fortress_stay >= e.data["stay"]:
                            r.buff_until = e.end

            for r in self.robots:
                if r.alive and r.heal_until > t and r.hp < 100:
                    r.hp = min(100, r.hp + r.heal_rate)

            # 8) 死亡扣 60（含事件死亡）
            for r in self.deaths_this_tick:
                r.damage = max(0.0, r.damage - 60.0)

            # 9) 收集 / 任务 / 复活
            for r in self.robots:
                if not r.alive:
                    if r.respawn_at > 0 and t >= r.respawn_at - 1:
                        self._respawn(r, t)
                    continue
                for th in (60, 100, 120, 160, 200):
                    if r.collected_value >= th and th not in self.milestones.setdefault(r.team, {}):
                        self.milestones[r.team][th] = t
                for res in list(self.resources):
                    if res.pos == r.pos:
                        r.collected_value += res.value
                        if res.value >= 20:
                            r.highs += 1
                        else:
                            r.normals += 1
                        self.resources.remove(res)
                if self.stage == 2:
                    self._check_missions(r)

            if verbose and t % 50 == 0:
                self.log.append(f"[t={t}] " + " | ".join(
                    f"{r.team}@{r.pos} hp={r.hp} R={r.collected_value} D={r.damage:.0f}"
                    for r in self.robots))

        return self.result()

    def _on_enter(self, r: Robot, t: int) -> None:
        for e in self.events:
            if e.kind == "angel" and e.start <= t < e.end:
                if r.pos in [tuple(p) for p in e.data["points"]]:
                    r.heal_until = t + e.data["effect"]
                    r.heal_rate = e.data["heal"]
            elif e.kind == "pan" and e.start <= t < e.start + e.data["window"]:
                r.vulnerable_until = max(r.vulnerable_until, e.start + e.data["vuln"])
            elif e.kind == "pepper" and e.start <= t < e.end:
                if r.pos in [tuple(p) for p in e.data["points"]]:
                    self._pepper_fire(r, e.data["damage"], t)

    def _pepper_fire(self, trigger: Robot, damage: int, t: int) -> None:
        px, py = trigger.pos
        for other in self.robots:
            if other is trigger or not other.alive or other.protected(t):
                continue
            if other.pos[0] == px or other.pos[1] == py:
                other.hp -= min(damage, other.hp)
                if other.hp <= 0:
                    self._kill(other, t, [trigger], reason="pepper")

    def _check_missions(self, r: Robot) -> None:
        for m in self.missions:
            mid = m["id"]
            if mid in r.mission_done:
                continue
            if m["type"] == "normal_resources" and r.normals >= m["target"]:
                r.mission_done.add(mid)
            elif m["type"] == "high_resources" and r.highs >= m["target"]:
                r.mission_done.add(mid)
            elif m["type"] == "reach" and tuple(m["position"]) == r.pos:
                r.mission_done.add(mid)

    def score(self, r: Robot, ai_ok: bool = True) -> float:
        if self.stage == 1:
            return 40.0 * r.collected_value / 200.0
        res = 30.0 * min(r.collected_value / 120.0, 1.0)
        dmg = 15.0 * min(max(0.0, r.damage) / 500.0, 1.0)
        m = len(r.mission_done) if ai_ok else 0
        return res + dmg + 5.0 * m

    def result(self) -> dict:
        return {"stage": self.stage, "size": (self.w, self.h), "rows": [{
            "team": r.team, "score": round(self.score(r), 3),
            "resource": r.collected_value, "damage": round(r.damage, 1),
            "missions": len(r.mission_done), "deaths": r.deaths,
            "normals": r.normals, "highs": r.highs,
        } for r in self.robots]}


# ---------------------------------------------------------------------------
# 控制器
# ---------------------------------------------------------------------------
class BrainController:
    def __init__(self, sim: Sim, team: str = "A", decision: str = "balanced",
                 params: Optional[Params] = None) -> None:
        self.fake = FakeLLM(sim)
        self.brain = Brain(params or Params(), decision=decision, llm=self.fake,
                           logger=lambda *_a: None)

    def __call__(self, sim: Sim, r: Robot) -> dict:
        act = self.brain.step(sim.observation(r))
        if act is None:
            return {"action": "wait"}
        kind, direction = act
        out = {"action": kind}
        if kind in ("move", "attack"):
            out["direction"] = direction
        return out


class FakeLLM:
    """离线仿真的"LLM 替身"：把已公告事件直接写成结构化 JSON。

    真实比赛必须走 /v1/chat/completions；这里只用于无网络环境下验证策略闭环，
    是测试工具的一部分，不属于参赛程序的决策逻辑。
    """

    def __init__(self, sim: Sim) -> None:
        self.sim = sim
        self.enabled = True
        self.quota_exhausted = False
        self._out = []
        self.success = 0

    def submit(self, kind: str, prompt: str, tick: int, session: str) -> bool:
        events = []
        for e in self.sim.events:
            if not e.announced:
                continue
            item = {"type": e.kind, "start_tick": e.start, "end_tick": e.end}
            d = e.data
            if e.kind == "explosion":
                x1, y1, x2, y2 = d["rect"]
                item["rect"] = [[x1, y1], [x2, y2]]
            elif e.kind == "illusion":
                x1, y1, x2, y2 = d["rect"]
                item["rect"] = [[x1, y1], [x2, y2]]
            elif e.kind == "angel":
                item["points"] = [list(p) for p in d["points"]]
            elif e.kind == "unrivaled":
                item["center"] = list(d["center"])
                item["rect"] = [[d["center"][0] - 1, d["center"][1] - 1],
                                [d["center"][0] + 1, d["center"][1] + 1]]
            elif e.kind == "world_split":
                item["center"] = list(d["point"])
            elif e.kind == "pepper":
                item["points"] = [list(p) for p in d["points"]]
            events.append(item)
        payload = {"events": events, "focus": "balanced", "aggression": 0.6, "tactic": "离线仿真"}
        self._out.append(type("R", (), {"ok": True, "content": json.dumps(payload),
                                        "session": session, "tick": tick})())
        self.success += 1
        return True

    def drain(self):
        out, self._out = self._out, []
        return out

    def stats(self):
        return {"success": self.success, "calls": self.success, "failures": 0,
                "quota_exhausted": False, "last_error": "", "latency": 0.0}

    def start(self):
        pass

    def stop(self):
        pass


class SimpleBot:
    """陪练：视野受限的贪心采集 + 顺手攻击，接近真实参赛队伍的强度。"""

    def __init__(self, mode: str = "collect", seed: int = 0, cheat: bool = False) -> None:
        self.mode = mode
        self.rng = random.Random(seed)
        self.cheat = cheat
        self.known: Dict[Cell, int] = {}      # 资源位置 -> 价值
        self.seen: Set[Cell] = set()
        self.aim: Optional[Cell] = None
        self.aim_until = -1

    def _scan(self, sim: Sim, r: Robot) -> None:
        for res in sim.resources:
            if self.cheat or max(abs(res.pos[0] - r.pos[0]), abs(res.pos[1] - r.pos[1])) <= 4:
                self.known[res.pos] = res.value
        alive = {res.pos for res in sim.resources}
        for p in list(self.known):
            if p not in alive:
                del self.known[p]
        for y in range(max(0, r.pos[1] - 4), min(sim.h, r.pos[1] + 5)):
            for x in range(max(0, r.pos[0] - 4), min(sim.w, r.pos[0] + 5)):
                self.seen.add((x, y))

    def __call__(self, sim: Sim, r: Robot) -> dict:
        if not r.alive:
            return {"action": "wait"}
        self._scan(sim, r)
        for d, (vx, vy) in DIR_VEC.items():
            for k in (1, 2):
                c = (r.pos[0] + vx * k, r.pos[1] + vy * k)
                if any(o.alive and o is not r and o.pos == c for o in sim.robots):
                    return {"action": "attack", "direction": d}

        target: Optional[Cell] = None
        if self.mode == "hunter":
            cands = [o for o in sim.robots if o.alive and o is not r]
            if cands:
                target = min(cands, key=lambda o: abs(o.pos[0] - r.pos[0])
                             + abs(o.pos[1] - r.pos[1])).pos
        if target is None and r.hp < 40:
            heals = [e for e in sim.events if e.kind == "angel" and e.start <= sim.tick < e.end]
            if heals and min(abs(heals[0].data["points"][0][0] - r.pos[0]),
                             abs(heals[0].data["points"][0][1] - r.pos[1])) < 8:
                target = tuple(heals[0].data["points"][0])
        if target is None and self.known:
            target = min(self.known, key=lambda p: abs(p[0] - r.pos[0]) + abs(p[1] - r.pos[1]))
        if target is None:
            if self.aim is None or sim.tick > self.aim_until:
                unseen = [(x, y) for y in range(1, sim.h - 1) for x in range(1, sim.w - 1)
                          if (x, y) not in self.seen]
                self.aim = self.rng.choice(unseen) if unseen else (sim.w // 2, sim.h // 2)
                self.aim_until = sim.tick + 25
            target = self.aim
        if target is None:
            return {"action": "wait"}
        step = self._step(sim, r.pos, target)
        if step:
            return {"action": "move", "direction": step}
        self.aim = None
        return {"action": "wait"}

    def _step(self, sim: Sim, start: Cell, goal: Cell) -> Optional[str]:
        blocked = sim.obstacles | {c for c, exp in sim.temp_obstacles.items() if exp > sim.tick}
        blocked |= {o.pos for o in sim.robots if o.alive and o.pos != start and o.pos != goal}
        q = deque([start])
        prev = {start: None}
        while q:
            cur = q.popleft()
            if cur == goal:
                break
            for vx, vy in DIR_VEC.values():
                n = (cur[0] + vx, cur[1] + vy)
                if n in prev or not (0 <= n[0] < sim.w and 0 <= n[1] < sim.h) or n in blocked:
                    continue
                prev[n] = cur
                q.append(n)
        if goal not in prev:
            return None
        node = goal
        while prev[node] is not None and prev[node] != start:
            node = prev[node]
        if prev.get(node) != start:
            return None
        dx, dy = node[0] - start[0], node[1] - start[1]
        for d, (vx, vy) in DIR_VEC.items():
            if (vx, vy) == (dx, dy):
                return d
        return None


# ---------------------------------------------------------------------------
def load_map_file(path: str) -> dict:
    """读取地图编辑器导出的两种 JSON：geometry（map+robots）或 practice（完整配置）。"""
    with open(path, encoding="utf-8") as fh:
        data = json.load(fh)
    mp = data.get("map") or {}
    w, h = int(mp.get("width", 24)), int(mp.get("height", 24))
    obstacles = [tuple(p) for p in mp.get("obstacles", [])]
    resources = [(tuple(r["position"]), int(r.get("value", 10))) for r in mp.get("resources", [])]
    robots = sorted(data.get("robots") or [], key=lambda r: str(r.get("team", "")))
    spawns = [tuple(r["spawn"]) for r in robots] or [(1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2)]
    stage = int(data.get("stage", 2 if len(spawns) > 1 else 1))
    reach = None
    for m in data.get("missions", []):
        if isinstance(m, dict) and m.get("type") == "reach" and isinstance(m.get("position"), list):
            reach = (int(m["position"][0]), int(m["position"][1]))
    if reach is None:
        reach = (12, 12) if w >= 22 and h >= 22 else (w // 2, h // 2)
    return {"width": w, "height": h, "stage": stage, "obstacles": obstacles,
            "resources": resources, "spawns": spawns, "reach": reach,
            "name": os.path.basename(path)}


def build_sim_from_file(path: str, seed: int = 1) -> Sim:
    meta = load_map_file(path)
    return Sim(stage=meta["stage"], seed=seed, size=(meta["width"], meta["height"]),
               obstacles=meta["obstacles"], resources=meta["resources"],
               spawns=meta["spawns"], reach=meta["reach"],
               session=f"map-{meta['name']}")


def build_random_sim(stage: int, seed: int) -> Sim:
    rng = random.Random(seed)
    size = (20, 20) if stage == 1 else (24, 24)
    spawns = ([(1, 1)] if stage == 1 else
              [(1, 1), (size[0] - 2, 1), (1, size[1] - 2), (size[0] - 2, size[1] - 2)])
    reserved = set(spawns) | {(size[0] // 2, size[1] // 2)}
    for sx, sy in spawns:
        reserved |= {(sx + dx, sy + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    obstacles = set()
    while len(obstacles) < int(size[0] * size[1] * 0.035):
        p = (rng.randrange(size[0]), rng.randrange(size[1]))
        if p in reserved:
            continue
        obstacles.add(p)
    return Sim(stage=stage, seed=seed, size=size, obstacles=list(obstacles), spawns=spawns)


def build_practice_sim(stage: int) -> Sim:
    p = PRACTICE_MAP
    return Sim(stage=stage, seed=7, size=(p["width"], p["height"]),
               obstacles=p["obstacles"], resources=p["resources"], spawns=p["spawns"],
               reach=p["reach"], session="practice-sim")


def run_match(stage: int, seed: int, decision: str = "balanced", verbose: bool = False,
              preset: str = "random", opponents: int = 3,
              params: Optional[Params] = None, map_path: str = "") -> dict:
    if map_path:
        sim = build_sim_from_file(map_path, seed=seed)
    elif preset == "practice":
        sim = build_practice_sim(stage)
    else:
        sim = build_random_sim(stage, seed)
    controllers = [BrainController(sim, "A", decision, params=params)]
    if stage == 2:
        modes = ["collect", "hunter", "collect"]
        for i in range(opponents):
            controllers.append(SimpleBot(modes[i % len(modes)], seed + i))
    else:
        sim.robots = sim.robots[:1]
    result = sim.run(controllers, verbose=verbose)
    if verbose:
        print("\n".join(sim.log))
        print(f"攻击次数 {sim.total_attacks}")
    return result


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, default=2)
    ap.add_argument("--matches", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1)
    ap.add_argument("--decision", default="balanced")
    ap.add_argument("--preset", default="random", choices=("random", "practice"))
    ap.add_argument("--map", default="", help="单张地图 JSON（geometry 或 practice）")
    ap.add_argument("--maps", default="", help="地图目录：批量跑 *.practice.json")
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if args.maps:
        files = sorted(glob.glob(os.path.join(args.maps, "*.practice.json")))
        if not files:
            print(f"{args.maps} 下没有 *.practice.json")
            return
        total = 0.0
        for idx, f in enumerate(files):
            out = run_match(2, args.seed + idx, args.decision, preset="random", map_path=f)
            row = out["rows"][0]
            total += row["score"]
            print(f"{os.path.basename(f):34s} 得分 {row['score']:6.2f} | 资源 {row['resource']:3d} | "
                  f"伤害 {row['damage']:6.0f} | 任务 {row['missions']} | 死亡 {row['deaths']}")
        print(f"\n{len(files)} 张地图平均得分 {total / len(files):.2f}")
        return

    scores, res, dmg, mis = [], [], [], []
    for i in range(args.matches):
        out = run_match(args.stage, args.seed + i, args.decision,
                        verbose=args.verbose and i == 0, preset=args.preset,
                        map_path=args.map)
        row = out["rows"][0]
        scores.append(row["score"])
        res.append(row["resource"])
        dmg.append(row["damage"])
        mis.append(row["missions"])
        print(f"[match {i + 1:02d}] 得分 {row['score']:6.2f} | 资源 {row['resource']:3d} | "
              f"伤害 {row['damage']:7.1f} | 任务 {row['missions']} | 死亡 {row['deaths']}")
    n = len(scores)
    print(f"\n平均得分 {sum(scores) / n:.2f} | 最高 {max(scores):.2f} | 最低 {min(scores):.2f}")
    print(f"平均资源 {sum(res) / n:.1f} | 平均伤害 {sum(dmg) / n:.1f} | 平均任务 {sum(mis) / n:.2f}")


if __name__ == "__main__":
    main()
