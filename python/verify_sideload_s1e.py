"""Validation live S1e : fallback belt +v intermédiaire devant le splitter (volet A tap).

RÉSULTAT 2026-07-24 (serveur headless) :
  - Sideload perpendiculaire DIRECT sur splitter = IMPOSSIBLE en Factorio 2.0 (un splitter
    ne reçoit les items que par son entrée dédiée -v, pas par le côté). Test préliminaire sans
    la belt `inter` : trans +u atterrit sur la tuile au nord du splitter (vide) -> items
    stagnant (trans=2, out1=out2=0).
  - Fallback belt +v intermédiaire = VALIDÉ (ce script) : trans +u -> inter +v (sideload
    belt->belt, OK) -> splitter entrée -v, flux réparti out1=1 out2=1.
  Implémenté dans layout_planner.py `_route_bus_to_target` (note `split_entry_v`).
  CONSTAT : le tap S1e reste non-circuiterie car bus->transition (lane continue) est aussi
  impossible sans underground -> S1f.

Géométrie facing=2 (u=east=+x, v=south=+y), zone 1000,1000 :
  lane    (1000,1000) south   pousse -> (1000,1001) = trans
  trans   (1000,1001) east    pousse -> (1001,1001) = inter [sideload belt->belt]
  inter   (1001,1001) south   pousse -> (1001,1002) = entrée nord-gauche splitter
  splitter(1001.5,1002) south entrée -v y=1001 (x=1001,1002), sorties +v y=1003
  out1    (1001,1003) south   out2 (1002,1003) south

Si out1>0 ET out2>0 : le fallback belt +v intermédiaire répartit le flux -> tap S1e
(partie transition->splitter) VALIDÉ en jeu. Sinon : ré-examiner la géométrie.

Feed : insert_at_back (1 item/appel, count non supporté en 2.0) en boucle ;
mesures multiples (le train d'items traverse vite, on capture au passage).
Reproductible : utile pour valider S1f (underground bus->transition) plus tard.
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

SETUP = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local X0, Y0 = 1000, 1000
for _, e in ipairs(s.find_entities_filtered{area={{X0-2,Y0-1},{X0+4,Y0+5}}}) do
  if e.name ~= "character" then e.destroy() end
end
local function place(name, x, y, dir)
  return s.create_entity{name=name, position={x,y}, direction=dir, force="player"}
end
local lane  = place("transport-belt", X0,   Y0,   defines.direction.south)
local trans = place("transport-belt", X0,   Y0+1, defines.direction.east)
local inter = place("transport-belt", X0+1, Y0+1, defines.direction.south)
local spl   = place("splitter",       X0+1.5, Y0+2, defines.direction.south)
local out1  = place("transport-belt", X0+1, Y0+3, defines.direction.south)
local out2  = place("transport-belt", X0+2, Y0+3, defines.direction.south)
rcon.print('{"lane":'..tostring(lane~=nil)..',"trans":'..tostring(trans~=nil)..',"inter":'..tostring(inter~=nil)..',"spl":'..tostring(spl~=nil)..',"out1":'..tostring(out1~=nil)..',"out2":'..tostring(out2~=nil)..'}')
"""

FEED = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local X0, Y0 = 1000, 1000
local e = s.find_entities_filtered{position={X0+0.5, Y0+0.5}, name="transport-belt"}
if #e == 0 then rcon.print("0") else
  local line = e[1].get_transport_line(1)
  for i=1,6 do line.insert_at_back({name="iron-plate"}) end
  rcon.print("6")
end
"""

MEASURE = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local X0, Y0 = 1000, 1000
local function cnt(x, y)
  local e = s.find_entities_filtered{position={x+0.5, y+0.5}, name="transport-belt"}
  if #e == 0 then return -1 end
  local c = e[1].get_transport_line(1).get_contents()
  local t = 0
  for _, it in ipairs(c) do t = t + (it.count or 0) end
  return t
end
rcon.print('{"lane":'..cnt(X0,Y0)..',"trans":'..cnt(X0,Y0+1)..',"inter":'..cnt(X0+1,Y0+1)..',"out1":'..cnt(X0+1,Y0+3)..',"out2":'..cnt(X0+2,Y0+3)..'}')
"""

print("=== SETUP (clean zone 1000,1000 + place 5 entités) ===")
print("placed:", q(SETUP))

max_o1 = max_o2 = 0
last = {}
for cycle in range(8):
    q(FEED)
    time.sleep(0.25)
    m = q(MEASURE)
    last = m
    o1 = m.get("out1", -1); o2 = m.get("out2", -1)
    max_o1 = max(max_o1, o1); max_o2 = max(max_o2, o2)
    print(f"cycle {cycle}: lane={m.get('lane')} trans={m.get('trans')} inter={m.get('inter')} out1={o1} out2={o2}")
    if o1 > 0 and o2 > 0:
        print("  -> flux réparti sur 2 sorties détecté, arrêt")
        break

print(f"\n=== RESULT (max) === out1={max_o1} out2={max_o2} (dernier: {last})")
ok = max_o1 > 0 and max_o2 > 0
print("SIDELOAD-FALLBACK " + ("OK -> belt +v intermédiaire répartit le flux sur 2 sorties -> "
                              "tap S1e (transition->splitter) VALIDÉ en jeu"
                              if ok else
                              "ECHEC -> la belt +v intermédiaire ne feed pas le splitter -> ré-examiner la géométrie"))
sys.exit(0 if ok else 1)