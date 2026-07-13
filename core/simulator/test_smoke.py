"""
Дымовой тест имитационной модели — проверяет, что прототип считает физически
согласованно. Запуск без pytest:

    python -m core.simulator.test_smoke

Проверяются:
  1. Модель отрабатывает горизонт без исключений.
  2. Узкое место определяется как Сортировка (по замыслу мини-графа).
  3. Баланс сортировки: доля nonsort ~ заданной (5%).
  4. Оборот тары: пустых КТЯ ~ столько же, сколько вскрыто КТЯ (1 к 1).
  5. Буфер перед узким местом переполняется (признак блокировки).
"""

from __future__ import annotations

import os

from .model import SortingCenterModel, load_graph
from . import metrics


def run_model(hours: float = 0.5) -> SortingCenterModel:
    graph = load_graph(os.path.join(os.path.dirname(__file__), "graph_mini.json"))
    m = SortingCenterModel(graph, seed=42, warmup_s=120.0)
    return m.run(hours=hours)


def test_runs_and_produces_output():
    m = run_model()
    assert m.generated > 0, "генератор не подал ни одной палеты"
    rows = metrics.collect_rows(m)
    assert rows, "метрики не собраны"


def test_bottleneck_is_sorting():
    m = run_model()
    assert "Sortirovka" in metrics.find_bottleneck(m), "узкое место должно быть Сортировка"


def test_nonsort_share():
    m = run_model()
    sort = next(n for n in m.nodes.values() if n.type == "sort")
    proc = sort.processed
    nonsort = m._sinks.get("Nonsort")
    assert nonsort is not None, "нет потока nonsort"
    share = nonsort.count / proc if proc else 0
    assert 0.03 < share < 0.07, f"доля nonsort {share:.3f} вне ожидаемого ~0.05"


def test_tare_balance():
    m = run_model()
    unpack = next(n for n in m.nodes.values() if n.name == "Vskrytie_KTY")
    empty = m._sinks.get("EmptyKTY")
    assert empty is not None, "нет потока пустой тары"
    # на 1 вскрытый КТЯ -> 1 пустой КТЯ; допускаем расхождение из-за незавершённых в буферах
    assert abs(empty.count - unpack.processed) / max(unpack.processed, 1) < 0.05


def test_buffer_before_bottleneck_fills():
    m = run_model()
    rib2 = next(r for r in m.ribs if r.dst == 3)  # ребро в сортировку
    assert max(rib2.level_samples) >= rib2.capacity * 0.9, \
        "буфер перед узким местом не переполняется — блокировка не воспроизводится"


def _main():
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  OK   {t.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {t.__name__}: {e}")
    print("-" * 50)
    print(f"Пройдено: {len(tests) - failed}/{len(tests)}")
    return failed


if __name__ == "__main__":
    raise SystemExit(1 if _main() else 0)
