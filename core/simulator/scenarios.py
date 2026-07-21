"""
Сценарии моделирования: стресс-тесты одной и той же конфигурации центра.

Идея (Концепция, раздел 5): граф — это КОНФИГУРАЦИЯ центра, а сценарий — УСЛОВИЯ,
в которых он работает. Один граф гоняется под разными условиями, и сравнение
показывает, где система ломается и с каким запасом она спроектирована.

Что умеет сценарий:
    input_stream_factor  — множитель входного потока (пиковая нагрузка);
    nonsort_share        — доля товаров, не подлежащих автосортировке;
    capacity_factor      — {имя узла: множитель производительности} (деградация);
    outages              — отказы: [{"node": имя, "start_h": ч, "duration_h": ч}].

Запуск сравнения всех сценариев:
    python -m core.simulator.scenarios --graph core/simulator/graph_2stage.json --hours 4
"""

from __future__ import annotations

import argparse
import copy
import os
import sys

from .graph_loader import load_json, normalize
from .model import SortingCenterModel

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


# ---------------------------------------------------------------------------
# Набор сценариев для отчёта
# ---------------------------------------------------------------------------
SCENARIOS: list[dict] = [
    {
        "name": "Номинал",
        "description": "Проектный режим: 100 000 товаров/ч, nonsort 5%",
    },
    {
        "name": "Пик +50%",
        "description": "Пиковая нагрузка: входной поток 150 000 товаров/ч",
        "input_stream_factor": 1.5,
    },
    {
        "name": "Пик +100%",
        "description": "Двойная нагрузка: 200 000 товаров/ч — ищем предел",
        "input_stream_factor": 2.0,
    },
    {
        "name": "Рост nonsort 15%",
        "description": "Втрое больше товаров не проходят автосортировку",
        "nonsort_share": 0.15,
    },
    {
        "name": "Отказ секции",
        "description": "Самая нагруженная секция 2-й стадии встала на 1 час",
        "outages": [{"node": "Sort2_g00", "start_h": 1.0, "duration_h": 1.0}],
    },
    {
        "name": "Отказ вскрытия",
        "description": "Узел вскрытия КТЯ встал на 30 минут",
        "outages": [{"node": "unKTU", "start_h": 1.0, "duration_h": 0.5}],
    },
]


# ---------------------------------------------------------------------------
def apply_scenario(raw: dict, sc: dict) -> tuple[dict, list[dict]]:
    """Накладывает сценарий на сырой граф. Возвращает (граф, отказы)."""
    g = copy.deepcopy(raw)

    factor = sc.get("input_stream_factor")
    if factor:
        g["input_stream"] = float(g.get("input_stream", 100000)) * float(factor)

    share = sc.get("nonsort_share")
    if share is not None:
        for n in g.get("nodes", {}).values():
            if n.get("type_node") == "sort":
                out = n.get("output") or {}
                goods = next((t for t in out if t != "Nonsort"), "Product")
                n["output"] = {goods: 1.0 - float(share), "Nonsort": float(share)}

    for name, mult in (sc.get("capacity_factor") or {}).items():
        for n in g.get("nodes", {}).values():
            if n.get("name") == name:
                for e in n.get("effecive_ellements") or []:
                    e["ef"] = float(e.get("ef", 0)) * float(mult)

    return g, sc.get("outages") or []


def run_scenario(raw: dict, sc: dict, hours: float, seed: int,
                 warmup_s: float) -> dict:
    """Прогоняет один сценарий и возвращает сводные показатели."""
    g, outages = apply_scenario(raw, sc)
    m = SortingCenterModel(normalize(g), seed=seed, warmup_s=warmup_s,
                           outages=outages).run(hours=hours)
    h = m.sim_time / 3600.0

    pack = next((n for n in m.nodes.values() if n.by_direction), None)
    if pack is None:
        pack = next((n for n in m.nodes.values() if n.type == "pack"), None)

    out_kty = pack.filled if pack and pack.filled else pack.processed if pack else 0

    # узкое место: максимум (работа + блокировка)
    def load(n):
        cap = n.workers * m.sim_time
        return (n.busy + n.blocked) / cap if cap else 0.0

    workers = [n for n in m.nodes.values() if n.type != "source"]
    bottleneck = max(workers, key=load) if workers else None

    # средняя длина очередей в буферах (признак затора)
    uniq = {}
    for r in m.ribs:
        uniq.setdefault(id(r.store), r)
    fills = []
    for r in uniq.values():
        cap = r.store.capacity if r.store is not None else r.capacity
        if r.level_samples and cap:
            fills.append(100.0 * max(r.level_samples) / cap)

    # время пребывания товара в центре — до терминального стока (что отгружаем)
    term = m._sinks.get(m.output_type) if m.output_type else None
    if term is None:
        term = m._sinks.get("KTY_full") or m._sinks.get("KTY_out")
    if term is None and m._sinks:
        term = max(m._sinks.values(), key=lambda s: s.count)
    import statistics
    resid = statistics.mean(term.residence) / 60.0 if (
        term and term.residence) else 0.0

    return {
        "name": sc["name"],
        "in_per_h": m.generated / h * 540,          # товаров/ч на входе (палета=540 тов)
        "out_kty_per_h": out_kty / h,
        "out_items_per_h": (pack.items_packed / h) if pack else 0.0,
        "bottleneck": bottleneck.name if bottleneck else "—",
        "bottleneck_load": 100.0 * load(bottleneck) if bottleneck else 0.0,
        "max_buffer_fill": max(fills) if fills else 0.0,
        "residence_min": resid,
        "underfilled": (100.0 * pack.underfilled / pack.filled) if (
            pack and pack.filled) else 0.0,
    }


def main() -> None:
    here = os.path.dirname(__file__)
    ap = argparse.ArgumentParser(description="Сравнение сценариев работы центра")
    ap.add_argument("--graph", default=os.path.join(here, "graph_2stage.json"))
    ap.add_argument("--hours", type=float, default=4.0)
    ap.add_argument("--warmup", type=float, default=600.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="results_scenarios")
    args = ap.parse_args()

    raw = load_json(args.graph)
    print(f"Граф: {args.graph} | горизонт: {args.hours} ч | seed: {args.seed}\n")

    rows = []
    for sc in SCENARIOS:
        print(f"  считаю: {sc['name']} — {sc['description']}")
        rows.append(run_scenario(raw, sc, args.hours, args.seed, args.warmup))

    print("\n" + "=" * 100)
    print(f"{'сценарий':<20}{'вход тов/ч':>12}{'выход КТЯ/ч':>13}"
          f"{'узкое место':>18}{'загр.':>8}{'буфер':>8}{'в центре':>10}")
    print("-" * 100)
    base = rows[0]["out_kty_per_h"] or 1.0
    for r in rows:
        print(f"{r['name']:<20}{r['in_per_h']:>12,.0f}{r['out_kty_per_h']:>13,.0f}"
              f"{r['bottleneck']:>18}{r['bottleneck_load']:>7.0f}%"
              f"{r['max_buffer_fill']:>7.0f}%{r['residence_min']:>9.1f}м")
    print("-" * 100)
    for r in rows[1:]:
        delta = 100.0 * (r["out_kty_per_h"] - base) / base
        print(f"{r['name']:<20} выход к номиналу: {delta:+.1f}%")

    # выгрузка для отчёта
    os.makedirs(args.out, exist_ok=True)
    import csv
    path = os.path.join(args.out, "scenarios.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"\nCSV: {os.path.abspath(path)}")


if __name__ == "__main__":
    main()
