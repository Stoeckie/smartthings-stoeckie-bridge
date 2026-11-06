-- HTTP client for the Stoeckie Bridge LAN API.
local cosock = require "cosock"
local http = cosock.asyncify "socket.http"
local ltn12 = require "ltn12"
local json = require "st.json"
local sha = require "sha512"

local M = {}
M.TIMEOUT = 5
-- Signing tokens per bridge address (set by init.lua from the paired bridge device). With a token every
-- request carries an HMAC-SHA512 signature and every answer must carry the bridge's signature back.
M.tokens = {}

local function addr(ip, port) return tostring(ip) .. ":" .. tostring(port or 8787) end
function M.set_token(ip, port, token) if ip then M.tokens[addr(ip, port)] = token end end

math.randomseed(os.time())

local function request(method, ip, port, path, body, timeout, unsigned)
  if not ip or ip == "" then return nil, "no bridge IP" end
  local chunks = {}
  local headers = {}
  local source
  if body then
    body = json.encode(body)
    headers["Content-Type"] = "application/json"
    headers["Content-Length"] = tostring(#body)
    source = ltn12.source.string(body)
  end
  local token = (not unsigned) and M.tokens[addr(ip, port)] or nil
  local sig
  if token then
    local ts = string.format("%d.%06d", os.time(), math.random(0, 999999))
    sig = sha.hex(sha.hmac(token, method .. "\n" .. path .. "\n" .. ts .. "\n" .. (body or "")))
    headers["X-Stbridge-Time"] = ts
    headers["X-Stbridge-Sig"] = sig
  end
  http.TIMEOUT = timeout or M.TIMEOUT
  local ok, code, rheaders = http.request({
    url = string.format("http://%s:%d%s", ip, port or 8787, path),
    method = method,
    headers = headers,
    source = source,
    sink = ltn12.sink.table(chunks),
  })
  if not ok then return nil, tostring(code) end
  local text = table.concat(chunks)
  if token and code == 200 then
    local got
    for k, v in pairs(rheaders or {}) do if tostring(k):lower() == "x-stbridge-sig" then got = tostring(v):lower() end end
    if got ~= sha.hex(sha.hmac(token, sig .. "\n" .. text)) then
      return nil, "answer not signed by the paired bridge (another device at " .. tostring(ip) .. "?)"
    end
  end
  local dok, data = pcall(json.decode, text)
  if code ~= 200 then
    return nil, "HTTP " .. tostring(code) .. " " .. ((dok and type(data) == "table" and data.error) or text)
  end
  if not dok or type(data) ~= "table" then return nil, "unexpected reply: " .. text end
  return data
end

function M.info(ip, port, timeout) return request("GET", ip, port, "/stbridge/info", nil, timeout) end

--- Discovery probe: returns the info table when ip:port is a Stoeckie Bridge.
function M.probe(ip, port, timeout)
  local data = M.info(ip, port, timeout or 1.5)
  if data and data.service == "stoeckie-bridge" and data.id then return data end
  return nil
end

function M.devices(ip, port)
  local data, err = request("GET", ip, port, "/stbridge/devices")
  if not data then return nil, err end
  return data.devices or {}
end

function M.command(ip, port, dev_id, cmd, value)
  local path = "/stbridge/devices/" .. dev_id .. "/command"
  return request("POST", ip, port, path, { cmd = cmd, value = value }, 15)
end

--- Ask the bridge for its token; only works while "Pair" is open on the bridge setup page.
function M.pair(ip, port)
  return request("POST", ip, port, "/stbridge/pair", { hub = true }, 5, true)
end

--- Buienradar on/off from the bridge device settings.
function M.buienradar(ip, port, enabled)
  return request("POST", ip, port, "/stbridge/buienradar", { enabled = enabled and true or false }, 10)
end

return M
