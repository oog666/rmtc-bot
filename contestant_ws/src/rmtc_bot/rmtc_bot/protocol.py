"""RMTC 通信协议的纯 Python 实现：感知解析 + 动作编码。

本模块不依赖 ROS，便于在离线仿真器中复用同一套解析逻辑，保证"仿真里怎么跑，
上场就怎么跑"。设计原则：
1. 任何字段缺失、类型错误都只让本条感知作废，绝不让进程崩溃；
2. 只读取协议公开字段，不解析随机事件文本（事件理解必须交给 LLM）；
3. 视野实体的 9x9 相对位置字符串按"固定世界轴"解析，不做朝向旋转。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

Cell = Tuple[int, int]

# 方向向量：坐标原点在左上角，x 向右、y 向下
DIR_VEC: Dict[str, Cell] = {
    "up": (0, -1),
    "down": (0, 1),
    "left": (-1, 0),
    "right": (1, 0),
}
DIRS: Tuple[str, ...] = ("up", "down", "left", "right")
OPPOSITE = {"up": "down", "down": "up", "left": "right", "right": "left"}


def _int(v: Any) -> Optional[int]:
    """严格取整数：bool 是 int 的子类，这里必须排除。"""
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    return None


def _float(v: Any) -> Optional[float]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    return None


def _bool(v: Any, default: bool = False) -> bool:
    return v if isinstance(v, bool) else default


def parse_relative_map(text: str) -> Optional[Cell]:
    """解析 9x9 相对位置图，返回 (dx, dy)。

    协议：9 行 × 9 字符，'1' 固定在第 5 行第 5 列；'2' 为目标；
    同一格时中心保留 '1'，整图无 '2'，表示相对位移 (0,0)。
    """
    if not isinstance(text, str) or not text:
        return None
    # 容错：极少数实现会把 "\\n" 当成字面两字符传进来
    if "\n" not in text and "\\n" in text:
        text = text.replace("\\n", "\n")
    rows = text.split("\n")
    if len(rows) != 9 or any(len(r) != 9 for r in rows):
        return None
    for row_idx, row in enumerate(rows):
        col = row.find("2")
        if col >= 0:
            return (col - 4, row_idx - 4)
    return (0, 0)


@dataclass
class Entity:
    kind: str                       # robot / resource_normal / resource_high / obstacle
    eid: str
    pos: Cell
    team: str = ""
    hp: Optional[int] = None
    defending: bool = False
    protected: bool = False
    value: int = 0


@dataclass
class SelfState:
    team: str = ""
    robot_id: str = ""
    pos: Cell = (0, 0)
    hp: int = 100
    alive: bool = True
    defending: bool = False
    protected: bool = False
    respawn_in: int = 0
    position_reason: str = ""
    last_action: Optional[dict] = None


@dataclass
class Observation:
    tick: int
    session_id: str
    map_w: int
    map_h: int
    vision: int
    me: SelfState
    entities: List[Entity] = field(default_factory=list)
    score: Dict[str, Any] = field(default_factory=dict)
    mission_text: str = ""
    missions: List[dict] = field(default_factory=list)
    event_text: str = ""

    @property
    def resource_value(self) -> float:
        return float(self.score.get("resource_value") or 0.0)

    @property
    def effective_damage(self) -> float:
        return float(self.score.get("effective_damage") or 0.0)

    @property
    def completed_ids(self) -> List[str]:
        ids = self.score.get("mission_completed_ids")
        return list(ids) if isinstance(ids, list) else []


def parse_perception(raw: str) -> Optional[Observation]:
    """解析一条 /team_X/perception 的 data。失败返回 None，调用方直接忽略该帧。"""
    if not isinstance(raw, str) or len(raw) < 8 or len(raw) > 262144:
        return None
    try:
        obj = json.loads(raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(obj, dict):
        return None

    tick = _int(obj.get("tick"))
    session_id = obj.get("session_id")
    if tick is None or not isinstance(session_id, str) or not session_id:
        return None

    mp = obj.get("map")
    if not isinstance(mp, dict):
        return None
    w, h = _int(mp.get("width")), _int(mp.get("height"))
    if w is None or h is None or not (1 <= w <= 128 and 1 <= h <= 128):
        return None

    self_obj = obj.get("self")
    if not isinstance(self_obj, dict):
        return None
    pos = self_obj.get("position")
    if not (isinstance(pos, list) and len(pos) == 2):
        return None
    px, py = _int(pos[0]), _int(pos[1])
    if px is None or py is None or not (0 <= px < w and 0 <= py < h):
        return None

    vision = _int(obj.get("vision_radius"))
    if vision is None or not (0 <= vision <= 4):
        vision = 4

    me = SelfState(
        team=str(self_obj.get("team") or ""),
        robot_id=str(self_obj.get("robot_id") or ""),
        pos=(px, py),
        hp=_int(self_obj.get("hp")) if _int(self_obj.get("hp")) is not None else 100,
        alive=_bool(self_obj.get("alive"), True),
        defending=_bool(self_obj.get("defending"), False),
        protected=_bool(self_obj.get("protected"), False),
        respawn_in=_int(self_obj.get("respawn_in")) or 0,
        position_reason=str(self_obj.get("position_reason") or ""),
        last_action=self_obj.get("last_action") if isinstance(self_obj.get("last_action"), dict) else None,
    )

    entities: List[Entity] = []
    raw_entities = obj.get("visible_entities")
    if isinstance(raw_entities, list):
        for item in raw_entities[:256]:
            if not isinstance(item, dict):
                continue
            kind = item.get("type")
            if not isinstance(kind, str):
                continue
            offset = parse_relative_map(item.get("position"))
            if offset is None:
                continue
            ex, ey = px + offset[0], py + offset[1]
            if not (0 <= ex < w and 0 <= ey < h):
                continue
            if kind == "robot":
                entities.append(Entity(
                    kind="robot",
                    eid=str(item.get("id") or item.get("robot_id") or ""),
                    pos=(ex, ey),
                    team=str(item.get("team") or ""),
                    hp=_int(item.get("hp")) if _int(item.get("hp")) is not None else 100,
                    defending=_bool(item.get("defending"), False),
                    protected=_bool(item.get("protected"), False),
                ))
            elif kind in ("resource_normal", "resource_high"):
                value = _int(item.get("value"))
                if value is None:
                    value = 20 if kind == "resource_high" else 10
                entities.append(Entity(
                    kind=kind,
                    eid=str(item.get("id") or f"{ex},{ey}"),
                    pos=(ex, ey),
                    value=value,
                ))
            elif kind == "obstacle":
                entities.append(Entity(kind="obstacle", eid="", pos=(ex, ey)))

    missions = [m for m in obj.get("missions", []) if isinstance(m, dict)] \
        if isinstance(obj.get("missions"), list) else []

    return Observation(
        tick=tick,
        session_id=session_id,
        map_w=w,
        map_h=h,
        vision=vision,
        me=me,
        entities=entities,
        score=obj.get("score") if isinstance(obj.get("score"), dict) else {},
        mission_text=str(obj.get("mission_text") or ""),
        missions=missions,
        event_text=str(obj.get("event_text") or ""),
    )


def encode_action(tick: int, session_id: str, kind: str, direction: Optional[str] = None) -> str:
    """编码动作。字段必须小写，move/attack 必须带 direction。"""
    payload: Dict[str, Any] = {"tick": int(tick), "session_id": session_id, "action": kind}
    if kind in ("move", "attack"):
        payload["direction"] = direction
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
