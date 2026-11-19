"""A simulated PC so calibration and control can be developed and tested
without hardware.

Thermal model (steady state, first-order dynamics toward it):

    T_case = T_ambient + (P_cpu + P_gpu) / G_case(case airflow)
    T_cpu  = T_case    +  P_cpu          / G_cpu(cpu fan airflow)
    T_gpu  = T_case    +  P_gpu          / G_gpu(gpu fan airflow)

The case fans are the "third arm": they don't cool either chip directly, but
they lower the intake air temperature both coolers work from.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


class SimClock:
    def __init__(self) -> None:
        self.t = 0.0
        self._listeners: list = []

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        step = 1.0
        remaining = seconds
        while remaining > 1e-9:
            dt = min(step, remaining)
            self.t += dt
            remaining -= dt
            for fn in self._listeners:
                fn(dt)


@dataclass
class SimFan:
    name: str
    max_rpm: float = 1800.0
    min_rpm: float = 400.0      # RPM right at the stall duty
    stall_duty: float = 18.0    # below this a spinning fan stops
    start_duty: float = 28.0    # a stopped fan needs at least this to start
    duty: float = 100.0
    spinning: bool = True
    managed: bool = False

    def take_control(self) -> None:
        self.managed = True

    def release_control(self) -> None:
        self.managed = False

    def set_duty(self, percent: float) -> None:
        self.duty = max(0.0, min(100.0, percent))
        if self.spinning and self.duty < self.stall_duty:
            self.spinning = False
        elif not self.spinning and self.duty >= self.start_duty:
            self.spinning = True

    def read_rpm(self) -> float:
        if not self.spinning:
            return 0.0
        frac = (self.duty - self.stall_duty) / (100.0 - self.stall_duty)
        return self.min_rpm + (self.max_rpm - self.min_rpm) * max(0.0, frac)

    def airflow(self) -> float:
        return self.read_rpm() / self.max_rpm  # 0..1


@dataclass
class SimSensor:
    name: str
    rig: "SimRig"
    noise: float = 0.15

    def read_celsius(self) -> float:
        return self.rig.temps[self.name] + random.gauss(0.0, self.noise)


@dataclass
class SimRig:
    clock: SimClock = field(default_factory=SimClock)
    ambient: float = 24.0
    cpu_power_idle: float = 25.0
    cpu_power_load: float = 150.0
    gpu_power_idle: float = 20.0
    gpu_power_load: float = 250.0
    loaded: bool = False

    def __post_init__(self) -> None:
        self.fans = {
            "cpu_fan": SimFan("cpu_fan", max_rpm=2000, stall_duty=15, start_duty=25),
            "gpu_fan": SimFan("gpu_fan", max_rpm=3000, stall_duty=20, start_duty=30, min_rpm=700),
            "case_front": SimFan("case_front", max_rpm=1500, stall_duty=20, start_duty=30),
            "case_rear": SimFan("case_rear", max_rpm=1500, stall_duty=22, start_duty=32),
        }
        self.temps = {"cpu": self.ambient + 10, "gpu": self.ambient + 8, "case": self.ambient + 3}
        self.sensors = {n: SimSensor(n, self) for n in self.temps}
        self.clock._listeners.append(self._step)

    # -- model -------------------------------------------------------------
    def _targets(self) -> dict[str, float]:
        p_cpu = self.cpu_power_load if self.loaded else self.cpu_power_idle
        p_gpu = self.gpu_power_load if self.loaded else self.gpu_power_idle
        case_air = (self.fans["case_front"].airflow() + self.fans["case_rear"].airflow()) / 2
        g_case = 4.0 + 26.0 * case_air ** 0.8          # W/K
        g_cpu = 2.2 + 3.8 * self.fans["cpu_fan"].airflow() ** 0.7
        g_gpu = 3.0 + 6.0 * self.fans["gpu_fan"].airflow() ** 0.7
        t_case = self.ambient + (p_cpu + p_gpu) / g_case
        return {"case": t_case, "cpu": t_case + p_cpu / g_cpu, "gpu": t_case + p_gpu / g_gpu}

    _tau = {"case": 90.0, "cpu": 25.0, "gpu": 45.0}  # seconds

    def _step(self, dt: float) -> None:
        targets = self._targets()
        for k, target in targets.items():
            a = min(1.0, dt / self._tau[k])
            self.temps[k] += (target - self.temps[k]) * a

    def steady_temps(self) -> dict[str, float]:
        return self._targets()
