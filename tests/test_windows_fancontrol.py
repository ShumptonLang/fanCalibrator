"""LibreHardwareMonitor backend (against a fake LHM object model) and the
FanControl exporter."""

import json

import pytest

from fancalibrator.calibrate import calibrate
from fancalibrator.cli import fan_ids_from_config, sim_setup
from fancalibrator.fancontrol_export import build_plan, export, graph_points
from fancalibrator.lhm import LhmFan, LhmSensor, LhmSession


# -- fake LibreHardwareMonitor --------------------------------------------------

class FakeControl:
    def __init__(self):
        self.software = None
        self.default_calls = 0

    def SetSoftware(self, v):
        self.software = v

    def SetDefault(self):
        self.default_calls += 1
        self.software = None


class FakeSensor:
    def __init__(self, ident, kind, value, name="s"):
        self.Identifier = ident     # LHM's Identifier stringifies to the path
        self.SensorType = kind
        self.Value = value
        self.Name = name
        self.Control = FakeControl() if kind == "Control" else None


class FakeHardware:
    def __init__(self, name, sensors, sub=()):
        self.Name = name
        self.Sensors = list(sensors)
        self.SubHardware = list(sub)
        self.updates = 0

    def Update(self):
        self.updates += 1


class FakeComputer:
    def __init__(self):
        self.superio = FakeHardware("Nuvoton NCT6798D", [
            FakeSensor("/lpc/nct6798d/control/1", "Control", 100.0, "Fan #1"),
            FakeSensor("/lpc/nct6798d/fan/1", "Fan", 1500.0, "Fan #1"),
        ])
        self.board = FakeHardware("Board", [], sub=[self.superio])
        self.cpu = FakeHardware("CPU", [
            FakeSensor("/amdcpu/0/temperature/2", "Temperature", 55.0, "Tctl"),
            FakeSensor("/amdcpu/0/load/0", "Load", 10.0, "Total"),
        ])
        self.Hardware = [self.board, self.cpu]
        self.closed = False

    def Close(self):
        self.closed = True


class Tick:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def test_session_indexes_subhardware_and_throttles_updates():
    comp, t = FakeComputer(), Tick()
    s = LhmSession(comp, t, min_interval=0.5)
    assert "/lpc/nct6798d/fan/1" in s.sensors        # found via SubHardware
    n = comp.superio.updates
    s.value("/amdcpu/0/temperature/2")
    s.value("/lpc/nct6798d/fan/1")
    assert comp.superio.updates == n                  # same tick: no extra Update()
    t.t = 1.0
    s.value("/lpc/nct6798d/fan/1")
    assert comp.superio.updates == n + 1


def test_missing_reading_and_unknown_id():
    s = LhmSession(FakeComputer(), Tick())
    s.sensors["/amdcpu/0/temperature/2"].Value = None
    with pytest.raises(OSError):
        s.value("/amdcpu/0/temperature/2")
    with pytest.raises(KeyError, match="list-lhm"):
        s.value("/nope")


def test_fan_and_sensor_adapters():
    comp = FakeComputer()
    s = LhmSession(comp, Tick())
    fan = LhmFan("cpu_fan", s, "/lpc/nct6798d/control/1", "/lpc/nct6798d/fan/1")
    fan.take_control()
    fan.set_duty(140)
    ctl = s.get("/lpc/nct6798d/control/1").Control
    assert ctl.software == 100.0
    fan.set_duty(42.5)
    assert ctl.software == 42.5
    assert fan.read_rpm() == 1500.0
    fan.release_control()
    assert ctl.default_calls == 1
    assert LhmSensor("cpu", s, "/amdcpu/0/temperature/2").read_celsius() == 55.0
    with pytest.raises(KeyError):
        LhmFan("x", s, "/bad/control", "/bad/fan").take_control()


def test_listing_only_shows_temps_fans_controls():
    rows = LhmSession(FakeComputer(), Tick()).listing()
    assert {r["type"] for r in rows} == {"Temperature", "Fan", "Control"}
    assert any(r["hardware"] == "Nuvoton NCT6798D" for r in rows)


# -- exporter -------------------------------------------------------------------

def test_graph_points_get_anchors():
    pts = graph_points([[35.0, 25.0], [70.0, 50.0], [83.0, 100.0]])
    assert pts[0] == (20.0, 25) and pts[-1] == (100.0, 100)
    assert [t for t, _ in pts] == sorted(t for t, _ in pts)


def test_zero_rpm_graph_keeps_a_zero_point_below_idle():
    pts = graph_points([[34.99, 0.0], [35.0, 25.0], [70.0, 50.0], [83.0, 100.0]], allow_stop=True)
    assert pts[:3] == [(20.0, 0), (34.0, 0), (35.0, 25)]


@pytest.fixture(scope="module")
def profile_dict(tmp_path_factory):
    setup, _ = sim_setup()
    prof = calibrate(setup, log=lambda m: None)
    path = tmp_path_factory.mktemp("p") / "profile.json"
    prof.save(path)
    return path, json.loads(path.read_text())


def test_plan_maps_case_arm_to_mix_max(profile_dict):
    _, prof = profile_dict
    plan = build_plan(prof)
    names = {c["name"]: c for c in plan["curves"]}
    mix = names["fancal case"]
    assert mix["type"] == "Mix" and mix["function"] == "Max"
    assert set(mix["curves"]) == {"fancal case <- cpu", "fancal case <- gpu"}
    assert names["fancal cpu"]["sensor"] == "fancal cpu avg"
    case_ctls = [c for c in plan["controls"] if c["curve"] == "fancal case"]
    assert {c["fan"] for c in case_ctls} == {"case_front", "case_rear"}
    # Arm-wide Minimum %: the pickier fan's floor wins.
    assert len({c["minimum_pct"] for c in case_ctls}) == 1
    for c in plan["controls"]:
        fp = prof["fans"][c["fan"]]
        assert c["start_pct"] == round(fp["start_duty"])
        assert c["minimum_pct"] >= round(fp["stall_duty"])


def test_export_writes_sheet_and_plan(profile_dict, tmp_path):
    path, _ = profile_dict
    out = tmp_path / "fc.md"
    export(path, out, fan_ids_from_config("config.example.windows.json"))
    text = out.read_text()
    assert "Mix Fan Curve" in text and "/gpu-nvidia/0/control/1" in text
    assert json.loads(out.with_suffix(".plan.json").read_text())["controls"]
