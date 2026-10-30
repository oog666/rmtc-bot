"""决策中枢：把感知 → 世界模型 → 策略 → 动作串起来。

一个 Tick 的处理顺序（严格非阻塞）：
1. 解析感知（失败即丢弃，不影响进程）；
2. 新场次/尺寸变化则清空全部记忆（规则要求换场清缓存）；
3. 融合地图、资源、敌车记忆；
4. 拉取 LLM 异步结果（如果有）并编译成事件约束；
5. 必要时投递新的 LLM 请求（首次简报 / 公告变化）；
6. 用**当帧**信息构建代价场，交给阶段策略选动作；
7. 处理幻术方向反转，缓存并返回动作。
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any, Dict, List, Optional, Tuple

from .collector import Collector
from .config import Params
from .events import EventMemory
from .llm import LLMClient
from .protocol import DIR_VEC, DIRS, OPPOSITE, Cell, Entity, Observation, parse_perception
from .strategies import LoopGuard, Strategy, build_strategy
from .world import INF, EnemyTrack, Field, WorldMap, build_field


def _log_default(msg: str) -> None:
    print(msg, flush=True)


class Brain:
    def __init__(
        self,
        params: Optional[Params] = None,
        decision: str = "balanced",
        llm: Optional[LLMClient] = None,
        logger=_log_default,
    ) -> None:
        self.params = (params or Params()).with_decision(decision)
        self.decision = decision
        self.llm = llm
        self.log = logger
        self.events = EventMemory()

        self.world: Optional[WorldMap] = None
        self.obs: Optional[Observation] = None
        self.field: Optional[Field] = None
        self.tick = -1
        self.session = ""
        self.stage = 1
        self.total_ticks = self.params.stage1_ticks
        self.seen_enemy_ever = False

        # 移动失败后的临时换向（破解"两车抢同一格"的死锁）
        self.avoid_dir: Optional[str] = None
        self.avoid_until = -1

        # 阵亡点记忆：复活后短时间内把上次阵亡格视为临时障碍，强制换路线
        self.death_cells: dict = {}
        self.last_hp: Optional[int] = None
        self.took_damage = False

        # 幻术自校正
        self.inverted = False
        self.invert_until = -1
        self.last_sent_dir: Optional[str] = None

        # 目标锁（抑制抖动）
        self.lock_cell: Optional[Cell] = None
        self.lock_kind: str = ""
        self.lock_until = -1
        self.lock_score = 0.0

        # 堡垒增益（感知不含 buff 字段，只能本地记账）
        self.fortress_stay = 0
        self.buff_until = -1
        self._last_fortress_tick = -1

        # 缓存动作（同 Tick 重发时复用）
        self.cached: Optional[Tuple[str, Optional[str]]] = None
        # 策略主动选择"原地不动"（如堡垒驻留），兜底逻辑不得覆盖
        self.intentional_wait = False

        # LLM 记账
        self._brief_sent = False
        self._brief_attempts = 0
        self._last_llm_attempt = 0.0
        self._event_hash = ""
        self._llm_ok = 0
        self.strategy_errors = 0

        self.collector = Collector(self.params)
        self.current_decision = decision
        self.strategy = build_strategy(decision, self.params)
        self.guard = LoopGuard()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def reset(self, obs: Observation) -> None:
        self.world = WorldMap(obs.map_w, obs.map_h)
        self.events.clear()
        self.tick = -1
        self.session = obs.session_id
        self.inverted = False
        self.invert_until = -1
        self.last_sent_dir = None
        self.death_cells = {}
        self.last_hp = None
        self.took_damage = False
        self.avoid_dir = None
        self.avoid_until = -1
        self.lock_cell = None
        self.lock_kind = ""
        self.lock_until = -1
        self.lock_score = 0.0
        self.fortress_stay = 0
        self.buff_until = -1
        self.cached = None
        self._brief_sent = False
        self._brief_attempts = 0
        self._last_llm_attempt = 0.0
        self._event_hash = ""
        self._llm_ok = 0
        self.strategy_errors = 0
        self.strategy.reset()
        self.guard.reset()
        self.log(f"[brain] 新场次 {obs.session_id} 地图 {obs.map_w}x{obs.map_h} "
                 f"策略 {self.current_decision}")

    # ------------------------------------------------------------------
    # 主入口
    # ------------------------------------------------------------------
    def step(self, raw: str, now: Optional[float] = None) -> Optional[Tuple[str, Optional[str]]]:
        now = time.time() if now is None else now
        obs = parse_perception(raw)
        if obs is None:
            return None

        if self.world is None or obs.session_id != self.session or \
                self.world.w != obs.map_w or self.world.h != obs.map_h:
            self.reset(obs)

        if obs.tick < self.tick:
            return None                                   # 过期帧，直接丢弃
        if obs.tick == self.tick and self.cached is not None:
            return self.cached                            # 同 Tick 重发：复用缓存动作

        self.obs = obs
        self.tick = obs.tick
        self._detect_stage(obs)

        self.world.update(obs)
        self.world.prune_temp(obs.tick)
        self._ingest_llm()
        self._sync_llm(obs, now)
        self._apply_illusion_hint(obs)
        self._track_move_failure(obs)
        self._track_death(obs)
        self.took_damage = (self.last_hp is not None and obs.me.hp < self.last_hp)
        self.last_hp = obs.me.hp

        # 防循环：记录轨迹；一旦发现两格之间往返，强制改走远点
        self.guard.observe(obs.me.pos, obs.tick)
        self.guard.prune(obs.tick)
        if self.guard.oscillating() and obs.tick >= self.guard.escape_until:
            cell = self._far_cell()
            if cell is not None:
                self.guard.escape_cell = cell
                self.guard.escape_until = obs.tick + 10
                self.log(f"[brain] 检测到往返抖动，改走 {cell} ({self.current_decision})")

        try:
            self.field = build_field(
                self.world,
                obs.me.pos,
                obs.tick,
                hp=obs.me.hp,
                danger=frozenset(self.events.danger_cells(obs.tick, obs.map_w, obs.map_h)),
                blocked_extra=frozenset(self.events.blocked_cells(obs.tick, obs.map_w, obs.map_h))
                | frozenset(c for c, exp in self.death_cells.items()
                            if exp > obs.tick and c != obs.me.pos),
                enemies=self.visible_enemies(),
                unknown_cost=self.params.unknown_step_cost,
                enemy_cost=self.params.enemy_cell_cost,
                threat_low=self.strategy.threat_low,
                threat_high=self.strategy.threat_high,
            )
        except Exception as exc:                          # 规划失败也不能崩
            self.log(f"[brain] 代价场构建失败: {exc}")
            self.field = None

        self.intentional_wait = False
        if not obs.me.alive:
            action: Tuple[str, Optional[str]] = ("wait", None)
        elif self.field is None:
            action = ("wait", None)
        else:
            try:
                action = self.strategy.decide(self) if self.stage == 2 else self.collector.decide(self)
            except Exception as exc:
                self.strategy_errors += 1
                if self.strategy_errors <= 3 or self.strategy_errors % 50 == 0:
                    self.log(f"[brain] 策略异常 tick={obs.tick}: {type(exc).__name__}: {exc}")
                action = ("wait", None)

            # 抖动逃生优先级高于普通移动，但低于攻击/防御
            if (self.guard.escape_until > obs.tick and self.guard.escape_cell is not None
                    and action[0] in ("move", "wait")):
                mv = self.move_to(self.guard.escape_cell)
                if mv is not None:
                    action = mv

            # 兜底：活着、周围没有敌人，却连续 3 Tick 不动 → 强制挪窝（防止任何策略写错后卡死）
            if (action[0] == "wait" and self.guard.same_pos >= 3
                    and not self.visible_enemies() and not self.intentional_wait):
                cell = self._far_cell()
                mv = self.move_to(cell) if cell is not None else None
                if mv is not None:
                    action = mv

        action = self._sanitize(action)
        self.cached = action
        return action

    # ------------------------------------------------------------------
    # 基础能力
    # ------------------------------------------------------------------
    def _detect_stage(self, obs: Observation) -> None:
        if obs.entities and any(e.kind == "robot" for e in obs.entities):
            self.seen_enemy_ever = True
        if obs.missions or obs.map_w >= 22 or self.seen_enemy_ever:
            self.stage = 2
        else:
            self.stage = 1
        self.total_ticks = self.params.stage2_ticks if self.stage == 2 else self.params.stage1_ticks

    def _sanitize(self, action: Tuple[str, Optional[str]]) -> Tuple[str, Optional[str]]:
        kind, direction = action
        if kind not in ("move", "attack", "defend", "wait"):
            return ("wait", None)
        if kind in ("move", "attack"):
            if direction not in DIRS:
                return ("wait", None)
            self.last_sent_dir = direction
        if kind == "move" and self.inverted and self.tick < self.invert_until:
            direction = OPPOSITE.get(direction or "", direction)
            self.last_sent_dir = direction
        return (kind, direction)

    def _apply_illusion_hint(self, obs: Observation) -> None:
        """两条证据链：
        1) 上一 Tick 的回执：请求方向与 effective_direction 相反 → 当前被反转（最可靠）；
        2) LLM 给出的幻术矩形 + 自身坐标：激活时自己在矩形内 → 预期会被反转。
        """
        la = obs.me.last_action
        if isinstance(la, dict) and la.get("action") == "move" and la.get("moved"):
            eff = la.get("effective_direction")
            req = self.last_sent_dir
            if isinstance(eff, str) and req in DIRS and eff in DIRS:
                if eff == req:
                    self.inverted = False
                elif eff == OPPOSITE.get(req):
                    self.inverted = True
                    self.invert_until = max(self.invert_until, obs.tick + 50)
                    self.log(f"[brain] 检测到方向反转，Tick {obs.tick} 起按反转补偿")

        exp = self.events.illusion_affects(obs.me.pos, obs.tick)
        if exp is not None and exp > obs.tick:
            self.inverted = True
            self.invert_until = max(self.invert_until, exp)
        elif self.invert_until > 0 and obs.tick >= self.invert_until:
            self.inverted = False

    # ------------------------------------------------------------------
    # 观测派生
    # ------------------------------------------------------------------
    def visible_enemies(self) -> List[Entity]:
        if not self.obs:
            return []
        return [e for e in self.obs.entities if e.kind == "robot" and e.team != self.obs.me.team]

    def huntable_enemies(self) -> List[EnemyTrack]:
        """可见敌车 + 近期记忆（记忆用于追猎，不用于威胁判定）。"""
        if not self.obs:
            return []
        out: Dict[str, EnemyTrack] = {}
        for e in self.visible_enemies():
            out[e.team] = EnemyTrack(e.team, e.pos, e.hp if e.hp is not None else 100,
                                     e.defending, e.protected, self.obs.tick)
        if self.world:
            for team, trk in self.world.enemies.items():
                if team in out:
                    continue
                if trk.age(self.obs.tick) <= 8:
                    out[team] = trk
        return list(out.values())

    def remaining_ticks(self) -> int:
        return max(0, self.total_ticks - self.tick)

    def point_per_value(self) -> float:
        """1 点资源价值折算成多少比赛分。资源封顶后只用于同分排名。"""
        if self.stage == 1:
            return 40.0 / 200.0
        if self.obs and self.obs.resource_value >= self.params.resource_cap_value:
            return 0.055
        return self.params.resource_score / self.params.resource_cap_value

    def density(self) -> float:
        """未知格的平均剩余价值密度（用于探索期望收益）。"""
        if not self.world or not self.obs:
            return self.params.resource_density_prior
        unknown = sum(1 for c in self.world.cells if c == 0)
        if unknown <= 0:
            return 0.0
        total_value = 200.0
        spent = (self.world.collected_value()
                 + self.world.taken_value
                 + sum(r.value for r in self.world.resources.values()))
        remaining = max(0.0, total_value - spent)
        # 对手也在采集：已探明区域越大，剩余资源越可能已被拿走。
        # 这里只做"上限"约束，避免最后几十个未知格把密度抬到离谱的水平。
        return min(remaining / unknown, 1.15)

    # ------------------------------------------------------------------
    # 行动
    # ------------------------------------------------------------------
    def move_to(self, cell: Cell, avoid_threat: bool = False) -> Optional[Tuple[str, Optional[str]]]:
        """走向目标格。第一步不可走时，退化为"更近的合法邻格"，避免撞车/撞墙空转。"""
        if not self.obs or not self.field:
            return None
        if cell == self.obs.me.pos:
            return None
        d = self.field.first_step(self.obs.me.pos, cell)
        if d is not None and self._step_ok(d, avoid_threat):
            return ("move", d)

        # 规划第一步不可走：先避开"上 Tick 撞掉的方向"，再退化为选最近的合法邻格
        for ignore_avoid in (False, True):
            best_dir = None
            best_d = INF
            for name, (vx, vy) in DIR_VEC.items():
                if not self._step_ok(name, avoid_threat, ignore_avoid=ignore_avoid):
                    continue
                nxt = (self.obs.me.pos[0] + vx, self.obs.me.pos[1] + vy)
                dd = self.field.d(nxt)
                if dd < best_d:
                    best_d = dd
                    best_dir = name
            if best_dir is not None and best_d < INF * 0.5:
                return ("move", best_dir)
        return None

    def _track_death(self, obs: Observation) -> None:
        """阵亡点记 25 Tick：复活后不再一头撞回同一个火力口。"""
        if obs.me.alive:
            self.death_cells = {c: t for c, t in self.death_cells.items() if t > obs.tick}
            return
        self.death_cells[obs.me.pos] = obs.tick + self.params.death_block_ticks

    def _track_move_failure(self, obs: Observation) -> None:
        """上一 Tick 想走却没走动 → 接下来 3 Tick 不再往那个方向顶。

        典型场景：两台车同时想进同一格，双方都失败；只要一方换向就能解开。
        """
        la = obs.me.last_action
        if not isinstance(la, dict) or la.get("action") != "move":
            return
        if la.get("moved"):
            self.avoid_dir = None
            self.avoid_until = -1
            return
        d = self.last_sent_dir
        if d in DIRS:
            self.avoid_dir = d
            self.avoid_until = obs.tick + 3

    def _step_ok(self, direction: str, avoid_threat: bool = False,
                 ignore_avoid: bool = False) -> bool:
        if not self.obs or not self.field:
            return False
        vx, vy = DIR_VEC[direction]
        cell = (self.obs.me.pos[0] + vx, self.obs.me.pos[1] + vy)
        if not (0 <= cell[0] < self.obs.map_w and 0 <= cell[1] < self.obs.map_h):
            return False
        if (not ignore_avoid) and direction == self.avoid_dir and self.tick < self.avoid_until:
            return False
        if self.world.is_blocked(cell, self.tick):
            return False
        if self.field.d(cell) >= INF * 0.5:
            return False
        enemies = self.visible_enemies()
        if any(e.pos == cell for e in enemies):
            return False
        if avoid_threat and Strategy.in_enemy_line(cell, enemies):
            return False
        return True

    def _far_cell(self) -> Optional[Cell]:
        """抖动逃生用的"远点"：优先最远的前沿，其次对角。"""
        if not self.obs or not self.field:
            return None
        best = None
        best_d = -1.0
        if self.world is not None:
            for cell in self.world.frontier_cells(160):
                d = self.field.d(cell)
                if d >= INF * 0.5:
                    continue
                if d > best_d:
                    best_d = d
                    best = cell
        if best is not None:
            return best
        w, h = self.obs.map_w, self.obs.map_h
        for c in ((1, 1), (w - 2, 1), (1, h - 2), (w - 2, h - 2)):
            if c != self.obs.me.pos and self.field.d(c) < INF * 0.5:
                return c
        return None

    def set_strategy(self, name: str) -> bool:
        """运行中切换策略（规则要求：需裁判批准，且只在暂停期间修改配置文件）。

        只换策略对象，**不清空**地图/资源/敌车/事件记忆——这是"三种策略共享记忆"的关键。
        """
        if name == self.current_decision:
            return False
        self.current_decision = name
        self.params = Params().with_decision(name)
        self.strategy = build_strategy(name, self.params)
        self.strategy.reset()
        self.collector = Collector(self.params)
        known = len(self.world.resources) if self.world else 0
        self.log(f"[brain] 策略切换 → {name}（保留记忆：已知资源 {known} 处）")
        return True

    def step_toward(self, cell: Cell) -> Optional[str]:
        if not self.obs or not self.field:
            return None
        return self.field.first_step(self.obs.me.pos, cell)

    def explore_candidates(self, point_scale: float = 1.0, offset: float = 3.4) -> List[Tuple[Cell, float]]:
        """探索前沿候选及其效用（按"预期发现分值 / 步数"排序）。"""
        if not self.world or not self.field or not self.obs:
            return []
        unknown = sum(1 for c in self.world.cells if c == 0)
        if unknown <= 0:
            return []
        dens = self.density()
        ppv = self.point_per_value()
        out: List[Tuple[Cell, float]] = []
        for cell in self.world.frontier_cells(260):
            d = self.field.d(cell)
            if d >= INF * 0.5 or d < 0.5:
                continue
            gain = self.world.info_gain(cell, self.obs.vision)
            if gain <= 0:
                continue
            u = point_scale * ppv * dens * gain / (d + offset)
            out.append((cell, u))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    def best_explore(self, point_scale: float = 1.0, offset: float = 3.4) -> Optional[Cell]:
        cands = self.explore_candidates(point_scale, offset)
        return cands[0][0] if cands else None

    # ------------------------------------------------------------------
    # LLM
    # ------------------------------------------------------------------
    def _sync_llm(self, obs: Observation, now: float) -> None:
        if self.llm is None or not self.llm.enabled or self.llm.quota_exhausted:
            return
        text = (obs.event_text or "").strip()[:4000]
        digest = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest() if text else ""

        need_brief = not self._brief_sent
        if need_brief:
            self._brief_sent = True
            self._brief_attempts += 1
            self._last_llm_attempt = now
            self.llm.submit("brief", self._prompt(obs, text, brief=True), obs.tick, obs.session_id)
            return

        if text and digest != self._event_hash:
            self._event_hash = digest
            self._last_llm_attempt = now
            self.llm.submit("analysis", self._prompt(obs, text, brief=False), obs.tick, obs.session_id)
            return

        # 必须拿到本场至少一次成功调用，否则任务分 M=0：失败时持续重试
        if self._llm_ok == 0 and self._brief_attempts < 10 and now - self._last_llm_attempt > 4.0:
            self._brief_attempts += 1
            self._last_llm_attempt = now
            self.llm.submit("brief", self._prompt(obs, text, brief=True), obs.tick, obs.session_id)

    def _prompt(self, obs: Observation, event_text: str, brief: bool) -> str:
        stage_name = "第一阶段（独立采集，只允许 move/wait）" if self.stage == 1 else "第二阶段（资源对抗与任务）"
        head = (
            "你是 RMTC 机甲大师赛的战术解析器。只输出一个 JSON 对象，不要解释、不要代码块。\n"
            f"阶段：{stage_name}\n当前 Tick：{obs.tick}\n地图：{obs.map_w}x{obs.map_h}\n"
            f"本车位置：[{obs.me.pos[0]},{obs.me.pos[1]}]，HP：{obs.me.hp}\n"
            f"任务原文：{obs.mission_text or '无'}\n"
        )
        if brief:
            task = (
                "请给出本场战术倾向。若没有事件公告，events 输出空数组。\n"
            )
        else:
            task = (
                "请解析下面全部随机事件公告（可能含多条历史公告），只解析公告中确实出现的事件，不要编造；"
                "公告里的“N Tick 后”请换算成绝对 Tick。\n"
            )
        body = f"{head}\n【随机事件公告】\n{event_text or '（无）'}\n\n{task}"
        schema = (
            '输出格式：{"events":[{"type":"explosion|illusion|angel|unrivaled|world_split|pepper|purity|pan",'
            '"start_tick":整数,"end_tick":整数,"rect":[[x1,y1],[x2,y2]]或null,'
            '"points":[[x,y]]或null,"center":[x,y]或null,"note":"20字内"}],'
            '"focus":"resources|combat|mission|survive|balanced","aggression":0到1的小数,'
            '"tactic":"30字内"}'
        )
        return body + "\n" + schema

    def _ingest_llm(self) -> None:
        if self.llm is None:
            return
        for res in self.llm.drain():
            if not res.ok:
                continue
            if res.session and res.session != self.session:
                continue
            payload = _extract_json(res.content)
            if payload is None:
                continue
            added = self.events.ingest(payload, self.tick, self.obs.map_w, self.obs.map_h)
            self._llm_ok += 1
            focus = self.events.focus
            if added:
                self.log(f"[llm] tick={self.tick} 吸收 {added} 个事件 focus={focus} "
                         f"tactic={self.events.tactic[:30]}")
            else:
                self.log(f"[llm] tick={self.tick} 简报完成 focus={focus}")

    def status(self) -> str:
        if not self.obs:
            return "no-obs"
        o = self.obs
        llm = self.llm.stats() if self.llm else {}
        return (
            f"t={self.tick} stage={self.stage} pos={o.me.pos} hp={o.me.hp} "
            f"R={o.resource_value:.0f} D={o.effective_damage:.0f} "
            f"m={len(o.completed_ids)} inv={o.score.get('invalid_actions', 0)} "
            f"llm={llm.get('success', 0)}/{llm.get('attempts', 0)}"
            f" cap{llm.get('budget', 0)} ev={self.events.summary(self.tick)}"
        )


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠出第一个完整 JSON 对象（允许 ```json 包裹）。"""
    if not isinstance(text, str) or not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(text[start:end + 1])
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None
