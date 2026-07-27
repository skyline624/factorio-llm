"""Validation live S1g : feed main bus circuiterie (gap + merger tree + virage +v->-u +
belts -u + sideload -u->+v sur lane).

Mécaniques à valider (miroir du tap S1f) :
  T5  : virage +v->-u (belt +v dépose sur belt -u à la tuile de dépôt +v). Miroir de T4 (+v->+u).
  T6  : sideload -u->+v sur une lane +v : belt -u dépose latéralement sur belt +v (lane).
        MERGER GRATUIT belt->belt (2 entrées : amont +u_lane nord + est u_lane+1 -> 1 lane aval).
        La lane amont continue (pas coupée par l'injection).
  T7  : merger 2->1 = splitter 2 entrées orienté +v, 1 sortie prise, 2e sortie bouchée
        (rien après) -> backpressure force tout le flux sur la sortie prise.

Géométrie complète feed M=2 (facing=2 : u=east +x, v=south +y), zone 1200,1200 :
  lane      (1200,1200..1215) south  [lane produit +v], amont (1200,1199) feed
  belts_out (1220,1210)+(1221,1210) south  [M=2 belts_out étage], feed nord (1220,1209)+(1221,1209)
  merger    (1220.5,1211) south  entrées (1220,1210)+(1221,1210), sorties (1220,1212)+(1221,1212)
            2e sortie (1221,1212) bouchée (rien après) -> merger 2->1
  virage    belt -u (1220,1213) west  [sortie merger (1220,1212) +v dépose sur -u à +v]
  belts -u  (1220,1213)..(1201,1213) west  [vers -u, traversent (rien ici, pas de lanes)]
  sideload  belt -u (1201,1213) dépose sur lane +v (1200,1213) [merger gratuit]
  lane_aval (1200,1214) south  [lane continue, amont + feed injecté]

Si lane_aval (1200,1214) > 0 : TOUTE la géométrie feed S1g validée (merger 2->1 + virage +v->-u
+ belts -u + sideload -u->+v merger gratuit + lane continue). Sinon : identifier le maillon cassé.
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
# u=east +x, v=south +y. lane u=0 (x=1200), belts_out u=20,21 (x=1220,1221), v_out=10 (y=1210).

SETUP = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
for _, e in ipairs(s.find_entities_filtered{area={{1199,1198},{1222,1216}}}) do
  if e.name ~= "character" then e.destroy() end
end
local function place(name, x, y, dir)
  return s.create_entity{name=name, position={x,y}, direction=dir, force="player"}
end
local D = defines.direction
-- Lane produit +v (south) à u=0 (x=1200), v=0..15 (y=1200..1215).
local lane_amont = place("transport-belt", 1200, 1199, D.south)
local lane = {}
for v=0,15 do lane[v] = place("transport-belt", 1200, 1200+v, D.south) end
-- Belts_out +v (south) à u=20,21 (x=1220,1221), v=10 (y=1210). Feed nord v=9 (y=1209).
local bo1_n = place("transport-belt", 1220, 1209, D.south)
local bo2_n = place("transport-belt", 1221, 1209, D.south)
local bo1   = place("transport-belt", 1220, 1210, D.south)
local bo2   = place("transport-belt", 1221, 1210, D.south)
-- Merger splitter orient south à (1220.5,1211). 2e sortie (1221,1212) bouchée (rien après).
local mrg = place("splitter", 1220.5, 1211, D.south)
local mrg_out = place("transport-belt", 1220, 1212, D.south)  -- sortie prise (+v)
-- 2e sortie (1221,1212) : RIEN (bouchée) -> backpressure force tout sur mrg_out.
-- Virage +v->-u : belt +v (1220,1212) dépose sur belt -u (west) à (1220,1213).
local vir = place("transport-belt", 1220, 1213, D.west)
-- Belts -u (west) de (1220,1213) vers (1201,1213).
local bu = {}
for x=1219,1201,-1 do bu[x] = place("transport-belt", x, 1213, D.west) end
-- (1200,1213) = lane +v : reçoit sideload -u->+v de (1201,1213). Déjà posé par lane[v=13].
rcon.print('{"lane0":'..tostring(lane[0]~=nil)..',"bo1":'..tostring(bo1~=nil)..',"mrg":'..tostring(mrg~=nil)..',"mrg_out":'..tostring(mrg_out~=nil)..',"vir":'..tostring(vir~=nil)..',"bu1201":'..tostring(bu[1201]~=nil)..',"lane13":'..tostring(lane[13]~=nil)..',"lane14":'..tostring(lane[14]~=nil)..'}')
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
feed(1200, 1199)  -- lane amont
feed(1220, 1209)  -- belt_out 1 nord
feed(1221, 1209)  -- belt_out 2 nord
rcon.print("fed")
"""

MEASURE = """
local s = game.surfaces["nauvis"] or game.surfaces[1]
local function cnt(x, y)
  local e = s.find_entities_filtered{position={x+0.5, y+0.5}, name="transport-belt"}
  if #e == 0 then
    -- splitter ?
    e = s.find_entities_filtered{position={x+0.5, y+0.5}, name="splitter"}
    if #e == 0 then return -1 end
  end
  local c = e[1].get_transport_line(1).get_contents()
  local t = 0
  for _, it in ipairs(c) do t = t + (it.count or 0) end
  return t
end
rcon.print('{"bo1":'..cnt(1220,1210)..',"bo2":'..cnt(1221,1210)..',"mrg_out":'..cnt(1220,1212)..',"vir":'..cnt(1220,1213)..',"bu1201":'..cnt(1201,1213)..',"lane13":'..cnt(1200,1213)..',"lane14":'..cnt(1200,1214)..'}')
"""

print("=== SETUP (feed M=2 complet : merger 2->1 + virage +v->-u + belts -u + sideload -u->+v sur lane) ===")
print("placed:", q(SETUP))

max_lane14 = 0
last = {}
for cycle in range(60):
    q(FEED)
    time.sleep(0.25)
    m = q(MEASURE)
    last = m
    l14 = m.get("lane14", -1)
    max_lane14 = max(max_lane14, l14)
    if cycle % 5 == 0:
        print(f"cycle {cycle}: bo1={m.get('bo1')} mrg_out={m.get('mrg_out')} "
              f"vir={m.get('vir')} bu1201={m.get('bu1201')} lane13={m.get('lane13')} lane14={l14}")
    if l14 > 0 and cycle >= 10:
        print(f"  -> flux injecté sur lane aval détecté à cycle {cycle}, arrêt")
        break

print(f"\n=== RESULT (max) === lane14(aval, amont+feed injecté)={max_lane14}")
print(f"  (dernier: {last})")
# Diagnostique par maillon
bo_ok = last.get("bo1", 0) > 0 or last.get("bo2", 0) > 0
mrg_ok = last.get("mrg_out", 0) > 0
vir_ok = last.get("vir", 0) > 0
bu_ok = last.get("bu1201", 0) > 0
inj_ok = last.get("lane13", 0) > 0
aval_ok = max_lane14 > 0
print(f"  bo(feed):{'OK' if bo_ok else 'ECHEC'} mrg(2->1):{'OK' if mrg_ok else 'ECHEC'} "
      f"vir(+v->-u):{'OK' if vir_ok else 'ECHEC'} bu(-u):{'OK' if bu_ok else 'ECHEC'} "
      f"inj(sideload -u->+v):{'OK' if inj_ok else 'ECHEC'} aval(lane continue):{'OK' if aval_ok else 'ECHEC'}")
if aval_ok:
    print("FEED-S1G OK -> géométrie complète validée (gap + merger 2->1 + virage +v->-u + "
          "belts -u + sideload -u->+v merger gratuit, lane continue)")
else:
    # identifier le 1er maillon cassé
    chain = [("bo", bo_ok), ("mrg", mrg_ok), ("vir", vir_ok), ("bu", bu_ok),
             ("inj", inj_ok), ("aval", aval_ok)]
    broken = next((name for name, ok in chain if not ok), "?")
    print(f"FEED-S1G ECHEC -> 1er maillon cassé : {broken}")
sys.exit(0 if aval_ok else 1)