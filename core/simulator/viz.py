"""
2D-схема сортировочного центра по результатам прогона имитационной модели.

Строит один самодостаточный HTML-файл (без интернета и внешних библиотек — важно
для проверки на изолированном сервере). Оформление — техническая схема цеха:
аппараты-блоки с тегами, ортогональные конвейеры со стрелками направления, сетка,
моноширинная типографика. Цвет заголовка блока и полоса внизу — загрузка узла.

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
            "hauled": bool(r.pool), "virtual": False,
        })
        seen.add(key)

    # источники (машина новых КТЯ) кладут тару прямо в буфер, минуя ребро графа —
    # рисуем пунктирную связь до узла, буфер которого они восполняют
    for n in m.nodes.values():
        if n.type != "source":
            continue
        rib = m._rib_by_name.get(n.params.get("target_rib", ""))
        if rib is not None:
            ribs.append({
                "src": n.id, "dst": rib.dst, "etype": n.params.get("emit_type", ""),
                "flow": round(n.produced / h), "cap": 0, "fill": 0.0,
                "travel": 0.0, "hauled": False, "virtual": True,
            })

    pack = next((n for n in m.nodes.values() if n.by_direction), None)
    kty = m._sinks.get("KTY_full") or m._sinks.get("KTY_out")

    summary = {
        "input_items_h": round(m.generated / h * 540),
        "sim_hours": round(h, 2),
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
    if m.pools:
        name = next(iter(m.pools))
        res = m.pools[name]
        util = 100.0 * m.pool_busy[name] / (res.capacity * m.sim_time) if res.capacity else 0.0
        summary["haulers"] = f"{res.capacity} ед · {util:.0f}%"

    cand = [n for n in nodes if n["type"] != "source"]
    if cand:
        b = max(cand, key=lambda n: n["busy"] + n["blocked"])
        summary["bottleneck"] = b["name"]
        summary["bottleneck_load"] = round(b["busy"] + b["blocked"], 1)

    return {"nodes": nodes, "ribs": ribs, "summary": summary}


HTML = """<meta charset="utf-8">
<title>Сортировочный центр — схема потоков</title>
<style>
  :root{
    --paper:#e7e4da; --panel:#faf9f4; --ink:#1b2733; --ink2:#63707c;
    --line:#c6c2b6; --grid:#d6d2c6; --edge:#35618e; --edge2:#8aa6c4;
    --ok:#3f7d54; --warm:#c1871c; --crit:#b23a2c; --down:#3a4149;
    --mono:'Cascadia Mono','JetBrains Mono',Consolas,'DejaVu Sans Mono',monospace;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--paper);color:var(--ink);font-family:var(--mono);
       font-size:13px;line-height:1.4;
       background-image:linear-gradient(var(--grid) 1px,transparent 1px),
                        linear-gradient(90deg,var(--grid) 1px,transparent 1px);
       background-size:26px 26px;background-position:-1px -1px}
  .sheet{max-width:1200px;margin:0 auto;padding:22px}

  /* заголовок как штамп чертежа */
  .titleblock{border:1.5px solid var(--ink);background:var(--panel);
       display:flex;align-items:stretch;margin-bottom:16px}
  .titleblock .main{padding:12px 16px;border-right:1.5px solid var(--ink);flex:1}
  .titleblock h1{margin:0;font-size:15px;letter-spacing:.14em;text-transform:uppercase;
       font-weight:700}
  .titleblock .sub{color:var(--ink2);font-size:12px;margin-top:4px}
  .titleblock .stamp{padding:12px 16px;display:flex;flex-direction:column;
       justify-content:center;min-width:190px}
  .titleblock .stamp .row{display:flex;justify-content:space-between;gap:14px}
  .titleblock .stamp .k{color:var(--ink2);text-transform:uppercase;font-size:10px;
       letter-spacing:.1em}
  .titleblock .stamp .v{font-weight:700}

  /* приборная панель */
  .gauges{display:flex;border:1.5px solid var(--ink);background:var(--panel);
       margin-bottom:16px;flex-wrap:wrap}
  .gauge{padding:10px 16px;border-right:1px solid var(--line);flex:1;min-width:150px}
  .gauge:last-child{border-right:none}
  .gauge .lbl{color:var(--ink2);text-transform:uppercase;font-size:10px;
       letter-spacing:.12em;margin-bottom:5px}
  .gauge .val{font-size:20px;font-weight:700;letter-spacing:.02em}
  .gauge .val small{font-size:12px;color:var(--ink2);font-weight:400}
  .gauge.alert .val{color:var(--crit)}

  .frame{border:1.5px solid var(--ink);background:var(--panel);padding:6px;
       overflow-x:auto}
  svg{display:block}
  .legend{display:flex;gap:20px;flex-wrap:wrap;color:var(--ink2);font-size:11px;
       margin-top:12px;text-transform:uppercase;letter-spacing:.06em}
  .legend .sw{display:inline-block;width:12px;height:12px;vertical-align:-2px;
       margin-right:6px;border:1px solid var(--ink)}
  .legend .ln{display:inline-block;width:20px;height:0;vertical-align:middle;
       margin-right:6px;border-top:3px solid var(--edge)}

  .tip{position:fixed;pointer-events:none;background:var(--ink);color:#f4f2ec;
       padding:7px 9px;font-size:11px;opacity:0;transition:opacity .08s;z-index:9;
       white-space:pre;line-height:1.5;border:1px solid #000;font-family:var(--mono)}
  text{font-family:var(--mono)}
</style>

<div class="sheet">
  <div class="titleblock">
    <div class="main">
      <h1>Сортировочный центр · имитационная модель</h1>
      <div class="sub" id="sub"></div>
    </div>
    <div class="stamp" id="stamp"></div>
  </div>

  <div class="gauges" id="gauges"></div>

  <div class="frame"><svg id="svg"></svg></div>

  <div class="legend">
    <span><i class="sw" style="background:var(--ok)"></i>загрузка &lt;70%</span>
    <span><i class="sw" style="background:var(--warm)"></i>70–90%</span>
    <span><i class="sw" style="background:var(--crit)"></i>&gt;90% узкое место</span>
    <span><i class="sw" style="background:var(--down)"></i>отказ</span>
    <span><i class="ln"></i>конвейер · толщина = поток</span>
    <span><i class="ln" style="border-top-style:dashed;border-top-color:var(--ink2)"></i>подача тары</span>
  </div>
</div>
<div class="tip" id="tip"></div>

<script>
const DATA = __DATA__;
const S = DATA.summary, N = DATA.nodes, R = DATA.ribs;
const css = k => getComputedStyle(document.documentElement).getPropertyValue(k).trim();
const C = {ok:css('--ok'),warm:css('--warm'),crit:css('--crit'),down:css('--down'),
           edge:css('--edge'),edge2:css('--edge2'),ink:css('--ink'),ink2:css('--ink2'),
           line:css('--line'),panel:css('--panel')};

// --- штамп + подпись ---
document.getElementById('sub').textContent =
  `Вход ${S.input_items_h.toLocaleString('ru')} товаров/ч · горизонт ${S.sim_hours} ч` +
  (S.directions ? ` · ${S.directions} направлений (${S.groups}×${S.directions/S.groups})` : '');
document.getElementById('stamp').innerHTML =
  `<div class="row"><span class="k">Узел-ограничитель</span></div>` +
  `<div class="row"><span class="v">${S.bottleneck||'—'}</span>` +
  `<span class="v">${S.bottleneck_load!=null?S.bottleneck_load+'%':''}</span></div>`;

// --- приборы ---
const gauges = [
  ['Выход', S.kty_per_h!=null ? S.kty_per_h.toLocaleString('ru') : '—', 'КТЯ/ч', false],
  ['Заполнение КТЯ', S.avg_fill!=null ? S.avg_fill : '—', S.batch?('из '+S.batch):'', false],
  ['Недозаполнено', S.underfilled!=null ? S.underfilled : '—', '%', S.underfilled>10],
  ['Ячейки', S.cells!=null ? S.cells : '—', 'шт', false],
  ['Товар в центре', S.residence_min||'—', 'мин', S.residence_min>30],
  ['Погрузчики', S.haulers || '—', '', false],
];
document.getElementById('gauges').innerHTML = gauges.map(([l,v,u,alert]) =>
  `<div class="gauge${alert?' alert':''}"><div class="lbl">${l}</div>`+
  `<div class="val">${v} <small>${u}</small></div></div>`).join('');

// --- геометрия схемы ---
const BW=168, BH=44, PADX=90, PADY=54, W=1180;
const xs=N.map(n=>n.x), ys=N.map(n=>n.y);
const x0=Math.min(...xs),x1=Math.max(...xs),y0=Math.min(...ys),y1=Math.max(...ys);

// Вертикаль: одинаковый шаг между соседними ЗАНЯТЫМИ уровнями, большие пустые
// промежутки сжимаем — иначе 20 секций растягивают холст и появляются пустоты.
const uy=[...new Set(N.map(n=>n.y))].sort((a,b)=>a-b);
let dense=Infinity;
for(let i=1;i<uy.length;i++) dense=Math.min(dense,uy[i]-uy[i-1]);
if(!isFinite(dense)||dense<=0) dense=1;
const ROW=BH+16;                       // шаг между соседними уровнями, px
const rowY={}; let py=PADY;
for(let i=0;i<uy.length;i++){
  if(i>0){
    const g=uy[i]-uy[i-1];
    py+=Math.max(ROW, Math.min(g/dense*ROW, 2.2*ROW));   // мелкий шаг=ROW, большие сжаты
  }
  rowY[uy[i].toFixed(4)]=py;
}
const H=Math.ceil(py+PADY);
const sx=v=>PADX+(x1===x0?.5:(v-x0)/(x1-x0))*(W-2*PADX);
const sy=v=>rowY[v.toFixed(4)] ?? (PADY+(v-y0)/(y1-y0||1)*(H-2*PADY));

const status=n=>{const l=n.busy+n.blocked;
  return n.down>1?C.down:l>90?C.crit:l>70?C.warm:C.ok;};
const maxFlow=Math.max(1,...R.filter(r=>!r.virtual).map(r=>r.flow));
const byId=Object.fromEntries(N.map(n=>[n.id,n]));

const svg=document.getElementById('svg');
svg.setAttribute('viewBox',`0 0 ${W} ${H}`);
svg.setAttribute('width',W); svg.setAttribute('height',H);
let out='';

// --- конвейеры: ортогональная разводка (Г-образные колена) ---
for(const r of R){
  const a=byId[r.src], b=byId[r.dst]; if(!a||!b)continue;
  const acx=sx(a.x), bcx=sx(b.x), ay=sy(a.y), by=sy(b.y);
  const ax = bcx>acx ? acx+BW/2 : acx-BW/2;
  const bx = bcx>acx ? bcx-BW/2 : bcx+BW/2;
  const midx=(ax+bx)/2;
  const d=`M ${ax} ${ay} H ${midx} V ${by} H ${bx}`;
  if(r.virtual){
    out+=`<path d="${d}" fill="none" stroke="${C.ink2}" stroke-width="1.4"
      stroke-dasharray="3 4" opacity=".8"
      data-tip="подача тары: ${r.flow.toLocaleString('ru')} шт/ч"/>`;
  }else{
    const w=1.4+6*Math.sqrt(r.flow/maxFlow);
    const col=r.fill>95?C.crit:C.edge;
    out+=`<path d="${d}" fill="none" stroke="${col}" stroke-width="${w.toFixed(1)}"
      opacity="${r.fill>95?.95:.62}" stroke-linejoin="miter"
      data-tip="${r.etype}: ${r.flow.toLocaleString('ru')} шт/ч&#10;буфер ${r.fill}% (ёмкость ${r.cap})&#10;${r.hauled?'везёт погрузчик · ':''}в пути ${r.travel} с"/>`;
    // стрелка направления у приёмника
    const dir=bx>midx?1:-1;
    out+=`<path d="M ${bx} ${by} l ${-7*dir} -4 l 0 8 z" fill="${col}" opacity=".85"/>`;
  }
}

// --- аппараты (узлы) ---
for(const n of N){
  const cx=sx(n.x), cy=sy(n.y), x=cx-BW/2, y=cy-BH/2, col=status(n);
  const load=Math.min(100,n.busy+n.blocked);
  const tip=`${n.name} · ${n.type}&#10;поток ${n.thr.toLocaleString('ru')} шт/ч из `+
    `${n.capacity.toLocaleString('ru')} шт/ч (${n.workers} ед.)&#10;`+
    `работа ${n.busy}% · блокировка ${n.blocked}% · голодание ${n.starved}%`+
    (n.down>0?` · отказ ${n.down}%`:'');
  out+=`<g data-tip="${tip}">
    <rect x="${x}" y="${y}" width="${BW}" height="${BH}" fill="${C.panel}"
      stroke="${C.ink}" stroke-width="1.4"/>
    <rect x="${x}" y="${y}" width="${BW}" height="15" fill="${col}"/>
    <text x="${x+8}" y="${y+11}" font-size="10.5" font-weight="700" fill="#fbfaf4"
      letter-spacing=".04em">${n.name}</text>
    <text x="${x+8}" y="${y+33}" font-size="13" font-weight="700" fill="${C.ink}">${n.thr.toLocaleString('ru')}<tspan font-size="9" fill="${C.ink2}"> /ч</tspan></text>
    <text x="${x+BW-8}" y="${y+33}" text-anchor="end" font-size="9" fill="${C.ink2}">${Math.round(load)}%</text>
    <rect x="${x}" y="${y+BH-4}" width="${BW}" height="4" fill="${C.line}"/>
    <rect x="${x}" y="${y+BH-4}" width="${(BW*load/100).toFixed(1)}" height="4" fill="${col}"/>
  </g>`;
}
svg.innerHTML=out;

// --- подсказки ---
const tip=document.getElementById('tip');
svg.addEventListener('mousemove',e=>{
  const el=e.target.closest('[data-tip]');
  if(!el){tip.style.opacity=0;return;}
  tip.textContent=el.getAttribute('data-tip');
  tip.style.left=(e.clientX+14)+'px';
  tip.style.top=(e.clientY+14)+'px';
  tip.style.opacity=1;
});
svg.addEventListener('mouseleave',()=>tip.style.opacity=0);
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
    print(f"Узел-ограничитель: {data['summary'].get('bottleneck', '—')} "
          f"({data['summary'].get('bottleneck_load', 0)}%)")


if __name__ == "__main__":
    main()
