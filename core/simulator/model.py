"""
Ядро дискретно-событийной имитационной модели сортировочного центра (SimPy).

Архитектура (см. Концепция_решения раздел 2):
    - Ребро = ОГРАНИЧЕННЫЙ буфер (simpy.Store с capacity=storage). Буфер входящего
      ребра И ЕСТЬ входная очередь узла-приёмника. Если буфер полон, put()
      приостанавливает воркера-источника -> так возникает БЛОКИРОВКА и каскадная
      деградация, которые требует показать задача.
    - Узел  = набор параллельных воркеров (процессов SimPy). Каждый воркер берёт
      1 входную сущность из входного буфера, держит её service_time_s секунд
      (обработка), затем порождает выходные сущности по transform_kof / type_output
      и раскладывает их по исходящим рёбрам.
    - Источник входного потока — внешний генератор палет с интенсивностью,
      пересчитанной из input_stream (товаров/ч).

Модель читает тот же файл графа, что и аналитическая модель (общий контракт).
Единственное расширение схемы — поле service_time_s (см. README, раздел 'Пробелы схемы').

Прототип намеренно ограничен: узлы с одним типом входа, без сборки (assembly)
и без цикла оборота тары. Эти механики — следующая итерация (см. README 'Дальше').
"""

from __future__ import annotations

import json
import random
from dataclasses import dataclass

import simpy


# ---------------------------------------------------------------------------
# Сущности, движущиеся по графу
# ---------------------------------------------------------------------------
@dataclass
class Entity:
    """Единица потока: тип + момент рождения (для расчёта времени пребывания)."""
    etype: str
    t_created: float


class Sink:
    """Сток: сущности, для которых нет исходящего ребра (выход системы,
    поток брака/nonsort, поток пустой тары). put() никогда не блокирует."""

    def __init__(self, etype: str):
        self.etype = etype
        self.count = 0
        self.residence: list[float] = []  # время пребывания в центре

    def put(self, now: float, e: Entity) -> None:
        self.count += 1
        self.residence.append(now - e.t_created)


class Rib:
    """Ребро = ограниченный буфер между двумя узлами.
    Его store одновременно служит входной очередью узла-приёмника."""

    def __init__(self, env: simpy.Environment, name: str, cfg: dict):
        self.name = name
        self.src = cfg["node_in"]      # id узла-источника (поток входит в ребро)
        self.dst = cfg["node_out"]     # id узла-приёмника (поток выходит из ребра)
        self.etype = cfg.get("type_el", "")
        self.capacity = cfg.get("storage", 100)
        self.store = simpy.Store(env, capacity=self.capacity)
        self.level_samples: list[int] = []  # заполненность буфера во времени


# ---------------------------------------------------------------------------
# Узел
# ---------------------------------------------------------------------------
class Node:
    def __init__(self, env: simpy.Environment, cfg: dict, model: "SortingCenterModel"):
        self.env = env
        self.model = model
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.type = cfg.get("type_node", "transform")
        self.kof = cfg.get("transform_kof", [1])
        self.out_types = cfg.get("type_output", [])
        self.in_types = list(cfg.get("type_input", {}).values())
        # ef трактуется как число параллельных воркеров (серверов) узла
        elems = cfg.get("effecive_ellements", [{"ef": 1}])
        self.workers = sum(int(e.get("ef", 1)) for e in elems) or 1
        self.service = cfg.get("service_time_s", model.default_service)
        self.params = cfg.get("params", {})

        # Входная очередь узла — задаётся моделью после создания всех рёбер:
        #   - для обычного узла это буфер входящего ребра (ограниченный);
        #   - для узла-источника (Input) — отдельный буфер, куда кладёт генератор.
        self.in_store: simpy.Store | None = None

        # --- статистика ---
        self.processed = 0            # обработано входных сущностей (всего)
        self.busy = 0.0               # суммарное время обработки всеми воркерами
        self.blocked = 0.0            # суммарное время ожидания на полном буфере
        self.processed_at_warmup = 0
        self.busy_at_warmup = 0.0
        self.proc_series: list[int] = []  # снимки processed каждую модельную минуту

    # ---- порождение воркеров ----
    def start(self) -> None:
        for _ in range(self.workers):
            self.env.process(self._worker())

    def _worker(self):
        env = self.env
        while True:
            e: Entity = yield self.in_store.get()    # взять 1 входную сущность
            t0 = env.now
            yield env.timeout(self.service)          # обработка
            self.busy += env.now - t0
            for out_e in self._transform(e):
                yield from self._route(out_e)
            self.processed += 1

    # ---- функция преобразования узла ----
    def _transform(self, e: Entity):
        """Порождает выходные сущности из одной входной по правилам узла."""
        if self.type == "sort":
            # Сортировка: 1 товар -> 1 товар, но доля nonsort уходит в отбраковку
            share = self.params.get("nonsort_share", 0.0)
            if self.model.rng.random() < share:
                yield Entity("Nonsort", e.t_created)
            else:
                yield Entity(self.out_types[0] if self.out_types else e.etype, e.t_created)
            return
        # Универсальное правило: для каждого (тип_выхода[i], kof[i]) порождаем kof копий
        for i, out_type in enumerate(self.out_types):
            k = int(self.kof[i]) if i < len(self.kof) else 1
            for _ in range(k):
                yield Entity(out_type, e.t_created)

    # ---- маршрутизация выхода по рёбрам ----
    def _route(self, e: Entity):
        rib = self.model.find_rib(self.id, e.etype)
        if rib is None:
            # нет исходящего ребра для этого типа -> сток (выход системы / тара / nonsort)
            self.model.sink(e.etype).put(self.env.now, e)
            return
        t0 = self.env.now
        yield rib.store.put(e)             # БЛОКИРУЕТСЯ, если буфер полон
        dt = self.env.now - t0
        if dt > 0:
            self.blocked += dt


# ---------------------------------------------------------------------------
# Модель целиком
# ---------------------------------------------------------------------------
class SortingCenterModel:
    def __init__(self, graph: dict, seed: int = 42, warmup_s: float = 300.0,
                 default_service: float = 30.0, sample_dt: float = 5.0):
        self.env = simpy.Environment()
        self.rng = random.Random(seed)
        self.warmup = warmup_s
        self.default_service = default_service
        self.sample_dt = sample_dt
        self.graph = graph

        # узлы
        self.nodes: dict[int, Node] = {}
        for cfg in graph["nodes"].values():
            n = Node(self.env, cfg, self)
            self.nodes[n.id] = n

        # рёбра
        self.ribs: list[Rib] = [Rib(self.env, name, c) for name, c in graph["ribs"].items()]
        # индекс: (id_источника, тип_сущности) -> ребро
        self._rib_index: dict[tuple[int, str], Rib] = {
            (r.src, r.etype): r for r in self.ribs
        }

        # входная очередь каждого узла = буфер его входящего ребра
        for r in self.ribs:
            self.nodes[r.dst].in_store = r.store
        # узлам-источникам (Input) даём отдельный входной буфер под генератор
        for n in self.nodes.values():
            if n.in_store is None:
                n.in_store = simpy.Store(self.env)

        # стоки (создаются лениво по типу сущности)
        self._sinks: dict[str, Sink] = {}

        # интенсивность входа: input_stream товаров/ч -> палет/ч
        # 1 палета -> 20 КТЯ -> 27 товаров: товаров = палет * 20 * 27
        self.input_stream = graph.get("input_stream", 100000)
        self.arrival_rate_h = self.input_stream / (20 * 27)  # палет/ч
        self.generated = 0  # сколько палет подано генератором

        self.sim_time = 0.0

    # ---- вспомогательное ----
    def find_rib(self, src_id: int, etype: str) -> Rib | None:
        return self._rib_index.get((src_id, etype))

    def sink(self, etype: str) -> Sink:
        if etype not in self._sinks:
            self._sinks[etype] = Sink(etype)
        return self._sinks[etype]

    # ---- источник входного потока ----
    def _source(self):
        env = self.env
        sources = [n for n in self.nodes.values() if n.type == "Input"]
        interval = 3600.0 / self.arrival_rate_h
        while True:
            yield env.timeout(interval)
            for n in sources:
                in_type = n.in_types[0] if n.in_types else "Palet"
                # источник не ограничен: если приёмка не успевает, палеты копятся
                # во входном буфере узла-источника (представляет очередь машин/двор)
                yield n.in_store.put(Entity(in_type, env.now))
                self.generated += 1

    # ---- мониторы ----
    def _sampler(self):
        env = self.env
        while True:
            yield env.timeout(self.sample_dt)
            for r in self.ribs:
                r.level_samples.append(len(r.store.items))

    def _minute_series(self):
        env = self.env
        while True:
            yield env.timeout(60.0)
            for n in self.nodes.values():
                n.proc_series.append(n.processed)

    def _warmup_snapshot(self):
        yield self.env.timeout(self.warmup)
        for n in self.nodes.values():
            n.processed_at_warmup = n.processed
            n.busy_at_warmup = n.busy

    # ---- запуск ----
    def run(self, hours: float = 1.0):
        for n in self.nodes.values():
            n.start()
        self.env.process(self._source())
        self.env.process(self._sampler())
        self.env.process(self._minute_series())
        self.env.process(self._warmup_snapshot())
        self.sim_time = hours * 3600.0
        self.env.run(until=self.sim_time)
        return self


def load_graph(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
