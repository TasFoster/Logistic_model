"""
Минимальная демонстрация БЛОКИРОВКИ на SimPy — критерий готовности прототипа.

Цепочка: источник -> ограниченный буфер -> медленная обработка -> сток.
Источник производит быстрее, чем обработка успевает разгребать => буфер
заполняется до предела => put() источника приостанавливается => источник встаёт.

Запуск:  python -m core.simulator.demo_blocking
"""

from __future__ import annotations

import sys

import simpy

try:  # корректный вывод кириллицы в консоли Windows
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

BUFFER_CAPACITY = 5
SOURCE_INTERVAL = 1.0   # источник кладёт 1 деталь в секунду
SERVICE_TIME = 3.0      # обработка одной детали — 3 секунды (втрое медленнее)
SIM_TIME = 30.0


def source(env: simpy.Environment, buf: simpy.Store, log: list):
    i = 0
    while True:
        yield env.timeout(SOURCE_INTERVAL)
        i += 1
        item = f"#{i}"
        t0 = env.now
        yield buf.put(item)                 # ждёт, если буфер полон
        wait = env.now - t0
        if wait > 0:
            log.append((round(t0, 1), f"источник ЗАБЛОКИРОВАН на {wait:.1f} с "
                                      f"(буфер полон), деталь {item}"))
        else:
            log.append((round(env.now, 1), f"источник -> буфер: {item} "
                                           f"(в буфере {len(buf.items)}/{BUFFER_CAPACITY})"))


def worker(env: simpy.Environment, buf: simpy.Store, log: list):
    while True:
        item = yield buf.get()
        yield env.timeout(SERVICE_TIME)
        log.append((round(env.now, 1), f"обработана {item} "
                                       f"(в буфере {len(buf.items)}/{BUFFER_CAPACITY})"))


def main() -> None:
    env = simpy.Environment()
    buf = simpy.Store(env, capacity=BUFFER_CAPACITY)
    log: list = []
    env.process(source(env, buf, log))
    env.process(worker(env, buf, log))
    env.run(until=SIM_TIME)

    print(f"Буфер={BUFFER_CAPACITY}, источник каждые {SOURCE_INTERVAL} с, "
          f"обработка {SERVICE_TIME} с/шт\n")
    for t, msg in sorted(log):
        print(f"[{t:>5}] {msg}")

    blocked = sum(1 for _, m in log if "ЗАБЛОКИРОВАН" in m)
    print(f"\nСобытий блокировки источника: {blocked} "
          f"(ожидаемо >0 — механика буфера воспроизведена).")


if __name__ == "__main__":
    main()
