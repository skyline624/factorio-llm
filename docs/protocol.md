# Protocole RCON factorio-llm

## Conventions de nommage

- Interface observation (synchrone) : **`fl_tools`**
- Interface action (asynchrone, via task_manager) : **`fl_ops`**
- Storage mod : `storage.fl = {test_mode, character, home_position, kit_given, tasks, current, current_started_tick, last_result}`
- Prefixe reglages mod : `fl-` (ex. `fl-tick-interval`, `fl-test-mode`).
- Logs structurés : `log("[fl] ...")`.

## Mode dual (production / test headless)

Le mod fonctionne dans deux modes bascules au runtime par `fl_ops.set_test_mode(v)` (et
au lancement par le réglage `fl-test-mode`, runtime-global, défaut `false`).

| | Production (`test_mode=false`) | Test headless (`test_mode=true`) |
|---|---|---|
| Avatar IA | character du joueur connecté (`game.connected_players[1]`) | character spawné sans `associated_player` |
| Physique | **réelle** : `walking_state`, `mining_state`, `begin_crafting`, `build_distance` | **simulée/instantanée** : teleport-step, extraction+destroy, retrait/insertion |
| Complétion mine/craft | events `on_player_mined_entity` / `on_player_crafted_item` | inline (compte décrémenté directement) |
| Stuck/recompute (walk) | actif (la marche réelle peut bloquer) | inactif (teleport ne se bloque pas) |
| `teleport_to` | **refusé** (instantané = triche) | autorisé (dev/validation) |
| Joueur requis | oui (un client doit rejoindre le serveur) | non |

Résolution de l'entité pilotée : `player.get_ai_entity()` (une seule source de vérité,
DRY) branche sur `storage.fl.test_mode`. Les handlers d'action lisent
`player.get_ai_player()` quand l'API joueur est requise (production uniquement).

## Wire

Côté Python, tous les appels passent par `/silent-command remote.call(...)` :

```
/silent-command remote.call("fl_tools","get_state")
/silent-command remote.call("fl_ops","walk_to",12.5,-3)
/silent-command remote.call("fl_ops","mine_entity","iron-ore",5)
```

`/silent-command` supprime l'echo dans le chat/logs mais renvoie bien la sortie
de `rcon.print(...)` au client RCON. Les fonctions remote ne renvoient rien ;
elles appellent `rcon.print(json)` et c'est ce JSON que le client lit. Toutes les
sorties sont du JSON (pas de `serpent.block`) → homogène côté Python.

## fl_tools (observation synchrone)

| methode | args | sortie JSON |
|---|---|---|
| `get_state` | — | état complet (ci-dessous) |
| `get_tick` | — | `{"tick": <int>}` |
| `scan_area` | `radius` (≤128) | `{tick, origin, radius, entities[], resources{}}` |
| `scan_factory` | — | `{tick, origin, radius:-1, entities[], resources:{}}` |
| `find_nearest` | `name` | `{name,x,y,distance}` ou `{}` |
| `describe` | `name` | `{name, recipe?, entity?}` |
| `get_recipe` | `item` | `{ingredients:[{name,count}], enabled}` ou `{error}` |
| `production_stats` | — | `{produced, consumed}` |

### `get_state() -> JSON`

```json
{
  "tick": 12345,
  "test_mode": false,
  "ready": true,
  "character": {
    "position": {"x": 10.5, "y": -3.0},
    "health": 100.0,
    "max_health": 100,
    "walking": false
  },
  "home_position": {"x": 0, "y": 0},
  "inventory": {"iron-plate": 50, "coal": 100},
  "surface": "nauvis",
  "task": {"state": "idle", "last_result": {"action":"walking","ok":true,"detail":"arrive"}, "pending": 0}
}
```

`task.state` vaut `"busy"` (tache en cours, avec `task`/`started_tick`/`elapsed`/
`calculating_path`/`path_remaining`/`recompute_count`) ou `"idle"` (avec `last_result`
et `pending`).

### scan_area / scan_factory

Chaque ligne d'entité :
```json
{"name":"stone-furnace","type":"furnace","x":12.3,"y":-4.5,
 "direction":"north","status":"working","recipe":"iron-plate"}
```
Pour un `mining-drill` : + `"mining":"iron-ore"`, `"oreUnder":2400`.
`scan_area` : entités dans un rayon (≤128, max 200 lignes, exclut `character`).
`scan_factory` : machines productrices de la force sur la surface entière (max 100,
`radius:-1`).

### find_nearest

Branche water (`find_tiles_filtered`, noms water) vs entités. Filtre les tiles de
resource déjà couvertes par une foreuse de la force (évite de renvoyer un minerai
sous un drill). Rayon 400. Renvoie `{}` si rien.

### describe

`recipe` (force-spécifique, `enabled` reflète ce qui est débloqué) + `entity`
(mécaniques : `type`, `energySource`, `needsFuel`, `size`, `miningSpeed?`,
`craftingSpeed?`, `resourceCategories?`). Clé absente = pas de recette / pas placeable.

### get_recipe / production_stats

`get_recipe` : ingredients + `enabled` (JSON, pas serpent). `production_stats` :
compteurs cumulés `get_item_production_statistics(surface)` — ground truth de ce
qui a été FABRIQUÉ (l'inventaire ne compte que ce qu'on tient).

## fl_ops (action asynchrone)

Toutes ces methodes enfilent une tache dans le `task_manager` et retournent
immediatement `{ok: bool, detail: str}`. L'execution se fait sur `on_tick`.

| methode | args | action |
|---|---|---|
| `walk_to` | `x, y` | marche (pathfinding `request_path`) vers la position |
| `walk_to_entity` | `entity_name, search_radius` | marche (pathfinding) vers l'entite la plus proche, reachability-aware |
| `teleport_to` | `x, y` | déplacement instantané (**test only**) |
| `mine_entity` | `entity_name, count` | mine `count` entites (`mining_state` animé en prod) |
| `place_entity_at` | `entity_name, x, y, direction` | pose une entite (garde reach + `can_place` en prod) |
| `move_items` | `item_name, entity_name, max_count, to_entity` | transfert inventaire IA <-> entites (rayon 32) |
| `move_items_at` | `item_name, entity_name, x, y, max_count, to_entity` | idem, entite unique à la position (rayon 1.5) |
| `wait` | `ticks` | attend `ticks` ticks (reels) |
| `craft_item` | `item_name, count` | craft `count` objets (`begin_crafting` en prod) |
| `research_technology` | `technology_name` | lance la recherche (force-level, pole `tech.researched`) |
| `set_test_mode` | `v` (bool) | bascule le mode dual au runtime |
| `setup` | — | initialise l'avatar IA (idempotent) |
| `status` | — | etat de la tache courante |
| `cancel` | — | vide la file et arrete la tache courante |

`direction` (place_entity_at) : string `'north'|'northeast'|'east'|...|'northwest'`
(convertie via `direction_from_name`) ou int `defines.direction`.

`to_entity` (move_items) : `true` → joueur→entité ; `false` → entité→joueur.
`max_count = 0` → infini (plafond `math.huge`).

### Mécanismes de complétion (physique réelle en production)

| action | exécution | complétion |
|---|---|---|
| walk | `walking_state` chaque tick | `path` vide → `arrive` (stuck/recompute) |
| mine | `mining_state` (animé) | `on_player_mined_entity` → `count--` (≤0 → done) |
| place_entity_at | synchrone (1 tick) | `create_entity` immédiat |
| move_items[_at] | synchrone (1 tick) | `moved_total` plafonné, rollback partiel si plein |
| wait | `remaining_ticks--` | à 0 |
| craft | `begin_crafting` (file réelle) | `on_player_crafted_item` → `crafted++` (≥count → done) |
| research | `add_research` | pole `tech.researched` |

Toutes complètent via `task_manager._complete(detail, ok)` → `last_result` lisible
par `status()`.

### `status() -> JSON`

```json
{"state": "busy", "task": {"type":"walking", "entity_name":"iron-ore", "goal_position":null},
 "started_tick": 12300, "elapsed": 45, "calculating_path": true,
 "path_remaining": 12, "recompute_count": 0}
```
ou
```json
{"state": "idle", "last_result": {"action":"walking","ok":true,"detail":"arrive"}, "pending": 0}
```

## Types de tache (task_manager)

| type | champs | executeur |
|---|---|---|
| `walking` | `entity_name`+`search_radius` ou `goal_position` | pathfinding `request_path` + suivi waypoint (`walking_state` prod / teleport-step test) |
| `mining` | `entity_name, count, stall_ticks, position` | `mining_state` (prod) / extraction+destroy (test) |
| `placing_at` | `entity_name, x, y, direction` | `can_place` + `create_entity` synchrone |
| `moving_items` | `item_name, entity_name, max_count, to_entity, position?` | transfert d'inventaires synchrone |
| `waiting` | `remaining_ticks` | compteur |
| `crafting` | `entity_name, wanted, count, crafted, started` | `begin_crafting` (prod) / simulé (test) |
| `researching` | `technology_name, started` | `add_research` + pole |
| `teleport` | `target{x,y}` | teleport instantané (test only) |

Garde-fou : une tache expire apres 6000 ticks (100s @ 60tps) → `last_result.ok=false, detail="timeout"`.
Minage abort si `stall_ticks > 90` (cible hors portée).

## Pathfinding (port d'airi-factorio)

Le deplacement `walk_to` / `walk_to_entity` utilise le pathfinder de Factorio
(`surface.request_path`), asynchrone. Le chemin calcule est recu dans l'event
`on_script_path_request_finished`. Le suivi se fait waypoint par waypoint :
`walking_state` (production, marche réelle) ou teleport-step (test headless).

Constantes (portées d'airi `control.ts`) :
- `WAYPOINT_REACHED = 0.35` (tiles), `STEP_PER_TICK = 2.0` (avance teleport-step en test)
- `STUCK_TICKS = 45`, `STUCK_PROGRESS_EPS = 0.03` → recompute apres blocage (prod)
- `MAX_PATH_RECOMPUTES = 6`, `DESTROY_AFTER_RECOMPUTES = 2` (degage arbres/rocks)
- `MAX_PATH_STAGES = 5` (staging intermediaire), `MAX_CANDIDATES = 8`,
  `MIN_CANDIDATE_SPACING = 16` (candidats = patches distincts)

Robustesse portée fidelement :
- **vraie collision** : `character.prototype.collision_box` + `collision_mask`
- **try_again_later** : pathfinder occupe → on relance au prochain tick (pas un abort)
- **staging** : si pas de chemin, on vise un waypoint intermediaire a `1/(stage+1)` vers le but
- **candidats** : sur echec, on essaye le prochain candidat nearest-first
- **stuck + recompute** : si le character n'avance plus, on recalcule ; apres 2 echecs on
  detruit les obstacles organiques ; apres 6 on abandonne

## Côté Python (`core/mod_api.py`)

`ModApi` wrappe chaque methode RCON typée. Helper `wait_until_idle(timeout, poll_interval)`
sonde `status()` jusqu'à idle. `main.py` (démo) accepte `--test` pour valider le tuyau
sans joueur connecté.

## À valider au premier run (incertitudes Factorio 2.0)

- `prototypes.entity[name]` accessible en runtime (describe).
- `surface.request_path` + `on_script_path_request_finished`.
- `player.begin_crafting{count, recipe}` / `on_player_crafted_item` / `on_player_mined_entity`.
- `prototypes.entity[name].get_crafting_speed()` / `mining_speed` / `resource_categories`.
- `find_non_colliding_position` signature (4 args).