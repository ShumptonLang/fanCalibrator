"""Find the quietest combination of CPU, GPU and case fan speeds that keeps
every constrained sensor under its target, under a fixed sustained load.

Why a joint search: the arms interact. Case fans don't touch either chip
directly, but they lower the air temperature both coolers breathe, so slowing
the case fans raises CPU *and* GPU temps. Sometimes it's quieter to run the
case fans a bit faster so the (often louder) GPU fan can slow down, and
sometimes the reverse. Tuning each fan in isolation can't see that trade.

Method: greedy descent from all-arms-100%, one measured step at a time.

1. Measure the steady state at the current point.
2. For each arm, consider lowering it by its current step size. Score the
   move by acoustic saving per unit of temperature headroom it is expected
   to consume. The expected rise comes from that arm's last measured
   sensitivity (deg C per % duty, per sensor). Arms not yet probed go first.
3. Apply the best move and measure. If every sensor stays under
   (target - guard band) keep it; otherwise halve that arm's step. If the
   move was legal but bought less than ``min_db_per_degree`` of quiet per
   deg C it cost, the arm is past its knee: undo it and stop moving that arm.
4. When no arm can move by at least ``min_step``, try *exchanges*: raise
   one arm a step so another can drop further (e.g. a bit more case airflow
   letting a louder GPU fan slow down). Keep an exchange only if it is
   measurably quieter and still within targets, then resume descending.

Each trial is a real steady-state measurement (a few minutes), so the
predictions only decide *which* move to try next; nothing is accepted
without being measured.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Callable, Mapping

from .characterize import FanProfile
from .hardware import Clock, Fan, Sensor
from .noise import fan_db, total_db
from .steady import SettleConfig, wait_steady


@dataclass
class Arm:
    name: str
    fans: list[str]
    controls: list[str]          # sensors whose temperature drives this arm's curve
    allow_stop: bool = False     # zero-RPM below the idle point


@dataclass
class OptimizeConfig:
    initial_step: float = 15.0   # % duty
    min_step: float = 3.0
    exchange_step: float = 10.0  # % duty used for exchange trials
    min_gain_db: float = 0.3     # an exchange must save at least this much
    max_exchanges: int = 6
    # Sensitivity grows as a fan slows, so linear predictions are optimistic;
    # size exchange drops as if the slowed arm were this much more sensitive.
    exchange_caution: float = 1.5
    guard_band: float = 2.0      # deg C kept below each target at the operating point
    floor_margin: float = 5.0    # % above stall duty that the arm may go down to
    # The knee: stop lowering an arm once a step saves less than this many dB
    # per deg C of headroom it uses. 0 = always run right up to the targets.
    min_db_per_degree: float = 0.05


@dataclass
class TrialPoint:
    duties: dict[str, float]
    temps: dict[str, float]
    noise_db: float
    accepted: bool
    settled: bool


@dataclass
class OptimizeResult:
    feasible: bool
    duties: dict[str, float]
    temps: dict[str, float]
    noise_db: float
    sensitivity: dict[str, dict[str, float]]   # arm -> sensor -> deg C per % duty
    history: list[TrialPoint] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


class ArmOptimizer:
    def __init__(
        self,
        arms: list[Arm],
        fans: Mapping[str, Fan],
        profiles: Mapping[str, FanProfile],
        sensors: Mapping[str, Sensor],
        targets: Mapping[str, float],
        clock: Clock,
        settle: SettleConfig = SettleConfig(),
        cfg: OptimizeConfig = OptimizeConfig(),
        fan_max_db: Mapping[str, float] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        self.arms = {a.name: a for a in arms}
        self.fans = fans
        self.profiles = profiles
        self.sensors = sensors
        self.targets = dict(targets)
        self.clock = clock
        self.settle = settle
        self.cfg = cfg
        self.fan_max_db = dict(fan_max_db or {})
        self.log = log

    # -- helpers ------------------------------------------------------------
    def floor(self, arm: str) -> float:
        return max(self.profiles[f].floor_duty(self.cfg.floor_margin) for f in self.arms[arm].fans)

    def noise(self, duties: Mapping[str, float]) -> float:
        levels = []
        for arm, duty in duties.items():
            for f in self.arms[arm].fans:
                p = self.profiles[f]
                levels.append(fan_db(p.rpm_at(duty), p.max_rpm, self.fan_max_db.get(f, 30.0)))
        return total_db(levels)

    def apply(self, duties: Mapping[str, float]) -> None:
        for arm, duty in duties.items():
            for f in self.arms[arm].fans:
                self.fans[f].set_duty(duty)

    def measure(self, duties: Mapping[str, float]) -> tuple[dict[str, float], bool]:
        self.apply(duties)
        return wait_steady(self.sensors, self.clock, self.settle)

    def within(self, temps: Mapping[str, float]) -> bool:
        return all(temps[s] <= t - self.cfg.guard_band for s, t in self.targets.items())

    def _fmt(self, duties, temps) -> str:
        d = " ".join(f"{a}={v:.0f}%" for a, v in duties.items())
        t = " ".join(f"{s}={temps[s]:.1f}" for s in self.targets)
        return f"[{d}] -> {t}  ({self.noise(duties):.1f} dB)"

    # -- search -------------------------------------------------------------
    def _trial(self, trial, history) -> tuple[dict[str, float], bool]:
        t_temps, settled = self.measure(trial)
        ok = self.within(t_temps)
        history.append(TrialPoint(dict(trial), t_temps, self.noise(trial), ok, settled))
        return t_temps, ok

    def _predict(self, sens, temps, change: Mapping[str, float]) -> dict[str, float] | None:
        """Temps after changing arms by ``change`` (% duty, negative = slower),
        or None if any arm involved hasn't been probed yet."""
        out = dict(temps)
        for a, delta in change.items():
            if sens[a] is None:
                return None
            for s in self.targets:
                out[s] -= sens[a].get(s, 0.0) * delta
        return out

    def _worth_it(self, saving_db: float, before: Mapping[str, float], after: Mapping[str, float]) -> bool:
        rise = max(after[s] - before[s] for s in self.targets)
        if rise < 0.5:   # within measurement noise: free quiet, take it
            return True
        return saving_db / rise >= self.cfg.min_db_per_degree

    def _descend(self, duties, temps, sens, history):
        step = {a: self.cfg.initial_step for a in self.arms}
        while True:
            best = None
            for a in self.arms:
                lo = self.floor(a)
                if step[a] < self.cfg.min_step or duties[a] <= lo + 1e-6:
                    continue
                new = max(lo, duties[a] - step[a])
                trial = {**duties, a: new}
                saving = self.noise(duties) - self.noise(trial)
                pred = self._predict(sens, temps, {a: new - duties[a]})
                if pred is None:
                    score = 1e9 + saving        # probe unmeasured arms first
                elif not self.within(pred):
                    # Predicted to break a target; shrink the step without
                    # spending a measurement on it.
                    step[a] /= 2
                    continue
                else:
                    if not self._worth_it(saving, temps, pred):
                        step[a] = 0.0           # past the knee, leave this arm
                        continue
                    use = max(
                        (pred[s] - temps[s]) / max(t - self.cfg.guard_band - temps[s], 0.1)
                        for s, t in self.targets.items()
                    )
                    score = saving / max(use, 1e-3)
                if best is None or score > best[0]:
                    best = (score, a, trial)
            if best is None:
                return duties, temps

            _, a, trial = best
            t_temps, ok = self._trial(trial, history)
            drop = duties[a] - trial[a]
            sens[a] = {s: max(0.0, (t_temps[s] - temps[s]) / drop) for s in self.targets}
            saving = self.noise(duties) - self.noise(trial)
            if ok and not self._worth_it(saving, temps, t_temps):
                history[-1].accepted = False
                self.log("knee " + self._fmt(trial, t_temps))
                step[a] = 0.0
                continue
            self.log(("keep " if ok else "back ") + self._fmt(trial, t_temps))
            if ok:
                duties, temps = trial, t_temps
            else:
                step[a] /= 2

    def _exchange(self, duties, temps, sens, history, tried: set):
        """Raise one arm a step, then lower another by as much as the freed
        headroom allows (per the measured sensitivities). Returns a better
        point or None."""
        x = self.cfg.exchange_step
        options = []
        for up in self.arms:
            if duties[up] >= 100 or sens[up] is None:
                continue
            raised = min(100.0, duties[up] + x)
            after_up = self._predict(sens, temps, {up: raised - duties[up]})
            for down in self.arms:
                if down == up or sens[down] is None or duties[down] <= self.floor(down) + 1e-6:
                    continue
                # Largest drop of `down` that keeps every predicted temp legal.
                drop = duties[down] - self.floor(down)
                for s_, t in self.targets.items():
                    k = sens[down].get(s_, 0.0) * self.cfg.exchange_caution
                    if k > 1e-6:
                        drop = min(drop, (t - self.cfg.guard_band - after_up[s_]) / k)
                drop = float(int(drop))     # whole percent, rounded toward safety
                if drop < self.cfg.min_step:
                    continue
                trial = {**duties, up: raised, down: duties[down] - drop}
                key = tuple(sorted((a_, round(v, 1)) for a_, v in trial.items()))
                gain = self.noise(duties) - self.noise(trial)
                if key in tried or gain < self.cfg.min_gain_db:
                    continue
                options.append((gain, key, trial))
        for gain, key, trial in sorted(options, key=lambda o: o[0], reverse=True):
            tried.add(key)
            t_temps, ok = self._trial(trial, history)
            self.log(("swap " if ok else "back ") + self._fmt(trial, t_temps))
            if ok:
                return trial, t_temps
        return None

    def run(self) -> OptimizeResult:
        duties = {a: 100.0 for a in self.arms}
        history: list[TrialPoint] = []
        temps, ok = self._trial(duties, history)
        self.log("start " + self._fmt(duties, temps))
        sens: dict[str, dict[str, float] | None] = {a: None for a in self.arms}

        if not ok:
            self.log("targets not reachable even at 100% on every arm")
            return OptimizeResult(False, duties, temps, self.noise(duties), {}, history)

        tried: set = set()
        for _ in range(self.cfg.max_exchanges + 1):
            duties, temps = self._descend(duties, temps, sens, history)
            swapped = self._exchange(duties, temps, sens, history, tried)
            if swapped is None:
                break
            duties, temps = swapped

        # Leave the rig at the chosen point (the last trial may have been rejected).
        temps, _ = self.measure(duties)
        clean_sens = {a: v for a, v in sens.items() if v is not None}
        return OptimizeResult(True, duties, temps, self.noise(duties), clean_sens, history)


def knee_report(result: OptimizeResult) -> dict[str, str]:
    """Plain-language read of each arm's final sensitivity."""
    out = {}
    for arm, s in result.sensitivity.items():
        worst = max(s.values()) if s else 0.0
        if worst < 0.05:
            note = "flat: this arm barely matters here, it's above its knee"
        elif worst < 0.25:
            note = "near the knee"
        else:
            note = "steep: below the knee, each % of duty is buying real cooling"
        out[arm] = f"{worst:.2f} C per % duty ({note})"
    return out
