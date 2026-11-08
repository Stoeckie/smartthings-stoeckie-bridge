"""Run the Edge driver (src/init.lua) with a mocked SmartThings runtime against the real bridge in
--demo mode: poll creates the children, the next poll emits their state, commands reach the bridge.

    pip install lupa pytest aiohttp
    python -m pytest tests
"""
import json
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest
from lupa import lua53

HERE = pathlib.Path(__file__).resolve().parent
SRC = HERE.parent / "src"
BRIDGE = HERE.parents[1] / "bridge" / "stoeckie_bridge.py"

HARNESS = r'''
-- a call like cap.switch.switch.on() or cap.thermostatCoolingSetpoint.coolingSetpoint({ value = 21 }) gives an
-- event shaped like the hub's: { capability = { ID = .. }, attribute = { NAME = .. }, value = { value = .. } },
-- plus path/arg for the assertions
local function capmock(path, segs)
  segs = segs or { "cap" }
  return setmetatable({}, {
    __index = function(t, k)
      if k == "NAME" then return segs[#segs] end
      local s = {}
      for i, x in ipairs(segs) do s[i] = x end
      s[#s + 1] = k  -- one segment, also for a namespaced id like silvervalley08660.airconMode
      local v = capmock(path .. "." .. k, s); rawset(t, k, v); return v end,
    __call = function(_, a, b)
      local val = a
      if type(val) ~= "table" then val = { value = segs[4] or a } end
      return { path = path, arg = a, capability = { ID = segs[2] }, attribute = { NAME = segs[3] }, value = val,
               visibility = b and b.visibility } end })
end
local caps = capmock("cap")
for _, id in ipairs({ "windowShade", "windowShadeLevel", "refresh", "dryerOperatingState", "switch", "battery",
                      "powerMeter", "presenceSensor", "contactSensor",
                      "airConditionerMode", "airConditionerFanMode", "fanOscillationMode",
                      "thermostatCoolingSetpoint", "lock", "waterSensor", "relativeHumidityMeasurement", "rainSensor", "precipitationSensor",
                      "silvervalley08660.airconMode", "silvervalley08660.targetTemperature", "silvervalley08660.targetTemperatureF",
                      "silvervalley08660.airflowDirection", "silvervalley08660.applianceProgram",
                      "silvervalley08660.cookSettings", "silvervalley08660.washerProgram", "dishwasherOperatingState", "ovenOperatingState",
                      "washerOperatingState", "audioVolume", "audioMute", "mediaInputSource", "tvChannel",
                      "ovenSetpoint", "temperatureMeasurement", "remoteControlStatus" }) do caps[id].ID = id end
package.loaded["st.capabilities"] = caps
package.loaded["st.driver"] = function(name, tmpl) DRIVER = tmpl; return { run = function() end } end
package.loaded["log"] = { info = function(m) print("I " .. m) end, warn = function(m) print("W " .. m) end,
                          error = function(m) print("E " .. m) end, debug = function() end }
package.loaded["st.json"] = require "st_json"
package.loaded["ltn12"] = {
  source = { string = function(s) return s end },
  sink = { table = function(t) return t end } }
local http = { request = function(req)
  local code, body, rh = py_http(req.method, req.url, req.source, package.loaded["st.json"].encode(req.headers or {}))
  req.sink[#req.sink + 1] = body
  return 1, code, package.loaded["st.json"].decode(rh)
end }
package.loaded["cosock"] = { socket = { sleep = function() end }, asyncify = function() return http end,
  spawn = function(fn) fn() end }

DEVICES = {}
DELAYED = {}
function RUN_DELAYED() local l = DELAYED; DELAYED = {}; for _, f in ipairs(l) do f() end end
function NEW_DEVICE(info)
  local d = { id = info.id or ("dev" .. (#DEVICES + 1)), fields = {}, preferences = info.preferences or {},
              device_network_id = info.device_network_id, parent_device_id = info.parent_device_id,
              parent_assigned_child_key = info.parent_assigned_child_key, label = info.label,
              events = {}, state = "online",
              profile = { components = { main = { id = "main" }, outdoor = { id = "outdoor" }, plug = { id = "plug" },
                                         climate = { id = "climate" }, status = { id = "status" },
                                         rain = { id = "rain" }, graph = { id = "graph" },
                                         dewNow = { id = "dewNow" }, dewMax = { id = "dewMax" } } } }
  function d:get_field(k) return self.fields[k] end
  -- like the hub: a persisted table comes back as a proxy: fields can be read, but next() and pairs() see
  -- nothing (seen on a hub 2026-11-08: next() of a persisted table was nil right after set_field)
  function d:set_field(k, v, opts)
    if opts and opts.persist and type(v) == "table" then v = setmetatable({}, { __index = v }) end
    self.fields[k] = v
  end
  d.latest = {}  -- comp:path -> last emitted arg (what the app shows), survives events resets in the tests
  function d:emit_event(e) self.events[#self.events + 1] = { comp = "main", path = e.path, arg = e.arg }; self.latest["main:" .. e.path] = e.value end
  function d:emit_component_event(c, e) self.events[#self.events + 1] = { comp = c.id, path = e.path, arg = e.arg }; self.latest[c.id .. ":" .. e.path] = e.value end
  function d:online() self.state = "online" end
  function d:offline() self.state = "offline" end
  function d:supports_capability() return false end
  function d:get_latest_state() return nil end
  function d:try_update_metadata(m) self.new_profile = m.profile end
  function d:get_parent_device() for _, p in ipairs(DEVICES) do if p.id == self.parent_device_id then return p end end end
  -- delayed functions are collected; a test runs them with RUN_DELAYED()
  d.thread = { cancel_timer = function() end, call_with_delay = function(_, _, fn) DELAYED[#DELAYED + 1] = fn; return {} end }
  DEVICES[#DEVICES + 1] = d
  return d
end
DRIVER_OBJ = { get_devices = function() return DEVICES end,
  try_create_device = function(self, info) NEW_DEVICE(info) end }
'''


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def start_bridge():
    data = tempfile.mkdtemp()
    port = free_port()
    pathlib.Path(data, "config.json").write_text(json.dumps({"port": port, "bridge_id": "test1"}))
    env = dict(os.environ, STBRIDGE_DATA=data)
    p = subprocess.Popen([sys.executable, str(BRIDGE), "--demo"], env=env,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(50):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/stbridge/info", timeout=1)
            break
        except OSError:
            time.sleep(0.2)
    return port, p


@pytest.fixture(scope="module")
def bridge():
    port, p = start_bridge()
    yield port
    p.terminate()
    p.wait()


def py_http(method, url, body, headers=None):
    method = method.decode() if isinstance(method, bytes) else method
    url = url.decode() if isinstance(url, bytes) else url
    data = body.encode() if isinstance(body, str) else body
    hdrs = {"Content-Type": "application/json"}
    if headers:
        h = json.loads(headers.decode() if isinstance(headers, bytes) else headers)
        if isinstance(h, dict):
            hdrs.update({k: str(v) for k, v in h.items()})
    hdrs.pop("Content-Length", None)
    req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode(), json.dumps(dict(r.headers))
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(), json.dumps(dict(e.headers))


def test_aircon_flow(bridge):
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)   -- creates children
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)   -- emits state
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-aircon" then AC = d end end
    ''')
    ac = lua.eval("AC")
    assert ac is not None, "aircon child not created"

    ltype = lua.eval("type")

    def py(v):
        if ltype(v) != "table":
            return v
        d = {k: py(x) for k, x in v.items()}
        if d and all(isinstance(k, int) for k in d):
            return [d[k] for k in sorted(d)]
        return d

    def events():
        out = {}
        for i in range(1, len(ac.events) + 1):
            e = ac.events[i]
            arg = py(e.arg)
            if isinstance(arg, dict) and "value" in arg:
                arg = arg["value"]
            out[f"{e.comp}:{e.path}"] = arg
        return out

    def app_shows(key):
        """What the app shows for an attribute: the last emitted value (unchanged values are not emitted again)."""
        arg = py(ac.latest[key])
        if isinstance(arg, dict) and "value" in arg:
            arg = arg["value"]
        return arg

    ev = events()
    assert ev["main:cap.switch.switch.on"] is None
    assert ev["main:cap.airConditionerMode.airConditionerMode"] == "heat"
    assert ev["main:cap.thermostatCoolingSetpoint.coolingSetpoint"] == 21.0
    assert ev["main:cap.temperatureMeasurement.temperature"] == 19.5
    assert ev["outdoor:cap.temperatureMeasurement.temperature"] == 8.0
    assert ev["main:cap.airConditionerFanMode.fanMode"] == "auto"
    assert ev["main:cap.fanOscillationMode.fanOscillationMode"] == "fixed"
    assert "fanOnly" in ev["main:cap.airConditionerMode.supportedAcModes"]
    assert ev["main:cap.thermostatCoolingSetpoint.coolingSetpointRange"]["minimum"] == 16.0
    # Minimum Heat is one of the modes (like in the FGLair app), no extra switch or profile
    assert "minHeat" in ev["main:cap.airConditionerMode.supportedAcModes"]
    assert ac.new_profile == "stoeckie-aircon-v3-h"  # the demo unit has horizontal louvers
    assert ev["main:cap.silvervalley08660.airflowDirection.position"] == "2"
    assert ev["main:cap.silvervalley08660.airflowDirection.supportedPositions"] == ["1", "2", "3", "4"]
    assert ev["main:cap.silvervalley08660.targetTemperature.temperature"] == 21.0
    assert "minHeat" in ev["main:cap.silvervalley08660.airconMode.supportedModes"]

    def state():
        with urllib.request.urlopen(f"http://127.0.0.1:{bridge}/stbridge/devices") as r:
            devs = json.loads(r.read())["devices"]
        return next(d for d in devs if d["id"] == "demo-aircon")["state"]

    h = "DRIVER.capability_handlers"
    lua.execute(f'{h}.thermostatCoolingSetpoint.setCoolingSetpoint(DRIVER_OBJ, AC, {{ args = {{ setpoint = 23.5 }} }})')
    lua.execute(f'{h}.airConditionerMode.setAirConditionerMode(DRIVER_OBJ, AC, {{ args = {{ mode = "fanOnly" }} }})')
    lua.execute(f'{h}.airConditionerFanMode.setFanMode(DRIVER_OBJ, AC, {{ args = {{ fanMode = "quiet" }} }})')
    lua.execute(f'{h}.fanOscillationMode.setFanOscillationMode(DRIVER_OBJ, AC, {{ args = {{ fanOscillationMode = "vertical" }} }})')
    s = state()
    assert (s["setpoint"], s["mode"], s["fan"], s["swing"]) == (23.5, "fan", "quiet", "vertical")
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    assert state()["power"] == "off"
    lua.execute(f'{h}.switch.on(DRIVER_OBJ, AC)')
    assert state()["power"] == "on"
    lua.execute(f'{h}.airConditionerMode.setAirConditionerMode(DRIVER_OBJ, AC, {{ args = {{ mode = "minHeat" }} }})')
    assert state()["minHeat"] is True and state()["mode"] == "minheat" and state()["power"] == "on"
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    assert app_shows("main:cap.airConditionerMode.airConditionerMode") == "minHeat"
    lua.execute(f'{h}["silvervalley08660.airconMode"].setMode(DRIVER_OBJ, AC, {{ args = {{ mode = "fanOnly" }} }})')
    assert state()["minHeat"] is False and state()["mode"] == "fan"
    lua.execute(f'{h}["silvervalley08660.targetTemperature"].setTemperature(DRIVER_OBJ, AC, {{ args = {{ temperature = 22.5 }} }})')
    assert state()["setpoint"] == 22.5
    lua.execute(f'{h}["silvervalley08660.airflowDirection"].setPosition(DRIVER_OBJ, AC, {{ args = {{ position = "4" }} }})')
    assert state()["vDir"] == 4 and state()["swing"] == "off"
    lua.execute(f'{h}.fanOscillationMode.setFanOscillationMode(DRIVER_OBJ, AC, {{ args = {{ fanOscillationMode = "vertical" }} }})')

    lua.execute("AC.events = {}")
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    ev = events()
    assert app_shows("main:cap.airConditionerMode.airConditionerMode") == "fanOnly"
    assert app_shows("main:cap.fanOscillationMode.fanOscillationMode") == "vertical"
    assert "main:cap.airConditionerMode.supportedAcModes" not in ev  # unchanged list is not re-emitted
    # unchanged values are not emitted again at all (the hub would drop them, the cloud rate limit counts them)
    lua.execute("AC.events = {}")
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    assert events() == {}

    # the app sends the shown value when a list is opened and closed without a choice: nothing may be sent
    # (a mode command switches a Fujitsu unit on)
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    shown = lua.eval('AC.fields.ac_shown.mode')
    lua.execute(f'{h}["silvervalley08660.airconMode"].setMode(DRIVER_OBJ, AC, {{ args = {{ mode = "{shown}" }} }})')
    lua.execute(f'{h}["silvervalley08660.airflowDirection"].setPosition(DRIVER_OBJ, AC, {{ args = {{ position = "4" }} }})')
    lua.execute(f'{h}.airConditionerFanMode.setFanMode(DRIVER_OBJ, AC, {{ args = {{ fanMode = "quiet" }} }})')
    assert state()["power"] == "off"
    # a change while the unit is off is kept (a Fujitsu switches on with any command) and shown in the app
    before = state()
    lua.execute("AC.events = {}")
    lua.execute(f'{h}["silvervalley08660.horizontalAirflow"].setPosition(DRIVER_OBJ, AC, {{ args = {{ position = "5" }} }})')
    lua.execute(f'{h}["silvervalley08660.airconMode"].setMode(DRIVER_OBJ, AC, {{ args = {{ mode = "cool" }} }})')
    # Fahrenheit: a °F value from a US location is converted and switches the device to the °F profile
    lua.execute(f'{h}.thermostatCoolingSetpoint.setCoolingSetpoint(DRIVER_OBJ, AC, {{ args = {{ setpoint = 72 }} }})')
    s = state()
    assert s["power"] == "off" and s["mode"] == before["mode"] and s["setpoint"] == before["setpoint"]
    assert s["hDir"] == before["hDir"]
    ev = events()
    assert ev["main:cap.silvervalley08660.airconMode.mode"] == "cool"
    assert ev["main:cap.silvervalley08660.horizontalAirflow.position"] == "5"
    assert ev["main:cap.thermostatCoolingSetpoint.coolingSetpoint"] == 22.0
    assert lua.eval("AC.new_profile") == "stoeckie-aircon-v3-f-h"
    # the next poll keeps showing the kept changes, not the values of the unit
    lua.execute("AC.events = {}")
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    assert app_shows("main:cap.silvervalley08660.airconMode.mode") == "cool"
    assert app_shows("main:cap.silvervalley08660.targetTemperatureF.temperature") == 72
    assert "main:cap.silvervalley08660.airconMode.mode" not in events()  # no flip back to the unit's value
    # the same value again (list opened and closed) is still nothing
    lua.execute(f'{h}["silvervalley08660.airconMode"].setMode(DRIVER_OBJ, AC, {{ args = {{ mode = "cool" }} }})')
    assert state()["power"] == "off"
    # switching on sends the kept mode at once and the other kept changes a few seconds later
    lua.execute("DELAYED = {}")
    lua.execute(f'{h}.switch.on(DRIVER_OBJ, AC)')
    s = state()
    assert (s["power"], s["mode"], s["hDir"]) == ("on", "cool", before["hDir"])
    lua.execute("RUN_DELAYED()")
    s = state()
    assert (s["power"], s["mode"], s["setpoint"], s["hDir"]) == ("on", "cool", 22.0, 5)
    assert lua.eval("AC.fields.ac_pending") is None
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    assert app_shows("main:cap.silvervalley08660.targetTemperatureF.temperature") == 72
    lua.execute(f'{h}["silvervalley08660.targetTemperatureF"].setTemperature(DRIVER_OBJ, AC, {{ args = {{ temperature = 68 }} }})')
    assert state()["setpoint"] == 20.0
    # setting "Changes while off: Send now": a change while off goes out right away (and switches the unit on)
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    lua.execute('AC.preferences.offChanges = "send"')
    lua.execute(f'{h}["silvervalley08660.airconMode"].setMode(DRIVER_OBJ, AC, {{ args = {{ mode = "heat" }} }})')
    assert (state()["power"], state()["mode"]) == ("on", "heat")
    lua.execute('AC.preferences.offChanges = nil')
    # switched off again before the delayed sends: they are dropped (else the unit would switch on again)
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    lua.execute(f'{h}.airConditionerFanMode.setFanMode(DRIVER_OBJ, AC, {{ args = {{ fanMode = "low" }} }})')
    lua.execute("DELAYED = {}")
    lua.execute(f'{h}.switch.on(DRIVER_OBJ, AC)')
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    lua.execute("RUN_DELAYED()")
    assert state()["power"] == "off" and state()["fan"] != "low"
    # a list opened right after switching off (before the next poll) does not switch the unit on again
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    lua.execute(f'{h}["silvervalley08660.airconMode"].setMode(DRIVER_OBJ, AC, {{ args = {{ mode = "dry" }} }})')
    assert state()["power"] == "off"
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    # switched off while changes were kept: they are dropped
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    lua.execute(f'{h}.airConditionerFanMode.setFanMode(DRIVER_OBJ, AC, {{ args = {{ fanMode = "high" }} }})')
    assert json.loads(lua.eval("AC.fields.ac_pending"))["fan"]["value"] == "high"
    lua.execute(f'{h}.switch.off(DRIVER_OBJ, AC)')
    assert lua.eval("AC.fields.ac_pending") is None
    # the driver reports what it shows to the bridge (setup page)
    with urllib.request.urlopen(f"http://127.0.0.1:{bridge}/setup") as r:
        page = r.read().decode()
    assert "Demo air conditioner in SmartThings: driver" in page and "stoeckie-aircon-v3-f-h" in page
    assert "last commands from SmartThings" in page and "version 2026-11-09a" in page


def test_aircon_unit_setting(bridge):
    """Setting Temperature unit to °F switches the device to the profile with the °F slider."""
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-aircon" then AC = d end end
    ''')
    assert lua.eval("AC.new_profile") == "stoeckie-aircon-v3-h"
    lua.execute('AC.preferences.temperatureUnit = "F"; DRIVER.lifecycle_handlers.infoChanged(DRIVER_OBJ, AC, "infoChanged", '
                '{ old_st_store = { preferences = { temperatureUnit = "auto" } } })')
    assert lua.eval("AC.new_profile") == "stoeckie-aircon-v3-f-h"
    lua.execute('AC.preferences.temperatureUnit = "C"; DRIVER.lifecycle_handlers.infoChanged(DRIVER_OBJ, AC, "infoChanged", '
                '{ old_st_store = { preferences = { temperatureUnit = "F" } } })')
    assert lua.eval("AC.new_profile") == "stoeckie-aircon-v3-h"


def test_car_flow(bridge):
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-car" then CAR = d end end
    ''')
    car = lua.eval("CAR")
    assert car is not None
    ev = {}
    for i in range(1, len(car.events) + 1):
        e = car.events[i]
        ev[f"{e.comp}:{e.path}"] = e.arg
    assert ev["main:cap.battery.battery"] == 64
    assert "main:cap.switch.switch.off" in ev and "status:cap.presenceSensor.presence.present" in ev
    assert "plug:cap.contactSensor.contact.closed" in ev and "climate:cap.switch.switch.off" in ev
    assert "status:cap.contactSensor.contact.closed" in ev

    def state():
        with urllib.request.urlopen(f"http://127.0.0.1:{bridge}/stbridge/devices") as r:
            devs = json.loads(r.read())["devices"]
        return next(d for d in devs if d["id"] == "demo-car")["state"]
    lua.execute('DRIVER.capability_handlers.switch.on(DRIVER_OBJ, CAR, { component = "main" })')
    assert state()["charging"] == "CHARGING"
    lua.execute('DRIVER.capability_handlers.switch.on(DRIVER_OBJ, CAR, { component = "climate" })')
    assert state()["climate"] == "ON"


def test_velux_lock_flow(bridge):
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-velux" then LOCK = d end end
    ''')
    lock = lua.eval("LOCK")
    assert lock is not None
    paths = [f"{lock.events[i].comp}:{lock.events[i].path}" for i in range(1, len(lock.events) + 1)]
    assert "main:cap.lock.lock.locked" in paths
    lua.execute('for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-rain" then RAIN = d end end')
    rain = lua.eval("RAIN")
    assert rain is not None and rain.events[1].path == "cap.rainSensor.rain.undetected"

    def state():
        with urllib.request.urlopen(f"http://127.0.0.1:{bridge}/stbridge/devices") as r:
            devs = json.loads(r.read())["devices"]
        return next(d for d in devs if d["id"] == "demo-velux")["state"]
    lua.execute('DRIVER.capability_handlers.lock.unlock(DRIVER_OBJ, LOCK, {})')
    assert state()["locked"] is False
    lua.execute('DRIVER.capability_handlers.lock.lock(DRIVER_OBJ, LOCK, {})')
    assert state()["locked"] is True


def test_car_battery_graph(bridge):
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-car" then CAR = d end end
      CAR.preferences = {{ batteryGraph = "humidity" }}
      DRIVER.lifecycle_handlers.infoChanged(DRIVER_OBJ, CAR, "infoChanged", {{ old_st_store = {{ preferences = {{}} }} }})
      CAR.events = {{}}
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
    ''')
    car = lua.eval("CAR")
    assert car.new_profile == "stoeckie-car-v2-hum"
    ev = {f"{car.events[i].comp}:{car.events[i].path}": car.events[i].arg for i in range(1, len(car.events) + 1)}
    assert ev["main:cap.relativeHumidityMeasurement.humidity"] == 64
    lua.execute('''
      CAR.preferences = { batteryGraph = "power" }
      DRIVER.lifecycle_handlers.infoChanged(DRIVER_OBJ, CAR, "infoChanged", { old_st_store = { preferences = {} } })
      CAR.events = {}
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
    ''')
    car = lua.eval("CAR")
    assert car.new_profile == "stoeckie-car-v2-pow"
    ev = {f"{car.events[i].comp}:{car.events[i].path}": car.events[i].arg for i in range(1, len(car.events) + 1)}
    assert ev["graph:cap.powerMeter.power"]["value"] == 64


def test_rainradar(bridge):
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-rainradar" then RR = d end end
    ''')
    rr = lua.eval("RR")
    ev = {f"{rr.events[i].comp}:{rr.events[i].path}": rr.events[i].arg for i in range(1, len(rr.events) + 1)}
    assert "main:cap.rainSensor.rain.undetected" in ev
    assert ev["main:cap.precipitationSensor.precipitationIntensity"] == "none"
    assert ev["main:cap.silvervalley08660.rainForecast.minutesUntilRain"]["value"] == 20
    assert ev["main:cap.silvervalley08660.rainForecast.minutesOfRain"]["value"] == 15
    assert ev["main:cap.silvervalley08660.rainForecast.summary"].startswith("Rain in 20 min")
    # dew point now and the highest in the next 48 h, each in its own component
    assert ev["dewNow:cap.temperatureMeasurement.temperature"]["value"] == 7.4
    assert ev["dewMax:cap.temperatureMeasurement.temperature"]["value"] == 11.2
    # simple mode: other profile, only rain or dry
    lua.execute('''
      RR.preferences = { rainMode = "simple" }
      DRIVER.lifecycle_handlers.infoChanged(DRIVER_OBJ, RR, "infoChanged", { old_st_store = { preferences = {} } })
      RR.events = {}
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
    ''')
    rr = lua.eval("RR")
    assert rr.new_profile == "stoeckie-rainradar-v3-simple"
    # unchanged values are not emitted again; what the app shows stays rain or dry plus the dew points
    paths = [f"{rr.events[i].comp}:{rr.events[i].path}" for i in range(1, len(rr.events) + 1)]
    assert paths == []
    latest = sorted(k for k in rr.latest.keys())
    assert "main:cap.rainSensor.rain.undetected" in latest and "dewMax:cap.temperatureMeasurement.temperature" in latest


def test_dryer_no_completion_time_spam(bridge):
    """The demo dryer runs: its end time is emitted once, not again on every poll."""
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      for i = 1, 4 do DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT) end
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-dryer" then DRYER = d end end
    ''')
    dryer = lua.eval("DRYER")
    paths = [dryer.events[i].path for i in range(1, len(dryer.events) + 1)]
    assert sum("completionTime" in p for p in paths) == 1


def test_pairing_signed_api():
    """Koppelen on the setup page, the driver pairs at its next poll and signs from then on; a forged token fails."""
    port, proc = start_bridge()
    try:
        lua = lua53.LuaRuntime(unpack_returned_tuples=True)
        lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
        lua.globals().py_http = py_http
        lua.execute(HARNESS)
        lua.execute(f'dofile("{SRC}/init.lua")')
        lua.execute(f'''
          PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                                preferences = {{ ipAddress = "127.0.0.1", port = "{port}" }} }})
          DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
          DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
        ''')
        assert lua.eval("PARENT.fields.api_token") is None
        # press Koppelen (the setup page accepts localhost)
        form = urllib.parse.urlencode({"section": "api_pair"}).encode()
        req = urllib.request.Request(f"http://127.0.0.1:{port}/setup", data=form, method="POST")
        try:
            urllib.request.urlopen(req, timeout=5)
        except urllib.error.HTTPError:
            pass
        lua.execute('''
          for _, d in ipairs(DEVICES) do d.events = {} end
          DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
        ''')
        token = lua.eval("PARENT.fields.api_token")
        assert token and len(token) == 64
        # unsigned requests are refused now, signed ones work (children get their state)
        assert py_http("GET", f"http://127.0.0.1:{port}/stbridge/devices", None)[0] == 401
        lua.execute('''
          for _, d in ipairs(DEVICES) do d.events = {} end
          DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
          N = 0
          for _, d in ipairs(DEVICES) do for _ in pairs(d.latest) do N = N + 1 end end
        ''')
        # the children have their state (unchanged values are not emitted again, so count what the app shows)
        assert lua.eval("N") > 0 and lua.eval("PARENT.state") == "online"
        # a wrong token: the bridge refuses and the hub does not accept an unproven new token
        lua.execute('''
          PARENT.fields.api_token = string.rep("0", 64)
          DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
        ''')
        assert lua.eval("PARENT.state") == "offline"
        assert lua.eval("PARENT.fields.api_token") == "0" * 64
    finally:
        proc.terminate()
        proc.wait()


def test_homeconnect_appliances(bridge):
    """Demo dishwasher and oven (Home Connect via the bridge): states, profiles and commands."""
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    lua.execute(f'package.path = "{SRC}/?.lua;{HERE}/?.lua;" .. package.path')
    lua.globals().py_http = py_http
    lua.execute(HARNESS)
    lua.execute(f'dofile("{SRC}/init.lua")')
    lua.execute(f'''
      PARENT = NEW_DEVICE({{ id = "p1", label = "Bridge", device_network_id = "stbridge-test1",
                            preferences = {{ ipAddress = "127.0.0.1", port = "{bridge}" }} }})
      DRIVER.lifecycle_handlers.init(DRIVER_OBJ, PARENT)
      for i = 1, 3 do DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT) end
      for _, d in ipairs(DEVICES) do
        if d.parent_assigned_child_key == "demo-dishwasher" then DISH = d end
        if d.parent_assigned_child_key == "demo-oven" then OVEN = d end
      end
    ''')
    dish, oven = lua.eval("DISH"), lua.eval("OVEN")
    assert dish is not None and oven is not None
    dpaths = [dish.events[i].path for i in range(1, len(dish.events) + 1)]
    opaths = [oven.events[i].path for i in range(1, len(oven.events) + 1)]
    assert "cap.dishwasherOperatingState.machineState.run" in dpaths
    assert "cap.ovenOperatingState.machineState.running" in opaths
    assert "cap.ovenSetpoint.ovenSetpoint" in opaths and "cap.temperatureMeasurement.temperature" in opaths
    lua.execute('DRIVER.capability_handlers.ovenOperatingState.stop(DRIVER_OBJ, OVEN, {})')
    lua.execute('DRIVER.capability_handlers.switch.off(DRIVER_OBJ, DISH, {})')
    st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{bridge}/stbridge/devices", timeout=5).read())
    by = {d["id"]: d["state"] for d in st["devices"]}
    assert by["demo-oven"]["operation"] == "inactive" and by["demo-dishwasher"]["power"] == "off"
    assert "cap.silvervalley08660.applianceProgram.supportedPrograms" in opaths
    assert "cap.silvervalley08660.cookSettings.temperature" in opaths
    h = 'DRIVER.capability_handlers["silvervalley08660.applianceProgram"]'
    c = 'DRIVER.capability_handlers["silvervalley08660.cookSettings"]'
    lua.execute(f'{c}.setTemperature(DRIVER_OBJ, OVEN, {{ args = {{ temperature = 200 }} }})')
    lua.execute(f'{c}.setDuration(DRIVER_OBJ, OVEN, {{ args = {{ duration = 45 }} }})')
    lua.execute(f'{h}.startProgram(DRIVER_OBJ, OVEN, {{ args = {{ program = "pizza" }} }})')
    lua.execute(f'{h}.setProgram(DRIVER_OBJ, DISH, {{ args = {{ program = "quick45" }} }})')
    lua.execute('DRIVER.capability_handlers.dishwasherOperatingState.setMachineState(DRIVER_OBJ, DISH, { args = { state = "run" } })')
    st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{bridge}/stbridge/devices", timeout=5).read())
    by = {d["id"]: d["state"] for d in st["devices"]}
    assert by["demo-oven"]["program"] == "pizza" and by["demo-oven"]["setpoint"] == 200.0
    assert by["demo-oven"]["duration"] == 45 and by["demo-oven"]["operation"] == "run"
    assert by["demo-dishwasher"]["program"] == "quick45" and by["demo-dishwasher"]["operation"] == "run"
    lua.execute('DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)')
    vals = {oven.events[i].path: oven.events[i].arg for i in range(1, len(oven.events) + 1)}
    assert vals.get("cap.silvervalley08660.applianceProgram.program") == "pizza"
    assert lua.eval("OVEN").new_profile in (None, "stoeckie-oven-v2")
    lua.execute('''
      for _, d in ipairs(DEVICES) do if d.parent_assigned_child_key == "demo-washer" then WASHER = d end end
    ''')
    washer = lua.eval("WASHER")
    assert washer is not None
    wpaths = [washer.events[i].path for i in range(1, len(washer.events) + 1)]
    assert "cap.washerOperatingState.machineState.run" in wpaths
    assert "cap.washerOperatingState.washerJobState.wash" in wpaths
    lua.execute('DRIVER.capability_handlers.washerOperatingState.setMachineState(DRIVER_OBJ, WASHER, { args = { state = "pause" } })')
    st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{bridge}/stbridge/devices", timeout=5).read())
    by = {d["id"]: d["state"] for d in st["devices"]}
    assert by["demo-washer"]["operation"] == "pause"
    vals = {washer.events[i].path: washer.events[i].arg for i in range(1, len(washer.events) + 1)}
    assert vals.get("cap.silvervalley08660.washerProgram.program") == "cotton"
    lua.execute('DRIVER.capability_handlers["silvervalley08660.washerProgram"].startProgram(DRIVER_OBJ, WASHER, { args = { program = "quick15" } })')
    st = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{bridge}/stbridge/devices", timeout=5).read())
    by = {d["id"]: d["state"] for d in st["devices"]}
    assert by["demo-washer"]["program"] == "quick15" and by["demo-washer"]["operation"] == "run"
