-- tools.lua : observations synchrones exposees via l'interface fl_tools (JSON via rcon).
-- Portage des blocs 2 d'airi-factorio (packages/autorio/src/tools.ts) : scan_area,
-- scan_factory, find_nearest, describe, get_recipe, production_stats.
--
-- Source de l'avatar : player.get_ai_entity() (joueur connecte en prod / character
-- headless en test). Une seule source de verite (DRY) ; helpers nom/type/statut/direction/
-- recipe/drill partages dans utils_entity. Sortie JSON homogene cote Python (json.encode,
-- pas serpent.block). Bornes partout (max 200 entites pour scan_area, 100 pour scan_factory,
-- rayon 400 pour find_nearest) pour ne pas exploser le paquet RCON.

local json = require("scripts.json")
local player_mod = require("scripts.player")
local utils_entity = require("scripts.utils_entity")

local M = {}

-- Arrondit a 1 decimale (pour les coordonnees JSON).
local function r1(v) return math.floor(v * 10) / 10 end

-- Types de producteurs pour scan_factory (cf. airi producer_types).
local PRODUCER_TYPES = {
  "mining-drill", "furnace", "assembling-machine", "lab", "boiler", "generator",
  "pumpjack", "chemical-plant", "oil-refinery", "rocket-silo", "electric-pole",
}

-- Noms de tiles d'eau (pour find_nearest "water"/"deepwater").
local WATER_TILE_NAMES = {"water", "deepwater", "water-shallow", "water-mud", "deepwater-green", "water-green"}

-- Construit une ligne d'entite pour scan_area/scan_factory (forme commune).
local function entity_row(surface, e)
  local rec = {
    name = e.name,
    type = e.type,
    x = r1(e.position.x),
    y = r1(e.position.y),
    direction = utils_entity.name_from_direction(e.direction),
    status = utils_entity.status_name(e.status),
  }
  if e.type == "mining-drill" then
    local mt = e.mining_target
    rec.mining = (mt and mt.valid and mt.name) or "nothing"
    rec.oreUnder = utils_entity.drill_ore_under(surface, e)
  end
  if utils_entity.is_crafting_machine(e) then
    local recipe = utils_entity.recipe_of(e)
    rec.recipe = recipe and recipe.name or "none"
  end
  return rec
end

-- ===== scan_area(radius) =====
function M.scan_area(radius)
  local char = player_mod.get_ai_entity()
  if not char then return json.encode({error = "aucun avatar IA"}) end
  local surface = char.surface
  local r = math.min((radius and radius > 0) and radius or 32, 128)

  local entities = {}
  local n = 0
  for _, e in ipairs(surface.find_entities_filtered({position = char.position, radius = r})) do
    if e.name ~= "character" then
      if n >= 200 then break end
      n = n + 1
      table.insert(entities, entity_row(surface, e))
    end
  end

  local resources = {}
  for _, res in ipairs(surface.find_entities_filtered({position = char.position, radius = r, type = "resource"})) do
    local cur = resources[res.name]
    if cur then
      cur.count = cur.count + 1
    else
      resources[res.name] = {count = 1, x = math.floor(res.position.x), y = math.floor(res.position.y)}
    end
  end

  return json.encode({
    tick = game.tick,
    origin = {x = char.position.x, y = char.position.y},
    radius = r,
    entities = entities,
    resources = resources,
  })
end

-- ===== scan_factory() =====
function M.scan_factory()
  local char = player_mod.get_ai_entity()
  if not char then return json.encode({error = "aucun avatar IA"}) end
  local surface = char.surface
  local force = char.force

  local entities = {}
  local n = 0
  for _, e in ipairs(surface.find_entities_filtered({force = force, type = PRODUCER_TYPES})) do
    if n >= 100 then break end
    n = n + 1
    table.insert(entities, entity_row(surface, e))
  end

  return json.encode({
    tick = game.tick,
    origin = {x = char.position.x, y = char.position.y},
    radius = -1,
    entities = entities,
    resources = {},
  })
end

-- ===== find_nearest(name) =====
-- Branche water (find_tiles_filtered) vs entites. Filtre les resource tiles deja
-- couvertes par une foreuse de la force (evite de renvoyer un minerai sous un drill).
function M.find_nearest(name)
  local char = player_mod.get_ai_entity()
  if not char then return json.encode({}) end
  local surface = char.surface
  local force = char.force
  local pp = char.position
  local bx, by, bd = 0, 0, -1

  if name == "water" or name == "deepwater" then
    for _, t in ipairs(surface.find_tiles_filtered({position = pp, radius = 400, name = WATER_TILE_NAMES})) do
      local dx = t.position.x - pp.x
      local dy = t.position.y - pp.y
      local d = dx * dx + dy * dy
      if bd < 0 or d < bd then
        bd = d; bx = t.position.x; by = t.position.y
      end
    end
  else
    -- Pre-calcule les bounding boxes des foreuses de la force (evite un find par candidat).
    local drill_boxes = {}
    for _, d in ipairs(surface.find_entities_filtered({type = "mining-drill", force = force, position = pp, radius = 400})) do
      local bb = d.bounding_box
      table.insert(drill_boxes, {left_top = {x = bb.left_top.x, y = bb.left_top.y}, right_bottom = {x = bb.right_bottom.x, y = bb.right_bottom.y}})
    end
    local function covered(px, py)
      for _, bb in ipairs(drill_boxes) do
        if px >= bb.left_top.x and px <= bb.right_bottom.x and py >= bb.left_top.y and py <= bb.right_bottom.y then
          return true
        end
      end
      return false
    end
    -- find_entities_filtered jette si name invalide ; pcall + fallback type.
    local ents = utils_entity.find_target_entities(surface, pp, 400, name)
    for _, e in ipairs(ents) do
      if not covered(e.position.x, e.position.y) then
        local dx = e.position.x - pp.x
        local dy = e.position.y - pp.y
        local d = dx * dx + dy * dy
        if bd < 0 or d < bd then
          bd = d; bx = r1(e.position.x); by = r1(e.position.y)
        end
      end
    end
  end

  if bd < 0 then
    return json.encode({})
  end
  return json.encode({name = name, x = bx, y = by, distance = math.floor(math.sqrt(bd))})
end

-- ===== describe(name) =====
-- Lookup autoritaire : recette (force-specifique) + mecaniques d'entite placeable.
local function energy_source_of(proto)
  if proto.electric_energy_source_prototype then return "electric" end
  if proto.burner_prototype then return "burner" end
  if proto.heat_energy_source_prototype then return "heat" end
  if proto.fluid_energy_source_prototype then return "fluid" end
  return "none"
end

function M.describe(name)
  local result = {name = name}

  -- Recette (force-specifique pour `enabled`).
  local force = player_mod.get_ai_force()
  local recipe = force.recipes[name]
  if recipe then
    local ingredients = {}
    for _, i in ipairs(recipe.ingredients) do
      table.insert(ingredients, {name = i.name, amount = i.amount})
    end
    local products = {}
    for _, p in ipairs(recipe.products) do
      table.insert(products, {name = p.name, amount = p.amount or 1})
    end
    result.recipe = {
      name = name,
      ingredients = ingredients,
      products = products,
      enabled = recipe.enabled,
      category = recipe.category,
    }
  end

  -- Entite placeable.
  local proto = prototypes.entity[name]
  if proto then
    local esrc = energy_source_of(proto)
    local box = proto.collision_box
    local entity = {
      name = name,
      type = proto.type,
      energySource = esrc,
      needsFuel = esrc == "burner",
      size = {w = math.ceil(box.right_bottom.x - box.left_top.x), h = math.ceil(box.right_bottom.y - box.left_top.y)},
    }
    if proto.type == "mining-drill" then
      local ok, speed = pcall(function() return proto.mining_speed end)
      if ok then entity.miningSpeed = speed end
      local ok2, cats = pcall(function() return proto.resource_categories end)
      if ok2 and cats then
        local cl = {}
        for k, _ in pairs(cats) do table.insert(cl, k) end
        entity.resourceCategories = cl
      end
    end
    if proto.type == "furnace" or proto.type == "assembling-machine" then
      local ok, speed = pcall(function() return proto.get_crafting_speed() end)
      if ok then entity.craftingSpeed = speed end
    end
    result.entity = entity
  end

  return json.encode(result)
end

-- ===== get_recipe(item) =====
function M.get_recipe(item_name)
  local force = player_mod.get_ai_force()
  local recipe = force.recipes[item_name]
  if not recipe then
    return json.encode({error = "recette inexistante: " .. tostring(item_name)})
  end
  if not recipe.enabled then
    return json.encode({error = "recette verrouillee: " .. tostring(item_name)})
  end
  local ingredients = {}
  for _, i in ipairs(recipe.ingredients) do
    table.insert(ingredients, {name = i.name, count = i.amount})
  end
  return json.encode({ingredients = ingredients, enabled = true})
end

-- ===== production_stats() =====
-- Compteurs cumules de production/consommation de la force (ground truth : ce qui a
-- ete FABRIQUE, pas seulement ce que l'inventaire tient).
function M.production_stats()
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({produced = {}, consumed = {}}) end
  local stats = player_mod.get_ai_force().get_item_production_statistics(surface)
  local produced = {}
  for item, count in pairs(stats.input_counts) do
    produced[item] = count
  end
  local consumed = {}
  for item, count in pairs(stats.output_counts) do
    consumed[item] = count
  end
  return json.encode({produced = produced, consumed = consumed})
end

return M