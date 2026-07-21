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

from .directions import DirectionProfile
from .graph_loader import load_json, normalize


# ---------------------------------------------------------------------------
# Сущности, движущиеся по графу
# ---------------------------------------------------------------------------
@dataclass
class Entity:
    """Единица потока: тип + момент рождения + направление отправки (для товаров)."""
    etype: str
    t_created: float
    dest: int | None = None
    ready_at: float = 0.0     # когда доедет по ребру и станет доступна потребителю


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
        # время в пути: ребро — это перемещение, а не мгновенная связь.
        # Считается по координатам узлов и скорости транспорта (см. graph_loader).
        self.travel = float(cfg.get("travel", 0.0))
        # группа направлений, которую несёт это ребро (двухстадийная сортировка):
        # ребро от сортировщика 1-й стадии к своей секции 2-й стадии
        self.dest_group = cfg.get("dest_group")
        self.dist_m = float(cfg.get("dist_m", 0.0))
        self.pool = cfg.get("pool")            # имя пула мобильных ресурсов
        self.batch = int(cfg.get("batch", 1))  # сколько единиц везёт один рейс
        self.dest_store: simpy.Store | None = None  # буфер узла-приёмника
        # store назначается моделью: рёбра, ведущие в ОДИН узел с ОДНИМ типом
        # сущности, делят общий буфер (у узла один вход на тип, но поставщиков
        # может быть много — 20 секций 2-й стадии кормят одну упаковку)
        self.store: simpy.Store | None = None
        self.level_samples: list[int] = []
        self.passed = 0               # сколько сущностей прошло по ребру (для схемы потоков)


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
        # у каждого исполнителя своя скорость: service = 3600 / ef (ef — производительность)
        self.services: list[float] = list(cfg["services"])
        if len(self.services) < self.workers:
            self.services += [self.services[-1]] * (self.workers - len(self.services))
        self.capacity_per_h = sum(3600.0 / s for s in self.services if s > 0)
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

        # --- упаковка по направлениям (одна коробка = одно направление) ---
        self.by_direction = bool(self.params.get("by_direction", False))
        self.flush_timeout = float(self.params.get("flush_timeout_s", 0))
        self.flush_check = float(self.params.get("flush_check_s", 60))
        self.bins: dict[int, list[tuple[float, Entity]]] = {}   # накопитель на направление
        self.open_bins = 0            # сколько ячеек занято сейчас
        self.max_open_bins = 0        # пик — столько ячеек нужно физически
        self.ready = simpy.Store(env)  # партии, готовые к упаковке
        self.filled = 0               # выпущено КТЯ
        self.underfilled = 0          # из них недозаполненных (закрыты по таймауту)
        self.items_packed = 0         # товаров в них

        # --- отказ оборудования (сценарии) ---
        self.up = True                 # узел работоспособен
        self._up_event = env.event()   # событие «узел снова в строю»

        # --- статистика ---
        self.processed = 0
        self.produced = 0             # для source-узлов
        self.busy = 0.0               # время обработки
        self.blocked = 0.0            # ожидание на полном выходном буфере
        self.starved = 0.0            # ожидание входных сущностей (нехватка входа)
        self.down = 0.0               # простой из-за отказа узла
        self.processed_at_warmup = 0
        self.busy_at_warmup = 0.0
        self.proc_series: list[int] = []

    def start(self) -> None:
        if self.type == "source":
            return                    # у source-узлов отдельный генератор
        if self.by_direction:
            # упаковка копит товары по направлениям: КТЯ закрывается, когда набрано
            # batch товаров ОДНОГО направления либо истёк таймаут (недозаполненный КТЯ)
            self.env.process(self._bin_feeder())
            if self.flush_timeout > 0:
                self.env.process(self._bin_flusher())
            for wid in range(self.workers):
                self.env.process(self._pack_worker(wid))
            return
        for wid in range(self.workers):
            self.env.process(self._worker(wid))

    # ---- накопители по направлениям ----
    def _batch_size(self) -> int:
        return int(self.inputs.get(self.model.goods_type, 27))

    def _bin_feeder(self):
        """Раскладывает приходящие товары по накопителям направлений."""
        env = self.env
        goods = self.model.goods_type
        batch = self._batch_size()
        store = self.in_stores[goods]
        while True:
            e = yield from self._take(store)
            d = e.dest if e.dest is not None else 0
            b = self.bins.setdefault(d, [])
            if not b:
                self.open_bins += 1
                if self.open_bins > self.max_open_bins:
                    self.max_open_bins = self.open_bins
            b.append((env.now, e))
            if len(b) >= batch:
                items = [en for _, en in b[:batch]]
                del b[:batch]
                if not b:
                    self.open_bins -= 1
                yield self.ready.put((d, items, True))     # КТЯ набран полностью

    def _bin_flusher(self):
        """Закрывает залежавшиеся накопители: хвостовые направления копятся часами,
        их КТЯ уезжает НЕДОЗАПОЛНЕННЫМ — иначе товар не уедет никогда."""
        env = self.env
        while True:
            yield env.timeout(self.flush_check)
            now = env.now
            # сначала собираем залежавшиеся накопители в снимок, потом выпускаем:
            # нельзя yield-ить внутри итерации self.bins — feeder может добавить
            # ключ (новое направление) и уронить цикл "dict changed size".
            due = []
            for d, b in list(self.bins.items()):
                if b and now - b[0][0] >= self.flush_timeout:
                    due.append((d, [en for _, en in b]))
                    b.clear()
                    self.open_bins -= 1
            for d, items in due:
                yield self.ready.put((d, items, False))   # КТЯ недозаполнен

    def _pack_worker(self, wid: int):
        env = self.env
        goods = self.model.goods_type
        box_types = [t for t in self.inputs if t != goods]
        out_type = next(iter(self.det_out), None) or next(iter(self.outputs), "KTY_full")
        while True:
            t = env.now
            self._wstate[wid] = ("starved", t)
            d, items, full = yield self.ready.get()       # готовая партия одного направления
            boxes = []
            for bt in box_types:                          # плюс пустая тара
                for _ in range(int(self.inputs[bt])):
                    boxes.append((yield from self._take(self.in_stores[bt])))
            self.starved += env.now - t

            if not self.up:                      # отказ узла
                t = env.now
                self._wstate[wid] = ("down", t)
                while not self.up:
                    yield self._up_event
                self.down += env.now - t

            t = env.now
            self._wstate[wid] = ("busy", t)
            yield env.timeout(self.services[wid])
            self.busy += env.now - t

            t_rep = min([e.t_created for e in items] + [b.t_created for b in boxes],
                        default=env.now)
            self.filled += 1
            self.items_packed += len(items)
            if not full:
                self.underfilled += 1

            t = env.now
            self._wstate[wid] = ("blocked", t)
            yield from self._emit([Entity(out_type, t_rep, dest=d)])
            self.blocked += env.now - t
            self.processed += 1

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

            # 2) отказ узла: пока не починят, обработка не идёт
            if not self.up:
                t = env.now
                self._wstate[wid] = ("down", t)
                while not self.up:
                    yield self._up_event
                self.down += env.now - t

            # 3) обработка (у каждого исполнителя своя скорость)
            t = env.now
            self._wstate[wid] = ("busy", t)
            yield env.timeout(self.services[wid])
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
            elif state == "down":
                self.down += dt

    def _take(self, store: simpy.Store):
        """Берёт сущность из буфера и, если она ещё в пути по ребру, ждёт прибытия.

        Буфер FIFO, время в пути на ребре постоянное => голова очереди всегда
        готова раньше остальных, поэтому ждать достаточно только её.
        """
        e = yield store.get()
        dt = e.ready_at - self.env.now
        if dt > 0:
            yield self.env.timeout(dt)
        return e

    def _gather(self):
        """Собирает нужное число входных сущностей каждого типа."""
        gathered: dict[str, list[Entity]] = {}
        for etype, qty in self.inputs.items():
            got: list[Entity] = []
            store = self.in_stores[etype]
            for _ in range(int(qty)):
                got.append((yield from self._take(store)))
            gathered[etype] = got
        return gathered

    def _make(self, etype: str, t_rep: float, src_dest: int | None) -> Entity:
        """Создаёт выходную сущность, проставляя направление.

        Товар РОЖДАЕТСЯ с направлением (на вскрытии КТЯ) — оно берётся из профиля
        распределения. Дальше по цепочке направление ПЕРЕНОСИТСЯ вместе с товаром.
        """
        e = Entity(etype, t_rep)
        prof = self.model.directions
        if src_dest is not None:
            e.dest = src_dest                                  # перенос по цепочке
        elif prof is not None and etype == self.model.goods_type:
            e.dest = prof.sample(self.model.rng)               # рождение товара
        return e

    def _transform(self, gathered: dict[str, list[Entity]]):
        """Порождает выходные сущности по правилам outputs."""
        all_ents = [e for lst in gathered.values() for e in lst]
        t_rep = min((e.t_created for e in all_ents), default=self.env.now)
        src_dest = next((e.dest for e in all_ents if e.dest is not None), None)

        # детерминированные выходы: размножение по количеству
        for etype, qty in self.det_out.items():
            for _ in range(qty):
                yield self._make(etype, t_rep, src_dest)

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
            yield self._make(chosen, t_rep, src_dest)

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
            rib = self.model.find_rib(self.id, e.etype, e.dest)
            if rib is None:
                self.model.sink(e.etype).put(self.env.now, e)   # выход системы / брак
            else:
                e.ready_at = self.env.now + rib.travel   # поедет по ребру
                rib.passed += 1
                events.append(rib.store.put(e))   # ставим put в очередь, не ждём здесь
        if not events:
            return
        yield self.env.all_of(events)             # ждём, пока все выходы примут


# ---------------------------------------------------------------------------
# Модель целиком
# ---------------------------------------------------------------------------
class SortingCenterModel:
    def __init__(self, graph: dict, seed: int = 42, warmup_s: float = 300.0,
                 sample_dt: float = 5.0, outages: list[dict] | None = None):
        """graph — КАНОНИЧЕСКИЙ граф (см. graph_loader.normalize).

        outages — отказы оборудования: [{"node": имя, "start_h": ч, "duration_h": ч}].
        """
        self.env = simpy.Environment()
        self.rng = random.Random(seed)
        self.warmup = warmup_s
        self.sample_dt = sample_dt
        self.graph = graph

        # профиль направлений — создаём ДО узлов: узлы на него ссылаются
        dcfg = graph.get("directions") or {}
        self.goods_type = dcfg.get("entity", "Product")
        self.directions = DirectionProfile(
            count=dcfg.get("count", 400),
            top_share=dcfg.get("top_share", 0.2),
            volume_share=dcfg.get("volume_share", 0.8),
            profile=dcfg.get("profile", "pareto"),
            groups=dcfg.get("groups", 0),
            grouping=dcfg.get("grouping", "balanced"),
        ) if dcfg else None

        self.nodes: dict[int, Node] = {
            nid: Node(self.env, cfg, self) for nid, cfg in graph["nodes"].items()
        }
        # пулы мобильных ресурсов (погрузчики, роботы-перевозчики).
        # По концепции они НЕ узлы графа, а ресурс, обслуживающий транспортные рёбра.
        pcfg = graph.get("resource_pools") or {}
        self.pool_cfg = pcfg
        self.pools = {name: simpy.Resource(self.env, capacity=int(c.get("count", 1)))
                      for name, c in pcfg.items()}
        self.pool_busy = {name: 0.0 for name in self.pools}   # время в рейсах
        self.pool_trips = {name: 0 for name in self.pools}    # число рейсов
        self.pool_carried = {name: 0 for name in self.pools}  # перевезено единиц
        self.pool_wait = {name: 0.0 for name in self.pools}   # ожидание свободной единицы

        self.ribs: list[Rib] = [Rib(self.env, c) for c in graph["ribs"]]
        self._rib_by_name = {r.name: r for r in self.ribs}

        # Общий буфер на (узел-приёмник, тип сущности): у узла один вход на тип,
        # но поставщиков может быть несколько (20 секций 2-й стадии -> одна упаковка).
        # Ёмкость общего буфера = сумма ёмкостей входящих рёбер.
        shared: dict[tuple[int, str], simpy.Store] = {}
        caps: dict[tuple[int, str], int] = {}
        for r in self.ribs:
            caps[(r.dst, r.etype)] = caps.get((r.dst, r.etype), 0) + r.capacity
        for key, cap in caps.items():
            shared[key] = simpy.Store(self.env, capacity=cap)
        for r in self.ribs:
            r.dest_store = shared[(r.dst, r.etype)]
            if r.pool:
                # ребро обслуживает погрузчик: узел-источник кладёт в НАКОПИТЕЛЬ,
                # а в буфер приёмника груз попадает только рейсом
                r.store = simpy.Store(self.env, capacity=r.capacity)
                # время рейса считаем по скорости погрузчика, а не конвейера
                sp = float(self.pool_cfg.get(r.pool, {}).get("speed_mps", 0)) or 0.0
                if sp > 0 and r.dist_m:
                    r.travel = r.dist_m / sp
            else:
                r.store = r.dest_store

        # индекс маршрутизации: (узел-источник, тип) -> список рёбер (могут различаться
        # группой направлений)
        self._rib_index: dict[tuple[int, str], list[Rib]] = {}
        for r in self.ribs:
            self._rib_index.setdefault((r.src, r.etype), []).append(r)

        # входные очереди узлов = общие буферы входящих рёбер
        for (dst, etype), store in shared.items():
            if dst in self.nodes:
                self.nodes[dst].in_stores[etype] = store
        # входы без входящего ребра (стартовый узел, source) — синтетический буфер
        for n in self.nodes.values():
            for t in n.inputs:
                if t not in n.in_stores:
                    n.in_stores[t] = simpy.Store(self.env)

        self._sinks: dict[str, Sink] = {}
        self.sink_series: list[dict[str, int]] = []   # снимки выходов по минутам

        self.arrival_rate_h = graph["arrival_rate_h"]
        self.start_node_id = graph.get("start_node_id")
        self.input_type = graph.get("input_type")
        self.output_type = graph.get("type_output")   # терминальная сущность (что отгружаем)
        self.input_stream = graph.get("input_stream", 100000)
        self.generated = 0
        self.outages = outages or []
        self.sim_time = 0.0

    def find_rib(self, src_id: int, etype: str, dest: int | None = None) -> Rib | None:
        """Ребро, по которому уходит сущность.

        Если из узла выходит несколько рёбер одного типа (сортировщик 1-й стадии ->
        20 секций 2-й стадии), выбирается то, чья группа совпадает с группой
        направления товара. Ребро без группы — запасной путь.
        """
        cands = self._rib_index.get((src_id, etype))
        if not cands:
            return None
        if len(cands) == 1:
            return cands[0]
        prof = self.directions
        if dest is not None and prof is not None and prof.group_of:
            g = prof.group_of[dest]
            for r in cands:
                if r.dest_group == g:
                    return r
        for r in cands:
            if r.dest_group is None:
                return r
        return cands[0]

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
                e = Entity(emit, env.now)
                e.ready_at = env.now          # машина стоит у буфера, везти не надо
                yield store.put(e)
                node.produced += 1
            else:
                yield env.timeout(poll)

    # ---- мобильные ресурсы (погрузчики) на рёбрах ----
    def _hauler(self, rib: Rib):
        """Рейс погрузчика: копит партию, берёт свободную единицу из пула,
        едет туда-обратно и выгружает груз в буфер приёмника."""
        env = self.env
        pool = self.pools[rib.pool]
        cfg = self.pool_cfg.get(rib.pool, {})
        handling = float(cfg.get("handling_s", 30))     # погрузка + разгрузка
        while True:
            items = [(yield rib.store.get())]           # ждём первую единицу
            while len(items) < rib.batch and rib.store.items:
                items.append((yield rib.store.get()))   # добираем партию из накопителя
            t0 = env.now
            with pool.request() as req:
                yield req                               # ждём свободный погрузчик
                self.pool_wait[rib.pool] += env.now - t0
                trip = 2.0 * rib.travel + handling      # туда, обратно, погрузка-выгрузка
                yield env.timeout(trip)
                self.pool_busy[rib.pool] += trip
                self.pool_trips[rib.pool] += 1
                self.pool_carried[rib.pool] += len(items)
            for e in items:
                e.ready_at = env.now                    # уже доставлено
                yield rib.dest_store.put(e)

    # ---- отказ оборудования ----
    def _outage(self, node: "Node", start_s: float, duration_s: float):
        """Узел выходит из строя на интервале и потом возвращается в строй."""
        env = self.env
        yield env.timeout(start_s)
        node.up = False
        yield env.timeout(duration_s)
        node.up = True
        ev, node._up_event = node._up_event, env.event()
        ev.succeed()                     # будим всех, кто ждал починки

    # ---- мониторы ----
    def _sampler(self):
        # рёбра могут делить общий буфер — семплируем каждый буфер один раз
        uniq = {}
        for r in self.ribs:
            uniq.setdefault(id(r.store), r)
        while True:
            yield self.env.timeout(self.sample_dt)
            for r in uniq.values():
                r.level_samples.append(len(r.store.items))

    def _minute_series(self):
        """Снимок счётчиков каждую модельную минуту — основа для агрегации
        на интервалах 1 мин / 1 ч / 12 ч / 24 ч (требование критериев)."""
        while True:
            yield self.env.timeout(60.0)
            for n in self.nodes.values():
                # у source-узлов (машина новых КТЯ) счётчик — выработка, а не обработка
                n.proc_series.append(n.produced if n.type == "source" else n.processed)
            self.sink_series.append({k: v.count for k, v in self._sinks.items()})

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
        for r in self.ribs:
            if r.pool and r.pool in self.pools:
                self.env.process(self._hauler(r))
        for o in self.outages:
            target = next((n for n in self.nodes.values() if n.name == o["node"]), None)
            if target is not None:
                self.env.process(self._outage(
                    target, float(o.get("start_h", 0)) * 3600.0,
                    float(o.get("duration_h", 0)) * 3600.0))
        self.env.process(self._sampler())
        self.env.process(self._minute_series())
        self.env.process(self._warmup_snapshot())
        self.sim_time = hours * 3600.0
        self.env.run(until=self.sim_time)
        for n in self.nodes.values():
            n.settle(self.env.now)     # досчитать незавершённое время воркеров
        # финальный снимок счётчиков: сэмплер по минутам не срабатывает на самой
        # границе env.run, поэтому за 24 ч выходит 1439 минут вместо 1440 и агрегаты
        # 12ч/24ч собираются не полностью. Дописываем последнюю минуту вручную.
        for n in self.nodes.values():
            n.proc_series.append(n.produced if n.type == "source" else n.processed)
        self.sink_series.append({k: v.count for k, v in self._sinks.items()})
        return self


def load_graph(path: str, scenario_path: str | None = None) -> dict:
    """Читает файл графа (любая из схем) и приводит к каноническому виду.
    scenario_path — файл со временами обработки и интенсивностью входа."""
    raw = load_json(path)
    scenario = load_json(scenario_path) if scenario_path else None
    return normalize(raw, scenario)
