"""Diagnostic 2 (JSON à la main) : mesurer TOUS les maillons bu[1221..1200] à y=1213
pour localiser où le flux s'arrête. Source est 1221 (ouest) -> vir 1220 -> bu 1219..1201
-> lane[13] (1200)."""
import sys, json, time
sys.path.insert(0, "D:/developpement/factorio-llm/python")
from core.rcon import get_rcon

rcon = get_rcon()

def q(lua):
    out = rcon.query_lua(lua).strip()
    i = out.find("{")
    if i >= 0:
        try:
            return json.loads(out[i:])
        except Exception:
            return {"raw": out}
    return {"raw": out}

SETUP = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
for _, e in ipairs(s.find_entities_filtered{area={{1199,1198},{1222,1216}}}) do
  if e.name ~= "character" then e.destroy() end
end
local function place(name, x, y, dir)
  return s.create_entity{name=name, position={x,y}, direction=dir, force="player"}
end
local D = defines.direction
for v=0,15 do place("transport-belt", 1200, 1200+v, D.south) end
place("transport-belt", 1200, 1199, D.south)
place("transport-belt", 1221, 1213, D.west)  -- source est (feed)
place("transport-belt", 1220, 1213, D.west)  -- vir
for x=1219,1201,-1 do place("transport-belt", x, 1213, D.west) end
-- Lister les belts présentes à y=1213 (x de 1200 à 1221) + leur direction
local parts = {}
for x=1221,1200,-1 do
  local e = s.find_entities_filtered{position={x+0.5,1213.5}, name="transport-belt"}
  parts[#parts+1] = tostring(x)..":"..((e[1] and e[1].direction) or -1)
end
rcon.print("{"..table.concat(parts,",").."}")
"""

FEED = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local e = s.find_entities_filtered{position={1221.5,1213.5}, name="transport-belt"}
if #e > 0 then
  local line = e[1].get_transport_line(1)
  for i=1,8 do line.insert_at_back({name="iron-plate"}) end
end
rcon.print("fed")
"""

MEASURE = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local function cnt(x, y)
  local e = s.find_entities_filtered{position={x+0.5, y+0.5}, name="transport-belt"}
  if #e == 0 then return -1 end
  local c = e[1].get_transport_line(1).get_contents()
  local t = 0
  for _, it in ipairs(c) do t = t + (it.count or 0) end
  return t
end
local parts = {}
for x=1221,1200,-1 do parts[#parts+1] = '"x'..x..'":'..cnt(x,1213) end
rcon.print("{"..table.concat(parts,",").."}")
"""

print("=== SETUP (belts à y=1213, x:direction) ===")
print(q(SETUP))
print()
print("=== FEED (source est 1221 ouest) + mesure tous maillons ===")
for cycle in range(15):
    q(FEED)
    time.sleep(0.3)
    m = q(MEASURE)
    chain = " ".join(f"{x}={m.get('x'+str(x),'?')}" for x in [1221,1220,1219,1218,1217,1216,1215,1214,1213,1212,1211,1210,1205,1201,1200])
    print(f"c{cycle}: {chain}")
    if m.get("x1200", 0) > 0 and cycle >= 5:
        print("  -> flux arrive sur lane, arrêt")
        break