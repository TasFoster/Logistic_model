"""
Запуск аналитической модели из командной строки.

Примеры:
    python -m core.analitic.run --graph core/simulator/graph_contract.json
    python -m core.analitic.run --graph core/simulator/graph_2stage.json

Печатает стационарные потоки и загрузки узлов, узкое место и предельную
пропускную способность. Формат сводки намеренно близок к симулятору
(core/simulator/run.py), чтобы числа сверялись глазами.
"""

from __future__ import annotations

import argparse
import os
import sys

from .loader import load_graph
from .balance import solve
from .resources import size_all
from . import metrics
from . import norms
from . import facility as facility_mod

try:  # корректная кириллица в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> None:
    here = os.path.dirname(__file__)
    default_graph = os.path.join(os.path.dirname(here), "simulator", "graph_contract.json")

    ap = argparse.ArgumentParser(description="Аналитическая модель сортировочного центра")
    ap.add_argument("--graph", default=default_graph, help="путь к файлу графа JSON")
    ap.add_argument("--input-stream", type=float, default=None,
                    help="переопределить входной поток, товаров/ч")
    ap.add_argument("--out", default="results", help="папка для CSV-результатов")
    args = ap.parse_args()

    graph = load_graph(args.graph)
    arrival = None
    if args.input_stream is not None:
        arrival = args.input_stream / (graph["input_stream"] / graph["arrival_rate_h"])
    res = solve(graph, arrival=arrival)
    sizing = size_all(graph, res)
    fac = facility_mod.compute(graph, res, sizing)

    bn = res.bottleneck()
    print(f"Граф: {args.graph}")
    print(f"Вход: {res.arrival_rate_h:.1f} ед/ч (~{res.input_stream:.0f} товаров/ч)")
    print(f"Узкое место: {bn.name} ({100 * bn.load:.1f}%)" if bn else "Узкое место: —")
    print(f"Предельный вход (узкое место -> 100%): ~{res.max_input_stream:,.0f} товаров/ч")
    print("-" * 74)
    print(f"{'узел':<18}{'throughput':>13}{'мощность':>11}{'загр.':>8}{'единиц (есть/надо)':>20}")
    total_have = total_needed = 0
    for r in res.nodes.values():
        if r.capacity <= 0:                       # мгновенные узлы (Storage/сток)
            print(f"{r.name:<18}{r.throughput:>10.0f} шт/ч{'—':>11}{'—':>8}{'—':>20}")
            continue
        s = sizing[r.id]
        total_have += s["have"]
        total_needed += s["needed"]
        mark = " *" if bn and r.id == bn.id else "  "
        units = f"{s['have']} / {s['needed']}"
        print(f"{r.name:<18}{r.throughput:>10.0f} шт/ч{r.capacity:>9.0f} шт/ч"
              f"{100 * r.load:>6.1f}%{mark}{units:>18}")
    print("-" * 74)
    print(f"{'ИТОГО единиц ресурса':<18}{'':>24}{'':>8}  {total_have} / {total_needed}"
          f"  (* = узкое место)")
    print("ПОТОКИ ПО РЁБРАМ (сущностей/ч):")
    for name, flow in res.rib_flow.items():
        rib = next(rr for rr in graph["ribs"] if rr["name"] == name)
        print(f"  {name:<16}{rib['src']}->{rib['dst']:<3} {rib['etype']:<10}{flow:>10.0f}")

    # --- штат / парк / площадь ---
    print("-" * 74)
    print("ШТАТ, ПАРК, ПЛОЩАДЬ:")
    print(f"  штат (люди)        : {fac.total_staff} чел")
    park = ", ".join(f"{k}×{v}" for k, v in sorted(fac.park.items()))
    print(f"  парк техники       : {park}")
    print(f"  площадь оборудования: {fac.equipment_area_m2:>10,.0f} м²")
    print(f"  площадь буферов     : {fac.buffer_area_m2:>10,.0f} м²")
    print(f"  накопители упаковки : {fac.accumulator_area_m2:>10,.0f} м²")
    print(f"  ИТОГО площадь (×{norms.AISLE_FACTOR:.2f} проходы): {fac.total_area_m2:>10,.0f} м²"
          f"  (ориентир 20 000–24 000)")

    # --- профиль направлений (для отчёта) ---
    dcfg = graph.get("directions") or {}
    if dcfg.get("count"):
        from .directions import DirectionProfile
        prof = DirectionProfile(
            count=dcfg.get("count", 400), top_share=dcfg.get("top_share", 0.2),
            volume_share=dcfg.get("volume_share", 0.8), profile=dcfg.get("profile", "pareto"),
            groups=dcfg.get("groups", 0), grouping=dcfg.get("grouping", "balanced"))
        print("-" * 74)
        print("НАПРАВЛЕНИЯ: " + prof.describe(res.input_stream))
        if prof.groups:
            gs = prof.group_volume_shares()
            print(f"  групп 2-й стадии ({dcfg.get('grouping')}): {prof.groups}, "
                  f"доля объёма группы от {100 * min(gs):.1f}% до {100 * max(gs):.1f}%")

    # выгрузка метрик в общий формат сверки
    os.makedirs(args.out, exist_ok=True)
    rows = metrics.build_rows(graph, res, sizing, fac)
    csv_path = os.path.join(args.out, "analytic_metrics.csv")
    metrics.write_csv(rows, csv_path)
    print("-" * 74)
    print(f"CSV метрик (формат сверки object,metric,value,unit): {os.path.abspath(csv_path)}")


if __name__ == "__main__":
    main()
