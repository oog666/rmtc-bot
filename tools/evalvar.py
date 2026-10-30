"""快速参数评估：十张地图 × N 个种子 × 指定策略。

用法：
  python3 tools/evalvar.py --decision balanced --seeds 2
  python3 tools/evalvar.py --decision balanced --override s2_hunt_penalty_low=0.0
"""

from __future__ import annotations

import argparse
import glob
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "contestant_ws", "src", "rmtc_bot"))

import sim as S  # noqa: E402
from rmtc_bot.config import Params  # noqa: E402

from experiment import Recorder  # noqa: E402


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--decision", default="balanced")
    ap.add_argument("--seeds", type=int, default=2)
    ap.add_argument("--maps", default="maps")
    ap.add_argument("--override", nargs="*", default=[],
                    help="参数覆盖，形如 key=value")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    base = Params().to_dict()
    for item in args.override:
        key, _, raw = item.partition("=")
        cur = base.get(key)
        base[key] = type(cur)(raw) if isinstance(cur, (int, float)) and not isinstance(cur, bool) else raw
    params = Params(**base)

    files = sorted(glob.glob(os.path.join(args.maps, "*.practice.json")))
    scores, per = [], {}
    for f in files:
        vals = []
        for seed in range(1, args.seeds + 1):
            sim = S.build_sim_from_file(f, seed=seed)
            ctl = [Recorder(sim, "A", args.decision, params=params)]
            for i, mode in enumerate(["collect", "hunter", "collect"]):
                ctl.append(S.SimpleBot(mode, seed + i))
            vals.append(sim.run(ctl)["rows"][0]["score"])
        per[os.path.basename(f)[:2]] = round(sum(vals) / len(vals), 1)
        scores.extend(vals)

    out = {"decision": args.decision, "mean": round(sum(scores) / len(scores), 2),
           "min": min(scores), "max": max(scores), "per_map": per}
    print(json.dumps(out, ensure_ascii=False) if args.json else
          f"{args.decision:12s} 均分 {out['mean']:.2f}  最低 {out['min']:.1f}  最高 {out['max']:.1f}  {per}")


if __name__ == "__main__":
    main()
