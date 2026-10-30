-- HTTP client for the Stoeckie Bridge LAN API.
local cosock = require "cosock"
local http = cosock.asyncify "socket.http"
local ltn12 = require "ltn12"
local json = require "st.json"

local M = {}
M.TIMEOUT = 5

local function request(method, ip, port, path, body, timeout)
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
  http.TIMEOUT = timeout or M.TIMEOUT
  local ok, code = http.request({
    url = string.format("http://%s:%d%s", ip, port or 8787, path),
    method = method,
    headers = headers,
    source = source,
    sink = ltn12.sink.table(chunks),
  })
  if not ok then return nil, tostring(code) end
  local text = table.concat(chunks)
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

return M
