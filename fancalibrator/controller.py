"""Runtime fan control from a calibrated profile.

Per arm, every tick:
  * smooth each controlling sensor with an exponential moving average, so a
    one-second CPU spike doesn't spin the fans up;
  * rising demand = max over sensors of curve_s(T_s);
    falling demand = max over sensors of curve_s(T_s + hysteresis), so the
    fans only slow down once temps have dropped clearly below where they rose;
  * limit how fast duty moves in each direction (fast up, slow down);
  * when a stopped arm (zero-RPM) needs to start, kick it at its start duty
    for a few seconds;
  * any sensor read failure or reading at/above ``panic_temp`` -> 100%.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping

from .curves import Curve
from .hardware import Clock, Fan, Sensor


@dataclass
class ControlConfig:
    period: float = 1.0          # s between ticks
    smoothing: float = 6.0       # EMA time constant, s
    hysteresis: float = 3.0      # deg C
    up_rate: float = 20.0        # max % per second when speeding up
    down_rate: float = 2.0       # max % per second when slowing down
    kick_time: float = 3.0       # s at start duty when restarting a stopped arm
    panic_temp: float = 95.0


@dataclass
class ArmState:
    name: str
    fans: list[str]
    curves: dict[str, Curve]      # sensor -> curve
    start_duty: float = 0.0       # highest start duty of the arm's fans
    duty: float = 100.0
    kick_until: float = -1.0
    stopped: bool = False


@dataclass
class Controller:
    arms: list[ArmState]
    fans: Mapping[str, Fan]
    sensors: Mapping[str, Sensor]
    clock: Clock
    cfg: ControlConfig = field(default_factory=ControlConfig)
    smoothed: dict[str, float] = field(default_factory=dict)

    def _read(self) -> dict[str, float] | None:
        try:
            return {n: s.read_celsius() for n, s in self.sensors.items()}
        except (OSError, ValueError):
            return None

    def tick(self, dt: float) -> dict[str, float]:
        raw = self._read()
        now = self.clock.now()
        if raw is None or any(v >= self.cfg.panic_temp for v in raw.values()):
            for arm in self.arms:
                self._set(arm, 100.0)
                arm.stopped = False
            return {a.name: 100.0 for a in self.arms}

        alpha = min(1.0, dt / self.cfg.smoothing) if self.cfg.smoothing > 0 else 1.0
        for n, v in raw.items():
            prev = self.smoothed.get(n, v)
            self.smoothed[n] = prev + (v - prev) * alpha

        for arm in self.arms:
            up = max(c(self.smoothed[s]) for s, c in arm.curves.items())
            down = max(c(self.smoothed[s] + self.cfg.hysteresis) for s, c in arm.curves.items())
            cur = arm.duty
            if up > cur:
                new = min(up, cur + self.cfg.up_rate * dt)
            elif down < cur:
                new = max(down, cur - self.cfg.down_rate * dt)
            else:
                new = cur

            if arm.stopped and new > 0:
                arm.kick_until = now + self.cfg.kick_time
                arm.stopped = False
            if new <= 0:
                arm.stopped = True
            out = max(new, arm.start_duty) if now < arm.kick_until else new
            arm.duty = new
            self._set(arm, out)
        return {a.name: a.duty for a in self.arms}

    def _set(self, arm: ArmState, duty: float) -> None:
        for f in arm.fans:
            self.fans[f].set_duty(duty)

    def run(self, duration: float | None = None, on_tick: Callable | None = None) -> None:
        start = self.clock.now()
        for f in self.fans.values():
            f.take_control()
        try:
            while duration is None or self.clock.now() - start < duration:
                duties = self.tick(self.cfg.period)
                if on_tick:
                    on_tick(dict(self.smoothed), duties)
                self.clock.sleep(self.cfg.period)
        finally:
            for f in self.fans.values():
                f.set_duty(100)
                f.release_control()
