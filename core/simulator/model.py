"""
Ядро дискретно-событийной имитационной модели сортировочного центра (SimPy).

Архитектура (см. Концепция_решения раздел 2):
    - Ребро = ОГРАНИЧЕННЫЙ буфер (simpy.Store с capacity=storage). Буфер входящего
      ребра И ЕСТЬ входная очередь узла-приёмника. Если буфер полон, put()
      приостанавливает воркера-источника -> так возникает БЛОКИРОВКА и каскадная
      деградация, которые требует показать задача.
    - Узел  = набор параллельных воркеров (процессов SimPy). Воркер собирает входные
      сущности по inputs {тип: количество} (сборка/assembly), держит их service секунд,
      затем порождает выходные сущности по outputs и раскладывает их по рёбрам.
    - Источник входного потока — внешний генератор палет.

Правила выходов (outputs {тип: значение}):
    значение >= 1  — детерминированное размножение (вскрытие: 27 Product + 1 Box);
    значение < 1   — доля вероятностной развилки: узел порождает ОДНУ сущность,
                     выбранную по долям (сортировка: 0.95 Product / 0.05 Nonsort;
                     тара: 0.8 реюз / 0.2 брак).
    пустой outputs — узел-сток (Storage): потребляет и ничего не порождает.

Модель работает на КАНОНИЧЕСКОМ графе (см. graph_loader.normalize), поэтому
одинаково понимает схему контракта (config/example_graph.json) и раннюю схему
прототипа (graph_mini/graph_tare).
"""

from __future__ import annotations

import random
from dataclasses import dataclass

import simpy

from .graph_loader import load_json, normalize


# ---------------------------------------------------------------------------
# Сущности, движущиеся по графу
# ---------------------------------------------------------------------------
@dataclass
class Entity:
    """Единица потока: тип + момент рождения (для расчёта времени пребывания)."""
    etype: str
    t_created: float


class Sink:
    """Сток: сущности, для которых нет исходящего ребра (выход системы, брак,
    nonsort). put() никогда не блокирует."""

    def __init__(self, etype: str):
        self.etype = etype
        self.count = 0
        self.residence: list[float] = []

    def put(self, now: float, e: Entity) -> None:
        self.count += 1
        self.residence.append(now - e.t_created)


class Rib:
    """Ребро = ограниченный буфер между двумя узлами.
    Его store одновременно служит входной очередью узла-приёмника."""

    def __init__(self, env: simpy.Environment, cfg: dict):
        self.name = cfg["name"]
        self.src = cfg["src"]
        self.dst = cfg["dst"]
        self.etype = cfg["etype"]
        self.capacity = cfg["capacity"]
        self.store = simpy.Store(env, capacity=self.capacity)
        self.level_samples: list[int] = []


# ---------------------------------------------------------------------------
# Узел
# ---------------------------------------------------------------------------
class Node:
    def __init__(self, env: simpy.Environment, cfg: dict, model: "SortingCenterModel"):
        self.env = env
        self.model = model
        self.id = cfg["id"]
        self.name = cfg["name"]
        self.type = cfg["type"]
        self.inputs: dict[str, float] = cfg["inputs"]     # {тип: сколько нужно}
        self.outputs: dict[str, float] = cfg["outputs"]   # {тип: кол-во или доля}
        self.workers = cfg["workers"]
        self.service = cfg["service"]
        self.params = cfg.get("params", {})

        # разделяем выходы на детерминированные (>=1) и вероятностные доли (<1)
        self.det_out = {t: int(q) for t, q in self.outputs.items() if q >= 1}
        self.prob_out = {t: float(q) for t, q in self.outputs.items() if 0 < q < 1}

        # сборка: если хотя бы одного типа нужно больше одной штуки, партию собирает
        # один воркер за раз (иначе воркеры разберут буфер и застрянут на неполных партиях)
        self._needs_lock = any(int(q) > 1 for q in self.inputs.values())
        self._gather_lock = simpy.Resource(env, capacity=1)

        self.in_stores: dict[str, simpy.Store] = {}
        self._wstate: dict[int, tuple[str, float]] = {}   # состояние каждого воркера

        # --- статистика ---
        self.processed = 0
        self.produced = 0             # для source-узлов
        self.busy = 0.0               # время обработки
        self.blocked = 0.0            # ожидание на полном выходном буфере
        self.starved = 0.0            # ожидание входных сущностей (нехватка входа)
        self.processed_at_warmup = 0
        self.busy_at_warmup = 0.0
        self.proc_series: list[int] = []

    def start(self) -> None:
        if self.type == "source":
            return                    # у source-узлов отдельный генератор
        for wid in range(self.workers):
            self.env.process(self._worker(wid))

    def _worker(self, wid: int):
        env = self.env
        while True:
            # 1) ждём входные сущности (голодание)
            t = env.now
            self._wstate[wid] = ("starved", t)
            if self._needs_lock:
                with self._gather_lock.request() as req:
                    yield req
                    inputs = yield from self._gather()
            else:
                inputs = yield from self._gather()
            self.starved += env.now - t

            # 2) обработка
            t = env.now
            self._wstate[wid] = ("busy", t)
            yield env.timeout(self.service)
            self.busy += env.now - t

            # 3) выдача выходов (блокировка, если буфер полон)
            outs = list(self._transform(inputs))
            t = env.now
            self._wstate[wid] = ("blocked", t)
            yield from self._emit(outs)
            self.blocked += env.now - t

            self.processed += 1

    def settle(self, now: float) -> None:
        """Досчитывает незавершённое время воркеров на момент конца прогона.

        Без этого время воркера, застрявшего в блокировке на полном буфере в момент
        остановки симуляции, не попадало бы в статистику вообще — и заторможенная
        система выглядела бы «незагруженной».
        """
        for state, t in self._wstate.values():
            dt = now - t
            if dt <= 0:
                continue
            if state == "starved":
                self.starved += dt
            elif state == "busy":
                self.busy += dt
            elif state == "blocked":
                self.blocked += dt

    def _gather(self):
        """Собирает нужное число входных сущностей каждого типа."""
        gathered: dict[str, list[Entity]] = {}
        for etype, qty in self.inputs.items():
            got: list[Entity] = []
            store = self.in_stores[etype]
            for _ in range(int(qty)):
                got.append((yield store.get()))
            gathered[etype] = got
        return gathered

    def _transform(self, gathered: dict[str, list[Entity]]):
        """Порождает выходные сущности по правилам outputs."""
        all_ents = [e for lst in gathered.values() for e in lst]
        t_rep = min((e.t_created for e in all_ents), default=self.env.now)

        # детерминированные выходы: размножение по количеству
        for etype, qty in self.det_out.items():
            for _ in range(qty):
                yield Entity(etype, t_rep)

        # вероятностная развилка: ровно одна сущность по долям
        if self.prob_out:
            r = self.model.rng.random()
            cum = 0.0
            chosen = list(self.prob_out)[-1]
            for etype, share in self.prob_out.items():
                cum += share
                if r <= cum:
                    chosen = etype
                    break
            yield Entity(chosen, t_rep)

    def _emit(self, entities: list[Entity]):
        """Выдаёт ВСЕ выходы узла ПАРАЛЛЕЛЬНО.

        Важно: выходы разных типов уходят на разные ленты одновременно. Если бы узел
        выкладывал их по очереди, затор на одной ленте не давал бы отдать выход на
        другую — и возникал бы круговой клинч (вскрытие держит короб, пока забита
        лента товаров; упаковка ждёт короб и не разбирает товары). Физически вскрытие
        КТЯ отдаёт товары и пустой короб одновременно.

        Узел всё равно остаётся занятым, пока ВСЕ выходы не приняты, — поэтому
        блокировка при переполнении буфера сохраняется.
        """
        events = []
        for e in entities:
            rib = self.model.find_rib(self.id, e.etype)
            if rib is None:
                self.model.sink(e.etype).put(self.env.now, e)   # выход системы / брак
            else:
                events.append(rib.store.put(e))   # ставим put в очередь, не ждём здесь
        if not events:
            return
        yield self.env.all_of(events)             # ждём, пока все выходы примут


# ---------------------------------------------------------------------------
# Модель целиком
# ---------------------------------------------------------------------------
class SortingCenterModel:
    def __init__(self, graph: dict, seed: int = 42, warmup_s: float = 300.0,
                 sample_dt: float = 5.0):
        """graph — КАНОНИЧЕСКИЙ граф (см. graph_loader.normalize)."""
        self.env = simpy.Environment()
        self.rng = random.Random(seed)
        self.warmup = warmup_s
        self.sample_dt = sample_dt
        self.graph = graph

        self.nodes: dict[int, Node] = {
            nid: Node(self.env, cfg, self) for nid, cfg in graph["nodes"].items()
        }
        self.ribs: list[Rib] = [Rib(self.env, c) for c in graph["ribs"]]
        self._rib_by_name = {r.name: r for r in self.ribs}
        self._rib_index: dict[tuple[int, str], Rib] = {(r.src, r.etype): r for r in self.ribs}

        # входные очереди узлов = буферы входящих рёбер
        for r in self.ribs:
            if r.dst in self.nodes:
                self.nodes[r.dst].in_stores[r.etype] = r.store
        # входы без входящего ребра (стартовый узел, source) — синтетический буфер
        for n in self.nodes.values():
            for t in n.inputs:
                if t not in n.in_stores:
                    n.in_stores[t] = simpy.Store(self.env)

        self._sinks: dict[str, Sink] = {}

        self.arrival_rate_h = graph["arrival_rate_h"]
        self.start_node_id = graph.get("start_node_id")
        self.input_type = graph.get("input_type")
        self.input_stream = graph.get("input_stream", 100000)
        self.generated = 0
        self.sim_time = 0.0

    def find_rib(self, src_id: int, etype: str) -> Rib | None:
        return self._rib_index.get((src_id, etype))

    def sink(self, etype: str) -> Sink:
        if etype not in self._sinks:
            self._sinks[etype] = Sink(etype)
        return self._sinks[etype]

    # ---- генератор входного потока (палеты в приёмку) ----
    def _palet_source(self):
        env = self.env
        # стартовый узел: из графа, иначе — узел типа Input
        start = self.nodes.get(self.start_node_id)
        if start is None:
            start = next((n for n in self.nodes.values() if n.type == "Input"), None)
        if start is None:
            return
        in_type = self.input_type or (next(iter(start.inputs), "Palet"))
        interval = 3600.0 / self.arrival_rate_h
        while True:
            yield env.timeout(interval)
            yield start.in_stores[in_type].put(Entity(in_type, env.now))
            self.generated += 1

    # ---- генератор тары (машина новых КТЯ) ----
    def _tare_source(self, node: Node):
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
        while True:
            yield self.env.timeout(self.sample_dt)
            for r in self.ribs:
                r.level_samples.append(len(r.store.items))

    def _minute_series(self):
        while True:
            yield self.env.timeout(60.0)
            for n in self.nodes.values():
                n.proc_series.append(n.processed)

    def _warmup_snapshot(self):
        yield self.env.timeout(self.warmup)
        for n in self.nodes.values():
            n.processed_at_warmup = n.processed
            n.busy_at_warmup = n.busy

    def run(self, hours: float = 1.0):
        for n in self.nodes.values():
            n.start()
        self.env.process(self._palet_source())
        for n in self.nodes.values():
            if n.type == "source":
                self.env.process(self._tare_source(n))
        self.env.process(self._sampler())
        self.env.process(self._minute_series())
        self.env.process(self._warmup_snapshot())
        self.sim_time = hours * 3600.0
        self.env.run(until=self.sim_time)
        for n in self.nodes.values():
            n.settle(self.env.now)     # досчитать незавершённое время воркеров
        return self


def load_graph(path: str, scenario_path: str | None = None) -> dict:
    """Читает файл графа (любая из схем) и приводит к каноническому виду.
    scenario_path — файл со временами обработки и интенсивностью входа."""
    raw = load_json(path)
    scenario = load_json(scenario_path) if scenario_path else None
    return normalize(raw, scenario)
