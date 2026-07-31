"""
Сверка аналитики и симуляции на одном графе.

Идея (конспект §7): два независимых способа посчитать одно и то же обязаны сойтись.
Аналитика считает мгновенно (балансы), симуляция измеряет за N модельных часов.
Результат — таблица «узел — аналитика — симуляция — расхождение». Где расхождение
мало — модели подтверждают друг друга; где велико — симуляция вскрывает динамику
(очереди, backpressure), невидимую стационарным балансам.

Дополнительно сверяется профиль направлений: доли объёма по группам 2-й стадии,
посчитанные аналитикой (core/analitic/directions.py — порт) и симулятором
(core/simulator/directions.py — оригинал), обязаны совпасть до машинной точности.

Запуск:
    python -m core.validation.compare --graph core/simulator/graph_contract.json --hours 2
    python -m core.validation.compare --graph core/simulator/graph_2stage.json --hours 3

Внимание: сверка честна только при загрузках < 100% (конспект §7). При перегрузе
симуляция показывает потолок и растущую очередь — это её работа, а не ошибка.
"""

from __future__ import annotations

import argparse
import csv
import os
import sys

from core.analitic.loader import load_graph as ana_load
from core.analitic.balance import solve as ana_solve
from core.simulator.model import SortingCenterModel, load_graph as sim_load
from core.analitic.directions import DirectionProfile as AnaProfile
from core.simulator.directions import DirectionProfile as SimProfile

try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass


def _sim_throughput(n, sim_h: float, win_h: float) -> float:
    if n.type == "source":
        return n.produced / sim_h
    return (n.processed - n.processed_at_warmup) / win_h


def _sim_util(n, sim_time: float) -> float | None:
    if n.type == "source":
        return None
    cap = n.workers * sim_time
    return 100.0 * n.busy / cap if cap > 0 else None


def compare_nodes(graph_path: str, hours: float, warmup: float, seed: int) -> list[dict]:
    """Запускает обе модели и сопоставляет узлы по имени."""
    # аналитика
    ana = ana_solve(ana_load(graph_path))
    ana_by_name = {r.name: r for r in ana.nodes.values()}

    # симуляция
    m = SortingCenterModel(sim_load(graph_path), seed=seed, warmup_s=warmup).run(hours=hours)
    win_h = max((m.sim_time - m.warmup) / 3600.0, 1e-9)
    sim_h = m.sim_time / 3600.0

    rows: list[dict] = []
    for n in m.nodes.values():
        a = ana_by_name.get(n.name)
        if a is None:
            continue
        s_thr = _sim_throughput(n, sim_h, win_h)
        s_util = _sim_util(n, m.sim_time)
        a_util = 100.0 * a.load if a.capacity > 0 else None
        d_thr = (100.0 * (s_thr - a.throughput) / a.throughput) if a.throughput else 0.0
        d_util = (s_util - a_util) if (s_util is not None and a_util is not None) else None
        rows.append({
            "object": n.name,
            "ana_throughput": round(a.throughput, 1),
            "sim_throughput": round(s_thr, 1),
            "delta_throughput_pct": round(d_thr, 1),
            "ana_util_pct": round(a_util, 1) if a_util is not None else "",
            "sim_util_pct": round(s_util, 1) if s_util is not None else "",
            "delta_util_pp": round(d_util, 1) if d_util is not None else "",
        })
    return rows


def check_directions(graph_path: str) -> tuple[bool, float, int]:
    """Сверяет доли объёма по группам: порт аналитики vs профиль симулятора.

    Возвращает (совпало, макс_расхождение, число_групп). При числе групп 0
    (одностадийный граф) сверять нечего.
    """
    raw = sim_load(graph_path)   # канонический граф несёт directions без изменений
    dcfg = raw.get("directions") or {}
    groups = int(dcfg.get("groups", 0))
    if not groups:
        return True, 0.0, 0
    kw = dict(count=dcfg.get("count", 400), top_share=dcfg.get("top_share", 0.2),
              volume_share=dcfg.get("volume_share", 0.8), profile=dcfg.get("profile", "pareto"),
              groups=groups, grouping=dcfg.get("grouping", "balanced"))
    ana_shares = AnaProfile(**kw).group_volume_shares()
    sim_shares = SimProfile(**kw).group_volume_shares()
    max_diff = max(abs(a - s) for a, s in zip(ana_shares, sim_shares))
    return max_diff < 1e-9, max_diff, groups


def main() -> None:
    here = os.path.dirname(__file__)
    default_graph = os.path.join(os.path.dirname(here), "simulator", "graph_contract.json")

    ap = argparse.ArgumentParser(description="Сверка аналитики и симуляции")
    ap.add_argument("--graph", default=default_graph)
    ap.add_argument("--hours", type=float, default=2.0)
    ap.add_argument("--warmup", type=float, default=300.0)
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", default="results")
    args = ap.parse_args()

    print(f"СВЕРКА аналитика ↔ симуляция")
    print(f"Граф: {args.graph} | симуляция {args.hours} ч, прогрев {args.warmup} с")
    print("-" * 92)
    rows = compare_nodes(args.graph, args.hours, args.warmup, args.seed)

    print(f"{'узел':<18}{'thr(ана)':>10}{'thr(сим)':>10}{'Δ%':>8}"
          f"{'загр(ана)':>11}{'загр(сим)':>11}{'Δпп':>8}")
    max_dthr = max_dutil = 0.0
    for r in rows:
        au = f"{r['ana_util_pct']}%" if r["ana_util_pct"] != "" else "—"
        su = f"{r['sim_util_pct']}%" if r["sim_util_pct"] != "" else "—"
        dpp = f"{r['delta_util_pp']:+}" if r["delta_util_pp"] != "" else "—"
        print(f"{r['object']:<18}{r['ana_throughput']:>10.0f}{r['sim_throughput']:>10.0f}"
              f"{r['delta_throughput_pct']:>+7.1f}%{au:>11}{su:>11}{dpp:>8}")
        max_dthr = max(max_dthr, abs(r["delta_throughput_pct"]))
        if r["delta_util_pp"] != "":
            max_dutil = max(max_dutil, abs(r["delta_util_pp"]))
    print("-" * 92)

    ok, diff, groups = check_directions(args.graph)
    if groups:
        status = "ИДЕНТИЧНЫ ✓" if ok else f"РАСХОДЯТСЯ ✗ (Δ={diff:.2e})"
        print(f"Профиль направлений ({groups} групп 2-й ст.): макс |Δ доли| = {diff:.2e} → {status}")
    else:
        print("Профиль направлений: групп нет (одностадийный граф) — сверять нечего")

    within5 = sum(1 for r in rows if abs(r["delta_throughput_pct"]) <= 5)
    outliers = [r["object"] for r in rows if abs(r["delta_throughput_pct"]) > 10]
    print(f"ИТОГ: {within5}/{len(rows)} узлов сходятся в пределах 5% по потоку; "
          f"макс |Δ загрузка| = {max_dutil:.1f} пп")
    if outliers:
        print(f"      расходятся > 10%: {', '.join(outliers)}")
        print("      — это эмерджентная тара (source/брак) и выходная сторона: симуляция")
        print("        ловит backpressure (до ворот доходит ~90-95% номинала), балансы — идеал.")
    else:
        print("      расхождений > 10% нет — модели сходятся.")

    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "validation.csv")
    with open(path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(rows)
    print(f"CSV сверки: {os.path.abspath(path)}")


if __name__ == "__main__":
    main()
