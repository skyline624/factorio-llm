"""Diagnostic S1g : le flux stagne sur vir (1220,1213 ouest) reçu par sideload de mrg_out
(1220,1212 sud), et ne part pas vers bu[1219] (1219,1213 ouest). Hypothèses :
  H1 : bu[1219] n'est pas orienté ouest ou mal posé (connexion vir->bu[1219] cassée).
  H2 : le sideload sud->ouest dépose sur une ligne de vir qui ne pousse pas vers l'ouest
       (ligne "near" bloquée ?). Test direct : vir reçoit par l'EST (entré normale) au lieu
       de sideload sud -> si le flux part, c'est le sideload qui pose problème.
  H3 : backpressure depuis la lane (lane[13] au bout de bu[1201]).
On mesure bu[1219], bu[1215], bu[1210], bu[1205], bu[1201] pour localiser où le flux s'arrête.
Et on teste H2 : une 2e config où vir reçoit par l'est (belt est->ouest direct) au lieu de
sideload sud.
"""
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

X0, Y0 = 1200, 1200

# Config 1 : sideload sud->ouest (la config qui échoue), + mesure bu intermédiaires
SETUP1 = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
for _, e in ipairs(s.find_entities_filtered{area={{1199,1198},{1222,1216}}}) do
  if e.name ~= "character" then e.destroy() end
end
local function place(name, x, y, dir)
  return s.create_entity{name=name, position={x,y}, direction=dir, force="player"}
end
local D = defines.direction
for v=0,15 do place("transport-belt", 1200, 1200+v, D.south) end  -- lane
place("transport-belt", 1200, 1199, D.south)  -- lane amont
place("transport-belt", 1220, 1209, D.south)  -- bo1 nord
place("transport-belt", 1221, 1209, D.south)  -- bo2 nord
place("transport-belt", 1220, 1210, D.south)  -- bo1
place("transport-belt", 1221, 1210, D.south)  -- bo2
place("splitter", 1220.5, 1211, D.south)  -- merger
place("transport-belt", 1220, 1212, D.south)  -- mrg_out
place("transport-belt", 1220, 1213, D.west)  -- vir (sideload sud->ouest)
for x=1219,1201,-1 do place("transport-belt", x, 1213, D.west) end  -- bu
-- Vérifie orientations
local v1219 = s.find_entities_filtered{position={1219.5,1213.5}, name="transport-belt"}
local dir1219 = v1219[1] and v1219[1].direction or -1
local vvir = s.find_entities_filtered{position={1220.5,1213.5}, name="transport-belt"}
local dirvir = vvir[1] and vvir[1].direction or -1
rcon.print('{"dir_vir":'..dirvir..',"dir_1219":'..dir1219..'}')
"""

FEED = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local function feed(x, y)
  local e = s.find_entities_filtered{position={x+0.5, y+0.5}, name="transport-belt"}
  if #e > 0 then
    local line = e[1].get_transport_line(1)
    for i=1,6 do line.insert_at_back({name="iron-plate"}) end
  end
end
feed(1220, 1209) feed(1221, 1209)
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
rcon.print('{"vir":'..cnt(1220,1213)..',"b1219":'..cnt(1219,1213)..',"b1215":'..cnt(1215,1213)..',"b1210":'..cnt(1210,1213)..',"b1205":'..cnt(1205,1213)..',"b1201":'..cnt(1201,1213)..',"lane13":'..cnt(1200,1213)..',"lane14":'..cnt(1200,1214)..'}')
"""

print("=== CONFIG 1 : sideload sud->ouest (mesure bu intermédiaires) ===")
print("dirs:", q(SETUP1))
for cycle in range(12):
    q(FEED)
    time.sleep(0.3)
    m = q(MEASURE)
    print(f"c{cycle}: vir={m.get('vir')} b1219={m.get('b1219')} b1215={m.get('b1215')} "
          f"b1210={m.get('b1210')} b1205={m.get('b1205')} b1201={m.get('b1201')} "
          f"lane13={m.get('lane13')} lane14={m.get('lane14')}")

print()
print("=== CONFIG 2 : vir reçoit par l'EST (belt est direct, pas sideload sud) ===")
SETUP2 = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
for _, e in ipairs(s.find_entities_filtered{area={{1198,1212},{1222,1216}}}) do
  if e.name ~= "character" then e.destroy() end
end
local function place(name, x, y, dir)
  return s.create_entity{name=name, position={x,y}, direction=dir, force="player"}
end
local D = defines.direction
for v=0,15 do place("transport-belt", 1200, 1200+v, D.south) end  -- lane
-- vir reçoit par l'est : belt est (1221,1213 ouest) feed direct -> vir (1220,1213 ouest)
place("transport-belt", 1221, 1213, D.west)  -- source est
place("transport-belt", 1220, 1213, D.west)  -- vir
for x=1219,1201,-1 do place("transport-belt", x, 1213, D.west) end  -- bu
rcon.print("setup2")
"""
FEED2 = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local e = s.find_entities_filtered{position={1221.5,1213.5}, name="transport-belt"}
if #e > 0 then
  local line = e[1].get_transport_line(1)
  for i=1,6 do line.insert_at_back({name="iron-plate"}) end
end
rcon.print("fed2")
"""
print("placed:", q(SETUP2))
for cycle in range(12):
    q(FEED2)
    time.sleep(0.3)
    m = q(MEASURE)
    print(f"c{cycle}: vir={m.get('vir')} b1219={m.get('b1219')} b1201={m.get('b1201')} "
          f"lane13={m.get('lane13')} lane14={m.get('lane14')}")
print()
print("Si CONFIG 2 (entrée est) circule mais CONFIG 1 (sideload sud) non :")
print("  -> le sideload sud->ouest est le problème (Factorio 2.0 ne dépose pas sud->ouest")
print("     sur une belt ouest qui pousse vers l'ouest). Il faut un virage explicite.")