"""
Профиль распределения товаров по направлениям отправки.

Из постановки задачи: направлений 400, и они НЕРАВНОЗНАЧНЫ по объёму —
«на 20% направлений может приходиться до 80% общего объёма», а на хвостовые
направления — «всего несколько товаров в час».

Реализация: степенной закон (Zipf) w(i) = 1 / (i+1)^alpha. Показатель alpha
подбирается численно так, чтобы верхние top_share направлений забирали
volume_share объёма (по умолчанию 20% -> 80%). Это даёт настоящий длинный хвост,
а не просто две ступеньки: у последних направлений действительно единицы товаров в час.

Именно хвост создаёт физику, которую требует задача: КТЯ хвостового направления
набивается часами, стоит на накопителе недозаполненным и занимает ячейку.
"""

from __future__ import annotations

import bisect
import itertools
import random


class DirectionProfile:
    def __init__(self, count: int = 400, top_share: float = 0.2,
                 volume_share: float = 0.8, profile: str = "pareto",
                 groups: int = 0, grouping: str = "balanced"):
        self.count = int(count)
        self.profile = profile
        self.top_share = top_share
        self.volume_share = volume_share

        if profile == "uniform":
            self.alpha = 0.0
            weights = [1.0] * self.count
        else:
            self.alpha = self._fit_alpha(self.count, top_share, volume_share)
            weights = [1.0 / ((i + 1) ** self.alpha) for i in range(self.count)]

        total = sum(weights)
        self.probs = [w / total for w in weights]
        self._cum = list(itertools.accumulate(self.probs))

        # --- разбиение на группы для двухстадийной сортировки (20 x 20 = 400) ---
        self.groups = int(groups)
        self.grouping = grouping
        self.group_of: list[int] = []
        if self.groups > 0:
            self.group_of = self._assign_groups()

    def _assign_groups(self) -> list[int]:
        """Раскладывает направления по группам первой стадии.

        sequential — подряд (0-19 в группу 0, 20-39 в группу 1 и т.д.). Наивно и
        плохо: из-за Парето в группу 0 попадают самые жирные направления, и первая
        секция второй стадии перегружена, а последняя простаивает.

        balanced — «змейкой» по убыванию объёма: направления раскладываются по
        группам так, чтобы суммарный объём групп был примерно равен. Так вторая
        стадия загружена равномерно.
        """
        g = [0] * self.count
        if self.grouping == "sequential":
            size = self.count // self.groups
            for d in range(self.count):
                g[d] = min(d // size, self.groups - 1)
            return g
        # balanced: направления уже отсортированы по убыванию объёма (probs убывают)
        order = sorted(range(self.count), key=lambda d: -self.probs[d])
        for rank, d in enumerate(order):
            cycle, pos = divmod(rank, self.groups)
            # змейка: чётный проход слева направо, нечётный — справа налево
            g[d] = pos if cycle % 2 == 0 else self.groups - 1 - pos
        return g

    def group_volume_shares(self) -> list[float]:
        """Доля объёма, приходящаяся на каждую группу первой стадии."""
        shares = [0.0] * self.groups
        for d in range(self.count):
            shares[self.group_of[d]] += self.probs[d]
        return shares

    # --- подбор alpha под заданное соотношение 20/80 ---
    @staticmethod
    def _top_volume(count: int, alpha: float, top_share: float) -> float:
        w = [1.0 / ((i + 1) ** alpha) for i in range(count)]
        total = sum(w)
        k = max(1, int(round(count * top_share)))
        return sum(w[:k]) / total

    @classmethod
    def _fit_alpha(cls, count: int, top_share: float, volume_share: float) -> float:
        lo, hi = 0.0, 5.0
        for _ in range(60):                      # бисекция: доля растёт по alpha
            mid = (lo + hi) / 2
            if cls._top_volume(count, mid, top_share) < volume_share:
                lo = mid
            else:
                hi = mid
        return (lo + hi) / 2

    # --- выборка направления ---
    def sample(self, rng: random.Random) -> int:
        # зажим: из-за плавающей точки сумма вероятностей может быть чуть < 1.0,
        # и тогда bisect для r близкого к 1 вернул бы count (выход за границу).
        return min(bisect.bisect_left(self._cum, rng.random()), self.count - 1)

    # --- справка для отчёта ---
    def share_of_top(self, top_share: float | None = None) -> float:
        k = max(1, int(round(self.count * (top_share or self.top_share))))
        return sum(self.probs[:k])

    def items_per_hour(self, direction: int, total_per_hour: float) -> float:
        return self.probs[direction] * total_per_hour

    def describe(self, total_per_hour: float) -> str:
        top = 100 * self.share_of_top()
        first = self.items_per_hour(0, total_per_hour)
        last = self.items_per_hour(self.count - 1, total_per_hour)
        return (f"{self.count} направлений, профиль {self.profile} (alpha={self.alpha:.2f}): "
                f"верхние {int(100*self.top_share)}% забирают {top:.1f}% объёма; "
                f"1-е направление ~{first:.0f} тов/ч, последнее ~{last:.1f} тов/ч")
