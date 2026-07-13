"""
Запуск имитационной модели из командной строки.

Примеры:
    python -m core.simulator.run
    python -m core.simulator.run --graph core/simulator/graph_mini.json --hours 1
    python -m core.simulator.run --hours 12 --warmup 600 --out results/

Результаты (CSV) кладутся в папку --out (по умолчанию results/).
"""

from __future__ import annotations

import argparse
import os
import sys

from .model import SortingCenterModel, load_graph
from . import metrics

try:  # корректный вывод кириллицы в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def main() -> None:
    here = os.path.dirname(__file__)
    default_graph = os.path.join(here, "graph_mini.json")

    ap = argparse.ArgumentParser(description="Имитационная модель сортировочного центра")
    ap.add_argument("--graph", default=default_graph, help="путь к файлу графа JSON")
    ap.add_argument("--hours", type=float, default=1.0, help="горизонт симуляции, часов")
    ap.add_argument("--warmup", type=float, default=300.0, help="прогрев (отбрасывается), секунд")
    ap.add_argument("--seed", type=int, default=42, help="зерно ГПСЧ (воспроизводимость)")
    ap.add_argument("--out", default="results", help="папка для CSV-результатов")
    args = ap.parse_args()

    graph = load_graph(args.graph)
    model = SortingCenterModel(graph, seed=args.seed, warmup_s=args.warmup)
    model.run(hours=args.hours)

    os.makedirs(args.out, exist_ok=True)
    rows = metrics.collect_rows(model)
    metrics.write_csv(rows, os.path.join(args.out, "metrics.csv"))
    metrics.write_minute_series(model, os.path.join(args.out, "series_minute.csv"))

    # краткая сводка в консоль
    print(f"Граф: {args.graph}")
    print(f"Горизонт: {args.hours} ч, прогрев: {args.warmup} с, seed: {args.seed}")
    print(f"Вход: {model.arrival_rate_h:.1f} палет/ч "
          f"(~{model.input_stream} товаров/ч), подано палет: {model.generated}")
    print(f"Узкое место: {metrics.find_bottleneck(model)}")
    print("-" * 68)
    print(f"{'узел':<16}{'throughput':>14}{'загрузка':>12}{'блокир.':>12}")
    win_h = max((model.sim_time - model.warmup) / 3600.0, 1e-9)
    for n in model.nodes.values():
        proc = n.processed - n.processed_at_warmup
        busy = n.busy - n.busy_at_warmup
        cap = n.workers * (model.sim_time - model.warmup)
        u = 100.0 * busy / cap if cap > 0 else 0.0
        b = 100.0 * n.blocked / cap if cap > 0 else 0.0
        print(f"{n.name:<16}{proc / win_h:>12.0f} шт/ч{u:>10.1f} %{b:>10.1f} %")
    print("-" * 68)
    for etype, s in sorted(model._sinks.items()):
        print(f"выход {etype:<12}: {s.count / (model.sim_time / 3600.0):>10.0f} шт/ч")
    print(f"\nCSV сохранены в: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
