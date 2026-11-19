"""Hardware abstraction: fans, temperature sensors, and a clock.

Everything above this layer talks to these small interfaces, so the same
calibration and control logic runs against real Linux hwmon devices or the
simulated rig in ``sim.py``.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


class Fan(Protocol):
    name: str

    def set_duty(self, percent: float) -> None: ...
    def read_rpm(self) -> float: ...
    def take_control(self) -> None: ...
    def release_control(self) -> None: ...


class Sensor(Protocol):
    name: str

    def read_celsius(self) -> float: ...


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class RealClock:
    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


# --------------------------------------------------------------------------
# Linux hwmon backend
#
# Motherboard fan headers (and AMD GPUs) appear under /sys/class/hwmon as
#   pwmN         0-255 duty
#   pwmN_enable  1 = manual, 2+ = automatic (driver/firmware specific)
#   fanN_input   RPM
#   tempN_input  millidegrees C
# Writing requires root. NVIDIA GPU fans are not exposed here; their arm can
# be left unmanaged or wired up through a custom Fan implementation.
# --------------------------------------------------------------------------


def _read_int(path: Path) -> int:
    return int(path.read_text().strip())


def _write_int(path: Path, value: int) -> None:
    path.write_text(f"{value}\n")


@dataclass
class HwmonFan:
    name: str
    pwm_path: Path
    rpm_path: Path

    def __post_init__(self) -> None:
        self.pwm_path = Path(self.pwm_path)
        self.rpm_path = Path(self.rpm_path)
        self._enable_path = self.pwm_path.with_name(self.pwm_path.name + "_enable")
        self._saved_enable: int | None = None

    def take_control(self) -> None:
        if self._enable_path.exists():
            self._saved_enable = _read_int(self._enable_path)
            _write_int(self._enable_path, 1)

    def release_control(self) -> None:
        if self._saved_enable is not None and self._enable_path.exists():
            _write_int(self._enable_path, self._saved_enable)

    def set_duty(self, percent: float) -> None:
        percent = max(0.0, min(100.0, percent))
        _write_int(self.pwm_path, round(percent * 255 / 100))

    def read_rpm(self) -> float:
        return float(_read_int(self.rpm_path))


@dataclass
class HwmonSensor:
    name: str
    path: Path

    def __post_init__(self) -> None:
        self.path = Path(self.path)

    def read_celsius(self) -> float:
        return _read_int(self.path) / 1000.0


def list_hwmon(root: Path = Path("/sys/class/hwmon")) -> list[dict]:
    """Enumerate hwmon chips with their pwm/fan/temp channels (for config setup)."""
    chips = []
    for chip in sorted(root.glob("hwmon*")):
        name_file = chip / "name"
        chips.append(
            {
                "path": str(chip),
                "name": name_file.read_text().strip() if name_file.exists() else "?",
                "pwm": sorted(p.name for p in chip.glob("pwm[0-9]")),
                "fans": sorted(p.name for p in chip.glob("fan*_input")),
                "temps": sorted(p.name for p in chip.glob("temp*_input")),
            }
        )
    return chips
