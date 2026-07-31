"""
Стационарный расчёт балансов потоков на графе (аналитическая модель).

Идея (конспект наставника, §3-4): у аналитики нет оси времени, только
интенсивности «штук в час». В установившемся режиме для каждого узла
«втекает = вытекает» (в пересчитанных единицах). Считаем:

  lam[node]  — активность узла, СРАБАТЫВАНИЙ/ч (это же throughput, что печатает
               симулятор: unKTU 3704 КТЯ/ч, Sort 100000 тов/ч и т.п.);
  flow[rib]  — поток по ребру, сущностей/ч (пара «число + тип»);
  load[node] — загрузка = lam / мощность (мощность = Σ ef).

Развилки и размножение — линейны, поэтому один проход по топологическому порядку
рёбер-DAG считает почти всё. Циклы оборота тары замкнуты не ребром, а узлом
`source` (машина новых КТЯ): он добирает ДЕФИЦИТ тары в узле-приёмнике
(спрос упаковки минус повторно используемые короба). Источники считаются после
основного прохода; для общности делаем несколько итераций (сходится за 1).

Маршрутизация одного типа по нескольким рёбрам (2-я стадия сортировки, поле
`dest_group`) на этом этапе делится ПОРОВНУ — точное деление по объёму направлений
добавим на этапе 2-стадийного графа (см. 'Процесс разработки.md', план §1.9).
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field

from .directions import DirectionProfile


@dataclass
class NodeResult:
    id: int
    name: str
    type: str
    throughput: float          # срабатываний/ч (= lam)
    capacity: float            # мощность, срабатываний/ч (0 => мгновенный/сток)
    load: float                # загрузка, доля (throughput/capacity)
    inflow: dict = field(default_factory=dict)
    outflow: dict = field(default_factory=dict)


@dataclass
class BalanceResult:
    arrival_rate_h: float
    input_stream: float
    nodes: dict                        # id -> NodeResult
    rib_flow: dict                     # name -> поток, сущностей/ч
    bottleneck_id: int | None
    max_input_stream: float            # предельный вход (товаров/ч), при котором узкое место = 100%

    def bottleneck(self) -> NodeResult | None:
        return self.nodes.get(self.bottleneck_id) if self.bottleneck_id is not None else None


def _topo_order(nodes: dict, ribs: list[dict]) -> list[int]:
    """Топологический порядок НЕ-source узлов по рёбрам (алгоритм Кана).

    Рёбра-source (впрыск тары) в DAG не входят — источники считаются отдельно.
    Если граф внезапно содержит цикл в рёбрах, оставшиеся узлы дописываются как есть.
    """
    ids = [nid for nid, n in nodes.items() if n["type"] != "source"]
    idset = set(ids)
    succ: dict[int, list[int]] = {nid: [] for nid in ids}
    indeg: dict[int, int] = {nid: 0 for nid in ids}
    for r in ribs:
        s, d = r["src"], r["dst"]
        if s in idset and d in idset:
            succ[s].append(d)
            indeg[d] += 1
    q = deque(nid for nid in ids if indeg[nid] == 0)
    order: list[int] = []
    while q:
        u = q.popleft()
        order.append(u)
        for v in succ[u]:
            indeg[v] -= 1
            if indeg[v] == 0:
                q.append(v)
    if len(order) < len(ids):
        order += [nid for nid in ids if nid not in set(order)]
    return order


def solve(graph: dict, arrival: float | None = None) -> BalanceResult:
    """Считает стационарные потоки и загрузки. arrival — переопределение входа."""
    nodes = graph["nodes"]
    ribs = graph["ribs"]
    if arrival is None:
        arrival = graph["arrival_rate_h"]
    start_id = graph.get("start_node_id")

    # фактический входной поток в товарах/ч (пересчёт активности старта в товары).
    # При переопределении arrival он тоже меняется, иначе max_input считался бы от
    # номинала графа, а не от фактического входа.
    items_per_unit = graph["input_stream"] / graph["arrival_rate_h"]
    input_stream = arrival * items_per_unit

    rib_by_name = {r["name"]: r for r in ribs}

    # --- метаданные источников (машина новых КТЯ) ---
    sources_by_target: dict[int, list[dict]] = {}
    for n in nodes.values():
        if n["type"] == "source":
            p = n["params"]
            emit = p.get("emit_type") or next(iter(n["outputs"]), None)
            tr = rib_by_name.get(p.get("target_rib", ""))
            n["_emit"] = emit
            n["_target"] = tr["dst"] if tr else None
            if n["_target"] is not None:
                sources_by_target.setdefault(n["_target"], []).append(n)
    # какие входные типы узла закрываются источником (значит, не «главные»)
    source_backed: dict[int, set] = {
        tgt: {s["_emit"] for s in srcs} for tgt, srcs in sources_by_target.items()
    }

    # исходящие рёбра по (узел, тип)
    out_by: dict[tuple[int, str], list[dict]] = {}
    for r in ribs:
        out_by.setdefault((r["src"], r["etype"]), []).append(r)

    # профиль направлений: доли объёма по группам 2-й стадии (если граф двухстадийный).
    # Нужен, чтобы делить поток по группам ПРОПОРЦИОНАЛЬНО объёму, а не поровну.
    dcfg = graph.get("directions") or {}
    group_shares: list[float] | None = None
    if dcfg.get("groups", 0):
        prof = DirectionProfile(
            count=dcfg.get("count", 400), top_share=dcfg.get("top_share", 0.2),
            volume_share=dcfg.get("volume_share", 0.8), profile=dcfg.get("profile", "pareto"),
            groups=dcfg.get("groups", 0), grouping=dcfg.get("grouping", "balanced"))
        group_shares = prof.group_volume_shares()

    order = _topo_order(nodes, ribs)

    lam: dict[int, float] = {nid: 0.0 for nid in nodes}
    rib_flow: dict[str, float] = {r["name"]: 0.0 for r in ribs}
    src_inject: dict[tuple[int, str], float] = {}

    def inflow(nid: int, etype: str) -> float:
        f = sum(rib_flow[r["name"]] for r in ribs if r["dst"] == nid and r["etype"] == etype)
        return f + src_inject.get((nid, etype), 0.0)

    for _ in range(3):                       # сходится за 1 проход; запас для общих случаев
        # 1) не-source узлы в топологическом порядке
        for nid in order:
            n = nodes[nid]
            if n["type"] == "Input" or nid == start_id:
                lam[nid] = arrival
            elif n["inputs"]:
                primary = [t for t in n["inputs"] if t not in source_backed.get(nid, set())]
                if not primary:
                    primary = list(n["inputs"])
                lam[nid] = min(inflow(nid, t) / n["inputs"][t] for t in primary)
            else:
                lam[nid] = 0.0
            # раскладываем выходы по рёбрам
            for etype, coef in n["outputs"].items():
                outs = out_by.get((nid, etype), [])
                if not outs:
                    continue
                total = lam[nid] * coef
                # несколько рёбер одного типа с группами направлений (2-я стадия):
                # делим ПРОПОРЦИОНАЛЬНО доле объёма группы, иначе — поровну.
                if len(outs) > 1 and group_shares is not None \
                        and all(r["dest_group"] is not None for r in outs):
                    weights = [group_shares[r["dest_group"]] for r in outs]
                    wsum = sum(weights) or 1.0
                    for r, w in zip(outs, weights):
                        rib_flow[r["name"]] = total * w / wsum
                else:
                    for r in outs:
                        rib_flow[r["name"]] = total / len(outs)

        # 2) источники: добирают дефицит тары в узле-приёмнике
        for tgt, srcs in sources_by_target.items():
            for s in srcs:
                et = s["_emit"]
                demand = lam[tgt] * nodes[tgt]["inputs"].get(et, 0.0)
                other = sum(rib_flow[r["name"]] for r in ribs
                            if r["dst"] == tgt and r["etype"] == et)
                deficit = max(0.0, demand - other)
                lam[s["id"]] = deficit
                src_inject[(tgt, et)] = deficit

    # --- сборка результата ---
    results: dict[int, NodeResult] = {}
    for nid, n in nodes.items():
        cap = n["capacity"]
        load = (lam[nid] / cap) if cap > 0 else 0.0
        inf = {t: inflow(nid, t) for t in n["inputs"]}
        outf = {t: lam[nid] * c for t, c in n["outputs"].items()}
        results[nid] = NodeResult(nid, n["name"], n["type"], lam[nid], cap, load, inf, outf)

    # узкое место — максимальная загрузка среди узлов с конечной мощностью
    finite = [r for r in results.values() if r.capacity > 0]
    bottleneck = max(finite, key=lambda r: r.load) if finite else None
    max_input = (input_stream / bottleneck.load) if (bottleneck and bottleneck.load > 0) else float("inf")

    return BalanceResult(
        arrival_rate_h=arrival,
        input_stream=input_stream,
        nodes=results,
        rib_flow=rib_flow,
        bottleneck_id=bottleneck.id if bottleneck else None,
        max_input_stream=max_input,
    )
