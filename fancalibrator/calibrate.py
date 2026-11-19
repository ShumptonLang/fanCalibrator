"""End-to-end calibration and profile (de)serialization.

Phases:
  1. characterize every fan at idle (PWM->RPM, stall, start)
  2. measure idle temps with every arm at its floor
  3. start the sustained load (CPU + GPU together, so the case arm sees the
     real combined heat), then run the three-arm optimizer
  4. stop the load, build curves, return a profile
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

from .characterize import CharacterizeConfig, FanProfile, characterize_fan
from .controller import ArmState
from .curves import Curve, build_curve
from .hardware import Clock, Fan, Sensor
from .optimize import Arm, ArmOptimizer, OptimizeConfig, OptimizeResult, knee_report
from .steady import SettleConfig, wait_steady


@dataclass
class Setup:
    fans: dict[str, Fan]
    sensors: dict[str, Sensor]
    arms: list[Arm]
    targets: dict[str, float]
    clock: Clock
    fan_max_db: dict[str, float] = field(default_factory=dict)
    start_load: Callable[[], None] = lambda: None
    stop_load: Callable[[], None] = lambda: None


@dataclass
class Profile:
    fans: dict[str, FanProfile]
    arms: list[Arm]
    targets: dict[str, float]
    idle_temps: dict[str, float]
    result: OptimizeResult
    curves: dict[str, dict[str, Curve]]       # arm -> sensor -> curve

    def to_dict(self) -> dict:
        return {
            "fans": {n: p.to_dict() for n, p in self.fans.items()},
            "arms": [vars(a) for a in self.arms],
            "targets": self.targets,
            "idle_temps": self.idle_temps,
            "result": self.result.to_dict(),
            "knee": knee_report(self.result),
            "curves": {a: {s: c.points for s, c in cs.items()} for a, cs in self.curves.items()},
        }

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2))

    @staticmethod
    def load_arm_states(path: str | Path) -> list[ArmState]:
        d = json.loads(Path(path).read_text())
        fans = {n: FanProfile.from_dict(p) for n, p in d["fans"].items()}
        states = []
        for a in d["arms"]:
            curves = {s: Curve([tuple(p) for p in pts]) for s, pts in d["curves"][a["name"]].items()}
            start = max((fans[f].start_duty or 0.0) for f in a["fans"])
            states.append(ArmState(a["name"], a["fans"], curves, start_duty=start))
        return states


def make_curves(
    arms: list[Arm],
    fans: Mapping[str, FanProfile],
    targets: Mapping[str, float],
    idle: Mapping[str, float],
    result: OptimizeResult,
    floor_margin: float,
) -> dict[str, dict[str, Curve]]:
    out: dict[str, dict[str, Curve]] = {}
    for arm in arms:
        floor = max(fans[f].floor_duty(floor_margin) for f in arm.fans)
        out[arm.name] = {
            s: build_curve(
                idle_temp=idle[s],
                load_temp=result.temps[s],
                target=targets[s],
                floor=floor,
                opt_duty=result.duties[arm.name],
                allow_stop=arm.allow_stop,
            )
            for s in arm.controls
        }
    return out


def calibrate(
    setup: Setup,
    settle: SettleConfig = SettleConfig(),
    char_cfg: CharacterizeConfig = CharacterizeConfig(),
    opt_cfg: OptimizeConfig = OptimizeConfig(),
    log: Callable[[str], None] = print,
) -> Profile:
    for f in setup.fans.values():
        f.take_control()
    try:
        for f in setup.fans.values():
            f.set_duty(100)

        log("== phase 1: characterizing fans (idle)")
        profiles = {
            n: characterize_fan(f, setup.clock, setup.sensors, char_cfg, log)
            for n, f in setup.fans.items()
        }

        log("== phase 2: idle temperatures at floor speeds")
        for arm in setup.arms:
            floor = max(profiles[f].floor_duty(opt_cfg.floor_margin) for f in arm.fans)
            for f in arm.fans:
                setup.fans[f].set_duty(floor)
        idle, _ = wait_steady(setup.sensors, setup.clock, settle)
        log("   idle: " + ", ".join(f"{k}={v:.1f}" for k, v in idle.items()))

        log("== phase 3: sustained load, three-arm search")
        for f in setup.fans.values():
            f.set_duty(100)
        setup.start_load()
        try:
            opt = ArmOptimizer(
                setup.arms, setup.fans, profiles, setup.sensors, setup.targets,
                setup.clock, settle, opt_cfg, setup.fan_max_db, log,
            )
            result = opt.run()
        finally:
            setup.stop_load()

        curves = make_curves(setup.arms, profiles, setup.targets, idle, result, opt_cfg.floor_margin)
        return Profile(profiles, setup.arms, setup.targets, idle, result, curves)
    finally:
        for f in setup.fans.values():
            f.set_duty(100)
            f.release_control()


class ShellLoad:
    """Runs a load command (e.g. stress-ng / Prime95 plus a GPU stress tool) in
    its own process group so the whole tree can be stopped afterwards."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.proc: subprocess.Popen | None = None

    def start(self) -> None:
        if os.name == "nt":
            self.proc = subprocess.Popen(
                self.command, shell=True,
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,  # type: ignore[attr-defined]
            )
        else:
            self.proc = subprocess.Popen(self.command, shell=True, start_new_session=True)

    def stop(self) -> None:
        if not self.proc or self.proc.poll() is not None:
            return
        if os.name == "nt":
            subprocess.run(["taskkill", "/T", "/F", "/PID", str(self.proc.pid)], capture_output=True)
            return
        os.killpg(self.proc.pid, signal.SIGTERM)
        try:
            self.proc.wait(10)
        except subprocess.TimeoutExpired:
            os.killpg(self.proc.pid, signal.SIGKILL)
