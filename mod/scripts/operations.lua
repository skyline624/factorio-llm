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

-- Conversion commune des directions : string ('north'|'east'|...) ou int 16-dir.
local function to_direction(direction)
  if type(direction) == "string" then return utils_entity.direction_from_name(direction) end
  if type(direction) == "number" then return direction end
  return defines.direction.north
end

-- place_entity_at(entity_name, x, y, direction, opts) : pose une entite a une position.
-- direction : string ('north'|'east'|...) convertie en int via utils_entity.
-- opts (optionnel, E2) : table {recipe, ug_type, priority_in, priority_out, modules,
-- fuel, fuel_count}. Les options sont appliquees APRES la pose et leurs echecs sont
-- consignes dans le detail sans annuler la pose (une entite mal reglee se corrige).
function M.place_entity_at(entity_name, x, y, direction, opts)
  if not require_entity() then return false, "aucun avatar IA" end
  if not entity_name or entity_name == "" then return false, "entity_name requis" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  if opts ~= nil and type(opts) ~= "table" then return false, "opts doit etre une table" end
  task_manager.queue(task_manager.new_place_entity_at(entity_name, x, y, to_direction(direction), opts))
  return true, string.format("en file (place %s at %.1f,%.1f)", entity_name, x, y)
end

-- ===== Correction d'une entite deja posee (E2) =====
-- Ces trois operations ferment la boucle « construire -> constater -> corriger ».
-- Sans elles le mod ne sait que construire, et toute erreur de pose est definitive.

-- remove_entity_at(x, y, entity_name) : retire l'entite a la position (rayon 1.5).
-- entity_name optionnel : sans lui, cible l'entite de notre force la plus proche,
-- a defaut une entite minable (arbre / rocher) pour degager un emplacement.
function M.remove_entity_at(x, y, entity_name)
  if not require_entity() then return false, "aucun avatar IA" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  task_manager.queue(task_manager.new_remove_entity_at(x, y, entity_name))
  return true, string.format("en file (remove at %.1f,%.1f)", x, y)
end

-- rotate_entity_at(x, y, direction, entity_name) : oriente une entite deja posee.
-- La direction est ABSOLUE (pas un cran de rotation).
function M.rotate_entity_at(x, y, direction, entity_name)
  if not require_entity() then return false, "aucun avatar IA" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  if direction == nil then return false, "direction requise" end
  task_manager.queue(task_manager.new_rotate_entity_at(x, y, to_direction(direction), entity_name))
  return true, string.format("en file (rotate at %.1f,%.1f)", x, y)
end

-- set_recipe_at(x, y, recipe, entity_name) : regle la recette d'une machine posee.
-- recipe = nil ou "" efface la recette. Verrou de toute automatisation au-dela des fours.
function M.set_recipe_at(x, y, recipe, entity_name)
  if not require_entity() then return false, "aucun avatar IA" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  if recipe == "" then recipe = nil end
  if recipe ~= nil and type(recipe) ~= "string" then return false, "recipe doit etre une chaine" end
  task_manager.queue(task_manager.new_set_recipe_at(x, y, recipe, entity_name))
  return true, string.format("en file (recipe %s at %.1f,%.1f)", tostring(recipe), x, y)
end

-- empty_output_at(x, y, entity_name) : vide la SORTIE d'une machine vers l'inventaire IA.
-- Une sortie pleine arrete une machine aussi surement qu'un reservoir vide. Contrairement
-- a move_items, le produit n'a pas a etre NOMME -- ce qu'on ignore justement quand on
-- decouvre une machine a l'arret.
function M.empty_output_at(x, y, entity_name)
  if not require_entity() then return false, "aucun avatar IA" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  task_manager.queue(task_manager.new_empty_output_at(x, y, entity_name))
  return true, string.format("en file (empty at %.1f,%.1f)", x, y)
end

-- enable_entity_at(x, y, entity_name) : reactive une entite desactivee. `active=false`
-- arrete une machine sans qu'aucun ingredient ne manque ; aucune autre reparation n'y
-- changerait rien.
function M.enable_entity_at(x, y, entity_name)
  if not require_entity() then return false, "aucun avatar IA" end
  if type(x) ~= "number" or type(y) ~= "number" then return false, "x,y numeriques requis" end
  task_manager.queue(task_manager.new_enable_entity_at(x, y, entity_name))
  return true, string.format("en file (enable at %.1f,%.1f)", x, y)
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

-- spawn_test_resources(surface) : en mode test, spawn un patch crude-oil + un bassin
-- d'eau + un patch coal près de l'origin pour valider la chaîne fluide (pumpjack +
-- offshore-pump) et la chaîne plastic-bar (coal solide + petroleum-gas fluide).
-- Idempotent via storage.fl.test_resources_spawned. No-op en production (les
-- ressources sont générées par la map). Retourne (ok, detail).
local function spawn_test_resources(surface)
  if not (storage.fl and storage.fl.test_mode) then return false, "test_mode requis" end
  if storage.fl.test_resources_spawned then return true, "ressources test deja spawnees" end
  -- Patch crude-oil : grille 3x3 de resource-entities autour de (8, 8). create_entity
  -- génère le gisement (et la tuile resource) ; amount contrôle initial_amount
  -- (quantité brute lue par scan_patch.total_amount).
  local ok_count = 0
  for dx = 0, 2 do
    for dy = 0, 2 do
      local ok, ent = pcall(function()
        return surface.create_entity{name = "crude-oil", position = {8 + dx, 8 + dy}, amount = 1000000}
      end)
      if ok and ent then ok_count = ok_count + 1 end
    end
  end
  -- Patch coal : grille 4x4 de resource-entities autour de (20, 8). Nécessaire pour la
  -- chaîne plastic-bar (coal solide inserter + petroleum-gas fluide -> plastic-bar).
  local coal_count = 0
  for dx = 0, 3 do
    for dy = 0, 3 do
      local ok, ent = pcall(function()
        return surface.create_entity{name = "coal", position = {20 + dx, 8 + dy}, amount = 500000}
      end)
      if ok and ent then coal_count = coal_count + 1 end
    end
  end
  -- Bassin d'eau : rectangle 3x2 de tiles water à (14, 8)..(16, 9). set_tiles génère
  -- les tuiles water (et le chunk si non généré). autocorrect=true corrige les tuiles
  -- voisines pour éviter les erreurs de collision.
  local water_tiles = {}
  for tx = 14, 16 do
    for ty = 8, 9 do
      table.insert(water_tiles, {name = "water", position = {tx, ty}})
    end
  end
  pcall(function() surface.set_tiles(water_tiles, true) end)
  storage.fl.test_resources_spawned = true
  return true, string.format("crude-oil %d + coal %d + bassin eau 3x2 spawnes", ok_count, coal_count)
end

-- setup() : initialise l'avatar IA (idempotent). Test : cree le character headless.
-- Prod : donne le kit de depart au joueur connecte. Retourne ok, detail.
-- S2a : en mode test, spawn en plus un patch crude-oil + bassin eau près de l'origin
-- (validation live de la chaîne fluide). Idempotent. No-op en production.
function M.setup()
  local ok, detail = player.setup()
  if not ok then return ok, detail end
  if player.is_test_mode() then
    local surface = game.surfaces.nauvis or game.surfaces[1]
    if surface then
      local _, rdetail = spawn_test_resources(surface)
      return true, detail .. " ; " .. rdetail
    end
  end
  return ok, detail
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