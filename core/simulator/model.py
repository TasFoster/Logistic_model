"""
Ядро дискретно-событийной имитационной модели сортировочного центра (SimPy).

Архитектура (см. Концепция_решения раздел 2):
    - Ребро = ОГРАНИЧЕННЫЙ буфер (simpy.Store с capacity=storage). Буфер входящего
      ребра И ЕСТЬ входная очередь узла-приёмника. Если буфер полон, put()
      приостанавливает воркера-источника -> так возникает БЛОКИРОВКА и каскадная
      деградация, которые требует показать задача.
    - Узел  = набор параллельных воркеров (процессов SimPy). Каждый воркер собирает
      входные сущности (одну или несколько — сборка/assembly), держит их
      service_time_s секунд (обработка), затем порождает выходные сущности по
      правилам узла и раскладывает их по исходящим рёбрам.
    - Источник входного потока — внешний генератор палет с интенсивностью,
      пересчитанной из input_stream (товаров/ч).

Типы узлов (type_node):
    Input      — точка приёмки: внешний генератор кладёт сюда входную сущность.
    transform  — 1 вход -> выходы по (type_output[i], transform_kof[i]).
    sort       — сортировка: 1 товар -> товар, доля nonsort уходит в отбраковку.
    split      — развилка по долям (params.routes): напр. тара 80% реюз / 20% брак.
    pack       — упаковка со сборкой (assembly): N товаров + 1 КТЯ -> 1 полный КТЯ.
    source     — генератор тары (машина новых КТЯ), восполняет буфер по низкому уровню.

Модель читает тот же файл графа, что и аналитическая модель (общий контракт).
Расширения схемы относительно config/example_graph.json (см. README, 'Пробелы схемы'):
    service_time_s  — время обработки одной сущности одним воркером, секунд;
    params          — параметры узла (nonsort_share, routes, source и т.д.);
    assembly        — {тип_входа: количество} для узлов сборки.
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

        # сборка: сколько сущностей каждого типа нужно, чтобы узел сработал.
        # По умолчанию — по 1 каждого входного типа (обычный узел).
        self.assembly: dict[str, int] = cfg.get(
            "assembly", {t: 1 for t in self.in_types}
        )
        self._needs_lock = any(int(q) > 1 for q in self.assembly.values())

        # Входные очереди узла по типу сущности (задаются моделью после рёбер):
        #   - обычный вход это буфер входящего ребра (ограниченный);
        #   - для Input/source-типов — синтетический буфер под генератор.
        self.in_stores: dict[str, simpy.Store] = {}
        # Замок сборки: гарантирует, что партию собирает один воркер за раз
        # (иначе воркеры разбирают буфер по частям и застревают на неполных партиях).
        self._gather_lock = simpy.Resource(env, capacity=1)

        # --- статистика ---
        self.processed = 0            # сколько раз узел сработал (всего)
        self.produced = 0             # для source-узлов: сколько сущностей порождено
        self.busy = 0.0               # суммарное время обработки всеми воркерами
        self.blocked = 0.0            # суммарное время ожидания на полном буфере
        self.processed_at_warmup = 0
        self.busy_at_warmup = 0.0
        self.proc_series: list[int] = []  # снимки processed каждую модельную минуту

    # ---- порождение воркеров ----
    def start(self) -> None:
        if self.type == "source":
            return  # у source-узлов отдельный генератор (см. модель)
        for _ in range(self.workers):
            self.env.process(self._worker())

    def _worker(self):
        env = self.env
        while True:
            if self._needs_lock:
                with self._gather_lock.request() as req:
                    yield req
                    inputs = yield from self._gather()
            else:
                inputs = yield from self._gather()
            t0 = env.now
            yield env.timeout(self.service)          # обработка
            self.busy += env.now - t0
            for out_e in self._transform(inputs):
                yield from self._route(out_e)
            self.processed += 1

    def _gather(self):
        """Собирает нужное число входных сущностей каждого типа (assembly)."""
        inputs: dict[str, list[Entity]] = {}
        for etype, qty in self.assembly.items():
            got: list[Entity] = []
            store = self.in_stores[etype]
            for _ in range(int(qty)):
                got.append((yield store.get()))
            inputs[etype] = got
        return inputs

    # ---- функция преобразования узла ----
    def _transform(self, inputs: dict[str, list[Entity]]):
        """Порождает выходные сущности из набора входных по правилам узла."""
        all_ents = [e for lst in inputs.values() for e in lst]
        t_rep = min((e.t_created for e in all_ents), default=self.env.now)

        if self.type == "sort":
            # Сортировка: 1 товар -> 1 товар, но доля nonsort уходит в отбраковку
            share = self.params.get("nonsort_share", 0.0)
            if self.model.rng.random() < share:
                yield Entity("Nonsort", t_rep)
            else:
                yield Entity(self.out_types[0] if self.out_types else all_ents[0].etype, t_rep)
            return

        if self.type == "split":
            # Развилка по долям: напр. тара 80% EmptyKTY (реюз) / 20% Brak (брак)
            routes = self.params.get("routes", [])
            r = self.model.rng.random()
            cum = 0.0
            chosen = routes[-1]["type"] if routes else (
                self.out_types[0] if self.out_types else all_ents[0].etype)
            for route in routes:
                cum += route["share"]
                if r <= cum:
                    chosen = route["type"]
                    break
            yield Entity(chosen, t_rep)
            return

        # transform / Input / pack: выходы по (type_output[i], transform_kof[i])
        for i, out_type in enumerate(self.out_types):
            k = int(self.kof[i]) if i < len(self.kof) else 1
            for _ in range(k):
                yield Entity(out_type, t_rep)

    # ---- маршрутизация выхода по рёбрам ----
    def _route(self, e: Entity):
        rib = self.model.find_rib(self.id, e.etype)
        if rib is None:
            # нет исходящего ребра для этого типа -> сток (выход системы / брак / nonsort)
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
        self._rib_by_name: dict[str, Rib] = {r.name: r for r in self.ribs}
        # индекс: (id_источника, тип_сущности) -> ребро
        self._rib_index: dict[tuple[int, str], Rib] = {
            (r.src, r.etype): r for r in self.ribs
        }

        # входные очереди узлов: буфер входящего ребра по типу сущности
        for r in self.ribs:
            self.nodes[r.dst].in_stores[r.etype] = r.store
        # недостающие входы (у Input/source нет входящего ребра) — синтетический буфер
        for n in self.nodes.values():
            for t in n.in_types:
                if t not in n.in_stores:
                    n.in_stores[t] = simpy.Store(self.env)

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

    # ---- источник входного потока (приёмка палет) ----
    def _palet_source(self):
        env = self.env
        sources = [n for n in self.nodes.values() if n.type == "Input"]
        interval = 3600.0 / self.arrival_rate_h
        while True:
            yield env.timeout(interval)
            for n in sources:
                in_type = n.in_types[0] if n.in_types else "Palet"
                # источник не ограничен: если приёмка не успевает, палеты копятся
                # во входном буфере узла-источника (представляет очередь машин/двор)
                yield n.in_stores[in_type].put(Entity(in_type, env.now))
                self.generated += 1

    # ---- генератор тары (машина новых КТЯ): восполняет буфер по низкому уровню ----
    def _tare_source(self, node: Node):
        """params: {emit_type, target_rib, low, poll}. Кладёт новую тару в целевой
        буфер, пока его уровень ниже low. Производительность эмерджентна = дефициту."""
        env = self.env
        emit = node.params.get("emit_type", "EmptyKTY")
        target = self._rib_by_name.get(node.params.get("target_rib", ""))
        low = int(node.params.get("low", 20))
        poll = float(node.params.get("poll", 1.0))
        if target is None:
            return
        store = target.store
        while True:
            if len(store.items) < low and len(store.items) < store.capacity:
                yield store.put(Entity(emit, env.now))
                node.produced += 1
            else:
                yield env.timeout(poll)

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
        # генераторы: приёмка палет + машины новых КТЯ (source-узлы)
        self.env.process(self._palet_source())
        for n in self.nodes.values():
            if n.type == "source":
                self.env.process(self._tare_source(n))
        self.env.process(self._sampler())
        self.env.process(self._minute_series())
        self.env.process(self._warmup_snapshot())
        self.sim_time = hours * 3600.0
        self.env.run(until=self.sim_time)
        return self


def load_graph(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)
