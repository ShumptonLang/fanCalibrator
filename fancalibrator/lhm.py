"""Windows backend: LibreHardwareMonitor via pythonnet.

FanControl itself is built on LibreHardwareMonitor, so this backend sees the
same fans and sensors FanControl does (including NVIDIA GPU fans, which Linux
hwmon can't control).

Requirements (Windows, run as Administrator):
    pip install pythonnet
    LibreHardwareMonitorLib.dll (and its HidSharp.dll dependency) from a
    LibreHardwareMonitor release; pass the folder with --lhm-dir or set
    FANCAL_LHM_DIR.

**Close FanControl before calibrating.** Two programs writing the same fan
controls fight each other, and the measurements become meaningless.

Identifiers are LibreHardwareMonitor's sensor identifiers, e.g.
    /lpc/nct6798d/control/1    a motherboard fan header's PWM control
    /lpc/nct6798d/fan/1        that header's RPM
    /amdcpu/0/temperature/2    CPU package temperature
    /gpu-nvidia/0/control/1    NVIDIA GPU fan control
Use ``fancal list-lhm`` to see the ones on your machine.
"""

from __future__ import annotations

import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


class LhmUnavailable(RuntimeError):
    pass


def load_computer(lhm_dir: str | None = None) -> Any:
    """Open a LibreHardwareMonitor ``Computer`` with every relevant group enabled."""
    lhm_dir = lhm_dir or os.environ.get("FANCAL_LHM_DIR")
    if not lhm_dir:
        raise LhmUnavailable("Point --lhm-dir (or FANCAL_LHM_DIR) at the folder holding LibreHardwareMonitorLib.dll")
    dll = Path(lhm_dir) / "LibreHardwareMonitorLib.dll"
    if not dll.exists():
        raise LhmUnavailable(f"{dll} not found")
    try:
        import clr  # type: ignore  # pythonnet
    except ImportError as e:
        raise LhmUnavailable("pythonnet is not installed: pip install pythonnet") from e
    sys.path.append(str(dll.parent))
    clr.AddReference(str(dll))
    from LibreHardwareMonitor.Hardware import Computer  # type: ignore

    c = Computer()
    c.IsCpuEnabled = True
    c.IsGpuEnabled = True
    c.IsMotherboardEnabled = True
    c.IsControllerEnabled = True   # AIO pumps/fan hubs (Corsair, NZXT, ...)
    c.IsStorageEnabled = True
    c.Open()
    return c


def _walk(hardware: Iterable[Any]) -> Iterable[Any]:
    for hw in hardware:
        yield hw
        yield from _walk(hw.SubHardware)


class LhmSession:
    """Indexes sensors by identifier and refreshes hardware at most every
    ``min_interval`` seconds, however many sensors are read per tick."""

    def __init__(self, computer: Any, clock: Callable[[], float], min_interval: float = 0.5) -> None:
        self.computer = computer
        self._clock = clock
        self.min_interval = min_interval
        self._last = -1e9
        self.sensors: dict[str, Any] = {}
        self.refresh(force=True)
        for hw in _walk(computer.Hardware):
            for s in hw.Sensors:
                self.sensors[str(s.Identifier)] = s

    def refresh(self, force: bool = False) -> None:
        now = self._clock()
        if force or now - self._last >= self.min_interval:
            for hw in _walk(self.computer.Hardware):
                hw.Update()
            self._last = now

    def get(self, ident: str) -> Any:
        try:
            return self.sensors[ident]
        except KeyError:
            raise KeyError(f"no LibreHardwareMonitor sensor {ident!r}; run `fancal list-lhm`") from None

    def value(self, ident: str) -> float:
        self.refresh()
        v = self.get(ident).Value
        if v is None:
            raise OSError(f"{ident} has no reading")
        return float(v)

    def close(self) -> None:
        self.computer.Close()

    def listing(self) -> list[dict]:
        self.refresh(force=True)
        out = []
        for hw in _walk(self.computer.Hardware):
            for s in hw.Sensors:
                kind = str(s.SensorType)
                if kind in ("Temperature", "Fan", "Control"):
                    out.append({
                        "hardware": str(hw.Name),
                        "type": kind,
                        "name": str(s.Name),
                        "id": str(s.Identifier),
                        "value": None if s.Value is None else round(float(s.Value), 1),
                    })
        return out


@dataclass
class LhmFan:
    name: str
    session: LhmSession
    control_id: str
    rpm_id: str

    def take_control(self) -> None:
        self.session.get(self.control_id)   # fail early on a bad id

    def release_control(self) -> None:
        # Hands the header back to BIOS/driver control.
        self.session.get(self.control_id).Control.SetDefault()

    def set_duty(self, percent: float) -> None:
        percent = max(0.0, min(100.0, percent))
        self.session.get(self.control_id).Control.SetSoftware(float(percent))

    def read_rpm(self) -> float:
        return self.session.value(self.rpm_id)


@dataclass
class LhmSensor:
    name: str
    session: LhmSession
    ident: str

    def read_celsius(self) -> float:
        return self.session.value(self.ident)


def fancontrol_running() -> bool:
    """True if FanControl.exe is running (Windows only; False elsewhere)."""
    if os.name != "nt":
        return False
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq FanControl.exe", "/NH"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "FanControl.exe" in out
