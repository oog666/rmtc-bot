"""十张实验地图 × 四种策略的对照实验。

输出：
  experiments/results.md   人读表格
  experiments/results.json 原始数据

用法：
  python3 tools/experiment.py --seeds 2
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from collections import Counter
from typing import List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "contestant_ws", "src", "rmtc_bot"))

import sim as S  # noqa: E402

STRATEGIES = ["greedy", "aggressive", "conservative", "balanced"]


class Recorder(S.BrainController):
    """记录轨迹，并区分"有效等待"与"无效打转"。

    有效等待：阵亡/复活保护、被事件墙封死、已无任何可到达的有价值目标。
    无效打转：活着、且有可到达的资源/任务/前沿，却连续原地 wait。
    """

    def __init__(self, *a, **k):
        super().__init__(*a, **k)
        self.rows: List[dict] = []
        self.actions = Counter()

    def __call__(self, sim, r):
        out = super().__call__(sim, r)
        self.rows.append({
            "tick": sim.tick,
            "pos": r.pos,
            "act": out.get("action", "wait"),
            "alive": r.alive,
            "target": self._has_target(),
            "can_step": self._can_step(),
        })
        self.actions[out.get("action", "wait")] += 1
        return out

    def _has_target(self) -> bool:
        b = getattr(self, "brain", None)
        if b is None or b.obs is None or b.field is None or not b.obs.me.alive:
            return False
        me = b.obs.me.pos
        # 只把"有实际收益"的目标算数：资源、堡垒、任务点。
        # 探索前沿不算——被事件墙封死时前沿仍在，但过去没有任何收益。
        cands = [res.pos for res in b.world.resources.values()]
        fort = b.events.fortress(b.tick)
        # 堡垒只对"会攻击"的策略有收益；贪婪/保守从不攻击，等它不算无效停滞
        if fort and b.current_decision in ("aggressive", "balanced"):
            rect = fort[0]
            cands.append(((rect[0] + rect[2]) // 2, (rect[1] + rect[3]) // 2))
        for m in b.obs.missions:
            if m.get("type") == "reach" and isinstance(m.get("position"), list):
                cands.append((int(m["position"][0]), int(m["position"][1])))
        for c in cands:
            if c != me and b.field.d(c) < 1e8:
                return True
        return False

    def _can_step(self) -> bool:
        """四个邻格里是否至少有一个能走（不是墙、也没有敌车占着）。

        被敌车堵住唯一出口时只能等，这不是"无效打转"。
        """
        b = getattr(self, "brain", None)
        obs = getattr(b, "obs", None) if b else None
        if obs is None or not obs.me.alive or b.world is None:
            return False
        me = obs.me.pos
        blocked = {e.pos for e in b.visible_enemies()}
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            c = (me[0] + dx, me[1] + dy)
            if not (0 <= c[0] < obs.map_w and 0 <= c[1] < obs.map_h):
                continue
            if b.world.cell_state(*c) == 2 or c in blocked:
                continue
            return True
        return False

    @property
    def trace(self):
        return [r["pos"] for r in self.rows]

    def stall(self) -> int:
        worst = cur = 0
        for i in range(1, len(self.rows)):
            prev, now = self.rows[i - 1], self.rows[i]
            stuck = (now["tick"] == prev["tick"] + 1
                     and now["pos"] == prev["pos"]
                     and now["act"] not in ("attack", "defend")
                     and now["alive"] and prev["alive"]
                     and now["target"] and now["can_step"])
            if stuck:
                cur += 1
                worst = max(worst, cur)
            else:
                cur = 0
        return worst


def one(map_path: str, strategy: str, seed: int) -> dict:
    sim = S.build_sim_from_file(map_path, seed=seed)
    ctl = [Recorder(sim, "A", strategy)]
    for i, mode in enumerate(["collect", "hunter", "collect"]):
        ctl.append(S.SimpleBot(mode, seed + i))
    out = sim.run(ctl)
    row = out["rows"][0]
    return {
        "map": os.path.basename(map_path).replace(".practice.json", ""),
        "strategy": strategy,
        "seed": seed,
        "score": row["score"],
        "resource": row["resource"],
        "damage": row["damage"],
        "missions": row["missions"],
        "deaths": row["deaths"],
        "stall": ctl[0].stall(),
        "actions": dict(ctl[0].actions),
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--maps", default="maps")
    args = ap.parse_args()

    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    map_files = sorted(glob.glob(os.path.join(repo, args.maps, "*.practice.json")))
    if not map_files:
        raise SystemExit("没有找到 maps/*.practice.json，请先运行 tools/make_maps.py")

    runs = []
    for f in map_files:
        for strategy in STRATEGIES:
            for s in range(1, args.seeds + 1):
                runs.append(one(f, strategy, s))

    out_dir = os.path.join(repo, "experiments")
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "results.json"), "w", encoding="utf-8") as fh:
        json.dump(runs, fh, ensure_ascii=False, indent=2)

    names = sorted({r["map"] for r in runs})
    lines = ["| 地图 | " + " | ".join(STRATEGIES) + " | 最优 |", "| --- | " + " | ".join(["---"] * len(STRATEGIES)) + " | --- |"]
    for m in names:
        vals = []
        for st in STRATEGIES:
            xs = [r["score"] for r in runs if r["map"] == m and r["strategy"] == st]
            vals.append(sum(xs) / len(xs))
        best = STRATEGIES[max(range(len(vals)), key=lambda i: vals[i])]
        lines.append(f"| {m} | " + " | ".join(f"{v:.1f}" for v in vals) + f" | {best} |")

    lines.append("")
    lines.append("| 策略 | 十图均分 | 最高 | 最低 | 平均资源 | 平均伤害 | 平均任务 | 最长无效停滞 | 攻击次数 |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- | --- | --- |")
    for st in STRATEGIES:
        rs = [r for r in runs if r["strategy"] == st]
        scores = [r["score"] for r in rs]
        lines.append(
            f"| {st} | {sum(scores) / len(scores):.2f} | {max(scores):.1f} | {min(scores):.1f} | "
            f"{sum(r['resource'] for r in rs) / len(rs):.0f} | "
            f"{sum(r['damage'] for r in rs) / len(rs):.0f} | "
            f"{sum(r['missions'] for r in rs) / len(rs):.2f} | "
            f"{max(r['stall'] for r in rs)} | "
            f"{sum(r['actions'].get('attack', 0) for r in rs)} |"
        )

    text = "\n".join(lines) + "\n"
    with open(os.path.join(out_dir, "results.md"), "w", encoding="utf-8") as fh:
        fh.write("# 十图实验数据\n\n" + text)
    print(text)


if __name__ == "__main__":
    main()
