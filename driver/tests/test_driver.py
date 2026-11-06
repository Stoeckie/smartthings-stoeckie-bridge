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
local function capmock(path)
  return setmetatable({}, {
    __index = function(t, k)
      if k == "NAME" then return path:match("([^.]+)$") end
      local v = capmock(path .. "." .. k); rawset(t, k, v); return v end,
    __call = function(_, a, b) return { path = path, arg = a } end })
end
local caps = capmock("cap")
for _, id in ipairs({ "windowShade", "windowShadeLevel", "refresh", "dryerOperatingState", "switch", "battery",
                      "powerMeter", "presenceSensor", "contactSensor",
                      "airConditionerMode", "airConditionerFanMode", "fanOscillationMode",
                      "thermostatCoolingSetpoint", "lock", "waterSensor", "relativeHumidityMeasurement", "rainSensor", "precipitationSensor",
                      "silvervalley08660.airconMode", "silvervalley08660.targetTemperature",
                      "silvervalley08660.airflowDirection" }) do caps[id].ID = id end
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
function NEW_DEVICE(info)
  local d = { id = info.id or ("dev" .. (#DEVICES + 1)), fields = {}, preferences = info.preferences or {},
              device_network_id = info.device_network_id, parent_device_id = info.parent_device_id,
              parent_assigned_child_key = info.parent_assigned_child_key, label = info.label,
              events = {}, state = "online",
              profile = { components = { main = { id = "main" }, outdoor = { id = "outdoor" }, plug = { id = "plug" },
                                         climate = { id = "climate" }, status = { id = "status" },
                                         rain = { id = "rain" }, graph = { id = "graph" } } } }
  function d:get_field(k) return self.fields[k] end
  function d:set_field(k, v) self.fields[k] = v end
  function d:emit_event(e) self.events[#self.events + 1] = { comp = "main", path = e.path, arg = e.arg } end
  function d:emit_component_event(c, e) self.events[#self.events + 1] = { comp = c.id, path = e.path, arg = e.arg } end
  function d:online() self.state = "online" end
  function d:offline() self.state = "offline" end
  function d:supports_capability() return false end
  function d:get_latest_state() return nil end
  function d:try_update_metadata(m) self.new_profile = m.profile end
  function d:get_parent_device() for _, p in ipairs(DEVICES) do if p.id == self.parent_device_id then return p end end end
  d.thread = { cancel_timer = function() end, call_with_delay = function() return {} end }
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
    assert ac.new_profile in (None, "stoeckie-aircon-v3")
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
    lua.execute("AC.events = {}")
    lua.execute("DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)")
    assert events()["main:cap.airConditionerMode.airConditionerMode"] == "minHeat"
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
    assert ev["main:cap.airConditionerMode.airConditionerMode"] == "fanOnly"
    assert ev["main:cap.fanOscillationMode.fanOscillationMode"] == "vertical"
    assert "main:cap.airConditionerMode.supportedAcModes" not in ev  # unchanged list is not re-emitted


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
    # simple mode: other profile, only rain or dry
    lua.execute('''
      RR.preferences = { rainMode = "simple" }
      DRIVER.lifecycle_handlers.infoChanged(DRIVER_OBJ, RR, "infoChanged", { old_st_store = { preferences = {} } })
      RR.events = {}
      DRIVER.capability_handlers.refresh.refresh(DRIVER_OBJ, PARENT)
    ''')
    rr = lua.eval("RR")
    assert rr.new_profile == "stoeckie-rainradar-simple"
    paths = [rr.events[i].path for i in range(1, len(rr.events) + 1)]
    assert paths == ["cap.rainSensor.rain.undetected"]


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
          for _, d in ipairs(DEVICES) do N = N + #d.events end
        ''')
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
