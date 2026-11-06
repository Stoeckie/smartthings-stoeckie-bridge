"""Home Connect profiles (zips from the Home Connect Profile Downloader) and helpers."""
import json
import zipfile

import stoeckie_bridge as sb


def make_zip(path, ha_id, typ, brand="SIEMENS", conn="TLS", iv=None):
    info = {"haId": ha_id, "type": typ, "brand": brand, "vib": "X1", "connectionType": conn, "key": "secret",
            "featureMappingFileName": f"{ha_id}_FeatureMapping.xml",
            "deviceDescriptionFileName": f"{ha_id}_DeviceDescription.xml"}
    if iv:
        info["iv"] = iv
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(f"{ha_id}.json", json.dumps(info))
        z.writestr(info["featureMappingFileName"], "<featureMappingFile/>")
        z.writestr(info["deviceDescriptionFileName"], "<device/>")


def test_profiles_and_hosts(tmp_path):
    make_zip(tmp_path / "dish.zip", "015100394724004530", "Dishwasher")
    make_zip(tmp_path / "oven.zip", "SIEMENS-CM676G0S6-68A40E6EC669", "Oven", conn="AES", iv="iv")
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    profs = sb.homeconnect_profiles(tmp_path)
    assert sorted(p["type"] for p in profs) == ["Dishwasher", "Oven"]
    assert all(p["xml"][0] == "<device/>" for p in profs)
    hosts = {p["type"]: sb.hc_default_host(p) for p in profs}
    assert hosts == {"Dishwasher": "siemens-dishwasher-015100394724004530.local",
                     "Oven": "SIEMENS-CM676G0S6-68A40E6EC669.local"}
    assert sb.homeconnect_profiles(tmp_path / "missing") == []


class E:
    def __init__(self, v, enum=None):
        self.value, self.enum = v, enum


class App:
    settings = {"BSH.Common.Setting.PowerState": E("On")}
    status = {"BSH.Common.Status.OperationState": E("Run"), "BSH.Common.Status.DoorState": E("Closed"),
              "BSH.Common.Status.RemoteControlStartAllowed": E(True),
              "Cooking.Oven.Status.CurrentCavityTemperature": E(165)}
    options = {"BSH.Common.Option.RemainingProgramTime": E(600), "BSH.Common.Option.ProgramProgress": E(30),
               "Cooking.Oven.Option.SetpointTemperature": E(180)}

    @property
    def active_program(self):
        return type("P", (), {"name": "Cooking.Oven.Program.HeatingMode.HotAir"})()


def test_state():
    st = sb.hc_state(App(), "oven")
    assert st["power"] == "on" and st["operation"] == "run" and st["door"] == "closed"
    assert st["program"] == "hotAir" and st["remainingTime"] == 600 and st["progress"] == 30
    assert st["temperature"] == 165.0 and st["setpoint"] == 180.0 and st["remoteStartAllowed"] is True
    assert st["completionTime"]


class Opt:
    def __init__(self, uid, name, access="readWrite", shadow=None):
        self.uid, self.name, self.access, self.value_shadow = uid, name, access, shadow


class Prog:
    def __init__(self, name, options=(), execution="selectandstart", available=True):
        self.name, self._options, self.execution, self.available = name, list(options), execution, available
        self.started = None

    async def start(self, options=None, *, override_options=False):
        self.started = (options, override_options)

    async def select(self, options=None, *, override_options=False):
        raise RuntimeError("not now")


def oven_app(allowed=True, op="Ready"):
    hot = Prog("Cooking.Oven.Program.HeatingMode.HotAir",
               [Opt(1, sb.HC_SETPOINT, shadow=160), Opt(2, sb.HC_DURATION, shadow=60), Opt(3, "X.Read", "read", 1)])
    app = App()
    app.status = dict(App.status)
    app.status["BSH.Common.Status.OperationState"] = E(op)
    app.status["BSH.Common.Status.RemoteControlStartAllowed"] = E(allowed)
    app.programs = {hot.name: hot,
                    "Cooking.Oven.Program.Cleaning.Pyrolysis": Prog("Cooking.Oven.Program.Cleaning.Pyrolysis",
                                                                    execution="selectonly"),
                    "Cooking.Oven.Program.Microwave.600Watt": Prog("Cooking.Oven.Program.Microwave.600Watt"),
                    "Cooking.Oven.Program.Dish.Automatic.Konv.Pavlova": Prog("x")}
    app.commands = {}
    type(app).selected_program = property(lambda self: None)
    return app, hot


def test_programs_and_options():
    app, hot = oven_app()
    assert sb.hc_programs(app) == ["hotAir", "micro600"]          # no select-only pyrolysis, no unknown dishes
    assert sb.hc_start_options(hot, "oven", {"setpoint": 200, "duration": 0}) == {1: 200}
    assert sb.hc_start_options(hot, "oven", {"setpoint": 200, "duration": 45}) == {1: 200, 2: 2700}
    dish = Prog(sb._HC_DW + "Eco50", [Opt(5, "Dishcare.Dishwasher.Option.HalfLoad", shadow=True),
                                      Opt(6, "Dishcare.Dishwasher.Option.HygienePlus", shadow=None),
                                      Opt(7, "BSH.Common.Option.StartInRelative", shadow=0)])
    assert sb.hc_start_options(dish, "dishwasher", {}) == {5: True}
    st = sb.hc_state(app, "oven", {"program": "micro600", "setpoint": 200, "duration": 30})
    assert st["programs"] == ["hotAir", "micro600"] and st["duration"] == 30


def test_commands(monkeypatch):
    import asyncio
    import pytest
    monkeypatch.setattr(sb, "load_config", lambda: {})
    monkeypatch.setattr(sb, "save_config", lambda cfg: None)
    hc = sb.HomeConnect({})
    app, hot = oven_app()
    hc.apps["OVEN"], hc.kinds["OVEN"] = app, "oven"
    h = hc._command("OVEN")
    asyncio.run(h("program", "hotAir"))
    asyncio.run(h("setpoint", 205))
    asyncio.run(h("duration", 30))
    assert hc.selection("OVEN") == {"program": "hotAir", "setpoint": 205, "duration": 30}
    asyncio.run(h("start", None))
    assert hot.started == ({1: 205, 2: 1800}, True)
    with pytest.raises(ValueError):
        asyncio.run(h("program", "nonsense"))
    app2, _ = oven_app(allowed=False)
    hc.apps["OVEN"] = app2
    with pytest.raises(ValueError, match="Remote Start"):
        asyncio.run(h("start", "hotAir"))
