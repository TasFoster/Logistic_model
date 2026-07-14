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
                 volume_share: float = 0.8, profile: str = "pareto"):
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
        return bisect.bisect_left(self._cum, rng.random())

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
