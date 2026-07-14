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


def run_tare(hours: float = 3.0) -> SortingCenterModel:
    graph = load_graph(os.path.join(os.path.dirname(__file__), "graph_tare.json"))
    m = SortingCenterModel(graph, seed=42, warmup_s=300.0)
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


def test_tare_split_80_20():
    """Развилка тары даёт ~80% реюза и ~20% брака от вскрытых пустых КТЯ."""
    m = run_tare()
    split = next(n for n in m.nodes.values() if n.type == "split")
    brak = m._sinks.get("Brak")
    assert brak is not None and split.processed > 0, "нет потока брака тары"
    scrap_share = brak.count / split.processed
    assert 0.17 < scrap_share < 0.23, f"доля брака {scrap_share:.3f} вне ~0.20"


def test_tare_new_kty_covers_deficit():
    """Машина новых КТЯ включается, и баланс тары сходится:
    реюз + новые КТЯ ≈ потреблению упаковки (в пределах 3%)."""
    m = run_tare()
    src = next(n for n in m.nodes.values() if n.type == "source")
    pack = next(n for n in m.nodes.values() if n.type == "pack")
    split = next(n for n in m.nodes.values() if n.type == "split")
    brak = m._sinks.get("Brak")
    assert src.produced > 0, "машина новых КТЯ не произвела ни одной тары"
    reused = split.processed - brak.count
    supply = reused + src.produced
    demand = pack.processed  # 1 пустой КТЯ на 1 полный КТЯ
    assert abs(supply - demand) / max(demand, 1) < 0.03, \
        f"баланс тары не сходится: спрос {demand}, поставка {supply}"


def test_pack_assembly_ratio():
    """Упаковка собирает 27 товаров в 1 КТЯ: выход КТЯ ≈ вход товаров / 27."""
    m = run_tare()
    pack = next(n for n in m.nodes.values() if n.type == "pack")
    kty_out = m._sinks.get("KTY_out")
    assert kty_out is not None and kty_out.count > 0
    # каждое срабатывание упаковки -> 1 полный КТЯ из 27 товаров
    assert abs(pack.processed - kty_out.count) / max(pack.processed, 1) < 0.05


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
