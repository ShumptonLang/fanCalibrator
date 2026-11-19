import json
import math
import random

import pytest

from fancalibrator.calibrate import Profile, calibrate
from fancalibrator.characterize import characterize_fan
from fancalibrator.cli import sim_setup
from fancalibrator.controller import ArmState, ControlConfig, Controller
from fancalibrator.curves import Curve, build_curve
from fancalibrator.noise import fan_db, total_db
from fancalibrator.sim import SimClock, SimFan, SimRig
from fancalibrator.steady import OverTemperature, SettleConfig, wait_steady


@pytest.fixture(autouse=True)
def seed():
    random.seed(1234)


def quiet(_msg):
    pass


# -- noise ------------------------------------------------------------------

def test_doubling_rpm_adds_about_15_db():
    assert fan_db(2000, 2000, 30) - fan_db(1000, 2000, 30) == pytest.approx(15.05, abs=0.01)


def test_two_equal_sources_add_3_db():
    assert total_db([30, 30]) == pytest.approx(33.01, abs=0.01)
    assert total_db([-math.inf]) == -math.inf


# -- characterization ---------------------------------------------------------

def test_characterize_finds_stall_and_start():
    clock = SimClock()
    fan = SimFan("f", stall_duty=18, start_duty=28)
    prof = characterize_fan(fan, clock, log=quiet)
    assert prof.max_rpm == pytest.approx(1800)
    # Refined to the 2% start_step on either side of the true thresholds.
    assert 18 <= prof.stall_duty <= 20
    assert 28 <= prof.start_duty <= 30
    assert prof.floor_duty() == prof.stall_duty + 5
    assert fan.duty == 100  # left at full speed


# -- steady state -------------------------------------------------------------

def test_wait_steady_settles_near_model_steady_state():
    rig = SimRig(loaded=True)
    temps, settled = wait_steady(rig.sensors, rig.clock, SettleConfig(max_slope=0.05))
    assert settled
    for k, v in rig.steady_temps().items():
        assert v == pytest.approx(temps[k], abs=1.0)


def test_wait_steady_aborts_on_over_temperature():
    rig = SimRig(loaded=True)
    for f in rig.fans.values():
        f.set_duty(0)
    with pytest.raises(OverTemperature):
        wait_steady(rig.sensors, rig.clock, SettleConfig(abort_temp=85))


# -- curves -------------------------------------------------------------------

def test_curve_shape():
    c = build_curve(idle_temp=35, load_temp=70, target=80, floor=25, opt_duty=50)
    assert c(20) == 25
    assert c(37) == 25
    assert c(70) == pytest.approx(50)
    assert c(83) == 100 and c(120) == 100
    temps = [t for t, _ in c.points]
    assert temps == sorted(temps) and len(set(temps)) == len(temps)


def test_curve_stays_monotonic_when_idle_and_load_overlap():
    c = build_curve(idle_temp=60, load_temp=59, target=60, floor=30, opt_duty=30)
    temps = [t for t, _ in c.points]
    assert all(b > a for a, b in zip(temps, temps[1:]))


def test_allow_stop_curve_starts_at_zero():
    c = build_curve(35, 70, 80, floor=25, opt_duty=50, allow_stop=True)
    assert c(30) == 0 and c(37) == 25


# -- three-arm calibration ------------------------------------------------------

def run_sim_calibration(**fan_db_overrides):
    setup, rig = sim_setup()
    setup.fan_max_db.update(fan_db_overrides)
    return calibrate(setup, log=quiet), setup, rig


def test_calibration_meets_targets_and_is_quieter():
    prof, setup, _ = run_sim_calibration()
    r = prof.result
    assert r.feasible
    for s, target in setup.targets.items():
        assert r.temps[s] <= target - 2.0 + 0.5  # guard band, allow sensor jitter
    assert r.noise_db < r.history[0].noise_db - 5
    assert all(d < 100 for d in r.duties.values())
    # Every arm got probed, so every arm has a sensitivity estimate.
    assert set(r.sensitivity) == {"cpu", "gpu", "case"}


def test_loud_gpu_fan_shifts_work_to_the_case_fans():
    normal, *_ = run_sim_calibration()
    random.seed(1234)
    loud, *_ = run_sim_calibration(gpu_fan=46)
    assert loud.result.duties["gpu"] < normal.result.duties["gpu"]
    assert loud.result.duties["case"] >= normal.result.duties["case"]


def test_unreachable_targets_are_reported():
    setup, _ = sim_setup()
    setup.targets = {"cpu": 50.0, "gpu": 50.0}
    prof = calibrate(setup, log=quiet)
    assert not prof.result.feasible
    assert all(d == 100 for d in prof.result.duties.values())


def test_profile_round_trip(tmp_path):
    prof, *_ = run_sim_calibration()
    path = tmp_path / "profile.json"
    prof.save(path)
    data = json.loads(path.read_text())
    assert set(data["curves"]["case"]) == {"cpu", "gpu"}
    states = Profile.load_arm_states(path)
    assert {s.name for s in states} == {"cpu", "gpu", "case"}
    case = next(s for s in states if s.name == "case")
    assert case.start_duty == max(prof.fans[f].start_duty for f in ("case_front", "case_rear"))


# -- controller ---------------------------------------------------------------

def test_controller_holds_targets_under_load(tmp_path):
    prof, setup, rig = run_sim_calibration()
    path = tmp_path / "p.json"
    prof.save(path)
    rig.loaded = True
    ctl = Controller(Profile.load_arm_states(path), setup.fans, setup.sensors, setup.clock)
    seen = []
    ctl.run(duration=1200, on_tick=lambda t, d: seen.append((t, d)))
    late = seen[-300:]
    for s, target in setup.targets.items():
        assert max(t[s] for t, _ in late) <= target
    # Settled, not hunting: duty barely moves over the last 5 minutes.
    for arm in ("cpu", "gpu", "case"):
        ds = [d[arm] for _, d in late]
        assert max(ds) - min(ds) < 2.0


class FakeSensor:
    def __init__(self, name, value):
        self.name, self.value = name, value

    def read_celsius(self):
        if self.value is None:
            raise OSError("gone")
        return self.value


def make_ctl(temp, curve, allow_stop_start=0.0, **cfg):
    clock = SimClock()
    fan = SimFan("f")
    sensor = FakeSensor("t", temp)
    arm = ArmState("a", ["f"], {"t": curve}, start_duty=allow_stop_start)
    ctl = Controller([arm], {"f": fan}, {"t": sensor}, clock,
                     ControlConfig(smoothing=0, up_rate=1000, down_rate=1000, **cfg))
    return ctl, arm, fan, sensor, clock


def test_hysteresis_holds_duty_on_small_dip():
    curve = Curve([(40, 20), (80, 100)])
    ctl, arm, fan, sensor, _ = make_ctl(60, curve, hysteresis=3)
    arm.duty = 20                            # coming up from idle
    ctl.tick(1)
    assert arm.duty == pytest.approx(60)
    sensor.value = 58.5                      # dipped less than the hysteresis
    ctl.tick(1)
    assert arm.duty == pytest.approx(60)
    sensor.value = 50                        # clearly cooler: follow T + hysteresis
    ctl.tick(1)
    assert arm.duty == pytest.approx(curve(53))


def test_sensor_failure_goes_full_speed():
    ctl, arm, fan, sensor, _ = make_ctl(50, Curve([(40, 20), (80, 100)]))
    ctl.tick(1)
    assert fan.duty < 100
    sensor.value = None
    ctl.tick(1)
    assert fan.duty == 100


def test_zero_rpm_restart_gets_kicked():
    curve = Curve([(44.99, 0), (45, 20), (80, 100)])
    ctl, arm, fan, sensor, clock = make_ctl(30, curve, allow_stop_start=35, kick_time=3, hysteresis=0)
    ctl.tick(1)
    assert fan.duty == 0 and arm.stopped
    sensor.value = 46
    ctl.tick(1)
    assert fan.duty == 35                    # kicked to start duty, not 22%
    clock.sleep(4)
    ctl.tick(1)
    assert fan.duty == pytest.approx(curve(46))
