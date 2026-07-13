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
    for n in m.nodes.values():
        proc = n.processed - n.processed_at_warmup          # обработано за окно
        busy = n.busy - n.busy_at_warmup                    # занятость за окно
        throughput = proc / win_h                           # шт/ч
        capacity_time = n.workers * (m.sim_time - m.warmup)  # доступное время всех воркеров
        utilization = 100.0 * busy / capacity_time if capacity_time > 0 else 0.0
        blocked = 100.0 * n.blocked / capacity_time if capacity_time > 0 else 0.0

        add(n.name, "throughput", throughput, "шт/ч")
        add(n.name, "utilization", utilization, "%")
        add(n.name, "blocked", blocked, "%")
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
    """Узел с максимальной загрузкой — узкое место системы."""
    win = m.sim_time - m.warmup
    best, best_u = None, -1.0
    for n in m.nodes.values():
        busy = n.busy - n.busy_at_warmup
        cap = n.workers * win
        u = busy / cap if cap > 0 else 0.0
        if u > best_u:
            best, best_u = n, u
    return f"{best.name} (загрузка {100 * best_u:.1f}%)" if best else "—"
