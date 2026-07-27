"""Probe dédié : topologie réelle des fluidboxes oil-refinery en Factorio 2.0.

Objectif : déterminer quelle tuile du périmètre connecte quelle box output
(heavy/light/petroleum) pour corriger le hardcode GEOMETRY_FIXTURE K7.

Méthode : poser oil-refinery, remplir les 3 outputs (b3=heavy, b4=light, b5=petroleum),
poser des pipes sur les tuiles du périmètre, laisser le serveur ticker (sleep),
lire le fluide propagé dans chaque pipe.
"""
import sys, time
sys.path.insert(0, "D:/developpement/factorio-llm/python")
from core.rcon import get_rcon

SETUP = r"""
local s=game.surfaces['nauvis'] or game.surfaces[1]
for _,e in ipairs(s.find_entities_filtered{area={{40,40},{60,60}}}) do if e.name~='character' then e.destroy() end end
local e=s.create_entity{name='oil-refinery', position={50,50}, direction=0, force='player'}
if not e then rcon.print('create KO'); return end
e.fluidbox[3]={name='heavy-oil',amount=1000}
e.fluidbox[4]={name='light-oil',amount=1000}
e.fluidbox[5]={name='petroleum-gas',amount=1000}
-- pipes a distance 3 (adjacents aux ports distance 2) : 12 positions perimetre
local ps={{47,50},{53,50},{50,47},{50,53},{47,48},{47,52},{53,48},{53,52},{48,47},{52,47},{48,53},{52,53}}
for _,p in ipairs(ps) do s.create_entity{name='pipe', position={p[1],p[2]}, force='player'} end
rcon.print('setup done')
"""

READ = r"""
local s=game.surfaces['nauvis'] or game.surfaces[1]
local out=''
local ps={{47,50},{53,50},{50,47},{50,53},{47,48},{47,52},{53,48},{53,52},{48,47},{52,47},{48,53},{52,53}}
for _,p in ipairs(ps) do
  local e=s.find_entities_filtered{name='pipe', position={p[1],p[2]}}[1]
  if e then
    local f=e.fluidbox[1]
    local nm='(vide)'
    if f then nm=tostring(f.name)..':'..tostring(math.floor(f.amount)) end
    out=out..'('..p[1]..','..p[2]..')='..nm..'; '
  end
end
rcon.print(out)
"""

CLEAN = r"""
local s=game.surfaces['nauvis'] or game.surfaces[1]
for _,e in ipairs(s.find_entities_filtered{area={{40,40},{60,60}}}) do if e.name~='character' then e.destroy() end end
rcon.print('clean')
"""

def main():
    r = get_rcon()
    print("setup:", r.query_lua(SETUP))
    time.sleep(3)
    print("read:", r.query_lua(READ))
    print("clean:", r.query_lua(CLEAN))
    r.close()

if __name__ == "__main__":
    main()