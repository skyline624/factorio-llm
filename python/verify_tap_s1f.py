"""Validation live S1f volet B : splitter de prélèvement priority="left" coupe-t-il la lane bus ?

Question critique : un splitter orienté +v avec 1 SEULE entrée (la lane bus amont) et
output_priority="left" envoie-t-il TOUT le flux vers la sortie prioritaire (+u, consommateur),
laissant la sortie non-prio (lane aval +v) à 0 ? Si oui, le tap coupe le bus -> bug volet B.

Géométrie facing=2 (u=east +x, v=south +y), zone 1100,1100 :
  amont   (1100,1100) south  feed -> splitter entrée nord-gauche
  splitter(1100.5,1101) south  1 entrée active (1100,1100), entrée (1101,1100) VIDE
           priority="left" -> sortie est (+u) = (1101,1102) [consommateur]
           sortie ouest (right) = (1100,1102) [lane bus aval]
  out_left  (1101,1102) south  consommateur (sortie prio +u)
  out_right (1100,1102) south  lane bus aval (sortie non-prio +v)

Si out_left > 0 ET out_right == 0 : le splitter coupe la lane (TOUT au prio) -> bug volet B.
Si out_left > 0 ET out_right > 0 : la lane continue (splitter répartit) -> OK.
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

X0, Y0 = 1100, 1100

SETUP = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
for _, e in ipairs(s.find_entities_filtered{area={{%d-1,%d-1},{%d+3,%d+4}}}) do
  if e.name ~= "character" then e.destroy() end
end
local function place(name, x, y, dir)
  return s.create_entity{name=name, position={x,y}, direction=dir, force="player"}
end
local amont = place("transport-belt", %d,   %d,   defines.direction.south)
local spl   = place("splitter",       %d+0.5, %d+1, defines.direction.south)
spl.splitter_output_priority = "left"
local ol    = place("transport-belt", %d+1, %d+2, defines.direction.south)
local or_   = place("transport-belt", %d,   %d+2, defines.direction.south)
rcon.print('{"amont":'..tostring(amont~=nil)..',"spl":'..tostring(spl~=nil)..',"ol":'..tostring(ol~=nil)..',"or":'..tostring(or_~=nil)..',"prio":'..tostring(spl.splitter_output_priority)..'}')
""" % (X0, Y0, X0+3, Y0+4, X0, Y0, X0, Y0, X0, Y0, X0, Y0)

FEED = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local e = s.find_entities_filtered{position={%d+0.5, %d+0.5}, name="transport-belt"}
if #e == 0 then rcon.print("0") else
  local line = e[1].get_transport_line(1)
  for i=1,8 do line.insert_at_back({name="iron-plate"}) end
  rcon.print("8")
end
""" % (X0, Y0)

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
rcon.print('{"amont":'..cnt(%d,%d)..',"out_left":'..cnt(%d+1,%d+2)..',"out_right":'..cnt(%d,%d+2)..'}')
""" % (X0, Y0, X0, Y0, X0, Y0)

print("=== SETUP (splitter priority=left, 1 entrée active) ===")
print("placed:", q(SETUP))

max_l = max_r = 0
last = {}
for cycle in range(12):
    q(FEED)
    time.sleep(0.3)
    m = q(MEASURE)
    last = m
    ol = m.get("out_left", -1); orr = m.get("out_right", -1)
    max_l = max(max_l, ol); max_r = max(max_r, orr)
    print(f"cycle {cycle}: amont={m.get('amont')} out_left(prio,+u)={ol} out_right(lane,+v)={orr}")
    if ol > 0 and orr > 0:
        print("  -> lane continue (splitter répartit) -> OK volet B")
        break
    if ol > 0 and cycle >= 5:
        print("  -> out_left alimenté, out_right reste 0 -> splitter coupe la lane ?")
        # continuer quelques cycles pour confirmer

print(f"\n=== RESULT (max) === out_left(prio,+u)={max_l} out_right(lane,+v)={max_r}")
if max_r > 0:
    print("LANE-CONTINUE -> splitter priority=left ne coupe PAS la lane bus (répartit) -> volet B OK")
elif max_l > 0 and max_r == 0:
    print("LANE-COUPEE -> splitter priority=left + 1 entrée envoie TOUT au prio -> tap coupe le bus")
    print("  -> BUG volet B : il faut splitter équilibré (priority=none) OU 2 entrées actives")
else:
    print("INCONNU -> flux non détecté sur les sorties")
sys.exit(0 if max_r > 0 else 1)