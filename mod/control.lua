-- control.lua : point d'entree du mod factorio-llm.
-- Expose deux interfaces RCON (appelees depuis l'orchestrateur Python via
-- /silent-command remote.call(...)):
--   fl_tools : observation synchrone (get_state, get_tick, scan_area, scan_factory,
--              find_nearest, describe, get_recipe, production_stats)
--   fl_ops   : action asynchrone (walk_to, walk_to_entity, teleport_to, mine_entity,
--              place_entity_at, move_items, move_items_at, wait, craft_item,
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
  production_stats = function() safe(function() return tools.production_stats() end) end,
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
  place_entity_at = function(entity_name, x, y, direction)
    safe(function() local ok, d = operations.place_entity_at(entity_name, x, y, direction) reply_ack(ok, d) end)
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