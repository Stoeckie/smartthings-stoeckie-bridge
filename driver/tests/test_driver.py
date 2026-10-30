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
                      "thermostatCoolingSetpoint" }) do caps[id].ID = id end
package.loaded["st.capabilities"] = caps
package.loaded["st.driver"] = function(name, tmpl) DRIVER = tmpl; return { run = function() end } end
package.loaded["log"] = { info = function(m) print("I " .. m) end, warn = function(m) print("W " .. m) end,
                          error = function(m) print("E " .. m) end, debug = function() end }
package.loaded["st.json"] = require "st_json"
package.loaded["ltn12"] = {
  source = { string = function(s) return s end },
  sink = { table = function(t) return t end } }
local http = { request = function(req)
  local code, body = py_http(req.method, req.url, req.source)
  req.sink[#req.sink + 1] = body
  return 1, code
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
                                         climate = { id = "climate" }, status = { id = "status" } } } }
  function d:get_field(k) return self.fields[k] end
  function d:set_field(k, v) self.fields[k] = v end
  function d:emit_event(e) self.events[#self.events + 1] = { comp = "main", path = e.path, arg = e.arg } end
  function d:emit_component_event(c, e) self.events[#self.events + 1] = { comp = c.id, path = e.path, arg = e.arg } end
  function d:online() self.state = "online" end
  function d:offline() self.state = "offline" end
  function d:supports_capability() return false end
  function d:get_latest_state() return nil end
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


@pytest.fixture(scope="module")
def bridge():
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
    yield port
    p.terminate()
    p.wait()


def py_http(method, url, body):
    method = method.decode() if isinstance(method, bytes) else method
    url = url.decode() if isinstance(url, bytes) else url
    data = body.encode() if isinstance(body, str) else body
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


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
