-- Operations : actions asynchrones exposees via l'interface fl_ops.
-- Chaque operation enfile une tache (construite par task_manager) et retourne
-- immediatement {ok, detail}. L'execution se fait sur on_tick. L'orchestrateur
-- Python interroge fl_ops.status / fl_tools.get_state pour la completion.
--
-- Une responsabilite : traduire un appel RCON en tache (validation leger + enqueue).
-- La physique (physique reelle en prod / simulee en test) vit dans task_manager.

local task_manager = require("scripts.task_manager")
local player = require("scripts.player")
local utils_entity = require("scripts.utils_entity")

local M = {}

-- Garde : presence d'un avatar IA (sinon echec immediat, lisible par l'orchestrateur).
local function require_entity()
  return player.get_ai_entity() ~= nil
end

-- ===== Deplacement =====

-- walk_to(x, y) : marche (pathfinding request_path) vers une position.
function M.walk_to(x, y)
  if not require_entity() then return false, "aucun avatar IA (setup ou connecter un joueur)" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  task_manager.queue(task_manager.new_walk_to(x, y))
  return true, "en file (pathfinding)"
end

-- walk_to_entity(entity_name, search_radius) : marche vers l'entite la plus proche
-- du nom/type donne, dans search_radius. Reachability-aware (essaye plusieurs candidats).
function M.walk_to_entity(entity_name, search_radius)
  if not require_entity() then return false, "aucun avatar IA (setup ou connecter un joueur)" end
  if not entity_name or entity_name == "" then return false, "entity_name requis" end
  search_radius = search_radius or 100
  task_manager.queue(task_manager.new_walk_to_entity(entity_name, search_radius))
  return true, "en file (pathfinding vers " .. tostring(entity_name) .. ")"
end

-- teleport_to(x, y) : deplacement instantane. Reserve au mode test (instantane = triche
-- en production). En prod, refuse.
function M.teleport_to(x, y)
  if not player.is_test_mode() then return false, "teleport interdit en production" end
  if not require_entity() then return false, "aucun avatar IA (setup)" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  task_manager.queue(task_manager.new_teleport(x, y))
  return true, "en file (teleport)"
end

-- ===== Minage =====

-- mine_entity(entity_name, count) : mine `count` entites du nom/type donne.
function M.mine_entity(entity_name, count)
  if not require_entity() then return false, "aucun avatar IA" end
  if not entity_name or entity_name == "" then return false, "entity_name requis" end
  count = count or 1
  if count < 1 then return false, "count >= 1 requis" end
  task_manager.queue(task_manager.new_mine_entity(entity_name, count))
  return true, string.format("en file (mine %s x%d)", entity_name, count)
end

-- ===== Placement =====

-- place_entity_at(entity_name, x, y, direction) : pose une entite a une position.
-- direction : string ('north'|'east'|...) convertie en int via utils_entity.
function M.place_entity_at(entity_name, x, y, direction)
  if not require_entity() then return false, "aucun avatar IA" end
  if not entity_name or entity_name == "" then return false, "entity_name requis" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  local dir = defines.direction.north
  if type(direction) == "string" then
    dir = utils_entity.direction_from_name(direction)
  elseif type(direction) == "number" then
    dir = direction
  end
  task_manager.queue(task_manager.new_place_entity_at(entity_name, x, y, dir))
  return true, string.format("en file (place %s at %.1f,%.1f)", entity_name, x, y)
end

-- ===== Transfert d'items =====

-- move_items(item_name, entity_name, max_count, to_entity) : deplace des items entre
-- l'inventaire IA et les entites du nom donne dans un rayon 32.
--   to_entity=true  -> joueur -> entite
--   to_entity=false -> entite -> joueur
function M.move_items(item_name, entity_name, max_count, to_entity)
  if not require_entity() then return false, "aucun avatar IA" end
  if not item_name or item_name == "" then return false, "item_name requis" end
  if not entity_name or entity_name == "" then return false, "entity_name requis" end
  task_manager.queue(task_manager.new_move_items(item_name, entity_name, max_count, to_entity))
  return true, string.format("en file (move %s <-> %s)", item_name, entity_name)
end

-- move_items_at(item_name, entity_name, x, y, max_count, to_entity) : idem mais entite
-- unique a la position (rayon 1.5), evite le split de charge.
function M.move_items_at(item_name, entity_name, x, y, max_count, to_entity)
  if not require_entity() then return false, "aucun avatar IA" end
  if not item_name or item_name == "" then return false, "item_name requis" end
  if not entity_name or entity_name == "" then return false, "entity_name requis" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  task_manager.queue(task_manager.new_move_items_at(item_name, entity_name, x, y, max_count, to_entity))
  return true, string.format("en file (move %s at %.1f,%.1f)", item_name, x, y)
end

-- ===== Attente =====

-- wait(ticks) : attend `ticks` ticks (reels).
function M.wait(ticks)
  if type(ticks) ~= "number" or ticks < 0 then return false, "ticks >= 0 requis" end
  task_manager.queue(task_manager.new_wait(math.floor(ticks)))
  return true, string.format("en file (wait %d)", math.floor(ticks))
end

-- ===== Craft =====

-- craft_item(item_name, count) : lance le craft de `count` objets (file de craft reelle
-- en prod, simulation en test).
function M.craft_item(item_name, count)
  if not require_entity() then return false, "aucun avatar IA" end
  if not item_name or item_name == "" then return false, "item_name requis" end
  count = count or 1
  if count < 1 then return false, "count >= 1 requis" end
  task_manager.queue(task_manager.new_craft_item(item_name, count))
  return true, string.format("en file (craft %s x%d)", item_name, count)
end

-- ===== Recherche =====

-- research_technology(technology_name) : lance la recherche d'une technologie.
function M.research_technology(technology_name)
  if not technology_name or technology_name == "" then return false, "technology_name requis" end
  task_manager.queue(task_manager.new_research(technology_name))
  return true, "en file (research " .. technology_name .. ")"
end

-- ===== Mode / setup =====

-- set_test_mode(v) : bascule le mode dual au runtime. true -> test headless (spawn un
-- character si absent), false -> production (joueur connecte requis).
function M.set_test_mode(v)
  storage.fl = storage.fl or {}
  storage.fl.test_mode = v and true or false
  task_manager.clear()
  if storage.fl.test_mode then
    local surface = game.surfaces.nauvis or game.surfaces[1]
    if surface and not player.get_headless() then
      player.create_headless(surface)
    end
  end
  log("[fl] set_test_mode=" .. tostring(storage.fl.test_mode))
  return true, storage.fl.test_mode and "mode test headless" or "mode production"
end

-- setup() : initialise l'avatar IA (idempotent). Test : cree le character headless.
-- Prod : donne le kit de depart au joueur connecte. Retourne ok, detail.
function M.setup()
  return player.setup()
end

-- reset_character() : detruit et recree le character headless avec le kit integral
-- (mode test uniquement). Rend les tests d'integration reproductibles sans relancer le
-- serveur. Refuse en production (ne touche jamais le character d'un joueur).
function M.reset_character()
  if not player.is_test_mode() then return false, "reset interdit en production" end
  task_manager.clear()
  return player.reset_headless()
end

-- ===== Etats =====

function M.status()
  return task_manager.status()
end

function M.cancel()
  task_manager.clear()
  return true, "file videe"
end

return M