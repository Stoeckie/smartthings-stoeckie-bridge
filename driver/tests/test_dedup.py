from lupa import lua53
import pathlib
SRC = pathlib.Path("/home/claude/smartthings-stoeckie-bridge/driver/src")
def test_push_dedup():
    lua = lua53.LuaRuntime(unpack_returned_tuples=True)
    t = open(SRC / "init.lua", encoding="utf-8").read()
    a = t.index("local stjson = require"); b = t.index("-- emit one event")
    lua.execute('package.loaded["st.json"] = { encode = function(x) local s = "" for i = 1, 2 do local v = x[i] if type(v) == "table" then for k, vv in pairs(v) do s = s .. tostring(k) .. "=" .. tostring(vv) .. ";" end end s = s .. "|" end return s end }')
    lua.execute('log = { info = function() end, warn = function() end }')
    lua.execute(t[a:b] + '''
      DEV = { id = "d1", label = "D", f = {}, out = {} }
      function DEV:get_field(k) return self.f[k] end
      function DEV:set_field(k, v) self.f[k] = v end
      function DEV:emit_event(e) local s = e.state or e.value; self.out[#self.out + 1] = (s and s.value or "raw") .. (e.state_change and "!" or "") end
      function DEV:emit_component_event(c, e) self.out[#self.out + 1] = c.id .. ":" .. e.state.value .. (e.state_change and "!" or "") end
      function EV(v) return { capability_id = "switch", attribute_id = "switch", state = { value = v } } end
      PUSH = push
      REFRESH_BY_PARENT_G = REFRESH_BY_PARENT
      NOW = 1000
      os.time = function() return NOW end
    ''')
    lua.execute('PUSH(DEV, nil, EV("on")); PUSH(DEV, nil, EV("on")); PUSH(DEV, nil, EV("off")); PUSH(DEV, nil, EV("on"))')
    lua.execute('PUSH(DEV, {id="outdoor"}, EV("on"))')
    # resend off (default): an unchanged value is never sent again, however long ago
    lua.execute('NOW = NOW + 24 * 3600; PUSH(DEV, nil, EV("on"))')
    # resend every 15 min: the same value goes out again with state_change, spread over up to 5 min
    lua.execute('REFRESH_BY_PARENT_G["p"] = 15 * 60; DEV.parent_device_id = "p"')
    lua.execute('NOW = NOW + 15 * 60 - 1; PUSH(DEV, nil, EV("on"))')
    lua.execute('NOW = NOW + 1 + 300; PUSH(DEV, nil, EV("on"))')
    lua.execute('DEV.parent_device_id = nil')
    # the SDK shape: capability/attribute objects instead of ids
    lua.execute('PUSH(DEV, nil, { capability = { ID = "switch" }, attribute = { NAME = "switch" }, value = { value = "on" } })')
    # the hub shape (capability, attribute, value): a change is seen
    lua.execute('PUSH(DEV, nil, { capability = { ID = "switch" }, attribute = { NAME = "switch" }, value = { value = "off" } })')
    lua.execute('PUSH(DEV, nil, { other = true })')  # unknown shape: always emitted
    out = lua.eval("DEV.out")
    vals = [out[i] for i in range(1, len(out) + 1)]
    # first time and the hourly refresh carry state_change; a real change does not (the hub sets it)
    assert vals == ["on!", "off", "on", "outdoor:on!", "on!", "off", "raw"], vals
    # a refused emit is not cached: the next identical value is tried again
    lua.execute('''
      DEV2 = { id = "d2", label = "D2", f = {}, out = {}, fail = true }
      function DEV2:get_field(k) return self.f[k] end
      function DEV2:set_field(k, v) self.f[k] = v end
      function DEV2:emit_event(e) if self.fail then error("not in profile") end; self.out[#self.out + 1] = (e.state or e.value).value end
      pcall(PUSH, DEV2, nil, EV("on")); DEV2.fail = false; PUSH(DEV2, nil, EV("on"))
    ''')
    assert lua.eval("DEV2.out[1]") == "on" and lua.eval("#DEV2.out") == 1
