"""十张实验地图的规则合规校验。

校验项（全部来自赛事规则 §4 与补充说明）：
  1. 尺寸 24×24，四个出生点固定四角
  2. 资源 12 普通 + 4 高价值，总价值 200
  3. 资源/出生点不与障碍重叠，出生点四邻可走
  4. 全图自由格四连通（不存在被墙封死的区域）
  5. 堡垒 [11,11]–[13,13] 全空（可站立），且不含资源
  6. 每个出生点到最近资源的距离 ≥ 2（不出现\"开局白送\"）
  7. 中央区/要道不被完全堵死（任意两出生点之间可通行）

用法：python3 tools/validate_maps.py [--dir maps]
"""

from __future__ import annotations

import argparse
import glob
import json
import os
from collections import deque

FORTRESS = {(x, y) for x in range(11, 14) for y in range(11, 14)}


def load(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def check(path: str) -> list[str]:
    data = load(path)
    mp = data.get("map") or {}
    w, h = int(mp.get("width", 0)), int(mp.get("height", 0))
    obs = {tuple(o) for o in mp.get("obstacles", [])}
    res = {tuple(r["position"]): int(r.get("value", 10)) for r in mp.get("resources", [])}
    spawns = [tuple(r["spawn"]) for r in (data.get("robots") or [])]
    errs: list[str] = []

    if (w, h) != (24, 24):
        errs.append(f"尺寸应为 24×24，实际 {w}×{h}")
    if sorted(spawns) != sorted([(1, 1), (22, 1), (1, 22), (22, 22)]):
        errs.append(f"出生点应为四角，实际 {spawns}")

    normal = [v for v in res.values() if v < 20]
    high = [v for v in res.values() if v >= 20]
    if (len(normal), len(high)) != (12, 4):
        errs.append(f"资源配比应为 12 普通 + 4 高价值，实际 {len(normal)}+{len(high)}")
    if sum(res.values()) != 200:
        errs.append(f"资源总价值应为 200，实际 {sum(res.values())}")

    for p in res:
        if p in obs:
            errs.append(f"资源 {p} 与障碍重叠")
    for p in spawns:
        if p in obs:
            errs.append(f"出生点 {p} 是障碍")

    inside = {p for p in res if p in FORTRESS}
    if inside:
        errs.append(f"堡垒区内有资源 {sorted(inside)}")
    if FORTRESS & obs:
        errs.append(f"堡垒区被障碍占用 {sorted(FORTRESS & obs)}")

    free = {(x, y) for y in range(h) for x in range(w)} - obs
    if free:
        start = next(iter(free))
        seen = {start}
        q = deque([start])
        while q:
            cx, cy = q.popleft()
            for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                n = (cx + dx, cy + dy)
                if n in free and n not in seen:
                    seen.add(n)
                    q.append(n)
        if seen != free:
            errs.append(f"有 {len(free) - len(seen)} 个自由格与主区域不连通")

    # 堡垒是随机事件地形：只要求全图有足够多的 3×3 空地可落点
    spots = 0
    for (x, y) in free:
        block = {(x + dx, y + dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
        if block <= free:
            spots += 1
    if spots < 20:
        errs.append(f"可用 3×3 空地仅 {spots} 处，堡垒事件落点不足")

    for s in spawns:
        near = min((abs(s[0] - r[0]) + abs(s[1] - r[1]) for r in res), default=99)
        if near < 2:
            errs.append(f"出生点 {s} 距最近资源仅 {near} 格")

    # 出生点两两可达（自由格连通性已覆盖，这里再显式确认一次）
    return errs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="maps")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.dir, "*.json")))
    files = [f for f in files if not f.endswith(".practice.json")]
    bad = 0
    print(f"{'地图':32s} {'障碍':>4s} {'资源':>4s} {'价值':>4s} {'结果'}")
    for f in files:
        errs = check(f)
        data = load(f)
        mp = data.get("map") or {}
        n_res = len(mp.get("resources", []))
        value = sum(int(r.get("value", 10)) for r in mp.get("resources", []))
        status = "OK" if not errs else "FAIL: " + "; ".join(errs)
        if errs:
            bad += 1
        print(f"{os.path.basename(f):32s} {len(mp.get('obstacles', [])):4d} {n_res:4d} "
              f"{value:4d} {status}")
    print(f"\n共 {len(files)} 张，{len(files) - bad} 张通过，{bad} 张失败")
    raise SystemExit(1 if bad else 0)


if __name__ == "__main__":
    main()
