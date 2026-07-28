-- player.lua : gestion de l'avatar IA (dual).
-- Production : l'IA pilote le character du joueur connecte (game.connected_players[1]).
--   -> donne le kit de depart a ce character (idempotent) sur setup / on_player_joined.
-- Test headless : aucun joueur ; on spawn un character sans associated_player.
--   -> character.create_headless(surface) + kit de depart.
--
-- La resolution de "quelle entite piloter" (get_ai_entity/get_ai_player) vit dans
-- control.lua (couche de controle) ; ce module fournit les briques : kit, spawn
-- headless, setup idempotent. Responsabilite unique (SOLID).

local M = {}

-- Kit de depart du personnage IA (cf. ai-player-v3 character.lua).
-- S2a : ajoute les entites fluides (pipe/pumpjack/oil-refinery/chemical-plant/
-- offshore-pump/boiler/steam-engine/pump/storage-tank/pipe-to-ground) pour valider
-- la chaîne plastic-bar en live. Idempotent via storage.fl.kit_given (reset_character
-- rearme le flag -> le nouveau character repart avec le kit integral).
local STARTING_ITEMS = {
  {name = "wood", count = 50},
  {name = "coal", count = 100},
  {name = "stone", count = 50},
  {name = "iron-plate", count = 50},
  {name = "copper-plate", count = 30},
  {name = "burner-mining-drill", count = 4},
  {name = "stone-furnace", count = 4},
  {name = "burner-inserter", count = 10},
  {name = "transport-belt", count = 50},
  {name = "small-electric-pole", count = 20},
  -- E2 : de quoi valider les options de pose (recette, sens underground, priorite
  -- splitter) et amorcer l'automatisation reelle — le kit n'avait aucun assembleur,
  -- donc aucune machine a recette reglable hors chaine fluide.
  {name = "assembling-machine-1", count = 4},
  {name = "assembling-machine-2", count = 2},
  {name = "underground-belt", count = 10},
  {name = "splitter", count = 4},
  {name = "lab", count = 2},
  {name = "pipe", count = 100},
  {name = "iron-chest", count = 4},
  -- S2a : fluides (socle + chaîne plastic-bar).
  {name = "pipe-to-ground", count = 20},
  {name = "pump", count = 4},
  {name = "offshore-pump", count = 2},
  {name = "pumpjack", count = 2},
  {name = "oil-refinery", count = 1},
  {name = "chemical-plant", count = 2},
  {name = "boiler", count = 2},
  {name = "steam-engine", count = 2},
  {name = "storage-tank", count = 4},
  -- S3b : beacons + modules + electric-furnace (validateur live S3b/S3c).
  {name = "beacon", count = 20},
  {name = "speed-module-3", count = 50},
  {name = "productivity-module-3", count = 50},
  {name = "speed-module-2", count = 20},
  {name = "electric-furnace", count = 10},
}

-- Donne le kit de depart a un character (idempotent : une seule fois via storage).
local function give_kit(character)
  if storage.fl.kit_given then return end
  for _, item in ipairs(STARTING_ITEMS) do
    character.insert({name = item.name, count = item.count})
  end
  storage.fl.kit_given = true
  log("[fl] kit de depart donne")
end

-- Cree (ou recree) un character headless sans associated_player (mode test).
function M.create_headless(surface)
  local force = game.forces.player
  if storage.fl.character and storage.fl.character.valid then
    storage.fl.character.destroy()
  end
  local pos = surface.find_non_colliding_position("character", {0, 0}, 32, 0.5) or {0, 0}
  local char = surface.create_entity({name = "character", position = pos, force = force})
  if not char then return nil, "impossible de creer le character" end
  give_kit(char)
  storage.fl.character = char
  storage.fl.home_position = pos
  return char
end

-- Reset du character headless (mode test uniquement) : detruit l'ancien, rearme le
-- flag kit_given, et recree un character neuf avec le kit integral. Sert a rendre les
-- tests d'integration reproductibles sans relancer le serveur. Refuse en production
-- (ne jamais toucher le character d'un joueur connecte). Retourne (ok, detail).
function M.reset_headless()
  if not (storage.fl and storage.fl.test_mode) then
    return false, "reset interdit en production"
  end
  local surface = game.surfaces.nauvis or game.surfaces[1]
  if not surface then return false, "aucune surface" end
  -- Rearme le kit : le nouveau character repartira avec l'inventaire de depart.
  storage.fl.kit_given = false
  local char = M.create_headless(surface)
  if not char then return false, "echec creation character headless" end
  log("[fl] character headless reset (kit rearme)")
  return true, "character headless reset"
end

-- Character headless courant (mode test), ou nil.
function M.get_headless()
  local c = storage.fl and storage.fl.character
  if c and c.valid then return c end
  return nil
end

-- setup() idempotent : branche selon le mode.
--   test : cree le character headless si absent.
--   prod : donne le kit au character du joueur connecte (si present), ancre home.
-- Retourne (ok, detail).
function M.setup()
  storage.fl = storage.fl or {}
  if storage.fl.test_mode then
    local c = M.get_headless()
    if not c then
      local surface = game.surfaces.nauvis or game.surfaces[1]
      if not surface then return false, "aucune surface" end
      c = M.create_headless(surface)
      if not c then return false, "echec creation character headless" end
    end
    return true, "character headless pret"
  else
    local player = game.connected_players[1]
    if not player or not player.character then return false, "aucun joueur connecte" end
    give_kit(player.character)
    if not storage.fl.home_position then
      storage.fl.home_position = {x = player.character.position.x, y = player.character.position.y}
    end
    return true, "joueur IA pret"
  end
end

-- Donne le kit au character d'un joueur qui vient de se connecter (on_player_joined).
function M.on_player_joined(player)
  if not player or not player.character then return end
  -- En production, le premier joueur connecte est l'IA.
  give_kit(player.character)
  if not storage.fl.home_position then
    storage.fl.home_position = {x = player.character.position.x, y = player.character.position.y}
  end
end

-- Home position ancrée (position de reference pour return_home etc.).
function M.home_position()
  return storage.fl and storage.fl.home_position
end

-- ===== Resolution de l'entite pilotee (dual) =====

-- Joueur IA (production uniquement) ou nil (test headless / aucun connecte).
function M.get_ai_player()
  if storage.fl and storage.fl.test_mode then return nil end
  return game.connected_players[1]
end

-- Character pilote : character headless (test) ou character du joueur connecte (prod).
-- Retourne nil si absent (handlers attendent ; l'orchestrateur doit setup/connecter).
function M.get_ai_entity()
  if storage.fl and storage.fl.test_mode then
    return M.get_headless()
  end
  local player = game.connected_players[1]
  if player and player.character and player.character.valid then
    return player.character
  end
  return nil
end

-- Mode courant (bool).
function M.is_test_mode()
  return storage.fl and storage.fl.test_mode == true
end

-- Inventaire principal de l'IA (player.get_main_inventory en prod,
-- character_main en test). nil si aucun avatar.
function M.get_ai_inventory()
  local e = M.get_ai_entity()
  if not e then return nil end
  if M.is_test_mode() then
    return e.get_inventory(defines.inventory.character_main)
  end
  local p = M.get_ai_player()
  if p then
    local inv = p.get_main_inventory()
    if inv then return inv end
  end
  return e.get_inventory(defines.inventory.character_main)
end

-- Force de l'IA (force-level, marche dans les deux modes).
function M.get_ai_force()
  return game.forces.player
end

return M