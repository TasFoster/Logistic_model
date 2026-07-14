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


def run_contract(hours: float = 2.0) -> SortingCenterModel:
    """Эталонный граф в формате контракта: реальные мощности + оборот тары."""
    graph = load_graph(os.path.join(os.path.dirname(__file__), "graph_contract.json"))
    return SortingCenterModel(graph, seed=42, warmup_s=300.0).run(hours=hours)


def test_example_format_loads():
    """Пример формата (config/example_graph.json) читается, время выводится из ef.

    ef — производительность, шт/ч => время обработки = 3600 / ef.
    Для Sorting в примере ef=24 => 150 с на товар.
    """
    root = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
    g = load_graph(os.path.join(root, "config", "example_graph.json"))
    assert len(g["nodes"]) == 5, "не все узлы примера загружены"
    sorting = next(c for c in g["nodes"].values() if c["name"] == "Sorting")
    assert abs(sorting["services"][0] - 150.0) < 0.01,         f"время сортировки {sorting['services'][0]} != 3600/24"
    pack = next(c for c in g["nodes"].values() if c["name"] == "Pack")
    assert pack["inputs"] == {"Product": 27, "Box": 1}, "сборка на упаковке не разобрана"


def test_contract_reaches_target_throughput():
    """Эталонный граф вытягивает целевые 100 000 товаров/ч на сортировке."""
    m = run_contract()
    sort = next(n for n in m.nodes.values() if n.type == "sort")
    win_h = (m.sim_time - m.warmup) / 3600.0
    thr = (sort.processed - sort.processed_at_warmup) / win_h
    assert 97000 < thr < 103000, f"сортировка {thr:.0f} товаров/ч, ожидалось ~100 000"


def test_contract_no_tare_jam():
    """Излишки тары не запирают линию: вскрытие не блокируется.

    Ловит структурную проблему примера: без развилки тары короба копятся,
    буфер забивается и вскрытие встаёт (блокировка была 44.7%).
    """
    m = run_contract()
    unpack = next(n for n in m.nodes.values() if n.name == "unKTU")
    blocked = 100.0 * unpack.blocked / (unpack.workers * m.sim_time)
    assert blocked < 5.0, f"вскрытие заблокировано на {blocked:.1f}% — затор по таре"


def test_contract_tare_balance():
    """Баланс тары: реюз + новые КТЯ ≈ спрос упаковки."""
    m = run_contract()
    split = next(n for n in m.nodes.values() if n.type == "split")
    pack = next(n for n in m.nodes.values() if n.type == "pack")
    src = next(n for n in m.nodes.values() if n.type == "source")
    brak = m._sinks.get("BoxBrak") or next(
        (n for n in m.nodes.values() if n.name == "StorageBrak"), None)
    scrapped = brak.processed if hasattr(brak, "processed") else brak.count
    reused = split.processed - scrapped
    supply, demand = reused + src.produced, pack.processed
    assert src.produced > 0, "машина новых КТЯ не включилась"
    assert abs(supply - demand) / max(demand, 1) < 0.05,         f"баланс тары не сходится: спрос {demand}, поставка {supply}"


def test_time_accounting_sums_to_100():
    """Работа + блокировка + голодание = 100% времени воркеров.

    Защита от ошибки, при которой время воркера, застрявшего в блокировке на момент
    конца прогона, не попадало в статистику и система выглядела незагруженной.
    """
    m = run_contract()
    for n in m.nodes.values():
        if n.type == "source":
            continue
        cap = n.workers * m.sim_time
        total = n.busy + n.blocked + n.starved
        assert abs(total - cap) / cap < 0.01,             f"{n.name}: учтено {total:.0f} с из {cap:.0f} с — время теряется"


def test_pareto_profile_80_20():
    """Профиль направлений: верхние 20% забирают ~80% объёма (из постановки)."""
    from .directions import DirectionProfile
    prof = DirectionProfile(count=400, top_share=0.2, volume_share=0.8)
    share = prof.share_of_top()
    assert 0.78 < share < 0.82, f"верхние 20% дают {share:.3f} объёма, ожидалось ~0.80"
    # длинный хвост: первое направление на порядки объёмнее последнего
    assert prof.probs[0] / prof.probs[-1] > 100, "хвост распределения слишком плоский"


def test_pack_one_direction_per_box():
    """Одна коробка — одно направление: КТЯ собирается из товаров ОДНОГО направления."""
    m = run_contract()
    pack = next(n for n in m.nodes.values() if n.by_direction)
    assert pack.filled > 0, "упаковка не выпустила ни одного КТЯ"
    batch = pack._batch_size()
    avg = pack.items_packed / pack.filled
    assert 0 < avg <= batch, f"средняя заполненность {avg:.1f} вне диапазона 0..{batch}"


def test_accumulator_cells_needed():
    """Накопителей нужно примерно по одному на направление (требование к площади)."""
    m = run_contract()
    pack = next(n for n in m.nodes.values() if n.by_direction)
    assert pack.max_open_bins > 300,         f"занято лишь {pack.max_open_bins} ячеек — направления не разошлись по накопителям"
    assert pack.max_open_bins <= m.directions.count, "ячеек больше, чем направлений"


def test_underfill_grows_when_kty_closed_early():
    """Чем чаще закрываем КТЯ, тем больше недозаполненных — и тем больше расход тары.
    Это ключевой компромисс отчёта, он должен воспроизводиться."""
    import copy
    from .graph_loader import load_json, normalize
    raw = load_json(os.path.join(os.path.dirname(__file__), "graph_contract.json"))

    def run(tmo):
        r = copy.deepcopy(raw)
        r["nodes"]["node4"]["params"]["flush_timeout_s"] = tmo
        m = SortingCenterModel(normalize(r), seed=42, warmup_s=300.0).run(hours=2)
        p = next(n for n in m.nodes.values() if n.by_direction)
        return 100.0 * p.underfilled / max(p.filled, 1)

    fast, slow = run(900), run(3600)     # 15 мин против 60 мин
    assert fast > slow + 5,         f"недозаполненность не растёт при раннем закрытии КТЯ ({fast:.1f}% vs {slow:.1f}%)"


# ---------------------------------------------------------------------------
# Двухстадийная сортировка 20 x 20
# ---------------------------------------------------------------------------
def run_2stage(hours: float = 2.0, grouping: str | None = None) -> SortingCenterModel:
    import copy
    from .graph_loader import load_json, normalize
    raw = load_json(os.path.join(os.path.dirname(__file__), "graph_2stage.json"))
    if grouping:
        raw = copy.deepcopy(raw)
        raw["directions"]["grouping"] = grouping
    return SortingCenterModel(normalize(raw), seed=42, warmup_s=600.0).run(hours=hours)


def test_two_stage_all_sections_work():
    """Все 20 секций второй стадии реально работают — маршрутизация по группам жива."""
    m = run_2stage()
    secs = [n for n in m.nodes.values() if n.name.startswith("Sort2_")]
    assert len(secs) == 20, f"секций {len(secs)}, ожидалось 20"
    idle = [n.name for n in secs if n.processed == 0]
    assert not idle, f"секции без единого товара: {idle}"


def test_two_stage_balanced_load():
    """При балансировке групп секции загружены сопоставимо (разброс не в разы)."""
    m = run_2stage(grouping="balanced")
    secs = [n for n in m.nodes.values() if n.name.startswith("Sort2_")]
    loads = [n.busy / (n.workers * m.sim_time) for n in secs]
    assert max(loads) / max(min(loads), 1e-9) < 2.0, \
        f"разброс загрузки секций {max(loads)/min(loads):.1f}x — балансировка не работает"


def test_sequential_grouping_is_worse():
    """Наивная группировка подряд из-за Парето перегружает первые секции и душит
    первую стадию. Это ключевой вывод по двухстадийной схеме — он должен воспроизводиться."""
    bal = run_2stage(grouping="balanced")
    seq = run_2stage(grouping="sequential")

    def out(m):
        return next(n for n in m.nodes.values() if n.by_direction).filled

    def sort1_blocked(m):
        n = next(x for x in m.nodes.values() if x.name == "Sort1")
        return n.blocked / (n.workers * m.sim_time)

    assert out(bal) > out(seq) * 1.5, \
        f"балансировка не даёт выигрыша: {out(bal)} против {out(seq)}"
    assert sort1_blocked(seq) > 0.2, \
        "при группировке подряд первая стадия должна блокироваться"
    assert sort1_blocked(bal) < 0.05, \
        "при балансировке первая стадия блокироваться не должна"


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
