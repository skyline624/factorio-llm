-- Sérialiseur JSON maison pour factorio-llm.
-- Le runtime Lua de Factorio n'a pas de lib JSON, on l'écrit à la main.
-- Convention : une table Lua avec des cles 1..n contigues -> array JSON ;
-- toute autre table -> object JSON.
-- Supporte : string, number, boolean, nil, table (array/object), imbrique.

local M = {}

local function is_array(t)
  local n = 0
  for k in pairs(t) do
    n = n + 1
  end
  if n == 0 then
    -- table vide : on encode comme array [] (convention pour listes vides)
    return true
  end
  for i = 1, n do
    if t[i] == nil then
      return false
    end
  end
  return true
end

local function encode_string(s)
  s = tostring(s)
  s = s:gsub("\\", "\\\\")
  s = s:gsub("\"", "\\\"")
  s = s:gsub("\n", "\\n")
  s = s:gsub("\r", "\\r")
  s = s:gsub("\t", "\\t")
  return "\"" .. s .. "\""
end

local function encode_value(v)
  local tv = type(v)
  if tv == "nil" then
    return "null"
  elseif tv == "boolean" then
    return v and "true" or "false"
  elseif tv == "number" then
    if v ~= v or v == math.huge or v == -math.huge then
      return "null"
    end
    if math.floor(v) == v and math.abs(v) < 1e15 then
      return tostring(math.floor(v))
    end
    return tostring(v)
  elseif tv == "string" then
    return encode_string(v)
  elseif tv == "table" then
    return M.encode(v)
  end
  -- function / userdata : on renvoie le type pour debug
  return encode_string("<" .. tv .. ">")
end

function M.encode(t)
  if type(t) ~= "table" then
    return encode_value(t)
  end
  if is_array(t) then
    local parts = {}
    for i = 1, #t do
      parts[#parts + 1] = encode_value(t[i])
    end
    return "[" .. table.concat(parts, ",") .. "]"
  else
    local parts = {}
    for k, v in pairs(t) do
      parts[#parts + 1] = encode_string(tostring(k)) .. ":" .. encode_value(v)
    end
    return "{" .. table.concat(parts, ",") .. "}"
  end
end

return M