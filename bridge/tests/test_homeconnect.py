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
    make_zip(tmp_path / "dish.zip", "012345678901234567", "Dishwasher")
    make_zip(tmp_path / "oven.zip", "SIEMENS-CM676G0S6-68A40E6EC669", "Oven", conn="AES", iv="iv")
    (tmp_path / "junk.zip").write_bytes(b"not a zip")
    profs = sb.homeconnect_profiles(tmp_path)
    assert sorted(p["type"] for p in profs) == ["Dishwasher", "Oven"]
    assert all(p["xml"][0] == "<device/>" for p in profs)
    hosts = {p["type"]: sb.hc_default_host(p) for p in profs}
    assert hosts == {"Dishwasher": "siemens-dishwasher-012345678901234567.local",
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


def test_off_stops_a_running_program_first(monkeypatch):
    # the oven refused Standby while heating: off worked only from the Siemens app (DS 15-11)
    import asyncio
    monkeypatch.setattr(sb, "load_config", lambda: {})
    monkeypatch.setattr(sb, "save_config", lambda cfg: None)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(sb.asyncio, "sleep", no_sleep)
    order = []

    class Power:
        enum = {1: "On", 2: "Standby"}

        async def set_value(self, v):
            order.append(("power", v))

    class Abort:
        async def execute(self, v):
            order.append(("abort", v))

    hc = sb.HomeConnect({})
    app, _ = oven_app(op="Run")
    app.settings = {"BSH.Common.Setting.PowerState": Power()}
    app.commands = {"BSH.Common.Command.AbortProgram": Abort()}
    hc.apps["OVEN"], hc.kinds["OVEN"] = app, "oven"
    asyncio.run(hc._command("OVEN")("off", None))
    assert order == [("abort", 1), ("power", "Standby")]
    order.clear()
    app.status["BSH.Common.Status.OperationState"] = E("Ready")
    asyncio.run(hc._command("OVEN")("off", None))
    assert order == [("power", "Standby")]
    assert sb.hc_program_active("Run") and not sb.hc_program_active("Ready") and not sb.hc_program_active(None)


class Delayed(App):
    status = dict(App.status, **{"BSH.Common.Status.OperationState": E("DelayedStart")})
    options = dict(App.options, **{"BSH.Common.Option.StartInRelative": E(3600)})


def test_delayed_start_counts_the_wait():
    app = Delayed()
    st = sb.hc_state(app, "dishwasher")
    assert st["operation"] == "delayedStart"
    assert 4190 <= st["remainingTime"] <= 4200          # 3600 s wait + 600 s program
    # the appliance does not count StartInRelative down: the first sighting is the anchor
    app._stb_delay = (3600, sb.time.monotonic() - 600)
    assert 3590 <= sb.hc_state(app, "dishwasher")["remainingTime"] <= 3600
    assert sb.hc_delay_left(App(), "Run") is None


POWER, OPER = "BSH.Common.Setting.PowerState", "BSH.Common.Status.OperationState"


class Ent:
    """An entity as the library has it: a value (maybe from the description) and update callbacks."""

    def __init__(self, v=None):
        self.value, self.enum, self.cbs = v, None, []

    def register_callback(self, cb):
        self.cbs.append(cb)

    async def report(self, v):                  # the appliance tells its value
        self.value = v
        for cb in self.cbs:
            await cb(self)


def test_first_state_waits_for_the_appliance():
    import asyncio

    class Oven:                                 # the description gives the oven power "On" before it has said anything
        def __init__(self):
            self.settings, self.status, self.options = {POWER: Ent("On")}, {OPER: Ent()}, {}

    first = sb.HcReported(Oven(), 3)
    assert first.has_key and not first.ready()  # not connected: nothing, whatever the description says
    first.connected = True
    assert [first.ready() for _ in range(4)] == [False, False, False, True]    # silent appliance: after the limit
    app = Oven()
    first = sb.HcReported(app, 3)
    first.connected = True
    asyncio.run(app.status[OPER].report("Run"))
    assert first.ready()

    class Fridge:                               # no power or operation state at all: shown once connected
        settings, status, options = {}, {}, {}
    first = sb.HcReported(Fridge(), 3)
    assert not first.has_key and not first.ready()
    first.connected = True
    assert first.ready()


def run_one(monkeypatch, scenario):
    """Run HomeConnect._run_one against a fake library; scenario(app, dev, states) drives the appliance."""
    import asyncio
    import sys
    import types
    monkeypatch.setattr(sb, "load_config", lambda: {})
    monkeypatch.setattr(sb, "save_config", lambda cfg: None)
    reg = sb.Registry()
    states = []                                 # every state the bridge showed, in order
    upsert = reg.upsert

    def spy(dev_id, dev_type, name, state, online=True, handler=None):
        states.append(dict(state))
        upsert(dev_id, dev_type, name, state, online=online, handler=handler)
    reg.upsert = spy
    monkeypatch.setattr(sb, "REG", reg)
    monkeypatch.setattr(sb.HomeConnect, "POLL", 0.05)
    made = {}

    class FakeAppliance:
        active_program = None
        selected_program = None

        def __init__(self, desc, host, name, app_id, key, iv, **kw):
            self.cb = kw["connection_callback"]
            # as the library does: values from the description before the appliance has said anything
            self.settings, self.status = {POWER: Ent("On")}, {OPER: Ent()}
            self.options, self.programs, self.commands = {}, {}, {}
            made["app"] = self

        async def connect(self):
            return None

    fake = types.ModuleType("homeconnect_websocket")
    fake.HomeAppliance = FakeAppliance
    fake.parse_device_description = lambda *xml: {}
    monkeypatch.setitem(sys.modules, "homeconnect_websocket", fake)

    async def main():
        hc = sb.HomeConnect({})
        prof = {"haId": "OVEN1", "type": "Oven", "brand": "SIEMENS", "vib": "X1", "key": "k", "xml": ("<d/>", "<f/>")}
        task = asyncio.create_task(hc._run_one(prof, "appid", None))
        try:
            await asyncio.sleep(0.3)            # several polls: the socket is up, the appliance has not reported
            await scenario(made["app"], lambda: reg.devices["homeconnect-OVEN1"], states)
        finally:
            task.cancel()
    asyncio.run(main())
    return states


def test_bridge_start_shows_no_state_from_the_description(monkeypatch):
    # DS 17-11: at every bridge start the oven showed "on" for some seconds (the description says power On) and a
    # running dishwasher showed "stopped", so the "done" notification came. Nothing may be shown before the
    # appliance itself has reported.
    import asyncio

    async def standby_oven(app, dev, states):
        assert dev()["state"] == {} and dev()["online"] is False
        await app.settings[POWER].report("Standby")
        await app.status[OPER].report("Inactive")
        await app.cb("connected")               # the library calls this after reading all values
        await asyncio.sleep(0.8)
        assert dev()["online"] is True
        assert dev()["state"]["power"] == "off" and dev()["state"]["operation"] == "inactive"

    states = run_one(monkeypatch, standby_oven)
    assert not any(st.get("power") == "on" for st in states), states

    async def running_oven(app, dev, states):
        await app.settings[POWER].report("On")
        await app.status[OPER].report("Run")
        await app.cb("connected")
        await asyncio.sleep(0.8)
        assert dev()["state"]["operation"] == "run"

    states = run_one(monkeypatch, running_oven)
    shown = [st["operation"] for st in states if "operation" in st]
    assert shown and set(shown) == {"run"}, states  # never "inactive" (= ready, the "done" trigger) on the way


def test_connected_but_silent_appliance_is_shown_after_a_minute(monkeypatch):
    import asyncio
    monkeypatch.setattr(sb.HomeConnect, "FIRST_STATE_POLLS", 30)     # 30 polls of 0.05 s in this test

    async def silent(app, dev, states):
        await app.cb("connected")
        await asyncio.sleep(1.0)                # connected for a while, power and operation state never came
        assert dev()["state"] == {}
        for _ in range(100):                    # shown with what there is once the limit has passed
            if dev()["state"]:
                break
            await asyncio.sleep(0.1)
        assert dev()["state"].get("power") == "on"

    run_one(monkeypatch, silent)


def test_the_library_fills_values_from_the_description():
    # the reason for HcReported, checked against the real library when it is installed
    import asyncio
    import pytest
    hc = pytest.importorskip("homeconnect_websocket")
    desc = {"info": {},
            "setting": [{"uid": 539, "name": POWER, "access": "readwrite", "available": True, "initValue": "2",
                         "protocolType": "Integer", "enumeration": {"1": "Off", "2": "On", "3": "Standby"}}],
            "status": [{"uid": 552, "name": OPER, "access": "read", "available": True, "protocolType": "Integer",
                        "enumeration": {"1": "Inactive", "2": "Ready", "3": "Run"}}]}

    async def main():
        app = hc.HomeAppliance(desc, "127.0.0.1", "test", "id", "AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
        first = sb.HcReported(app, 3)
        assert sb.hc_state(app, "oven")["power"] == "on"        # before the appliance has said anything
        assert first.has_key and not first.real
        await app._update_entities([{"uid": 539, "value": 3}, {"uid": 552, "value": 3}])   # the appliance reports
        await asyncio.sleep(0.05)
        st = sb.hc_state(app, "oven")
        assert first.real and st["power"] == "off" and st["operation"] == "run"
    asyncio.run(main())
