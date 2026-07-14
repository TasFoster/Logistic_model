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


def _scrapped_count(model, split) -> int | None:
    """Сколько пустой тары ушло в брак.

    Брак с развилки может уходить либо в сток (нет исходящего ребра), либо в узел
    Storage (вывоз). Ищем ту ветку развилки, которая НЕ ведёт обратно в упаковку.
    """
    reuse_types = set()
    for n in model.nodes.values():
        if n.by_direction or n.type == "pack":
            reuse_types |= set(n.inputs)          # то, что упаковка потребляет — это реюз
    for etype in split.outputs:
        if etype in reuse_types:
            continue                              # это ветка повторного использования
        rib = model.find_rib(split.id, etype)
        if rib is None:                           # брак ушёл в сток
            sink = model._sinks.get(etype)
            if sink is not None:
                return sink.count
        else:                                     # брак ушёл в узел (Storage/вывоз)
            node = model.nodes.get(rib.dst)
            if node is not None:
                return node.processed
    return None


def main() -> None:
    here = os.path.dirname(__file__)
    default_graph = os.path.join(here, "graph_mini.json")

    ap = argparse.ArgumentParser(description="Имитационная модель сортировочного центра")
    ap.add_argument("--graph", default=default_graph, help="путь к файлу графа JSON")
    ap.add_argument("--scenario", default=None,
                    help="файл сценария: времена обработки и интенсивность входа "
                         "(нужен для графов из config/, где времени нет)")
    ap.add_argument("--hours", type=float, default=1.0, help="горизонт симуляции, часов")
    ap.add_argument("--warmup", type=float, default=300.0, help="прогрев (отбрасывается), секунд")
    ap.add_argument("--seed", type=int, default=42, help="зерно ГПСЧ (воспроизводимость)")
    ap.add_argument("--out", default="results", help="папка для CSV-результатов")
    args = ap.parse_args()

    graph = load_graph(args.graph, args.scenario)
    model = SortingCenterModel(graph, seed=args.seed, warmup_s=args.warmup)
    model.run(hours=args.hours)

    os.makedirs(args.out, exist_ok=True)
    rows = metrics.collect_rows(model)
    metrics.write_csv(rows, os.path.join(args.out, "metrics.csv"))
    metrics.write_minute_series(model, os.path.join(args.out, "series_minute.csv"))
    written = metrics.write_interval_series(model, args.out)

    # краткая сводка в консоль
    print(f"Граф: {args.graph}")
    print(f"Горизонт: {args.hours} ч, прогрев: {args.warmup} с, seed: {args.seed}")
    print(f"Вход: {model.arrival_rate_h:.1f} палет/ч "
          f"(~{model.input_stream} товаров/ч), подано палет: {model.generated}")
    print(f"Узкое место: {metrics.find_bottleneck(model)}")
    print("-" * 68)
    # загрузка/блокировка/голодание считаются от полного времени прогона и в сумме = 100%
    print(f"{'узел':<18}{'throughput':>13}{'работа':>10}{'блокир.':>10}{'голод':>10}")
    win_h = max((model.sim_time - model.warmup) / 3600.0, 1e-9)
    sim_h = model.sim_time / 3600.0
    for n in model.nodes.values():
        if n.type == "source":
            # у source-узлов нет обработки — показываем выработку (produced)
            print(f"{n.name:<18}{n.produced / sim_h:>11.0f} шт/ч{'—':>10}{'—':>10}{'—':>10}")
            continue
        cap = n.workers * model.sim_time
        u = 100.0 * n.busy / cap if cap > 0 else 0.0
        b = 100.0 * n.blocked / cap if cap > 0 else 0.0
        s = 100.0 * n.starved / cap if cap > 0 else 0.0
        proc = n.processed - n.processed_at_warmup
        print(f"{n.name:<18}{proc / win_h:>11.0f} шт/ч{u:>9.1f}%{b:>9.1f}%{s:>9.1f}%")
    print("-" * 68)
    for etype, s in sorted(model._sinks.items()):
        print(f"выход {etype:<12}: {s.count / sim_h:>10.0f} шт/ч")

    # сводка по двухстадийной сортировке (если в графе есть группы направлений)
    prof = model.directions
    if prof is not None and prof.groups > 0:
        sec_ids = {r.dst for r in model.ribs if r.dest_group is not None}
        secs = [model.nodes[i] for i in sorted(sec_ids) if i in model.nodes]
        if secs:
            print("-" * 68)
            print(f"ДВУХСТАДИЙНАЯ СОРТИРОВКА: {prof.groups} групп x "
                  f"{prof.count // prof.groups} направлений = {prof.count}")
            print(f"  группировка: {prof.grouping}")
            loads = []
            for n in secs:
                cap = n.workers * model.sim_time
                loads.append(100.0 * n.busy / cap if cap else 0.0)
            print(f"  секций 2-й стадии  : {len(secs)}")
            print(f"  загрузка секций    : от {min(loads):.1f}% до {max(loads):.1f}%")
            print(f"  мощность секций    : от {min(3600/n.services[0] for n in secs):.0f} "
                  f"до {max(3600/n.services[0] for n in secs):.0f} шт/ч")
            hot = max(secs, key=lambda n: n.processed)
            print(f"  самая нагруженная  : {hot.name} ({hot.processed / sim_h:.0f} шт/ч)")

    # сводка по направлениям и заполняемости КТЯ
    pack = next((n for n in model.nodes.values() if n.by_direction), None)
    if pack is not None and model.directions is not None:
        print("-" * 68)
        print("НАПРАВЛЕНИЯ И ЗАПОЛНЯЕМОСТЬ КТЯ:")
        print("  " + model.directions.describe(model.input_stream))
        batch = pack._batch_size()
        if pack.filled:
            avg = pack.items_packed / pack.filled
            share_under = 100.0 * pack.underfilled / pack.filled
            print(f"  выпущено КТЯ       : {pack.filled / sim_h:>8.0f} шт/ч")
            print(f"  средняя заполненность: {avg:>6.1f} из {batch} товаров "
                  f"({100*avg/batch:.0f}%)")
            print(f"  недозаполненных КТЯ: {share_under:>8.1f}%  "
                  f"(закрыты по таймауту {pack.flush_timeout/60:.0f} мин)")
        print(f"  ячеек-накопителей   : {pack.max_open_bins:>8} (пик одновременно занятых)")

    # сводка баланса оборота тары (если в графе есть цикл тары)
    split = next((n for n in model.nodes.values() if n.type == "split"), None)
    new_kty = sum(n.produced for n in model.nodes.values() if n.type == "source")
    if split is not None:
        scrapped_total = _scrapped_count(model, split)
        if scrapped_total is not None:
            empties = split.processed / sim_h
            scrapped = scrapped_total / sim_h
            reused = empties - scrapped
            new_rate = new_kty / sim_h
            supply = reused + new_rate
            print("-" * 68)
            print("ОБОРОТ ТАРЫ (КТЯ):")
            print(f"  вскрыто пустых КТЯ : {empties:>8.0f} шт/ч")
            print(f"  реюз (повторно)    : {reused:>8.0f} шт/ч  ({100*reused/empties:>4.1f}%)")
            print(f"  в брак (вывоз)     : {scrapped:>8.0f} шт/ч  ({100*scrapped/empties:>4.1f}%)")
            print(f"  новых КТЯ (машина) : {new_rate:>8.0f} шт/ч")
            print(f"  баланс: реюз+новые = {supply:>6.0f} шт/ч (спрос упаковки)")
    if written:
        print("-" * 68)
        labels = ", ".join(os.path.basename(p).replace("series_", "").replace(".csv", "")
                           for p in written)
        print(f"Агрегация по интервалам: {labels}")
    print(f"\nCSV сохранены в: {os.path.abspath(args.out)}")


if __name__ == "__main__":
    main()
