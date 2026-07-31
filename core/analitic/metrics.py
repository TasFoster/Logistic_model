"""
Выгрузка метрик аналитической модели.

Формат — общий контракт сверки (конспект наставника, §6, контракт 3):
CSV со столбцами `object, metric, value, unit`. Тот же формат, что у симулятора
(core/simulator/metrics.py), поэтому сверка двух моделей = построчный diff.

Пересечение с метриками симулятора для сверки: по узлам `throughput` (шт/ч) и
`utilization` (%). Аналитика добавляет свои величины (мощность, размерение,
потоки по рёбрам, предельный вход), которых у симуляции нет, — они не мешают
сверке, но обогащают отчёт.
"""

from __future__ import annotations

import csv


def build_rows(graph: dict, balance, sizing: dict, facility=None) -> list[dict]:
    """Собирает метрики в список строк {object, metric, value, unit}.

    facility (опционально) — результат facility.compute(): добавляет по узлам
    операторов/машины/площадь и системные итоги по штату, парку и площади.
    """
    rows: list[dict] = []

    def add(obj, metric, value, unit):
        rows.append({
            "object": obj,
            "metric": metric,
            "value": round(value, 2) if isinstance(value, float) else value,
            "unit": unit,
        })

    # ---- по узлам ----
    for nid, r in balance.nodes.items():
        add(r.name, "throughput", r.throughput, "шт/ч")
        if r.capacity > 0:
            add(r.name, "capacity", r.capacity, "шт/ч")
            add(r.name, "utilization", 100.0 * r.load, "%")
            s = sizing[nid]
            add(r.name, "units_have", s["have"], "ед")
            add(r.name, "units_needed", s["needed"], "ед")
        if facility is not None:
            f = facility.per_node[nid]
            add(r.name, "operators", f["operators"], "чел")
            add(r.name, "area", f["area"], "м²")

    # ---- по рёбрам (интенсивность потока) ----
    rib_by_name = {rr["name"]: rr for rr in graph["ribs"]}
    for name, flow in balance.rib_flow.items():
        rr = rib_by_name[name]
        add(f"{name}({rr['src']}->{rr['dst']},{rr['etype']})", "flow", flow, "шт/ч")

    # ---- по системе ----
    add("system", "input", balance.arrival_rate_h, "палет/ч")
    add("system", "input_items", float(balance.input_stream), "товаров/ч")
    add("system", "max_input", balance.max_input_stream, "товаров/ч")
    bn = balance.bottleneck()
    if bn is not None:
        add("system", "bottleneck", bn.name, "")
        add("system", "bottleneck_load", 100.0 * bn.load, "%")

    # выходы системы: потоки, которые узел порождает, но никакое ребро не уносит.
    # Узлы source исключаем: их выработка не покидает систему, а впрыскивается в
    # целевое ребро (тара идёт в упаковку) и уже учтена в балансе.
    exits: dict[str, float] = {}
    for nid, r in balance.nodes.items():
        if graph["nodes"][nid]["type"] == "source":
            continue
        for t, f in r.outflow.items():
            has_rib = any(rr["src"] == nid and rr["etype"] == t for rr in graph["ribs"])
            if not has_rib and f > 0:
                exits[t] = exits.get(t, 0.0) + f
    for t, f in sorted(exits.items()):
        add(f"output:{t}", "throughput", f, "шт/ч")

    # ---- штат / парк / площадь ----
    if facility is not None:
        add("system", "staff_total", facility.total_staff, "чел")
        add("system", "area_total", facility.total_area_m2, "м²")
        add("system", "area_equipment", facility.equipment_area_m2, "м²")
        add("system", "area_buffers", facility.buffer_area_m2, "м²")
        add("system", "area_accumulators", facility.accumulator_area_m2, "м²")
        for kind, cnt in sorted(facility.park.items()):
            add(f"park:{kind}", "count", cnt, "ед")

    return rows


def write_csv(rows: list[dict], path: str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["object", "metric", "value", "unit"])
        w.writeheader()
        w.writerows(rows)
