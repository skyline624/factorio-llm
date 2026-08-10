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
local math_utils = require("scripts.utils_math")   -- E3 : distance (get_power_state)

local M = {}

-- Arrondit a 1 decimale (pour les coordonnees JSON).
local function r1(v) return math.floor(v * 10) / 10 end

-- Types de producteurs pour scan_factory (cf. airi producer_types).
local PRODUCER_TYPES = {
  "mining-drill", "furnace", "assembling-machine", "lab", "boiler", "generator",
  "pumpjack", "chemical-plant", "oil-refinery", "rocket-silo", "electric-pole",
  "beacon",
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
    local okd, dp = pcall(function() return e.drop_position end)
    if okd and dp then rec.dropX, rec.dropY = r1(dp.x), r1(dp.y) end
  end
  -- E13 : ou un inserter PREND et ou il DEPOSE, en coordonnees monde.
  -- Sans ces deux champs, un inserter qui depose dans le vide est indiscernable d'un
  -- inserter correct : il est pose, sans erreur, avec le statut d'un bras qui attend.
  -- Mesure : un inserter oriente `north` prend en y-1 et depose en y+1.3 -- l'inverse
  -- de la convention attendue. On ne DEDUIT donc plus le sens d'un inserter, on le LIT.
  if e.type == "inserter" then
    local oki, pk = pcall(function() return e.pickup_position end)
    if oki and pk then rec.pickupX, rec.pickupY = r1(pk.x), r1(pk.y) end
    local okd, dp = pcall(function() return e.drop_position end)
    if okd and dp then rec.dropX, rec.dropY = r1(dp.x), r1(dp.y) end
  end
  if utils_entity.is_crafting_machine(e) then
    local recipe = utils_entity.recipe_of(e)
    rec.recipe = recipe and recipe.name or "none"
  end
  -- S3b : modules insérés (beacon + crafting-machines). get_module_inventory() est
  -- accessible au runtime sur l'INSTANCE (contrairement aux prototypes). Retourne
  -- [{name, count}] par slot. Indispensable pour valider live que les beacons posés
  -- contiennent bien les modules attendus (scan_factory).
  -- E2 : rendre VERIFIABLES les options de pose du LayoutPlanner. Sans ces champs,
  -- on pose un underground-belt ou un splitter sans pouvoir controler que le sens et
  -- les priorites ont bien ete appliques (belt_to_ground_type n'est pas modifiable
  -- apres coup : une erreur ici oblige a retirer l'entite).
  if e.type == "underground-belt" then
    local okug, ug = pcall(function() return e.belt_to_ground_type end)
    if okug then rec.ugType = ug end
  end
  if e.type == "splitter" then
    local oki, pin = pcall(function() return e.splitter_input_priority end)
    local oko, pout = pcall(function() return e.splitter_output_priority end)
    if oki then rec.prioIn = pin end
    if oko then rec.prioOut = pout end
  end
  if e.type == "beacon" or utils_entity.is_crafting_machine(e) then
    local okmi, inv = pcall(function() return e.get_module_inventory() end)
    if okmi and inv and inv.valid then
      local mods = {}
      for i = 1, #inv do
        local s = inv[i]
        if s and s.valid and s.valid_for_read then
          table.insert(mods, {name = s.name, count = s.count})
        end
      end
      rec.modules = mods
    end
  end
  return rec
end

-- ===== scan_area(radius) =====
function M.scan_area(radius)
  local char = player_mod.get_ai_entity()
  if not char then return json.encode({error = "aucun avatar IA"}) end
  local surface = char.surface
  local r = math.min((radius and radius > 0) and radius or 32, 128)

  -- Les RESSOURCES sont exclues de cette liste : elles sont deja agregees plus bas,
  -- par nom et par comptage. Les y laisser les faisait consommer le plafond de 200
  -- entites -- mesure sur un gisement de charbon : scan_area rendait 200 lignes, toutes
  -- `resource` ou `electric-pole`, et le boiler pose au milieu etait INVISIBLE. Le
  -- diagnostic concluait alors « aucune machine » sur une usine bien reelle.
  local entities = {}
  local n = 0
  for _, e in ipairs(surface.find_entities_filtered({position = char.position, radius = r})) do
    if e.name ~= "character" and e.type ~= "resource" then
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

  -- QUI PRODUIT CET ITEM. Le NOM d'une recette n'est pas son produit : le gaz sort de
  -- `basic-oil-processing`, l'acide de `sulfuric-acid`... Interroger `describe` sur le
  -- PRODUIT ne rendait donc rien, et l'appelant en concluait qu'il fallait le MINER --
  -- « petroleum-gas » figurait parmi les gisements a prospecter. On expose donc les
  -- recettes qui le fabriquent ; laquelle retenir est un arbitrage, il appartient a
  -- l'appelant (certaines bouclent, d'autres ne font que vider des barils).
  local producteurs = {}
  for nom, r in pairs(force.recipes) do
    for _, p in pairs(r.products or {}) do
      if p.name == name and nom ~= name then
        producteurs[#producteurs + 1] = {name = nom, enabled = r.enabled and true or false,
                                         n_ingredients = #r.ingredients}
        break
      end
    end
  end
  if #producteurs > 0 then result.recipes_producing = producteurs end
  if recipe then
    -- S2a : type ("item"|"fluid") par ingredient/produit (lisible sur LuaRecipe 2.0
    -- via i.type/p.type). Defaut "item" (back-compat : recipes solides inchangues).
    local ingredients = {}
    for _, i in ipairs(recipe.ingredients) do
      local it = {name = i.name, amount = i.amount}
      local ok, tp = pcall(function() return i.type end)
      it.type = (ok and tp) or "item"
      table.insert(ingredients, it)
    end
    local products = {}
    for _, p in ipairs(recipe.products) do
      local pr = {name = p.name, amount = p.amount or 1}
      local ok, tp = pcall(function() return p.type end)
      pr.type = (ok and tp) or "item"
      table.insert(products, pr)
    end
    result.recipe = {
      name = name,
      ingredients = ingredients,
      products = products,
      enabled = recipe.enabled,
      category = recipe.category,
      -- craft_time (en secondes a crafting_speed=1) : energy requise pour 1 craft.
      -- Dispo via LuaRecipe.energy. Indispensable au solveur de debit :
      -- debit_machine = result_count * crafting_speed / energy.
      energy = recipe.energy,
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
    -- Marqueur sentinelle : confirme que cette version du mod est chargee
    -- (distingue "mod non recharge" de "nom de champ Lua API faux").
    entity._layoutMark = "tools_v2"
    if proto.type == "mining-drill" then
      local ok, speed = pcall(function() return proto.mining_speed end)
      if ok then entity.miningSpeed = speed end
      local ok2, cats = pcall(function() return proto.resource_categories end)
      if ok2 and cats then
        local cl = {}
        for k, _ in pairs(cats) do table.insert(cl, k) end
        entity.resourceCategories = cl
      end
      -- Zone d'extraction : NON lisible au runtime Factorio 2.0 (pas d'accesseur
      -- sur LuaEntityPrototype) -> hardcode Python (electric=5x5, burner=2x2).
    end
    -- Inserter (portee pickup/drop) + electric-pole (wire/supply) + mining-drill
    -- (mining_area) : NON lisibles au runtime Factorio 2.0 sur le prototype
    -- (LuaEntityPrototype n'expose ni propriete ni getter utilisable via
    -- prototypes.entity[name]). -> hardcode Python (valeurs wiki stables),
    -- valide en S0b par mesure in-game (poser l'entite, lire sa zone reelle).
    if proto.type == "furnace" or proto.type == "assembling-machine" then
      local ok, speed = pcall(function() return proto.get_crafting_speed() end)
      if ok then entity.craftingSpeed = speed end
      -- Categories de craft supportees par la machine (set -> liste).
      -- Indispensable au solveur pour matcher recette.category ∈ machine.crafting_categories
      -- (ex. electronic-circuit category=electronics -> faut un assembler qui la supporte).
      local okc, cats = pcall(function() return proto.crafting_categories end)
      if okc and cats then
        local cl = {}
        for k, _ in pairs(cats) do table.insert(cl, k) end
        entity.craftingCategories = cl
      end
    end
    -- S2a : fluid_boxes (Factorio 2.0) : connexions pipe des machines fluides
    -- (furnace, assembling-machine, mining-drill). Chaque box = {production_type,
    -- pipe_connections:[{x,y,direction?}]} (positions relatives au centre entite).
    -- Indispensable au LayoutPlanner pour poser les pipes devant le bon port fluide.
    -- CONSTAT API 2.0 : proto.fluid_boxes est INEXISTANT au runtime (pcall échoue,
    -- "LuaEntityPrototype doesn't contain key fluid_boxes"). Cette branche retourne donc
    -- un fluid_boxes vide en 2.0. La source de vérité devient measure_entity (instance
    -- posée, cf. ci-dessous) + hardcode Python GEOMETRY_FIXTURE (fallback validé S2a).
    if proto.type == "furnace" or proto.type == "assembling-machine" or proto.type == "mining-drill" then
      local ok, fbs = pcall(function() return proto.fluid_boxes end)
      if ok and fbs then
        local boxes = {}
        for _, fb in ipairs(fbs) do
          local box = {production_type = fb.production_type}
          local conns = {}
          local okc, pcs = pcall(function() return fb.pipe_connections end)
          if okc and pcs then
            for _, pc in ipairs(pcs) do
              local c = {x = r1(pc.position.x), y = r1(pc.position.y)}
              local okd, d = pcall(function() return pc.direction end)
              if okd and d ~= nil then c.direction = d end
              table.insert(conns, c)
            end
          end
          box.pipe_connections = conns
          table.insert(boxes, box)
        end
        entity.fluid_boxes = boxes
      end
    end
    -- S2a : offshore-pump : output_fluid (nom du fluide produit) + fluidbox_pipe_connections
    -- (connexion pipe output, position relative). Type distinct des machines ci-dessus.
    if proto.type == "offshore-pump" then
      local ok, of = pcall(function() return proto.output_fluid end)
      if ok and of then
        entity.output_fluid = type(of) == "string" and of or of.name
      end
      local ok2, pcs = pcall(function() return proto.fluidbox_pipe_connections end)
      if ok2 and pcs then
        local conns = {}
        for _, pc in ipairs(pcs) do
          table.insert(conns, {x = r1(pc.position.x), y = r1(pc.position.y)})
        end
        entity.fluidbox_pipe_connections = conns
      end
    end
    -- S3b : beacon (Factorio 2.0). supply_area_distance = portée du beacon (accessible
    -- via ent.prototype, déjà lu pour electric-poles dans measure_entity). module_slots,
    -- allowed_effects, distribution_effectivity, max_energy_usage = CONSTAT probable
    -- (inaccessibles au runtime sur le prototype, cf. fluid_boxes S2a / max_energy_usage
    -- S2b-2) -> renvoyés nil si pcall échoue, hardcode Python BEACON_FIXTURE authoritative.
    if proto.type == "beacon" then
      local b = {}
      local oksa, sa = pcall(function() return proto.supply_area_distance end)
      if oksa and sa ~= nil then b.supply_area_distance = sa end
      local okms, ms = pcall(function() return proto.module_slots end)
      if okms and ms ~= nil then b.module_slots = ms end
      local okae, ae = pcall(function() return proto.allowed_effects end)
      if okae and ae ~= nil then
        local el = {}
        for k, _ in pairs(ae) do table.insert(el, k) end
        b.allowed_effects = el
      end
      local okde, de = pcall(function() return proto.distribution_effectivity end)
      if okde and de ~= nil then b.distribution_effectivity = de end
      local okme, meu = pcall(function() return proto.max_energy_usage end)
      if okme and meu ~= nil then b.max_energy_usage = meu end
      entity.beacon = b
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
    -- S2a : type ("item"|"fluid") + forward-compat probability/amount_min/amount_max.
    -- count inchangé (back-compat : perception.recipe_of lit ingredients[].count).
    local it = {name = i.name, count = i.amount}
    local ok, tp = pcall(function() return i.type end)
    it.type = (ok and tp) or "item"
    table.insert(ingredients, it)
  end
  local products = {}
  for _, p in ipairs(recipe.products) do
    local pr = {name = p.name, count = p.amount or 1}
    local ok, tp = pcall(function() return p.type end)
    pr.type = (ok and tp) or "item"
    local okp, prob = pcall(function() return p.probability end)
    if okp and prob ~= nil then pr.probability = prob end
    local okmn, amn = pcall(function() return p.amount_min end)
    if okmn and amn ~= nil then pr.amount_min = amn end
    local okmx, amx = pcall(function() return p.amount_max end)
    if okmx and amx ~= nil then pr.amount_max = amx end
    table.insert(products, pr)
  end
  -- Aligne sur describe : products + category + energy (enrichit sans casser la
  -- lecture existante de ingredients[].count par perception.recipe_of).
  return json.encode({
    ingredients = ingredients,
    products = products,
    enabled = true,
    category = recipe.category,
    energy = recipe.energy,
  })
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

-- ===== Validation LayoutPlanner (S0b) =====
-- Commandes synchrones NON destructives pour valider un blueprint : can_place_check
-- (surface.can_place_entity sans poser), scan_patch (bbox d'un gisement reel),
-- measure_entity (pose + mesure + detruit, mode test only -> valide le hardcode Python).

-- can_place_check(name, x, y, direction) : test non destructif de placabilite.
-- Retourne {name, x, y, can_place, error?}. direction = string cardinale ou int 16-dir.
--
-- build_check_type = manual : OBLIGATOIRE pour que la verification predise la POSE.
-- state_placing_at (task_manager.lua) pose avec `manual` ; sans lui, can_place_entity
-- utilise le mode `script`, plus permissif, et les deux divergent. Mesure live E1 :
-- burner-mining-drill sur de l'herbe -> can_place_check(script)=True 26 fois sur 26,
-- pose(manual)=echec 26 fois sur 26 ("cannot place here"), car un mining-drill hors
-- gisement est refuse par le curseur joueur ("no ore"). L'executor Python posait donc
-- en aveugle et rapportait des poses fantomes. Meme drill sur une tuile iron-ore : pose OK.
function M.can_place_check(name, x, y, direction)
  local surface = game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  if not name or name == "" then return json.encode({name = name, can_place = false, error = "name requis"}) end
  local dir = defines.direction.north
  if type(direction) == "string" then
    dir = utils_entity.direction_from_name(direction)
  elseif type(direction) == "number" then
    dir = direction
  end
  local force = game.forces.player
  local ok, can = pcall(function()
    return surface.can_place_entity{
      name = name, position = {x = x, y = y}, direction = dir, force = force,
      build_check_type = defines.build_check_type.manual,
    }
  end)
  if not ok then return json.encode({name = name, x = r1(x), y = r1(y), can_place = false, error = tostring(can)}) end
  if can == true then
    return json.encode({name = name, x = r1(x), y = r1(y), can_place = true})
  end

  -- POURQUOI, ET PAS SEULEMENT NON. `can_place_entity` rend un booleen muet : l'appelant
  -- sait qu'il ne peut pas poser, jamais ce qui l'en empeche. Une seule entite refusee
  -- fait abandonner un plan de plusieurs centaines, et il a fallu quatre hypotheses
  -- fausses -- l'avatar, la direction, un obstacle, une collision interne -- avant de
  -- decouvrir que l'agent avait MINE le minerai sous sa propre foreuse. Le jeu connait la
  -- cause ; il suffit de la lui demander.
  local proto = prototypes.entity[name]
  local w = (proto and proto.tile_width) or 1
  local h = (proto and proto.tile_height) or 1
  local x1, y1 = x - w / 2, y - h / 2
  local x2, y2 = x + w / 2, y + h / 2
  local motifs = {}

  local occ = {}
  for _, e in pairs(surface.find_entities_filtered{area = {{x1, y1}, {x2, y2}}}) do
    if e.type ~= "resource" and e.type ~= "decorative" then
      occ[#occ + 1] = e.name .. (e.type == "character" and " (l'avatar)" or "")
    end
  end
  if #occ > 0 then motifs[#motifs + 1] = "occupe par " .. table.concat(occ, ", ") end

  local mauvaises = {}
  for tx = math.floor(x1), math.ceil(x2) - 1 do
    for ty = math.floor(y1), math.ceil(y2) - 1 do
      local t = surface.get_tile(tx, ty)
      local n = t and t.name or ""
      if n == "out-of-map" or string.find(n, "water") then
        mauvaises[#mauvaises + 1] = n
      end
    end
  end
  if #mauvaises > 0 then motifs[#motifs + 1] = "tuile " .. table.concat(mauvaises, ", ") end

  -- Une foreuse exige du minerai SOUS son emprise : c'est le refus le plus frequent, et
  -- le plus trompeur, puisque la position etait valide au moment ou le plan l'a choisie.
  if proto and proto.type == "mining-drill" then
    local n = #surface.find_entities_filtered{area = {{x1, y1}, {x2, y2}}, type = "resource"}
    if n == 0 then motifs[#motifs + 1] = "aucun minerai sous la foreuse" end
  end

  return json.encode({name = name, x = r1(x), y = r1(y), can_place = false,
                      motif = (#motifs > 0) and table.concat(motifs, " ; ")
                              or "refus sans cause visible (portee ? force ?)"})
end

-- scan_patch(resource, radius) : bbox + count d'un gisement reel autour de l'avatar.
-- Chaque tuile de minerai = 1 entite resource. Retourne {resource, count, bbox, sample}.
function M.scan_patch(resource, radius)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local origin = char and char.position or {x = 0, y = 0}
  local r = math.min((radius and radius > 0) and radius or 400, 400)
  local ok, resources = pcall(function()
    return surface.find_entities_filtered{name = resource, area = {
      left_top = {x = origin.x - r, y = origin.y - r},
      right_bottom = {x = origin.x + r, y = origin.y + r}}}
  end)
  if not ok or not resources or #resources == 0 then
    return json.encode({resource = resource, count = 0, origin = {x = r1(origin.x), y = r1(origin.y)}})
  end
  local x1, y1, x2, y2 = math.huge, math.huge, -math.huge, -math.huge
  local total_amount = 0
  for _, re in ipairs(resources) do
    local p = re.position
    if p.x < x1 then x1 = p.x end if p.x > x2 then x2 = p.x end
    if p.y < y1 then y1 = p.y end if p.y > y2 then y2 = p.y end
    -- S2a : total_amount = somme des initial_amount (quantité brute du gisement).
    -- Indispensable au solveur fluide pour estimer la duree de vie d'un puits.
    local oka, amt = pcall(function() return re.initial_amount end)
    if oka and amt then total_amount = total_amount + amt end
  end
  -- Le sample est trie par distance a l'observateur, et c'est essentiel : sans tri, il
  -- rendait les 12 PREMIERES entites de l'ordre d'iteration du moteur. Mesure en jeu --
  -- joueur en (0,-41), charbon a 64 tuiles au sud-ouest, sample[0] a 258 tuiles au nord.
  -- Tous les appelants ancrent sur sample[1] en le croyant proche : l'agent partait
  -- traverser 258 tuiles de terrain hostile, et `approvisionner` aurait conclu « trop
  -- loin pour une belt, il faudrait un train » alors qu'un gisement etait a portee.
  --
  -- La `bbox` reste l'enveloppe de TOUS les gisements du rayon (1171 tuiles de charbon
  -- sur plusieurs patches n'ont pas de boite commune qui ait un sens) : on ancre sur une
  -- tuile du sample, jamais sur le centre de la bbox.
  table.sort(resources, function(a, b)
    local da = (a.position.x - origin.x) ^ 2 + (a.position.y - origin.y) ^ 2
    local db = (b.position.x - origin.x) ^ 2 + (b.position.y - origin.y) ^ 2
    return da < db
  end)
  -- DOUZE TUILES DECRIVENT UNE ANCRE, PAS UN GISEMENT. Le sample servait a choisir ou
  -- poser UNE machine ; depuis qu'un plan complet s'implante sur plusieurs gisements, il
  -- faut aussi savoir quelle EMPRISE est reellement du minerai. La bbox ne le dit pas --
  -- elle enveloppe tous les patches du rayon, trous compris -- et le LayoutPlanner, qui
  -- s'y fie, posait un foreur sur de l'herbe : « can_place=False » a 112 tuiles, mesure
  -- en jeu. Avec un echantillon dense, l'appelant reconstruit une emprise de minerai
  -- GARANTI. Les tuiles restent triees par distance : les premieres sont inchangees,
  -- donc tous les appelants qui ancrent sur sample[1] gardent exactement leur comportement.
  local sample = {}
  for i = 1, math.min(#resources, 400) do
    table.insert(sample, {x = math.floor(resources[i].position.x), y = math.floor(resources[i].position.y)})
  end
  return json.encode({
    resource = resource, count = #resources,
    bbox = {x1 = math.floor(x1), y1 = math.floor(y1), x2 = math.floor(x2), y2 = math.floor(y2)},
    sample = sample, origin = {x = r1(origin.x), y = r1(origin.y)},
    total_amount = math.floor(total_amount),
  })
end

-- scan_patches(resource, radius, max) : les gisements DISTINCTS, separes et decrits.
--
-- `scan_patch` rend une bbox unique pour tout ce qu'il trouve. Mesure en jeu : 1052
-- tuiles de charbon dans un rayon de 300 -> une boite de 318x240 tuiles qui n'entoure
-- aucun gisement reel. On ne peut ni s'y ancrer, ni comparer deux sites.
--
-- Ici les tuiles sont groupees par CELLULES de 16 adjacentes (flood-fill 8-connexe sur
-- une grille grossiere) : deux gisements separes par une bande vide deviennent deux
-- entrees. Chacune porte de quoi CHOISIR -- centre, taille, richesse, distance -- au
-- lieu du seul « le plus proche », qui n'est pas toujours le meilleur : un gisement a
-- 60 tuiles borde d'un nid vaut moins qu'un autre a 90 tuiles tranquille.
local CELL = 16

function M.scan_patches(resource, radius, max_patches)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local origin = char and char.position or {x = 0, y = 0}
  local r = math.min((radius and radius > 0) and radius or 300, 500)
  local nmax = math.min((max_patches and max_patches > 0) and max_patches or 8, 20)

  local ok, res = pcall(function()
    return surface.find_entities_filtered{name = resource, area = {
      left_top = {x = origin.x - r, y = origin.y - r},
      right_bottom = {x = origin.x + r, y = origin.y + r}}}
  end)
  if not ok or not res or #res == 0 then
    return json.encode({resource = resource, patches = {}, count = 0,
                        origin = {x = r1(origin.x), y = r1(origin.y)}})
  end

  -- Index par cellule.
  local cells, ordre = {}, {}
  for _, e in ipairs(res) do
    local cx = math.floor(e.position.x / CELL)
    local cy = math.floor(e.position.y / CELL)
    local k = cx .. ":" .. cy
    if not cells[k] then
      cells[k] = {cx = cx, cy = cy, list = {}}
      table.insert(ordre, k)
    end
    table.insert(cells[k].list, e)
  end

  -- Flood-fill 8-connexe sur les cellules occupees.
  local vues, patches = {}, {}
  for _, k0 in ipairs(ordre) do
    if not vues[k0] then
      local pile, groupe = {k0}, {}
      vues[k0] = true
      while #pile > 0 do
        local k = table.remove(pile)
        local c = cells[k]
        table.insert(groupe, c)
        for dx = -1, 1 do for dy = -1, 1 do
          local vk = (c.cx + dx) .. ":" .. (c.cy + dy)
          if cells[vk] and not vues[vk] then
            vues[vk] = true
            table.insert(pile, vk)
          end
        end end
      end
      local x1, y1, x2, y2 = math.huge, math.huge, -math.huge, -math.huge
      local n, amount = 0, 0
      for _, c in ipairs(groupe) do
        for _, e in ipairs(c.list) do
          local p = e.position
          if p.x < x1 then x1 = p.x end if p.x > x2 then x2 = p.x end
          if p.y < y1 then y1 = p.y end if p.y > y2 then y2 = p.y end
          n = n + 1
          local oka, a = pcall(function() return e.amount end)
          if oka and a then amount = amount + a end
        end
      end
      local cx, cy = (x1 + x2) / 2, (y1 + y2) / 2
      table.insert(patches, {
        x = r1(cx), y = r1(cy), count = n, amount = math.floor(amount),
        x1 = math.floor(x1), y1 = math.floor(y1),
        x2 = math.floor(x2), y2 = math.floor(y2),
        dist = r1(math.sqrt((cx - origin.x) ^ 2 + (cy - origin.y) ^ 2)),
      })
    end
  end

  table.sort(patches, function(a, b) return a.dist < b.dist end)
  local sortie = {}
  for i = 1, math.min(#patches, nmax) do table.insert(sortie, patches[i]) end
  return json.encode({resource = resource, patches = sortie, count = #res,
                      groupes = #patches, origin = {x = r1(origin.x), y = r1(origin.y)}})
end

-- scan_water_edge(radius) : tiles d'eau adjacents à terre (bord d'un plan d'eau).
-- Retourne {tiles:[{x,y}], bbox, count, origin}. Non destructif. Sert à positionner
-- un offshore-pump (2x1) sur une tuile d'eau au bord, connectée à la terre ferme.
-- Un tile d'eau est "edge" si au moins un voisin 4-connexe n'est pas de l'eau.
function M.scan_water_edge(radius)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local origin = char and char.position or {x = 0, y = 0}
  local r = math.min((radius and radius > 0) and radius or 200, 400)
  local water = {}
  for _, t in ipairs(surface.find_tiles_filtered{name = WATER_TILE_NAMES, area = {
    left_top = {x = origin.x - r, y = origin.y - r},
    right_bottom = {x = origin.x + r, y = origin.y + r}}}) do
    water[math.floor(t.position.x) .. "," .. math.floor(t.position.y)] = true
  end
  local tiles = {}
  local x1, y1, x2, y2 = math.huge, math.huge, -math.huge, -math.huge
  for key in pairs(water) do
    local sx, sy = string.match(key, "(-?%d+),(-?%d+)")
    local x, y = tonumber(sx), tonumber(sy)
    local on_edge = false
    for _, d in ipairs({{1, 0}, {-1, 0}, {0, 1}, {0, -1}}) do
      if not water[(x + d[1]) .. "," .. (y + d[2])] then on_edge = true; break end
    end
    if on_edge then
      table.insert(tiles, {x = x, y = y})
      if x < x1 then x1 = x end if x > x2 then x2 = x end
      if y < y1 then y1 = y end if y > y2 then y2 = y end
    end
  end
  return json.encode({
    tiles = tiles, count = #tiles,
    bbox = #tiles > 0 and {x1 = x1, y1 = y1, x2 = x2, y2 = y2} or nil,
    origin = {x = r1(origin.x), y = r1(origin.y)},
  })
end

-- scan_obstacles(radius) : obstacles organiques (rochers/arbres/cliffs) autour de l'avatar.
-- Retourne {obstacles:[{x,y,w,h,name,type}], bbox, count, origin}. Non destructif (lecture
-- seule, n'appelle PAS clear_obstacles_near). Sert au LayoutPlanner S4 a detecter le terrain
-- a contourner (replan auto shift anchor en v). bbox = bounding_box floore (x1,y1,w,h).
function M.scan_obstacles(radius)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local origin = char and char.position or {x = 0, y = 0}
  local r = math.min((radius and radius > 0) and radius or 400, 400)
  local obstacles = {}
  local x1, y1, x2, y2 = math.huge, math.huge, -math.huge, -math.huge
  for _, e in ipairs(surface.find_entities_filtered{type = {"tree", "simple-entity", "cliff"},
      area = {left_top = {x = origin.x - r, y = origin.y - r},
              right_bottom = {x = origin.x + r, y = origin.y + r}}}) do
    local bb = e.bounding_box
    local bx1, by1 = math.floor(bb.left_top.x), math.floor(bb.left_top.y)
    local bx2, by2 = math.floor(bb.right_bottom.x), math.floor(bb.right_bottom.y)
    table.insert(obstacles, {x = bx1, y = by1, w = bx2 - bx1, h = by2 - by1,
                             name = e.name, type = e.type})
    if bx1 < x1 then x1 = bx1 end if bx2 > x2 then x2 = bx2 end
    if by1 < y1 then y1 = by1 end if by2 > y2 then y2 = by2 end
  end
  return json.encode({
    obstacles = obstacles, count = #obstacles,
    bbox = #obstacles > 0 and {x1 = x1, y1 = y1, x2 = x2, y2 = y2} or nil,
    origin = {x = r1(origin.x), y = r1(origin.y)},
  })
end

-- scan_tiles_bbox(x1,y1,x2,y2) : toutes les tuiles dans une bbox arbitraire (pas de filtre
-- name). Retourne {tiles:[{x,y,name}], bbox, count}. Cap aire 200x200 pour eviter un payload
-- RCON massif. Sert au LayoutPlanner S4 a peupler Terrain.tile_grid (water/out-of-map precis
-- au niveau tuile, vs bbox-vs-bbox imprécis).
function M.scan_tiles_bbox(x1, y1, x2, y2)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local lx1, ly1, lx2, ly2 = math.floor(x1), math.floor(y1), math.floor(x2), math.floor(y2)
  -- normaliser l'ordre (left_top < right_bottom)
  if lx1 > lx2 then lx1, lx2 = lx2, lx1 end
  if ly1 > ly2 then ly1, ly2 = ly2, ly1 end
  -- cap aire 200x200
  if (lx2 - lx1) > 200 then lx2 = lx1 + 200 end
  if (ly2 - ly1) > 200 then ly2 = ly1 + 200 end
  local tiles = {}
  for _, t in ipairs(surface.find_tiles_filtered{area = {
      left_top = {x = lx1, y = ly1}, right_bottom = {x = lx2, y = ly2}}}) do
    table.insert(tiles, {x = math.floor(t.position.x), y = math.floor(t.position.y),
                         name = t.name})
  end
  return json.encode({
    tiles = tiles, count = #tiles,
    bbox = {x1 = lx1, y1 = ly1, x2 = lx2, y2 = ly2},
  })
end

-- get_tile(x,y) : nom de la tuile a une position. Retourne {x,y,name}. Non destructif.
-- Sert au LayoutPlanner S4 a verifier ponctuellement water/out-of-map (frontiere headless).
function M.get_tile(x, y)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local t = surface.get_tile(math.floor(x), math.floor(y))
  return json.encode({x = math.floor(x), y = math.floor(y), name = t.name})
end

-- inspect_at(x, y, radius) : ce qui est POSE a une position donnee. Non destructif.
--
-- Pendant en LECTURE de remove_entity_at / rotate_entity_at, et complement de
-- scan_area : celui-ci est centre sur le PERSONNAGE, donc inutilisable pour verifier
-- une entite qu'on vient de poser a 40 tuiles. C'est ce qui manquait pour CONTROLER
-- une pose au lieu de la supposer reussie -- la plupart des defauts de ce projet
-- etaient des poses acceptees sans erreur mais geometriquement fausses.
--
-- Retourne {x, y, radius, entities = [entity_row]} : memes champs que scan_area,
-- donc pickup/drop des inserters et drop des foreuses inclus.
function M.inspect_at(x, y, radius)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  -- Le plafond etait de 16 tuiles, applique EN SILENCE. Un appelant qui demandait 25 --
  -- le Coordinator le fait avec son propre rayon d'usine -- en obtenait 16 sans que rien
  -- ne le dise, et se croyait donc devant une zone vide. Mesure : une foreuse posee a 18
  -- tuiles de la zone etait introuvable, `verify_gisement_e21` concluait « l'usine ne
  -- produit pas apres 14 tours » et se mettait en SKIP, alors que l'usine tournait et que
  -- le journal annoncait « 7 machines en etat de marche ».
  --
  -- Le plafond reste (une aire sans borne rendrait des milliers de lignes), mais il est
  -- porte a la taille d'une usine reelle. Le champ `radius` rend TOUJOURS le rayon
  -- effectivement inspecte : un appelant qui demande plus doit pouvoir s'en apercevoir.
  local r = math.min((radius and radius > 0) and radius or 0.5, 64)
  -- Recherche par AIRE, et non par {position, radius} : la variante `radius` compare le
  -- CENTRE des entites au point, si bien qu'une machine 3x2 dont le centre est a une
  -- tuile n'est jamais trouvee -- alors que le point interroge tombe en plein dedans.
  -- C'est ce qui faisait conclure « ce bras depose dans le vide » a un inserter dont le
  -- drop tombait pourtant dans le boiler. `area` teste l'intersection des bounding box,
  -- ce qui est la question reellement posee : qu'y a-t-il A cet endroit ?
  local rows = {}
  local aire = {{x - r, y - r}, {x + r, y + r}}
  for _, e in ipairs(surface.find_entities_filtered({area = aire})) do
    if e.valid and e.type ~= "character" and e.type ~= "resource" then
      table.insert(rows, entity_row(surface, e))
    end
  end
  return json.encode({x = r1(x), y = r1(y), radius = r, entities = rows})
end

-- scan_threats(x, y, radius) : ce qui menace l'usine autour d'une position (J5).
--
-- Non destructif. Trois grandeurs, parce qu'elles ne disent pas la meme chose :
--   nids       : la menace STRUCTURELLE. Un nid ne bouge pas ; c'est de lui que
--                partiront les vagues, et sa distance donne le temps dont on dispose.
--   unites     : la menace IMMEDIATE. Des biters deja en approche ne se traitent pas
--                comme un nid a 300 tuiles.
--   pollution  : le DECLENCHEUR. En Factorio les vagues partent quand le nuage atteint
--                un nid ; une usine sans pollution ne se fait pas attaquer, quel que
--                soit le nombre de nids alentour. C'est ce qui permet de ne PAS
--                fortifier trop tot -- chaque minute passee a se defendre n'est pas
--                passee a produire.
--
-- Retourne {x, y, radius, pollution, totalPollution, peaceful, nests:[{name,x,y,dist}],
-- nestCount, unitCount, nearest:{name,x,y,dist}, evolution?}.
function M.scan_threats(x, y, radius)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local pos = {x = x or (char and char.position.x) or 0,
               y = y or (char and char.position.y) or 0}
  local r = math.min((radius and radius > 0) and radius or 300, 600)

  local out = {x = r1(pos.x), y = r1(pos.y), radius = r}
  pcall(function() out.pollution = r1(surface.get_pollution(pos)) end)
  pcall(function() out.totalPollution = r1(surface.get_total_pollution()) end)
  pcall(function() out.peaceful = surface.peaceful_mode end)
  -- L'evolution a change de place en 2.0 : on tente les deux formes plutot que de
  -- supposer, et on omet le champ si aucune ne repond.
  if not pcall(function() out.evolution = r1(game.forces.enemy.get_evolution_factor(surface)) end) then
    pcall(function() out.evolution = r1(game.forces.enemy.evolution_factor) end)
  end

  local function dist(e)
    local dx, dy = e.position.x - pos.x, e.position.y - pos.y
    return math.sqrt(dx * dx + dy * dy)
  end

  local nests = {}
  local ok_n, spawners = pcall(function()
    return surface.find_entities_filtered{type = {"unit-spawner", "turret"},
                                          force = "enemy", position = pos, radius = r}
  end)
  if ok_n and spawners then
    local tri = {}
    for _, e in ipairs(spawners) do
      if e.valid then table.insert(tri, {e = e, d = dist(e)}) end
    end
    table.sort(tri, function(a, b) return a.d < b.d end)
    out.nestCount = #tri
    -- Cap a 12 : au-dela, la liste ne sert plus a decider, seulement a alourdir.
    for i = 1, math.min(#tri, 12) do
      table.insert(nests, {name = tri[i].e.name, x = r1(tri[i].e.position.x),
                           y = r1(tri[i].e.position.y), dist = r1(tri[i].d)})
    end
  else
    out.nestCount = 0
  end
  out.nests = nests

  local ok_u, units = pcall(function()
    return surface.find_entities_filtered{type = "unit", force = "enemy",
                                          position = pos, radius = r}
  end)
  out.unitCount = (ok_u and units) and #units or 0
  -- Unites PRES de l'usine, comptees a part. « 39 unites dans 300 tuiles » et « des
  -- biters sur l'usine » demandent des reactions opposees, et le compte global ne les
  -- distingue pas. Mesure : 39 unites dans 300, 0 dans 60 -- la difference est tout
  -- l'enjeu. `find_nearest_enemy` ne suffit pas non plus : il rend l'ennemi le plus
  -- proche quel qu'il soit, nid ou unite.
  local ok_p, proches = pcall(function()
    return surface.find_entities_filtered{type = "unit", force = "enemy",
                                          position = pos, radius = 60}
  end)
  out.unitsNear = (ok_p and proches) and #proches or 0

  local ok_ne, ne = pcall(function()
    return surface.find_nearest_enemy{position = pos, max_distance = r,
                                      force = game.forces.player}
  end)
  if ok_ne and ne and ne.valid then
    out.nearest = {name = ne.name, x = r1(ne.position.x), y = r1(ne.position.y),
                   dist = r1(dist(ne))}
  end
  return json.encode(out)
end

-- get_power_state(x, y, radius) : etat ELECTRIQUE autour d'une position (E3).
--
-- Repond aux trois questions qu'aucun outil ne couvrait, et sans lesquelles on sait
-- poser une centrale mais pas verifier qu'elle alimente quoi que ce soit :
--   1. l'entite est-elle RELIEE a un reseau ?          -> networkId, connected
--   2. le reseau tient-il la charge ?                  -> productionKW / consumptionKW
--   3. sinon, pourquoi ?                               -> status (no_power / low_power)
--
-- Le point 2 impose un detour : electric_network_statistics n'existe QUE sur un poteau
-- electrique. On cherche donc un poteau du MEME reseau (rayon elargi) pour lire les flux.
-- Les compteurs de flux sont en joules par tick -> x60 pour des watts.
--
-- Non destructif. Retourne {found, name, x, y, status, networkId, connected,
-- bufferEnergy, bufferSize, productionKW, consumptionKW, satisfaction} ou {found=false}.
function M.get_power_state(x, y, radius)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local r = (radius and radius > 0) and radius or 4
  local pos = {x = x, y = y}

  -- 1. L'entite electrique la plus proche de la position visee.
  local target, best_d = nil, nil
  for _, e in ipairs(surface.find_entities_filtered({position = pos, radius = r})) do
    if e.valid and e.type ~= "character" then
      local okn, nid = pcall(function() return e.electric_network_id end)
      local is_elec = okn and nid ~= nil
      if not is_elec then
        -- Une entite non alimentee n'a pas de network_id : on la retient quand meme
        -- si elle CONSOMME de l'electricite, sinon on ne saurait pas dire « debranchee ».
        local okb, buf = pcall(function() return e.electric_buffer_size end)
        is_elec = okb and buf ~= nil
      end
      if is_elec then
        local d = math_utils.distance(pos, e.position)
        if not best_d or d < best_d then target, best_d = e, d end
      end
    end
  end
  if not target then return json.encode({found = false, x = x, y = y}) end

  local out = {
    found = true,
    name = target.name,
    x = r1(target.position.x),
    y = r1(target.position.y),
    status = utils_entity.status_name(target.status),
  }
  pcall(function() out.networkId = target.electric_network_id end)
  pcall(function() out.connected = target.is_connected_to_electric_network() end)
  pcall(function() out.bufferEnergy = r1(target.energy) end)
  pcall(function() out.bufferSize = r1(target.electric_buffer_size) end)

  -- 2. Statistiques du reseau : elles ne sont lisibles que via un POTEAU du meme reseau.
  if out.networkId then
    local pole = nil
    for _, e in ipairs(surface.find_entities_filtered({position = pos, radius = 40,
                                                       type = "electric-pole"})) do
      local okn, nid = pcall(function() return e.electric_network_id end)
      if okn and nid == out.networkId then pole = e break end
    end
    if pole then
      local okst, stats = pcall(function() return pole.electric_network_statistics end)
      if okst and stats then
        -- Les categories sont du point de vue de l'ENTITE, pas du reseau :
        --   input_counts  = entites qui ont RECU de l'energie -> les CONSOMMATEURS
        --   output_counts = entites qui en ont FOURNI          -> les PRODUCTEURS
        -- Mesure en jeu : sur un reseau centrale + four, input_counts={electric-furnace}
        -- et output_counts={steam-engine}. L'intuition inverse (« ce qui entre dans le
        -- reseau = la production ») donne une production nulle et un diagnostic faux.
        -- Fenetre : one_minute. five_seconds sort a zero juste apres la mise en route,
        -- ce qui ferait conclure a tort a une centrale en panne.
        local prec = defines.flow_precision_index.one_minute
        local function total(counts, category)
          local sum = 0
          for name, _ in pairs(counts or {}) do
            local okf, v = pcall(function()
              return stats.get_flow_count({name = name, category = category,
                                           precision_index = prec})
            end)
            if okf and v then sum = sum + v end
          end
          return sum
        end
        -- joules/tick -> kW : x60 ticks/s / 1000.
        local prod = total(stats.output_counts, "output") * 60 / 1000
        local cons = total(stats.input_counts, "input") * 60 / 1000
        out.productionKW = r1(prod)
        out.consumptionKW = r1(cons)
        if cons > 0 then out.satisfaction = r1(math.min(prod / cons, 9.9)) end
      end
    else
      out.noPole = true   -- reseau sans poteau a portee : stats indisponibles
    end
  end
  return json.encode(out)
end

-- generate_terrain(x, y, radius) : genere les chunks autour de (x, y) SYNCHRONE.
-- request_to_generate_chunks(position, radius_chunks) + force_generate_chunk_requests()
-- (API Factorio 2.0). Resout le CONSTAT S1d/S1g : sans ceci, walk_to (pathfinding) ne
-- peut pas planifier vers du out-of-map (tuiles non walkable) -> le character ne s'y
-- rend jamais -> le terrain n'est jamais genere -> orniere. Ici on genere AVANT le
-- walk, rendant la cible walkable. Aussi utile en headless (le character headless ne
-- genere pas le terrain en marchant, contrairement au joueur connecte).
-- radius en TUILES, converti en chunks (1 chunk = 32 tuiles). Cap 200 tuiles (7 chunks).
-- Non destructif (cree du terrain vierge, ne detruit rien). Retourne {x, y,
-- radius_chunks, generated, total} ou {error}. Sert au LayoutPlanner S4 + couche P2
-- (Coordinator ordonne la generation avant de builder au-dela de la starting_area).
function M.generate_terrain(x, y, radius)
  local char = player_mod.get_ai_entity()
  local surface = char and char.surface or game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local r_tiles = math.min(math.max((radius and radius > 0) and radius or 30, 1), 200)
  local r_chunks = math.floor(r_tiles / 32) + 1
  surface.request_to_generate_chunks({x = x, y = y}, r_chunks)
  surface.force_generate_chunk_requests()
  -- Statut : compte les chunks generes autour de (x, y) pour confirmation cote Python.
  local cx, cy = math.floor(x / 32), math.floor(y / 32)
  local generated = 0
  for dy = -r_chunks, r_chunks do
    for dx = -r_chunks, r_chunks do
      if surface.is_chunk_generated({x = cx + dx, y = cy + dy}) then
        generated = generated + 1
      end
    end
  end
  local side = 2 * r_chunks + 1
  return json.encode({
    x = math.floor(x), y = math.floor(y),
    radius_chunks = r_chunks, generated = generated, total = side * side,
  })
end

-- measure_entity(name, x, y, direction) : pose une entite, mesure ses proprietes
-- reelles (size, pickup/drop_position, belt_speed, mining_drill_radius, wire/supply),
-- puis la DETRUIT. Mode test uniquement (terrain jetable). Valide le hardcode Python
-- (cf. constat API 2.0 : size lisible, geometries fines a confirmer par mesure).
function M.measure_entity(name, x, y, direction)
  if not player_mod.is_test_mode() then
    return json.encode({error = "measure_entity reserve au mode test (pose puis detruit)"})
  end
  local surface = game.surfaces.nauvis or game.surfaces[1]
  if not surface then return json.encode({error = "aucune surface"}) end
  local dir = defines.direction.north
  if type(direction) == "string" then
    dir = utils_entity.direction_from_name(direction)
  elseif type(direction) == "number" then
    dir = direction
  end
  local force = game.forces.player
  -- find_non_colliding_position pour eviter l'echec si (x,y) deja occupe.
  local pos = surface.find_non_colliding_position(name, {x = x, y = y}, 32, 0.5) or {x = x, y = y}
  local ok, ent = pcall(function()
    return surface.create_entity{name = name, position = pos, direction = dir, force = force}
  end)
  if not ok or not ent then
    return json.encode({name = name, error = "placement echoue", err = tostring(ent)})
  end
  local bb = ent.bounding_box
  local m = {
    name = name,
    x = r1(ent.position.x), y = r1(ent.position.y),
    size = {w = math.ceil(bb.right_bottom.x - bb.left_top.x), h = math.ceil(bb.right_bottom.y - bb.left_top.y)},
  }
  -- Proprietes de l'INSTANCE (accessibles au runtime, contrairement au prototype) :
  local _, pp = pcall(function() return ent.pickup_position end)
  if pp and pp.x then m.pickup_position = {x = r1(pp.x), y = r1(pp.y)} end
  local _, dp = pcall(function() return ent.drop_position end)
  if dp and dp.x then m.drop_position = {x = r1(dp.x), y = r1(dp.y)} end
  -- Proprietes du PROTOTYPE (pcall : echouent silencieusement si non exposées en 2.0).
  local _, bs = pcall(function() return ent.prototype.belt_speed end)
  if bs then m.belt_speed = bs end
  local _, mdr = pcall(function() return ent.prototype.mining_drill_radius end)
  if mdr then m.mining_drill_radius = mdr end
  -- S3b-fix : pcall retourne (ok, val|errmsg). Sur échec val=errmsg (truthy) -> il faut
  -- tester `ok` (1er retour) et non `val`, sinon on stocke le message d'erreur comme
  -- valeur (CONSTAT beacon : supply_area_distance lève sur instance -> errmsg stocké).
  local okmwd, mwd = pcall(function() return ent.prototype.max_circuit_wire_distance end)
  if okmwd and mwd ~= nil then m.max_wire_distance = mwd end
  local oksa, sad = pcall(function() return ent.prototype.supply_area_distance end)
  if oksa and sad ~= nil then m.supply_area_distance = sad end
  -- S2b-1 : fluidbox.get_prototype(i) — API Factorio 2.0 CORRECTE.
  -- CONSTAT API 2.0 : proto.fluid_boxes / ent.fluid_boxes / ent.output_fluid sont INEXISTANTS
  -- au runtime ("LuaEntity doesn't contain key fluid_boxes/output_fluid"). La source de
  -- vérité des positions fluide = ent.fluidbox.get_prototype(i) (instance posée), qui expose
  -- production_type + pipe_connections[j].positions (table de {x,y}, relatives au centre
  -- entité, canoniques NON rotées — invariantes par rotation car symétriques 4-fold).
  -- Chaque box = {production_type, pipe_connections:[{positions:[{x,y}], direction,
  -- flow_direction}]}. NOTE : en 2.0 les 3 outputs oil-refinery (b3/b4/b5) ont chacun 4
  -- positions (coins pour b3/b5 partagés, milieux pour b4 exclusif) — la séparation fine des
  -- 3 fluides nécessite un test de débit dédié (reporté S2b-3) ; can_place valide l'absence
  -- de collision (pattern S2a). pcall : si type sans fluidbox, champ absent (hardcode Python
  -- GEOMETRY_FIXTURE = fallback, source vérité back-compat S2a).
  local okfb, nfb = pcall(function() return #ent.fluidbox end)
  if okfb and nfb and nfb > 0 then
    local boxes = {}
    for i = 1, nfb do
      local okgp, gp = pcall(function() return ent.fluidbox.get_prototype(i) end)
      if okgp and gp then
        local box = {index = i}
        local okpt, pt = pcall(function() return gp.production_type end)
        if okpt and pt then box.production_type = pt end
        local conns = {}
        local okc, pcs = pcall(function() return gp.pipe_connections end)
        if okc and pcs then
          for _, pc in ipairs(pcs) do
            local c = {}
            local okps, ps = pcall(function() return pc.positions end)
            if okps and ps then
              local pl = {}
              for _, pos in ipairs(ps) do
                table.insert(pl, {x = r1(pos.x), y = r1(pos.y)})
              end
              c.positions = pl
            end
            local okd, d = pcall(function() return pc.direction end)
            if okd and d ~= nil then c.direction = d end
            local okfd, fd = pcall(function() return pc.flow_direction end)
            if okfd and fd then c.flow_direction = fd end
            table.insert(conns, c)
          end
        end
        box.pipe_connections = conns
        -- E3 : positions REELLES des ports, via l'INSTANCE posee.
        -- `pipe_connections[j].positions` (ci-dessus) vient du PROTOTYPE : ce sont les
        -- positions canoniques NON rotees, a charge du consommateur d'appliquer la
        -- rotation. `fluidbox.get_pipe_connections(i)` donne au contraire les positions
        -- absolues effectives de l'entite posee ; on les rend relatives a son centre REEL
        -- (create_entity ayant pu snapper la position demandee).
        --   port  = tuile du port sur l'entite
        --   voisin = tuile ou doit se trouver le tuyau qui s'y raccorde
        -- Mesure (facing north) : boiler -> eau aux deux cotes (+-1, +0.5), vapeur au
        -- nord (0, -0.5) ; offshore-pump -> sortie au centre, voisin (0, +1) ;
        -- steam-engine -> entrees aux deux extremites (0, +-2).
        local okpc, pcs2 = pcall(function() return ent.fluidbox.get_pipe_connections(i) end)
        if okpc and pcs2 then
          local ports = {}
          for _, c in ipairs(pcs2) do
            local p = {}
            pcall(function()
              p.x = r1(c.position.x - ent.position.x)
              p.y = r1(c.position.y - ent.position.y)
              p.tx = r1(c.target_position.x - ent.position.x)
              p.ty = r1(c.target_position.y - ent.position.y)
            end)
            if p.x then table.insert(ports, p) end
          end
          if #ports > 0 then box.ports = ports end
        end
        table.insert(boxes, box)
      end
    end
    if #boxes > 0 then m.fluid_boxes = boxes end
  end
  -- S2b-1 : offshore-pump output_fluid. CONSTAT API 2.0 : ent.output_fluid INEXISTANT au
  -- runtime. Le fluide produit (water) est fixé par le type offshore-pump ; hardcode Python
  -- (FLUID_RAW_RESOURCES) = source vérité, validé via can_place (rec 10). On tente aussi
  -- ent.get_fluid (fallback) au cas où l'API l'expose.
  local oko, of = pcall(function() return ent.output_fluid end)
  if oko and of then
    m.output_fluid = type(of) == "string" and of or of.name
  end
  -- S3b : beacon. Proto module_slots/distribution_effectivity (CONSTAT probable inaccessible,
  -- cf. fluid_boxes/max_energy_usage) + round-trip modules (ent.insert speed-module-3 puis
  -- lecture get_module_inventory, accessible sur l'instance). Valide que le beacon accepte
  -- les modules et expose son inventaire -> scan_factory peut vérifier les modules insérés.
  if ent.type == "beacon" then
    local b = {}
    local okms, ms = pcall(function() return ent.prototype.module_slots end)
    if okms and ms ~= nil then b.module_slots = ms end
    local okde, de = pcall(function() return ent.prototype.distribution_effectivity end)
    if okde and de ~= nil then b.distribution_effectivity = de end
    local okae, ae = pcall(function() return ent.prototype.allowed_effects end)
    if okae and ae ~= nil then
      local el = {}
      for k, _ in pairs(ae) do table.insert(el, k) end
      b.allowed_effects = el
    end
    if next(b) then m.beacon = b end
    -- Round-trip modules : insert 2 speed-module-3 (si disponible en inventaire force),
    -- lit get_module_inventory, confirme l'acceptation. Échec insert (item non craftable en
    -- test) -> modules vide, CONSTAT documenté (hardcode Python BEACON_FIXTURE.module_slots).
    local oki, _ = pcall(function() return ent.insert({name = "speed-module-3", count = 2}) end)
    if oki then
      local okmi, inv = pcall(function() return ent.get_module_inventory() end)
      if okmi and inv and inv.valid then
        local mods = {}
        for i = 1, #inv do
          local s = inv[i]
          if s and s.valid and s.valid_for_read then
            table.insert(mods, {name = s.name, count = s.count})
          end
        end
        m.modules = mods
      end
    end
  end
  ent.destroy()
  return json.encode(m)
end

-- get_technologies() : ce qui est acquis, ce qui est ouvert, et A QUEL PRIX.
--
-- L'agent butait sur des recettes verrouillees sans pouvoir dire pourquoi : il rendait
-- « ni ressource, ni recette accessible », ce qui melange « je ne sais pas faire » et
-- « ce n'est pas encore debloque ». Il faut donc exposer l'arbre.
--
-- Le prix n'est pas toujours en flacons. Dans Factorio 2.0, le debut de l'arbre est une
-- chaine de DECLENCHEURS (`research_trigger`) : `electronics` s'ouvre en fabriquant dix
-- plaques de cuivre, `automation-science-pack` en fabriquant un laboratoire. Une
-- technologie peut donc etre a portee immediate d'un agent qui sait fondre, sans
-- laboratoire ni flacon. Ne rendre que `research_unit_ingredients` afficherait « gratuit »
-- pour ces technologies-la et laisserait l'agent attendre un cout qui ne vient jamais.
--
-- `pret` dit que TOUS les prerequis sont acquis -- c'est-a-dire recherchable MAINTENANT,
-- la seule categorie sur laquelle une decision peut porter.

-- ===== Messages du joueur (chat du jeu) =====
-- Le joueur tape dans le chat, l'agent lit. C'est le seul canal par lequel un humain
-- s'adresse a lui EN COURS DE PARTIE : sans cela il faut arreter, changer sa skill et
-- tout relancer, ce qui coute une manche entiere pour une phrase.
--
-- La file se VIDE a la lecture. Un conseil repete a chaque tour deviendrait un bruit de
-- fond que l'agent apprendrait a ignorer -- on veut qu'il le lise une fois et agisse.

function M.push_message(joueur, texte)
  storage.fl = storage.fl or {}
  storage.fl.messages = storage.fl.messages or {}
  -- Plafond bas et volontaire : si personne ne lit, c'est que l'agent est occupe ailleurs,
  -- et vingt messages en attente ne l'aideront pas davantage que les cinq derniers.
  if #storage.fl.messages >= 20 then table.remove(storage.fl.messages, 1) end
  table.insert(storage.fl.messages, {
    tick = game.tick, joueur = joueur or "?", texte = texte or "",
  })
end

function M.read_messages()
  storage.fl = storage.fl or {}
  local file = storage.fl.messages or {}
  storage.fl.messages = {}
  return json.encode({messages = file})
end

function M.get_technologies(seulement_pretes)
  local force = player_mod.get_ai_force()
  local acquises, ouvertes = {}, {}
  for _, t in pairs(force.technologies) do
    if t.researched then
      table.insert(acquises, t.name)
    elseif t.enabled then
      local pret = true
      for _, p in pairs(t.prerequisites) do
        if not p.researched then pret = false break end
      end
      if pret or not seulement_pretes then
        local cout = {}
        for _, u in pairs(t.research_unit_ingredients) do
          table.insert(cout, {name = u.name, count = u.amount})
        end
        local row = {
          name = t.name,
          pret = pret,
          unites = t.research_unit_count,
          cout = cout,
          debloque = {},
        }
        -- Le declencheur, quand il y en a un : type ("craft-item", "mine-entity", ...),
        -- item ou entite vise, et combien. C'est ce qui rend la premiere marche
        -- franchissable sans laboratoire.
        local ok, tr = pcall(function() return t.prototype.research_trigger end)
        if ok and tr then
          local cible = nil
          if tr.item then cible = (type(tr.item) == "table" and tr.item.name) or tr.item end
          if not cible and tr.entity then cible = tr.entity end
          if not cible and tr.fluid then cible = tr.fluid end
          row.declencheur = {type = tostring(tr.type), cible = cible,
                             count = tr.count or 1}
        end
        -- Ce que la technologie OUVRE : sans cela, l'agent sait qu'il peut chercher,
        -- mais pas si cela lui sert a quelque chose.
        local ok2, effets = pcall(function() return t.prototype.effects end)
        if ok2 and effets then
          for _, e in pairs(effets) do
            if e.recipe then table.insert(row.debloque, e.recipe) end
          end
        end
        table.insert(ouvertes, row)
      end
    end
  end
  return json.encode({acquises = acquises, ouvertes = ouvertes,
                      en_cours = force.current_research and force.current_research.name or nil,
                      progres = force.research_progress})
end

return M