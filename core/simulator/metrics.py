"""
Сбор и выгрузка метрик имитационной модели.

Формат выгрузки — общий контракт сверки с аналитикой (см. Конспект, раздел 6):
CSV со столбцами: object, metric, value, unit

Метрики считаются на установившемся участке (после прогрева warmup), чтобы
сверка с аналитическими балансами была честной (см. Конспект, раздел 7).
"""

from __future__ import annotations

import csv
import statistics

from .model import SortingCenterModel


def _eff_window_h(m: SortingCenterModel) -> float:
    """Длина установившегося окна (после прогрева) в часах."""
    return max((m.sim_time - m.warmup) / 3600.0, 1e-9)


def collect_rows(m: SortingCenterModel) -> list[dict]:
    """Собирает все метрики модели в список строк {object, metric, value, unit}."""
    rows: list[dict] = []
    win_h = _eff_window_h(m)

    def add(obj, metric, value, unit):
        rows.append({"object": obj, "metric": metric,
                     "value": round(value, 2) if isinstance(value, float) else value,
                     "unit": unit})

    # ---- по узлам ----
    sim_h = m.sim_time / 3600.0
    for n in m.nodes.values():
        if n.type == "source":
            # source-узел (машина новых КТЯ): нет обработки, есть выработка
            add(n.name, "produced", n.produced / sim_h, "шт/ч")
            add(n.name, "workers", n.workers, "шт")
            continue
        proc = n.processed - n.processed_at_warmup          # обработано за окно
        throughput = proc / win_h                           # шт/ч
        # доля работы/блокировки/голодания — от полного времени всех воркеров (в сумме 100%)
        capacity_time = n.workers * m.sim_time
        utilization = 100.0 * n.busy / capacity_time if capacity_time > 0 else 0.0
        blocked = 100.0 * n.blocked / capacity_time if capacity_time > 0 else 0.0
        starved = 100.0 * n.starved / capacity_time if capacity_time > 0 else 0.0

        add(n.name, "throughput", throughput, "шт/ч")
        add(n.name, "utilization", utilization, "%")
        add(n.name, "blocked", blocked, "%")
        add(n.name, "starved", starved, "%")
        add(n.name, "workers", n.workers, "шт")

    # ---- по рёбрам (буферам) ----
    for r in m.ribs:
        if r.level_samples:
            mean_lvl = statistics.mean(r.level_samples)
            max_lvl = max(r.level_samples)
        else:
            mean_lvl = max_lvl = 0
        fill = 100.0 * max_lvl / r.capacity if r.capacity else 0.0
        add(f"{r.name}({r.src}->{r.dst},{r.etype})", "queue_mean", float(mean_lvl), "шт")
        add(f"{r.name}({r.src}->{r.dst},{r.etype})", "queue_max", max_lvl, "шт")
        add(f"{r.name}({r.src}->{r.dst},{r.etype})", "buffer_fill_max", fill, "%")

    # ---- по системе ----
    add("system", "input_generated", m.generated / (m.sim_time / 3600.0), "палет/ч")
    for etype, s in sorted(m._sinks.items()):
        add(f"output:{etype}", "count", s.count, "шт")
        add(f"output:{etype}", "throughput", s.count / (m.sim_time / 3600.0), "шт/ч")
        if s.residence:
            add(f"output:{etype}", "residence_mean", statistics.mean(s.residence), "с")

    return rows


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["object", "metric", "value", "unit"])
        w.writeheader()
        w.writerows(rows)


def write_minute_series(m: SortingCenterModel, path: str) -> None:
    """Ряд производительности по минутам (интервал 1 мин из требований критериев).
    Столбцы: minute, <node1>, <node2>, ... — прирост processed за минуту (шт/мин)."""
    names = [n.name for n in m.nodes.values()]
    series = {n.name: n.proc_series for n in m.nodes.values()}
    length = min((len(v) for v in series.values()), default=0)
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["minute"] + names)
        prev = {name: 0 for name in names}
        for i in range(length):
            row = [i + 1]
            for name in names:
                cur = series[name][i]
                row.append(cur - prev[name])
                prev[name] = cur
            w.writerow(row)


def find_bottleneck(m: SortingCenterModel) -> str:
    """Узкое место — узел, который меньше всех простаивает без дела.

    Критерий: максимальная (работа + блокировка), т.е. минимальное голодание.
    Такой узел либо занят на пределе, либо заперт полным буфером — в обоих случаях
    именно он ограничивает систему, а не ждёт входа от других.
    """
    best, best_load = None, -1.0
    for n in m.nodes.values():
        if n.type == "source":
            continue
        cap = n.workers * m.sim_time
        load = (n.busy + n.blocked) / cap if cap > 0 else 0.0
        if load > best_load:
            best, best_load = n, load
    if not best:
        return "—"
    cap = best.workers * m.sim_time
    return (f"{best.name} (работа {100*best.busy/cap:.1f}% + "
            f"блокировка {100*best.blocked/cap:.1f}%)")

# Интервалы агрегации из критериев задачи: 1 мин / 1 ч / 12 ч / 24 ч
INTERVALS = [("1min", 1), ("1h", 60), ("12h", 720), ("24h", 1440)]


def write_interval_series(m: SortingCenterModel, out_dir: str) -> list[str]:
    """Выгружает производительность на интервалах 1 мин / 1 ч / 12 ч / 24 ч.

    Критерии требуют уметь показывать результаты на этих интервалах. Счётчики
    снимаются раз в модельную минуту, из них складываются интервалы покрупнее.
    Пишутся только те интервалы, которые целиком укладываются в горизонт прогона.

    Столбцы: interval, <узлы...>, output:<тип...> — обработано ЗА интервал, штук.
    """
    import os

    node_names = [n.name for n in m.nodes.values()]
    series = {n.name: n.proc_series for n in m.nodes.values()}
    minutes = min((len(v) for v in series.values()), default=0)
    if minutes == 0:
        return []

    # приросты по минутам: узлы
    node_delta: dict[str, list[int]] = {}
    for name in node_names:
        vals, prev, out = series[name], 0, []
        for i in range(minutes):
            out.append(vals[i] - prev)
            prev = vals[i]
        node_delta[name] = out

    # приросты по минутам: выходы системы
    sink_types = sorted(m._sinks)
    sink_delta: dict[str, list[int]] = {}
    for st in sink_types:
        prev, out = 0, []
        for i in range(minutes):
            cur = m.sink_series[i].get(st, 0) if i < len(m.sink_series) else prev
            out.append(cur - prev)
            prev = cur
        sink_delta[st] = out

    written = []
    for label, size in INTERVALS:
        if minutes < size:
            continue                       # интервал не укладывается в горизонт
        path = os.path.join(out_dir, f"series_{label}.csv")
        with open(path, "w", encoding="utf-8", newline="") as f:
            w = csv.writer(f)
            w.writerow(["interval"] + node_names + [f"output:{t}" for t in sink_types])
            for start in range(0, minutes - size + 1, size):
                row = [f"{label}#{start // size + 1}"]
                row += [sum(node_delta[n][start:start + size]) for n in node_names]
                row += [sum(sink_delta[t][start:start + size]) for t in sink_types]
                w.writerow(row)
        written.append(path)
    return written
