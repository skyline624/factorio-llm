from core.rcon import get_rcon

r = get_rcon()

# 1) stone-furnace : inventaires + can_insert (cause probable des "moved 0")
lua_furnace = r"""
local e = game.connected_players[1].character
local s = e.surface
local f = s.find_entities_filtered{name="stone-furnace", radius=10, position=e.position}
local out = {nfound=#f}
if #f > 0 then
  local ent = f[1]
  local ok, n = pcall(function() return ent.get_max_inventory_index() end)
  out.max_index = (ok and n) or ("ERR:"..tostring(n))
  local fi = ent.get_inventory(defines.inventory.fuel)
  out.fuel_valid = fi and fi.valid
  out.fuel_can_coal = fi and fi.can_insert({name="coal", count=1})
  local si = ent.get_inventory(defines.inventory.furnace_source)
  out.src_valid = si and si.valid
  out.src_can_ironore = si and si.can_insert({name="iron-ore", count=1})
  -- inventaire joueur
  local pi = game.connected_players[1].get_main_inventory()
  out.player_coal = pi and pi.get_item_count("coal")
  out.player_ironore = pi and pi.get_item_count("iron-ore")
end
rcon.print(serpent.block(out))
"""
print("=== stone-furnace inventaires ===")
print(r.query_lua(lua_furnace))

# 2) research : add_research + current_research (avec labs ?)
lua_research = r"""
local force = game.forces.player
local out = {}
out.has_labs = #game.surfaces.nauvis.find_entities_filtered{name="lab", force=force} > 0
local tech = force.technologies["automation"]
out.tech_exists = tech ~= nil
out.tech_enabled = tech and tech.enabled
out.tech_researched = tech and tech.researched
out.cur_before = force.current_research and force.current_research.name or "nil"
force.add_research("automation")
out.cur_after = force.current_research and force.current_research.name or "nil"
rcon.print(serpent.block(out))
"""
print("\n=== research automation ===")
print(r.query_lua(lua_research))

r.close()