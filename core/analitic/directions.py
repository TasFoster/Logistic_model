"""
Профиль распределения по 400 направлениям — для аналитики.

Нужен для двухстадийной сортировки: узел 1-й стадии раскидывает товар по 20 группам
направлений НЕ поровну, а по доле объёма группы. Из-за Парето (верхние 20%
направлений забирают ~80% объёма) одна «жирная» группа берёт кратно больше —
поэтому и мощности секций 2-й стадии неодинаковы.

Это ПОРТ существенной математики из core/simulator/directions.py (степенной закон
Zipf + подбор alpha под 20/80 + балансировка «змейкой»), но без зависимостей.
Аналитике нужны только доли объёма по группам, полного семплирования не требуется.

TODO(Этап 6): сверить group_volume_shares() с DirectionProfile напарника, чтобы
гарантировать, что обе модели используют идентичные доли (иначе разойдутся числа
2-й стадии). Порт написан построчно по оригиналу именно ради этого.
"""

from __future__ import annotations


def _fit_alpha(count: int, top_share: float, volume_share: float) -> float:
    """Подбор показателя степени так, чтобы верхние top_share направлений забирали
    volume_share объёма (бисекция: доля монотонно растёт по alpha)."""
    def top_volume(alpha: float) -> float:
        w = [1.0 / ((i + 1) ** alpha) for i in range(count)]
        total = sum(w)
        k = max(1, int(round(count * top_share)))
        return sum(w[:k]) / total

    lo, hi = 0.0, 5.0
    for _ in range(60):
        mid = (lo + hi) / 2
        if top_volume(mid) < volume_share:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2


class DirectionProfile:
    def __init__(self, count: int = 400, top_share: float = 0.2,
                 volume_share: float = 0.8, profile: str = "pareto",
                 groups: int = 0, grouping: str = "balanced"):
        self.count = int(count)
        self.groups = int(groups)
        self.grouping = grouping
        if profile == "uniform":
            self.alpha = 0.0
            weights = [1.0] * self.count
        else:
            self.alpha = _fit_alpha(self.count, top_share, volume_share)
            weights = [1.0 / ((i + 1) ** self.alpha) for i in range(self.count)]
        total = sum(weights)
        self.probs = [w / total for w in weights]     # доли объёма, по убыванию
        self.group_of = self._assign_groups() if self.groups > 0 else []

    def _assign_groups(self) -> list[int]:
        """Раскладка направлений по группам 1-й стадии.

        sequential — подряд (наивно, из-за Парето перекос);
        balanced — «змейкой» по убыванию объёма (группы примерно равны, насколько
        позволяет неделимость самого жирного направления).
        """
        g = [0] * self.count
        if self.grouping == "sequential":
            size = self.count // self.groups
            for d in range(self.count):
                g[d] = min(d // size, self.groups - 1)
            return g
        order = sorted(range(self.count), key=lambda d: -self.probs[d])
        for rank, d in enumerate(order):
            cycle, pos = divmod(rank, self.groups)
            g[d] = pos if cycle % 2 == 0 else self.groups - 1 - pos
        return g

    def group_volume_shares(self) -> list[float]:
        """Доля общего объёма, приходящаяся на каждую группу 1-й стадии."""
        shares = [0.0] * self.groups
        for d in range(self.count):
            shares[self.group_of[d]] += self.probs[d]
        return shares

    def share_of_top(self, top_share: float = 0.2) -> float:
        k = max(1, int(round(self.count * top_share)))
        return sum(sorted(self.probs, reverse=True)[:k])

    def describe(self, total_per_hour: float) -> str:
        top = 100 * self.share_of_top()
        first = self.probs[0] * total_per_hour
        last = self.probs[-1] * total_per_hour
        return (f"{self.count} направлений (alpha={self.alpha:.2f}): верхние 20% берут "
                f"{top:.1f}% объёма; 1-е ~{first:.0f} тов/ч, последнее ~{last:.1f} тов/ч")
