"""Translate a calibration profile into FanControl settings.

FanControl's plugin API only lets plugins *provide* sensors and controls; it
can't drive a calibration. Its config file format isn't documented either.
So this produces a setup sheet: the exact curves and control settings to
enter in FanControl's UI, using only settings its docs describe:

    Graph curve (points, hysteresis, response time,
                 "ignore hysteresis at minimum and maximum temps")
    Mix Fan Curve (function: Max)
    Time Average custom sensor
    Control: Minimum %, Start %, Stop %, Step up %, Step down %

How fanCalibrator maps onto those:
    cpu/gpu arm      -> one Graph curve on that chip's (time-averaged) temp
    case arm         -> one Graph curve per watched sensor + a Mix curve (Max)
    floor duty       -> Minimum % (or Stop % for zero-RPM arms)
    start duty       -> Start %
    EMA smoothing    -> Time Average sensor
    hysteresis       -> Graph hysteresis
    up/down rates    -> Step up % / Step down % (FanControl updates at 1 Hz)
"""

from __future__ import annotations

import json
from pathlib import Path

from .controller import ControlConfig

LOW_TEMP = 20.0    # anchor points so the graph is defined over the whole range
HIGH_TEMP = 100.0


def graph_points(points: list[list[float]], allow_stop: bool = False) -> list[tuple[float, float]]:
    pts = [(round(t, 1), round(d)) for t, d in points]
    if allow_stop and pts[0][1] == 0:
        # fanCalibrator's step to 0% sits 0.01 C below the floor point; widen
        # it to 1 C so it survives rounding. Stop % (= floor) then snaps the
        # fan off below the idle point.
        pts[0] = (round(pts[1][0] - 1.0, 1), 0)
    if pts[0][0] > LOW_TEMP:
        pts.insert(0, (LOW_TEMP, pts[0][1]))
    if pts[-1][0] < HIGH_TEMP:
        pts.append((HIGH_TEMP, 100))
    return pts


def build_plan(profile: dict, fan_ids: dict[str, str] | None = None,
               cfg: ControlConfig = ControlConfig()) -> dict:
    """Machine-readable plan (also what a future config writer would consume)."""
    fan_ids = fan_ids or {}
    fans = profile["fans"]
    smooth_window = max(2, round(cfg.smoothing * 2))   # box average ~ EMA with tau = window / 2
    sensors = sorted({s for a in profile["arms"] for s in a["controls"]})
    plan = {
        "custom_sensors": [
            {"name": f"fancal {s} avg", "type": "Time Average", "source": s, "seconds": smooth_window}
            for s in sensors
        ],
        "curves": [],
        "controls": [],
    }
    for arm in profile["arms"]:
        name = arm["name"]
        floor = max(_floor(fans[f]) for f in arm["fans"])
        graphs = []
        for s, pts in profile["curves"][name].items():
            gname = f"fancal {name}" if len(arm["controls"]) == 1 else f"fancal {name} <- {s}"
            plan["curves"].append({
                "name": gname,
                "type": "Graph",
                "sensor": f"fancal {s} avg",
                "points": graph_points(pts, arm.get("allow_stop", False)),
                "hysteresis": cfg.hysteresis,
                "response_time": 1,
                "ignore_hysteresis_at_min_max": True,
            })
            graphs.append(gname)
        curve = graphs[0]
        if len(graphs) > 1:
            curve = f"fancal {name}"
            plan["curves"].append({"name": curve, "type": "Mix", "function": "Max", "curves": graphs})
        for f in arm["fans"]:
            fp = fans[f]
            plan["controls"].append({
                "fan": f,
                "lhm_id": fan_ids.get(f),
                "curve": curve,
                "minimum_pct": 0 if arm.get("allow_stop") else round(floor),
                "stop_pct": round(floor) if arm.get("allow_stop") else 0,
                "start_pct": round(fp["start_duty"]) if fp.get("start_duty") is not None else None,
                "step_up_pct": round(cfg.up_rate),
                "step_down_pct": round(cfg.down_rate),
            })
    return plan


def _floor(fan: dict, margin: float = 5.0) -> float:
    return 0.0 if fan.get("stall_duty") is None else min(100.0, fan["stall_duty"] + margin)


def render_sheet(profile: dict, plan: dict) -> str:
    r = profile["result"]
    out = ["# FanControl setup from fanCalibrator", ""]
    if not r["feasible"]:
        out += ["> **Targets were not met even at 100% on every fan.** These curves",
                "> will run flat out under load; improve cooling or raise targets.", ""]
    out += ["Calibrated operating point (sustained CPU+GPU load):", ""]
    out += [f"- **{a}** arm at {d:.0f}%" for a, d in r["duties"].items()]
    out += ["- temps: " + ", ".join(f"{k} {v:.1f} °C (target {profile['targets'][k]:.0f})"
                                    for k, v in r["temps"].items()), ""]

    out += ["## 1. Custom sensors (Time Average)", "",
            "Smooths out 1-second spikes so fans don't react to them.", ""]
    for c in plan["custom_sensors"]:
        out.append(f"- **{c['name']}**: Time Average of your *{c['source']}* temperature, {c['seconds']} s")
    out.append("")

    out += ["## 2. Curves", ""]
    for c in plan["curves"]:
        if c["type"] == "Graph":
            out += [f"### {c['name']} (Graph)", "",
                    f"- Temperature source: **{c['sensor']}**",
                    f"- Hysteresis: {c['hysteresis']:g} °C, response time: {c['response_time']} s, "
                    "*ignore hysteresis at minimum and maximum temps*: on",
                    "- Points:", "",
                    "  | °C | % |", "  |---:|---:|"]
            out += [f"  | {t:g} | {d:g} |" for t, d in c["points"]]
            out.append("")
        else:
            out += [f"### {c['name']} (Mix Fan Curve)", "",
                    f"- Function: **{c['function']}**",
                    "- Curves: " + ", ".join(f"*{n}*" for n in c["curves"]), ""]

    out += ["## 3. Controls", "",
            "| Fan | LHM id | Curve | Minimum % | Start % | Stop % | Step up % | Step down % |",
            "|---|---|---|---:|---:|---:|---:|---:|"]
    for c in plan["controls"]:
        start = "" if c["start_pct"] is None else c["start_pct"]
        out.append(f"| {c['fan']} | `{c['lhm_id'] or '?'}` | {c['curve']} | {c['minimum_pct']} | "
                   f"{start} | {c['stop_pct']} | {c['step_up_pct']} | {c['step_down_pct']} |")
    out += ["",
            "Notes:",
            "- NVIDIA GPU fans have a 30% minimum in FanControl; if the GPU arm's",
            "  numbers are lower, use 30% and expect the GPU a little cooler than calibrated.",
            "- Re-calibrate if you change fans, coolers, case layout, or the room gets",
            "  much warmer than when you calibrated.", ""]
    return "\n".join(out)


def export(profile_path: str | Path, out_path: str | Path, fan_ids: dict[str, str] | None = None) -> dict:
    profile = json.loads(Path(profile_path).read_text())
    plan = build_plan(profile, fan_ids)
    Path(out_path).write_text(render_sheet(profile, plan))
    Path(out_path).with_suffix(".plan.json").write_text(json.dumps(plan, indent=2))
    return plan
