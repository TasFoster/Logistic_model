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


def _node_resources(n: dict) -> tuple[int, list[float]]:
    """Ресурсы узла -> (число параллельных исполнителей, время обработки каждого).

    effecive_ellements[].ef — ПРОИЗВОДИТЕЛЬНОСТЬ единицы ресурса в штуках в час.
    Отсюда время обработки одной сущности этой единицей:  service = 3600 / ef.
    Каждый элемент списка — отдельный параллельный исполнитель со своей скоростью,
    поэтому суммарная пропускная способность узла = sum(ef) шт/ч.

    Узел без ресурсов (например, Storage) считается мгновенным (service = 0):
    он ничего не обрабатывает, а только принимает — иначе давал бы ложное узкое место.
    """
    elems = n.get("effecive_ellements") or []
    rates = [float(e.get("ef", 0)) for e in elems]
    rates = [r for r in rates if r > 0]
    if not rates:
        return 1, [0.0]
    return len(rates), [3600.0 / r for r in rates]


def _travel_time(raw: dict, rib: dict, src_pos: dict, dst_pos: dict) -> float:
    """Время перемещения по ребру, секунд.

    Приоритет — явное поле travel_time_s на ребре. Иначе считается по координатам
    узлов: расстояние между pos (в единицах плана) * unit_m / скорость + время
    погрузки-разгрузки. Блок transport задаётся на уровне графа:

        "transport": {"unit_m": 30, "speed_mps": 1.5, "handling_s": 0}

    Если transport не задан, время в пути = 0 (рёбра мгновенные, как раньше).
    """
    if "travel_time_s" in rib:
        return float(rib["travel_time_s"])
    tr = raw.get("transport") or {}
    unit_m = float(tr.get("unit_m", 0))
    speed = float(tr.get("speed_mps", 1.0))
    handling = float(tr.get("handling_s", 0))
    if not unit_m or not src_pos or not dst_pos or speed <= 0:
        return 0.0
    dx = float(src_pos.get("x", 0)) - float(dst_pos.get("x", 0))
    dy = float(src_pos.get("y", 0)) - float(dst_pos.get("y", 0))
    dist_m = ((dx * dx + dy * dy) ** 0.5) * unit_m
    return dist_m / speed + handling


def _ribs(raw: dict) -> list[dict]:
    pos = {n["id"]: n.get("pos", {}) for n in (raw.get("nodes") or {}).values()}
    out = []
    for name, r in (raw.get("ribs") or {}).items():
        src = r.get("node_in_id", r.get("node_in"))
        dst = r.get("node_out_id", r.get("node_out"))
        out.append({
            "name": name,
            "src": src,                                      # обе версии схемы
            "dst": dst,
            "etype": r.get("type_el", ""),
            "capacity": r.get("storage", 100),
            "travel": _travel_time(raw, r, pos.get(src, {}), pos.get(dst, {})),
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
        workers, services = _node_resources(n)

        # Явное время в узле или в сценарии перекрывает расчёт из производительности.
        # Нужно для ранних графов прототипа (graph_mini/graph_tare) и для сценариев
        # «а что, если узел станет медленнее» (отказ оборудования и т.п.).
        override = n.get("service_time_s")
        if override is None:
            override = times_by_name.get(n["name"], times_by_type.get(ntype))
        if override is None and not (n.get("effecive_ellements") or []):
            override = default_service if ntype not in ("Storage", "source") else None
        if override is not None:
            # при явном времени ef снова означает число параллельных единиц
            elems = n.get("effecive_ellements") or []
            workers = sum(int(e.get("ef", 1)) for e in elems) or 1
            services = [float(override)] * workers

        nodes[n["id"]] = {
            "id": n["id"],
            "name": n["name"],
            "type": ntype,
            "inputs": _node_inputs(n),
            "outputs": _node_outputs(n),
            "workers": workers,
            "services": services,
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
        "directions": raw.get("directions"),
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
