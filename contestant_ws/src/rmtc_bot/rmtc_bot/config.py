"""集中管理可调参数与决策档位。

规则 §2.3 要求：现场只能通过 decision_config.json 切换"已登记的决策类型"，
不能改代码、Prompt 或算法参数。因此这里把三类可登记档位固化在枚举里，
其余战术参数一律当作封存版本的一部分，不在现场修改。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from typing import Dict

# 赛前登记、现场允许切换的决策类型（枚举必须与提交材料一致）
DECISION_TYPES = ("greedy", "aggressive", "conservative", "balanced")
DEFAULT_DECISION = "balanced"


@dataclass
class Params:
    # ---------------- 通用 ----------------
    vision_fallback: int = 4
    # 阶段时长（规则 §3 固定值，仅用于时间预算，不用于读取外部信息）
    stage1_ticks: int = 100
    stage2_ticks: int = 500

    # ---------------- 第一阶段：在线定向采集 ----------------
    # 资源效用 = 价值 * s1_value_scale / (距离 + s1_dist_offset)
    s1_value_scale: float = 1.0
    s1_dist_offset: float = 0.2
    # 折返代价系数：k 越小越愿意绕路捡资源（1.0≈只算单程，2.0≈算往返）
    s1_detour_k: float = 0.32
    # 探索收益的确定性折扣（探索是概率收益，资源是确定收益）
    s1_explore_risk: float = 0.85
    # 探索效用 = 期望发现价值 / (距离 + s1_explore_offset)
    s1_explore_scale: float = 2.30
    s1_explore_offset: float = 3.4
    # 已锁定目标需要被超越多少倍才切换（抑制抖动）
    s1_switch_margin: float = 1.28
    # 目标锁定时长（Tick）
    s1_lock_ticks: int = 4
    # 未知格承载资源的先验密度（16 个 / 400 格 ≈ 0.04）
    resource_density_prior: float = 0.040
    # 资源平均价值（12×10 + 4×20 = 200 / 16 = 12.5）
    resource_value_mean: float = 12.5

    # ---------------- 第二阶段：任务/资源/对抗 ----------------
    resource_score: float = 30.0          # 资源满分
    resource_cap_value: float = 120.0     # 资源满分门槛
    combat_score: float = 15.0            # 对抗满分
    combat_cap_damage: float = 500.0      # 对抗满分门槛
    mission_score: float = 5.0            # 单个任务分值

    # 交战门槛
    engage_hp: float = 38.0               # 低于该 HP 不再主动追击（可反杀时例外）
    hunt_start_tick: int = 0              # 该 Tick 之前不主动追人（先抢资源）
    retreat_hp: int = 25                  # 低于该 HP 立即脱离
    attack_margin: int = -100            # 顺手攻击的血量差门槛；负值=不额外限制（仍受换血保护约束）
    trade_guard_hp: int = 28             # 低于该 HP 且无法斩杀时，拒绝 20 换 20
    defend_hp: int = 35                   # 低于该 HP 且与敌车对线时优先防御（把 20 减到 10）
    death_block_ticks: int = 25           # 阵亡点作为临时障碍的持续 Tick
    chase_max_dist: int = 9               # 主动追击的最大距离
    kill_secure_hp: int = 20              # 目标 HP ≤ 该值视为可斩杀

    # ---------------- 三种策略各自的阈值 ----------------
    greedy_defend_hp: int = 70            # 贪婪型：血 ≤ 该值且在敌方攻击线 → 防御
    greedy_avoid_hp: int = 75             # 贪婪型：血 ≤ 该值时绕开敌方攻击线
    greedy_heal_hp: int = 60              # 贪婪型：低于该 HP 才去天使点
    aggressive_retreat_hp: int = 40       # 侵略型：低于该 HP 脱离
    conservative_flee_radius: int = 5     # 保守型：敌车进入该半径就躲
    conservative_heal_hp: int = 85        # 保守型：低于该 HP 就去天使点
    conservative_alert_hp: int = 55       # 保守型：警戒半径内且低于该 HP 就撤

    # 事件与战术
    angel_hp_threshold: int = 70          # 低于该 HP 才去天使点
    fortress_max_dist: int = 12           # 堡垒最大争夺距离
    pepper_avoid: bool = True             # 默认绕开辣椒点

    # 探索（第二阶段资源搜刮）
    s2_explore_scale: float = 1.55
    s2_explore_offset: float = 3.6
    s2_resource_bias: float = 1.0
    # 第二阶段绕路系数（与第一阶段同源：越小越愿意为资源绕路）
    s2_detour_k: float = 0.45
    s2_high_k: float = 0.22          # 高价值资源更值得绕路
    s2_hunt_low_res: float = 60.0    # 资源低于该值时，追猎权重乘以下面的系数
    s2_hunt_penalty_low: float = 0.10
    s2_hunt_penalty_mid: float = 0.45
    s2_reach_delay: int = 120        # 前 N Tick 不优先做"到达坐标"任务
    s2_switch_margin: float = 1.30

    # ---------------- 上游模型请求参数 ----------------
    # 网关除 model 外全部原样转发；这些字段直接决定上游怎么算
    llm_model: str = "deepseek-flash"
    llm_disable_thinking: bool = True     # 顶层 thinking={"type":"disabled"}，实时决策不需要长思考
    llm_json_mode: bool = True            # response_format={"type":"json_object"}
    llm_max_tokens: int = 512
    llm_temperature: float = 0.1

    # LLM 影响上限（防止模型输出异常导致策略崩坏）
    llm_focus_gain: float = 0.22          # 单项效用最多 ±22%
    llm_aggression_gain: float = 0.12

    # ---------------- 规划 ----------------
    unknown_step_cost: float = 0.075       # 未知格略微加价，优先走已知通路
    enemy_cell_cost: float = 7.0           # 当前被敌车占据的格
    threat_cost_low_hp: float = 0.95
    threat_cost_high_hp: float = 0.22

    def to_dict(self) -> Dict[str, float]:
        return asdict(self)

    def with_decision(self, decision_type: str) -> "Params":
        """按现场登记的 decision_type 生成参数变体（只调整权重，不改代码路径）。"""
        p = Params(**self.to_dict())
        if decision_type in ("collect", "greedy"):
            # 贪婪型：资源优先，少绕路
            p.s2_resource_bias = 1.25
            p.s2_explore_scale = 1.65
        elif decision_type == "aggressive":
            # 侵略型：追得更远、更愿意接敌
            p.s2_resource_bias = 0.90
            p.chase_max_dist = 12
            p.engage_hp = 26.0
            p.s2_explore_scale = 1.30
        elif decision_type in ("defensive", "conservative"):
            # 保守型：警戒半径更大，更早去回血
            p.s2_resource_bias = 1.10
            p.conservative_flee_radius = 6
            p.angel_hp_threshold = 85
        return p
