-- SmartThings Edge driver: Stoeckie Bridge (local).
-- One parent device per bridge (a pc in the LAN running stoeckie_bridge.py). The parent polls
-- GET /stbridge/devices and creates/updates one child device per bridge device:
--   dryer  (AEG via Electrolux API)  -> dryerOperatingState, remoteControlStatus, contactSensor (door)
--   dishwasher / oven (Home Connect, local) -> switch, dishwasher/ovenOperatingState, contactSensor (door),
--                                        remoteControlStatus, oven temperature + setpoint, program list +
--                                        start (custom applianceProgram), oven temperature + cooking time
--   washer (Samsung, local)          -> switch, washerOperatingState, remoteControlStatus, course list + start
--   tv (Samsung, local)              -> switch, audioVolume, audioMute, mediaInputSource, tvChannel
--   window (VELUX ACTIVE via HomeKit) -> windowShade, windowShadeLevel
--   sensor (VELUX climate sensor)     -> temperature, humidity, CO2
--   aircon (Fujitsu via FGLair)       -> switch, airConditionerMode (plus minHeat on units that have it, like the
--                                        FGLair app), setpoint, fan, swing, indoor/outdoor temp
--   car    (Škoda public API)         -> battery, charging switch + power, at home, plug, climate, doors
--   velux_lock (VELUX departure mode) -> lock (locked = away: all windows and blinds blocked)
--   rain   (VELUX gateway rain sensor) -> rainSensor (detected = raining)
--   rainradar (Buienradar / Open-Meteo) -> rainSensor; detail: precipitationSensor + rainForecast (mm/h, minutes until
--                                       rain, minutes of rain, summary); components dewNow / dewMax: outdoor dew
--                                       point now and the highest in the next 48 h (temperatureMeasurement, routines)
local Driver = require "st.driver"
local capabilities = require "st.capabilities"

-- Connection status as a capability, so routines can notify when this device loses its connection
-- (SmartThings itself has no "device offline" trigger for routines).
-- Short drops (a reconnect, one missed poll) are ignored: Offline is only emitted after the "Offline after" setting (default 5 min) down.
-- Every drop is remembered (time, duration, reason) and shown as "Last drop" once the connection is back.
local conn_cap = capabilities["silvervalley08660.connectionStatus"]
local drop_cap = capabilities["silvervalley08660.connectionLog"]
local CONN_GRACE = 300
local function conn_grace(dev)
  -- setting "Offline after (minutes)": a router or network update at night takes a few minutes
  local m = tonumber(dev.preferences and dev.preferences.offlineAfter)
  if m and m >= 1 then return math.floor(m * 60) end
  return CONN_GRACE
end
local function emit_conn(dev, v)
  if dev:get_field("conn_status") == v then return end
  local ok = pcall(function() dev:emit_event(conn_cap.status(v)) end)
  if ok then dev:set_field("conn_status", v) end
end
local function fmt_secs(s)
  if s < 120 then return string.format("%d s", s) end
  if s < 7200 then return string.format("%d min", s // 60) end
  return string.format("%.1f h", s / 3600)
end
local function note_drop(dev, reason)
  if dev:get_field("conn_drop") then return end
  dev:set_field("conn_drop", { at = os.time(), reason = tostring(reason or "unknown"):sub(1, 120) }, { persist = true })
end
local function report_drop(dev)
  local d = dev:get_field("conn_drop")
  if type(d) ~= "table" or not d.at then return end
  dev:set_field("conn_drop", nil, { persist = true })
  local txt = string.format("%s UTC, %s: %s", os.date("!%Y-%m-%d %H:%M:%S", d.at), fmt_secs(math.max(0, os.time() - d.at)), tostring(d.reason))
  require("log").info("[conn] " .. tostring(dev.label) .. " back after drop at " .. txt)
  pcall(function() dev:emit_event(drop_cap.lastDrop(txt:sub(1, 200))) end)
end
local function set_conn(dev, up, reason)
  if up then
    dev:set_field("conn_down_since", nil)
    if not dev:get_field("drop_seen") then
      -- the app wants a value for every attribute ("not all status information updated yet" otherwise)
      dev:set_field("drop_seen", true)
      local ok, v = pcall(dev.get_latest_state, dev, "main", drop_cap.ID, "lastDrop")
      if ok and v == nil and not dev:get_field("conn_drop") then
        pcall(function() dev:emit_event(drop_cap.lastDrop("No drops yet")) end)
      end
    end
    report_drop(dev)
    emit_conn(dev, "online")
    return
  end
  note_drop(dev, reason)
  if dev:get_field("conn_down_since") then return end
  local since = os.time()
  dev:set_field("conn_down_since", since)
  local ok = pcall(function()
    dev.thread:call_with_delay(conn_grace(dev), function()
      if dev:get_field("conn_down_since") == since then emit_conn(dev, "offline") end
    end)
  end)
  if not ok then emit_conn(dev, "offline") end
end
local log = require "log"
local cosock = require "cosock"
local api = require "bridge_api"
local sha = require "sha512"

local BUILD = "2026-11-08l"
local FAST_POLL_SECS = 2
local FAST_POLL_FOR = 40
local SCAN_CONCURRENCY = 16
local DEFAULT_PORT = 8787

local windowShade = capabilities.windowShade
local windowShadeLevel = capabilities.windowShadeLevel
local dryerOp = capabilities.dryerOperatingState
local dishOp = capabilities.dishwasherOperatingState
local washerOp = capabilities.washerOperatingState
local ovenOp = capabilities.ovenOperatingState
local ovenSetpoint = capabilities.ovenSetpoint
local remoteControl = capabilities.remoteControlStatus
local contact = capabilities.contactSensor
local acMode = capabilities.airConditionerMode
local acFan = capabilities.airConditionerFanMode
local oscillation = capabilities.fanOscillationMode
local coolSetpoint = capabilities.thermostatCoolingSetpoint
-- custom capabilities with English and Dutch labels: mode list with Minimum Heat, and "Set temperature"
-- (the standard cooling setpoint is called "Cooling setpoint" in the app, also in heat mode)
local acModeX = capabilities["silvervalley08660.airconMode"]
local targetTemp = capabilities["silvervalley08660.targetTemperature"]
local targetTempF = capabilities["silvervalley08660.targetTemperatureF"] -- same, in °F (profiles ...-f)
local airflowDir = capabilities["silvervalley08660.airflowDirection"]  -- louver position 1..n (swing off)
local hAirflow = (function() local ok, c = pcall(function() return capabilities["silvervalley08660.horizontalAirflow"] end); return ok and c or nil end)() -- left/right louvers (profiles ...-h)
local windKmh = (function() local ok, c = pcall(function() return capabilities["silvervalley08660.windSpeedKmh"] end); return ok and c or nil end)() -- wind on the rain radar device (km/h)
-- Home Connect: program list with start button, and oven temperature + cooking time (labels EN/NL)
local hcProg = capabilities["silvervalley08660.applianceProgram"]
local cookSet = capabilities["silvervalley08660.cookSettings"]
local washerProg = capabilities["silvervalley08660.washerProgram"]   -- Samsung washer courses

local PROFILE = { dryer = "stoeckie-dryer", window = "stoeckie-window", sensor = "stoeckie-climate",
                  aircon = "stoeckie-aircon-v3", car = "stoeckie-car-v2",
                  velux_lock = "stoeckie-velux-lock", rain = "stoeckie-rain-v4",
                  rainradar = "stoeckie-rainradar-v3", dishwasher = "stoeckie-dishwasher-v2", oven = "stoeckie-oven-v2",
                  washer = "stoeckie-washer-v3", tv = "stoeckie-tv" }
local MANUFACTURER = { dryer = "AEG", aircon = "Fujitsu", car = "Skoda", dishwasher = "Home Connect", oven = "Home Connect",
                       washer = "Samsung", tv = "Samsung" }

-- bridge value <-> SmartThings value
local AC_MODE_TO_ST = { auto = "auto", cool = "cool", dry = "dry", fan = "fanOnly", heat = "heat", minheat = "minHeat" }
local AC_MODE_FROM_ST = { auto = "auto", cool = "cool", dry = "dry", fanOnly = "fan", fan = "fan", heat = "heat",
                          minHeat = "minheat" }
local SWING_TO_ST = { off = "fixed", vertical = "vertical", horizontal = "horizontal", both = "all" }
local SWING_FROM_ST = { fixed = "off", vertical = "vertical", horizontal = "horizontal", all = "both" }

-- Temperature unit of an air conditioner: the bridge works in °C. A US location shows °F: the standard
-- capabilities are converted by the app, the custom "Set temperature" slider is not, so a °F device gets a
-- profile with a °F slider (...-f). Setting "Temperature unit": auto (°F once a °F value arrives), C or F.
local function ac_unit_f(device)
  local p = device.preferences and device.preferences.temperatureUnit
  if p == "F" then return true end
  if p == "C" then return false end
  return device:get_field("unitF") == true
end
local function c_to_f(c) return math.floor(c * 9 / 5 + 32 + 0.5) end
local function f_to_c(f) return math.floor(((f - 32) * 5 / 9) * 2 + 0.5) / 2 end

local state = {}       -- parent device.id -> { poll_timer, fast_until }
local discovered = {}  -- dni -> { ip, port, info }

-- helpers ------------------------------------------------------------------

local function is_parent(device) return device.parent_assigned_child_key == nil end

local function bridge_addr(parent)
  local p = parent.preferences or {}
  local ip = p.ipAddress
  if ip == nil or ip == "" or ip == "auto" then ip = parent:get_field("ip") end
  local port = tonumber(p.port) or parent:get_field("port") or DEFAULT_PORT
  api.set_token(ip, port, parent:get_field("api_token"))
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

-- Every poll (10 s, 2 s for 40 s after a command) reads all devices. Emitting every attribute each time was
-- about 6 events a second for one bridge; the hub then stopped passing events of a device to the cloud (seen
-- 2026-11-08: the switch stayed "on" in the app while the driver emitted "off"). So an event only goes out when
-- its value changed. The hub itself drops an event whose value it already has, so a value the cloud missed would
-- stay wrong for ever: the first event after a driver start is sent with state_change, which the hub always
-- passes on. The bridge device setting "Resend all values every (minutes)" does the same periodically; it is off
-- by default, because a forced event also fires routines and notifications again (like a button press does).
local stjson = require "st.json"
local EMIT_REFRESH = 0                -- default off; setting "Resend all values every (minutes)" of the bridge device
local REFRESH_BY_PARENT = {}          -- parent device.id -> seconds (set at every poll from that setting)
local EMIT_CACHE = {}  -- device.id -> key -> { v = encoded value, t = time }; plain memory, not a device field
local function refresh_secs(dev)
  return REFRESH_BY_PARENT[dev.parent_device_id or dev.id] or EMIT_REFRESH
end
-- spread the resends of one device over up to 5 minutes instead of all at once
local function jitter(key, secs)
  local span = math.max(1, math.min(300, math.floor(secs / 2)))
  local n = 0
  for i = 1, #key do n = (n + key:byte(i)) % span end
  return n
end
-- a capability event on the hub is { capability = <capability>, attribute = <attribute>, value = { value = .., unit = .. } }
-- (seen in the hub log 2026-11-08); older notes name the state field "state", so both are accepted
local function ev_state(ev) return ev.value ~= nil and ev.value or ev.state end
local function ev_key(comp, ev)
  if type(ev) ~= "table" then return nil end
  local cap = ev.capability_id or ev.capability
  if type(cap) == "table" then cap = cap.ID or cap.id end
  local attr = ev.attribute_id or ev.attribute
  if type(attr) == "table" then attr = attr.NAME or attr.name end
  if type(cap) ~= "string" or type(attr) ~= "string" or ev_state(ev) == nil then return nil end
  return (comp and comp.id or "main") .. "/" .. cap .. "/" .. attr
end
local function push(dev, comp, ev)
  local key, val = ev_key(comp, ev), nil
  if key then
    local ok, s = pcall(stjson.encode, { ev_state(ev), ev.visibility })
    val = ok and s or nil
  end
  if not EMIT_CACHE[dev.id] then
    EMIT_CACHE[dev.id] = {}
    if key then log.info("[stbridge] " .. tostring(dev.label) .. ": event shape ok (" .. key .. ")")
    else
      local ks = {}
      if type(ev) == "table" then for k in pairs(ev) do ks[#ks + 1] = tostring(k) end end
      log.warn("[stbridge] " .. tostring(dev.label) .. ": unknown event shape, no filtering: " .. table.concat(ks, ","))
    end
  end
  if key and val then
    local cache = EMIT_CACHE[dev.id]
    local c, now = cache[key], os.time()
    local refresh = refresh_secs(dev)
    if c and c.v == val and (refresh <= 0 or now - c.t < refresh + jitter(key, refresh)) then return end
    if not c or c.v == val then ev.state_change = true end
    if comp then dev:emit_component_event(comp, ev) else dev:emit_event(ev) end
    cache[key] = { v = val, t = now }  -- only after the emit went through: a refused event is tried again
    return
  end
  if comp then dev:emit_component_event(comp, ev) else dev:emit_event(ev) end
end

-- emit one event; an unknown enum value only skips that event, not the rest
local function try_emit(dev, make)
  local ok, err = pcall(function() push(dev, nil, make()) end)
  if not ok then log.warn("[stbridge] " .. dev.label .. ": " .. tostring(err)) end
  return ok
end

local function iso_secs(v)
  local y, mo, d, h, mi, se = tostring(v):match("^(%d+)-(%d+)-(%d+)T(%d+):(%d+):(%d+)")
  if not y then return nil end
  return os.time({ year = tonumber(y), month = tonumber(mo), day = tonumber(d),
    hour = tonumber(h), min = tonumber(mi), sec = tonumber(se) })
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
  -- completion time only while a program runs, and only when it moves 2+ minutes:
  -- before, every poll sent "now" (idle) or a time a few seconds off, a history entry each minute
  if s.completionTime then
    local new = iso_secs(s.completionTime)
    local old = dev:get_field("ct_secs")
    if not (new and old and math.abs(new - old) < 120) then
      try_emit(dev, function() return dryerOp.completionTime(s.completionTime) end)
      dev:set_field("ct_secs", new)
    end
  end
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
  push(dev, nil, windowShadeLevel.shadeLevel(pos))
  local ps = s.positionState
  if ps == "opening" then push(dev, nil, windowShade.windowShade.opening())
  elseif ps == "closing" then push(dev, nil, windowShade.windowShade.closing())
  elseif pos >= 99 then push(dev, nil, windowShade.windowShade.open())
  elseif pos <= 1 then push(dev, nil, windowShade.windowShade.closed())
  else push(dev, nil, windowShade.windowShade.partially_open()) end
end

local function emit_sensor(dev, s)
  if s.temperature then
    push(dev, nil, capabilities.temperatureMeasurement.temperature({ value = tonumber(s.temperature), unit = "C" }))
  end
  if s.humidity then
    push(dev, nil, capabilities.relativeHumidityMeasurement.humidity(math.floor(tonumber(s.humidity) + 0.5)))
  end
  if s.co2 then
    push(dev, nil, capabilities.carbonDioxideMeasurement.carbonDioxide({ value = tonumber(s.co2), unit = "ppm" }))
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
  -- remembered only when the event went out, so a refused event (e.g. a capability the hub still has in an
  -- older version) is sent again at the next poll
  if try_emit(dev, function() return make(list, { visibility = { displayed = false } }) end) then
    dev:set_field(key, enc)
  end
end

local sync_profile -- defined with the polling code below

local send_diag -- defined with the commands below

-- changes kept while an air conditioner is off. Stored as a JSON string: on the hub a persisted Lua table
-- reads back as a proxy whose fields work but next()/pairs() see nothing (seen 2026-11-08); a string is safe.
local function get_pending(dev)
  local v = dev:get_field("ac_pending")
  if type(v) ~= "string" or v == "" then return nil end
  local ok, t = pcall(stjson.decode, v)
  if ok and type(t) == "table" and (t.mode or t.setpoint or t.fan or t.swing or t.vdir or t.hdir) then return t end
  return nil
end
local function set_pending(dev, t)
  dev:set_field("ac_pending", t and stjson.encode(t) or nil, { persist = true })
end

local function emit_aircon(dev, s)
  try_emit(dev, function() return s.power == "off" and capabilities.switch.switch.off() or capabilities.switch.switch.on() end)
  -- changes made while the unit is off are kept until it is switched on (see ac_change): show them, not the
  -- values of the unit, else the app jumps back. Once the unit is on (also from the remote or FGLair) they go.
  local pend = get_pending(dev)
  if s.power ~= "off" then
    if pend or dev:get_field("ac_pending") then set_pending(dev, nil) end
    pend = nil
  elseif pend then
    local c = {}
    for k, v in pairs(s) do c[k] = v end
    if pend.setpoint then c.setpoint = pend.setpoint.shown end
    if pend.fan then c.fan = pend.fan.shown end
    if pend.swing then c.swing = SWING_FROM_ST[pend.swing.shown] or c.swing end
    if pend.vdir then c.vDir = tonumber(pend.vdir.shown) end
    if pend.hdir then c.hDir = tonumber(pend.hdir.shown) end
    s = c
  else
    pend = nil
  end
  local modes = map_list(s.supportedModes, AC_MODE_TO_ST)
  local m = AC_MODE_TO_ST[s.mode or ""]
  if m then
    dev:set_field("lastAcMode", m, { persist = true })
  else
    -- switched off: the mode list still needs a value, else the app shows "This device hasn't updated all of
    -- its status information yet" (and on some phones the old mode list): keep the last mode
    m = dev:get_field("lastAcMode") or (modes[1] and (modes[#modes] == "minHeat" and "heat" or modes[1])) or "auto"
  end
  if pend and pend.mode then m = pend.mode.shown end
  try_emit(dev, function() return acMode.airConditionerMode(m) end)
  try_emit(dev, function() return acModeX.mode(m) end)
  emit_list(dev, "acModes", modes, acMode.supportedAcModes)
  emit_list(dev, "acModesA", modes, acMode.availableAcModes) -- the app also reads the "available" lists
  emit_list(dev, "acModesX", modes, acModeX.supportedModes)
  if s.setpoint then
    try_emit(dev, function() return coolSetpoint.coolingSetpoint({ value = tonumber(s.setpoint), unit = "C" }) end)
    local f = ac_unit_f(dev)
    if f then
      try_emit(dev, function() return targetTempF.temperature({ value = c_to_f(tonumber(s.setpoint)), unit = "F" }) end)
    end
    -- °C slider: also while a °F profile switch has not happened yet, so it never shows an old value
    if not f or not dev:supports_capability(targetTempF) then
      try_emit(dev, function() return targetTemp.temperature({ value = tonumber(s.setpoint), unit = "C" }) end)
    end
  end
  -- what the app shows now: a command with the same value (the app sends one when you open a list and go
  -- back without choosing) must not switch the unit on
  dev:set_field("ac_shown", { power = s.power == "off" and "off" or "on", mode = m, fan = s.fan, swing = SWING_TO_ST[s.swing or ""],
    vdir = tonumber(s.vDir) and tostring(math.floor(s.vDir)) or nil,
    hdir = tonumber(s.hDir) and tostring(math.floor(s.hDir)) or nil, setpoint = tonumber(s.setpoint) })
  local n = tonumber(s.vDirCount) or 0
  if n > 0 then
    local list = {}
    for i = 1, math.min(n, 8) do list[#list + 1] = tostring(i) end
    emit_list(dev, "vDirs", list, airflowDir.supportedPositions)
    if tonumber(s.vDir) then try_emit(dev, function() return airflowDir.position(tostring(math.floor(s.vDir))) end) end
  end
  -- horizontal louvers (left/right): only units that report positions get the profile with this control
  local hn = tonumber(s.hDirCount) or 0
  if (hn > 0) ~= (dev:get_field("hasHDir") == true) then
    dev:set_field("hasHDir", hn > 0, { persist = true })
    sync_profile(dev)
  end
  if hn > 0 and hAirflow and dev:supports_capability(hAirflow) then
    local list = {}
    for i = 1, math.min(hn, 8) do list[#list + 1] = tostring(i) end
    emit_list(dev, "hDirs", list, hAirflow.supportedPositions)
    if tonumber(s.hDir) then try_emit(dev, function() return hAirflow.position(tostring(math.floor(s.hDir))) end) end
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
  -- an outdoor temperature that stays empty: switch to the profile without it (and back when it shows up)
  local no_out = s.outdoorTemperature == nil
  local n = no_out and ((dev:get_field("noOutCount") or 0) + 1) or 0
  dev:set_field("noOutCount", n)
  if (n >= 3) ~= (dev:get_field("noOutdoor") == true) and (n >= 3 or not no_out) then
    dev:set_field("noOutdoor", n >= 3, { persist = true })
    sync_profile(dev)
  end
  if s.outdoorTemperature and dev.profile.components.outdoor then
    local t = tonumber(s.outdoorTemperature)
    local ok, err = pcall(function()
      push(dev, dev.profile.components.outdoor,
        capabilities.temperatureMeasurement.temperature({ value = t, unit = "C" }))
    end)
    if not ok then log.warn("[stbridge] " .. dev.label .. " outdoor: " .. tostring(err)) end
  end
  if s.fan then try_emit(dev, function() return acFan.fanMode(s.fan) end) end
  emit_list(dev, "acFans", map_list(s.supportedFans), acFan.supportedAcFanModes)
  emit_list(dev, "acFansA", map_list(s.supportedFans), acFan.availableAcFanModes)
  local sw = SWING_TO_ST[s.swing or ""]
  if sw then try_emit(dev, function() return oscillation.fanOscillationMode(sw) end) end
  emit_list(dev, "acSwings", map_list(s.supportedSwings, SWING_TO_ST), oscillation.supportedFanOscillationModes)
  emit_list(dev, "acSwingsA", map_list(s.supportedSwings, SWING_TO_ST), oscillation.availableFanOscillationModes)
  if send_diag then send_diag(dev) end
end

local function comp_emit(dev, id, make)
  local c = dev.profile and dev.profile.components and dev.profile.components[id]
  if not c then return end
  local ok, err = pcall(function() push(dev, c, make()) end)
  if not ok then log.warn("[stbridge] " .. dev.label .. " " .. id .. ": " .. tostring(err)) end
end

-- battery history: SmartThings draws no graph for battery %, so optionally repeat it as a reading that has one
local CAR_GRAPH = { off = "", humidity = "-hum", power = "-pow" }

local function graph_mode(dev)
  local m = (dev.preferences or {}).batteryGraph
  return CAR_GRAPH[m or ""] and m or "off"
end

local function emit_car(dev, s)
  if tonumber(s.battery) then
    local soc = math.max(0, math.min(100, math.floor(tonumber(s.battery) + 0.5)))
    try_emit(dev, function() return capabilities.battery.battery(soc) end)
    local mode = graph_mode(dev)
    if mode == "humidity" then
      try_emit(dev, function() return capabilities.relativeHumidityMeasurement.humidity(soc) end)
    elseif mode == "power" then
      comp_emit(dev, "graph", function() return capabilities.powerMeter.power({ value = soc, unit = "W" }) end)
    end
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

local function emit_velux_lock(dev, s)
  if s.locked ~= nil then
    try_emit(dev, function() return s.locked and capabilities.lock.lock.locked() or capabilities.lock.lock.unlocked() end)
  end
end

local function emit_rain(dev, s)
  if s.raining ~= nil then
    try_emit(dev, function() return s.raining and capabilities.rainSensor.rain.detected() or capabilities.rainSensor.rain.undetected() end)
  end
end

-- Buienradar: custom capability (namespace of the driver author) with rain rate and timing
local rainForecast = capabilities["silvervalley08660.rainForecast"]

local function rain_simple(dev) return (dev.preferences or {}).rainMode == "simple" end

-- dew point now and the highest in the next hours, each in its own component (usable as routine conditions)
local function emit_dew(dev, s)
  for comp, key in pairs({ dewNow = "dewPointNow", dewMax = "dewPointMax" }) do
    local c = dev.profile.components[comp]
    local v = tonumber(s[key])
    if c and v then
      local ok, err = pcall(function()
        push(dev, c, capabilities.temperatureMeasurement.temperature({ value = v, unit = "C" }))
      end)
      if not ok then log.warn("[stbridge] " .. dev.label .. ": " .. tostring(err)) end
    end
  end
end

-- wind now (km/h, Open-Meteo), in its own component: usable in routines (e.g. no airing above 80 km/h)
local function emit_wind(dev, s)
  local c = dev.profile.components.wind
  if not (c and windKmh) then return end
  for _, a in ipairs({ { "speed", "windSpeed", "km/h" }, { "gust", "windGust", "km/h" }, { "direction", "windDirection", "°" } }) do
    local v = tonumber(s[a[2]])
    if v then
      local ok, err = pcall(function() push(dev, c, windKmh[a[1]]({ value = v, unit = a[3] })) end)
      if not ok then log.warn("[stbridge] " .. dev.label .. ": " .. tostring(err)) end
    end
  end
end

local function emit_rainradar(dev, s)
  emit_dew(dev, s)
  emit_wind(dev, s)
  if s.raining ~= nil then
    try_emit(dev, function() return s.raining and capabilities.rainSensor.rain.detected() or capabilities.rainSensor.rain.undetected() end)
  end
  if rain_simple(dev) then return end
  if s.intensity then
    try_emit(dev, function() return capabilities.precipitationSensor.precipitationIntensity(s.intensity) end)
  end
  if rainForecast then
    if s.rainRate then try_emit(dev, function() return rainForecast.rainRate({ value = tonumber(s.rainRate), unit = "mm/h" }) end) end
    if s.minutesUntilRain then
      try_emit(dev, function() return rainForecast.minutesUntilRain({ value = math.floor(tonumber(s.minutesUntilRain)), unit = "min" }) end)
    end
    if s.minutesOfRain then
      try_emit(dev, function() return rainForecast.minutesOfRain({ value = math.floor(tonumber(s.minutesOfRain)), unit = "min" }) end)
    end
    if s.summary then try_emit(dev, function() return rainForecast.summary(tostring(s.summary)) end) end
  end
end

-- Home Connect (Bosch / Siemens) dishwasher and oven, local via the bridge
local function hc_common(dev, s)
  try_emit(dev, function() return s.power == "on" and capabilities.switch.switch.on() or capabilities.switch.switch.off() end)
  try_emit(dev, function() return remoteControl.remoteControlEnabled(s.remoteStartAllowed and "true" or "false") end)
  if s.door then
    try_emit(dev, function() return s.door == "open" and contact.contact.open() or contact.contact.closed() end)
  end
end

local function hc_program(dev, s, cap)
  cap = cap or hcProg
  if type(s.programs) == "table" and #s.programs > 0 then
    local list = {}
    for _, k in ipairs(s.programs) do list[#list + 1] = k end
    emit_list(dev, "hcProgs", list, cap.supportedPrograms)
  end
  if s.program then try_emit(dev, function() return cap.program(tostring(s.program)) end) end
end

local function hc_completion(dev, s, cap)
  if not s.completionTime then return end
  local new = iso_secs(s.completionTime)
  local old = dev:get_field("ct_secs")
  if not (new and old and math.abs(new - old) < 120) then
    try_emit(dev, function() return cap.completionTime(s.completionTime) end)
    dev:set_field("ct_secs", new)
  end
end

local function emit_dishwasher(dev, s)
  hc_common(dev, s)
  local op = s.operation or "inactive"
  local machine = (op == "run" or op == "delayedStart") and "run" or (op == "pause" and "pause" or "stop")
  local job = op == "finished" and "finish" or ((machine == "run" and op ~= "delayedStart") and "wash" or "unknown")
  try_emit(dev, function() return dishOp.machineState[machine]() end)
  try_emit(dev, function() return dishOp.dishwasherJobState[job]() end)
  if s.progress then try_emit(dev, function() return dishOp.progress(math.floor(s.progress)) end) end
  hc_completion(dev, s, dishOp)
  hc_program(dev, s)
end

-- Samsung washer, local via the bridge (OCF over CoAP-DTLS)
local WASHER_JOBS = { none = true, wash = true, rinse = true, spin = true, finish = true, preWash = true,
  weightSensing = true, delayWash = true, airWash = true, drying = true, cooling = true, wrinklePrevent = true,
  aIWash = true, aIRinse = true, aISpin = true, freezeProtection = true }
local function emit_washer(dev, s)
  try_emit(dev, function() return s.power == "on" and capabilities.switch.switch.on() or capabilities.switch.switch.off() end)
  try_emit(dev, function() return remoteControl.remoteControlEnabled(s.remoteStartAllowed and "true" or "false") end)
  local op = s.operation or "inactive"
  local machine = op == "run" and "run" or (op == "pause" and "pause" or "stop")
  local job = WASHER_JOBS[s.job or ""] and s.job or "none"
  try_emit(dev, function() return washerOp.machineState[machine]() end)
  try_emit(dev, function() return washerOp.washerJobState[job]() end)
  if s.progress and washerOp.progress then try_emit(dev, function() return washerOp.progress(math.floor(s.progress)) end) end
  hc_completion(dev, s, washerOp)
  hc_program(dev, s, washerProg)
end

-- Samsung TV, local via the bridge: power, volume, mute, input
local function emit_tv(dev, s)
  try_emit(dev, function() return s.power == "on" and capabilities.switch.switch.on() or capabilities.switch.switch.off() end)
  if tonumber(s.volume) then
    try_emit(dev, function() return capabilities.audioVolume.volume({ value = math.floor(s.volume), unit = "%" }) end)
  end
  if s.mute ~= nil then
    try_emit(dev, function() return s.mute and capabilities.audioMute.mute.muted() or capabilities.audioMute.mute.unmuted() end)
  end
  if type(s.inputs) == "table" then emit_list(dev, "tvInputs", s.inputs, capabilities.mediaInputSource.supportedInputSources) end
  if s.input then try_emit(dev, function() return capabilities.mediaInputSource.inputSource(s.input) end) end
end

local function emit_oven(dev, s)
  hc_common(dev, s)
  local op = s.operation or "inactive"
  local machine = (op == "run" or op == "delayedStart") and "running" or (op == "pause" and "paused" or "ready")
  local job = op == "finished" and "finished" or (op == "delayedStart" and "scheduledStart" or
    (machine == "running" and "cooking" or "ready"))
  try_emit(dev, function() return ovenOp.machineState[machine]() end)
  try_emit(dev, function() return ovenOp.ovenJobState[job]() end)
  if s.progress then try_emit(dev, function() return ovenOp.progress(math.floor(s.progress)) end) end
  hc_completion(dev, s, ovenOp)
  if s.temperature then
    try_emit(dev, function() return capabilities.temperatureMeasurement.temperature({ value = s.temperature, unit = "C" }) end)
  end
  if s.setpoint then
    try_emit(dev, function() return ovenSetpoint.ovenSetpoint(math.floor(s.setpoint)) end)
    try_emit(dev, function() return cookSet.temperature({ value = math.floor(s.setpoint), unit = "C" }) end)
  end
  if s.duration then try_emit(dev, function() return cookSet.duration({ value = math.floor(s.duration), unit = "min" }) end) end
  hc_program(dev, s)
end

local EMIT = { dryer = emit_dryer, window = emit_window, sensor = emit_sensor, aircon = emit_aircon, car = emit_car,
               velux_lock = emit_velux_lock, rain = emit_rain,
               rainradar = emit_rainradar, dishwasher = emit_dishwasher, oven = emit_oven, washer = emit_washer,
  tv = emit_tv }

-- polling -------------------------------------------------------------------------

-- profile for an existing child, from its key (the bridge type is not stored on the device)
local function child_profile(key, device)
  if key:match("^velux%-rain%-") or key == "demo-rain" then return PROFILE.rain end
  if key:match("^velux%-") or key == "demo-velux" then return PROFILE.velux_lock end
  if key:match("^skoda%-") or key == "demo-car" then return PROFILE.car .. CAR_GRAPH[graph_mode(device)] end
  -- Minimum Heat is a mode (airConditionerMode minHeat); 2026-11-06b had a separate switch in profile -mh,
  -- -v2 adds the custom mode list and "Set temperature" (custom presentation hides the standard ones),
  -- -v3 the airflow direction (louver position)
  -- -in: units without an outdoor temperature (an empty outdoor value made the app warn about missing status)
  if key:match("^fglair%-") or key == "demo-aircon" then
    -- -h: units with horizontal louver positions (left/right)
    return PROFILE.aircon .. (device:get_field("noOutdoor") and "-in" or "") .. (ac_unit_f(device) and "-f" or "")
      .. ((device:get_field("hasHDir") and hAirflow) and "-h" or "")
  end
  if key == "buienradar" or key == "demo-rainradar" then
    return PROFILE.rainradar .. (rain_simple(device) and "-simple" or "")
  end
  -- -v3: course list + Start (own capability washerProgram; -v2 used applianceProgram)
  if key:match("^samsung%-") or key == "demo-washer" then return PROFILE.washer end
  -- -v2: program list, start button, oven temperature and cooking time
  if key:match("^homeconnect%-") or key == "demo-dishwasher" or key == "demo-oven" then
    if device:supports_capability(dishOp) then return PROFILE.dishwasher end
    if device:supports_capability(ovenOp) then return PROFILE.oven end
  end
end

-- a capability that only the newest profile of a kind has: when the device lacks it, the switch did not happen
local function profile_markers(want)
  if want:sub(1, #PROFILE.aircon) == PROFILE.aircon then
    local m = { airflowDir }
    if want:find("-f", 1, true) then m[#m + 1] = targetTempF end
    if want:find("-h", 1, true) then m[#m + 1] = hAirflow end
    return m
  end
  if want == PROFILE.washer then return { washerProg } end
  -- -v2: dew point components, -v3: wind component
  if want:sub(1, #PROFILE.rainradar) == PROFILE.rainradar then return { windKmh or capabilities.temperatureMeasurement } end
  if want == PROFILE.dishwasher then return { hcProg } end
  if want == PROFILE.oven then return { cookSet } end
  return {}
end

sync_profile = function(device)
  local want = child_profile(device.parent_assigned_child_key or "", device)
  if not want then return end
  local missing = false
  for _, marker in ipairs(profile_markers(want)) do
    if marker ~= nil and not device:supports_capability(marker) then missing = true end
  end
  local same = device:get_field("profile") == want
  if same and not missing then return end
  local tries = 0
  if same then
    -- switched before, but the device still lacks the new capability (the switch got lost): try again,
    -- at most 10 times, 5 min apart
    tries = device:get_field("profileTries") or 0
    local last = device:get_field("profileAt") or 0
    if tries >= 10 or os.time() - last < 300 then return end
  end
  device:set_field("profileTries", tries + 1)
  device:set_field("profileAt", os.time())
  device:try_update_metadata({ profile = want })
  device:set_field("profile", want, { persist = true })
  log.info("[stbridge] " .. device.label .. ": profile " .. want .. (missing and " (retry " .. (tries + 1) .. ")" or ""))
end

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

-- pairing: the bridge hands out its token only while "Pair" is open on its setup page. A hub that already
-- has a token only accepts a new one with a proof made with the old token (or after "Forget pairing").
local function try_pair(parent, ip, port, err)
  local st = state[parent.id]
  if not st or not ip then return end
  local tok = parent:get_field("api_token")
  local forget = (parent.preferences or {}).forgetPairing == true
  local why = tostring(err or "")
  local asked = why:find("pairing required") or why:find("bad signature") or why:find("not signed")
  if tok and not forget and not asked then return end
  if tok and os.time() - (st.last_pair or 0) < 30 then return end
  st.last_pair = os.time()
  local res = api.pair(ip, port)
  if not (res and type(res.token) == "string" and #res.token >= 32) then
    if asked and os.time() - (st.pair_hint or 0) > 600 then
      st.pair_hint = os.time()
      if why:find("not signed") then
        log.warn("[stbridge] " .. parent.label .. ": the bridge at " .. ip .. " does not sign its answers. After a "
          .. "bridge reset: switch on Forget pairing in this device's settings and press Pair on the bridge setup page")
      else
        log.warn("[stbridge] " .. parent.label .. ": the bridge asks for pairing: press Pair on its setup page "
          .. "(https://" .. ip .. ":8443/setup)")
      end
    end
    return
  end
  if tok and not forget and res.token ~= tok
      and tostring(res.proof or ""):lower() ~= sha.hex(sha.hmac(tok, res.token)) then
    log.warn("[stbridge] " .. parent.label .. ": ignored a pairing answer from " .. ip .. " without proof of the old token")
    return
  end
  parent:set_field("api_token", res.token, { persist = true })
  api.set_token(ip, port, res.token)
  log.info("[stbridge] " .. parent.label .. ": paired with the bridge, requests are signed from now on")
end

local function maybe_rescan(driver, parent)
  local st = state[parent.id]
  if st and not st.scanning and os.time() - (st.last_scan or 0) > 120 then
    st.scanning, st.last_scan = true, os.time()
    cosock.spawn(function() rescan(driver); st.scanning = false end, "stbridge-rescan")
  end
end

local function poll(driver, parent)
  local rm = tonumber((parent.preferences or {}).resyncMinutes)
  REFRESH_BY_PARENT[parent.id] = (rm and rm >= 1) and math.floor(rm * 60) or EMIT_REFRESH
  local ip, port = bridge_addr(parent)
  if not ip then
    maybe_rescan(driver, parent)
    return
  end
  local devices, err = api.devices(ip, port)
  local st = state[parent.id]
  if st then st.fails = devices and 0 or (st.fails or 0) + 1 end
  try_pair(parent, ip, port, err)
  if not devices then
    log.warn(string.format("[stbridge] %s: poll failed: %s", parent.label, tostring(err)))
    -- bridge moved to another pc or got a new IP: look for it again (same bridge id), unless the IP is fixed
    local fixed = (parent.preferences or {}).ipAddress
    if (fixed == nil or fixed == "" or fixed == "auto") and st and st.fails >= 2 then maybe_rescan(driver, parent) end
    set_conn(parent, false, "poll failed: " .. tostring(err))
    parent:offline()
    for _, child in pairs(children_by_key(driver, parent)) do child:offline() end
    return
  end
  parent:online()
  set_conn(parent, true)
  local kids = children_by_key(driver, parent)
  for _, d in ipairs(devices) do
    local child = kids[d.id]
    if not child then
      create_child(driver, parent, d)
    else
      if d.online == false then child:offline() else child:online() end
      sync_profile(child)
      local fn = EMIT[d.type]
      -- an empty state (e.g. a washer the bridge has not reached yet after a restart) emits nothing
      if fn and type(d.state) == "table" and next(d.state) ~= nil then
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
    return false
  end
  speed_up(driver, parent)
  return true
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

local function handle_hc_machine(driver, device, cmd)
  local want = cmd.args.state or cmd.args.machineState
  if want == "stop" or want == "ready" then send(driver, device, "stop")
  elseif want == "pause" or want == "paused" then send(driver, device, "pause")
  elseif want == "run" or want == "running" then send(driver, device, "start") end  -- resumes when paused
end

-- order in which changes kept while an air conditioner was off are sent after switching on (mode first: on a
-- Fujitsu a mode command also switches the unit on; a louver position after swing, as it ends swing)
local PENDING_ORDER = { "setpoint", "fan", "swing", "vdir", "hdir" }
local PENDING_DELAY = 8
local PENDING_TIMER = {}  -- device.id -> timer of the delayed sends
local unit_off            -- defined with the other air conditioner helpers below
local function cancel_pending_timer(device)
  local tm = PENDING_TIMER[device.id]
  if tm then pcall(function() device.thread:cancel_timer(tm) end); PENDING_TIMER[device.id] = nil end
end
local function handle_switch_on(driver, device, cmd)
  local c = cmd and cmd.component
  if c == "climate" then send(driver, device, "climate_on"); return end
  local pend = get_pending(device)
  if pend then
    set_pending(device, nil)
    device:set_field("ac_shown", nil)
    log.info("[stbridge] " .. device.label .. ": switching on with the changes made while off")
    -- a mode switches a Fujitsu on; Minimum Heat is a separate setting, so switch on first
    local rest = {}
    if pend.mode and pend.mode.value ~= "minheat" then
      send(driver, device, pend.mode.what, pend.mode.value)
    else
      send(driver, device, "on")
      if pend.mode then rest[#rest + 1] = pend.mode end
    end
    -- the other settings a few seconds later: sent right after "on" the unit ignored them (seen 2026-11-08)
    for _, k in ipairs(PENDING_ORDER) do if pend[k] then rest[#rest + 1] = pend[k] end end
    cancel_pending_timer(device)
    if #rest > 0 then
      PENDING_TIMER[device.id] = device.thread:call_with_delay(PENDING_DELAY, function()
        PENDING_TIMER[device.id] = nil
        if unit_off(device) then return end  -- switched off again in the meantime
        for _, x in ipairs(rest) do send(driver, device, x.what, x.value) end
      end)
    end
    return
  end
  local s = device:get_field("ac_shown")
  if type(s) == "table" then s.power = "on" end
  send(driver, device, "on")
end
local function handle_switch_off(driver, device, cmd)
  local c = cmd and cmd.component
  if c ~= "climate" then
    cancel_pending_timer(device)
    if device:get_field("ac_pending") then set_pending(device, nil) end
    -- off right away for the check in ac_change (the next poll confirms it), so a list opened just after
    -- switching off does not switch the unit on again
    local s = device:get_field("ac_shown")
    if type(s) == "table" then s.power = "off" end
  end
  send(driver, device, c == "climate" and "climate_off" or "off")
end
-- a refused lock command (e.g. unlock without the gateway key): show the real state again instead of a spinner
local function lock_cmd(driver, device, cmd)
  if send(driver, device, cmd) then return end
  local cur = device:get_latest_state("main", capabilities.lock.ID, capabilities.lock.lock.NAME)
  pcall(function()
    local ev = cur == "unlocked" and capabilities.lock.lock.unlocked() or capabilities.lock.lock.locked()
    ev.state_change = true
    device:emit_event(ev)
  end)
end
local function handle_lock(driver, device) lock_cmd(driver, device, "lock") end
local function handle_unlock(driver, device) lock_cmd(driver, device, "unlock") end
-- what the app shows for each setting (fallback when the last poll is not known yet)
local SHOWN_STATE = {
  mode = { "silvervalley08660.airconMode", "mode" }, fan = { "airConditionerFanMode", "fanMode" },
  swing = { "fanOscillationMode", "fanOscillationMode" }, vdir = { "silvervalley08660.airflowDirection", "position" },
  hdir = { "silvervalley08660.horizontalAirflow", "position" }, setpoint = { "thermostatCoolingSetpoint", "coolingSetpoint" },
}
local function shown_value(device, key)
  local shown = device:get_field("ac_shown")
  if type(shown) == "table" and shown[key] ~= nil then return shown[key] end
  local a = SHOWN_STATE[key]
  if not a then return nil end
  local ok, v = pcall(device.get_latest_state, device, "main", a[1], a[2])
  return ok and v or nil
end
local function unchanged(device, key, value)
  local v = shown_value(device, key)
  if v ~= nil and (v == value or (key == "setpoint" and tonumber(v) == tonumber(value))) then
    log.info(string.format("[stbridge] %s: %s already %s, nothing sent", device.label, key, tostring(value)))
    return true
  end
  return false
end
unit_off = function(device)
  local shown = device:get_field("ac_shown")
  if type(shown) == "table" and shown.power then return shown.power == "off" end
  local ok, v = pcall(device.get_latest_state, device, "main", capabilities.switch.ID, capabilities.switch.switch.NAME)
  return ok and v == "off"
end
-- show a setting right away (a change kept while off, which the unit does not report)
local function show_setting(device, key, v)
  if key == "mode" then
    try_emit(device, function() return acMode.airConditionerMode(v) end)
    try_emit(device, function() return acModeX.mode(v) end)
  elseif key == "fan" then try_emit(device, function() return acFan.fanMode(v) end)
  elseif key == "swing" then try_emit(device, function() return oscillation.fanOscillationMode(v) end)
  elseif key == "vdir" then try_emit(device, function() return airflowDir.position(v) end)
  elseif key == "hdir" and hAirflow then try_emit(device, function() return hAirflow.position(v) end)
  elseif key == "setpoint" then
    try_emit(device, function() return coolSetpoint.coolingSetpoint({ value = v, unit = "C" }) end)
    if ac_unit_f(device) and device:supports_capability(targetTempF) then
      try_emit(device, function() return targetTempF.temperature({ value = c_to_f(v), unit = "F" }) end)
    else
      try_emit(device, function() return targetTemp.temperature({ value = v, unit = "C" }) end)
    end
  end
end
-- after a command the unit reports back at the next poll; until then the app shows the new value
local function ac_send(driver, device, what, value, key, shown)
  local s = device:get_field("ac_shown")
  if type(s) == "table" and key then
    s[key] = shown
    if key == "vdir" or key == "hdir" then s.swing = nil end    -- a position ends swing
    if key == "swing" then s.vdir, s.hdir = nil, nil end
    if key == "mode" then s.power = "on" end                    -- a mode command switches a Fujitsu on
  end
  local ok = send(driver, device, what, value)
  if not ok then device:set_field("ac_shown", nil) end
  return ok
end
-- every setting of an air conditioner goes through here:
-- * the same value as shown (the app sends that when a list is opened and closed without a choice): nothing
-- * the unit is off: on a Fujitsu a command switches it on, so keep the change and send it when it is switched
--   on (setting "Changes while off": "Send now" sends right away)
local function ac_change(driver, device, key, shown, what, value)
  log.info(string.format("[stbridge] %s: command %s %s (unit %s)", device.label, key, tostring(shown),
    unit_off(device) and "off" or "on"))
  if unchanged(device, key, shown) then return end
  if unit_off(device) and (device.preferences or {}).offChanges ~= "send" then
    local pend = get_pending(device) or {}
    pend[key] = { what = what, value = value, shown = shown }
    if key == "vdir" or key == "hdir" then pend.swing = nil end      -- a position ends swing
    if key == "swing" then pend.vdir, pend.hdir = nil, nil end
    set_pending(device, pend)
    local s = device:get_field("ac_shown")
    if type(s) == "table" then
      s[key] = shown
      if key == "vdir" or key == "hdir" then s.swing = nil end
      if key == "swing" then s.vdir, s.hdir = nil, nil end
    end
    show_setting(device, key, shown)
    log.info(string.format("[stbridge] %s: unit is off, %s %s kept until it is switched on", device.label, key,
      tostring(shown)))
    return
  end
  ac_send(driver, device, what, value, key, shown)
end
local function handle_ac_mode(driver, device, cmd)
  local m = AC_MODE_FROM_ST[cmd.args.mode or ""]
  if not m then log.warn("[stbridge] unsupported AC mode " .. tostring(cmd.args.mode)); return end
  ac_change(driver, device, "mode", AC_MODE_TO_ST[m], "mode", m)
end
local function send_setpoint(driver, device, c)
  if not c then return end
  ac_change(driver, device, "setpoint", c, "setpoint", c)
end
-- standard setpoint (routines, other apps): °C, or °F from a US location (then the device switches to °F)
local function handle_setpoint(driver, device, cmd)
  local v = tonumber(cmd.args.setpoint or cmd.args.temperature)
  if not v then return end
  if v > 40 then
    if device:get_field("unitF") ~= true then
      device:set_field("unitF", true, { persist = true })
      if sync_profile then sync_profile(device) end
    end
    v = f_to_c(v)
  end
  send_setpoint(driver, device, v)
end
local function handle_setpoint_f(driver, device, cmd)
  local v = tonumber(cmd.args.temperature)
  if v then send_setpoint(driver, device, f_to_c(v)) end
end
local function handle_fan_mode(driver, device, cmd)
  ac_change(driver, device, "fan", cmd.args.fanMode, "fan", cmd.args.fanMode)
end
local function handle_oscillation(driver, device, cmd)
  local m = SWING_FROM_ST[cmd.args.fanOscillationMode or ""]
  if not m then return end
  ac_change(driver, device, "swing", cmd.args.fanOscillationMode, "swing", m)
end
local function handle_vdir(driver, device, cmd)
  local n = tonumber(cmd.args.position)
  if n then ac_change(driver, device, "vdir", tostring(math.floor(n)), "vdir", math.floor(n)) end
end
local function handle_hdir(driver, device, cmd)
  local n = tonumber(cmd.args.position)
  if n then ac_change(driver, device, "hdir", tostring(math.floor(n)), "hdir", math.floor(n)) end
end
-- what the driver shows, for the bridge setup page (driver build, profile, °C/°F): only sent when it changes
send_diag = function(dev)
  local want = child_profile and child_profile(dev.parent_assigned_child_key or "", dev) or "?"
  local unit = ac_unit_f(dev) and (dev:supports_capability(targetTempF) and "F" or "F (profile switch pending)") or "C"
  local horizontal = dev:get_field("hasHDir") and "positions reported" or "no positions reported"
  if not hAirflow then horizontal = horizontal .. ", capability missing on this hub"
  elseif dev:supports_capability(hAirflow) then horizontal = horizontal .. ", control shown" end
  local d = { build = BUILD, profile = tostring(dev:get_field("profile") or want), unit = unit,
    unitSetting = tostring((dev.preferences or {}).temperatureUnit or "auto"), horizontal = horizontal }
  local enc = d.build .. d.profile .. d.unit .. d.unitSetting .. d.horizontal
  if dev:get_field("diag_sent") == enc then return end
  local parent = dev:get_parent_device()
  if not parent then return end
  local ip, port = bridge_addr(parent)
  -- one attempt per change, also when the bridge is older and refuses "diag" (tried again at the next start)
  dev:set_field("diag_sent", enc)
  pcall(api.command, ip, port, dev.parent_assigned_child_key, "diag", d)
end

-- lifecycle -----------------------------------------------------------------------

-- Buienradar location settings live on the Buienradar device; the bridge stores them in its config. Pushed on
-- every change and again at every driver start, so a change made while the bridge was down is not lost.
local function push_rain_settings(driver, device)
  local p = device.preferences or {}
  if p.postcode == nil and p.location == nil and p.address == nil then return end
  send(driver, device, "settings", { postcode = p.postcode or "", housenumber = p.houseNumber or "",
    address = p.address or "", location = p.location or "", radius_m = p.radiusMeters,
    threshold = p.rainThreshold, provider = p.provider or "auto", humid_hours = p.humidHours })
end

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
  if device:supports_capability(dishOp) then
    device:emit_event(dishOp.supportedMachineStates({ "run", "stop" }, { visibility = { displayed = false } }))
  end
  if device:supports_capability(ovenOp) then
    device:emit_event(ovenOp.supportedMachineStates({ "ready", "running", "paused" }, { visibility = { displayed = false } }))
  end
  if device:supports_capability(washerOp) then
    device:emit_event(washerOp.supportedMachineStates({ "run", "pause", "stop" }, { visibility = { displayed = false } }))
  end
  if device:supports_capability(dryerOp) then
    device:emit_event(dryerOp.supportedMachineStates({ "run", "pause", "stop" }, { visibility = { displayed = false } }))
  end
  -- profile changed since the device was made (new category/icon, battery graph option): switch it once
  sync_profile(device)
  if key == "buienradar" or key == "demo-rainradar" then
    device.thread:call_with_delay(30, function() pcall(push_rain_settings, driver, device) end)
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
    -- Buienradar on/off: the bridge starts or stops the rain radar (settings are on the Buienradar device)
    local old = args and args.old_st_store and args.old_st_store.preferences or {}
    local p = device.preferences or {}
    if p.forgetPairing == true and old.forgetPairing ~= true then
      device:set_field("api_token", nil, { persist = true })
      local ip, port = bridge_addr(device)
      api.set_token(ip, port, nil)
      log.info("[stbridge] " .. device.label .. ": pairing forgotten; press Pair on the bridge setup page")
    end
    if p.rainRadar ~= nil and p.rainRadar ~= old.rainRadar then
      local ip, port = bridge_addr(device)
      local res, err = api.buienradar(ip, port, p.rainRadar)
      if not res then log.error("[stbridge] Buienradar on/off: " .. tostring(err)) end
    end
    schedule_poll(driver, device)
    return
  end
  device:set_field("profileTries", 0) -- a changed setting earns new attempts
  sync_profile(device) -- battery graph option changed: other profile
  -- car settings: pass them to the bridge
  local old = args and args.old_st_store and args.old_st_store.preferences or {}
  local p = device.preferences or {}
  if p.pollMinutes ~= nil and (p.pollMinutes ~= old.pollMinutes or p.climateTemp ~= old.climateTemp) then
    send(driver, device, "settings", { poll_minutes = p.pollMinutes, climate_temp = p.climateTemp })
  end
  -- update interval of the FGLair and VELUX sources
  if p.pollSeconds ~= nil and p.pollSeconds ~= old.pollSeconds then
    send(driver, device, "settings", { poll_seconds = p.pollSeconds })
  end
  -- Buienradar settings: pass them to the bridge (it looks up the postcode)
  local rkeys = { "postcode", "houseNumber", "address", "location", "radiusMeters", "rainThreshold", "provider",
    "humidHours" }
  local rchanged = false
  for _, k in ipairs(rkeys) do if p[k] ~= nil and p[k] ~= old[k] then rchanged = true end end
  if rchanged then push_rain_settings(driver, device) end
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
    local tok = dev:get_field("api_token")
    if tok and dev:get_field("ip") ~= ip then
      -- paired: only follow the bridge to a new address when it proves it has our token (signed answer)
      api.set_token(ip, port, tok)
      local ok_info, verr = api.info(ip, port, 3)
      if not ok_info then
        log.warn(string.format("[stbridge] ignored %s at %s: %s", info.id, ip, tostring(verr)))
        return
      end
    end
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
    [dishOp.ID] = {
      [dishOp.commands.setMachineState.NAME] = handle_hc_machine,
    },
    [ovenOp.ID] = {
      [ovenOp.commands.setMachineState.NAME] = handle_hc_machine,
      [ovenOp.commands.stop.NAME] = function(driver, device) send(driver, device, "stop") end,
    },
    [ovenSetpoint.ID] = {
      [ovenSetpoint.commands.setOvenSetpoint.NAME] = function(driver, device, cmd)
        send(driver, device, "setpoint", tonumber(cmd.args.setpoint))
      end,
    },
    [hcProg.ID] = {
      [hcProg.commands.setProgram.NAME] = function(driver, device, cmd) send(driver, device, "program", cmd.args.program) end,
      [hcProg.commands.startProgram.NAME] = function(driver, device, cmd) send(driver, device, "start", cmd.args.program) end,
      [hcProg.commands.start.NAME] = function(driver, device) send(driver, device, "start") end,
    },
    [capabilities.audioVolume.ID] = {
      [capabilities.audioVolume.commands.setVolume.NAME] = function(driver, device, cmd) send(driver, device, "volume", cmd.args.volume) end,
      [capabilities.audioVolume.commands.volumeUp.NAME] = function(driver, device) send(driver, device, "volumeUp") end,
      [capabilities.audioVolume.commands.volumeDown.NAME] = function(driver, device) send(driver, device, "volumeDown") end,
    },
    [capabilities.audioMute.ID] = {
      [capabilities.audioMute.commands.mute.NAME] = function(driver, device) send(driver, device, "mute", true) end,
      [capabilities.audioMute.commands.unmute.NAME] = function(driver, device) send(driver, device, "mute", false) end,
      [capabilities.audioMute.commands.setMute.NAME] = function(driver, device, cmd) send(driver, device, "mute", cmd.args.state == "muted") end,
    },
    [capabilities.mediaInputSource.ID] = {
      [capabilities.mediaInputSource.commands.setInputSource.NAME] = function(driver, device, cmd) send(driver, device, "input", cmd.args.mode) end,
    },
    [capabilities.tvChannel.ID] = {
      [capabilities.tvChannel.commands.channelUp.NAME] = function(driver, device) send(driver, device, "channelUp") end,
      [capabilities.tvChannel.commands.channelDown.NAME] = function(driver, device) send(driver, device, "channelDown") end,
    },
    [washerProg.ID] = {
      [washerProg.commands.setProgram.NAME] = function(driver, device, cmd) send(driver, device, "program", cmd.args.program) end,
      [washerProg.commands.startProgram.NAME] = function(driver, device, cmd) send(driver, device, "start", cmd.args.program) end,
      [washerProg.commands.start.NAME] = function(driver, device) send(driver, device, "start") end,
    },
    [cookSet.ID] = {
      [cookSet.commands.setTemperature.NAME] = function(driver, device, cmd)
        send(driver, device, "setpoint", tonumber(cmd.args.temperature))
      end,
      [cookSet.commands.setDuration.NAME] = function(driver, device, cmd)
        send(driver, device, "duration", tonumber(cmd.args.duration))
      end,
    },
    [washerOp.ID] = {
      [washerOp.commands.setMachineState.NAME] = handle_hc_machine,
    },
    [dryerOp.ID] = {
      [dryerOp.commands.setMachineState.NAME] = handle_machine_state,
    },
    [capabilities.switch.ID] = {
      [capabilities.switch.commands.on.NAME] = handle_switch_on,
      [capabilities.switch.commands.off.NAME] = handle_switch_off,
    },
    [capabilities.lock.ID] = {
      [capabilities.lock.commands.lock.NAME] = handle_lock,
      [capabilities.lock.commands.unlock.NAME] = handle_unlock,
    },
    [acMode.ID] = {
      [acMode.commands.setAirConditionerMode.NAME] = handle_ac_mode,
    },
    [coolSetpoint.ID] = {
      [coolSetpoint.commands.setCoolingSetpoint.NAME] = handle_setpoint,
    },
    [acModeX.ID] = {
      [acModeX.commands.setMode.NAME] = handle_ac_mode,
    },
    [targetTemp.ID] = {
      [targetTemp.commands.setTemperature.NAME] = handle_setpoint,
    },
    [targetTempF.ID] = {
      [targetTempF.commands.setTemperature.NAME] = handle_setpoint_f,
    },
    [airflowDir.ID] = {
      [airflowDir.commands.setPosition.NAME] = handle_vdir,
    },
    [acFan.ID] = {
      [acFan.commands.setFanMode.NAME] = handle_fan_mode,
    },
    ["silvervalley08660.horizontalAirflow"] = {
      setPosition = handle_hdir,
    },
    [oscillation.ID] = {
      [oscillation.commands.setFanOscillationMode.NAME] = handle_oscillation,
    },
  },
})

rescan = function(drv) discovery(drv, {}, function() return true end) end

log.info("[stbridge] Stoeckie Bridge (local) driver starting, build " .. BUILD)
driver:run()
