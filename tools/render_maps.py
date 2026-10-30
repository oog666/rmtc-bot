"""把十张地图和四种策略的实测成绩渲染成一张自包含的 HTML 总览页。

输入：maps/*.json（geometry）、experiments/results.json
输出：docs/十图实验总览.html
"""

from __future__ import annotations

import glob
import html
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)

TITLES = {
    "01-open-field": ("开阔平原", "几乎无遮挡，纯速度与路线之争"),
    "02-center-keep": ("中央堡垒", "中央 3×3 堡垒区，抢占天下无双"),
    "03-quadrants": ("四象限", "十字长墙切成四块，只留中路通道"),
    "04-ring-corridor": ("环形走廊", "内外双环，走廊窄、遭遇战密集"),
    "05-twin-towers": ("双塔窄道", "两座实心塔压缩视野与机动空间"),
    "06-zigzag": ("锯齿墙", "交错短墙制造反复折线路径"),
    "07-rich-center": ("资源富集中心", "16 个资源围成一圈，中心开花"),
    "08-edge-resources": ("边缘资源", "资源贴边分布，逼迫长距离横穿"),
    "09-two-lanes": ("对称双通道", "上下两条主通道，侧翼可绕"),
    "10-maze-fragments": ("迷宫碎片", "密集碎片墙，局部死路多"),
}

STRATS = [("greedy", "贪婪型"), ("aggressive", "侵略型"),
          ("conservative", "保守型"), ("balanced", "综合型")]


def load_maps() -> dict:
    out = {}
    for f in sorted(glob.glob(os.path.join(REPO, "maps", "*.json"))):
        if f.endswith(".practice.json"):
            continue
        with open(f, encoding="utf-8") as fh:
            data = json.load(fh)
        name = os.path.basename(f)[:-5]
        out[name] = data
    return out


def load_scores() -> dict:
    path = os.path.join(REPO, "experiments", "results.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        rows = json.load(fh)
    agg = {}
    for r in rows:
        key = (r["map"], r["strategy"])
        agg.setdefault(key, []).append(r["score"])
    return {k: sum(v) / len(v) for k, v in agg.items()}


def svg_map(data: dict, size: int = 216) -> str:
    mp = data["map"]
    w, h = mp["width"], mp["height"]
    cell = size / max(w, h)
    obs = {tuple(o) for o in mp["obstacles"]}
    res = {tuple(r["position"]): r["value"] for r in mp["resources"]}
    spawns = {tuple(r["spawn"]) for r in data.get("robots", [])}
    parts = [f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
             f'xmlns="http://www.w3.org/2000/svg" role="img">']
    parts.append(f'<rect width="{size}" height="{size}" fill="#141b2b" rx="6"/>')
    for y in range(h):
        for x in range(w):
            px, py = x * cell, y * cell
            if (x, y) in obs:
                fill = "#33405e"
            else:
                fill = "#1b2437"
            parts.append(f'<rect x="{px:.1f}" y="{py:.1f}" width="{cell:.2f}" '
                         f'height="{cell:.2f}" fill="{fill}"/>')
    fx, fy = 11 * cell, 11 * cell
    parts.append(f'<rect x="{fx:.1f}" y="{fy:.1f}" width="{cell*3:.2f}" height="{cell*3:.2f}" '
                 f'fill="none" stroke="#34d399" stroke-width="1.4" stroke-dasharray="3 2" rx="2"/>')
    for (x, y), v in res.items():
        cx, cy = (x + 0.5) * cell, (y + 0.5) * cell
        color = "#ff8a4c" if v >= 20 else "#f5c451"
        parts.append(f'<circle cx="{cx:.1f}" cy="{cy:.1f}" r="{cell*0.3:.2f}" fill="{color}"/>')
    for (x, y) in spawns:
        cx, cy = (x + 0.5) * cell, (y + 0.5) * cell
        parts.append(f'<rect x="{cx-cell*0.34:.1f}" y="{cy-cell*0.34:.1f}" '
                     f'width="{cell*0.68:.2f}" height="{cell*0.68:.2f}" fill="#60a5fa" rx="1.5"/>')
    parts.append("</svg>")
    return "".join(parts)


def heat(score: float) -> str:
    t = max(0.0, min(1.0, score / 60.0))
    # 低分红 → 高分绿松，深色底上保持可读
    r = int(58 + (52 - 58) * t)
    g = int(70 + (211 - 70) * t)
    b = int(100 + (153 - 100) * t)
    return f"rgba({r},{g},{b},{0.14 + 0.5 * t:.2f})"


def main() -> None:
    maps = load_maps()
    scores = load_scores()

    cards = []
    for name, data in maps.items():
        title, desc = TITLES.get(name, ("", ""))
        best = ""
        if scores:
            row = [(s, scores.get((name, s), -1)) for s, _ in STRATS]
            top = max(row, key=lambda kv: kv[1])
            if top[1] >= 0:
                label = dict(STRATS).get(top[0], top[0])
                best = f'<span class="best">最优 {label} {top[1]:.1f}</span>'
        cards.append(f'''<div class="card">
  <div class="map">{svg_map(data)}</div>
  <div class="meta"><h3>{html.escape(title)}</h3>
  <p>{html.escape(desc)}</p>
  <div class="tags"><code>{name}</code>{best}</div></div>
</div>''')

    head = "".join(f"<th>{label}</th>" for _, label in STRATS)
    body = []
    for name in maps:
        cells = []
        for s, _ in STRATS:
            v = scores.get((name, s))
            if v is None:
                cells.append("<td>—</td>")
            else:
                cls = " class=\"peak\"" if v >= 59.9 else ""
                cells.append(f'<td style="background:{heat(v)}"{cls}>{v:.1f}</td>')
        body.append(f"<tr><td class=\"name\">{name}</td>{''.join(cells)}</tr>")

    total = []
    for s, label in STRATS:
        vals = [scores[(n, s)] for n in maps if (n, s) in scores]
        total.append(f"<td>{sum(vals)/len(vals):.2f}</td>" if vals else "<td>—</td>")

    doc = f'''<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>RMTC 十图实验总览</title>
<style>
  :root {{ color-scheme: dark; }}
  * {{ box-sizing: border-box; }}
  body {{ margin: 0; padding: 32px; background: #0d1220; color: #e6edf7;
         font-family: "PingFang SC", "Microsoft YaHei", system-ui, sans-serif; }}
  h1 {{ font-size: 24px; margin: 0 0 6px; letter-spacing: .5px; }}
  .sub {{ color: #93a4c3; font-size: 13px; margin-bottom: 22px; }}
  .legend {{ display: flex; gap: 18px; flex-wrap: wrap; font-size: 12px;
            color: #a9b8d4; margin-bottom: 22px; }}
  .legend i {{ display: inline-block; width: 10px; height: 10px; border-radius: 2px;
              margin-right: 5px; vertical-align: -1px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fill,minmax(300px,1fr));
          gap: 14px; }}
  .card {{ display: flex; gap: 14px; padding: 12px; border-radius: 12px;
          background: #131a2a; border: 1px solid #1f2a40; }}
  .map svg {{ display: block; border-radius: 8px; }}
  .meta h3 {{ margin: 2px 0 4px; font-size: 15px; }}
  .meta p {{ margin: 0 0 8px; font-size: 12px; color: #93a4c3; line-height: 1.5; }}
  .tags {{ display: flex; flex-direction: column; gap: 6px; align-items: flex-start; }}
  code {{ font-size: 11px; color: #7f92b3; background: #0f1626; padding: 2px 6px;
         border-radius: 4px; }}
  .best {{ font-size: 11px; color: #6ee7b7; background: rgba(52,211,153,.12);
          padding: 2px 6px; border-radius: 4px; }}
  h2 {{ font-size: 16px; margin: 34px 0 10px; }}
  table {{ border-collapse: separate; border-spacing: 2px; font-size: 12.5px; width: 100%; }}
  th {{ color: #93a4c3; font-weight: 500; padding: 6px; text-align: center;
       background: #131a2a; border-radius: 4px; }}
  td {{ padding: 7px 6px; text-align: center; border-radius: 4px; background: #141b2b; }}
  td.name {{ text-align: left; color: #c8d5ea; font-family: ui-monospace, monospace; }}
  td.peak {{ outline: 1px solid #34d399; }}
  tfoot td {{ font-weight: 600; color: #e6edf7; background: #172034; }}
  .note {{ margin-top: 18px; font-size: 12px; color: #7f92b3; line-height: 1.7; }}
</style></head><body>
<h1>RMTC 十图实验总览</h1>
<div class="sub">10 张 24×24 规则合规地图 × 4 种策略 × 3 个随机种子 · 每局 500 Tick、3 台陪练</div>
<div class="legend">
  <span><i style="background:#33405e"></i>障碍</span>
  <span><i style="background:#f5c451"></i>普通资源 10</span>
  <span><i style="background:#ff8a4c"></i>高价值资源 20</span>
  <span><i style="background:#60a5fa"></i>出生点</span>
  <span><i style="border:1px dashed #34d399;background:transparent"></i>堡垒区 [11,11]–[13,13]</span>
</div>
<div class="grid">{''.join(cards)}</div>
<h2>实测得分（0–60）</h2>
<table><thead><tr><th>地图</th>{head}</tr></thead>
<tbody>{''.join(body)}</tbody>
<tfoot><tr><td class="name">十图均分</td>{''.join(total)}</tr></tfoot></table>
<div class="note">得分 = 资源 30 × min(R/120,1) + 对抗 15 × min(D/500,1) + 任务 5 × M。
同一格内 3 局取平均；陪练为视野受限的贪心采集/追击机器人。绿框表示打满 60 分。</div>
</body></html>'''

    out = os.path.join(REPO, "docs", "十图实验总览.html")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(doc)
    print(f"written {out} ({len(doc)} bytes, {len(maps)} maps)")


if __name__ == "__main__":
    main()
