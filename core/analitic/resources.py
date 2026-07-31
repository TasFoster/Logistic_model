"""
Размерение ресурсов узлов (конспект наставника, §4 — вопрос «сколько купить»).

Дано: поток через узел lam (срабатываний/ч) и производительность единицы ресурса
(ef, шт/ч). Ищем минимальное число единиц, которое покрывает поток, с округлением
ВВЕРХ (округление вниз оставило бы узел перегруженным). Сравнение «есть / нужно»
показывает запас или дефицит по каждому узлу.

Единица ресурса = один параллельный исполнитель (человек-оператор, автомат,
сортировщик). Их производительности берём из effecive_ellements. Если единицы
разной мощности — считаем по средней (capacity / число единиц); в наших графах
единицы узла однородны, поэтому это точно.
"""

from __future__ import annotations

import math


def size_node(node_cfg: dict, throughput: float) -> dict:
    """Возвращает {have, needed, per_unit, spare} для одного узла.

    have    — сколько единиц ресурса в графе;
    needed  — минимум единиц под поток throughput (округление вверх);
    per_unit— производительность одной единицы, шт/ч;
    spare   — запас единиц (have - needed); отрицательный => узел недоразмерен.
    """
    have = node_cfg["n_units"]
    cap = node_cfg["capacity"]
    if have <= 0 or cap <= 0:                 # мгновенный узел (Storage/сток)
        return {"have": have, "needed": 0, "per_unit": 0.0, "spare": have}
    per_unit = cap / have
    # -1e-9: страховка от плавающей точки (ceil(10.0000001) не должен давать 11)
    needed = max(1, math.ceil(throughput / per_unit - 1e-9)) if throughput > 0 else 0
    return {"have": have, "needed": needed, "per_unit": per_unit, "spare": have - needed}


def size_all(graph: dict, balance) -> dict:
    """Размерение по всем узлам. balance — результат balance.solve()."""
    out = {}
    for nid, node_cfg in graph["nodes"].items():
        out[nid] = size_node(node_cfg, balance.nodes[nid].throughput)
    return out
