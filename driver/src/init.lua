-- SmartThings Edge driver: Stoeckie Bridge (local).
-- One parent device per bridge (a pc in the LAN running stoeckie_bridge.py). The parent polls
-- GET /stbridge/devices and creates/updates one child device per bridge device:
--   dryer  (AEG via Electrolux API)  -> dryerOperatingState, remoteControlStatus, contactSensor (door)
--   window (VELUX ACTIVE via HomeKit) -> windowShade, windowShadeLevel
--   sensor (VELUX climate sensor)     -> temperature, humidity, CO2
--   aircon (Fujitsu via FGLair)       -> switch, airConditionerMode, setpoint, fan, swing, indoor/outdoor temp
--   car    (Škoda public API)         -> battery, charging switch + power, at home, plug, climate, doors
local Driver = require "st.driver"
local capabilities = require "st.capabilities"
local log = require "log"
local cosock = require "cosock"
local api = require "bridge_api"

local BUILD = "2026-10-30d"
local FAST_POLL_SECS = 2
local FAST_POLL_FOR = 40
local SCAN_CONCURRENCY = 16
local DEFAULT_PORT = 8787

local windowShade = capabilities.windowShade
local windowShadeLevel = capabilities.windowShadeLevel
local dryerOp = capabilities.dryerOperatingState
local remoteControl = capabilities.remoteControlStatus
local contact = capabilities.contactSensor
local acMode = capabilities.airConditionerMode
local acFan = capabilities.airConditionerFanMode
local oscillation = capabilities.fanOscillationMode
local coolSetpoint = capabilities.thermostatCoolingSetpoint

local PROFILE = { dryer = "stoeckie-dryer", window = "stoeckie-window", sensor = "stoeckie-climate",
                  aircon = "stoeckie-aircon", car = "stoeckie-car" }
local MANUFACTURER = { dryer = "AEG", aircon = "Fujitsu", car = "Skoda" }

-- bridge value <-> SmartThings value
local AC_MODE_TO_ST = { auto = "auto", cool = "cool", dry = "dry", fan = "fanOnly", heat = "heat" }
local AC_MODE_FROM_ST = { auto = "auto", cool = "cool", dry = "dry", fanOnly = "fan", fan = "fan", heat = "heat" }
local SWING_TO_ST = { off = "fixed", vertical = "vertical", horizontal = "horizontal", both = "all" }
local SWING_FROM_ST = { fixed = "off", vertical = "vertical", horizontal = "horizontal", all = "both" }

local state = {}       -- parent device.id -> { poll_timer, fast_until }
local discovered = {}  -- dni -> { ip, port, info }

-- helpers ------------------------------------------------------------------

local function is_parent(device) return device.parent_assigned_child_key == nil end

local function bridge_addr(parent)
  local p = parent.preferences or {}
  local ip = p.ipAddress
  if ip == nil or ip == "" or ip == "auto" then ip = parent:get_field("ip") end
  local port = tonumber(p.port) or parent:get_field("port") or DEFAULT_PORT
  return ip, port
end

local function children_by_key(driver, parent)
  local out = {}
  for _, dev in ipairs(driver:get_devices()) do
    if dev.parent_device_id == parent.id and dev.parent_assigned_child_key then
      out[dev.parent_assigned_child_key] = dev
    end
  end
  return out
end

-- state -> events --------------------------------------------------------------

-- emit one event; an unknown enum value only skips that event, not the rest
local function try_emit(dev, make)
  local ok, err = pcall(function() dev:emit_event(make()) end)
  if not ok then log.warn("[stbridge] " .. dev.label .. ": " .. tostring(err)) end
end

local function dryer_job(s)
  local a, ph = s.applianceState or "OFF", (s.cyclePhase or ""):upper()
  if a == "END_OF_CYCLE" then return "stop", "finished" end
  if a == "DELAYED_START" then return "run", "delayWash" end
  if a == "RUNNING" or a == "PAUSED" then
    local job = "drying"
    if ph:find("COOL") then job = "cooling"
    elseif ph:find("CREASE") or ph:find("WRINKLE") then job = "wrinklePrevent"
    elseif ph:find("WEIGH") or ph:find("SENS") then job = "weightSensing" end
    return (a == "PAUSED") and "pause" or "run", job
  end
  return "stop", "none"
end

local function emit_dryer(dev, s)
  local machine, job = dryer_job(s)
  try_emit(dev, function() return dryerOp.machineState[machine]() end)
  try_emit(dev, function() return dryerOp.dryerJobState[job]() end)
  local ct = s.completionTime or os.date("!%Y-%m-%dT%H:%M:%SZ")
  try_emit(dev, function() return dryerOp.completionTime(ct) end)
  local rc = tostring(s.remoteControl or ""):upper()
  local enabled = (rc:find("ENABLED") and not rc:find("DISABLED")) and "true" or "false"
  try_emit(dev, function() return remoteControl.remoteControlEnabled(enabled) end)
  local door = tostring(s.doorState or ""):upper()
  if door ~= "" then
    try_emit(dev, function() return door == "OPEN" and contact.contact.open() or contact.contact.closed() end)
  end
end

local function emit_window(dev, s)
  local pos = tonumber(s.position)
  if pos == nil then return end
  pos = math.max(0, math.min(100, math.floor(pos + 0.5)))
  dev:emit_event(windowShadeLevel.shadeLevel(pos))
  local ps = s.positionState
  if ps == "opening" then dev:emit_event(windowShade.windowShade.opening())
  elseif ps == "closing" then dev:emit_event(windowShade.windowShade.closing())
  elseif pos >= 99 then dev:emit_event(windowShade.windowShade.open())
  elseif pos <= 1 then dev:emit_event(windowShade.windowShade.closed())
  else dev:emit_event(windowShade.windowShade.partially_open()) end
end

local function emit_sensor(dev, s)
  if s.temperature then
    dev:emit_event(capabilities.temperatureMeasurement.temperature({ value = tonumber(s.temperature), unit = "C" }))
  end
  if s.humidity then
    dev:emit_event(capabilities.relativeHumidityMeasurement.humidity(math.floor(tonumber(s.humidity) + 0.5)))
  end
  if s.co2 then
    dev:emit_event(capabilities.carbonDioxideMeasurement.carbonDioxide({ value = tonumber(s.co2), unit = "ppm" }))
  end
end

local function map_list(list, map)
  local out = {}
  for _, v in ipairs(list or {}) do
    local m = map and map[v] or v
    if m then out[#out + 1] = m end
  end
  return out
end

-- supported* lists change rarely: only emit them when they differ from last time
local function emit_list(dev, key, list, make)
  local enc = table.concat(list, ",")
  if dev:get_field(key) == enc then return end
  dev:set_field(key, enc)
  try_emit(dev, function() return make(list, { visibility = { displayed = false } }) end)
end

local function emit_aircon(dev, s)
  try_emit(dev, function() return s.power == "off" and capabilities.switch.switch.off() or capabilities.switch.switch.on() end)
  local m = AC_MODE_TO_ST[s.mode or ""]
  if m then try_emit(dev, function() return acMode.airConditionerMode(m) end) end
  emit_list(dev, "acModes", map_list(s.supportedModes, AC_MODE_TO_ST), acMode.supportedAcModes)
  if s.setpoint then
    try_emit(dev, function() return coolSetpoint.coolingSetpoint({ value = tonumber(s.setpoint), unit = "C" }) end)
  end
  local r = s.setpointRange
  if type(r) == "table" and r[1] and r[2] then
    local enc = r[1] .. "-" .. r[2]
    if dev:get_field("spRange") ~= enc then
      dev:set_field("spRange", enc)
      try_emit(dev, function()
        return coolSetpoint.coolingSetpointRange({ value = { minimum = r[1], maximum = r[2], step = 0.5 }, unit = "C" })
      end)
    end
  end
  if s.temperature then
    try_emit(dev, function() return capabilities.temperatureMeasurement.temperature({ value = tonumber(s.temperature), unit = "C" }) end)
  end
  if s.outdoorTemperature and dev.profile.components.outdoor then
    local t = tonumber(s.outdoorTemperature)
    local ok, err = pcall(function()
      dev:emit_component_event(dev.profile.components.outdoor,
        capabilities.temperatureMeasurement.temperature({ value = t, unit = "C" }))
    end)
    if not ok then log.warn("[stbridge] " .. dev.label .. " outdoor: " .. tostring(err)) end
  end
  if s.fan then try_emit(dev, function() return acFan.fanMode(s.fan) end) end
  emit_list(dev, "acFans", map_list(s.supportedFans), acFan.supportedAcFanModes)
  local sw = SWING_TO_ST[s.swing or ""]
  if sw then try_emit(dev, function() return oscillation.fanOscillationMode(sw) end) end
  emit_list(dev, "acSwings", map_list(s.supportedSwings, SWING_TO_ST), oscillation.supportedFanOscillationModes)
end

local function comp_emit(dev, id, make)
  local c = dev.profile and dev.profile.components and dev.profile.components[id]
  if not c then return end
  local ok, err = pcall(function() dev:emit_component_event(c, make()) end)
  if not ok then log.warn("[stbridge] " .. dev.label .. " " .. id .. ": " .. tostring(err)) end
end

local function emit_car(dev, s)
  if tonumber(s.battery) then
    try_emit(dev, function() return capabilities.battery.battery(math.max(0, math.min(100, math.floor(tonumber(s.battery) + 0.5)))) end)
  end
  local charging = tostring(s.charging or ""):upper()
  if charging ~= "" then
    try_emit(dev, function() return charging == "CHARGING" and capabilities.switch.switch.on() or capabilities.switch.switch.off() end)
  end
  if s.chargePowerKw ~= nil then
    try_emit(dev, function() return capabilities.powerMeter.power({ value = (tonumber(s.chargePowerKw) or 0) * 1000, unit = "W" }) end)
  end
  -- main: battery, charging on/off and power; then cable, climate, and the car itself (home, doors)
  local plug = tostring(s.plug or ""):upper()
  if plug ~= "" then
    comp_emit(dev, "plug", function() return plug == "CONNECTED" and contact.contact.closed() or contact.contact.open() end)
  end
  local climate = tostring(s.climate or ""):upper()
  if climate ~= "" then
    comp_emit(dev, "climate", function() return (climate == "OFF" or climate == "INVALID") and capabilities.switch.switch.off() or capabilities.switch.switch.on() end)
  end
  if s.atHome ~= nil then
    comp_emit(dev, "status", function() return s.atHome and capabilities.presenceSensor.presence.present() or capabilities.presenceSensor.presence.not_present() end)
  end
  local doors = tostring(s.doors or ""):upper()
  if doors ~= "" then
    comp_emit(dev, "status", function() return doors == "CLOSED" and contact.contact.closed() or contact.contact.open() end)
  end
end

local EMIT = { dryer = emit_dryer, window = emit_window, sensor = emit_sensor, aircon = emit_aircon, car = emit_car }

-- polling -------------------------------------------------------------------------

local function create_child(driver, parent, d)
  local profile = PROFILE[d.type]
  if not profile then return end
  log.info(string.format("[stbridge] creating child '%s' (%s, %s)", d.name or d.id, d.type, d.id))
  driver:try_create_device({
    type = "EDGE_CHILD",
    parent_device_id = parent.id,
    parent_assigned_child_key = d.id,
    label = d.name or d.id,
    profile = profile,
    manufacturer = MANUFACTURER[d.type] or "VELUX",
    model = "Stoeckie Bridge " .. d.type,
    vendor_provided_label = d.name or d.id,
  })
end

local rescan -- forward declaration

local function poll(driver, parent)
  local ip, port = bridge_addr(parent)
  if not ip then
    local st = state[parent.id]
    if st and not st.scanning and os.time() - (st.last_scan or 0) > 120 then
      st.scanning, st.last_scan = true, os.time()
      cosock.spawn(function() rescan(driver); st.scanning = false end, "stbridge-rescan")
    end
    return
  end
  local devices, err = api.devices(ip, port)
  if not devices then
    log.warn(string.format("[stbridge] %s: poll failed: %s", parent.label, tostring(err)))
    parent:offline()
    for _, child in pairs(children_by_key(driver, parent)) do child:offline() end
    return
  end
  parent:online()
  local kids = children_by_key(driver, parent)
  for _, d in ipairs(devices) do
    local child = kids[d.id]
    if not child then
      create_child(driver, parent, d)
    else
      if d.online == false then child:offline() else child:online() end
      local fn = EMIT[d.type]
      if fn and d.state then
        local ok, e = pcall(fn, child, d.state)
        if not ok then log.error("[stbridge] emit " .. d.id .. ": " .. tostring(e)) end
      end
    end
  end
end

local function schedule_poll(driver, parent)
  local st = state[parent.id] or {}
  state[parent.id] = st
  if st.poll_timer then parent.thread:cancel_timer(st.poll_timer) end
  local interval = tonumber((parent.preferences or {}).pollInterval) or 10
  local function tick()
    poll(driver, parent)
    local wait = interval
    if st.fast_until and os.time() < st.fast_until then wait = FAST_POLL_SECS end
    st.poll_timer = parent.thread:call_with_delay(wait, tick)
  end
  st.poll_timer = parent.thread:call_with_delay(1, tick)
end

local function speed_up(driver, parent)
  local st = state[parent.id]
  if not st then return end
  st.fast_until = os.time() + FAST_POLL_FOR
  schedule_poll(driver, parent)
end

-- commands ------------------------------------------------------------------------

local function send(driver, child, cmd, value)
  local parent = child:get_parent_device()
  if not parent then
    log.error("[stbridge] " .. child.label .. " has no parent")
    return
  end
  local ip, port = bridge_addr(parent)
  local res, err = api.command(ip, port, child.parent_assigned_child_key, cmd, value)
  if not res then
    log.error(string.format("[stbridge] %s %s failed: %s", child.label, cmd, tostring(err)))
    return
  end
  speed_up(driver, parent)
end

local function handle_refresh(driver, device)
  if is_parent(device) then
    poll(driver, device)
  else
    send(driver, device, "refresh")
  end
end

local function handle_open(driver, device) send(driver, device, "open") end
local function handle_close(driver, device) send(driver, device, "close") end
local function handle_pause(driver, device) send(driver, device, "stop") end
local function handle_level(driver, device, cmd)
  send(driver, device, "level", tonumber(cmd.args.shadeLevel) or 0)
end

local function handle_machine_state(driver, device, cmd)
  local want = cmd.args.state or cmd.args.machineState
  local current = device:get_latest_state("main", dryerOp.ID, dryerOp.machineState.NAME)
  if want == "run" then
    send(driver, device, current == "pause" and "resume" or "run")
  elseif want == "pause" then
    send(driver, device, "pause")
  elseif want == "stop" then
    send(driver, device, "stop")
  end
end

local function handle_switch_on(driver, device, cmd)
  send(driver, device, (cmd and cmd.component == "climate") and "climate_on" or "on")
end
local function handle_switch_off(driver, device, cmd)
  send(driver, device, (cmd and cmd.component == "climate") and "climate_off" or "off")
end
local function handle_ac_mode(driver, device, cmd)
  local m = AC_MODE_FROM_ST[cmd.args.mode or ""]
  if m then send(driver, device, "mode", m) else log.warn("[stbridge] unsupported AC mode " .. tostring(cmd.args.mode)) end
end
local function handle_setpoint(driver, device, cmd)
  send(driver, device, "setpoint", tonumber(cmd.args.setpoint))
end
local function handle_fan_mode(driver, device, cmd) send(driver, device, "fan", cmd.args.fanMode) end
local function handle_oscillation(driver, device, cmd)
  local m = SWING_FROM_ST[cmd.args.fanOscillationMode or ""]
  if m then send(driver, device, "swing", m) end
end

-- lifecycle -----------------------------------------------------------------------

local function device_init(driver, device)
  if is_parent(device) then
    state[device.id] = state[device.id] or {}
    schedule_poll(driver, device)
    return
  end
  local key = device.parent_assigned_child_key or ""
  if device:supports_capability(windowShade) then
    device:emit_event(windowShade.supportedWindowShadeCommands({ "open", "close", "pause" }, { visibility = { displayed = false } }))
  end
  if device:supports_capability(dryerOp) then
    device:emit_event(dryerOp.supportedMachineStates({ "run", "pause", "stop" }, { visibility = { displayed = false } }))
  end
  log.debug("[stbridge] init child " .. key)
end

local function device_added(driver, device)
  if is_parent(device) then
    local d = discovered[device.device_network_id]
    if d then
      device:set_field("ip", d.ip, { persist = true })
      device:set_field("port", d.port, { persist = true })
    end
  end
  device_init(driver, device)
end

local function info_changed(driver, device, event, args)
  if is_parent(device) then
    schedule_poll(driver, device)
    return
  end
  -- car settings: pass them to the bridge
  local old = args and args.old_st_store and args.old_st_store.preferences or {}
  local p = device.preferences or {}
  if p.pollMinutes ~= nil and (p.pollMinutes ~= old.pollMinutes or p.climateTemp ~= old.climateTemp) then
    send(driver, device, "settings", { poll_minutes = p.pollMinutes, climate_temp = p.climateTemp })
  end
end

local function device_removed(driver, device)
  local st = state[device.id]
  if st and st.poll_timer then device.thread:cancel_timer(st.poll_timer) end
  state[device.id] = nil
end

-- discovery: probe every host in the hub's /24 on the bridge port --------------------------------

local function existing_parent(driver, dni)
  for _, dev in ipairs(driver:get_devices()) do
    if dev.device_network_id == dni then return dev end
  end
end

local function handle_found(driver, ip, port, info)
  local dni = "stbridge-" .. info.id
  discovered[dni] = { ip = ip, port = port, info = info }
  local dev = existing_parent(driver, dni)
  if dev then
    if dev:get_field("ip") ~= ip then
      dev:set_field("ip", ip, { persist = true })
      log.info(string.format("[stbridge] %s moved to %s", dev.label, ip))
    end
    poll(driver, dev) -- also creates new child devices
    return
  end
  log.info(string.format("[stbridge] found bridge %s at %s:%d", info.id, ip, port))
  driver:try_create_device({
    type = "LAN",
    device_network_id = dni,
    label = "Stoeckie Bridge (" .. (info.name or ip) .. ")",
    profile = "stoeckie-bridge",
    manufacturer = "Stoeckie",
    model = "Bridge " .. tostring(info.version),
    vendor_provided_label = "Stoeckie Bridge",
  })
end

local function hub_ip(driver)
  local ip = driver.environment_info and driver.environment_info.hub_ipv4
  if ip then return ip end
  local ok, addr = pcall(function()
    local s = cosock.socket.udp()
    s:setpeername("8.8.8.8", 53)
    local a = s:getsockname()
    s:close()
    return a
  end)
  if ok and type(addr) == "string" and addr:match("^%d+%.%d+%.%d+%.%d+$") then return addr end
end

local function discovery(driver, opts, should_continue)
  -- manual IPs on existing parents first
  for _, dev in ipairs(driver:get_devices()) do
    if is_parent(dev) then
      local ip, port = bridge_addr(dev)
      local info = ip and api.probe(ip, port, 3)
      if info then handle_found(driver, ip, port, info) end
    end
  end
  local hip = hub_ip(driver)
  if not hip then
    log.warn("[stbridge] hub IP unknown, cannot scan")
    return
  end
  local prefix = hip:match("^(%d+%.%d+%.%d+%.)")
  log.info("[stbridge] scanning " .. prefix .. "0/24 port " .. DEFAULT_PORT)
  local next_host, pending, found = 1, 0, 0
  local function worker()
    while next_host <= 254 and should_continue() do
      local ip = prefix .. next_host
      next_host = next_host + 1
      local info = api.probe(ip, DEFAULT_PORT, 3)
      if info then
        found = found + 1
        local ok, err = pcall(handle_found, driver, ip, DEFAULT_PORT, info)
        if not ok then log.error("[stbridge] handle_found: " .. tostring(err)) end
      end
    end
    pending = pending - 1
  end
  for i = 1, SCAN_CONCURRENCY do
    pending = pending + 1
    cosock.spawn(worker, "stbridge-scan-" .. i)
  end
  while pending > 0 do cosock.socket.sleep(0.5) end
  log.info(string.format("[stbridge] scan done, %d bridge(s) found", found))
end

local driver = Driver("stoeckie-bridge-local", {
  discovery = discovery,
  lifecycle_handlers = {
    init = device_init,
    added = device_added,
    infoChanged = info_changed,
    removed = device_removed,
  },
  capability_handlers = {
    [capabilities.refresh.ID] = {
      [capabilities.refresh.commands.refresh.NAME] = handle_refresh,
    },
    [windowShade.ID] = {
      [windowShade.commands.open.NAME] = handle_open,
      [windowShade.commands.close.NAME] = handle_close,
      [windowShade.commands.pause.NAME] = handle_pause,
    },
    [windowShadeLevel.ID] = {
      [windowShadeLevel.commands.setShadeLevel.NAME] = handle_level,
    },
    [dryerOp.ID] = {
      [dryerOp.commands.setMachineState.NAME] = handle_machine_state,
    },
    [capabilities.switch.ID] = {
      [capabilities.switch.commands.on.NAME] = handle_switch_on,
      [capabilities.switch.commands.off.NAME] = handle_switch_off,
    },
    [acMode.ID] = {
      [acMode.commands.setAirConditionerMode.NAME] = handle_ac_mode,
    },
    [coolSetpoint.ID] = {
      [coolSetpoint.commands.setCoolingSetpoint.NAME] = handle_setpoint,
    },
    [acFan.ID] = {
      [acFan.commands.setFanMode.NAME] = handle_fan_mode,
    },
    [oscillation.ID] = {
      [oscillation.commands.setFanOscillationMode.NAME] = handle_oscillation,
    },
  },
})

rescan = function(drv) discovery(drv, {}, function() return true end) end

log.info("[stbridge] Stoeckie Bridge (local) driver starting, build " .. BUILD)
driver:run()
