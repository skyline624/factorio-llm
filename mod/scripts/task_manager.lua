-- Gestionnaire de taches asynchrones pour factorio-llm (mode dual).
-- Portage du systeme de pathfinding d'airi-factorio (packages/autorio/src/control.ts
-- + task_manager.ts) + etats d'action (mine/place/move_items/wait/craft/research).
--
-- Mode dual (cf. player.lua) :
--   PRODUCTION  (joueur connecte) : physique reelle. WALKING = player.walking_state
--     (marche a vitesse reelle, contourne les obstacles), MINING = player.mining_state
--     (anime, completion via on_player_mined_entity), CRAFT = player.begin_crafting
--     (completion via on_player_crafted_item). Stuck/recompute actif (la marche peut bloquer).
--   TEST headless (pas de joueur) : actions simulees/instantanees. WALKING = teleport-step,
--     MINING = extraction+destroy, CRAFT = retrait/insertion. Pas de stuck detection
--     pertinente (la teleport ne se bloque pas), garde quand meme par defensivite.
--
-- Partie difficile portee fidelement (commune aux deux modes) :
--   - request_path avec la VRAIE collision_box / collision_mask du character
--   - handler on_script_path_request_finished : try_again_later / staging / candidats / abort
--   - suivi waypoint par waypoint (WAYPOINT_REACHED)
--   - selection reachability-aware (MAX_CANDIDATES espaces de MIN_CANDIDATE_SPACING)
--   - staging intermediaire (1/(stage+1) vers le but, max MAX_PATH_STAGES)
--
-- Une tache active a la fois (KISS) ; dispatcher par cur.type (ouvert/ferme : ajouter un
-- etat = ajouter une branche + une fabrique). Helpers DRY dans utils_entity / player.

local math_utils = require("scripts.utils_math")
local utils_entity = require("scripts.utils_entity")
local player_mod = require("scripts.player")

local M = {}

-- ===== Constantes de tuning (portees d'airi control.ts lignes 41-50, 600-610) =====

local WAYPOINT_REACHED = 0.35      -- distance (tiles) = waypoint atteint
local STEP_PER_TICK = 2.0          -- avance par teleport le long du chemin (mode test)
local STUCK_PROGRESS_EPS = 0.03    -- deplacement < ça = pas de progres (prod)
local STUCK_TICKS = 45             -- ticks consecutifs sans progres = bloque (prod)
local MAX_PATH_RECOMPUTES = 6     -- apres ça, abandon
local DESTROY_AFTER_RECOMPUTES = 2 -- apres ça, detruire arbres/rocks autour
local OBSTACLE_CLEAR_RADIUS = 2.5
local MAX_PATH_STAGES = 5          -- subdivisions staging avant abandon
local MAX_CANDIDATES = 8            -- candidats (patches distincts) a essayer
local MIN_CANDIDATE_SPACING = 16    -- espacement min entre candidats (tiles)
local MAX_TASK_TICKS = 6000        -- garde-fou global (100s @ 60 tps)
local MINING_REACH = 5             -- rayon de recherche cible de minage (prod)
local TEST_MINING_RADIUS = 12      -- rayon de recherche cible de minage (test : on atterrit sur le bord du patch)
local MINING_STALL_MAX = 90        -- ticks sans progres minage = abort
local MOVE_RADIUS = 32             -- rayon de recherche entites pour move_items
local MOVE_AT_RADIUS = 1.5         -- rayon de recherche entite pour move_items_at
local TARGET_AT_RADIUS = 1.5       -- rayon de ciblage d'une entite deja posee (E2)

-- ===== Etats =====
local WALKING = "walking"
local MINING = "mining"
local PLACING_AT = "placing_at"
local MOVING_ITEMS = "moving_items"
local WAITING = "waiting"
local CRAFTING = "crafting"
local RESEARCHING = "researching"
-- E2 : agir sur une entite DEJA posee. Sans ces trois etats, le mod ne sait que
-- construire : une erreur de pose est definitive et aucune machine ne peut recevoir
-- de recette (donc aucune automatisation au-dela des fours).
local REMOVING_AT = "removing_at"
local ROTATING_AT = "rotating_at"
local SETTING_RECIPE = "setting_recipe"
local EMPTYING_AT = "emptying_at"
local ENABLING_AT = "enabling_at"

-- ===== Storage =====

-- Arret explicite de la marche (prod). Idempotent. Defini ici (avant clear/_complete)
-- car clear() et le garde-fou timeout l'appellent.
local function stop_walking()
  if player_mod.is_test_mode() then return end
  local p = player_mod.get_ai_player()
  if p then p.walking_state = {walking = false} end
end

function M.init()
  storage.fl = storage.fl or {}
  storage.fl.tasks = {}
  storage.fl.current = nil           -- table de parametres de la tache active (avec .type)
  storage.fl.current_started_tick = nil
  storage.fl.last_result = nil       -- {action, ok, detail, seq}
  storage.fl.completion_seq = storage.fl.completion_seq or 0  -- incremente a chaque completion
end

function M.queue(task)
  if not storage.fl.tasks then M.init() end
  table.insert(storage.fl.tasks, task)
  -- Demarrage immediat si idle (comme airi add_task : si la file passe a 1, on lance).
  if not storage.fl.current then
    M._start_next()
  end
  return true
end

function M._start_next()
  if storage.fl.current then return end -- verrou : une tache a la fois
  if #storage.fl.tasks == 0 then
    storage.fl.current = nil
    return
  end
  storage.fl.current = table.remove(storage.fl.tasks, 1)
  storage.fl.current_started_tick = game.tick
  log("[fl] tache demarree: " .. tostring(storage.fl.current.type))
end

function M._complete(detail, ok)
  local cur = storage.fl.current
  local label = cur and cur.type or "?"
  -- Sequence de completion : permet a l'orchestrateur de distinguer la completion de la
  -- NOUVELLE tache de celle de la precedente (race condition entre enqueue et dispatch).
  storage.fl.completion_seq = (storage.fl.completion_seq or 0) + 1
  storage.fl.last_result = {action = label, ok = ok, detail = detail, seq = storage.fl.completion_seq}
  log(string.format("[fl] tache terminee: %s ok=%s detail=%s (seq=%d)", label, tostring(ok), tostring(detail), storage.fl.completion_seq))
  storage.fl.current = nil
  storage.fl.current_started_tick = nil
  -- Demarrage de la suivante au prochain tick (via _start_next dans tick).
end

function M.clear()
  storage.fl = storage.fl or {}
  storage.fl.completion_seq = storage.fl.completion_seq or 0
  stop_walking()
  storage.fl.tasks = {}
  storage.fl.current = nil
  storage.fl.current_started_tick = nil
end

function M.status()
  local fl = storage.fl
  if not fl or not fl.tasks then
    return {state = "uninitialized", seq = (fl and fl.completion_seq) or 0}
  end
  local seq = fl.completion_seq or 0
  if fl.current then
    local cur = fl.current
    return {
      state = "busy",
      seq = seq,
      task = {type = cur.type, entity_name = cur.entity_name, goal_position = cur.goal_position},
      started_tick = fl.current_started_tick,
      elapsed = game.tick - (fl.current_started_tick or game.tick),
      calculating_path = cur.calculating_path == true,
      path_remaining = cur.path and #cur.path or 0,
      recompute_count = cur.recompute_count or 0,
    }
  end
  return {
    state = "idle",
    seq = seq,
    last_result = fl.last_result,
    pending = #fl.tasks,
  }
end

-- ===== Pathfinding : emission de la requete (port lignes ~860-880) =====

-- Construit la liste des candidats (positions) triee nearest-first, avec espacement.
local function build_candidates(char, params)
  if params.goal_position then
    -- walk_to_position : un seul candidat.
    return {params.goal_position}
  end
  local ents = utils_entity.find_target_entities(char.surface, char.position, params.search_radius, params.entity_name)
  if #ents == 0 then return nil end
  table.sort(ents, function(a, b)
    return math_utils.distance_sq(char.position, a.position) < math_utils.distance_sq(char.position, b.position)
  end)
  local picked = {}
  for _, e in ipairs(ents) do
    if #picked >= MAX_CANDIDATES then break end
    local p = {x = e.position.x, y = e.position.y}
    local distinct = true
    for _, q in ipairs(picked) do
      if math_utils.distance(p, q) < MIN_CANDIDATE_SPACING then distinct = false break end
    end
    if distinct then table.insert(picked, p) end
  end
  return picked
end

local function request_path_to(char, params, goal)
  -- start : position libre pres du character (airi : find_non_colliding_position char 10 0.5).
  local start = char.surface.find_non_colliding_position("character", char.position, 10, 0.5)
  if not start then start = {x = char.position.x, y = char.position.y} end

  local proto = char.prototype
  local bbox = proto.collision_box
  local mask = proto.collision_mask

  char.surface.request_path({
    bounding_box = bbox,
    collision_mask = mask,
    radius = 2,
    start = start,
    goal = goal,
    force = char.force,
    entity_to_ignore = char,
    pathfind_flags = {
      cache = false,
      no_break = true,
      prefer_straight_paths = false,
      allow_paths_through_own_entities = false,
    },
  })
  params.calculating_path = true
  params.final_goal = params.final_goal or goal
  params.target_position = goal
  log(string.format("[fl] requete path vers (%.1f, %.1f)", goal.x, goal.y))
end

-- ===== Suivi du chemin =====

-- Mode test : avance le long du prochain waypoint par petits teleports (headless-safe).
-- Retourne true si le chemin est entierement consomme, "blocked" si la tuile est occupee.
local function follow_path_teleport(char, params)
  local path = params.path
  if not path or #path == 0 then return true end
  local wp = path[1].position
  local d = math_utils.distance(char.position, wp)
  if d < WAYPOINT_REACHED then
    table.remove(path, 1)
    if #path == 0 then return true end
    return false
  end
  local step = math.min(STEP_PER_TICK, d)
  local nx = char.position.x + (wp.x - char.position.x) / d * step
  local ny = char.position.y + (wp.y - char.position.y) / d * step
  local landed = char.surface.find_non_colliding_position("character", {x = nx, y = ny}, 4, 0.25)
  if landed then
    char.teleport(landed)
  else
    return "blocked"
  end
  return false
end

-- Mode prod : marche reelle via walking_state (8-way direction). Retourne true si chemin
-- consomme. La completion de l'arret (walking_state=false) est faite par stop_walking().
local function follow_path_walking(char, params)
  local path = params.path
  if not path or #path == 0 then return true end
  local wp = path[1].position
  local d = math_utils.distance(char.position, wp)
  if d < WAYPOINT_REACHED then
    table.remove(path, 1)
    if #path == 0 then return true end
    return false
  end
  local player = player_mod.get_ai_player()
  if player then
    player.walking_state = {walking = true, direction = utils_entity.get_direction(char.position, wp)}
  end
  return false
end

-- Chemin consomme : si waypoint intermediaire (staging), on re-demandera vers la cible
-- reelle (retourne true) ; sinon c'est l'arrivee (retourne false -> complete).
local function on_path_consumed(params)
  if params.staged_goal then
    log("[fl] waypoint intermediaire atteint, continue vers la cible")
    params.staged_goal = nil
    params.path = nil
    params.last_position = nil
    params.stuck_ticks = 0
    params.recompute_count = 0
    params.path_stage = 0
    return true
  end
  return false
end

-- ===== Etat WALKING (port de state_walking_to_entity lignes 672-880) =====

local function state_walking(char, params)
  if params.calculating_path then return end

  if params.path and #params.path > 0 then
    if player_mod.is_test_mode() then
      -- test : teleport-step (pas de stuck detection pertinente)
      local res = follow_path_teleport(char, params)
      if res == "blocked" then
        params.stuck_ticks = STUCK_TICKS -- force un recompute au prochain tick
        return
      elseif res == true then
        if not on_path_consumed(params) then M._complete("arrive", true) end
      end
      return
    end

    -- prod : walking_state + stuck detection
    local moved = params.last_position and math_utils.distance(char.position, params.last_position) or 999
    params.last_position = {x = char.position.x, y = char.position.y}
    params.stuck_ticks = (params.stuck_ticks or 0)
    if moved < STUCK_PROGRESS_EPS then
      params.stuck_ticks = params.stuck_ticks + 1
    else
      params.stuck_ticks = 0
    end

    if params.stuck_ticks >= STUCK_TICKS then
      params.recompute_count = (params.recompute_count or 0) + 1
      if params.recompute_count > MAX_PATH_RECOMPUTES then
        stop_walking()
        M._complete("bloque (pas de chemin apres " .. MAX_PATH_RECOMPUTES .. " recomputes)", false)
        return
      end
      if params.recompute_count >= DESTROY_AFTER_RECOMPUTES then
        local cleared = utils_entity.clear_obstacles_near(char.surface, char.position, OBSTACLE_CLEAR_RADIUS)
        if cleared > 0 then log("[fl] degage " .. cleared .. " obstacle(s)") end
      end
      log("[fl] bloque, recompute route (" .. params.recompute_count .. "/" .. MAX_PATH_RECOMPUTES .. ")")
      stop_walking()
      params.path = nil
      params.last_position = nil
      params.stuck_ticks = 0
      -- tombe dans la branche de demande de chemin ci-dessous
    else
      local res = follow_path_walking(char, params)
      if res == true then
        if not on_path_consumed(params) then
          stop_walking()
          M._complete("arrive", true)
        end
      end
      return
    end
  end

  -- Construction des candidats (une seule fois).
  if not params.candidates then
    local picked = build_candidates(char, params)
    if not picked or #picked == 0 then
      M._complete("aucune cible trouvee", false)
      return
    end
    params.candidates = picked
    params.candidate_index = 0
    log("[fl] " .. #picked .. " candidat(s) cible")
  end

  local candidate = params.candidates[(params.candidate_index or 0) + 1]
  if not candidate then
    M._complete("aucune cible atteignable", false)
    return
  end

  -- (re)emission de la requete de chemin.
  if not params.calculating_path and not params.path then
    local goal = params.staged_goal or candidate
    params.final_goal = params.final_goal or candidate
    request_path_to(char, params, goal)
  end
end

-- ===== Handler de fin de calcul de chemin (port lignes 437-508) =====

function M.on_path_finished(event)
  local params = storage.fl.current
  if not params or params.type ~= WALKING then return end

  if not event.path then
    if event.try_again_later then
      log("[fl] pathfinder occupe (try_again_later), retry au prochain tick")
      params.calculating_path = false
      params.path = nil
      return
    end

    local stage = (params.path_stage or 0) + 1
    local goal = params.final_goal or params.target_position

    -- 1) staging : viser un waypoint intermediaire plus proche.
    if goal and stage <= MAX_PATH_STAGES then
      local char = player_mod.get_ai_entity()
      local start = char and char.position or {x = 0, y = 0}
      local frac = 1 / (stage + 1)
      params.path_stage = stage
      params.staged_goal = {
        x = start.x + (goal.x - start.x) * frac,
        y = start.y + (goal.y - start.y) * frac,
      }
      params.calculating_path = false
      params.path = nil
      log("[fl] path echoue, staging " .. stage .. "/" .. MAX_PATH_STAGES)
      return
    end

    -- 2) prochain candidat (reachability-aware).
    local next_index = (params.candidate_index or 0) + 1
    if params.candidates and next_index < #params.candidates then
      params.candidate_index = next_index
      params.path_stage = 0
      params.staged_goal = nil
      params.calculating_path = false
      params.path = nil
      log("[fl] cible injoignable, candidat suivant " .. (next_index + 1) .. "/" .. #params.candidates)
      return
    end

    -- 3) abandon.
    stop_walking()
    M._complete("aucune cible atteignable apres tous les candidats", false)
    return
  end

  params.path = event.path
  params.calculating_path = false
  params.path_stage = 0
  local n = #event.path
  local p1 = event.path[1].position
  local pN = event.path[n].position
  -- En 2.0, le 1er nœud du chemin = point de depart (on y est deja) : on le retire,
  -- sinon le character vise son propre depart et ne consomme jamais aucun waypoint.
  table.remove(params.path, 1)
  log(string.format("[fl] chemin recu, %d wp (depart retire -> %d) ; depart=(%.1f,%.1f) cible=(%.1f,%.1f)", n, #params.path, p1.x, p1.y, pN.x, pN.y))
  -- Edge case : chemin trivial (0 wp apres retrait du depart) = on est deja a la cible.
  -- Sans ça, la tache reste dans un etat mort (path={} non-nil mais vide : aucune
  -- re-demande, aucune completion) -> timeout 100s. On complete donc en arrivee.
  if #params.path == 0 then
    if not on_path_consumed(params) then
      stop_walking()
      M._complete("arrive (deja a la cible)", true)
    end
  end
end

-- ===== Etat MINING =====

-- Lance le minage reel (prod) sur une position cible.
local function start_mining(player, target_pos)
  player.update_selected_entity(target_pos)
  player.mining_state = {mining = true, position = target_pos}
end

-- Mine une entite dans l'inventaire IA (mode test) via l'API native entity.mine.
-- mine() n'accepte qu'un inventaire de script ou d'entite basique ; on passe par un
-- inventaire tampon (cf. airi) puis on transfere vers l'inventaire du character.
local function mine_into(target, dst_inv)
  local tmp = game.create_inventory(50)
  local ok = target.mine({inventory = tmp, force = true, raise_destroyed = true})
  if ok and dst_inv then
    -- get_contents() renvoie un tableau [{name,count}] en 2.0.
    for _, it in ipairs(tmp.get_contents()) do
      if it.name then dst_inv.insert({name = it.name, count = it.count}) end
    end
  end
  tmp.destroy()
  return ok
end

local function state_mining(char, params)
  if player_mod.is_test_mode() then
    -- simulation : mine une entite par tick via entity.mine (native, items reels).
    local ents = utils_entity.find_target_entities(char.surface, char.position, TEST_MINING_RADIUS, params.entity_name)
    if #ents == 0 then
      params.stall_ticks = (params.stall_ticks or 0) + 1
      if params.stall_ticks > MINING_STALL_MAX then
        M._complete("cible hors portee", false)
      end
      return
    end
    params.stall_ticks = 0
    -- La plus proche LIBRE, et non la plus proche tout court : sinon une foreuse posee
    -- sur le bord du gisement suffit a bloquer un minage manuel que rien n'empechait.
    local target, couvertes = utils_entity.nearest_free_entity(
      char.surface, char.force, char.position, ents, 6)
    if not target then
      M._complete("minerai sous foreuse (" .. couvertes .. " tuile(s) couverte(s), "
                  .. "aucune libre a portee)", false)
      return
    end
    local inv = player_mod.get_ai_inventory()
    if mine_into(target, inv) then
      params.count = params.count - 1
      if params.count <= 0 then
        M._complete("mine (simule)", true)
      end
    else
      params.stall_ticks = (params.stall_ticks or 0) + 1
      if params.stall_ticks > MINING_STALL_MAX then
        M._complete("mine echec", false)
      end
    end
    return
  end

  -- prod : mining_state anime, completion via on_player_mined_entity.
  local player = player_mod.get_ai_player()
  if not player then return end
  if player.mining_state.mining then
    params.stall_ticks = 0
    return
  end
  params.stall_ticks = (params.stall_ticks or 0) + 1
  if params.stall_ticks > MINING_STALL_MAX then
    M._complete("cible hors portee / introuvable", false)
    return
  end
  -- (re)trouve la cible la plus proche (params.position est vide apres chaque mine).
  local target_pos = params.position
  if not target_pos then
    local ents = utils_entity.find_target_entities(char.surface, char.position, MINING_REACH, params.entity_name)
    if #ents == 0 then return end -- stall grandit
    local target, couvertes = utils_entity.nearest_free_entity(
      char.surface, char.force, char.position, ents, 6)
    if not target then
      M._complete("minerai sous foreuse (" .. couvertes .. " tuile(s) couverte(s), "
                  .. "aucune libre a portee)", false)
      return
    end
    target_pos = target.position
    params.position = {x = target_pos.x, y = target_pos.y}
  end
  start_mining(player, target_pos)
end

-- Appele par control.lua sur on_player_mined_entity (prod) : decremente le compte restant.
function M.on_player_mined(event)
  local cur = storage.fl.current
  if not cur or cur.type ~= MINING then return end
  cur.count = cur.count - 1
  cur.position = nil -- re-find la prochaine cible au prochain tick
  if cur.count <= 0 then
    M._complete("mine", true)
  end
end

-- ===== Helpers partages : portee et ciblage par position (E2) =====

-- Portee du joueur en PRODUCTION. En mode test il n'y en a aucune : le mode test
-- MASQUE cette contrainte, donc toute primitive doit etre validee en production.
-- kind = "build" (poser -> build_distance) ou "reach" (miner/tourner/regler la
-- recette -> reach_distance, la portee d'interaction).
local function out_of_reach(char, target, kind)
  if player_mod.is_test_mode() then return false end
  local p = player_mod.get_ai_player()
  local max_d
  if kind == "build" then
    max_d = (p and (p.build_distance + 2)) or 10
  else
    max_d = (p and (p.reach_distance + 2)) or 10
  end
  return math_utils.distance(char.position, target) > max_d
end

-- Entite visee par une action a la position (x, y).
--   name fourni  -> filtre par nom (ou type, cf. utils_entity.find_target_entities)
--   name absent  -> entite de NOTRE force la plus proche ; a defaut une entite
--                   minable (arbre / rocher : degager un emplacement est legitime).
-- Le character est toujours exclu (on ne se mine pas soi-meme).
local function entity_at(char, x, y, name)
  local target = {x = x, y = y}
  local ents = utils_entity.find_target_entities(char.surface, target, TARGET_AT_RADIUS, name)
  local best, best_d, spare, spare_d = nil, nil, nil, nil
  for _, e in ipairs(ents) do
    if e.valid and e ~= char and e.type ~= "character" then
      local d = math_utils.distance(target, e.position)
      if e.force == char.force then
        if not best_d or d < best_d then best, best_d = e, d end
      elseif e.minable then
        if not spare_d or d < spare_d then spare, spare_d = e, d end
      end
    end
  end
  return best or spare
end

-- Applique les options de pose a l'entite creee (E2). Chaque option est independante
-- et son echec est CONSIGNE sans annuler la pose : une entite posee mais mal reglee
-- se corrige (set_recipe_at / rotate_entity_at), une pose annulee laisse un trou dans
-- la chaine. Retourne la liste des notes a joindre au detail de la tache.
local function apply_place_options(ent, opts, inv)
  local notes = {}
  if not opts then return notes end

  -- Recette : sans elle un assembleur ne produit RIEN.
  if opts.recipe then
    local ok, ejected = pcall(function() return ent.set_recipe(opts.recipe) end)
    if ok then
      -- set_recipe rend les ingredients de l'ancienne recette : ne pas les perdre.
      if ejected and inv then
        for _, st in pairs(ejected) do pcall(function() inv.insert(st) end) end
      end
      table.insert(notes, "recipe=" .. tostring(opts.recipe))
    else
      table.insert(notes, "recipe KO")
    end
  end

  -- Underground belt : le sens est donne A LA CREATION (champ `type` de create_entity)
  -- et n'est PAS reglable ensuite (belt_to_ground_type est en lecture seule). On ne
  -- fait donc pas confiance a la creation : on relit, et on corrige par rotation.
  if opts.ug_type then
    local got = nil
    pcall(function() got = ent.belt_to_ground_type end)
    if got and got ~= opts.ug_type then
      pcall(function() ent.rotate({}) end)
      pcall(function() got = ent.belt_to_ground_type end)
    end
    table.insert(notes, "ug_type=" .. tostring(got))
  end

  -- Splitter : priorites d'entree/sortie (RW, donc reglables apres la pose).
  if opts.priority_in then
    local ok = pcall(function() ent.splitter_input_priority = opts.priority_in end)
    table.insert(notes, "prio_in=" .. tostring(opts.priority_in) .. (ok and "" or " KO"))
  end
  if opts.priority_out then
    local ok = pcall(function() ent.splitter_output_priority = opts.priority_out end)
    table.insert(notes, "prio_out=" .. tostring(opts.priority_out) .. (ok and "" or " KO"))
  end

  -- Modules : preleves sur l'inventaire IA, comme l'entite elle-meme.
  if opts.modules then
    local minv = nil
    pcall(function() minv = ent.get_module_inventory() end)
    if minv and minv.valid then
      for name, count in pairs(opts.modules) do
        local have = (inv and inv.get_item_count(name)) or 0
        local n = math.min(have, count)
        if n > 0 then
          local placed = minv.insert({name = name, count = n})
          if placed > 0 and inv then inv.remove({name = name, count = placed}) end
          table.insert(notes, string.format("module %s x%d", name, placed))
        else
          table.insert(notes, "module " .. name .. " absent de l'inventaire")
        end
      end
    else
      table.insert(notes, "pas d'emplacement de module")
    end
  end

  -- Carburant a la pose : supprime le trou d'autonomie entre la pose et
  -- l'alimentation, et un aller-retour move_items.
  if opts.fuel and (opts.fuel_count or 0) > 0 then
    local finv = nil
    pcall(function() finv = ent.get_fuel_inventory() end)
    if finv and finv.valid then
      local have = (inv and inv.get_item_count(opts.fuel)) or 0
      local n = math.min(have, opts.fuel_count)
      if n > 0 then
        local placed = finv.insert({name = opts.fuel, count = n})
        if placed > 0 and inv then inv.remove({name = opts.fuel, count = placed}) end
        table.insert(notes, string.format("fuel %s x%d", opts.fuel, placed))
      end
    end
  end
  return notes
end

-- ===== Etat PLACING_AT (synchrone, 1 tick) =====

local function state_placing_at(char, params)
  local target = {x = params.x, y = params.y}
  if out_of_reach(char, target, "build") then
    M._complete("walk closer first", false)
    return
  end
  local surface = char.surface
  local dir = params.direction or defines.direction.north
  local ok = surface.can_place_entity({
    name = params.entity_name, position = target, direction = dir,
    force = char.force, build_check_type = defines.build_check_type.manual,
  })
  if not ok then
    M._complete("cannot place here", false)
    return
  end
  local create_args = {
    name = params.entity_name, position = target, direction = dir,
    force = char.force, raise_built = true,
  }
  -- underground-belt / loader : le sens (input/output) se donne A LA CREATION.
  local opts = params.opts
  if opts and opts.ug_type then create_args.type = opts.ug_type end
  if not player_mod.is_test_mode() then
    local p = player_mod.get_ai_player()
    if p then create_args.player = p end
  end
  local ent = surface.create_entity(create_args)
  if not ent then
    M._complete("create_entity failed", false)
    return
  end
  local inv = player_mod.get_ai_inventory()
  if inv then inv.remove({name = params.entity_name, count = 1}) end
  utils_entity.push_character_clear(char, ent)
  local notes = apply_place_options(ent, opts, inv)
  local detail = string.format("place %s at (%.1f, %.1f)", params.entity_name, target.x, target.y)
  if #notes > 0 then detail = detail .. " [" .. table.concat(notes, ", ") .. "]" end
  log("[fl] [RESULT] place_entity_at " .. detail)
  M._complete(detail, true)
end

-- ===== Etats REMOVING_AT / ROTATING_AT / SETTING_RECIPE (synchrones, 1 tick) =====
-- Les trois agissent sur une entite DEJA posee et partagent la meme sequence :
-- verifier la portee -> cibler l'entite -> agir -> RELIRE le resultat. On ne croit
-- jamais l'affectation : le succes rapporte est celui qu'on a relu dans le jeu.

-- Retire l'entite a une position. `char.mine_entity` mine COMME LE JOUEUR : les
-- produits tombent dans l'inventaire IA, alors que destroy() les perdrait.
-- Sans cette primitive, une erreur de construction est definitive.
local function state_removing_at(char, params)
  local target = {x = params.x, y = params.y}
  if out_of_reach(char, target, "reach") then
    M._complete("walk closer first", false)
    return
  end
  local ent = entity_at(char, params.x, params.y, params.entity_name)
  if not ent then
    M._complete("aucune entite a cette position", false)
    return
  end
  local name = ent.name
  local ok, mined = pcall(function() return char.mine_entity(ent, true) end)
  if ok and mined then
    M._complete(string.format("retire %s at (%.1f, %.1f)", name, target.x, target.y), true)
    return
  end
  -- Repli 1 : mine{} vers l'inventaire IA (entites non minables par le character).
  local inv = player_mod.get_ai_inventory()
  local ok2, mined2 = pcall(function()
    return ent.mine({inventory = inv, force = true, raise_destroyed = true})
  end)
  if ok2 and mined2 then
    M._complete(string.format("retire %s at (%.1f, %.1f) (mine)", name, target.x, target.y), true)
    return
  end
  -- Repli 2 : destruction seche. Les items sont perdus -> on le DIT dans le detail.
  local ok3 = pcall(function() return ent.destroy({raise_destroy = true}) end)
  if not ok3 then
    M._complete("retrait impossible: " .. name, false)
    return
  end
  M._complete(string.format("detruit %s at (%.1f, %.1f) (items perdus)", name, target.x, target.y), true)
end

-- Oriente une entite deja posee. La direction est ABSOLUE (pas un cran de rotation) :
-- le planner connait la direction cible, il ne compte pas les crans.
local function state_rotating_at(char, params)
  local target = {x = params.x, y = params.y}
  if out_of_reach(char, target, "reach") then
    M._complete("walk closer first", false)
    return
  end
  local ent = entity_at(char, params.x, params.y, params.entity_name)
  if not ent then
    M._complete("aucune entite a cette position", false)
    return
  end
  local dir = params.direction or defines.direction.north
  local name = ent.name
  if not pcall(function() ent.direction = dir end) then
    M._complete("rotation refusee: " .. name, false)
    return
  end
  local got = nil
  pcall(function() got = ent.direction end)
  M._complete(string.format("oriente %s -> %s at (%.1f, %.1f)", name, tostring(got), target.x, target.y),
    got == dir)
end

-- Regle la recette d'une machine. C'est le verrou de toute automatisation au-dela
-- des fours : un assembleur sans recette ne produit rien.
local function state_setting_recipe(char, params)
  local target = {x = params.x, y = params.y}
  if out_of_reach(char, target, "reach") then
    M._complete("walk closer first", false)
    return
  end
  local ent = entity_at(char, params.x, params.y, params.entity_name)
  if not ent then
    M._complete("aucune entite a cette position", false)
    return
  end
  local inv = player_mod.get_ai_inventory()
  local ok, ejected = pcall(function() return ent.set_recipe(params.recipe) end)
  if not ok then
    M._complete("set_recipe refuse: " .. ent.name, false)
    return
  end
  -- La machine rend les ingredients de l'ancienne recette : ne pas les perdre.
  if ejected and inv then
    for _, st in pairs(ejected) do pcall(function() inv.insert(st) end) end
  end
  local got = nil
  pcall(function() local r = ent.get_recipe() got = r and r.name or nil end)
  M._complete(string.format("recipe %s sur %s at (%.1f, %.1f)", tostring(got), ent.name, target.x, target.y),
    got == params.recipe)
end

-- Vide l'inventaire de SORTIE d'une machine vers l'inventaire IA. Une sortie pleine
-- arrete la machine aussi surement qu'un reservoir vide, et le depannage est le meme :
-- on enleve ce qui bouche. Contrairement a move_items, on n'a pas besoin de NOMMER le
-- produit -- ce qu'on ignore justement quand on decouvre une machine a l'arret.
local function state_emptying_at(char, params)
  local target = {x = params.x, y = params.y}
  if out_of_reach(char, target, "reach") then
    M._complete("walk closer first", false)
    return
  end
  local ent = entity_at(char, params.x, params.y, params.entity_name)
  if not ent then
    M._complete("aucune entite a cette position", false)
    return
  end
  local out = nil
  pcall(function() out = ent.get_output_inventory() end)
  if not out then
    M._complete(ent.name .. " n'a pas d'inventaire de sortie", false)
    return
  end
  local inv = player_mod.get_ai_inventory()
  local pris = 0
  for _, st in pairs(out.get_contents()) do
    local n = st.count
    if inv then
      local ok, mis = pcall(function() return inv.insert({name = st.name, count = n}) end)
      if ok and mis and mis > 0 then
        out.remove({name = st.name, count = mis})
        pris = pris + mis
      end
    end
  end
  -- On RELIT la sortie : un transfert partiel (inventaire IA plein) n'est pas un succes.
  local reste = 0
  for _, st in pairs(out.get_contents()) do reste = reste + st.count end
  M._complete(string.format("vide %d item(s) de %s, reste %d", pris, ent.name, reste),
    pris > 0)
end

-- Reactive une entite desactivee. `active = false` arrete une machine sans qu'aucun
-- ingredient ne manque : le statut le dit, mais aucune autre reparation n'y changerait
-- rien.
local function state_enabling_at(char, params)
  local target = {x = params.x, y = params.y}
  if out_of_reach(char, target, "reach") then
    M._complete("walk closer first", false)
    return
  end
  local ent = entity_at(char, params.x, params.y, params.entity_name)
  if not ent then
    M._complete("aucune entite a cette position", false)
    return
  end
  if not pcall(function() ent.active = true end) then
    M._complete("reactivation refusee: " .. ent.name, false)
    return
  end
  local actif = nil
  pcall(function() actif = ent.active end)
  M._complete(string.format("%s actif=%s", ent.name, tostring(actif)), actif == true)
end

-- ===== Etat MOVING_ITEMS (synchrone, 1 tick) =====

-- Nombre max d'inventaires d'une entite (pcall : certaines entites n'exposent pas la methode).
local function max_inventory_index(entity)
  local ok, n = pcall(function() return entity.get_max_inventory_index() end)
  if ok and n then return n end
  return 0
end

local function state_moving_items(char, params)
  local surface = char.surface
  local force = char.force
  local entities
  if params.position then
    -- move_items_at : entite unique a la position (rayon 1.5).
    entities = utils_entity.find_target_entities(surface, params.position, MOVE_AT_RADIUS, params.entity_name)
    local f = {}
    for _, e in ipairs(entities) do
      if e.valid and e.force == force then table.insert(f, e) end
    end
    entities = f
  else
    -- move_items : entites du nom rayon 32.
    local ents = utils_entity.find_target_entities(surface, char.position, MOVE_RADIUS, params.entity_name)
    entities = {}
    for _, e in ipairs(ents) do
      if e.valid and e.force == force then table.insert(entities, e) end
    end
  end
  if #entities == 0 then
    M._complete("aucune entite cible", false)
    return
  end

  local player_inv = player_mod.get_ai_inventory()
  if not player_inv then M._complete("aucun inventaire IA", false) return end

  local max_count = params.max_count
  local moved = 0
  for _, e in ipairs(entities) do
    if moved >= max_count then break end
    if e.valid then
      local max_inv = max_inventory_index(e)
      for i = 1, max_inv do
        if moved >= max_count then break end
        local inv = e.get_inventory(i)
        if inv and inv.valid then
          local want = max_count - moved
          if params.to_entity then
            -- joueur -> entite : on puise dans l'inventaire IA et on insert dans l'entite.
            -- PAS de garde sur le contenu de l'entite (sinon impossible de la remplir).
            if inv.can_insert({name = params.item_name, count = want}) then
              local have = player_inv.get_item_count(params.item_name)
              if have > 0 then
                local take = math.min(have, want)
                local taken = player_inv.remove({name = params.item_name, count = take})
                if taken > 0 then
                  local inserted = inv.insert({name = params.item_name, count = taken})
                  if inserted < taken then
                    player_inv.insert({name = params.item_name, count = taken - inserted}) -- rollback
                  end
                  moved = moved + inserted
                end
              end
            end
          else
            -- entite -> joueur : on puise dans l'inventaire de l'entite (avail > 0 requis).
            local avail = inv.get_item_count(params.item_name)
            if avail > 0 then
              local take = math.min(avail, want)
              if player_inv.can_insert({name = params.item_name, count = take}) then
                local taken = inv.remove({name = params.item_name, count = take})
                if taken > 0 then
                  local inserted = player_inv.insert({name = params.item_name, count = taken})
                  if inserted < taken then
                    inv.insert({name = params.item_name, count = taken - inserted}) -- rollback
                  end
                  moved = moved + inserted
                end
              end
            end
          end
        end
      end
    end
  end
  M._complete(string.format("moved %d %s", moved, params.item_name), true)
end

-- ===== Etat WAITING =====

local function state_waiting(params)
  params.remaining_ticks = params.remaining_ticks - 1
  if params.remaining_ticks <= 0 then
    M._complete("wait", true)
  end
end

-- ===== Etat CRAFTING =====

-- Valide une recette + la disponibilite des ingredients. Retourne ok, detail, per, crafts.
local function check_can_craft(inv, item_name, count)
  local force = game.forces.player
  local recipe = force.recipes[item_name]
  if not recipe then return false, "recette inexistante: " .. item_name end
  if not recipe.enabled then return false, "recette verrouillee: " .. item_name end
  local per = 0
  for _, p in ipairs(recipe.products) do
    if p.name == item_name then per = per + (p.amount or p.amount_min or 1) end
  end
  if per <= 0 then return false, "produit non fabrique par la recette" end
  local crafts = math.ceil(count / per)
  if inv then
    for _, ing in ipairs(recipe.ingredients) do
      if ing.type ~= "fluid" then
        local need = ing.amount * crafts
        local have = inv.get_item_count(ing.name)
        if have < need then
          return false, string.format("manque %s: %d/%d", ing.name, have, need), per, crafts
        end
      end
    end
  end
  return true, nil, per, crafts
end

-- Simulation du craft (test) : retrait des ingredients + insertion des produits.
-- Simule un craft en mode test : consomme les ingredients, insere le produit, ET
-- DECLARE LES FLUX.
--
-- Les flux ne sont pas un detail comptable. Un craft reel consomme et PRODUIT, et cette
-- production est ce que le jeu observe : dans Factorio 2.0 les premieres technologies
-- se debloquent sur un `research_trigger` de type `craft-item`. Sans declaration, le
-- mode test deplace des objets d'une case a l'autre sans que rien ne l'ait jamais
-- « fabrique » -- le banc headless ment alors sur l'effet d'un craft.
--
-- Mesure qui a mis la puce a l'oreille : `electronics` (declencheur : dix plaques de
-- cuivre) tombait en headless quand les plaques sortaient d'un FOUR -- une production
-- reelle, comptee -- mais `automation-science-pack` (declencheur : un laboratoire) ne
-- tombait pas sur un craft simule. Ce n'etait pas le jeu qui exigeait un joueur : c'est
-- notre simulation qui etait incomplete.
local function simulate_craft(params)
  local force = game.forces.player
  local recipe = force.recipes[params.entity_name]
  if not recipe then return end
  local inv = player_mod.get_ai_inventory()
  if not inv then return end

  local stats = nil
  local ok_stats = pcall(function()
    local char = player_mod.get_ai_entity()
    local surface = (char and char.surface) or game.surfaces[1]
    stats = force.get_item_production_statistics(surface)
  end)

  for _, ing in ipairs(recipe.ingredients) do
    if ing.type ~= "fluid" then
      local pris = ing.amount * params.count
      inv.remove({name = ing.name, count = pris})
      if ok_stats and stats then pcall(function() stats.on_flow(ing.name, -pris) end) end
    end
  end
  local produits = params.count * params.per
  inv.insert({name = params.entity_name, count = produits})
  if ok_stats and stats then
    pcall(function() stats.on_flow(params.entity_name, produits) end)
  end
end

local function state_crafting(char, params)
  if not params.started then
    local inv = player_mod.get_ai_inventory()
    local ok, detail, per, crafts = check_can_craft(inv, params.entity_name, params.wanted)
    if not ok then
      M._complete(detail or "craft impossible", false)
      return
    end
    params.count = crafts
    params.per = per
    params.started = true
    if player_mod.is_test_mode() then
      simulate_craft(params)
      M._complete(string.format("craft simule %s x%d", params.entity_name, crafts), true)
      return
    end
    local p = player_mod.get_ai_player()
    if not p then M._complete("aucun joueur (craft)", false) return end
    p.begin_crafting({count = crafts, recipe = params.entity_name})
    log(string.format("[fl] begin_crafting %s x%d", params.entity_name, crafts))
    return
  end
  -- prod, en attente de l'event on_player_crafted_item (inert ; garde-fou global couvre le timeout).
end

-- Appele par control.lua sur on_player_crafted_item (prod) : compte les crafts termines.
function M.on_player_crafted(event)
  local cur = storage.fl.current
  if not cur or cur.type ~= CRAFTING then return end
  cur.crafted = cur.crafted + 1
  if cur.crafted >= cur.count then
    M._complete(string.format("craft %s x%d", cur.entity_name, cur.count), true)
  end
end

-- ===== Etat RESEARCHING =====

local function state_researching(params)
  local force = player_mod.get_ai_force()
  if not params.started then
    local tech = force.technologies[params.technology_name]
    if not tech then M._complete("technologie inexistante: " .. params.technology_name, false) return end
    if tech.researched then M._complete("deja recherchee", true) return end
    if not tech.enabled then M._complete("technologie verrouillee", false) return end
    -- `add_research` REND un booleen, et son refus etait ignore : la tache partait
    -- alors en attente d'une recherche jamais lancee, jusqu'au timeout du garde-fou.
    -- Mesure : `research_technology('automation-science-pack')` rendait « timeout »
    -- avec `current_research = nil` -- c'est-a-dire un refus immediat maquille en
    -- attente, qui envoie chercher la panne du cote du delai.
    --
    -- Le cas normal est celui d'une technologie a DECLENCHEUR (Factorio 2.0) : elle ne
    -- se met pas en file, elle tombe quand le geste est fait. Le dire evite de
    -- s'acharner sur la file de recherche alors qu'il fallait aller fabriquer.
    if not force.add_research(params.technology_name) then
      local tr = tech.prototype and tech.prototype.research_trigger
      if tr then
        M._complete("technologie a declencheur (" .. tostring(tr.type)
                    .. ") : elle ne se met pas en file, il faut accomplir le geste", false)
      else
        M._complete("recherche refusee (prerequis, ou file pleine)", false)
      end
      return
    end
    params.started = true
    -- L'horizon est celui de la TECHNOLOGIE : nombre d'unites x duree d'une unite, avec
    -- une marge (le laboratoire s'arrete quand il manque de courant ou de flacons). Sans
    -- cela, le garde-fou par defaut tue une recherche qui se deroulait normalement.
    local total = (tech.research_unit_count or 1) * (tech.research_unit_energy or 900)
    params.max_ticks = math.floor(total * 1.5) + 600
    log("[fl] recherche lancee: " .. params.technology_name
        .. " (horizon " .. params.max_ticks .. " ticks)")
    return
  end
  local tech = force.technologies[params.technology_name]
  if tech.researched then
    M._complete("recherchee: " .. params.technology_name, true)
    return
  end
  if force.current_research and force.current_research.name ~= params.technology_name then
    M._complete("recherche interrompue", false)
    return
  end
end

-- ===== Boucle principale (appelee depuis on_tick) =====

function M.tick()
  if not storage.fl.tasks then return end

  -- Garde-fou global. Une tache peut demander un horizon PLUS LONG que le defaut, et
  -- une seule le fait : la recherche. `logistics` reclame 20 unites de 900 ticks, soit
  -- 18 000 ticks -- trois fois le garde-fou. Mesure : le laboratoire cherchait
  -- normalement (statut working, 8 flacons deja consommes sur 20) et la tache etait
  -- tuee a 40 % avec un laconique « timeout », ce qui envoie soupconner l'energie ou
  -- l'approvisionnement alors que tout marchait. Allonger le defaut pour autant serait
  -- payer toutes les autres actions au prix de la plus lente.
  if storage.fl.current and storage.fl.current_started_tick then
    -- `storage.fl.current` EST la table de parametres (elle porte `.type`) : l'horizon
    -- pose par la tache s'y lit directement.
    local limite = storage.fl.current.max_ticks or MAX_TASK_TICKS
    if game.tick - storage.fl.current_started_tick > limite then
      stop_walking()
      M._complete("timeout", false)
      return
    end
  end

  -- Demarrage de la prochaine tache si idle.
  if not storage.fl.current then
    M._start_next()
    if not storage.fl.current then return end
  end

  local cur = storage.fl.current

  -- Etats sans avatar (force-level / pur compteur).
  if cur.type == WAITING then state_waiting(cur) return end
  if cur.type == RESEARCHING then state_researching(cur) return end

  local char = player_mod.get_ai_entity()
  if not char then return end

  if cur.type == WALKING then
    state_walking(char, cur)
  elseif cur.type == MINING then
    state_mining(char, cur)
  elseif cur.type == PLACING_AT then
    state_placing_at(char, cur)
  elseif cur.type == REMOVING_AT then
    state_removing_at(char, cur)
  elseif cur.type == ROTATING_AT then
    state_rotating_at(char, cur)
  elseif cur.type == SETTING_RECIPE then
    state_setting_recipe(char, cur)
  elseif cur.type == EMPTYING_AT then
    state_emptying_at(char, cur)
  elseif cur.type == ENABLING_AT then
    state_enabling_at(char, cur)
  elseif cur.type == MOVING_ITEMS then
    state_moving_items(char, cur)
  elseif cur.type == CRAFTING then
    state_crafting(char, cur)
  elseif cur.type == "teleport" then
    -- teleport reserve au mode test (instantane = triche en production).
    if player_mod.is_test_mode() then
      local landed = char.surface.find_non_colliding_position("character", cur.target, 8, 0.4)
      char.teleport(landed or cur.target)
      M._complete("teleporte", landed ~= nil)
    else
      M._complete("teleport interdit en production", false)
    end
  end
end

-- ===== Fabriques de taches (utilisees par operations.lua) =====

local function new_walk_common()
  return {
    type = WALKING, path = nil, calculating_path = false,
    candidates = nil, candidate_index = 0,
    staged_goal = nil, final_goal = nil, path_stage = 0,
    last_position = nil, stuck_ticks = 0, recompute_count = 0,
  }
end

function M.new_walk_to(x, y)
  local t = new_walk_common()
  t.entity_name = "(position)"
  t.search_radius = 0
  t.goal_position = {x = x, y = y}
  return t
end

function M.new_walk_to_entity(entity_name, search_radius)
  local t = new_walk_common()
  t.entity_name = entity_name
  t.search_radius = search_radius
  t.goal_position = nil
  return t
end

function M.new_teleport(x, y)
  -- teleport : tache immediate resolue dans tick (pas de pathfinding). Test only.
  return {type = "teleport", target = {x = x, y = y}}
end

function M.new_mine_entity(entity_name, count)
  return {
    type = MINING, entity_name = entity_name,
    count = count or 1, stall_ticks = 0, position = nil,
  }
end

-- `opts` (optionnel, E2) : {recipe, ug_type, priority_in, priority_out, modules, fuel,
-- fuel_count}. Le LayoutPlanner calcule deja ces champs (S1 a S3) ; sans eux la pose
-- ne sait produire ni bus (underground/splitter), ni beacons, ni machines a recette.
function M.new_place_entity_at(entity_name, x, y, direction, opts)
  return {
    type = PLACING_AT, entity_name = entity_name,
    x = x, y = y, direction = direction, opts = opts,
  }
end

function M.new_remove_entity_at(x, y, entity_name)
  return {type = REMOVING_AT, x = x, y = y, entity_name = entity_name}
end

function M.new_rotate_entity_at(x, y, direction, entity_name)
  return {type = ROTATING_AT, x = x, y = y, direction = direction, entity_name = entity_name}
end

function M.new_set_recipe_at(x, y, recipe, entity_name)
  return {type = SETTING_RECIPE, x = x, y = y, recipe = recipe, entity_name = entity_name}
end

function M.new_empty_output_at(x, y, entity_name)
  return {type = EMPTYING_AT, x = x, y = y, entity_name = entity_name}
end

function M.new_enable_entity_at(x, y, entity_name)
  return {type = ENABLING_AT, x = x, y = y, entity_name = entity_name}
end

function M.new_move_items(item_name, entity_name, max_count, to_entity)
  return {
    type = MOVING_ITEMS, item_name = item_name, entity_name = entity_name,
    max_count = (max_count and max_count > 0) and max_count or math.huge,
    to_entity = to_entity ~= false, position = nil,
  }
end

function M.new_move_items_at(item_name, entity_name, x, y, max_count, to_entity)
  return {
    type = MOVING_ITEMS, item_name = item_name, entity_name = entity_name,
    max_count = (max_count and max_count > 0) and max_count or math.huge,
    to_entity = to_entity ~= false, position = {x = x, y = y},
  }
end

function M.new_wait(ticks)
  return {type = WAITING, remaining_ticks = ticks}
end

function M.new_craft_item(item_name, count)
  return {
    type = CRAFTING, entity_name = item_name,
    wanted = count or 1, count = 0, crafted = 0, started = false,
  }
end

function M.new_research(technology_name)
  return {type = RESEARCHING, technology_name = technology_name, started = false}
end

return M