-- SHA-512, HMAC-SHA512 and HKDF-SHA512 in pure Lua 5.3 (64-bit integers).
local M = {}

local K = {
  0x428a2f98d728ae22, 0x7137449123ef65cd, -0x4a3f043013b2c4d1, -0x164a245a7e762444,
  0x3956c25bf348b538, 0x59f111f1b605d019, -0x6dc07d5b50e6b065, -0x54e3a12a25927ee8,
  -0x27f855675cfcfdbe, 0x12835b0145706fbe, 0x243185be4ee4b28c, 0x550c7dc3d5ffb4e2,
  0x72be5d74f27b896f, -0x7f214e01c4e9694f, -0x6423f958da38edcb, -0x3e640e8b3096d96c,
  -0x1b64963e610eb52e, -0x1041b879c7b0da1d, 0x0fc19dc68b8cd5b5, 0x240ca1cc77ac9c65,
  0x2de92c6f592b0275, 0x4a7484aa6ea6e483, 0x5cb0a9dcbd41fbd4, 0x76f988da831153b5,
  -0x67c1aead11992055, -0x57ce3992d24bcdf0, -0x4ffcd8376704dec1, -0x40a680384110f11c,
  -0x391ff40cc257703e, -0x2a586eb86cf558db, 0x06ca6351e003826f, 0x142929670a0e6e70,
  0x27b70a8546d22ffc, 0x2e1b21385c26c926, 0x4d2c6dfc5ac42aed, 0x53380d139d95b3df,
  0x650a73548baf63de, 0x766a0abb3c77b2a8, -0x7e3d36d1b812511a, -0x6d8dd37aeb7dcac5,
  -0x5d40175eb30efc9c, -0x57e599b443bdcfff, -0x3db4748f2f07686f, -0x3893ae5cf9ab41d0,
  -0x2e6d17e62910ade8, -0x2966f9dbaa9a56f0, -0x0bf1ca7aa88edfd6, 0x106aa07032bbd1b8,
  0x19a4c116b8d2d0c8, 0x1e376c085141ab53, 0x2748774cdf8eeb99, 0x34b0bcb5e19b48a8,
  0x391c0cb3c5c95a63, 0x4ed8aa4ae3418acb, 0x5b9cca4f7763e373, 0x682e6ff3d6b2b8a3,
  0x748f82ee5defb2fc, 0x78a5636f43172f60, -0x7b3787eb5e0f548e, -0x7338fdf7e59bc614,
  -0x6f410005dc9ce1d8, -0x5baf9314217d4217, -0x41065c084d3986eb, -0x398e870d1c8dacd5,
  -0x35d8c13115d99e64, -0x2e794738de3f3df9, -0x15258229321f14e2, -0x0a82b08011912e88,
  0x06f067aa72176fba, 0x0a637dc5a2c898a6, 0x113f9804bef90dae, 0x1b710b35131c471b,
  0x28db77f523047d84, 0x32caab7b40c72493, 0x3c9ebe0a15c9bebc, 0x431d67c49c100d4c,
  0x4cc5d4becb3e42b6, 0x597f299cfc657e2a, 0x5fcb6fab3ad6faec, 0x6c44198c4a475817,
}

local function rotr(x, n) return (x >> n) | (x << (64 - n)) end

local function compress(H, block)
  local w = {}
  for i = 1, 16 do
    w[i] = string.unpack(">i8", block, (i - 1) * 8 + 1)
  end
  for i = 17, 80 do
    local a, b = w[i - 15], w[i - 2]
    local s0 = rotr(a, 1) ~ rotr(a, 8) ~ (a >> 7)
    local s1 = rotr(b, 19) ~ rotr(b, 61) ~ (b >> 6)
    w[i] = w[i - 16] + s0 + w[i - 7] + s1
  end
  local a, b, c, d, e, f, g, h = H[1], H[2], H[3], H[4], H[5], H[6], H[7], H[8]
  for i = 1, 80 do
    local S1 = rotr(e, 14) ~ rotr(e, 18) ~ rotr(e, 41)
    local ch = (e & f) ~ ((~e) & g)
    local t1 = h + S1 + ch + K[i] + w[i]
    local S0 = rotr(a, 28) ~ rotr(a, 34) ~ rotr(a, 39)
    local maj = (a & b) ~ (a & c) ~ (b & c)
    local t2 = S0 + maj
    h, g, f, e, d, c, b, a = g, f, e, d + t1, c, b, a, t1 + t2
  end
  H[1], H[2], H[3], H[4] = H[1] + a, H[2] + b, H[3] + c, H[4] + d
  H[5], H[6], H[7], H[8] = H[5] + e, H[6] + f, H[7] + g, H[8] + h
end

function M.sha512(msg)
  local H = {
    0x6a09e667f3bcc908, -0x4498517a7b3558c5, 0x3c6ef372fe94f82b, -0x5ab00ac5a0e2c90f,
    0x510e527fade682d1, -0x64fa9773d4c193e1, 0x1f83d9abfb41bd6b, 0x5be0cd19137e2179,
  }
  local len = #msg
  local pad = 128 - ((len + 17) % 128)
  if pad == 128 then pad = 0 end
  msg = msg .. "\128" .. string.rep("\0", pad) .. string.pack(">I8I8", 0, len * 8)
  for i = 1, #msg, 128 do
    compress(H, msg:sub(i, i + 127))
  end
  return string.pack(">i8i8i8i8i8i8i8i8", H[1], H[2], H[3], H[4], H[5], H[6], H[7], H[8])
end

function M.hmac(key, msg)
  if #key > 128 then key = M.sha512(key) end
  key = key .. string.rep("\0", 128 - #key)
  local ipad = key:gsub(".", function(c) return string.char(c:byte() ~ 0x36) end)
  local opad = key:gsub(".", function(c) return string.char(c:byte() ~ 0x5c) end)
  return M.sha512(opad .. M.sha512(ipad .. msg))
end

-- HKDF-SHA512 (RFC 5869)
function M.hkdf(ikm, salt, info, len)
  local prk = M.hmac(salt or "", ikm)
  local out, t, i = {}, "", 1
  local total = 0
  while total < len do
    t = M.hmac(prk, t .. info .. string.char(i))
    out[#out + 1] = t
    total = total + #t
    i = i + 1
  end
  return table.concat(out):sub(1, len)
end

function M.hex(s) return (s:gsub(".", function(c) return string.format("%02x", c:byte()) end)) end

return M
