-- Perception : observations synchrones exposees via l'interface fl_tools (get_state).
-- Etat compact mais suffisant pour valider la boucle : tick, position/sante/inventaire
-- de l'avatar IA, tache en cours. Source de l'avatar : player.get_ai_entity() (dual).

local json = require("scripts.json")
local player_mod = require("scripts.player")
local task_manager = require("scripts.task_manager")

local M = {}

-- Inventaire compact {item_name = count} du main inventory de l'avatar IA.
-- Factorio 2.0 : get_contents() renvoie un tableau [{name,count,...}] ; on normalise
-- en dict {name -> count} (homogene avec GameState cote Python).
local function inventory_contents(inv)
  if not inv then return {} end
  local contents = inv.get_contents()
  if not contents then return {} end
  local out = {}
  for _, it in ipairs(contents) do
    if type(it) == "table" and it.name then
      out[it.name] = (out[it.name] or 0) + (it.count or 0)
    end
  end
  -- Fallback dict form ({name -> count}) si la forme tableau n'a rien donne.
  if next(out) == nil then
    for name, count in pairs(contents) do
      if type(name) == "string" and type(count) == "number" then
        out[name] = count
      end
    end
  end
  return out
end

-- Sante max de l'avatar : max_health vit sur l'entite (LuaEntity) en 2.0, pas sur le
-- prototype. pcall + fallback 100 (defensif : certaines entites n'exposent pas health).
local function max_health_of(char)
  local ok, v = pcall(function() return char.max_health end)
  if ok and v then return v end
  local ok2, v2 = pcall(function() return char.prototype.max_health end)
  if ok2 and v2 then return v2 end
  return 100
end

-- Etat complet, renvoye en JSON via rcon.print (lu cote Python).
function M.get_state()
  local char = player_mod.get_ai_entity()
  local tick = game.tick
  local state = {
    tick = tick,
    test_mode = player_mod.is_test_mode(),
    ready = char ~= nil,
    task = task_manager.status(),
  }
  if char then
    local pos = char.position
    -- walking_state est sur le joueur (prod) ; en test headless, pas de marche -> false.
    local walking = false
    local p = player_mod.get_ai_player()
    if p then
      local ok, w = pcall(function() return p.walking_state.walking end)
      if ok then walking = w end
    end
    state.character = {
      position = {x = pos.x, y = pos.y},
      health = char.health,
      max_health = max_health_of(char),
      walking = walking,
    }
    state.home_position = storage.fl.home_position
    state.inventory = inventory_contents(player_mod.get_ai_inventory())
    state.surface = char.surface.name
  end
  return json.encode(state)
end

return M