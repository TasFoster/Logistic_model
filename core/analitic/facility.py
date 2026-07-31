"""
Штат, парк техники и площадь центра (Этап 4).

Надстройка над балансами: из потоков и размерения ресурсов считаем людей, технику
и квадратные метры по нормативам из norms.py. Это закрывает критерии «занимаемая
площадь» (0–10) и часть «инженерной реалистичности» (штат/парк).

Модель штата (norms.py):
  - автосортировщик: загрузчики = throughput / 1000 (1 чел на 1000 тов/ч)  [PDF];
  - ручная сортировка: 1 человек на 250 тов/ч                              [PDF];
  - остальное: операторы обслуживания на единицу оборудования            [ДОП].

Модель площади:
  площадь = ( Σ площадь_оборудования(единицы) + Σ площадь_буферов(рёбра)
              + накопители_упаковки ) × коэф_проходов.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from . import norms


@dataclass
class FacilityResult:
    per_node: dict = field(default_factory=dict)   # id -> {role, operators, machines, area}
    total_staff: int = 0
    total_area_m2: float = 0.0
    equipment_area_m2: float = 0.0
    buffer_area_m2: float = 0.0
    accumulator_area_m2: float = 0.0
    park: dict = field(default_factory=dict)        # тип техники -> число единиц


def role(node: dict) -> str:
    """Классифицирует узел в роль для нормативов (по типу и имени)."""
    t = node["type"]
    name = node["name"].lower()
    if t == "Storage":
        return "storage"
    if t == "source":
        return "source_machine"
    if t == "Input":
        return "depal"
    if t == "sort":
        return "auto_sorter"
    if t == "pack":
        return "pack"
    if "ruchnaya" in name or "manual" in name:
        return "manual_sort"
    if name.startswith("sort2"):
        return "auto_sorter_stage2"
    if "unktu" in name or "vskr" in name:
        return "unpack"
    if "zaklej" in name or "tape" in name:
        return "tape"
    if "sortkty" in name:
        return "sortkty"
    if "pallet" in name:
        return "palletize"
    if "otgruzka" in name and "vorot" in name:
        return "gate"
    if t == "split":
        return "tare_split"
    return "auto_transform"


def _operators(r: str, throughput: float, units_needed: int) -> int:
    """Число операторов узла по нормативам."""
    if r == "auto_sorter":                                 # [PDF] загрузчики инфида
        return math.ceil(throughput / norms.LOADER_RATE) if throughput > 0 else 0
    if r == "manual_sort":                                 # [PDF] 250 тов/ч на человека
        return units_needed                                # единица размерения = человек
    per = norms.OPERATORS_PER_UNIT.get(r, 0.5)             # [ДОП]
    return math.ceil(per * units_needed)


def _node_area(r: str, units_needed: int, operators: int, graph: dict) -> float:
    """Площадь оборудования узла, м²."""
    if r == "manual_sort":
        return operators * norms.AREA_PER_UNIT_M2["manual_sort"]
    area = units_needed * norms.AREA_PER_UNIT_M2.get(r, norms.AREA_PER_UNIT_M2["auto_transform"])
    return area


def compute(graph: dict, balance, sizing: dict) -> FacilityResult:
    res = FacilityResult()

    for nid, node in graph["nodes"].items():
        r = role(node)
        thr = balance.nodes[nid].throughput
        need = sizing[nid]["needed"]
        ops = _operators(r, thr, need)
        machines = 0 if r in ("manual_sort", "storage") else need
        area = _node_area(r, need, ops, graph)

        res.per_node[nid] = {"role": r, "operators": ops, "machines": machines, "area": area}
        res.total_staff += ops
        res.equipment_area_m2 += area
        if machines:
            res.park[r] = res.park.get(r, 0) + machines

    # накопители упаковки: одна ячейка на направление («одна коробка — одно направление»)
    dcfg = graph.get("directions") or {}
    cells = int(dcfg.get("count", 0))
    has_pack = any(v["role"] == "pack" for v in res.per_node.values())
    if has_pack and cells:
        res.accumulator_area_m2 = cells * norms.ACCUMULATOR_CELL_M2

    # площадь буферов на рёбрах
    for rr in graph["ribs"]:
        fp = norms.ITEM_FOOTPRINT_M2.get(rr["etype"], norms.DEFAULT_ITEM_M2)
        res.buffer_area_m2 += rr["storage"] * fp * norms.STACK_FACTOR

    # мобильная техника (пулы) + водители
    for pname, pcfg in graph.get("resource_pools", {}).items():
        cnt = int(pcfg.get("count", 0))
        res.park[pname] = cnt
        res.total_staff += math.ceil(cnt * norms.OPERATORS_PER_FORKLIFT)

    tech = res.equipment_area_m2 + res.buffer_area_m2 + res.accumulator_area_m2
    res.total_area_m2 = tech * norms.AISLE_FACTOR
    return res
