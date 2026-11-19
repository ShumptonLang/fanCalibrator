"""Command line entry point.

    fancal list-hwmon                                (Linux)
    fancal list-lhm --lhm-dir DIR                    (Windows, LibreHardwareMonitor)
    fancal calibrate CONFIG -o profile.json          (root/Administrator, real hardware)
    fancal calibrate --sim -o profile.json           (simulated rig)
    fancal run CONFIG profile.json                   (root, real hardware)
    fancal run --sim profile.json --duration 600
    fancal export-fancontrol profile.json [CONFIG] -o fancontrol-setup.md
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


def config_setup(path: str, lhm_dir: str | None = None) -> Setup:
    """Config file format: see config.example.json (Linux hwmon) and
    config.example.windows.json (LibreHardwareMonitor)."""
    cfg = json.loads(Path(path).read_text())
    backend = cfg.get("backend", "hwmon")
    clock = RealClock()
    if backend == "hwmon":
        fans = {n: HwmonFan(n, f["pwm"], f["rpm"]) for n, f in cfg["fans"].items()}
        sensors = {n: HwmonSensor(n, p) for n, p in cfg["sensors"].items()}
    elif backend == "lhm":
        from .lhm import LhmFan, LhmSensor, LhmSession, load_computer

        session = LhmSession(load_computer(lhm_dir or cfg.get("lhm_dir")), clock.now)
        fans = {n: LhmFan(n, session, f["control"], f["rpm"]) for n, f in cfg["fans"].items()}
        sensors = {n: LhmSensor(n, session, i) for n, i in cfg["sensors"].items()}
    else:
        raise SystemExit(f"unknown backend {backend!r} (expected 'hwmon' or 'lhm')")
    arms = [
        Arm(n, a["fans"], a["controls"], a.get("allow_stop", False))
        for n, a in cfg["arms"].items()
    ]
    setup = Setup(
        fans=fans,
        sensors=sensors,
        arms=arms,
        targets={k: float(v) for k, v in cfg["targets"].items()},
        clock=clock,
        fan_max_db={n: f["max_db"] for n, f in cfg["fans"].items() if "max_db" in f},
    )
    if cfg.get("load_command"):
        load = ShellLoad(cfg["load_command"])
        setup.start_load, setup.stop_load = load.start, load.stop
    else:
        setup.start_load = lambda: input("Start your CPU+GPU stress load now, then press Enter...")
        setup.stop_load = lambda: print("You can stop the stress load now.")
    return setup


def fan_ids_from_config(path: str | None) -> dict[str, str]:
    if not path:
        return {}
    cfg = json.loads(Path(path).read_text())
    return {n: f.get("control") or f.get("pwm") for n, f in cfg["fans"].items()}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="fancal")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("list-hwmon", help="show hwmon chips and their channels (Linux)")
    lh = sub.add_parser("list-lhm", help="show LibreHardwareMonitor sensors and controls (Windows)")
    lh.add_argument("--lhm-dir")

    e = sub.add_parser("export-fancontrol", help="write FanControl settings for a profile")
    e.add_argument("profile")
    e.add_argument("config", nargs="?", help="adds each fan's control id to the sheet")
    e.add_argument("-o", "--output", default="fancontrol-setup.md")

    c = sub.add_parser("calibrate", help="characterize fans and search for the quietest load point")
    c.add_argument("config", nargs="?")
    c.add_argument("--sim", action="store_true")
    c.add_argument("-o", "--output", default="profile.json")
    c.add_argument("--lhm-dir")
    c.add_argument("--ignore-fancontrol", action="store_true",
                   help="calibrate even though FanControl.exe is running (not recommended)")

    r = sub.add_parser("run", help="control fans from a profile")
    r.add_argument("config", nargs="?")
    r.add_argument("profile")
    r.add_argument("--sim", action="store_true")
    r.add_argument("--duration", type=float, default=None)
    r.add_argument("--lhm-dir")

    args = p.parse_args(argv)

    if args.cmd == "list-hwmon":
        print(json.dumps(list_hwmon(), indent=2))
        return 0

    if args.cmd == "list-lhm":
        from .lhm import LhmSession, load_computer

        session = LhmSession(load_computer(args.lhm_dir), RealClock().now)
        try:
            for row in session.listing():
                print(f"{row['type']:12s} {row['id']:40s} {row['value']!s:>8}  {row['hardware']} / {row['name']}")
        finally:
            session.close()
        return 0

    if args.cmd == "export-fancontrol":
        from .fancontrol_export import export

        export(args.profile, args.output, fan_ids_from_config(args.config))
        print(f"FanControl setup written to {args.output} (+ {Path(args.output).with_suffix('.plan.json')})")
        return 0

    if args.cmd == "calibrate":
        if not args.sim and not args.config:
            p.error("calibrate needs a CONFIG file or --sim")
        if not args.sim and not args.ignore_fancontrol:
            from .lhm import fancontrol_running

            if fancontrol_running():
                print("FanControl is running. Close it first: two programs driving the same\n"
                      "fans make the measurements meaningless. (--ignore-fancontrol to override)")
                return 3
        setup = sim_setup()[0] if args.sim else config_setup(args.config, args.lhm_dir)
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
            setup = config_setup(args.config, args.lhm_dir)
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
