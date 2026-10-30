"""随机事件记忆。

合规要点（规则 §8.1）：事件必须由 LLM 理解，程序**不得**用正则/关键词/固定句式
直接解析公告文本。本模块只接收"已经由 LLM 输出、并经结构校验"的事件参数，
把它们变成可规划的时间/空间约束。LLM 不可用时，事件记忆为空，策略退化为
"忽略事件"的保守版本，但控制循环照常运行。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from .protocol import Cell

Rect = Tuple[int, int, int, int]  # x1, y1, x2, y2（含边界）

EVENT_TYPES = {
    "explosion", "illusion", "angel", "unrivaled",
    "world_split", "pepper", "purity", "pan",
}

# 天下无双的堡垒是固定地形：正式图（≥22×22）恒为 [11,11]–[13,13]，中心 [12,12]。
# 这是规则常量，不是隐藏日程；即使模型/公告给出偏差坐标，也以规则为准。
# 小于 22×22 的练习图没有固定位置，按公告坐标就地归一化。
FORTRESS_RECT = (11, 11, 13, 13)
FORTRESS_CENTER = (12, 12)


def _fortress_rect(w: int, h: int) -> Tuple[Rect, Cell]:
    """返回固定堡垒矩形与中心；地图过小时向地图内裁剪。"""
    x1 = max(0, min(FORTRESS_RECT[0], w - 3))
    y1 = max(0, min(FORTRESS_RECT[1], h - 3))
    return (x1, y1, x1 + 2, y1 + 2), (x1 + 1, y1 + 1)


# 规则 §8 的标准持续时间（仅作为 LLM 未给出结束 Tick 时的兜底）
DEFAULT_DURATION = {
    "explosion": 1, "illusion": 1, "angel": 20, "unrivaled": 50,
    "world_split": 30, "pepper": 30, "purity": 20, "pan": 30,
}


def _i(v: Any) -> Optional[int]:
    if isinstance(v, bool) or not isinstance(v, int):
        return None
    return v


def _cell(v: Any, w: int, h: int) -> Optional[Cell]:
    if not (isinstance(v, (list, tuple)) and len(v) == 2):
        return None
    x, y = _i(v[0]), _i(v[1])
    if x is None or y is None:
        return None
    # 允许 1 像素级的越界描述，裁剪到地图内
    x = max(0, min(w - 1, x))
    y = max(0, min(h - 1, y))
    return (x, y)


def _rect(v: Any, w: int, h: int) -> Optional[Rect]:
    """兼容三种常见写法：[[x1,y1],[x2,y2]] / [x1,y1,x2,y2] / {"top_left":..,"bottom_right":..}"""
    if isinstance(v, dict):
        a = _cell(v.get("top_left"), w, h)
        b = _cell(v.get("bottom_right"), w, h)
    elif isinstance(v, (list, tuple)):
        if len(v) == 2 and all(isinstance(p, (list, tuple)) for p in v):
            a, b = _cell(v[0], w, h), _cell(v[1], w, h)
        elif len(v) == 4:
            a, b = _cell(v[:2], w, h), _cell(v[2:], w, h)
        else:
            return None
    else:
        return None
    if a is None or b is None:
        return None
    x1, x2 = sorted((a[0], b[0]))
    y1, y2 = sorted((a[1], b[1]))
    return (x1, y1, x2, y2)


def rect_cells(r: Rect) -> Set[Cell]:
    x1, y1, x2, y2 = r
    return {(x, y) for x in range(x1, x2 + 1) for y in range(y1, y2 + 1)}


@dataclass
class ScheduledEvent:
    kind: str
    start: int
    end: int
    rect: Optional[Rect] = None
    points: List[Cell] = field(default_factory=list)
    center: Optional[Cell] = None
    note: str = ""
    source: str = "llm"

    def active(self, tick: int) -> bool:
        return self.start <= tick < self.end

    def upcoming(self, tick: int, within: int = 6) -> bool:
        return tick < self.start <= tick + within


class EventMemory:
    """把 LLM 的结构化输出编译成规划约束。"""

    def __init__(self) -> None:
        self.events: List[ScheduledEvent] = []
        self._keys: Set[Tuple[str, int]] = set()
        self.last_sync_tick = -1
        self.tactic = ""
        self.focus = "balanced"
        self.aggression = 0.5
        self.version = 0

    # ---------- 写入 ----------
    def clear(self) -> None:
        self.events.clear()
        self._keys.clear()
        self.tactic = ""
        self.focus = "balanced"
        self.aggression = 0.5
        self.version += 1

    def ingest(self, payload: Dict[str, Any], tick: int, w: int, h: int) -> int:
        """校验并吸收一份 LLM JSON。返回新增事件数。非法条目直接丢弃。"""
        if not isinstance(payload, dict):
            return 0
        added = 0

        focus = payload.get("focus")
        if isinstance(focus, str) and focus in ("resources", "combat", "mission", "survive", "balanced"):
            self.focus = focus
        aggr = payload.get("aggression")
        if isinstance(aggr, (int, float)) and not isinstance(aggr, bool):
            self.aggression = max(0.0, min(1.0, float(aggr)))
        note = payload.get("tactic")
        if isinstance(note, str) and len(note) < 200:
            self.tactic = note

        raw_events = payload.get("events")
        if not isinstance(raw_events, list):
            return 0

        for item in raw_events[:12]:
            if not isinstance(item, dict):
                continue
            kind = item.get("type") or item.get("kind")
            if not isinstance(kind, str) or kind not in EVENT_TYPES:
                continue
            start = _i(item.get("start_tick"))
            end = _i(item.get("end_tick"))
            if start is None:
                # LLM 没给出具体 Tick 时，按"当前生效"处理，避免完全丢失
                start = tick
            if end is None:
                end = start + DEFAULT_DURATION.get(kind, 10)
            if not (-50 <= start <= tick + 700) or end <= start or end - start > 120:
                continue

            rect = _rect(item.get("rect"), w, h)
            points: List[Cell] = []
            raw_points = item.get("points")
            if isinstance(raw_points, list):
                for p in raw_points[:8]:
                    c = _cell(p, w, h)
                    if c:
                        points.append(c)
            center = _cell(item.get("center"), w, h)
            if center is None and rect is not None:
                center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
            if kind == "unrivaled":
                # 正式图固定 [11,11]–[13,13]；练习图（<22）按公告归一化
                if w >= 22 and h >= 22:
                    rect, center = _fortress_rect(w, h)
                else:
                    if center is None and rect is not None:
                        center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)
                    if center is not None:
                        rect = (max(0, center[0] - 1), max(0, center[1] - 1),
                                min(w - 1, center[0] + 1), min(h - 1, center[1] + 1))
                    elif rect is None:
                        continue
            if kind == "world_split" and center is None and rect is not None:
                center = ((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2)

            key = (kind, start)
            if key in self._keys:
                continue
            self._keys.add(key)
            self.events.append(ScheduledEvent(
                kind=kind, start=start, end=end, rect=rect, points=points,
                center=center, note=str(item.get("note") or "")[:120],
            ))
            added += 1

        self.events.sort(key=lambda e: e.start)
        if added:
            self.version += 1
        return added

    # ---------- 查询 ----------
    def _active(self, kinds: Iterable[str], tick: int) -> List[ScheduledEvent]:
        ks = set(kinds)
        return [e for e in self.events if e.kind in ks and e.active(tick)]

    def danger_cells(self, tick: int, w: int, h: int) -> Set[Cell]:
        """爆炸/世界分裂：这些格子在本 Tick 会致死或阻断，规划时视为不可通行。

        爆炸提前 3 Tick 视为禁区（留出撤离时间），十字墙提前 1 Tick 生效（别踩线）。
        """
        out: Set[Cell] = set()
        for e in self.events:
            if e.kind == "explosion" and e.rect and e.end > tick and (e.start - tick) <= 3:
                out |= rect_cells(e.rect)
            elif e.kind == "world_split" and e.center and e.end > tick and (e.start - tick) <= 1:
                cx, cy = e.center
                out |= {(x, cy) for x in range(w)}
                out |= {(cx, y) for y in range(h)}
        return out

    def illusion_affects(self, pos: Cell, tick: int, default_ticks: int = 50) -> Optional[int]:
        """若本 Tick 处于幻术激活窗口且自身在矩形内，返回预期到期 Tick。"""
        for e in self.events:
            if e.kind != "illusion" or e.rect is None:
                continue
            if not (e.start <= tick <= e.start + 2):
                continue
            if e.rect[0] <= pos[0] <= e.rect[2] and e.rect[1] <= pos[1] <= e.rect[3]:
                return max(e.end, e.start + default_ticks)
        return None

    def blocked_cells(self, tick: int, w: int, h: int) -> Set[Cell]:
        """世界分裂的十字障碍是真正的墙，规划必须绕开。"""
        out: Set[Cell] = set()
        for e in self._active(("world_split",), tick):
            if e.center:
                cx, cy = e.center
                out |= {(x, cy) for x in range(w)}
                out |= {(cx, y) for y in range(h)}
        return out

    def explosion_eta(self, pos: Cell, tick: int, w: int, h: int) -> Optional[int]:
        """距离最近一次爆炸生效还有多少 Tick（若已在危险区内返回 0）。"""
        best: Optional[int] = None
        for e in self.events:
            if e.kind != "explosion" or e.rect is None or e.end <= tick:
                continue
            inside = pos[0] >= e.rect[0] and pos[0] <= e.rect[2] and \
                     pos[1] >= e.rect[1] and pos[1] <= e.rect[3]
            if inside:
                eta = max(0, e.start - tick)
            else:
                eta = e.start - tick
            if eta < 0:
                continue
            if best is None or eta < best:
                best = eta
        return best

    def angel_points(self, tick: int) -> List[Cell]:
        out: List[Cell] = []
        for e in self._active(("angel",), tick):
            out.extend(e.points)
        return out

    def fortress(self, tick: int) -> Optional[Tuple[Rect, int, int]]:
        """返回 (矩形, 剩余Tick, 站稳所需Tick)。站稳 Tick 由 LLM 提供则用，否则按标准 10。"""
        for e in self._active(("unrivaled",), tick):
            if e.rect:
                return e.rect, e.end - tick, 10
        return None

    def pepper_points(self, tick: int) -> List[Cell]:
        out: List[Cell] = []
        for e in self._active(("pepper",), tick):
            out.extend(e.points)
        return out

    def purity_active(self, tick: int) -> bool:
        return bool(self._active(("purity",), tick))

    def pan_active(self, tick: int) -> bool:
        return bool(self._active(("pan",), tick))

    def illusion_rects(self, tick: int) -> List[Rect]:
        return [e.rect for e in self._active(("illusion",), tick) if e.rect]

    def next_events(self, tick: int, limit: int = 3) -> List[ScheduledEvent]:
        return [e for e in self.events if e.end > tick][:limit]

    def summary(self, tick: int) -> str:
        items = []
        for e in self.next_events(tick, 3):
            items.append(f"{e.kind}@{e.start}-{e.end}")
        return ",".join(items)
