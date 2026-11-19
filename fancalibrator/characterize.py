"""Per-fan characterization: PWM -> RPM curve, stall duty and start duty.

Run at idle. Each fan is swept on its own while the others stay at 100%.

* stall duty: lowest duty at which an already-spinning fan keeps spinning
  (sweep downward until RPM reads 0).
* start duty: lowest duty that gets a stopped fan turning again (sweep upward
  from 0). This is usually noticeably higher than the stall duty, which is why
  a zero-RPM curve needs a "kick" when it restarts a fan.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Mapping

from .hardware import Clock, Fan, Sensor


@dataclass
class FanProfile:
    name: str
    max_rpm: float
    stall_duty: float | None          # None: fan never stopped, even at 0%
    start_duty: float | None
    curve: list[tuple[float, float]] = field(default_factory=list)  # (duty, rpm)

    def floor_duty(self, margin: float = 5.0) -> float:
        """Lowest duty that's safe to run continuously."""
        if self.stall_duty is None:
            return 0.0
        return min(100.0, self.stall_duty + margin)

    def rpm_at(self, duty: float) -> float:
        """Interpolated RPM for a duty, assuming the fan is spinning."""
        pts = sorted(p for p in self.curve if p[1] > 0)
        if not pts:
            return 0.0
        if duty <= pts[0][0]:
            return pts[0][1]
        for (d0, r0), (d1, r1) in zip(pts, pts[1:]):
            if d0 <= duty <= d1:
                return r0 + (r1 - r0) * (duty - d0) / (d1 - d0) if d1 > d0 else r1
        return pts[-1][1]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "FanProfile":
        d = dict(d)
        d["curve"] = [tuple(p) for p in d.get("curve", [])]
        return cls(**d)


@dataclass
class CharacterizeConfig:
    step: float = 5.0           # duty step for the downward sweep (%)
    start_step: float = 2.0     # duty step for the upward start search (%)
    spin_settle: float = 4.0    # s to let RPM settle after a change
    stop_wait: float = 10.0     # s to let a fan spin down fully
    safety_temp: float = 80.0   # abort + restore if any sensor exceeds this


class CharacterizeAbort(RuntimeError):
    pass


def _avg_rpm(fan: Fan, clock: Clock, n: int = 3) -> float:
    total = 0.0
    for _ in range(n):
        total += fan.read_rpm()
        clock.sleep(0.5)
    return total / n


def characterize_fan(
    fan: Fan,
    clock: Clock,
    sensors: Mapping[str, Sensor] | None = None,
    cfg: CharacterizeConfig = CharacterizeConfig(),
    log=print,
) -> FanProfile:
    def check_safe() -> None:
        for n, s in (sensors or {}).items():
            t = s.read_celsius()
            if t > cfg.safety_temp:
                fan.set_duty(100)
                raise CharacterizeAbort(f"{n} at {t:.1f} C while characterizing {fan.name}")

    fan.set_duty(100)
    clock.sleep(cfg.spin_settle * 2)
    curve: list[tuple[float, float]] = []
    stall: float | None = None

    duty = 100.0
    while duty >= 0:
        fan.set_duty(duty)
        clock.sleep(cfg.spin_settle)
        check_safe()
        rpm = _avg_rpm(fan, clock)
        curve.append((duty, rpm))
        log(f"  {fan.name}: {duty:5.1f}% -> {rpm:6.0f} rpm")
        if rpm <= 0:
            stall = curve[-2][0] if len(curve) > 1 else 100.0
            break
        duty = round(duty - cfg.step, 3)

    start: float | None = None
    if stall is not None:
        # Refine the stall point between the last spinning step and the stop.
        fan.set_duty(stall)
        clock.sleep(cfg.spin_settle * 2)
        d = stall
        while d > 0:
            nd = round(d - cfg.start_step, 3)
            fan.set_duty(nd)
            clock.sleep(cfg.spin_settle)
            if _avg_rpm(fan, clock) <= 0:
                break
            d = nd
        stall = d

        # Find the start duty from a full stop.
        fan.set_duty(0)
        clock.sleep(cfg.stop_wait)
        d = 0.0
        while d <= 100:
            fan.set_duty(d)
            clock.sleep(cfg.spin_settle)
            check_safe()
            if _avg_rpm(fan, clock) > 0:
                start = d
                break
            d = round(d + cfg.start_step, 3)

    fan.set_duty(100)
    max_rpm = max(r for _, r in curve) if curve else 0.0
    prof = FanProfile(fan.name, max_rpm, stall, start, sorted(curve))
    log(f"  {fan.name}: max {max_rpm:.0f} rpm, stall {stall}%, start {start}%")
    return prof
