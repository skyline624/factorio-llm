-- utils_entity.lua : helpers Factorio mutualises (DRY) pour factorio-llm.
-- Regroupe la logique nom-vs-type, statut, direction, plus-proche, et helpers
-- de placement utilises par task_manager, operations et tools. Une seule source
-- de verite pour eviter la duplication (SOLID/DRY).

local M = {}

-- Distance euclidienne au carre (pas de sqrt : pour comparaisons uniquement).
local function dist_sq(a, b)
  local dx = a.x - b.x
  local dy = a.y - b.y
  return dx * dx + dy * dy
end
M.dist_sq = dist_sq

-- Entite la plus proche d'une position d'origine parmi une liste.
-- (Port de utils/entity.ts ; prend une position plutot qu'un player -> reutilisable
--  pour le character headless comme pour le joueur connecte.)
function M.get_nearest_entity(origin, entities)
  if not entities or #entities == 0 then return nil end
  local best, best_d = nil, math.huge
  for _, e in ipairs(entities) do
    if e.valid then
      local d = dist_sq(origin, e.position)
      if d < best_d then best_d = d best = e end
    end
  end
  return best
end

-- Quantite de minerai sous une foreuse (somme des resources dans son rayon de minage).
function M.drill_ore_under(surface, drill)
  local radius = drill.prototype.mining_drill_radius or 1
  local p = drill.position
  local area = {
    left_top = {x = p.x - radius, y = p.y - radius},
    right_bottom = {x = p.x + radius, y = p.y + radius},
  }
  local total = 0
  for _, r in ipairs(surface.find_entities_filtered({area = area, type = "resource"})) do
    total = total + (r.amount or 0)
  end
  return math.floor(total)
end

-- Vrai si l'entite est une machine de craft (assembler/four/silo).
function M.is_crafting_machine(e)
  local t = e.type
  return t == "assembling-machine" or t == "furnace" or t == "rocket-silo"
end

-- Recette posee sur une machine de craft, ou nil (four -> nil : smelt par categorie).
function M.recipe_of(e)
  if not M.is_crafting_machine(e) then return nil end
  local ok, recipe = pcall(function() return e.get_recipe() end)
  if ok then return recipe end
  return nil
end

-- Recherche d'entites cibles par nom ou type (find_entities_filtered jette si name
-- invalide). On essaie par name d'abord, puis par type en fallback (pcall les deux).
function M.find_target_entities(surface, pos, radius, name)
  local ok, ents = pcall(function()
    return surface.find_entities_filtered({position = pos, radius = radius, name = name})
  end)
  if ok and ents and #ents > 0 then return ents end
  ok, ents = pcall(function()
    return surface.find_entities_filtered({position = pos, radius = radius, type = name})
  end)
  if ok then return ents or {} end
  return {}
end

-- ===== Direction =====

-- Nom de direction ('north'|'east'|...) -> defines.direction (int). 8-way.
function M.direction_from_name(name)
  local map = {
    north = defines.direction.north,
    northeast = defines.direction.northeast,
    east = defines.direction.east,
    southeast = defines.direction.southeast,
    south = defines.direction.south,
    southwest = defines.direction.southwest,
    west = defines.direction.west,
    northwest = defines.direction.northwest,
  }
  return map[name] or defines.direction.north
end

-- defines.direction (int) -> nom de direction (pour sortie JSON).
function M.name_from_direction(dir)
  local map = {
    [defines.direction.north] = "north",
    [defines.direction.northeast] = "northeast",
    [defines.direction.east] = "east",
    [defines.direction.southeast] = "southeast",
    [defines.direction.south] = "south",
    [defines.direction.southwest] = "southwest",
    [defines.direction.west] = "west",
    [defines.direction.northwest] = "northwest",
  }
  return map[dir] or "north"
end

-- Convertit un vecteur start->end en une des 8 directions defines.direction.
-- Convention Factorio walking_state.direction : angle depuis le NORD, sens horaire,
-- avec y croissant vers le SUD (nord = -y). D'ou angle = atan2(dx, -dy).
-- (Le port precedent d'airi utilisait atan2(dy, -dx) -> directions est/ouest miroir,
--  le character marchait dans une mauvaise direction et se bloquait sur les waypoints.)
function M.get_direction(start_pos, end_pos)
  local dx = end_pos.x - start_pos.x
  local dy = end_pos.y - start_pos.y
  -- Factorio : math.atan ignore son 2e argument (1-arg). On DOIT utiliser math.atan2.
  -- atan2(dx, -dy) = angle depuis le NORD, sens horaire (y croit vers le sud), [-pi, pi].
  local angle = math.atan2(dx, -dy)
  if angle < 0 then angle = angle + 2 * math.pi end
  -- 0=north, 1=northeast, 2=east, 3=southeast, 4=south, 5=southwest, 6=west, 7=northwest
  local idx = math.floor(angle / (math.pi / 4) + 0.5) % 8
  -- Factorio 2.0 : defines.direction est 16-directions, les 8 cardinales sont a pas de 2
  -- (north=0, northeast=2, east=4, southeast=6, south=8, southwest=10, west=12, northwest=14).
  -- On renvoie donc idx*2 pour tomber sur la bonne cardinale.
  return idx * 2
end

-- ===== Statut d'entite =====

-- Mappe defines.entity_status -> string lisible (port de tools.ts status_name).
function M.status_name(status)
  if status == nil then return "n/a" end
  local s = defines.entity_status
  local map = {
    [s.working] = "working",
    [s.normal] = "normal",
    [s.no_power] = "no_power",
    [s.low_power] = "low_power",
    [s.no_fuel] = "no_fuel",
    [s.item_ingredient_shortage] = "item_ingredient_shortage",
    [s.fluid_ingredient_shortage] = "fluid_ingredient_shortage",
    [s.no_input_fluid] = "no_input_fluid",
    [s.full_output] = "full_output",
    [s.waiting_for_space_in_destination] = "waiting_for_space_in_destination",
    [s.waiting_for_source_items] = "waiting_for_source_items",
    [s.not_plugged_in_electric_network] = "not_plugged_in_electric_network",
    [s.no_minable_resources] = "no_minable_resources",
    [s.disabled_by_script] = "disabled_by_script",
    [s.no_recipe] = "no_recipe",
    [s.waiting_for_target_to_be_built] = "waiting_for_target_to_be_built",
    [s.preparing_rocket_for_launch] = "preparing_rocket_for_launch",
    [s.waiting_to_launch_rocket] = "waiting_to_launch_rocket",
    [s.not_connected_to_rail] = "not_connected_to_rail",
    [s.no_modules_to_transmit] = "no_modules_to_transmit",
    [s.marked_for_deconstruction] = "marked_for_deconstruction",
  }
  return map[status] or "other"
end

-- ===== Placement =====

-- Si le character se retrouve piege dans la bounding box d'une entite fraichement
-- posee, le teleporte sur le tile libre le plus proche (evite l'auto-emprisonnement).
function M.push_character_clear(character, entity)
  if not character or not character.valid or not entity or not entity.valid then return end
  local bb = entity.bounding_box
  local cp = character.position
  if cp.x >= bb.left_top.x and cp.x <= bb.right_bottom.x
     and cp.y >= bb.left_top.y and cp.y <= bb.right_bottom.y then
    local free = character.surface.find_non_colliding_position("character", cp, 6, 0.25)
    if free then
      character.teleport(free)
      log("[fl] character degage de l'entite " .. entity.name)
    end
  end
end

-- Detruit les obstacles organiques (arbres/rocks) autour d'une position.
-- Escape hatch quand le pathfinding seul ne suffit pas a degager la voie.
function M.clear_obstacles_near(surface, position, radius)
  radius = radius or 2.5
  local ents = surface.find_entities_filtered({
    position = position, radius = radius, type = {"tree", "simple-entity"},
  })
  local n = 0
  for _, o in ipairs(ents) do
    if o.valid then o.destroy() n = n + 1 end
  end
  return n
end

-- Verifie qu'une position n'est pas deja couverte par une foreuse de la force
-- (evite de renvoyer un minerai deja sous un drill dans find_nearest).
function M.covered_by_drill(surface, force, pos, radius)
  local drills = surface.find_entities_filtered({
    position = pos, radius = radius or 400, type = "mining-drill", force = force,
  })
  for _, d in ipairs(drills) do
    local bb = d.bounding_box
    if pos.x >= bb.left_top.x and pos.x <= bb.right_bottom.x
       and pos.y >= bb.left_top.y and pos.y <= bb.right_bottom.y then
      return d
    end
  end
  return nil
end

return M