"""参数扫描器：在固定随机种子上比较不同参数组的平均得分。

用法：
  python3 tools/tune.py --stage 1 --grid detour
  python3 tools/tune.py --stage 1 --grid explore
"""

from __future__ import annotations

import argparse
import itertools
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from sim import run_match  # noqa: E402
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "contestant_ws", "src", "rmtc_bot"))
from rmtc_bot.config import Params  # noqa: E402


def evaluate(stage: int, seeds, **overrides) -> float:
    total = 0.0
    for s in seeds:
        p = Params(**{**Params().to_dict(), **overrides})
        out = run_match(stage, s, params=p, preset="random", opponents=3)
        total += out["rows"][0]["score"]
    return total / len(seeds)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", type=int, default=1)
    ap.add_argument("--seeds", type=int, default=10)
    ap.add_argument("--grid", default="detour")
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))

    if args.grid == "detour":
        combos = list(itertools.product([0.7, 0.85, 1.0, 1.15, 1.3], [0.6, 1.2, 1.6]))
        for k, off in combos:
            sc = evaluate(args.stage, seeds, s1_detour_k=k, s1_dist_offset=off)
            print(f"k={k:<5} offset={off:<4} → {sc:6.3f}")
    elif args.grid == "explore":
        combos = list(itertools.product([1.8, 2.1, 2.4, 2.7], [0.6, 0.8, 1.0]))
        for sc_, risk in combos:
            sc = evaluate(args.stage, seeds, s1_explore_scale=sc_, s1_explore_risk=risk)
            print(f"scale={sc_:<5} risk={risk:<4} → {sc:6.3f}")
    elif args.grid == "lock":
        for lock, margin in itertools.product([1, 3, 5, 8], [1.05, 1.2, 1.4]):
            sc = evaluate(args.stage, seeds, s1_lock_ticks=lock, s1_switch_margin=margin)
            print(f"lock={lock:<3} margin={margin:<5} → {sc:6.3f}")


if __name__ == "__main__":
    main()
