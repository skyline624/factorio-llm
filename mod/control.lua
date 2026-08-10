-- control.lua : point d'entree du mod factorio-llm.
-- Expose deux interfaces RCON (appelees depuis l'orchestrateur Python via
-- /silent-command remote.call(...)):
--   fl_tools : observation synchrone (get_state, get_tick, scan_area, scan_factory,
--              find_nearest, describe, get_recipe, production_stats)
--   fl_ops   : action asynchrone (walk_to, walk_to_entity, teleport_to, mine_entity,
--              place_entity_at, remove_entity_at, rotate_entity_at, set_recipe_at,
--              move_items, move_items_at, wait, craft_item,
--              research_technology, set_test_mode, setup, status, cancel)
-- Les operations ne font qu'enfiler une tache ; l'execution se fait sur on_tick.
--
-- Mode dual : fl-test-mode (settings.lua) + toggle runtime fl_ops.set_test_mode.
--   production = joueur connecte, physique reelle ; test = character headless, simule.
-- player.lua detient la resolution de l'entite pilotee (get_ai_entity/get_ai_player).

local perception = require("scripts.perception")
local operations = require("scripts.operations")
local task_manager = require("scripts.task_manager")
local player_mod = require("scripts.player")
local tools = require("scripts.tools")
local json = require("scripts.json")

-- Helper : repond au client RCON si on est dans un contexte console.
local function reply(s)
  if rcon then rcon.print(s) end
end

-- Repond un petit ack JSON {ok, detail}.
local function reply_ack(ok, detail)
  reply(json.encode({ok = ok, detail = detail}))
end

-- Enveloppe pcall : renvoie le resultat de fn(...) en JSON, ou une erreur JSON propre
-- si fn leve. Cote Python on parse toujours du JSON.
local function safe(fn, ...)
  local ok, result = pcall(fn, ...)
  if ok then
    if type(result) == "string" then
      reply(result)
    elseif result == nil then
      -- rien a repondre (ack deja emis par fn)
    else
      reply(json.encode(result))
    end
  else
    reply(json.encode({ok = false, error = tostring(result)}))
  end
end

-- ===== Interfaces RCON =====

remote.add_interface("fl_tools", {
  get_state = function() safe(function() return perception.get_state() end) end,
  get_tick = function() safe(function() return json.encode({tick = game.tick}) end) end,
  scan_area = function(radius) safe(function() return tools.scan_area(radius) end) end,
  scan_factory = function() safe(function() return tools.scan_factory() end) end,
  find_nearest = function(name) safe(function() return tools.find_nearest(name) end) end,
  describe = function(name) safe(function() return tools.describe(name) end) end,
  get_recipe = function(item) safe(function() return tools.get_recipe(item) end) end,
  get_technologies = function(pretes)
    safe(function() return tools.get_technologies(pretes ~= false) end)
  end,
  production_stats = function() safe(function() return tools.production_stats() end) end,
  -- Ce que le joueur a tape dans le chat depuis la derniere lecture. Vide la file.
  read_messages = function() safe(function() return tools.read_messages() end) end,
  -- Deposer un message SANS passer par le clavier. Deux usages reels : prouver le
  -- canal de bout en bout sans dependre d'un humain qui tape (`require` depuis la
  -- console RCON rend une COPIE du module, pas celui du mod -- la file deposee n'est
  -- alors pas celle que lit l'interface, et le test passe a cote), et permettre au
  -- lanceur d'annoncer la fin du budget.
  push_message = function(auteur, texte)
    safe(function() tools.push_message(auteur, texte) return json.encode({ok = true}) end)
  end,
  -- Validation LayoutPlanner (S0b) : synchrones, non destructives (sauf measure_entity).
  can_place_check = function(name, x, y, direction)
    safe(function() return tools.can_place_check(name, x, y, direction) end)
  end,
  scan_patch = function(resource, radius)
    safe(function() return tools.scan_patch(resource, radius) end)
  end,
  -- E16 : les gisements SEPARES, pour pouvoir en choisir un plutot que subir le plus proche.
  scan_patches = function(resource, radius, max_patches)
    safe(function() return tools.scan_patches(resource, radius, max_patches) end)
  end,
  -- S2a : bord d'un plan d'eau (tiles d'eau adjacents à terre) pour offshore-pump.
  scan_water_edge = function(radius)
    safe(function() return tools.scan_water_edge(radius) end)
  end,
  -- S4a : observation terrain (obstacles organiques, tuiles bbox, tuile ponctuelle).
  -- Non destructif. Donne au Python la visibilité terrain manquante pour le replan S4b.
  scan_obstacles = function(radius)
    safe(function() return tools.scan_obstacles(radius) end)
  end,
  scan_tiles_bbox = function(x1, y1, x2, y2)
    safe(function() return tools.scan_tiles_bbox(x1, y1, x2, y2) end)
  end,
  get_tile = function(x, y)
    safe(function() return tools.get_tile(x, y) end)
  end,
  -- E13 : lire ce qui est pose a une position PRECISE, loin du personnage.
  -- Sans elle, verifier une pose obligeait a y marcher (scan_area suit le perso).
  inspect_at = function(x, y, radius)
    safe(function() return tools.inspect_at(x, y, radius) end)
  end,
  -- J5 : menace (nids, unites, pollution). La pollution est le declencheur des
  -- vagues : sans elle, des nids proches ne justifient pas encore de fortifier.
  scan_threats = function(x, y, radius)
    safe(function() return tools.scan_threats(x, y, radius) end)
  end,
  -- E3 : etat electrique (reseau, charge, statut). Sans lui on sait poser une
  -- centrale mais pas verifier qu'elle alimente.
  get_power_state = function(x, y, radius)
    safe(function() return tools.get_power_state(x, y, radius) end)
  end,
  -- S4d : generation synchrone de chunks autour d'une position (resout out-of-map).
  -- request_to_generate_chunks + force_generate_chunk_requests. Non destructif.
  generate_terrain = function(x, y, radius)
    safe(function() return tools.generate_terrain(x, y, radius) end)
  end,
  measure_entity = function(name, x, y, direction)
    safe(function() return tools.measure_entity(name, x, y, direction) end)
  end,
})

remote.add_interface("fl_ops", {
  -- Deplacement
  walk_to = function(x, y)
    safe(function() local ok, d = operations.walk_to(x, y) reply_ack(ok, d) end)
  end,
  walk_to_entity = function(entity_name, search_radius)
    safe(function() local ok, d = operations.walk_to_entity(entity_name, search_radius) reply_ack(ok, d) end)
  end,
  teleport_to = function(x, y)
    safe(function() local ok, d = operations.teleport_to(x, y) reply_ack(ok, d) end)
  end,
  -- Actions
  mine_entity = function(entity_name, count)
    safe(function() local ok, d = operations.mine_entity(entity_name, count) reply_ack(ok, d) end)
  end,
  place_entity_at = function(entity_name, x, y, direction, opts)
    safe(function() local ok, d = operations.place_entity_at(entity_name, x, y, direction, opts) reply_ack(ok, d) end)
  end,
  -- E2 : agir sur une entite deja posee (corriger au lieu de subir).
  remove_entity_at = function(x, y, entity_name)
    safe(function() local ok, d = operations.remove_entity_at(x, y, entity_name) reply_ack(ok, d) end)
  end,
  rotate_entity_at = function(x, y, direction, entity_name)
    safe(function() local ok, d = operations.rotate_entity_at(x, y, direction, entity_name) reply_ack(ok, d) end)
  end,
  set_recipe_at = function(x, y, recipe, entity_name)
    safe(function() local ok, d = operations.set_recipe_at(x, y, recipe, entity_name) reply_ack(ok, d) end)
  end,
  -- E14 : reparer deux pannes que le diagnostic savait nommer sans savoir traiter --
  -- une sortie saturee et une machine desactivee.
  empty_output_at = function(x, y, entity_name)
    safe(function() local ok, d = operations.empty_output_at(x, y, entity_name) reply_ack(ok, d) end)
  end,
  enable_entity_at = function(x, y, entity_name)
    safe(function() local ok, d = operations.enable_entity_at(x, y, entity_name) reply_ack(ok, d) end)
  end,
  move_items = function(item_name, entity_name, max_count, to_entity)
    safe(function() local ok, d = operations.move_items(item_name, entity_name, max_count, to_entity) reply_ack(ok, d) end)
  end,
  move_items_at = function(item_name, entity_name, x, y, max_count, to_entity)
    safe(function() local ok, d = operations.move_items_at(item_name, entity_name, x, y, max_count, to_entity) reply_ack(ok, d) end)
  end,
  wait = function(ticks)
    safe(function() local ok, d = operations.wait(ticks) reply_ack(ok, d) end)
  end,
  craft_item = function(item_name, count)
    safe(function() local ok, d = operations.craft_item(item_name, count) reply_ack(ok, d) end)
  end,
  research_technology = function(technology_name)
    safe(function() local ok, d = operations.research_technology(technology_name) reply_ack(ok, d) end)
  end,
  -- Mode / setup
  set_test_mode = function(v)
    safe(function() local ok, d = operations.set_test_mode(v) reply_ack(ok, d) end)
  end,
  setup = function()
    safe(function() local ok, d = operations.setup() reply_ack(ok, d) end)
  end,
  reset_character = function()
    safe(function() local ok, d = operations.reset_character() reply_ack(ok, d) end)
  end,
  -- Etats
  status = function() safe(function() return json.encode(operations.status()) end) end,
  cancel = function()
    safe(function() local ok, d = operations.cancel() reply_ack(ok, d) end)
  end,
})

-- ===== Initialisation =====

local function init_storage()
  storage.fl = storage.fl or {}
  -- Mode test depuis le setting (runtime-global). Defaut false (production).
  if storage.fl.test_mode == nil then
    storage.fl.test_mode = settings.global["fl-test-mode"] and settings.global["fl-test-mode"].value or false
  end
  task_manager.init()
end

script.on_init(function()
  init_storage()
  -- En mode test, on cree le character headless des l'init. En production, on attend
  -- on_player_joined (ou setup) pour donner le kit au joueur connecte.
  if storage.fl.test_mode then
    player_mod.setup()
  end
end)

script.on_configuration_changed(function()
  init_storage()
  if storage.fl.test_mode and not player_mod.get_headless() then
    player_mod.setup()
  end
end)

-- ===== Boucle d'execution des taches =====
-- on_tick a chaque tick pour la fluidite du walk (les sauts et la marche ont besoin de
-- resolution temporelle). Le setting fl-tick-interval cadencera la decision cote
-- orchestrateur Python, pas l'execution mod.

script.on_event(defines.events.on_tick, function(event)
  task_manager.tick()
end)

-- Reception asynchrone du chemin calcule par le pathfinder de Factorio.
script.on_event(defines.events.on_script_path_request_finished, function(event)
  task_manager.on_path_finished(event)
end)

-- ===== Events de completion physique (production) =====
-- Mining : decremente le compte restant -> tache terminee quand count <= 0.
script.on_event(defines.events.on_player_mined_entity, function(event)
  task_manager.on_player_mined(event)
end)
-- Craft : compte les crafts termines -> tache terminee quand crafted >= count.
script.on_event(defines.events.on_player_crafted_item, function(event)
  task_manager.on_player_crafted(event)
end)
-- Joueur connecte : donne le kit de depart (production). Le premier joueur connecte = l'IA.
script.on_event(defines.events.on_player_joined_game, function(event)
  if storage.fl.test_mode then return end
  local player = game.get_player(event.player_index)
  if player then player_mod.on_player_joined(player) end
end)

-- ===== Le joueur parle a l'agent =====
-- `on_console_chat` porte AUSSI les messages du serveur et des commandes ; on ne retient
-- que ceux qui viennent d'un joueur, sans quoi l'agent lirait ses propres notifications.
script.on_event(defines.events.on_console_chat, function(event)
  if not event.player_index then return end
  local player = game.get_player(event.player_index)
  if not player or not event.message or event.message == "" then return end
  tools.push_message(player.name, event.message)
end)
