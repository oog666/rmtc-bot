"""第二阶段：任务 + 资源 + 对抗的统一决策。

计分结构（规则 §10.2）：
    资源分 = 30 × min(R/120, 1)   → 1 点资源价值 ≈ 0.25 分
    对抗分 = 15 × min(D/500, 1)   → 1 HP 有效伤害 ≈ 0.03 分，一次击杀奖励 60 ≈ 1.8 分
    任务分 = 5 × M                → 单个任务 5 分，性价比最高

因此整场策略是"先抢任务、再囤资源、最后刷伤害"：
* 任务只要求 2 普通 + 1 高价 + 1 个坐标，早期顺路做完，拿满 15 分；
* 资源 120 价值封顶，拿到后资源权重自动衰减（保留少量权重用于同分排名）；
* 战斗随时"顺手打"：只要在攻击线上且不亏，就先打一下再赶路；
* 血量低于阈值时转为保命/找天使点，避免死亡扣 60 有效伤害。
"""

from __future__ import annotations

import math
from collections import deque
from typing import Any, List, Optional, Tuple

from .combat import attack_cells, attack_options, choose_attack, aligned_threats
from .config import Params
from .world import INF, chebyshev


class Battle:
    """综合型（balanced）：资源/任务/对抗统一打分。

    保持为一个独立的"策略实现"，供 strategies.build_strategy 注册；
    与三种人格化策略共享同一套记忆和规划器。
    """

    name = "balanced"
    threat_low = 0.95
    threat_high = 0.22

    def __init__(self, params: Params) -> None:
        self.p = params
        self.commit_cell: Optional[Tuple[int, int]] = None
        self._last_defend_tick = -99
        self._disengage_until = -1
        self._disengage_cell: Optional[Tuple[int, int]] = None
        self.commit_until = -1
        self._last_pos: Optional[Tuple[int, int]] = None
        self._same_pos_ticks = 0
        self._history: deque = deque(maxlen=9)
        self.wander_cell: Optional[Tuple[int, int]] = None
        self.wander_until = -1

    # ------------------------------------------------------------------
    def reset(self) -> None:
        """换场次时清空跨场状态（规则要求：新 session 不得沿用旧规划）。"""
        self.commit_cell = None
        self.commit_until = -1
        self._last_pos = None
        self._same_pos_ticks = 0
        self._history.clear()
        self.wander_cell = None
        self.wander_until = -1

    def decide(self, b) -> Tuple[str, Optional[str]]:
        obs = b.obs
        me = obs.me
        enemies = b.visible_enemies()
        purity = b.events.purity_active(b.tick)
        pan = b.events.pan_active(b.tick)
        buff = b.buff_until > b.tick

        self._track_fortress(b)

        # 1) 能打就先打；但"换血必输"时不打——先防御把 20 降到 10，再找机会脱离。
        #    典型死法：双方都剩 20 HP，我一味对拼，对方先结算就白送一条命。
        opts = attack_options(me.pos, enemies, purity=purity, unrivaled=buff)
        opt = choose_attack(opts, my_hp=me.hp, protected=me.protected)
        if opt is not None:
            target = opt.target          # AttackOption.target 是实体本身，不是坐标
            can_kill = target is not None and opt.damage >= target.hp
            losing_trade = (me.hp <= self.p.trade_guard_hp and not can_kill
                            and aligned_threats(me.pos, enemies))
            # 顺手攻击也要挑：均势对拼只会把时间和血量换成 0 收益。
            # 值得打 = 能斩杀 / 我明显占优 / 残血自保 / 有 AoE / 资源已封顶转刷伤害。
            resource_done = obs.resource_value >= self.p.resource_cap_value * 0.85
            worth = (can_kill or buff or purity or b.took_damage
                     or me.hp <= self.p.defend_hp + 5
                     or (target is not None and me.hp - target.hp >= self.p.attack_margin)
                     or (resource_done and obs.effective_damage < self.p.combat_cap_damage))
            if not losing_trade and worth:
                return ("attack", opt.direction)

        # 2) 残血且被贴脸：先防一 Tick 把伤害减半，紧接着必须脱离（不能站桩到死）
        if self._should_defend(b, enemies, purity, pan):
            if b.tick - self._last_defend_tick > 1:
                self._last_defend_tick = b.tick
                return ("defend", None)
            cell = self._escape_cell(b, enemies)
            if cell is not None:
                mv = b.move_to(cell)
                if mv is not None:
                    return mv
            return ("defend", None)

        # 3) 堡垒驻留中：原地不动把进度攒满（攻击已在上一步处理）
        fort = b.events.fortress(b.tick)
        if fort is not None:
            rect, left, need = fort
            inside = _in_rect(me.pos, rect)
            if inside and b.fortress_stay < need and left > (need - b.fortress_stay):
                # 规则：必须"移动后仍在堡垒内"才累计，所以要在 3×3 里换格，不能站着等
                step = self._fortress_step(b, rect)
                if step is not None:
                    mv = b.move_to(step)
                    if mv is not None:
                        return mv
                b.intentional_wait = True      # 无合法格可走时才驻留（此时不累计）
                return ("wait", None)

        # 4) 低血量脱离
        if me.hp <= self.p.retreat_hp and enemies:
            cell = self._escape_cell(b, enemies)
            if cell is not None:
                mv = b.move_to(cell)
                if mv is not None:
                    return mv

        # 5) 常规目标
        return self._move(b, enemies, buff)

    # ------------------------------------------------------------------
    # 堡垒驻留记账
    # ------------------------------------------------------------------
    def _track_fortress(self, b) -> None:
        fort = b.events.fortress(b.tick)
        if fort is None:
            b.fortress_stay = 0
            return
        rect, left, need = fort
        if _in_rect(b.obs.me.pos, rect):
            la = b.obs.me.last_action
            moved = isinstance(la, dict) and la.get("action") == "move" and la.get("moved")
            if moved and b._last_fortress_tick != b.tick:
                b.fortress_stay += 1
                b._last_fortress_tick = b.tick
            if b.fortress_stay >= need and b.buff_until < b.tick:
                b.buff_until = b.tick + left + 1
                b.log(f"[battle] 获得天下无双，至 Tick {b.buff_until}")
        else:
            b.fortress_stay = 0

    def _fortress_step(self, b, rect) -> Optional[Tuple[int, int]]:
        """在堡垒 3×3 内找一个可走的相邻格（用于累计驻留，而不是原地等待）。"""
        me = b.obs.me.pos
        cx = (rect[0] + rect[2]) // 2
        cy = (rect[1] + rect[3]) // 2
        cands = [(me[0] + 1, me[1]), (me[0] - 1, me[1]), (me[0], me[1] + 1), (me[0], me[1] - 1),
                 (cx, cy)]
        for c in cands:
            if c == me or not _in_rect(c, rect):
                continue
            if b.world.is_blocked(c, b.tick):
                continue
            if any(e.pos == c for e in b.visible_enemies()):
                continue
            if b.field.d(c) < INF * 0.5:
                return c
        return None

    # ------------------------------------------------------------------
    # 防御判断
    # ------------------------------------------------------------------
    def _should_defend(self, b, enemies, purity: bool, pan: bool) -> bool:
        me = b.obs.me
        if not aligned_threats(me.pos, enemies):
            return False
        if purity and me.hp < 75:
            return True          # 反伤环境：防御 = 让对手自伤 20
        if me.hp <= self.p.defend_hp:
            return True          # 残血先减伤，别再 20 换 20
        if pan and me.hp <= 55:
            return True          # 易伤 + 移动会加倍受伤，站住防御更划算
        return False

    # ------------------------------------------------------------------
    # 目标选择
    # ------------------------------------------------------------------
    def _objectives(self, b, enemies, buff: bool) -> List[Tuple[float, str, Any]]:
        obs = b.obs
        me = obs.me
        P = self.p
        out: List[Tuple[float, str, Any]] = []

        ppv = b.point_per_value()
        completed = set(obs.completed_ids)
        norm_have, high_have = b.world.collected_count()

        # 任务缺口 → 资源偏好
        norm_need = 0
        high_need = 0
        damage_mission = False
        for m in obs.missions:
            mid = m.get("id")
            if not isinstance(mid, str) or mid in completed:
                continue
            mtype = m.get("type")
            target = m.get("target") if isinstance(m.get("target"), int) else 1
            if mtype == "normal_resources":
                norm_need = max(norm_need, target - norm_have)
            elif mtype == "high_resources":
                high_need = max(high_need, target - high_have)
            elif mtype == "effective_damage":
                damage_mission = True
            elif mtype == "reach":
                pos = m.get("position")
                if isinstance(pos, list) and len(pos) == 2:
                    cell = (int(pos[0]), int(pos[1]))
                    d = b.field.d(cell)
                    if d < INF * 0.5:
                        u = P.mission_score / (P.s2_detour_k * d + 0.25)
                        if d <= 1:
                            u *= 1.8          # 一步到位，立刻拿 5 分
                        if b.tick < P.s2_reach_delay:
                            u *= 0.30         # 开局先把资源抢下来，任务放到中盘补
                        out.append((u, "reach", cell))

        # 资源
        cap_done = obs.resource_value >= P.resource_cap_value
        for rid, res in b.world.resources.items():
            d = b.field.d(res.pos)
            if d >= INF * 0.5:
                continue
            k = P.s2_high_k if res.value >= 20 else P.s2_detour_k
            u = ppv * res.value / (k * d + 0.3) * P.s2_resource_bias
            if not cap_done:
                if res.value < 20 and norm_need > 0:
                    u *= 1.40
                if res.value >= 20:
                    # 高价值资源 = 2 倍资源分 + 任务分（缺 1 个就是 5 分），必须优先
                    u *= 2.55 if high_need > 0 else 1.35
            if cap_done:
                u *= 0.35
            out.append((u, "resource", res.pos))

        # 天使恩泽（低血量时的续航）
        if me.hp < P.angel_hp_threshold:
            for p in b.events.angel_points(b.tick):
                d = b.field.d(p)
                if d < INF * 0.5:
                    heal = min(100 - me.hp, 100)
                    out.append((0.085 * heal / (d + 2.0), "angel", p))

        # 天下无双堡垒（AoE 是刷伤害最快的途径）
        fort = b.events.fortress(b.tick)
        if fort is not None and b.buff_until <= b.tick:
            rect, left, need = fort
            cx = (rect[0] + rect[2]) // 2
            cy = (rect[1] + rect[3]) // 2
            d = b.field.d((cx, cy))
            # 必须在堡垒结束前赶得到并站满：剩余时间 > 路程 + 驻留需求 + 余量
            if (not _in_rect(me.pos, rect)) and d < INF * 0.5 and d <= P.fortress_max_dist \
                    and left > need + d + 2:
                u = 10.0 / (d + 2.0)
                if obs.effective_damage < P.combat_cap_damage:
                    u *= 1.30        # 还缺对抗分时，AoE 增益更值钱
                out.append((u, "fortress", (cx, cy)))

        # 追击：只追视野内的敌人（追记忆位置极易被"幽灵目标"拖死）
        rng = 999 if buff else P.chase_max_dist
        capped = obs.effective_damage >= P.combat_cap_damage
        # 对抗分封顶后再追人是零收益，只会把时间换成死亡与丢资源
        for e in (() if capped else b.visible_enemies()):
            if chebyshev(e.pos, me.pos) > rng:
                continue
            d = b.field.d(e.pos)
            if d >= INF * 0.5:
                continue
            hp = max(1, min(100, e.hp))
            est = 0.03 * hp + 1.8
            ticks = math.ceil(hp / 20.0)
            u = est / (0.55 * d + ticks + 1.0)
            if d > 3 * chebyshev(e.pos, me.pos) + 6:
                u *= 0.15          # 绕路代价远高于直线：中间大概率有墙，先放弃
            if e.hp <= P.kill_secure_hp:
                u *= 1.35
            if buff:
                u *= 1.55
            if me.hp <= P.engage_hp:
                u *= 0.45
            if me.hp <= 60 and len(b.visible_enemies()) >= 2:
                continue          # 被两车以上盯上时不主动贴上去
            if obs.resource_value < P.s2_hunt_low_res:
                u *= P.s2_hunt_penalty_low     # 资源还没拿到一半：追人几乎无收益
            elif obs.resource_value < P.resource_cap_value:
                u *= P.s2_hunt_penalty_mid
            if e.protected:
                u *= 0.5
            if damage_mission:
                u *= 1.15
            if b.tick < P.hunt_start_tick:
                continue           # 开局资源窗口：不主动追人，只保留顺手攻击
            if b.remaining_ticks() < 180 and obs.effective_damage < P.combat_cap_damage:
                u *= 1.45          # 收官阶段：对抗分还差一截，主动找人打
            elif obs.resource_value < P.resource_cap_value:
                # 资源期：30 分资源 > 15 分对抗，且伤害可以后补、资源不会刷新。
                # 只保留"顺手打"（攻击分支），不主动追人。
                u *= 0.30
            if me.protected:
                u *= 0.25          # 出生保护期不能攻击，追人没有意义
            out.append((u, "hunt", e))

        # 探索（找不到目标时）
        explore_scale = P.s2_explore_scale
        if b.remaining_ticks() < 40:
            explore_scale *= 0.4
        for cell, u in b.explore_candidates(explore_scale, P.s2_explore_offset)[:10]:
            out.append((u, "explore", cell))

        # ---- LLM 战术倾向：有限幅度的加权，避免模型幻觉破坏策略 ----
        focus = b.events.focus
        g = P.llm_focus_gain
        aggr = b.events.aggression
        adjusted: List[Tuple[float, str, Any]] = []
        for u, kind, ref in out:
            if kind == "hunt":
                u *= 0.85 + 0.3 * aggr
            if focus == "resources" and kind in ("resource", "explore"):
                u *= 1.0 + g
            elif focus == "combat" and kind == "hunt":
                u *= 1.0 + g
            elif focus == "mission" and kind in ("reach", "resource"):
                u *= 1.0 + g
            elif focus == "survive":
                if kind in ("angel",):
                    u *= 1.0 + 1.5 * g
                if kind == "hunt":
                    u *= 1.0 - g
            adjusted.append((u, kind, ref))
        return adjusted

    # ------------------------------------------------------------------
    # 移动
    # ------------------------------------------------------------------
    def _move(self, b, enemies, buff: bool) -> Tuple[str, Optional[str]]:
        self._detect_stuck(b)
        if self._is_cycling(b) and b.tick >= self.wander_until:
            self.wander_cell = self._far_cell(b)
            self.wander_until = b.tick + 12
            if self.wander_cell is not None:
                b.log(f"[battle] 检测到振荡，改走远点 {self.wander_cell}")
        if b.tick < self.wander_until and self.wander_cell is not None:
            mv = b.move_to(self.wander_cell)
            if mv is not None:
                return mv
        objs = self._objectives(b, enemies, buff)
        if not objs:
            return self._fallback(b)
        objs.sort(key=lambda t: t[0], reverse=True)
        _, kind, ref = objs[0]

        cell: Optional[Tuple[int, int]] = None
        if kind == "hunt":
            visible = any(e.team == ref.team for e in b.visible_enemies())
            cell = self._attack_cell(b, ref.pos) if visible else None
            if cell is None:
                cell = self._approach_cell(b, ref.pos)
        elif kind == "stay":
            return ("wait", None)
        else:
            cell = ref  # type: ignore[assignment]

        if cell is None:
            return self._fallback(b)
        mv = b.move_to(cell)
        if mv is not None:
            return mv
        return self._fallback(b)

    def _is_cycling(self, b) -> bool:
        """最近 9 Tick 只在 ≤2 个格子之间来回 → 判定为振荡。"""
        pos = b.obs.me.pos
        self._history.append(pos)
        if len(self._history) < 9:
            return False
        return len(set(self._history)) <= 2

    def _far_cell(self, b) -> Optional[Tuple[int, int]]:
        me = b.obs.me.pos
        best, best_d = None, -1
        for cell in b.world.frontier_cells(120) or []:
            d = b.field.d(cell)
            if d >= INF * 0.5:
                continue
            if d > best_d:
                best, best_d = cell, d
        if best is not None:
            return best
        # 没有前沿就取对角
        cand = (b.obs.map_w - 2, b.obs.map_h - 2) if me[0] < b.obs.map_w // 2 else (1, 1)
        return cand if b.field.d(cand) < INF * 0.5 else None

    def _detect_stuck(self, b) -> None:
        """连续 4 Tick 没挪窝就换目标，避免在两个格子之间来回横跳。"""
        pos = b.obs.me.pos
        if pos == self._last_pos:
            self._same_pos_ticks += 1
        else:
            self._same_pos_ticks = 0
            self._last_pos = pos
        if self._same_pos_ticks >= 4:
            self.commit_cell = None
            self.commit_until = -1
            self._same_pos_ticks = 0

    def _fallback(self, b) -> Tuple[str, Optional[str]]:
        if self.commit_cell is not None and b.tick < self.commit_until:
            mv = b.move_to(self.commit_cell)
            if mv is not None:
                return mv
        cell = b.best_explore(self.p.s2_explore_scale, self.p.s2_explore_offset)
        if cell is not None:
            self.commit_cell = cell
            self.commit_until = b.tick + 10
            mv = b.move_to(cell)
            if mv is not None:
                return mv
        # 地图探明且没有目标：向地图中心靠拢，保持机动（便于抢刷新点/接敌）
        me = b.obs.me.pos
        cx, cy = b.obs.map_w // 2, b.obs.map_h // 2
        for target in ((cx, cy), (cx, me[1]), (me[0], cy)):
            if target != me:
                mv = b.move_to(target)
                if mv is not None:
                    return mv
        return ("wait", None)

    def _attack_cell(self, b, enemy_pos) -> Optional[Tuple[int, int]]:
        best = None
        best_d = INF
        for c in attack_cells(enemy_pos):
            if c == b.obs.me.pos:
                continue                      # 已站位时由攻击分支处理，避免原地空转
            d = b.field.d(c)
            if d >= INF * 0.5:
                continue
            if b.world.is_blocked(c, b.tick):
                continue
            if d < best_d:
                best_d = d
                best = c
        return best

    def _approach_cell(self, b, enemy_pos) -> Optional[Tuple[int, int]]:
        ex, ey = enemy_pos
        cands = [(ex + 1, ey), (ex - 1, ey), (ex, ey + 1), (ex, ey - 1)]
        best = None
        best_d = INF
        for c in cands:
            if c == b.obs.me.pos:
                continue
            d = b.field.d(c)
            if d >= INF * 0.5 or b.world.is_blocked(c, b.tick):
                continue
            if d < best_d:
                best_d = d
                best = c
        return best

    def _escape_cell(self, b, enemies) -> Optional[Tuple[int, int]]:
        """朝远离敌人的方向撤：在 5 步内挑"最近敌人距离最大、路又最短"的格。"""
        best = None
        best_score = -INF
        for y in range(b.obs.map_h):
            for x in range(b.obs.map_w):
                c = (x, y)
                d = b.field.d(c)
                if d >= INF * 0.5 or d > 5:
                    continue
                if b.world.is_blocked(c, b.tick):
                    continue
                danger = min((chebyshev(c, e.pos) for e in enemies), default=9)
                score = min(danger, 6) * 1.0 - 0.18 * d
                if score > best_score:
                    best_score = score
                    best = c
        return best


def _in_rect(cell, rect) -> bool:
    return rect[0] <= cell[0] <= rect[2] and rect[1] <= cell[1] <= rect[3]
