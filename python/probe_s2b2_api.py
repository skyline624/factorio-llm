"""Probe API Factorio 2.0 : boiler, steam-engine, steam, water.
Géométrie (size, fluidbox.get_prototype positions), propriétés prototype.
"""
import sys
sys.path.insert(0, "D:/developpement/factorio-llm/python")
from core.rcon import get_rcon

PROBE = r"""
local s=game.surfaces['nauvis'] or game.surfaces[1]
for _,e in ipairs(s.find_entities_filtered{area={{__X__-2,__Y__-2},{__X__+2,__Y__+2}}}) do if e.name~='character' then e.destroy() end end
local e=s.create_entity{name='__NAME__', position={__X__,__Y__}, direction=__DIR__, force='player'}
if not e then rcon.print('create KO'); return end
local bb=e.bounding_box
local out='size='..math.ceil(bb.right_bottom.x-bb.left_top.x)..'x'..math.ceil(bb.right_bottom.y-bb.left_top.y)..' '
out=out..'pos=('..e.position.x..','..e.position.y..') '
local nfb=#e.fluidbox
out=out..'#fluidbox='..nfb..'; '
for i=1,nfb do
  local gp=e.fluidbox.get_prototype(i)
  out=out..'b'..i..':'..tostring(gp.production_type)..';'
  for _,pc in ipairs(gp.pipe_connections) do
    out=out..'{flow='..tostring(pc.flow_direction)..' pos={'
    for _,pos in ipairs(pc.positions) do out=out..'('..pos.x..','..pos.y..')' end
    out=out..'}} '
  end
end
local p=e.prototype
local function tryf(field) local ok,v=pcall(function() return p[field] end) return ok and tostring(v) or '-' end
out=out..'proto: target_temp='..tryf('target_temperature')..' max_temp='..tryf('maximum_temperature')
out=out..' max_energy_prod='..tryf('max_energy_production')..' max_energy_use='..tryf('max_energy_usage')
out=out..' effectivity='..tryf('effectivity')..' type='..tryf('type')
e.destroy()
rcon.print(out)
"""

def probe(rcon, name, x, y, dirn):
    lua = (PROBE.replace("__NAME__", name).replace("__X__", str(x))
           .replace("__Y__", str(y)).replace("__DIR__", str(dirn)))
    return rcon.query_lua(lua)

def probe_fluid(rcon, name):
    lua = "local f=game.fluid_prototypes['" + name + "'] "
    lua += "if not f then rcon.print('fluid " + name + " KO'); return end "
    lua += "rcon.print('fluid " + name + ": heat_capacity='..tostring(f.heat_capacity)"
    lua += "..' default_temp='..tostring(f.default_temperature)"
    lua += "..' max_temp='..tostring(f.maximum_temperature)"
    lua += "..' gas_temp='..tostring(f.gas_temperature))"
    return rcon.query_lua(lua)

def main():
    r = get_rcon()
    print("BOILER north:", probe(r, "boiler", 70, 70, 0))
    print("BOILER east :", probe(r, "boiler", 80, 70, 2))
    print("STEAM-ENG north:", probe(r, "steam-engine", 90, 70, 0))
    print("STEAM-ENG east :", probe(r, "steam-engine", 100, 70, 2))
    print(probe_fluid(r, "steam"))
    print(probe_fluid(r, "water"))
    r.close()

if __name__ == "__main__":
    main()