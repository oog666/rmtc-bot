"""评测基线：跑固定种子，输出可直接写进报告的表格。

用法：
  python3 tools/bench.py --seeds 12
"""

from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "contestant_ws", "src", "rmtc_bot"))

import sim as S  # noqa: E402


def run_stage(stage: int, seeds, preset: str) -> dict:
    scores, res, dmg, mis = [], [], [], []
    for s in seeds:
        out = S.run_match(stage, s, preset=preset)
        row = out["rows"][0]
        scores.append(row["score"])
        res.append(row["resource"])
        dmg.append(row["damage"])
        mis.append(row["missions"])
    n = len(scores)
    return {
        "stage": stage, "preset": preset, "n": n,
        "score": sum(scores) / n, "best": max(scores), "worst": min(scores),
        "res": sum(res) / n, "dmg": sum(dmg) / n, "mis": sum(mis) / n,
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", type=int, default=12)
    args = ap.parse_args()
    seeds = list(range(1, args.seeds + 1))

    rows = [
        run_stage(1, [1], "practice"),
        run_stage(1, seeds, "random"),
        run_stage(2, [1], "practice"),
        run_stage(2, seeds, "random"),
    ]

    print("| 场景 | 样本 | 平均分 | 最高 | 最低 | 资源均值 | 伤害均值 | 任务均值 |")
    print("| --- | --- | --- | --- | --- | --- | --- | --- |")
    for r in rows:
        name = ("第一阶段·练习图" if r["stage"] == 1 else "第二阶段·练习图") if r["preset"] == "practice" \
            else ("第一阶段·随机 20×20" if r["stage"] == 1 else "第二阶段·随机 24×24（3 陪练）")
        print(f"| {name} | {r['n']} | {r['score']:.2f} | {r['best']:.2f} | {r['worst']:.2f} | "
              f"{r['res']:.1f} | {r['dmg']:.0f} | {r['mis']:.2f} |")

    print("\n策略对比（第二阶段，随机 8 局，3 台陪练）：")
    print("| 策略 | 平均分 | 资源均值 |")
    print("| --- | --- | --- |")
    for dec in ("greedy", "aggressive", "conservative", "balanced"):
        scores, res = [], []
        for s in range(1, 9):
            out = S.run_match(2, s, decision=dec)
            scores.append(out["rows"][0]["score"])
            res.append(out["rows"][0]["resource"])
        print(f"| {dec} | {sum(scores) / len(scores):.2f} | {sum(res) / len(res):.0f} |")


if __name__ == "__main__":
    main()
