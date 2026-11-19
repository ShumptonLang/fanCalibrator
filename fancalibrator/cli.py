"""Command line entry point.

    fancal list-hwmon
    fancal calibrate CONFIG -o profile.json          (root, real hardware)
    fancal calibrate --sim -o profile.json           (simulated rig)
    fancal run CONFIG profile.json                   (root, real hardware)
    fancal run --sim profile.json --duration 600
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .calibrate import Profile, Setup, ShellLoad, calibrate
from .controller import Controller
from .hardware import HwmonFan, HwmonSensor, RealClock, list_hwmon
from .optimize import Arm, knee_report
from .sim import SimRig


def sim_setup() -> tuple[Setup, SimRig]:
    rig = SimRig()
    arms = [
        Arm("cpu", ["cpu_fan"], ["cpu"]),
        Arm("gpu", ["gpu_fan"], ["gpu"]),
        Arm("case", ["case_front", "case_rear"], ["cpu", "gpu"]),
    ]
    setup = Setup(
        fans=dict(rig.fans),
        sensors={k: rig.sensors[k] for k in ("cpu", "gpu")},
        arms=arms,
        targets={"cpu": 80.0, "gpu": 75.0},
        clock=rig.clock,
        fan_max_db={"cpu_fan": 32, "gpu_fan": 38, "case_front": 30, "case_rear": 30},
        start_load=lambda: setattr(rig, "loaded", True),
        stop_load=lambda: setattr(rig, "loaded", False),
    )
    return setup, rig


def config_setup(path: str) -> Setup:
    """Config file format: see config.example.json."""
    cfg = json.loads(Path(path).read_text())
    fans = {n: HwmonFan(n, f["pwm"], f["rpm"]) for n, f in cfg["fans"].items()}
    sensors = {n: HwmonSensor(n, p) for n, p in cfg["sensors"].items()}
    arms = [
        Arm(n, a["fans"], a["controls"], a.get("allow_stop", False))
        for n, a in cfg["arms"].items()
    ]
    setup = Setup(
        fans=fans,
        sensors=sensors,
        arms=arms,
        targets={k: float(v) for k, v in cfg["targets"].items()},
        clock=RealClock(),
        fan_max_db={n: f["max_db"] for n, f in cfg["fans"].items() if "max_db" in f},
    )
    if cfg.get("load_command"):
        load = ShellLoad(cfg["load_command"])
        setup.start_load, setup.stop_load = load.start, load.stop
    else:
        setup.start_load = lambda: input("Start your CPU+GPU stress load now, then press Enter...")
        setup.stop_load = lambda: print("You can stop the stress load now.")
    return setup


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fancal")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-hwmon", help="show hwmon chips and their channels")

    c = sub.add_parser("calibrate", help="characterize fans and search for the quietest load point")
    c.add_argument("config", nargs="?")
    c.add_argument("--sim", action="store_true")
    c.add_argument("-o", "--output", default="profile.json")

    r = sub.add_parser("run", help="control fans from a profile")
    r.add_argument("config", nargs="?")
    r.add_argument("profile")
    r.add_argument("--sim", action="store_true")
    r.add_argument("--duration", type=float, default=None)

    args = p.parse_args(argv)

    if args.cmd == "list-hwmon":
        print(json.dumps(list_hwmon(), indent=2))
        return 0

    if args.cmd == "calibrate":
        if not args.sim and not args.config:
            p.error("calibrate needs a CONFIG file or --sim")
        setup = sim_setup()[0] if args.sim else config_setup(args.config)
        prof = calibrate(setup)
        prof.save(args.output)
        r = prof.result
        print("\n== result" + ("" if r.feasible else " (TARGETS NOT MET at 100%)"))
        for arm, d in r.duties.items():
            print(f"  {arm:6s} {d:5.1f}%")
        print("  temps: " + ", ".join(f"{k}={v:.1f}" for k, v in r.temps.items()))
        print(f"  est. noise {r.noise_db:.1f} dB (relative)")
        for arm, note in knee_report(r).items():
            print(f"  {arm}: {note}")
        print(f"profile written to {args.output}")
        return 0 if r.feasible else 2

    if args.cmd == "run":
        if args.sim:
            setup, rig = sim_setup()
            rig.loaded = True
        else:
            if not args.config:
                p.error("run needs a CONFIG file or --sim")
            setup = config_setup(args.config)
        ctl = Controller(Profile.load_arm_states(args.profile), setup.fans, setup.sensors, setup.clock)

        def show(temps, duties):
            t = " ".join(f"{k}={v:5.1f}" for k, v in temps.items())
            d = " ".join(f"{k}={v:5.1f}%" for k, v in duties.items())
            print(f"{t} | {d}", flush=True)

        try:
            ctl.run(args.duration, on_tick=show)
        except KeyboardInterrupt:
            pass
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
