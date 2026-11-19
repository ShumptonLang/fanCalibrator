"""Turn a calibration into fan curves, one per (arm, controlling sensor).

For each arm and each sensor it watches:

    duty
    100 |                         ___________
        |                        /
    opt |               ________/            <- reaches the optimized duty at the
        |          ____/                       measured full-load temperature,
  floor |_________/                            then ramps hard to 100% by
        +---------+-------------+---+------    target + full_speed_offset
                idle+2       load  target

Below the idle point the arm sits at its floor (or 0 if ``allow_stop``).
An arm watching several sensors (the case fans watch CPU *and* GPU) takes the
highest duty any of its curves asks for.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Curve:
    points: list[tuple[float, float]]     # (temp C, duty %), temps strictly increasing

    def __call__(self, temp: float) -> float:
        pts = self.points
        if temp <= pts[0][0]:
            return pts[0][1]
        for (t0, d0), (t1, d1) in zip(pts, pts[1:]):
            if temp <= t1:
                return d0 + (d1 - d0) * (temp - t0) / (t1 - t0)
        return pts[-1][1]


def build_curve(
    idle_temp: float,
    load_temp: float,
    target: float,
    floor: float,
    opt_duty: float,
    allow_stop: bool = False,
    idle_offset: float = 2.0,
    full_speed_offset: float = 3.0,
) -> Curve:
    t_idle = idle_temp + idle_offset
    t_full = target + full_speed_offset
    # Keep the temperatures strictly increasing, even when idle and load are
    # close together (e.g. the GPU arm while the GPU was barely loaded).
    t_load = max(load_temp, t_idle + 1.0)
    t_full = max(t_full, t_load + 1.0)
    opt_duty = max(opt_duty, floor)
    pts: list[tuple[float, float]] = []
    if allow_stop:
        pts.append((t_idle - 0.01, 0.0))
    pts += [(t_idle, floor), (t_load, opt_duty), (t_full, 100.0)]
    return Curve(pts)
