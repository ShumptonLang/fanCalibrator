"""Wait for temperatures to reach steady state instead of sleeping a fixed time.

A point is "settled" when the least-squares slope of every watched sensor over
the trailing window is below ``max_slope`` (deg C per minute). The reported
temperature is the window mean, which averages out sensor jitter.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Callable, Mapping

from .hardware import Clock, Sensor


class OverTemperature(RuntimeError):
    pass


@dataclass
class SettleConfig:
    sample_period: float = 2.0     # s
    window: float = 60.0           # s of history used for slope + mean
    max_slope: float = 0.15        # deg C / min
    min_time: float = 60.0         # s, never accept earlier than this
    max_time: float = 900.0        # s, give up and take what we have
    abort_temp: float = 95.0       # any sensor above this -> abort


def _slope(points: list[tuple[float, float]]) -> float:
    n = len(points)
    mt = sum(t for t, _ in points) / n
    mv = sum(v for _, v in points) / n
    num = sum((t - mt) * (v - mv) for t, v in points)
    den = sum((t - mt) ** 2 for t, _ in points)
    return num / den if den else 0.0


def wait_steady(
    sensors: Mapping[str, Sensor],
    clock: Clock,
    cfg: SettleConfig = SettleConfig(),
    on_sample: Callable[[dict[str, float]], None] | None = None,
) -> tuple[dict[str, float], bool]:
    """Returns (mean temps over the final window, settled_flag)."""
    maxlen = max(3, int(cfg.window / cfg.sample_period))
    hist = {n: deque(maxlen=maxlen) for n in sensors}
    start = clock.now()
    while True:
        t = clock.now() - start
        reading = {n: s.read_celsius() for n, s in sensors.items()}
        if on_sample:
            on_sample(reading)
        for n, v in reading.items():
            if v >= cfg.abort_temp:
                raise OverTemperature(f"{n} reached {v:.1f} C (abort at {cfg.abort_temp})")
            hist[n].append((t, v))

        full = all(len(h) == maxlen for h in hist.values())
        if full and t >= cfg.min_time:
            slopes = {n: abs(_slope(list(h))) * 60 for n, h in hist.items()}
            if all(s <= cfg.max_slope for s in slopes.values()):
                return _means(hist), True
        if t >= cfg.max_time:
            return _means(hist), False
        clock.sleep(cfg.sample_period)


def _means(hist) -> dict[str, float]:
    return {n: sum(v for _, v in h) / len(h) for n, h in hist.items()}
