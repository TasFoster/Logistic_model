"""
2D-визуализация сортировочного центра: план, потоки, тепловая карта загрузки.

Строит один самодостаточный HTML-файл (без внешних зависимостей и интернета):
    - узлы размещены по координатам pos из графа;
    - цвет узла — тепловая карта загрузки (зелёный -> жёлтый -> красный);
    - рамка узла подсвечивает проблему: блокировка (красный), отказ (чёрный);
    - толщина ребра — интенсивность потока, цвет — пиковая заполненность буфера;
    - панель метрик, узкое место, оборот тары, заполняемость КТЯ.

Запуск:
    python -m core.simulator.viz --graph core/simulator/graph_2stage.json --hours 3
    -> results/plan.html
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys

from .graph_loader import load_json, normalize
from .model import SortingCenterModel

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def collect(m: SortingCenterModel) -> dict:
    """Собирает всё, что нужно схеме, из отработавшей модели."""
    h = m.sim_time / 3600.0
    nodes = []
    for n in m.nodes.values():
        cap = n.workers * m.sim_time
        pos = m.graph["nodes"][n.id].get("pos", {}) or {}
        if n.type == "source":
            busy = blocked = starved = down = 0.0
            thr = n.produced / h
        else:
            busy = 100.0 * n.busy / cap if cap else 0.0
            blocked = 100.0 * n.blocked / cap if cap else 0.0
            starved = 100.0 * n.starved / cap if cap else 0.0
            down = 100.0 * n.down / cap if cap else 0.0
            thr = n.processed / h
        nodes.append({
            "id": n.id, "name": n.name, "type": n.type,
            "x": float(pos.get("x", 0)), "y": float(pos.get("y", 0)),
            "busy": round(busy, 1), "blocked": round(blocked, 1),
            "starved": round(starved, 1), "down": round(down, 1),
            "thr": round(thr), "workers": n.workers,
            "capacity": round(sum(3600.0 / s for s in n.services if s > 0)),
        })

    seen, ribs = set(), []
    for r in m.ribs:
        # ёмкость реального буфера: рёбра могут делить общий (20 секций -> упаковка)
        cap = r.store.capacity if r.store is not None else r.capacity
        fill = 0.0
        if r.level_samples and cap:
            fill = 100.0 * max(r.level_samples) / cap
        key = id(r.store)
        ribs.append({
            "src": r.src, "dst": r.dst, "etype": r.etype,
            "flow": round(r.passed / h), "cap": cap,
            "fill": round(fill, 1), "travel": round(r.travel, 1),
            "group": r.dest_group,
            "shared": key in seen,
        })
        seen.add(key)

    pack = next((n for n in m.nodes.values() if n.by_direction), None)
    kty = m._sinks.get("KTY_full") or m._sinks.get("KTY_out")

    summary = {
        "input_items_h": round(m.generated / h * 540),
        "sim_hours": round(h, 2),
        "outputs": {k: round(v.count / h) for k, v in sorted(m._sinks.items())},
        "residence_min": round(statistics.mean(kty.residence) / 60.0, 1)
        if (kty and kty.residence) else 0.0,
    }
    if pack and pack.filled:
        summary["kty_per_h"] = round(pack.filled / h)
        summary["avg_fill"] = round(pack.items_packed / pack.filled, 1)
        summary["batch"] = pack._batch_size()
        summary["underfilled"] = round(100.0 * pack.underfilled / pack.filled, 1)
        summary["cells"] = pack.max_open_bins
    if m.directions:
        summary["directions"] = m.directions.count
        summary["groups"] = m.directions.groups
        summary["grouping"] = m.directions.grouping

    # узкое место: максимум (работа + блокировка)
    cand = [n for n in nodes if n["type"] != "source"]
    if cand:
        b = max(cand, key=lambda n: n["busy"] + n["blocked"])
        summary["bottleneck"] = f"{b['name']} ({b['busy']:.0f}% работа + {b['blocked']:.0f}% блокировка)"

    return {"nodes": nodes, "ribs": ribs, "summary": summary}


HTML = """<meta charset="utf-8">
<title>План сортировочного центра</title>
<style>
  :root{--bg:#0f1115;--fg:#e8eaed;--mut:#9aa0a6;--card:#171a20;--line:#2a2f3a}
  @media (prefers-color-scheme: light){
    :root{--bg:#f7f8fa;--fg:#1a1d21;--mut:#5f6368;--card:#fff;--line:#dfe1e5}
  }
  body{margin:0;background:var(--bg);color:var(--fg);
       font:14px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
  .wrap{max-width:1280px;margin:0 auto;padding:24px}
  h1{font-size:20px;margin:0 0 4px}
  .sub{color:var(--mut);margin-bottom:20px}
  .cards{display:flex;flex-wrap:wrap;gap:12px;margin-bottom:20px}
  .card{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:12px 16px;min-width:150px}
  .card .k{color:var(--mut);font-size:12px}
  .card .v{font-size:20px;font-weight:600;margin-top:2px}
  .plan{background:var(--card);border:1px solid var(--line);border-radius:10px;
        padding:12px;overflow-x:auto}
  svg{display:block;min-width:900px;width:100%;height:auto}
  .legend{display:flex;gap:18px;flex-wrap:wrap;color:var(--mut);
          font-size:12px;margin-top:12px}
  .sw{display:inline-block;width:12px;height:12px;border-radius:3px;
      vertical-align:-2px;margin-right:5px}
  .tip{position:fixed;pointer-events:none;background:#000;color:#fff;padding:8px 10px;
       border-radius:6px;font-size:12px;opacity:0;transition:opacity .1s;z-index:9;
       white-space:pre;line-height:1.45}
  text{font:11px system-ui,sans-serif}
</style>
<div class="wrap">
  <h1>План сортировочного центра</h1>
  <div class="sub" id="sub"></div>
  <div class="cards" id="cards"></div>
  <div class="plan"><svg id="svg"></svg></div>
  <div class="legend">
    <span><i class="sw" style="background:#34a853"></i>загрузка &lt;70%</span>
    <span><i class="sw" style="background:#fbbc04"></i>70–90%</span>
    <span><i class="sw" style="background:#ea4335"></i>&gt;90% — узкое место</span>
    <span><i class="sw" style="background:#000;border:1px solid #888"></i>отказ</span>
    <span><i class="sw" style="background:#8ab4f8"></i>толщина ребра — поток</span>
    <span><i class="sw" style="background:#ea4335"></i>красное ребро — буфер переполнен</span>
  </div>
</div>
<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const S = DATA.summary, N = DATA.nodes, R = DATA.ribs;

document.getElementById('sub').textContent =
  `Вход ${S.input_items_h.toLocaleString('ru')} товаров/ч · горизонт ${S.sim_hours} ч` +
  (S.directions ? ` · ${S.directions} направлений` : '') +
  (S.groups ? ` · ${S.groups} групп (${S.grouping})` : '');

const cards = [
  ['Узкое место', S.bottleneck || '—'],
  ['Выход КТЯ', S.kty_per_h ? S.kty_per_h.toLocaleString('ru') + ' шт/ч' : '—'],
  ['Заполненность КТЯ', S.avg_fill ? `${S.avg_fill} / ${S.batch}` : '—'],
  ['Недозаполнено', S.underfilled !== undefined ? S.underfilled + '%' : '—'],
  ['Ячеек-накопителей', S.cells || '—'],
  ['Товар в центре', S.residence_min ? S.residence_min + ' мин' : '—'],
];
document.getElementById('cards').innerHTML = cards.map(([k,v]) =>
  `<div class="card"><div class="k">${k}</div><div class="v">${v}</div></div>`).join('');

// --- геометрия ---
// Высота холста считается от плотности узлов: в колонке 2-й стадии их 20, и при
// фиксированной высоте блоки наезжали друг на друга.
const NH = 34, GAP = 8, PAD = 70, W = 1180;
const xs = N.map(n=>n.x), ys = N.map(n=>n.y);
const x0=Math.min(...xs), x1=Math.max(...xs), y0=Math.min(...ys), y1=Math.max(...ys);

// Высота: самая плотная колонка занимает лишь ЧАСТЬ вертикального диапазона
// (ниже неё есть другие узлы), поэтому холст растягиваем с учётом этой доли —
// иначе блоков в колонке не хватает места и они наезжают.
const cols = {};
for (const n of N) { const k = n.x.toFixed(2); (cols[k] = cols[k] || []).push(n); }
const span = y1 - y0 || 1;
let need = 620;
for (const c of Object.values(cols)) {
  if (c.length < 2) continue;
  const cy = c.map(n => n.y);
  const frac = Math.max((Math.max(...cy) - Math.min(...cy)) / span, 1e-6);
  need = Math.max(need, c.length * (NH + GAP) / frac + 2 * PAD);
}
const H = Math.ceil(need);

const sx = v => PAD + (x1===x0?0.5:(v-x0)/(x1-x0)) * (W-2*PAD);
const sy = v => PAD + (y1===y0?0.5:(v-y0)/(y1-y0)) * (H-2*PAD);

const load = n => n.busy + n.blocked;
const heat = n => { const l=load(n);
  return n.down>1 ? '#000' : l>90 ? '#ea4335' : l>70 ? '#fbbc04' : '#34a853'; };
const maxFlow = Math.max(1, ...R.map(r=>r.flow));

const svg = document.getElementById('svg');
svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
const byId = Object.fromEntries(N.map(n=>[n.id,n]));
let out = '';

// рёбра
for (const r of R) {
  const a = byId[r.src], b = byId[r.dst];
  if (!a || !b) continue;
  const w = 1 + 7 * Math.sqrt(r.flow / maxFlow);
  const col = r.fill > 95 ? '#ea4335' : r.fill > 60 ? '#fbbc04' : '#8ab4f8';
  const op  = r.fill > 95 ? 0.95 : 0.5;
  const HW = 74;                                  // половина ширины блока
  const ax = sx(a.x) + (sx(b.x) > sx(a.x) ? HW : -HW);
  const bx = sx(b.x) + (sx(b.x) > sx(a.x) ? -HW : HW);
  out += `<line x1="${ax}" y1="${sy(a.y)}" x2="${bx}" y2="${sy(b.y)}"
    stroke="${col}" stroke-width="${w.toFixed(1)}" opacity="${op}"
    data-tip="${r.etype}: ${r.flow.toLocaleString('ru')} шт/ч&#10;буфер ${r.fill}% (ёмкость ${r.cap})&#10;в пути ${r.travel} с"/>`;
}
// узлы: имя и поток в одну строку — так влезают все 20 секций без наложения
for (const n of N) {
  const w = 148, h = NH, x = sx(n.x)-w/2, y = sy(n.y)-h/2;
  const stroke = n.down>1 ? '#fff' : n.blocked>20 ? '#ea4335' : 'transparent';
  out += `<g data-tip="${n.name} (${n.type})&#10;поток ${n.thr.toLocaleString('ru')} шт/ч из ${n.capacity.toLocaleString('ru')} шт/ч&#10;работа ${n.busy}% · блокировка ${n.blocked}%&#10;голодание ${n.starved}% · отказ ${n.down}%">
    <rect x="${x}" y="${y}" width="${w}" height="${h}" rx="7"
      fill="${heat(n)}" stroke="${stroke}" stroke-width="2"/>
    <text x="${x+9}" y="${sy(n.y)+4}" fill="#fff" font-weight="600">${n.name}</text>
    <text x="${x+w-9}" y="${sy(n.y)+4}" text-anchor="end" fill="#fff" opacity=".85">${n.thr.toLocaleString('ru')}/ч</text>
  </g>`;
}
svg.innerHTML = out;

// подсказки
const tip = document.getElementById('tip');
svg.addEventListener('mousemove', e => {
  const el = e.target.closest('[data-tip]');
  if (!el) { tip.style.opacity = 0; return; }
  tip.textContent = el.getAttribute('data-tip');
  tip.style.left = (e.clientX + 14) + 'px';
  tip.style.top  = (e.clientY + 14) + 'px';
  tip.style.opacity = 1;
});
svg.addEventListener('mouseleave', () => tip.style.opacity = 0);
</script>
"""


def main() -> None:
    here = os.path.dirname(__file__)
    ap = argparse.ArgumentParser(description="2D-схема центра по результатам прогона")
    ap.add_argument("--graph", default=os.path.join(here, "graph_2stage.json"))
    ap.add_argument("--hours", type=float, default=3.0)
    ap.add_argument("--warmup", type=float, default=600.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="results/plan.html")
    args = ap.parse_args()

    graph = normalize(load_json(args.graph))
    m = SortingCenterModel(graph, seed=args.seed, warmup_s=args.warmup).run(hours=args.hours)
    data = collect(m)

    os.makedirs(os.path.dirname(os.path.abspath(args.out)), exist_ok=True)
    html = HTML.replace("__DATA__", json.dumps(data, ensure_ascii=False))
    with open(args.out, "w", encoding="utf-8") as f:
        f.write(html)

    print(f"Схема: {os.path.abspath(args.out)}")
    print(f"Узкое место: {data['summary'].get('bottleneck', '—')}")


if __name__ == "__main__":
    main()
