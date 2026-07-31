"""
Загрузчик графа для аналитической модели.

Читает ТОТ ЖЕ файл графа, что и симулятор, и приводит его к каноническому виду —
но БЕЗ зависимости от SimPy, чтобы аналитику можно было запускать на голом
Python (условие: проверка на изолированном сервере, минимум зависимостей).

ВАЖНО — контракт совместимости (см. 'Процесс разработки.md', §1.5):
трактовка полей ОБЯЗАНА совпадать с core/simulator/graph_loader.normalize, иначе
сверка двух моделей бессмысленна. Основные правила:

  effecive_ellements[].ef  — производительность единицы ресурса, шт/ч = СРАБАТЫВАНИЙ/ч;
                             мощность узла = Σ ef.
  output {тип: N>=1}       — детерминированное размножение (1 КТЯ -> 27 товаров + 1 короб).
  output {тип: доля<1}     — вероятностная развилка (сортировка 0.95/0.05).
  input  {тип: N}          — сборка: сколько нужно на одно срабатывание.
  узел без ef              — мгновенный (Storage): мощность считаем бесконечной.
  type_node: input_list    — алиас точки входа (Input).
  узлы/рёбра списком, *_list — актуальная схема контракта, читаем наравне со словарной.

TODO(Р2): после согласования с напарником объединить загрузчики в общий модуль
core/ (конспект: «код чтения/валидации — один, не дублировать»). Пока — свой,
чтобы не тянуть SimPy и не править чужой код.
"""

from __future__ import annotations

import json


def load_json(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


# --- обход узлов/рёбер в обеих формах: словарь {n1:{...}} или список [{...}] ---
def _iter_nodes(raw: dict) -> list[dict]:
    nodes = raw.get("nodes") or {}
    return list(nodes.values()) if isinstance(nodes, dict) else list(nodes)


def _iter_ribs(raw: dict) -> list[tuple[str, dict]]:
    ribs = raw.get("ribs") or {}
    if isinstance(ribs, dict):
        return list(ribs.items())
    return [(r.get("name", f"rib{i + 1}"), r) for i, r in enumerate(ribs)]


def _node_inputs(n: dict) -> dict[str, float]:
    if "input_list" in n:
        return dict(n["input_list"] or {})
    if "input" in n:
        return dict(n["input"] or {})
    in_types = list((n.get("type_input") or {}).values())
    asm = n.get("assembly", {})
    return {t: asm.get(t, 1) for t in in_types}


def _node_outputs(n: dict) -> dict[str, float]:
    if "output_list" in n:
        return dict(n["output_list"] or {})
    if "output" in n:
        return dict(n["output"] or {})
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


def _rates(n: dict) -> list[float]:
    """Производительности параллельных единиц ресурса узла (шт/ч), только > 0."""
    elems = n.get("effecive_ellements") or []
    return [float(e.get("ef", 0)) for e in elems if float(e.get("ef", 0)) > 0]


def load_graph(path: str) -> dict:
    """Читает файл графа и возвращает канонический вид для аналитики."""
    raw = load_json(path)

    nodes: dict[int, dict] = {}
    for n in _iter_nodes(raw):
        ntype = n.get("type_node", "transform")
        if ntype == "input_list":
            ntype = "Input"
        rates = _rates(n)
        nodes[n["id"]] = {
            "id": n["id"],
            "name": n["name"],
            "type": ntype,
            "inputs": _node_inputs(n),
            "outputs": _node_outputs(n),
            "rates": rates,                       # мощности единиц (для размерения)
            "n_units": len(rates),                # число параллельных единиц ресурса
            "capacity": sum(rates),               # мощность узла, срабатываний/ч (0 => мгновенный)
            "elements": n.get("effecive_ellements") or [],
            "params": n.get("params", {}),
            "pos": n.get("pos", {}),
            "scale": n.get("scale", {}),
        }

    ribs: list[dict] = []
    for name, r in _iter_ribs(raw):
        ribs.append({
            "name": name,
            "src": r.get("node_in_id", r.get("node_in")),
            "dst": r.get("node_out_id", r.get("node_out")),
            "etype": r.get("type_el", ""),
            "storage": r.get("storage", 100),
            "dest_group": r.get("dest_group"),
            "pool": r.get("pool"),
            "batch": int(r.get("batch", 1)),
        })

    arrival = float(raw.get("input_stream", 100000)) / _items_per_start_unit(raw, nodes, ribs)

    return {
        "nodes": nodes,
        "ribs": ribs,
        "arrival_rate_h": arrival,               # активность стартового узла, ед/ч (палет/ч)
        "input_stream": raw.get("input_stream", 100000),
        "start_node_id": raw.get("start_node_id"),
        "type_input": raw.get("type_input"),
        "type_output": raw.get("type_output"),
        "directions": raw.get("directions"),
        "resource_pools": raw.get("resource_pools") or {},
        "transport": raw.get("transport") or {},
    }


def _items_per_start_unit(raw: dict, nodes: dict[int, dict], ribs: list[dict]) -> float:
    """Сколько товаров получается из одной стартовой единицы (палеты).

    Идёт по цепочке от стартового узла, перемножая детерминированные коэффициенты
    до появления товарной сущности. Совпадает с логикой симулятора, чтобы обе
    модели брали одинаковую интенсивность входа. По умолчанию 20*27 = 540.
    """
    start_id = raw.get("start_node_id")
    if start_id is None or start_id not in nodes:
        return 20.0 * 27.0
    factor = 1.0
    current = nodes[start_id]
    seen: set = set()
    while current and current["id"] not in seen:
        seen.add(current["id"])
        det = {t: q for t, q in current["outputs"].items() if q >= 1}
        if not det:
            break
        etype, qty = max(det.items(), key=lambda kv: kv[1])
        factor *= float(qty)
        if etype.lower().startswith("product") or etype.lower().startswith("tovar"):
            return factor
        current = _next_node(nodes, ribs, current["id"], etype)
    return factor if factor > 1 else 20.0 * 27.0


def _next_node(nodes: dict[int, dict], ribs: list[dict], src_id: int, etype: str):
    for r in ribs:
        if r["src"] == src_id and r["etype"] == etype:
            return nodes.get(r["dst"])
    return None
