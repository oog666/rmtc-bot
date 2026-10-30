"""生成 10 张符合 RMTC 规则的 24×24 实验地图。

输出两种格式（对应裁判端地图编辑器的两个导出选项）：
  maps/NN-name.json            正式裁判地图（map + robots），用于「导入地图」
  maps/NN-name.practice.json   测试裁判完整配置（含事件/任务），用于「导入配置」

规则约束：
  * 24×24；A/B/C/D 四个出生点在四角；
  * 12 个普通资源（10）+ 4 个高价值资源（20），合计 200；
  * 出生点、资源、障碍不重叠；
  * (12,12) 必须可通行（到达任务）；
  * 堡垒 [11,11]–[13,13] 保持无障碍、无资源；
  * 所有出生点与资源从 A 出生点可达（四邻接 BFS）。

用法：
  python3 tools/make_maps.py
"""

from __future__ import annotations

import copy
import json
import os
from collections import deque
from typing import List, Tuple

Cell = Tuple[int, int]
W = H = 24
SPAWNS = [("A", (1, 1)), ("B", (22, 1)), ("C", (1, 22)), ("D", (22, 22))]
FORTRESS = {(x, y) for x in range(11, 14) for y in range(11, 14)}

MAPS: List[dict] = [
    {
        "name": "01-open-field",
        "title": "开阔平原",
        "desc": "障碍少、视野开阔，资源均匀铺开，考验基础采集与遭遇战。",
        "obstacles": [(5, 5), (6, 5), (17, 5), (18, 5), (5, 18), (6, 18), (17, 18), (18, 18),
                      (11, 7), (12, 7), (11, 16), (12, 16)],
        "high": [(6, 12), (17, 11), (12, 4), (11, 19)],
        "normal": [(3, 3), (20, 3), (3, 20), (20, 20), (8, 7), (15, 7), (8, 16), (15, 16),
                   (2, 12), (21, 12), (12, 9), (12, 14)],
    },
    {
        "name": "02-center-keep",
        "title": "中央堡垒",
        "desc": "中心 5×5 环墙留四个门，堡垒区被墙包围，抢中心要算好进出路线。",
        "obstacles": [(x, y) for x in range(10, 15) for y in range(10, 15)
                      if (x, y) in {(10, 10), (11, 10), (12, 10), (13, 10), (14, 10),
                                    (10, 11), (14, 11), (10, 12), (14, 12), (10, 13), (14, 13),
                                    (10, 14), (11, 14), (12, 14), (13, 14), (14, 14)}
                      and (x, y) not in {(12, 10), (12, 14), (10, 12), (14, 12)}]
                     + [(4, 4), (19, 4), (4, 19), (19, 19)],
        "high": [(12, 11), (11, 12), (5, 5), (18, 18)],
        "normal": [(3, 3), (20, 3), (3, 20), (20, 20), (9, 9), (15, 9), (9, 15), (15, 15),
                   (12, 3), (12, 20), (3, 12), (20, 12)],
    },
    {
        "name": "03-quadrants",
        "title": "四象限",
        "desc": "十字墙把地图切成四块，中心留通道；适合观察分路与转线。",
        "obstacles": [(11, y) for y in list(range(2, 10)) + list(range(14, 22))]
                     + [(y, 11) for y in list(range(2, 10)) + list(range(14, 22))],
        "high": [(6, 6), (17, 6), (6, 17), (17, 17)],
        "normal": [(3, 3), (20, 3), (3, 20), (20, 20), (9, 5), (14, 5), (9, 18), (14, 18),
                   (5, 12), (18, 12), (12, 5), (12, 18)],
    },
    {
        "name": "04-ring-corridor",
        "title": "环形走廊",
        "desc": "外环墙留四个门，内外两层资源；绕圈与切内线是两种打法。",
        "obstacles": [(x, y) for x in range(3, 21) for y in range(3, 21)
                      if (x in (3, 20) or y in (3, 20))
                      and (x, y) not in {(11, 3), (12, 3), (11, 20), (12, 20),
                                         (3, 11), (3, 12), (20, 11), (20, 12)}],
        "high": [(12, 6), (12, 17), (6, 12), (17, 12)],
        "normal": [(2, 2), (21, 2), (2, 21), (21, 21), (8, 8), (15, 8), (8, 15), (15, 15),
                   (6, 6), (17, 6), (6, 17), (17, 17)],
    },
    {
        "name": "05-twin-towers",
        "title": "双塔窄道",
        "desc": "左右两座长条塔夹出中央走廊与两侧边道，走廊是必经的冲突点。",
        "obstacles": [(x, y) for x in range(6, 10) for y in range(4, 20)]
                     + [(x, y) for x in range(14, 18) for y in range(4, 20)],
        "high": [(11, 6), (12, 17), (4, 12), (19, 12)],
        "normal": [(11, 3), (12, 3), (11, 20), (12, 20), (2, 3), (21, 3), (2, 20), (21, 20),
                   (4, 6), (19, 6), (4, 17), (19, 17)],
    },
    {
        "name": "06-zigzag",
        "title": "锯齿墙",
        "desc": "交错短墙形成蛇形通道，逼迫绕行，考验路径规划与耐心。",
        "obstacles": [(4, y) for y in range(4, 14)] + [(8, y) for y in range(10, 20)]
                     + [(12, y) for y in range(4, 10)] + [(16, y) for y in range(14, 20)]
                     + [(20, y) for y in range(4, 10)],
        "high": [(6, 7), (18, 7), (6, 17), (18, 17)],
        "normal": [(3, 3), (20, 3), (3, 20), (20, 20), (11, 5), (10, 5), (11, 18), (12, 18),
                   (6, 12), (17, 12), (10, 12), (2, 22)],
    },
    {
        "name": "07-rich-center",
        "title": "资源富集中心",
        "desc": "四个角楼占位，资源向中心聚集，开局就会爆发争夺。",
        "obstacles": [(x, y) for x in range(3, 6) for y in range(3, 6)]
                     + [(x, y) for x in range(18, 21) for y in range(3, 6)]
                     + [(x, y) for x in range(3, 6) for y in range(18, 21)]
                     + [(x, y) for x in range(18, 21) for y in range(18, 21)],
        "high": [(9, 9), (14, 9), (9, 14), (14, 14)],
        "normal": [(11, 8), (12, 8), (13, 8), (8, 11), (8, 12), (8, 13),
                   (15, 11), (15, 12), (15, 13), (11, 15), (12, 15), (13, 15)],
    },
    {
        "name": "08-edge-resources",
        "title": "边缘资源",
        "desc": "资源沿边分布，中心是带门的环形墙；贪边还是控中需要取舍。",
        "obstacles": [(x, y) for x in range(9, 16) for y in range(9, 16)
                      if (x in (9, 15) or y in (9, 15))
                      and (x, y) not in {(12, 9), (12, 15), (9, 12), (15, 12)}],
        "high": [(4, 12), (19, 12), (12, 2), (12, 21)],
        "normal": [(2, 4), (2, 8), (2, 16), (2, 20), (21, 4), (21, 8), (21, 16), (21, 20),
                   (6, 2), (17, 2), (6, 21), (17, 21)],
    },
    {
        "name": "09-two-lanes",
        "title": "对称双通道",
        "desc": "两道横墙各留两个门，形成上中下三条走廊；适合拦截与反拦截。",
        "obstacles": [(x, 7) for x in range(3, 21) if x not in (6, 17)]
                     + [(x, 16) for x in range(3, 21) if x not in (6, 17)],
        "high": [(6, 4), (17, 4), (6, 19), (17, 19)],
        "normal": [(3, 3), (20, 3), (3, 20), (20, 20), (12, 3), (12, 20), (3, 12), (20, 12),
                   (12, 5), (12, 18), (8, 12), (15, 12)],
    },
    {
        "name": "10-maze-fragments",
        "title": "迷宫碎片",
        "desc": "散布 2×2 小块，视野被频繁切割，遭遇战多为近距离。",
        "obstacles": [(x + dx, y + dy) for (x, y) in
                      [(5, 5), (5, 9), (5, 13), (5, 17), (9, 5), (9, 17),
                       (13, 5), (13, 17), (17, 5), (17, 9), (17, 13), (17, 17)]
                      for dx in (0, 1) for dy in (0, 1)],
        "high": [(7, 7), (16, 7), (7, 16), (16, 16)],
        "normal": [(3, 3), (20, 3), (3, 20), (20, 20), (11, 6), (12, 6), (11, 18), (12, 18),
                   (6, 12), (17, 12), (12, 9), (12, 14)],
    },
]


def _relocate(m: dict, cell, used: set) -> tuple:
    """把落在禁区（堡垒/出生点附近）的资源挪到最近的合法格。"""
    obstacles = set(m["obstacles"])
    spawns = [p for _, p in SPAWNS]
    q = deque([tuple(cell)])
    seen = {tuple(cell)}
    while q:
        c = q.popleft()
        ok = (c not in obstacles and c not in used and c not in FORTRESS
              and min(abs(c[0] - s[0]) + abs(c[1] - s[1]) for s in spawns) >= 3
              and 0 <= c[0] < W and 0 <= c[1] < H)
        if ok:
            return c
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            n = (c[0] + dx, c[1] + dy)
            if n not in seen and 0 <= n[0] < W and 0 <= n[1] < H:
                seen.add(n)
                q.append(n)
    return tuple(cell)


def normalize(m: dict) -> None:
    """确保没有资源压在堡垒区，也没有资源紧贴出生点（开局白送）。"""
    spawns = [p for _, p in SPAWNS]
    used = {tuple(c) for c in m["high"] + m["normal"]}
    for key in ("high", "normal"):
        out = []
        for cell in m[key]:
            c = tuple(cell)
            bad = (c in FORTRESS
                   or min(abs(c[0] - s[0]) + abs(c[1] - s[1]) for s in spawns) < 3)
            if bad:
                used.discard(c)
                c = _relocate(m, c, used)
                used.add(c)
            out.append(list(c))
        m[key] = out


def validate(m: dict) -> None:
    obstacles = set(m["obstacles"])
    if len(obstacles) != len(m["obstacles"]):
        raise ValueError(f"{m['name']}: 障碍重复")
    spawns = {tuple(p) for _, p in SPAWNS}
    resources = [tuple(c) for c in (m["high"] + m["normal"])]
    if len(m["high"]) != 4 or len(m["normal"]) != 12:
        raise ValueError(f"{m['name']}: 资源数量必须是 4 高 + 12 普通")
    if len(set(resources)) != 16:
        raise ValueError(f"{m['name']}: 资源坐标重复")
    for c in list(obstacles) + list(resources):
        if not (0 <= c[0] < W and 0 <= c[1] < H):
            raise ValueError(f"{m['name']}: 坐标越界 {c}")
    for c in resources:
        if c in obstacles:
            raise ValueError(f"{m['name']}: 资源与障碍重叠 {c}")
        if c in spawns:
            raise ValueError(f"{m['name']}: 资源与出生点重叠 {c}")
    for c in spawns:
        if c in obstacles:
            raise ValueError(f"{m['name']}: 出生点在障碍上 {c}")
    if (12, 12) in obstacles:
        raise ValueError(f"{m['name']}: (12,12) 被障碍占用")
    if FORTRESS & obstacles:
        raise ValueError(f"{m['name']}: 堡垒区被障碍占用 {FORTRESS & obstacles}")
    if FORTRESS & set(resources):
        raise ValueError(f"{m['name']}: 堡垒区有资源 {FORTRESS & set(resources)}")
    for c in resources:
        if min(abs(c[0] - s[0]) + abs(c[1] - s[1]) for s in spawns) < 3:
            raise ValueError(f"{m['name']}: 资源 {c} 紧贴出生点")

    # 连通性：从 A 出生点四邻接 BFS
    blocked = obstacles
    q = deque([(1, 1)])
    seen = {(1, 1)}
    while q:
        x, y = q.popleft()
        for dx, dy in ((0, -1), (0, 1), (-1, 0), (1, 0)):
            n = (x + dx, y + dy)
            if n in seen or n in blocked or not (0 <= n[0] < W and 0 <= n[1] < H):
                continue
            seen.add(n)
            q.append(n)
    unreachable = [c for c in resources if c not in seen]
    if unreachable:
        raise ValueError(f"{m['name']}: 资源不可达 {unreachable}")
    for team, p in SPAWNS:
        if p not in seen:
            raise ValueError(f"{m['name']}: 出生点 {team} 不可达")


def geometry(m: dict) -> dict:
    resources = [{"id": f"high_{x}_{y}", "position": [x, y], "value": 20} for x, y in m["high"]]
    resources += [{"id": f"normal_{x}_{y}", "position": [x, y], "value": 10} for x, y in m["normal"]]
    return {
        "map": {
            "width": W, "height": H,
            "obstacles": [list(c) for c in m["obstacles"]],
            "resources": resources,
        },
        "robots": [{"team": t, "spawn": list(p)} for t, p in SPAWNS],
    }


def practice(m: dict, template: dict) -> dict:
    cfg = copy.deepcopy(template)
    cfg["stage"] = 2
    cfg["map"] = geometry(m)["map"]
    cfg["robots"] = geometry(m)["robots"]
    cfg["mission_text"] = "获得至少两个普通资源；获得至少一个高价值资源；比赛结束前进入坐标 [12, 12]。"
    cfg["missions"] = [
        {"id": "collect_two_normal", "type": "normal_resources", "target": 2},
        {"id": "collect_high_value", "type": "high_resources", "target": 1},
        {"id": "reach_target", "type": "reach", "target": 1, "position": [12, 12]},
    ]

    ev = cfg["events"]
    ev["enabled"] = True
    ev["enabled_stages"] = [2]
    d = ev["definitions"]

    d["explosion"]["damage"] = 80
    d["explosion"]["rect"] = {"top_left": [4, 4], "bottom_right": [8, 8]}
    d["illusion"]["rect"] = {"top_left": [7, 3], "bottom_right": [11, 7]}
    d["illusion"]["effect_ticks"] = 50
    d["angel"]["points"] = [[4, 11], [19, 11]]
    d["angel"]["duration_ticks"] = 20
    d["angel"]["acquire_ticks"] = 20
    d["angel"]["effect_ticks"] = 10
    d["angel"]["heal_per_tick"] = 10
    d["unrivaled"]["duration_ticks"] = 50
    d["unrivaled"]["stay_ticks"] = 10
    d["unrivaled"]["attack_damage"] = 50
    d["unrivaled"]["defended_damage"] = 10
    d["unrivaled"]["center_mode"] = "map_center"
    d["unrivaled"]["size"] = 3
    d["world_split"]["point"] = [7, 7]
    d["world_split"]["duration_ticks"] = 30
    d["pepper"]["points"] = [[6, 7], [18, 16]]
    d["pepper"]["duration_ticks"] = 30
    d["pepper"]["damage"] = 50
    d["purity"]["duration_ticks"] = 20
    d["purity"]["reflection_damage"] = 20
    d["pan"]["duration_ticks"] = 30
    d["pan"]["movement_window_ticks"] = 20
    d["pan"]["vulnerability_ticks"] = 30
    d["pan"]["damage_multiplier"] = 2
    ev["announcement_count"] = len(ev["schedule"])
    return cfg


def main() -> None:
    here = os.path.dirname(os.path.abspath(__file__))
    repo = os.path.dirname(here)
    out_dir = os.path.join(repo, "maps")
    os.makedirs(out_dir, exist_ok=True)

    template_path = os.path.join(here, "event_template.json")
    with open(template_path, encoding="utf-8") as fh:
        template = json.load(fh)

    for m in MAPS:
        normalize(m)
        validate(m)
        g = geometry(m)
        with open(os.path.join(out_dir, f"{m['name']}.json"), "w", encoding="utf-8") as fh:
            json.dump(g, fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        with open(os.path.join(out_dir, f"{m['name']}.practice.json"), "w", encoding="utf-8") as fh:
            json.dump(practice(m, template), fh, ensure_ascii=False, indent=2)
            fh.write("\n")
        print(f"[ok] {m['name']:22s} {m['title']:8s} 障碍 {len(m['obstacles']):3d}  资源 16 / 200")

    print(f"\n已生成 {len(MAPS)} 张地图 → {out_dir}")


if __name__ == "__main__":
    main()
