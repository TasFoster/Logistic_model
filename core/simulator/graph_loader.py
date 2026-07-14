"""
Загрузчик и нормализатор графа.

Приводит любой из вариантов файла графа к единому внутреннему виду, чтобы ядро
симуляции не зависело от версии схемы:

  A) Схема контракта (config/example_graph.json, Станислав):
       узел: input {тип: кол-во}, output {тип: кол-во или доля}, effecive_ellements
       ребро: node_in_id / node_out_id
       граф: input_stream, type_input, start_node_id
  B) Ранняя схема прототипа (graph_mini.json, graph_tare.json):
       узел: type_input / type_output / transform_kof / assembly / params
       ребро: node_in / node_out

Ключевой момент: в схеме контракта НЕТ времени обработки узла. Аналитической
модели оно не нужно (она считает интенсивности «штук в час»), а имитационной —
необходимо: без времени не из чего строить события. Поэтому времена задаются
ОТДЕЛЬНЫМ слоем-сценарием (scenario_*.json), чтобы не менять общий контракт.
Это же согласуется с правилом «интенсивность входа — свойство сценария, а не графа».

Канонический вид узла:
    {id, name, type, inputs {тип: кол-во}, outputs {тип: кол-во|доля},
     workers, service, params, pos}
"""

from __future__ import annotations

import json


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _node_inputs(n: dict) -> dict[str, float]:
    """Входы узла: {тип сущности: сколько нужно на одно срабатывание}."""
    if "input" in n:                       # схема контракта
        return {k: v for k, v in (n["input"] or {}).items()}
    # ранняя схема: type_input + assembly
    in_types = list((n.get("type_input") or {}).values())
    asm = n.get("assembly", {})
    return {t: asm.get(t, 1) for t in in_types}


def _node_outputs(n: dict) -> dict[str, float]:
    """Выходы узла: {тип: количество (>=1) или доля (<1)}.

    Доли (<1) означают вероятностную развилку — узел порождает ОДНУ сущность,
    выбранную по долям (сортировка: 0.95 Product / 0.05 Nonsort).
    Количества (>=1) — детерминированное размножение (вскрытие: 27 Product + 1 Box).
    """
    if "output" in n:                      # схема контракта
        return {k: v for k, v in (n["output"] or {}).items()}

    # ранняя схема: type_output + transform_kof, доли жили в params
    params = n.get("params", {})
    outs = n.get("type_output", []) or []
    kof = n.get("transform_kof", []) or []

    if n.get("type_node") == "sort" and "nonsort_share" in params:
        share = float(params["nonsort_share"])
        main = outs[0] if outs else "Product"
        return {main: 1.0 - share, "Nonsort": share}

    if n.get("type_node") == "split" and "routes" in params:
        return {r["type"]: float(r["share"]) for r in params["routes"]}

    return {t: (kof[i] if i < len(kof) else 1) for i, t in enumerate(outs)}


def _node_workers(n: dict) -> int:
    """Число параллельных исполнителей узла.

    ДОПУЩЕНИЕ: effecive_ellements[].ef трактуется как КОЛИЧЕСТВО единиц ресурса
    (людей/машин), а не как производительность. Это неоднозначность контракта —
    вынесена на согласование (см. README, 'Пробелы схемы').
    """
    elems = n.get("effecive_ellements") or []
    total = sum(int(e.get("ef", 1)) for e in elems)
    return total or 1


def _ribs(raw: dict) -> list[dict]:
    out = []
    for name, r in (raw.get("ribs") or {}).items():
        out.append({
            "name": name,
            "src": r.get("node_in_id", r.get("node_in")),    # обе версии схемы
            "dst": r.get("node_out_id", r.get("node_out")),
            "etype": r.get("type_el", ""),
            "capacity": r.get("storage", 100),
        })
    return out


def normalize(raw: dict, scenario: dict | None = None) -> dict:
    """Приводит сырой граф к каноническому виду, накладывая слой-сценарий."""
    scenario = scenario or {}
    times_by_name = (scenario.get("service_time_s") or {}).get("by_name", {})
    times_by_type = (scenario.get("service_time_s") or {}).get("by_type", {})
    default_service = scenario.get("default_service_s", 30.0)

    nodes: dict[int, dict] = {}
    for n in (raw.get("nodes") or {}).values():
        ntype = n.get("type_node", "transform")
        service = n.get("service_time_s")           # если время всё же есть в графе
        if service is None:
            service = times_by_name.get(n["name"], times_by_type.get(ntype, default_service))
        nodes[n["id"]] = {
            "id": n["id"],
            "name": n["name"],
            "type": ntype,
            "inputs": _node_inputs(n),
            "outputs": _node_outputs(n),
            "workers": _node_workers(n),
            "service": float(service),
            "params": n.get("params", {}),
            "pos": n.get("pos", {}),
        }

    # интенсивность входа: приоритет у сценария (это свойство сценария, не графа)
    arrival = scenario.get("arrival_rate_per_h")
    if arrival is None:
        # запасной путь: input_stream (товаров/ч) -> палет/ч через цепочку 1 палета
        # -> 20 КТЯ -> 27 товаров. Коэффициент считается по самому графу, если можно.
        arrival = float(raw.get("input_stream", 100000)) / _items_per_start_unit(raw, nodes)

    return {
        "nodes": nodes,
        "ribs": _ribs(raw),
        "arrival_rate_h": float(arrival),
        "start_node_id": raw.get("start_node_id"),
        "input_type": raw.get("type_input"),
        "input_stream": raw.get("input_stream", 100000),
    }


def _items_per_start_unit(raw: dict, nodes: dict[int, dict]) -> float:
    """Сколько товаров получается из одной стартовой единицы (палеты).

    Идёт по цепочке от стартового узла, перемножая детерминированные коэффициенты
    до появления товарной сущности. Если определить не удалось — 20*27=540 (по задаче).
    """
    start_id = raw.get("start_node_id")
    if start_id is None or start_id not in nodes:
        return 20.0 * 27.0

    # перемножаем количества по цепочке, пока не встретим сущность-товар
    factor = 1.0
    current = nodes[start_id]
    seen = set()
    while current and current["id"] not in seen:
        seen.add(current["id"])
        det = {t: q for t, q in current["outputs"].items() if q >= 1}
        if not det:
            break
        # берём выход с наибольшим количеством — это «основной» поток
        etype, qty = max(det.items(), key=lambda kv: kv[1])
        factor *= float(qty)
        if etype.lower().startswith("product") or etype.lower().startswith("tovar"):
            return factor
        nxt = _next_node(raw, nodes, current["id"], etype)
        current = nxt
    return factor if factor > 1 else 20.0 * 27.0


def _next_node(raw: dict, nodes: dict[int, dict], src_id: int, etype: str) -> dict | None:
    for r in _ribs(raw):
        if r["src"] == src_id and r["etype"] == etype:
            return nodes.get(r["dst"])
    return None
