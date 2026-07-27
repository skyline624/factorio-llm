# LayoutPlanner — spec vivante (sous-module déterministe de FactoryBuilder)

> Rôle : transformer une **BOM** (sortie du `ProductionSolver`) + le **terrain réel**
> (scanné via RCON) en un **blueprint positionné et adapté au terrain** : liste
> d'entités `{name, x, y, direction, role}` + connexions. **Déterministe, sans
> LLM** — l'arbitrage (choix des tiers, où approximativement bâtir, replan gros
> obstacle) reste à FactoryBuilder ; le LayoutPlanner **calcule** la logistique
> (combien de belts/inserters/poles) à partir des débits, comme le solveur
> calcule le nombre de machines.
>
> Parallèle avec le solveur :
> - `ProductionSolver` : `(item, rate)` → **BOM** (machines + quantités, débits effectifs).
> - `LayoutPlanner`   : `BOM + terrain` → **Blueprint** (entités + positions + orientations + connexions, dimensionné au débit).
>
> C'est le `services/geometer.py` prévu par `docs/agents-roadmap.md` (couche 0,
> service partagé). Renommé `LayoutPlanner` pour la clarté du rôle. Règle
> roadmap : *« si une tâche est un algorithme → service, pas d'agent LLM »* — le
> placement est un algorithme.

Statut : **spec** (non implémenté). Implémentation en `python/services/layout_planner.py`.

---

## 1. Périmètre et non-périmètre

**Fait (déterministe) :**
- **Scanner le terrain** via RCON (gisements de fer/cuivre/coal/stone, obstacles,
  eau) — c'est une **entrée**.
- **Positionner les foreuses sur le gisement** réel (selon `mining_area` des drills
  et la forme du gisement), pas à un ancrage arbitraire.
- **Bâtir l'usine par rapport** au gisement (étage smelting près des foreuses,
  cascade selon `facing`), en **s'adaptant** au terrain (contourner un obstacle).
- **Disposer les machines** d'un étage en bande (manifold) + belts entrée/sortie
  + inserters + poles.
- **Calculer la logistique** au débit : `belts_per_stage = ceil(rate / belt_speed)`,
  `inserters_in_per_machine = ceil(debit_consommation / inserter_throughput)`,
  idem pour la sortie. Comme le solveur calcule `machine_count`, le LayoutPlanner
  calcule le compte de belts/inserters.
- Produire un blueprint absolu (coordonnées de map) prêt pour `place_entity_at`.

**Ne fait pas (arbitrage = FactoryBuilder / LLM) :**
- Choisir **quel gisement** exploiter ni **la zone d'implantation** (FactoryBuilder
  passe la zone/gisement cible au LayoutPlanner).
- Choisir les **tiers** (belts/inserters/poles/machines) — paramètres d'entrée
  (comme `machine_tiers` du solveur).
- Décider du layout alternatif (main bus, blocs tileable, beacons) — S1+.
- Arbitrer un replan lourd (déplacer toute l'usine) — S4, et FactoryBuilder décide.

---

## 2. Interface (entrées / sorties)

### `LayoutRequest`
```python
@dataclass
class LayoutRequest:
    plan: ProductionPlan          # BOM du solveur (nodes + leaves + totals + ingredients@effectif)
    terrain: Terrain               # scan RCON du terrain (gisements, obstacles, eau)
    anchor: tuple[float, float]    # point de rACCORDEMENT au gisement (sur le gisement cible)
    facing: int = 2               # direction principale du flux (0=N, 2=E, 4=S, 6=W)
    constraints: LayoutConstraints = default
```
`anchor` est sur le gisement (où les foreuses déposent) ; l'usine pousse dans le
sens `facing` à partir de là. Pas d'ancrage "absolu arbitraire" — le terrain dicte.

### `Terrain` (scan RCON, déterministe)
```python
@dataclass class ResourcePatch:
    resource: str                  # "iron-ore" | "copper-ore" | "coal" | "stone"
    tiles: list[tuple[int,int]]    # tuiles du gisement (scan_surface)
    bbox: tuple[int,int,int,int]
@dataclass class Terrain:
    patches: list[ResourcePatch]
    obstacles: list[tuple[int,int,int,int]]   # bbox entités bloquantes
    water: list[tuple[int,int,int,int]]       # bbox eau
    surface_area: tuple[int,int,int,int]      # zone allouée (FactoryBuilder)
```

### `LayoutConstraints` (arbitrage tiers = entrée, comme machine_tiers du solveur)
```python
@dataclass class LayoutConstraints:
    belt_tier: str = "transport-belt"        # yellow/red/blue
    inserter_tier: str = "burner-inserter"   # burner/inserter/fast/long/stack
    pole_tier: str = "small-electric-pole"   # small/medium/big/substation
    machine_gap: int = 1                     # tuiles de gap (inserter)
    stage_gap: int = 2                        # gap entre étages (transition belt)
    # NB : inserters_per_machine et belts_per_stage NE SONT PAS ici.
    # Ils sont CALCULES par le LayoutPlanner à partir des débits (cf §4).
```

### `LayoutEntity`
```python
@dataclass
class LayoutEntity:
    name: str
    x: float; y: float              # position absolue (centre, coord map)
    direction: int                  # 0=N, 2=E, 4=S, 6=W
    role: str                        # "machine" | "belt" | "inserter" | "pole" | "drill"
    node_item: str = ""             # item produit (machine) ou transporté (belt)
    in_port: tuple[float,float] = (0,0)   # point d'entrée (belt/inserter drop)
    out_port: tuple[float,float] = (0,0)  # point de sortie
```

### `LayoutPlan`
```python
@dataclass
class LayoutPlan:
    request: LayoutRequest
    entities: list[LayoutEntity]
    connections: list[tuple[int,int,str]]   # (from_idx, to_idx, item)
    bbox: tuple[float,float,float,float]   # (x1,y1,x2,y2)
    # Dimensionnement logistique (sortie du calcul de débit, pour audit) :
    stage_logistics: dict[str, StageLogistics]   # par node_item
    totals: dict[str,int]                       # {entity_name: count}
    feasibility: str = "ok"
    notes: list[str] = field(default_factory=list)

@dataclass
class StageLogistics:
    item: str
    rate_effective: float          # du solveur
    belts_per_stage: int           # = ceil(rate / belt_speed)
    inserters_in_per_machine: int  # = ceil(debit_conso_machine / inserter_throughput)
    inserters_out_per_machine: int # = ceil(debit_prod_machine / inserter_throughput)
```

`totals` étend les `total_machines` du solveur avec la logistique dimensionnée.

---

## 3. Modèle de données — `ENTITY_GEOMETRY` + `THROUGHPUTS` (lu via RCON)

Le LayoutPlanner a besoin de la **géométrie** et des **débits** de chaque entité.
Source de vérité = RCON (`describe` étendu) + cache + injection fixture (DIP).

### Géométrie — source RCON vs hardcode Python (constat API Factorio 2.0)

Investigation 2026-07-24 (doc `lua-api.factorio.com` + test en jeu) : en Factorio
2.0, `prototypes.entity[name]` renvoie un `LuaEntityPrototype` qui **n'expose PAS au
runtime** les géométries fines (ni `pickup_position`, ni `max_wire_distance`, ni
`mining_area`, ni `belt_speed` — ni comme propriété, ni via getter utilisable). Seule
`size` (depuis `collision_box`) est lisible. Donc :

| Champ | Entités | Source | Rôle |
|---|---|---|---|
| `size {w,h}` | toutes | ✅ **RCON** (validé 15/15) | emprise (2×2, 3×3, 1×1) |
| `direction` | toutes | ✅ (`place_entity_at`) | 0/2/4/6 |
| `belt_speed` | belts | hardcode Python (15/30/45) | débit items/s |
| `pickup_reach`/`drop_reach` | inserters | hardcode Python (1.0 / 2.0 long) | portée prise/dépose |
| `wire_reach`/`supply_area` | poles | hardcode Python (small 7.5/2.5, med 9/3, big 30/2, substation 64/9) | portée fil + zone alim |
| `mining_area` | drills | hardcode Python (burner 2×2, electric 5×5) | zone extraction (gisement) |

Les valeurs hardcodées sont les **valeurs wiki stables** (ne changent pas entre mods).
Validées en S0b par **mesure in-game** : poser l'entité et lire sa zone réelle via une
commande dédiée (ex. `surface.find_entities_filtered` autour d'un pole posé pour
vérifier son `supply_area`, mesurer la zone minée d'un drill sur un gisement).

### Débits (throughputs) — pour le calcul de la logistique

| Entité | Débit | Source | S0 |
|---|---|---|---|
| **transport-belt** (yellow/red/blue) | 15 / 30 / 45 items/s | hardcodé (wiki) | ✅ |
| **burner-inserter / inserter** | ~0.6 items/s | mesuré/connu | ✅ (fixture) |
| **long-handed-inserter** | ~0.83 items/s | mesuré/connu | ✅ |
| **fast-inserter** | ~2.7 items/s | mesuré/connu | ✅ |
| **stack-inserter** | dépend stack bonus | S1+ | — |
| **electric-pole** supply_area | couverture élec | RCON `supplyArea` | ✅ |

> Les débits inserter en régime permanent dépendent du tier et de la distance
> (swing). En S0 on hardcode des valeurs de régime mesurées/connues (comme le
> solver hardcode les `craft_time`). S0b mesure en jeu pour confirmer. La
> dépendance à la distance (pickup/drop) est affine (S1+).

### KB géométrique + débits
```python
@dataclass class EntityGeometry:
    name: str; w: int; h: int
    pickup_distance: float = 0.0; drop_distance: float = 0.0
    wire_reach: float = 0.0; supply_area: float = 0.0
    mining_area: tuple[float,float,float,float] = None   # (x1,y1,x2,y2)

THROUGHPUTS = {                  # regime permanent (items/s) — fixture S0
    "transport-belt": 15.0, "fast-transport-belt": 30.0, "express-transport-belt": 45.0,
    "burner-inserter": 0.6, "inserter": 0.6, "long-handed-inserter": 0.83,
    "fast-inserter": 2.7,
}

class GeometryBase:
    def geometry(self, name) -> Optional[EntityGeometry]: ...
    def populate_from_rcon(api, names): ...
```

### Conventions Factorio
- **Directions** : `0=N, 2=E, 4=S, 6=W`. Belt dir 2 pousse vers l'Est.
- **Grille** : alignement 1 tuile (machines paires 2×2 sur grille paire).
- **Ancrage** : sur le gisement (point de raccordement foreuses→belts), l'usine
  pousse dans le sens `facing`. Pas d'absolu arbitraire.

---

## 4. Algorithme — layout en bande + dimensionnement logistique + adaptation terrain

### 4.1 Scan + positionnement des foreuses (feuilles du solveur)
1. Pour chaque feuille (drill, item=`iron-ore`/...), trouver le `ResourcePatch`
   correspondant dans `terrain.patches` (FactoryBuilder a choisi la zone).
2. Couvrir le gisement de drills : grille de drills espacées selon `mining_area`
   (les zones d'extraction se chevauchent -> espacement optimal = 2× mining_radius).
3. Une **belt de collecte** ramasse le minerai des drills, converge vers `anchor`.

### 4.2 Dimensionnement logistique (le CALCUL, comme le solveur)
Pour chaque **nœud** du `ProductionPlan` :
- `rate_effective` (débit produit par l'étage) — du solveur.
- `debit_conso_machine` = ingredient_amount × rate_effectif / result_count /
  machine_count (débit consommé par UNE machine) — du solveur (`ingredients@effectif`).
- **`belts_per_stage`** = `ceil(rate_effective / belt_speed)` — combien de belts
  en parallèle pour transporter le débit de l'étage (ex. 120 plate/s + yellow
  belt 15/s → 8 belts). À haut débit, plusieurs belts en parallèle (S1 : splitters).
- **`inserters_in_per_machine`** = `ceil(debit_conso_machine / inserter_throughput)`.
- **`inserters_out_per_machine`** = `ceil(debit_prod_machine / inserter_throughput)`.

→ Le LayoutPlanner ne « devine » pas : il dimensionne à partir du débit (solveur)
et des vitesses (throughputs). Production jamais < demande (ceil).

### 4.3 Étage (un nœud du solveur)
Pour un nœud produisant `item` avec `machine_count` machines :
1. Rangée de `machine_count` machines alignées sur l'axe perpendiculaire à
   `facing` (espacement `machine_gap`).
2. `belts_per_stage` belts d'entrée (ingrédients) d'un côté, autant de sortie de
   l'autre, direction = `facing`.
3. `inserters_in_per_machine` inserters d'entrée + `inserters_out_per_machine`
   de sortie par machine.
4. **Power poles** : un pole tous les `N` machines (N = couverture via
   `supply_area`). Un réseau par étage.
5. Taille étage : `width = machine_count × (machine_w + gap)` ;
   `depth = machine_h + 2×inserter + belts_per_stage×1`.

### 4.4 Cascade d'étages + adaptation terrain
- Étage `n+1` en aval (`facing`) de l'étage `n`, belt de transition (`stage_gap`).
- Belt de sortie étage n → belt d'entrée étage n+1 (même item → connexion).
- **Détection per-entité (S4b)** : `_occ_terrain(terrain, x, y, w, h)` retourne
  `"obstacle"|"water"|"out-of-map"|None` (bbox-vs-bbox contre `terrain.obstacles`/
  `water`/`out_of_map` ; précision tuile si `terrain.tile_grid` peuplé). Gated par
  `constraints.terrain_check=True` (sinon check post-hoc global S3d inchangé).
- **Replan auto déterministe (S4b)** : si hits → `feasibility="obstacle_blocking"` →
  `_plan_with_replan` essaie (règles fixes, budget `replan_budget`) : shift
  `cascade_offset_v` (offset uniforme au 1er étage machine, propage à toute la
  cascade via `v_out` S1a ; les drills restent sur `patch.bbox`, indépendantes de
  l'anchor) ±`bypass_offset_v`/±2×, puis pivot `facing` ±90°/180°. Garde-fous :
  `bypass_max_offset_v`, `constructible_zone`, `tried` set (anti-boucle). Si ok →
  retourne le LayoutPlan ; si épuisé → best layout (max entités) + note
  `replan_exhausted` (handoff FactoryBuilder).
- **Replan lourd = FactoryBuilder (S4c)** : si le planner épuise son budget
  (`obstacle_blocking`), `FactoryBuilder.build_layout` arbitre — changer de
  gisement cible (`scan_patch` rayon croissant 400/800/1200) puis monter tier belt
  (yellow→red→blue, cascade plus compacte). Frontière LLM/déterministe (§7) : le
  planner est déterministe pur (replan léger offset/facing), FactoryBuilder est
  stratégique (replan lourd gisement/tier, LLM optionnel pour la décision).

### 4.5 Connexions
`connections` encode : `machine → inserter_in → belt_in` (alim),
`machine → inserter_out → belt_out` (évac), `belt_out(étage n) → belt_in(étage n+1)`,
`pole → machine` (élec). Graphe pour l'exécution ordonnée.

### 4.6 Feasibility
- `missing_geometry:<name>` / `missing_patch:<resource>` (gisement absent).
- `belt_overflow` : `belts_per_stage` > capacité de la rangée (trop de belts à
  caser) → monter tier belt ou splitter (S1).
- `obstacle_blocking` : terrain infaisable tel quel. Notes `per_entity:<N> hits
  kind=<obstacle|water|out-of-map>` (S4b, détection per-entité) et/ou
  `replan_exhausted:<K> tentatives (handoff FactoryBuilder)` (S4b, budget épuisé).
  Replan auto S4b (shift offset/facing) puis replan lourd FactoryBuilder S4c
  (gisement/tier).
- `inserter_insufficient` : `inserters_in_per_machine` > slots dispo sur la
  machine → monter tier inserter (fast/stack, S1+).

---

## 5. Exemple chiffré (S0)

**Entrée** : `ProductionPlan` pour `iron-gear-wheel@5/s` (S0b validé) +
`terrain` avec 1 patch iron-ore.
```
nodes : gear (5 asm-1) / iron-plate (32 stone-furnace, eff 5... ) / iron-ore (20 electric-drill, feuille)
totals : assembling-machine-1=5, stone-furnace=32, electric-mining-drill=20
ingredients@effectif : gear consomme 10 plate/s ; plate consomme 10 ore/s
```

**Dimensionnement logistique** (belts yellow 15/s, burner-inserter 0.6/s) :
- Étage furnaces (32, eff 5... → 10 plate/s produit ; consomme 10 ore/s) :
  - `belts_per_stage = ceil(10/15) = 1` belt entrée (ore) + 1 sortie (plate). OK 1 belt suffit.
  - `debit_conso_machine = 10/32 = 0.31 ore/s/machine` → `inserters_in = ceil(0.31/0.6) = 1`.
  - `debit_prod_machine = 10/32 = 0.31 plate/s/machine` → `inserters_out = ceil(0.31/0.6) = 1`.
- Étage asm (5, eff 5 gear/s ; consomme 10 plate/s) :
  - `belts_per_stage = ceil(10/15) = 1`.
  - `inserters_in = ceil((10/5)/0.6) = ceil(2.0/0.6) = 4` (2 plate/s/machine → 4 inserters !).
  - `inserters_out = ceil((5/5)/0.6) = ceil(1.0/0.6) = 2`.
→ Ici le calcul révèle qu'à 5 gear/s, les asm ont besoin de **4 bras d'entrée**
chacun (burner-inserter trop lent) — le LayoutPlanner le calcule au lieu de le
fixer. FactoryBuilder arbitrerait : passer en fast-inserter (2.7/s → 1 bras suffit).

**Sortie `LayoutPlan`** (approximatif) :
```
Étage drills  : 20 electric-drill sur le patch iron-ore + belt de collecte -> anchor.
Étage furnaces : 32 stone-furnace en rangée, 1 belt ore (N), 1 belt plate (S), 1+1 inserter/four, poles.
Étage asm     : 5 asm-1, 1 belt plate (N), 1 belt gear (S), 4+2 inserters/asm, poles.
Connexions + totals étendu (belts/inserters/poles dimensionnés).
```

**Test de cohérence S0** : le `inserters_in_per_machine=4` (burner) tombe du
calcul → confirme que le LayoutPlanner dimensionne la logistique, pas l'utilisateur.

---

## 6. Plan d'implémentation S0 → S4

| Étape | Contenu | Test |
|---|---|---|
| **S0** | Étendre `describe` mod (reach/poles/mining_area) + `verify_layout_data.py`. Puis `services/layout_planner.py` + `GeometryBase`/`THROUGHPUTS` (knowledge.py) + test unitaire fixture (terrain patch simulé + géométries mesurées). Bande, chaîne linéaire, dimensionnement logistique. Pas de fluides/splitters/beacons. | Unitaires sans serveur |
| **S0b** | Valider en jeu : géométries RCON (verify_layout_data.py), throughputs inserter (mesure), `can_place_entity` sur chaque entité du blueprint, scan d'un vrai patch iron-ore. | Live headless |
| **S1a** | Throughput inserter **affine** (`inserter_throughput(name, swing) = base - k*(swing - ref)`, `INSERTER_AFFINE` k=0 → back-compat S0) + **belts de transition physiques** entre étages (alignement v, adjacence si stage_gap=2, belts intermédiaires FACING_DIR_U si gap>2, anti-collision `_occupied`). DIP via `LayoutRequest.inserter_throughput_fn`. | Unitaires (55/55) |
| **S1b** | **Splitters/mergers** (arbre binaire, modèle "1 bus inter-étage le long de u"). Itération S1d : **merger orienté FACING_DIR_V** (couvre 2 lanes en u, pos `v_a+1`) + **splitter conservé FACING_DIR_U** (correction V = virage S1e). `_build_split_tree`/`_build_merge_tree`, M-1 mergers / N-1 splitters) + **multi-ingrédients** (empilement en u, ingrédient 1 = `long-handed-inserter` reach 2.0 swing 4.0, `max_ingredients_per_stage=2`, note `too_many_ingredients` au-delà). Connexion **par ingrédient** via `out_idx_by_item`. | Unitaires (78/78) |
| **S1c** | **Main bus** (layout alternatif, `bus_layout=True` défaut False). Bus perpendiculaire au facing (longe v), lanes empilées en u (1 par item intermédiaire = produit ET consommé). Étages qui **tapent** (splitter prélève sur la lane → belts_in, réutilise `_build_split_tree`) et **feedent** (merger réinjecte belts_out → lane, réutilise `_build_merge_tree`). Dispatch `if constraints.bus_layout: return _plan_bus(...)`. Role `bus-belt`. Feed/tap coordination approximée → S1e. | Unitaires (101/101) |
| **S1d** | Validation live (`verify_layout_s1.py`) : `can_place_check` sur blueprint S1 + mesure swing. **Constat itération offsets** : S1c main bus 575/575 can_place 100% ; merger M=2 corrigé (orient V + `v_a+1`, validé live) ; splitters/mergers ~54% fail = feed M>2 (collision belts_in consommateur, S1e) + splitter virage (S1e) + terrain S4 (rangée 6477 entités déborde). `k=0` conservé. | Live headless (12/12) |
| **S1e** | **Volet A (tap)** : API `target_idx` sur `_build_split_tree` + arbre binaire équilibré en `-v` (`_split_subtree`, orient FACING_DIR_V, aligné sur le consommateur u≈86) + transition `+u` (`_route_bus_to_target`). **Validation live 2026-07-24** (headless zone 1000,1000, `_sideload_validate.py`) : sideload perpendiculaire **direct** sur splitter = **IMPOSSIBLE** en Factorio 2.0 (un splitter ne reçoit que par son entrée dédiée `-v`, pas par le côté ; transition `+u` atterrit sur la tuile nord vide → items stagnant) → fallback **belt `+v` intermédiaire `split_entry_v` VALIDÉ** (trans `+u`→belt `+v` sideload belt→belt → splitter entrée `-v`, flux réparti out1=1 out2=1). **CONSTAT** : tap reste **non-circuiterie-connexant** car sideload bus `+v`→transition `+u` d'une lane continue impossible aussi (bus dépose `+v`, pas `+u`) → **S1f underground**. Règle la lacune split inter-étage S1d (tree relatif au bus → aligné consommateur). **Volet B (feed) — revert + reporté S1f** : tentative `feed_side="bus"` (merger V sur lanes dir -u) géométriquement cassée (directions incohérentes + 187 collisions belts_out S1c déjà longés vers le bus). Revert → feed S1d (côté étage, CONSTAT). Feed correct (virage -u→+v + merger hors-zone belts_in + re-route vers lane) nécessite **underground belts** (collisions structurelles) = **S1f**. Collisions 194→10. | Unitaires (17/17) |
| **S1f** | **Probe live préalable** (headless zone 1100,1000+) VALIDÉ en jeu les 4 mécaniques : **T1** paire underground `type="input"/"output"` + croisement belt au-dessus (transit souterrain > belt, paire circule sans casser) ; **T2b** `splitter_output_priority="left"/"right"/"none"` (**STRING** Factorio 2.0, pas `defines.direction` — `east` échoue) settable runtime ; **T3** merger 1 entrée orienté +v → lane +v connexion directe ; **T4** sideload +v→+u. **Convention** : `+u` = toujours "left" du POV flux (4 facings) → `priority="left"` → +u (consommateur), "right" → lane +v. **Architecture commune** : `LayoutEntity` += `skip`/`ug_type`/`priority` ; `_occ` ignore skip ; helpers `_find_at`/`_skip_belt_at`/`_under_crossing` (modifie in-place lane +v : v_cross-1→under-in, v_cross+1→under-out, v_cross→skip ; idx stable). **Volet A (underground crossings) VALIDÉ** : `_under_crossing` + `test_underground_crossing` (18/18). **Volet B (tap redesign) VALIDÉ live** : `_tap_bus_to_consumer` remplace `_route_bus_to_target` — splitter prélèvement `priority="left"` (→+u) sur la lane + virage +v→+u (T4) + transition +u traversant les lanes intermédiaires via `_under_crossing` + `split_entry_v` feed split tree S1e. Count main bus = n_out (1 prélèvement + n_out-1 tree ; évolution vs S1e n_out-1). `test_bus_tap_splitter` MAJ (4) + `test_bus_tap_priority` (n_out=2 → 2). **Validation live `verify_tap_s1f.py`** : splitter priority="left" + 1 entrée envoie D'ABORD tout au prio (consommateur), puis en régime établi (backpressure) le surplus déborde vers la lane (`out_right=1` cycle 9) → tap main bus CORRECT, lane NON coupée. **Volet C (feed) BUG DE CONCEPTION → REVERT + reporté S1g** : tentative `_feed_consumer_to_bus` (M belts_out virées -u en parallèle + crossings + arbre mergers + merger-lane) **cassée** — les M belts_out côte à côte en u qui vireraient -u sur la MÊME rangée v=v_out+1 se chevauchent massivement (`feed_belt_u_collision` sur toute la rangée). En Factorio, M belts // +v ne peuvent pas toutes virer -u sur une seule rangée v. La bonne géométrie = merger tree côté étage (M→1) puis 1 belt vire -u, mais ce merger tree (v_out+1) collisionne les belts_in consommateur (étages alignés sans gap → S1d CONSTAT). Re-planification spatiale nécessaire (gap étages / merger -u décalé / underground sous étages) = **S1g**. Revert → feed S1d (CONSTAT, note `bus_feed_S1d`→S1g). `_feed_consumer_to_bus` conservé code mort référence. `test_bus_feed_merger` MAJ (CONSTAT → S1g). Collisions 194→2. | Unitaires (19/19) |
| **S1g** | **Re-planification spatiale feed main bus** (volet C S1f résolu). **Probe live préalable** (headless zone 1200,1200, `verify_feed_s1g.py`) VALIDÉ en jeu les 3 mécaniques : **T5** virage +v→-u (belt +v dépose sur belt -u à la tuile de dépôt +v, miroir de T4) ; **T6** sideload -u→+v sur lane +v (merger GRATUIT belt→belt, lane continue non coupée — pas de splitter) ; **T7** merger 2→1 (splitter orient +v, 2 entrées, 1 sortie prise, 2e bouchée → backpressure force tout sur la sortie prise). Géométrie feed complète M=2 validée (lane aval `lane14>0` = merger 2→1 + virage +v→-u + belts -u + sideload -u→+v + lane continue). **Approche S1g** : le bug S1f volet C (M belts_out // ne peuvent virer -u sur même rangée v) se résout en gardant le **merger tree côté étage** (`_build_merge_tree`, M-1 mergers conservé) dans un **gap entre étages** (`gap_feed = 4·ceil(log2 M)+6`, libère la zone `v_out+1..v_out+gap` qui collisionnait les belts_in consommateur en S1d) puis **1 seul belt vire +v→-u** (T5) + belts -u traversant les lanes bus intermédiaires via `_under_crossing` (T1) + sideload -u→+v sur la lane produit (T6, merger gratuit, count M-1 conservé — pas de merger-lane). `_route_feed_to_lane` (nouveau) implémente cette circuiterie. `gap_feed` conditionné par `out_item in bus_items` (mines ore non-bus → pas de gap → transition ore S1a préservée). **Validation live usine complète** `verify_layout_s1g.py` (gear@30/s main bus) : 12/12 recs, 0 merger_collision, feed_inject_S1g + bus_feed_S1g notés, plate merger 4→1=3, gear tap 4 splitters, can_place 100% (3969 entités) sur la portion dans la map générée (frontière starting_area y≈318 ; 3323 entités au-delà = artefact headless hors-scope S1g, pas collisions). `test_bus_feed_merger` MAJ (CONSTAT→S1g) + `test_bus_feed_merger_lane` (M=2 → 1 merger, 0 collision, lane alimentée). Collisions 2→0. | Unitaires (20/20) + Live (12/12) |
| **S2a** | **Socle fluides** (pipes/pumps/offshore-pump/pumpjack/oil-refinery/chemical-plant) + chaîne **plastic-bar** (crude-oil→petroleum-gas→plastic-bar). Layout fluide = **direct pipe machine→machine** (analogie bande S1a, PAS de pipe-bus ni splitter/merger fluide — chaîne 1→1). `describe` étendu (`type` ingredient/produit, `fluid_boxes`, `output_fluid`) ; `scan_water_edge` (bord plan d'eau) ; `set_test_mode` spawn crude-oil+coal+eau. Solveur : `transport`/`phase` par nœud (solid/fluid/mixed), `mining_machine(item)` item-aware (water→offshore-pump, crude-oil→pumpjack). Layout : `_place_pumpjacks`/`_place_offshore_pump` (feuilles fluides, pipe output direct), `_place_stage` branchement fluide (pipe collecte in/out, pas d'inserter pour les fluides), `_place_pipe_segment` (pipes 1×1 inter-étages, junction auto). `pipe_throughput` affine (k=0 en S2a). Pipe-bus = S2c plus tard. | Unitaires (28/28) + Live (12/12) |
| **S2b** | **Fluides avancés** : S2b-1 multi-produits (advanced-oil-processing 3 co-produits heavy+light+petroleum) + cracking (organic-or-chemistry) + co-produits orphelins → sinks `storage-tank` ; S2b-2 steam/boiler + power (chaîne boiler→steam→steam-engine, steam ingrédient coal-liquefaction) ; S2b-3 débit pipe affine k≠0 (viscosités hardcodées, pipes parallèles) + fix K7 séparation 3 outputs (outputs côtés distincts v=-2/0/+2 + lane décalée). `recipes_by_product` + sélecteur `recipe_of` (RECIPE_PREFERENCE), `result_counts` multi-produits, `measure_entity` fluid_boxes instance, `output_port_dv` mapping produit→port. | Unitaires (47/47) + Live (14/14 + 12/12 + 12/12) ✅ |
| **S2c** | Pipe-bus fluide (analogie main bus S1c mais pipes/junctions). | Live |
| **S3** | Beacons + modules (productivity/speed) + electric furnaces. S3a solveur `ModuleEffect` Option A (agrégé injecté via `ProductionRequest.module_effects`, appliqué à `per_machine` sans calcul, formule 2.0 FFF#409 rendements décroissants `dist*sqrt(n)*mod`, propagation productivité `ing_rate/(rc_item*effective_productivity)`) + `MachineSpec` electric-furnace (speed=2, smelting, module_slots=2). S3b API runtime Lua (`describe`/`measure_entity` beacon + `get_module_inventory()` instance + `scan_factory` modules) + fixtures `MODULE_FIXTURE`/`BEACON_FIXTURE`/`GEOMETRY_FIXTURE["beacon"]/["electric-furnace"]` + `compute_module_effect` + `measure_beacon` (CONSTAT live raffiné : `supply_area_distance`/`module_slots`/`max_energy_usage` INACCESSIBLES → fixtures, `distribution_effectivity`=1.5 ACCESSIBLE FFF#409 ×3, `get_module_inventory()` ACCESSIBLE instance). S3c placement beacons côté +u dans `_place_stage` (`u_beacon_pos = u_machine+offset_out_u+1.0+beacon_half_u` → couverture 2.5<3.0, poles relocalisés, `LayoutEntity.modules`) + modules insérés (`ent.insert` poseur RCON). S3d beacons côté -u (double couverture "8 beacons" = 4 +u + 4 -u) : `beacons_neg_per_stage` + placement miroir `u_machine-offset_out_u-1.0-beacon_half_u` + gate collision (skip multi-ingrédients) + réservation `u_next` inter-étage (CONSTAT : 1er étage machine face drills obtient les -u, étages suivants skippés car transition belts remplissent le gap). | Unitaires (52/52 + 55/55 + 59/59 pytest) + Live (12/12 + 8/8 + 8/8) ✅ COMPLET (a+b+c+d) |
| **S4** | Adaptation terrain avancée (contournement, replan) + FactoryBuilder arbitre. S4a mod Lua scan obstacles/tuiles (`scan_obstacles`/`scan_tiles_bbox`/`get_tile`, non-destructif, cap 400/200×200) + wrappers Python `ModApi`. S4b planner détection per-entité `_occ_terrain` (retourne obstacle/water/out-of-map, précision tuile via `tile_grid`) + replan auto déterministe `_plan_with_replan` (shift `cascade_offset_v` ±`bypass_offset_v`/±2× puis pivot `facing` ±90°/180°, budget `replan_budget`, `tried` anti-boucle, `constructible_zone`) ; `plan()` dispatcher (`terrain_check`/`replan_budget` → `_plan_with_replan`, sinon `_plan_core` S3d back-compat). S4c `FactoryBuilder.build_layout` arbitre replan lourd (gisement `scan_patch` rayon croissant 400/800/1200 + tier belt yellow→red→blue, garde-fou tier sans géométrie → skip) ; `Contract` += `replan_budget`/`layout_constraints`, `zone`→`constructible_zone`. | Unitaires (8/8 S4c) + Live (S4a/S4b/S4c à valider post-relance) ✅ COMPLET (a+b+c) |

### Statut S1 (2026-07-24)

| Sous-étape | Tests unitaires | Statut |
|---|---|---|
| S1a (throughput affine k=0 + belts transition physiques) | 55/55 | ✅ Validé, back-compat stricte (chaîne fer 5/32/20, 2 connexions, inserters_in=4) |
| S1b (splitters/mergers + multi-ingrédients) | 78/78 | ✅ Validé, back-compat 1→1 (pas d'arbre) ; offsets splitter/merger approximés → ajuster (S1d constat) |
| S1c (main bus `bus_layout=True`) | 101/101 | ✅ Validé, back-compat stricte (False → bande S1a/S1b inchangée) |
| S1d (validation live `verify_layout_s1.py`) | 12/12 | ✅ Constat reporté : main bus 100% can_place ; splitters/mergers ~54% (merger M=2 corrigé itération, reste feed M>2 + splitter virage = S1e + terrain S4) ; k=0 conservé |
| S1e (tap target-aligned + feed revert + validation live sideload) | 17/17 | ✅ Volet A tap : arbre équilibré -v aligné consommateur + fallback belt `split_entry_v` **VALIDÉ LIVE** (sideload direct splitter impossible — splitter ne reçoit que par entrée `-v` ; trans `+u`→belt `+v`→splitter, out1=1 out2=1). CONSTAT : tap reste non-circuiterie car bus→transition lane continue impossible aussi → S1f underground. Volet B feed revert (géométriquement cassé) → S1f. Collisions 194→10. |
| S1f (underground crossings + tap redesign + feed revert) | 19/19 | ✅ Volet A underground crossings `_under_crossing` VALIDÉ (paire input/output + belt skip). Volet B tap `_tap_bus_to_consumer` priority="left" **VALIDÉ LIVE** `verify_tap_s1f.py` (splitter prio + 1 entrée → tout au consommateur puis surplus déborde vers la lane en régime établi = tap main bus CORRECT, lane non coupée ; convention +u="left" POV flux, priority STRING Factorio 2.0). Volet C feed `_feed_consumer_to_bus` **BUG DE CONCEPTION** (M belts_out // ne peuvent virer -u sur même rangée v) → REVERT + reporté **S1g** (re-planification spatiale : gap étages / merger -u décalé / underground sous étages). Collisions 194→2. |
| S1g (re-planification spatiale feed main bus) | 20/20 | ✅ Volet C S1f résolu. Mécaniques **T5/T6/T7 VALIDÉ LIVE** `verify_feed_s1g.py` (virage +v→-u, sideload -u→+v merger gratuit lane continue, merger 2→1). `_route_feed_to_lane` : merger tree côté étage conservé (`_build_merge_tree`, M-1 mergers) dans un **gap_feed = 4·ceil(log2 M)+6** (libère la zone belts_in consommateur) + 1 belt vire +v→-u (T5) + belts -u via `_under_crossing` + sideload -u→+v sur lane (T6, count M-1 conservé). gap_feed conditionné `out_item in bus_items` (transition ore S1a préservée). **Usine complète** `verify_layout_s1g.py` 12/12 : 0 merger_collision, feed_inject_S1g + bus_feed_S1g, can_place 100% (3969) sur portion map générée (3323 hors_map = artefact starting_area headless, pas collision). Collisions 2→0. |

**Limitations S1 reportées** (itération future) :
- **Itération offsets S1d (2026-07-24)** : merger corrigé (orient **FACING_DIR_V** + pos `v_a+1`, couvre 2 lanes en u — validé M=2 isolé unitaire + live). Mais le taux global splitters/mergers reste **~54%** car les échecs résiduels sont : (a) **merger feed main bus M>2** collisionne les belts_in du consommateur (étages adjacents `stage_gap=2` → `v_out_prod+1` dans la zone belts_in) = coordination **S1e** ; (b) **splitter inter-étage** conservé orient FACING_DIR_U (la correction V nécessite un **virage** bus `+v`→transition `+u`→splitter V en tête consommateur + une **API target**, le tree étant relatif au bus `in_idx`) = **S1e**. Splitters U S1b ~77% OK (pos +u hors zone v).
- **S1e (2026-07-24)** : **volet A (tap)** — API `target_idx` + `_split_subtree` (arbre équilibré en `-v`, orient FACING_DIR_V, aligné sur le consommateur) + `_route_bus_to_target` (transition `+u`). Règle la lacune (a)+(b) split inter-étage : le tree n'est plus relatif au bus mais aligné sur le consommateur. Dump gear@22 : 2 splitters orient V à u=86.5/v=498.5 et u=87.5/v=500.5, 88 belts transition (4 collisions croisements bus lanes → S1f underground). **Validation live sideload 2026-07-24** (serveur headless zone 1000,1000, `_sideload_validate.py`) : sideload perpendiculaire **direct** sur splitter = **IMPOSSIBLE** en Factorio 2.0 (un splitter ne reçoit que par son entrée dédiée `-v`, pas par le côté ; transition `+u` atterrit sur la tuile nord vide → items stagnant, trans=2 out1=out2=0) → fallback **belt `+v` intermédiaire `split_entry_v` VALIDÉ** (trans `+u`→belt `+v` sideload belt→belt → splitter entrée `-v`, flux réparti out1=1 out2=1). Implémenté dans `_route_bus_to_target` (note `split_entry_v` à `(u_root_entry, v_root_in)` + transition `range(u_bus+1, u_root_entry)`). **CONSTAT** : le tap S1e reste **non-circuiterie-connexant** car le sideload bus `+v`→transition `+u` d'une lane **continue** est aussi impossible (le bus dépose vers `+v`, pas `+u`, donc la transition `(u_bus+1, v_root_in)` ne reçoit jamais le flux) → lacune structurelle → **S1f underground** (la lane est "percée" par un underground qui dérive le flux vers `+u` sans couper la lane ; la belt `split_entry_v` déjà posée feed alors le splitter). `test_bus_tap_splitter` enrichi (assertion `split_entry_v`). **Volet B (feed) revert + reporté S1f** : tentative `feed_side="bus"` (routing -u + merger V sur lanes virtuelles côté bus) géométriquement **cassée** — les belts_out sont dir `-u` (vers le bus) mais le merger orient V attend des lanes dir `+v` (entrée `-v`) → le merger ne reçoit pas le flux (directions incohérentes) ; + 187 `feed_liaison_collision` (les belts_out S1c sont **déjà** longés en `-u` vers le bus, le re-routing collisionne à 100%). Revert → feed S1d (côté étage, CONSTAT collision merger↔belts_in consommateur, 0 merger posé). Le feed correct nécessite : virage `-u`→`+v` à l'extrémité bus + merger tree V sur lanes dir `+v` + re-route vers la lane, avec collisions structurelles (merger sur la lane bus + croisements de bus lanes) → **underground belts (S1f)**. Collisions totales 194→10.
- Longues rangées haut-débit (ex. 192 furnaces → 650 tuiles) débordent sur terrain inconnu (eau/obstacles hors zone scannée) → `can_place` échoue sur le core. Problème **S4** (adaptation terrain / replan), pas S1.
- **S1g (2026-07-24)** : **volet C (feed main bus) résolu** par re-planification spatiale. Le CONSTAT S1d/S1f (merger feed M>2 collisionne les belts_in consommateur car étages alignés sans gap ; M belts_out // ne peuvent virer -u sur même rangée v) est levé par : (a) **gap entre étages** `gap_feed = 4·ceil(log2 M)+6` conditionné par `out_item in bus_items` (libère la zone `v_out+1..v_out+gap` ; mines ore non-bus → pas de gap → transition S1a préservée) ; (b) **merger tree côté étage conservé** (`_build_merge_tree`, M-1 mergers, count inchangé) dans le gap ; (c) **1 seul belt vire +v→-u** (T5) puis belts -u traversent les lanes bus intermédiaires via `_under_crossing` (T1) puis **sideload -u→+v sur la lane produit** (T6, merger gratuit belt→belt, lane continue, pas de merger-lane → count M-1 conservé). **Validation live** : `verify_feed_s1g.py` (mécaniques T5/T6/T7 isolées, lane aval alimentée) + `verify_layout_s1g.py` (usine gear@30/s main bus, 12/12 recs, 0 merger_collision, can_place 100% sur portion dans map générée). **Artefact headless** : en serveur headless sans character, la map n'est générée que sur la `starting_area` (frontière y≈318) ; `force.chart` révèle la visibilité mais ne génère PAS le terrain (`get_tile` reste "out-of-map", aucune API `request_from_area`/`set_chunk_generated`/`generate_chunks` en Factorio 2.0). L'usine gear@30/s s'étend à y=668 → 3323/7292 entités au-delà de la frontière = `out-of-map` (can_place faux négatif, **hors-scope S1g** — en jeu réel le joueur génère la map en explorant). Le validateur filtre via `_map_frontier` et mesure le can_place sur la portion générée (3969 entités, 100% OK, 0 collision interne). Le feed S1g lui-même est validé indépendamment (`verify_feed_s1g.py` à zone 1200,1200 via `create_entity` qui génère le chunk à la volée). Collisions 2→0. Back-compat stricte préservée (tests bande S0/S1a/S1b inchangés, chaîne fer 1→1 = 0 splitter/merger, counts N-1/M-1).
- Throughput inserter **dynamique** selon swing non mesuré (k=0 conservé) — nécessiterait une extension du mod (poser un circuit source→inserter→destination, compter les items transférés sur N ticks). Back-compat S0 préservée.

### Statut S2a (2026-07-26)

| Sous-étape | Tests | Statut |
|---|---|---|
| S2a-mod (describe `type`/`fluid_boxes`/`output_fluid` + `scan_water_edge` + spawn test crude-oil+coal+eau) | — | ✅ tools.lua/player.lua/operations.lua/control.lua/mod_api.py |
| S2a-kb (Recipe/MachineSpec/knowledge.py const + `pipe_throughput` + GEOMETRY_FIXTURE fluides) | — | ✅ FLUID_ITEMS/FLUID_RAW_RESOURCES/CATEGORY_DEFAULT_MACHINE/THROUGHPUTS étendus (ajouts seulement) |
| S2a-solveur (`transport`/`phase` par nœud + `mining_machine(item)` item-aware + garde `FLUID_RAW_RESOURCES`) | unitaires | ✅ back-compat stricte (chaîne fer inchangée, shim `mining_machine(machine_tiers)`) |
| S2a-layout (`_place_pumpjacks`/`_place_offshore_pump`/`_place_pipe_segment` + `_place_stage` branchement fluide + `plan()` dispatch) | 28/28 | ✅ pytest 28 passed (20 back-compat S0/S1 + 8 S2a) |
| S2a-live (`verify_layout_s2a.py`) | 12/12 OK | ✅ validé en jeu headless (2026-07-26) : spawn crude-oil+coal+eau, scan_patch/scan_water_edge, describe basic-oil-processing type fluide, solve plastic-bar@2 phases, plan() totals pipe=103/pumpjack=60/oil-refinery=3/chemical-plant=1 + connexions, can_place 35/35 (0 collision interne ; 2 pipes sur tuile water du bassin spawné = obstacle terrain filtré), back-compat fer 0 pipe |

**Décisions S2a figées** :
- Layout fluide = **direct pipe machine→machine** (chaîne 1→1, **pas de splitter/merger fluide**, pas de pipe-bus en S2a). Pipe-bus = S2c plus tard. Pipes 1×1 à 4 ports, junction auto Factorio.
- `pipe_throughput(name, length)` affine `base - k*(length-ref)`, **k=0 en S2a** (débit constant, back-compat). Ajustement post-live (S2b).
- `mining_machine(item)` item-aware : `water`→offshore-pump (débit `THROUGHPUTS["offshore-pump"]`=1200/s), `crude-oil`→pumpjack (`mining_speed`), sinon drill (back-compat S0/S1). Shim back-compat `mining_machine(machine_tiers)` préservé.
- `populate_from_rcon` indexe par **produit principal** (`products[0].name`) — back-compat solides (nom recette == produit : iron-plate, iron-gear-wheel), fluides (basic-oil-processing → clé `petroleum-gas` car le solveur cherche `recipe_of(item_produit)`).
- `Recipe.ingredients` type inchangé ; champs additifs (`ingredient_types`/`product_types`/`fluid_ingredients`/`fluid_products`) défauts vides → recettes solides inchangées (back-compat).
- `StageLogistics` += `phase`/`pipes_in_per_stage`/`pipes_out_per_stage` (défauts solid) ; `_place_drills`/`_place_stage` blocs solides inchangés (back-compat). `plan()` dispatch bus_layout inchangé ; branche directe += `transport=="pipe"` → pumpjack/offshore-pump.

**Limitations S2a reportées** :
- **CONSTAT API 2.0 (fluides)** : `proto.fluid_boxes` et `proto.output_fluid` sont **inexistants** sur le prototype au runtime (pcall → `"LuaEntityPrototype doesn't contain key fluid_boxes/output_fluid"`). Instance `fluidbox[i].pipe_connections` également inaccessible. Extension du constat S0 (géométries fines) aux fluides : positions `fluid_boxes` **hardcodées** en Python (`GEOMETRY_FIXTURE`, source de vérité), validées indirectement via `can_place` (les pipes se connectent aux machines sans collision). Recs describe 5/6/7 du validateur = CONSTAT documenté (limitation API, pas un bug LayoutPlanner).
- **Pumpjack mining_speed** : le solveur utilise `mining_speed` prototype sans modéliser le yield du gisement (débit réel dépend du patch `amount`). Count pumpjacks surestimé en live (60 pumpjacks pour crude-oil@60/s sur patch 3×3) → `can_place` du validateur exclut les pumpjacks (validés séparément : 1 pumpjack can_place OK sur gisement). Modèle de débit fluide = S2b.
- **Pipe-bus / multi-produits** (advanced-oil-processing heavy+light+petroleum) : S2c. `populate_from_rcon` indexe par `products[0]` seul (S2a = basic-oil-processing 1 produit).
- **can_place pipe sur tuile water** : le bassin water spawné pour le test (scan_water_edge + offshore-pump) à (14-16, 8-9) tombe dans le chemin +u des pipes crude-oil du layout → 2 pipes sur tuile water (non-constructible). Artefact terrain (S2a ne contourne pas les obstacles = S4 future), filtré dans le validateur via `get_tile` (coord tuile = `int(pos)`, pas `:.0f` qui fait round-half-to-even → mauvaise tuile). **offshore-pump can_place OK** en live (op=True sur tuile eau du bassin).

### Statut S2b (2026-07-26)

| Sous-étape | Tests | Statut |
|---|---|---|
| S2b-1-mod (`measure_entity` fluid_boxes instance + commentaire CONSTAT API 2.0 + wrapper `measure_fluid_boxes`) | — | ✅ tools.lua lit `ent.fluid_boxes`/`ent.output_fluid` sur l'INSTANCE posée (pcall) ; mod_api.py wrapper ; branche describe documentée morte en 2.0 |
| S2b-1-kb (`Recipe.result_counts` + `result_count_for` + `RECIPE_PREFERENCE` + `recipes_by_product` + `recipe_of` sélecteur + `populate_from_rcon` multi-produits + `GEOMETRY_FIXTURE` K7 5 ports + `organic-or-chemistry`) | — | ✅ ajouts additifs (défauts vides) ; `petroleum-gas[0]=basic-oil-processing` (back-compat plastic-bar) ; `kb.recipes` préservé (fallback) |
| S2b-1-solveur (`result_count_for` propagation + `coproducts` + sinks `role="store"` `machine="storage-tank"` + ordre topologique sinks après source + `source_item` + `_role_for` organic-or-chemistry) | unitaires | ✅ back-compat stricte (chaîne fer inchangée, plastic-bar S2a 0 sink) ; `ProductionNode.source_item` additif |
| S2b-1-layout (`_place_stage` multi-pipe 1+n_coproducts + `out_idx_by_item` multi-produit + `_place_storage_tank` + connexion co-produit→storage-tank + `ordered` sinks après source) | 34/34 | ✅ pytest 34 passed (28 back-compat S0/S1/S2a + 6 S2b-1) |
| S2b-1-live (`verify_layout_s2b.py`) | 14 recs | ✅ 14/14 OK — recs measure 1-2,13 VALIDÉS EN COMPLET (`fluidbox.get_prototype` oil-refinery 5 boxes/20 positions + chemical-plant 4 boxes + K7 3 outputs) ; rec 3 offshore-pump `output_fluid` reste CONSTAT (`ent.output_fluid` vraiment inexistant 2.0) |
| S2b-2-kb (`inject_power_units` recette synthétique steam `boiling` + `MachineSpec` boiler/steam-engine + `CATEGORY_DEFAULT_MACHINE["boiling"]→boiler` + `FLUID_CATEGORIES+="boiling"` + `GEOMETRY_FIXTURE` steam-engine 3×5 1 port input) | — | ✅ ajouts additifs/idempotents ; recette steam côté Python (pas RCON, steam n'est pas une recette Lua) ; `populate_from_rcon` appelle `inject_power_units` en fin ; back-compat (chaînes solides non affectées) |
| S2b-2-solveur (`_role_for("boiling")→"fluid"` + sink co-produit orphelin steam → `steam-engine` `role="power"` `machine_count=ceil(rate/30)` via `STEAM_ENGINE_CONSUMPTION=30` ; autres fluides orphelins → storage-tank) | unitaires | ✅ back-compat stricte (40→44 passed) ; règle steam-engine testée via recette fictive cogen (aucune recette Factorio ne co-produit steam) |
| S2b-2-layout (`_place_stage` gère le boiler nativement = étage fluide mono-produit water→steam ; `_place_storage_tank` renommée `_place_fluid_sink` générique pour sink fluide mono-input → gère steam-engine `role="power"` ; dispatch `plan()` route `role in ("store","power")` vers `_place_fluid_sink` ; ordre topologique sinks `store`+`power` après source) | 44/44 | ✅ pytest 44 passed (40 back-compat S0/S1/S2a/S2b-1 + 4 S2b-2) |
| S2b-2-live (`verify_layout_s2b_2.py`) | 12 recs | ✅ 12/12 OK — recs 1-4 validés en complet (`measure_fluid_boxes` boiler 2 boxes + steam-engine 1 box, `prototypes.fluid` steam/water) ; rec 12 CONSTAT débit boiler (propagation passive sans fuel inactive, hardcodé 60 steam/s) |
| S2b-3-kb (`pipe_throughput(name, length, fluid=None)` étendu affine `base - (k_name + k_fluid)*(length - ref)` + `FLUID_VISCOSITY` hardcodée wiki {water:0, steam:0, petroleum-gas:0.1, light-oil:0.5, crude-oil:1, heavy-oil:1, lubricant:1, sulfuric-acid:1}) | — | ✅ signature étendue `fluid=None` par défaut (back-compat : appels 2-args S2a inchangés, k_fluid=0 → débit constant `THROUGHPUTS`) ; viscosités NON lisibles runtime (prototypes.fluid sans champ viscosité) → hardcodé wiki (modèle affine simplifié, approximation) |
| S2b-3-layout (`_place_stage` branche fluide : `n_lanes = ceil(rate_effective / pipe_throughput_fn(pipe_tier, n_seg, node.item))` min 1 → `n_lanes` pipes parallèles espacés de 1 tuile en +u ; `pipes_out_per_stage = n_lanes + n_coproducts` ; `pipe_throughput_fn` passé DIP depuis `LayoutRequest`) | 47/47 | ✅ pytest 47 passed (44 back-compat S0/S1/S2a/S2b-1/S2b-2 + 3 S2b-3) ; back-compat : k_fluid=0 (water/steam) ou rate≤cap → n_lanes=1 (S2a/S2b-2 inchangés, rec 14 S2b-1 pipes_out=3 préservé) |
| S2b-3-live (`verify_layout_s2b_3.py`) | 12 recs | ✅ 12/12 OK — rec 7 **fix K7 VALIDÉ** : stage K7 propre (0 duplicata + 0 cross_adj zone stage u=24-26), routing résiduel duplicates=3 + cross_adj=21 (CONSTAT S2c underground crossings) ; rec 9 multi-lane DIP cap=10 → n_lanes=6 can_place 25/25 ; rec 12 CONSTAT viscosités non lisibles (`pcall f.viscosity` → ERR, hardcodé wiki) |
| **S2c** (`_pipe_under_crossing` + instrumentation segment u `_place_pipe_segment`) | 48/48 unit + 12/12 live | ✅ **Fix routing minimal — underground crossings pipe-to-ground**. Portée = "Fix routing minimal" (pas de pipe-bus complet). Mécanique pipe-to-ground 1×1 : 1 port surface (côté amont) + 1 port souterrain (vers jumeau) ; le souterrain NE junctionne PAS avec les pipes de surface (canal séparé). Crossing routing +u traverse lane +v (INTACTE en surface, pipe normal 4 ports, pas de skip) via 2 pipe-to-ground : INPUT `(lane_u-1, v_cross)` dir +u + OUTPUT `(lane_u+1, v_cross)` dir +u. `_pipe_under_crossing` mute in-place le pipe amont en pipe-to-ground INPUT (pattern `_under_crossing` S1f), pose OUTPUT via `_add`+`entities[idx].ug_type="output"`. Totals : `pipe -= 1`, `pipe-to-ground += 2`. Garde `no_room` : ne pose pas OUTPUT s'il dépasserait le sink (`out_u = cu+step`, `has_room = (out_u-ut)*step <= 0`). Fallback skip + note `pipe_collision_S2a` si crossing impossible. Constantes `PIPE_TO_GROUND_NAME="pipe-to-ground"` + `PIPE_UNDERGROUND_MAX=10` (Factorio 2.0 hardcodé, non lisible runtime) ; entrée `pipe-to-ground:{"w":1,"h":1}` au `GEOMETRY_FIXTURE`. **CONSTAT FONDAMENTAL S2c = connectivité vs séparation** : le crossing rétablit la CONNECTIVITÉ du sink éloigné (petroleum traverse la lane via souterrain au lieu d'être coupé par le skip/trou → sink alimenté, validé live `p2g=2 petroleum_traverse_lane=True`) mais NE RÉDUIT PAS le mélange (`cross_adj` inchangé : le OUTPUT à `lane_u+1` reste adjacent au light segment v à `lane_u` et à la lane heavy → junction cross-product persistante). Le 1er sink (light) est trop proche (`ut=27.0 < lane_u+1=27.5` → garde `no_room` → skip à la lane = trou, connectivité coupée pour light). DISTINCTION CRITIQUE : les "dups" floor (2 pipes à 0.5 d'écart, même tuile `floor()`) sont des **adjacences de junction** (mélange de connectivité, pipes Factorio distincts), PAS des collisions — `can_place` live passe 201/201 OK malgré les dups Python. Métriques live inchangées (3 dup + 21 cross_adj) car le crossing change le flux (souterrain), pas les adjacences de pipes. Séparation 100% = S2d pipe-bus (lanes parallèles + taps dédiés). Back-compat stricte : pytest 48/48 OK (`_add`/`_place_pipe_segment` signatures inchangées, chaîne fer 0 pipe, plastic-bar S2a, solid-fuel S2b-1, steam S2b-2 inchangés ; mono-produit `coproduct_items` vide → pas de crossing → comportement inchangé). Tests : `test_layout_solid_fuel_chain` assert `pipe-to-ground > 0` + `not petrol_skip_lane` (connectivité) + seuils bornés `dups<=1 cross<=10` CONSTAT S2d documenté ; `test_backcompat_s2b` assert `pipe-to-ground==0` fer/plastic-bar. Live : `verify_layout_s2b_3.py` 12/12 (rec 7 = assertion connectivité `p2g>0 and not petrol_skip_lane` + CONSTAT séparation S2d) ; `verify_layout_s1g.py` 12/12 + `verify_layout_s2a.py` 12/12 inchangés. **PROCHAINE = S2d** (pipe-bus fluide complet : lanes parallèles + taps dédiés → séparation 100% routing co-produit→sink, éliminer duplicates=3 + cross_adj=21 résiduels) |
| **S2d** (`_place_stage` branche multi-produit restructurée + helper `_place_pipe_bus_stub`) | 48/48 unit + 12/12 live | ✅ **Pipe-bus fluide complet — lanes parallèles par produit**. Portée = "Pipe-bus complet (lanes parallèles)". Restructuration branche `_place_stage` multi-produit `if coproduct_items and n_lanes==1` : 1 lane continue PAR produit, parallèles en u espacées de 2 tuiles (non adjacentes) — `lane_us = [ou_i_base+2, +4, +6]` (heavy/light/petroleum). Chaque lane = pipe continu +v couvrant `v_start..v_start+n_seg-1` (`v_start=v0-half_v+0.5=8.5`, `n_seg=24` pour N=4), dir `FACING_DIR_V[facing]`, `node_item` = son produit. Helper nouveau `_place_pipe_bus_stub(entities, totals, pipe_name, item, facing, ou_i_base, lane_u, v_port, intermediate_lanes, notes)` : pose pipe au port `(ou_i_base, v_port)` + pipes normaux `ou_i_base+1..first_lane_u-1` + **crossing multi-lanes UNIQUE** (mute `first_lane_u-1` en pipe-to-ground INPUT dir_u, pose OUTPUT à `last_lane_u+1` dir_u, distance = last-first+2 ≤ `PIPE_UNDERGROUND_MAX=10`) + pipes normaux `last_lane_u+2..lane_u-1`. Totals : `pipe -= 1` par crossing, `pipe-to-ground += 2`. heavy=0 crossing, light=1 paire (lane heavy), petroleum=1 paire multi-lanes (heavy+light, distance 4). Sinks alignés au v de leur port : `sink_av_by_coproduct[cp] = machine_v[-1] + cp_dv` retourné comme **11e élément** du tuple `_place_stage` (non déballé par callers existants `r[:9]`/`r[9]` → back-compat strict) ; `plan()` l'utilise `av_sink = sink_av_by_cp.get(cp, av)` (fallback `av` → mono-produit inchangé). Routing heavy sort au bout -v : `belt_out_last` = pipe heavy à `v_start-2` (SOUS les lanes co-produits) → 0 crossing, 0 mélange. `_u_next_min = lane_us[-1] + stage_gap` → `u_next = max(u_machine+offset_out_u+stage_gap, _u_next_min)` pour que sinks + étage suivant clarent le bus. **DISTINCTION collision vs mélange** : "dups" floor (2 pipes 0.5 d'écart, même tuile `floor()`) = adjacences de junction (mélange connectivité pipes Factorio distincts) PAS collisions — `can_place` OK malgré dups Python. `cross_adj` false-positive (pipe-to-ground × lane, port surface pointe away) éliminé par filtrage `ug_type==""` (pipe-normal only) dans `_detect_separation` ET `test_layout_solid_fuel_chain`. **Résultat live rec 7** : `pipe-to-ground=18 petroleum_traverse_lane=True duplicates=0 cross_adj=0 dup=[] cross=[]` = **séparation 100%** (avant S2d : 3 dup + 21 cross_adj). Back-compat stricte : pytest 48/48 OK (`test_layout_solid_fuel_chain` assert `dup==0`+`cross<=6` + `test_backcompat_s2b` assert `pipe-to-ground==0` fer/plastic-bar) ; mono-produit (coproduct_items vide OU n_lanes>1) → branche `else` inchangée (S2a/S2b-2/fer/plastic-bar/steam) ; signatures `_add`/`_place_pipe_segment`/`_pipe_under_crossing` inchangées ; 11e élément non déballé par callers. Live : `verify_layout_s2b_3.py` 12/12 (rec 7 dup=0 + cross_adj=0) + `verify_layout_s1g.py` 12/12 + `verify_layout_s2a.py` 12/12 inchangés. CONSTAT : multi-lane n_lanes>1 multi-produit NON couvert (branche `else`, CONSTAT S2c persiste pour multi-lane, documenté). **PROCHAINE = S3 beacons / S4 terrain** |
| **S3a** (`ModuleEffect` dataclass + `ProductionRequest.module_effects` + application solveur + `MachineSpec` electric-furnace) | 52/52 unit | ✅ **Solveur + ModuleEffect Option A (agrégé injecté, pas calculé par le solveur)**. `ModuleEffect(speed_bonus, productivity_bonus, energy_bonus)` dataclass frozen, fournie via `ProductionRequest.module_effects: dict[str, ModuleEffect]` (clé=nom machine). Découplage solveur↔layout : évite la circularité (machine_count calculé AVANT placement beacons). Formule (l.189) : `effective_speed = m.crafting_speed * (1 + speed_bonus)`, `effective_productivity = 1 + productivity_bonus`, `per_machine = result_count_for(item) * effective_productivity * effective_speed / craft_time_sec`, `count = math.ceil(rate/per_machine)` **inchangé**, `eff = count * per_machine` inchangé. Propagation (l.214) : `ing_rate = eff * ing_amount / (rc_item * effective_productivity)` — la productivité produit des bonus gratuits SANS consommer d'ingrédients. `energy_bonus` = audit seulement (solveur l'ignore). `ProductionNode` += champs audit `speed_bonus`/`productivity_bonus`. `MachineSpec("electric-furnace", crafting_speed=2.0, categories={"smelting"}, type="furnace", energy_source="electric", module_slots=2)` ; sélectionnable via `machine_tiers={"smelting":"electric-furnace"}`. Back-compat stricte : `module_effects={}` ⇒ `effective_productivity=1` ⇒ formule S2 inchangée (test pivot `test_arrondi_propagation_effectif` préservé). Tests runner maison : `test_module_speed_bonus` (iron-gear-wheel@4.3 + speed_bonus=1.0 → count=3 au lieu de 5), `test_module_productivity_bonus` (productivity_bonus=0.25 → plate propagé=8.0 et NON 10.0), `test_no_module_backcompat` (identique S2), `test_electric_furnace_tier` (iron-plate@10 speed=2 → count=16). pytest 52 passed (48 back-compat + 4 S3a). Pas de serveur. |
| **S3b** (`tools.lua` describe/measure beacon + `MODULE_FIXTURE`/`BEACON_FIXTURE`/`GEOMETRY_FIXTURE["beacon"]/["electric-furnace"]` + `compute_module_effect` + `measure_beacon`) | 52/52 unit + 12/12 live | ✅ **API runtime beacons + modules + electric-furnace (CONSTAT live raffiné)**. `PRODUCER_TYPES` += `"beacon"` ; `describe` branche beacon pcall `supply_area_distance`/`module_slots`/`allowed_effects`/`distribution_effectivity`/`max_energy_usage` → `entity.beacon` ; `measure_entity` étendu round-trip `ent.insert` + `get_module_inventory()` ; `scan_factory`/`entity_row` lit `e.get_module_inventory()` pour beacons. `STARTING_ITEMS` += beacon/speed-module-3/productivity-module-3/electric-furnace. Fix Lua `measure_entity` l.555-558 : `local _, sad = pcall(...)` + `if sad then` stockait le MESSAGE D'ERREUR pcall (truthy) comme valeur → corrigé en `local oksa, sad = pcall(...)` + `if oksa and sad ~= nil then` (pattern describe l.328). `compute_module_effect(beacon_count, module_name, beacon_name)` : **formule 2.0 FFF#409 rendements décroissants** `dist * sqrt(n) * module_bonus` (était LINÉAIRE 1.1 `n*dist*mod`). **CONSTAT live raffiné (contraire aux prédictions)** : `supply_area_distance` **INACCESSIBLE** pour beacon (ni proto ni instance ne l'exposent, contrairement aux electric-poles) → fallback fixture 3.0 ; `distribution_effectivity` **ACCESSIBLE live=1.5** (PAS 0.5 valeur 1.1, Factorio 2.0 rebalancé FFF#409 ×3) → `BEACON_FIXTURE` dist 0.5→1.5 ; `module_slots` **INACCESSIBLE** (proto) → fixture 2 ; `allowed_effects` ACCESSIBLE (table) ; `max_energy_usage` INACCESSIBLE ; `get_module_inventory()` **ACCESSIBLE** sur instance beacon (round-trip insert+scan VALIDÉ, retourne 2 slots count=1 chacun pas agrégé) ; `crafting_speed` electric-furnace ACCESSIBLE=2. `GEOMETRY_FIXTURE["beacon"]` = `{w:3,h:3,supply_area:3.0,module_slots:2,distribution_effectivity:1.5}` (fix bug S3b-8 : populate_from_rcon lit `fix` qui n'avait pas module_slots/distribution_effectivity). 8 beacons speed-module-3 → 1.5*sqrt(8)*0.5=2.12 (×3.12) vs 1.1 : 2.0 (×3) ; 16 beacons → 1.5*4*0.5=3.0 (×4) vs 1.1 : 4.0 (×5). Back-compat : `describe(stone-furnace)` sans beacon block. Live `verify_layout_s3b.py` 12/12 OK. |
| **S3c** (`LayoutConstraints` beacons + `LayoutEntity.modules` + placement beacons dans `_place_stage` côté +u) | 55/55 pytest + 161/161 runner + 8/8 live | ✅ **Placement beacons + modules insérés**. `LayoutConstraints` += `beacon_tier="beacon"`, `module_tier="speed-module-3"`, `beacons_per_stage=0` (0=pas de beacons, back-compat), `modules_per_beacon=2`. `LayoutEntity` += `modules: list = field(default_factory=list)` (role `"beacon"`). `_place_stage` : bloc beacons APRÈS poles (lecture `constraints.beacons_per_stage` directement, pas de changement signature). **Géométrie** : `u_beacon_pos = u_machine + offset_out_u + 1.0 + beacon_half_u` (offset_out_u = half_u+1.5) → edge-to-edge machine-beacon = 2.5 < supply_area(3.0) → couverture garantie quelle que soit la taille machine. Poles relocalisés conditionnellement au-delà des beacons quand actifs (`pole_u = u_beacon_pos + beacon_half_u + 1.0 + pole_half_u` vs back-compat `u_machine + offset_out_u + 2.0`). `n_beacons = max(1, int(constraints.beacons_per_stage))` posés uniformément le long de v de `row_first` à `row_last`, `entities[idx].modules = [module_tier]*modules_per_beacon`. `u_next` étendu : `u_next = max(u_next, u_beacon_pos + beacon_half_u + stage_gap)`. Poseur RCON fait `ent.insert({name=module, count=...})` après `create_entity`. Tests : `test_no_beacon_backcompat` (0 beacon défaut), `test_beacon_row_placement` (beacons posés, modules=[speed-module-3]*2, u_beacon=u_machine+5.5, aucune collision), `test_beacon_coverage` (chaque machine <= supply_area(3) d'un beacon). Back-compat stricte : `beacons_per_stage=0` défaut ⇒ aucun entity "beacon", `u_next` identique S2, 48 tests existants inchangés, signatures publiques préservées. Live `verify_layout_s3c.py` 8/8 OK : iron-plate@10 + electric-furnace + 4 beacons speed-module-3 → `compute_module_effect(4,"speed-module-3")`=1.5 speed_bonus → effective_speed=5.0 → count=7 (vs 16 sans bonus), can_place 167/167 0 collision, rec 7 LIVE beacon+insert+scan `get_module_inventory` validé, back-compat 0 beacon. Back-compat live : `verify_layout_s1g.py` 12/12 + `verify_layout_s2a.py` 12/12 + `verify_layout_s2b_3.py` 12/12 inchangés. |
| **S3d** (`LayoutConstraints.beacons_neg_per_stage` + placement beacons côté -u dans `_place_stage` + réservation `u_next`) | 59/59 pytest + 169/169 runner + 8/8 live | ✅ **Beacons côté -u (double couverture "8 beacons" = 4 +u + 4 -u)**. `LayoutConstraints` += `beacons_neg_per_stage: int = 0` (0=pas de beacon -u, back-compat S3c). Gate `beacon_neg_active = (gbeacon is not None and N>0 and not out_fluid and constraints.beacons_neg_per_stage>0)` indépendant de `beacon_active`. **Géométrie miroir** : `u_beacon_neg_pos = u_machine - offset_out_u - 1.0 - beacon_half_u` (= u_machine-5.5 pour machine 3×3, miroir du +u) → edge-to-edge machine-beacon = 2.5 < supply_area(3.0) → couverture symétrique garantie. `n_beacons_neg = max(1, int(constraints.beacons_neg_per_stage))` posés uniformément le long de v (mêmes `row_first`/`row_last` que +u), `entities[idx].modules = [module_tier]*modules_per_beacon`, `totals[beacon_tier] += n_beacons_neg`. **Gate collision (D3)** : avant de poser, vérifie l'absence de chevauchement (bounding-box 2 axes) du candidat -u contre les entités existantes (`entities` déjà placées : belts_in/inserters étage courant + étages précédents). Si collision → skip le beacon -u pour cet étage + note `beacon_neg_collision:<node.item>` (multi-ingrédients : belt ing1 long-handed reach 2.0 à u_machine-5.5 = position miroir → collision → skip). **Réservation inter-étage (D4)** : `u_next = max(u_next, cur_max_edge + 6.5)` où `cur_max_edge = (u_beacon_pos+beacon_half_u)` si `beacon_active` sinon `(u_machine+offset_out_u+0.5)` ; le 6.5 = décalage bord -u du beacon -u suivant (prev_u_next-5.5) + gap 1.0 → garantit pas de collision inter-étage. **CONSTAT probe** : la réservation D4 étend `u_next` mais les transition belts (placées par `plan()` entre `_place_stage` calls) remplissent le gap créé → seul le 1er étage machine (face aux drills, -u libre) obtient les -u beacons ; les étages suivants (face à un étage machine) sont skippés (collision transition belts). Comportement honest documenté (Risque #2 du plan). Bonus solveur (Option A S3a) : FactoryBuilder calcule `compute_module_effect(beacons_per_stage+beacons_neg_per_stage)` sur le total demandé et injecte dans `module_effects` — si le layout skip des -u, le bonus réel est inférieur (responsabilité FactoryBuilder, hors-scope S3d). Tests : `test_no_beacon_neg_backcompat` (0 défaut), `test_beacon_neg_row_placement` (beacons -u au miroir u_machine-5.5, modules, aucune collision), `test_beacon_neg_double_coverage` (chaque machine couverte ≥1 beacon +u ET ≥1 -u, edge-to-edge ≤3.0 des deux côtés), `test_beacon_neg_multi_ing_skip` (2 ings → note `beacon_neg_collision`, beacons +u présents, aucun crash). Back-compat stricte : `beacons_neg_per_stage=0` défaut ⇒ `beacon_neg_active=False` ⇒ aucune vérification, aucun beacon -u, `u_next` S3c/S2 inchangé, 55 tests S3c préservés, signatures `_place_stage` inchangées. Live `verify_layout_s3d.py` 8/8 OK : iron-plate@10 + electric-furnace + 4+4 beacons → `compute_module_effect(8,"speed-module-3")`=2.121 speed_bonus → effective_speed=6.243 → count=6 (vs 7 S3c, vs 16 sans bonus), totals beacon=8 (4+u + 4-u), beacons -u au miroir u_machine-5.5 modules=[speed-module-3]*2, can_place 158/158 0 collision, rec 7 LIVE beacon+insert+scan validé, back-compat 0 beacon. Back-compat live : `verify_layout_s3c.py` 8/8 + `verify_layout_s1g.py` 12/12 + `verify_layout_s2a.py` 12/12 + `verify_layout_s2b_3.py` 12/12 inchangés. **S3 COMPLET (a+b+c+d)**. **PROCHAINE = S4 terrain** |

### Statut S4 (2026-07-27)

| Sous-étape | Tests | Statut |
|---|---|---|
| S4a (mod Lua `scan_obstacles`/`scan_tiles_bbox`/`get_tile` non-destructif cap 400/200×200 + wrappers `ModApi` + `test_mod_api.py`) | 6/6 unit | ✅ Additions pures (aucune signature existante modifiée) ; non-destructif (lecture seule) ; `radius` defaulted+capé 400 ; `out-of-map` retourné en headless = artefact documenté (pas de `generate_chunks` 2.0). Live `verify_layout_s4a.py` 8 recs à valider post-relance serveur. |
| S4b (planner `_occ_terrain` per-entité + `_plan_with_replan` replan auto déterministe + `plan()` dispatcher + `cascade_offset_v`/`bypass_offset_v`/`bypass_max_offset_v`/`constructible_zone`/`replan_budget`/`terrain_check`) | 8/8 unit (67 pytest, 177 runner) | ✅ Détection per-entité précise (remplace post-hoc global quand `terrain_check=True`) ; replan auto shift `cascade_offset_v` (propage via `v_out` S1a, drills sur `patch.bbox` indépendantes) ±`bypass_offset_v`/±2× puis pivot `facing` ±90°/180° ; `tried` anti-boucle, `constructible_zone` garde-fou, `replan_exhausted` → handoff FactoryBuilder. **Back-compat stricte** : `terrain_check=False`+`replan_budget=0` défauts ⇒ `plan()` → `_plan_core` S3d inchangé (post-hoc global préservé) ; 59 tests S3d + 8 S4b = 67 pytest, 169+8 = 177 runner. Live `verify_layout_s4b.py` 10 recs (CONSTAT S2a contourné) à valider post-relance. |
| S4c (`FactoryBuilder.build_layout` arbitre replan lourd gisement/tier + `_build_terrain`/`_merge_constraints` + `Contract.replan_budget`/`layout_constraints` + `zone`→`constructible_zone`) | 8/8 unit (29/29 test_factory_builder) | ✅ Frontière LLM/déterministe (§7) : planner déterministe pur (replan léger offset/facing S4b), FactoryBuilder stratégique (replan lourd gisement `scan_patch` rayon croissant 400/800/1200 + tier belt yellow→red→blue). Garde-fou tier sans géométrie → skip (ne court-circuite pas le replan en retournant `missing_geometry`). `Contract` += `replan_budget=4`/`layout_constraints=None` défauts (back-compat : tests existants ne construisent pas de `LayoutRequest` via FactoryBuilder). **Back-compat stricte** : `build_layout` NOUVELLE méthode (ne casse pas `decide`/`run_loop`) ; runner 177/177 + pytest 67/67 inchangés. Live `verify_layout_s4c.py` 11 recs (chaîne complète FactoryBuilder→plan()→can_place_check) à valider post-relance. |

**Décisions S4 figées** (AskUserQuestion + plan approuvé `spicy-gliding-dawn.md`) :
1. **Découpage incrémental S4a/S4b/S4c** (valider chaque sous-étape avant la suivante).
2. **Frontière replan = planner contourne + replan auto déterministe AVANT handoff** : le replan auto (shift `cascade_offset_v` / pivot `facing`, règles fixes) reste déterministe (pas d'LLM dans le planner) ; le replan lourd (changer de gisement cible, monter tier belt) = FactoryBuilder (S4c, LLM optionnel pour la décision stratégique).
3. **Contournement = shift `cascade_offset_v` (pas offset in-place par étage)** : l'alignement v S1a propage automatiquement tout décalage du 1er étage machine à toute la cascade via `v_out` ; un offset par étage indépendant casserait cet alignement (zigzag). Les drills restent sur `patch.bbox` (indépendantes de l'anchor) ; la belt de collecte drills→étage1 s'allonge (`_place_transition` gère).
4. **Détection per-entité (pas post-hoc global)** : `_occ_terrain` retourne `obstacle|water|out-of-map|None` (bbox-vs-bbox, précision tuile si `tile_grid`). Gated par `terrain_check=True` ; le post-hoc global S3d est conservé pour back-compat (`terrain_check=False`).
5. **S4b n'adresse PAS le CONSTAT S1d headless** (débordement u hors starting_area = artefact, insoluble sans `generate_chunks` 2.0) ; il adresse les obstacles RÉELS scannés (water, rochers) dans la zone constructible (CONSTAT S2a).

**Limitations S4 reportées** :
- **`scan_patch` centré avatar** (pas point arbitraire) → multi-gisement limité au rayon croissant (workaround : `find_nearest` itéré ou scan à rayon croissant 400/800/1200).
- **Out-of-map headless** : FactoryBuilder ne génère pas la map. `scan_obstacles` retourne vide hors starting_area ; `scan_tiles_bbox` retourne "out-of-map" → `Terrain.tile_grid` peuplé → planner détecte. Artefact documenté.
- **Validation live en attente** : `verify_layout_s4a.py` (8 recs) + `verify_layout_s4b.py` (10 recs) + `verify_layout_s4c.py` (11 recs) nécessitent une relance du serveur par l'utilisateur (`scripts/start_factorio_dedicated.bat`, mod S4a modifié). Une seule relance couvre les 3.

**S4 COMPLET (a+b+c)** — clôt la roadmap LayoutPlanner (S0→S4). Suite logique : relier solveur+layout → FactoryBuilder (Coordinator émet `ProductionRequest` + zone/gisement, FactoryBuilder arbitre tiers puis délègue) = couche P2 `agents-roadmap`.

---

**Décisions S2b figées** (AskUserQuestion session précédente) :
1. **Périmètre = Tout** (S2b-1 + S2b-2 + S2b-3), exécuté incrémentalement (valider S2b-1-live avant S2b-2).
2. **Choix recette = `recipes_by_product` + arbitrage** : sélecteur déterministe `recipe_of(item, request=None)` avec `RECIPE_PREFERENCE` hardcodé (FactoryBuilder futur remplacera). Back-compat : 1 candidate → retourne-la ; 0 → fallback `kb.recipes`.
3. **Co-produits orphelins = `storage-tank`** (puit infini déterministe, pas de circuit/valve). Sink `role="store"`, `machine="storage-tank"`, `ingredients=[]` (le fluide entrant est géré par la connexion layout, pas par ingredients — évite l'auto-référence `_depths`).

**Chaîne de test S2b-1** : `solid-fuel` via advanced-oil.
`solid-fuel-from-heavy-oil (heavy 20 → solid-fuel 1) ← heavy-oil (advanced-oil : water 50 + crude 100 → heavy 25 + light 45 + petroleum 55) ← crude-oil (pumpjack) + water (offshore-pump)`. Co-produits orphelins : light-oil 45 + petroleum-gas 55 → 2 sinks storage-tank.

**Chaîne de test S2b-2** : `steam` via boiler (recette synthétique `boiling`).
`steam (boiling : water 60 → steam 60, boiler 1.8 MW → 60 steam/s) ← water (offshore-pump, tile water, 1200/s)`. Steam = cible (pas de sink ; le steam est consommé par steam-engine en jeu, hors solveur). Sink `steam-engine` validé en unitaire via recette fictive `cogen` (water 50 → lubricant 10 + steam 20, steam orphelin → sink `role="power"` `machine_count=ceil(20/30)=1`) car aucune recette Factorio ne co-produit steam. Débits hardcodés (probes live S2b-2) : boiler 1.8 MW, steam `heat_capacity=200` ΔT=150 → 30 000 J/unité → 60 steam/s ; steam-engine 900 kW → 30 steam/s.

**Chaîne de test S2b-3** : `solid-fuel` via advanced-oil (fluide visqueux heavy-oil k_fluid=1), reuse S2b-1. `pipe_throughput("pipe", length, "heavy-oil") = 1500 - 1*(length-1)` (décroît avec la longueur). `n_lanes = ceil(rate / cap)` → en pratique n_lanes=1 (cap~1481 >> rate 20/s pour n_seg~20). Multi-lane forcé en test via DIP (`pipe_throughput_fn` stub cap=10 → steam@60 n_lanes=6). **Test débit séparation fine 3 outputs oil-refinery** : rec 7 détecte duplicatas intra-blueprint (2 pipes sur même tuile) + adjacence cross-product (pipes heavy/light/petroleum adjacents → junction → mélange).

**Limitations S2b reportées** :
- **CONSTAT API 2.0 (fluides, suite S2a)** : `proto.fluid_boxes`/`output_fluid`/`viscosity` INACCESSIBLES sur le prototype au runtime. `measure_entity` étendu lit l'INSTANCE posée (`ent.fluidbox.get_prototype(i)` via pcall, API 2.0 corrigée S2b-1) — si l'instance est aussi opaque (mod non rechargé), retourne `[]` → CONSTAT (hardcode `GEOMETRY_FIXTURE` = source vérité, validé via `can_place`). Recs measure 1-2 du validateur S2b-2 = CONSTAT documenté si opaque. `game.fluid_prototypes` INEXISTANT → API fluides = `prototypes.fluid[name]` (champs `heat_capacity`, `default_temperature`, `max_temperature` [PAS `maximum_temperature`]).
- **Débit boiler/steam-engine non mesuré live** : `max_energy_usage` (boiler 1.8 MW) / `max_energy_production` (steam-engine 900 kW) INACCESSIBLES au runtime → hardcodés wiki. La mesure de débit réel (poser boiler + fuel + ticker) reste non couverte (propagation passive sans fuel inactive, cf. CONSTAT S2b-1 `set_fluidbox`).
- **Séparation fine 3 outputs oil-refinery (fix K7 VALIDÉ S2b-3)** : K7 corrigé aux positions réelles mesurées (`fluidbox.get_prototype` facing east, port actif index 1) — heavy-oil (box 3) port `(2,-2)`, light-oil (box 4) port `(2,0)`, petroleum-gas (box 5) port `(2,2)` : 3 outputs sur le côté +u à v=-2/0/+2 (2 tuiles d'écart → co-produits non adjacents entre eux). `pipe_ports` K7 = `[(-3,0,input),(-3,-1,input),(3,-2,output),(3,0,output),(3,2,output)]` + `output_port_dv={heavy-oil:-2, light-oil:0, petroleum-gas:2}` (mapping produit→port indépendant de l'ordre `fluid_products` RCON). `_place_stage` branche multi-produit (`if coproduct_items and n_lanes==1`) : lane principale décalée `lane_u=ou_i_base+2` (u_machine+5), stubs principal (2 pipes à `ou_i_base` et `ou_i_base+1` au v du port principal `p_dv`) relient port (u_machine+2) → lane, co-produits (1 pipe/machine à `ou_i_base` au v du port co-produit `cp_dv`, v distincts 2 tuiles d'écart). Debug print live a confirmé : node.item=heavy-oil, ou_i_base=24.5, lane_u=26.5, p_dv=-2, coproduct_items=[light-oil,petroleum-gas], machine_v=[11,17,23,29]. **Résultat** : stage K7 0 duplicata + 0 cross_adj (avant 12+14), routing résiduel 3+21 (co-produit→storage-tank traverse la lane u=26.5 → CONSTAT S2c, séparation 100% = underground crossings). Détecteur `_detect_separation` corrigé : `round()` banker's (`round(25.5)=26` ET `round(26.5)=26` confondait stubs u=25.5 et lane u=26.5 sur "tuile 26" = faux duplicata) → `math.floor()` (tuile Factorio = floor : entité 1×1 à x=25.5 occupe tuile 25). Back-compat mono-produit (coproduct_items vide OU n_lanes>1) : branche else lane à `ou_i_base` inchangée (S2a/S2b-2). Portée utilisateur "Éliminer duplicatas + réduire mélange" : stage atteint, mélange résiduel routing CONSTAT S2c.
- **Viscosités pipe hardcodées (CONSTAT S2b-3)** : `prototypes.fluid` n'expose pas de champ viscosité/débit → `FLUID_VISCOSITY` hardcodée wiki (modèle affine simplifié `base - (k_name + k_fluid)*(length - ref)`, approximation). Le modèle de débit pipe réel 2.0 est opaque au Lua. Back-compat : `fluid=None` ou viscosité 0 → débit constant (S2a inchangé).
- **Pipe-bus fluide** (analogie main bus S1c) : S2c. S2b reste direct pipe machine→machine (chaîne 1→1 + sinks).
- **Co-produits partiellement consommés** (surplus partiel) : S2b-1 ne traite que les orphelins (`rates[cp]==0` → sink). Le cas "partiellement consommé" (surplus non-nul) nécessite circuit/valve = S2c.

**Décisions S3 figées** (AskUserQuestion session précédente + plan approuvé) :
1. **Option A — `ModuleEffect` agrégé injecté par FactoryBuilder** (pas calculé par le solveur). `ProductionRequest.module_effects: dict[str, ModuleEffect]` (clé=nom machine). Le solveur **applique** le bonus à `per_machine` sans le calculer depuis le nombre de beacons. Justification : évite la circularité (machine_count calculé AVANT le layout qui place les beacons), déterministe, reflète comment un joueur dimensionne (densité de beacons choisie → bonus calculé une fois → dimensionnement). `energy_bonus` = audit seulement (solveur l'ignore). Découple solveur↔layout.
2. **Formule 2.0 FFF#409 rendements décroissants** : `compute_module_effect` = `dist * sqrt(n) * module_bonus` (était LINÉAIRE 1.1 `n*dist*mod`). `distribution_effectivity=1.5` (vanilla 2.0, ×3 vs 1.1). 8 beacons speed-module-3 → 1.5*sqrt(8)*0.5=2.12 (×3.12) ; 16 beacons → 3.0 (×4).
3. **Placement beacons côté +u** (au-delà des poles), rangée parallèle à l'axe v. `u_beacon_pos = u_machine + offset_out_u + 1.0 + beacon_half_u` → edge-to-edge machine-beacon = 2.5 < supply_area(3.0) → couverture garantie. Poles relocalisés au-delà des beacons quand actifs (position back-compat collisionne sinon). Côté -u (double couverture "8 beacons") = future S3d.
4. **Modules insérés modélisés** : `LayoutEntity.modules: list[str]` (noms de modules insérés). Poseur RCON fait `ent.insert({name=module, count=...})` après `create_entity`. Vérifiable live via `scan_factory` → `e.get_module_inventory()` (accessible runtime sur instance, contrairement aux prototypes).

**Chaîne de test S3c** : `iron-plate` via electric-furnace (smelting) + 4 beacons speed-module-3.
`iron-plate@10/s (electric-furnace, smelting, speed=2, module_slots=2) <- iron-ore (electric-mining-drill)`. Bonus solveur (Option A, formule 2.0 FFF#409) : 4 beacons speed-module-3 → `compute_module_effect(4,"speed-module-3")` = 1.5*sqrt(4)*0.5 = 1.5 speed_bonus → `effective_speed = 2*(1+1.5) = 5.0` → `per_machine = 1*1*5.0/3.2 = 1.5625` → `count = ceil(10/1.5625) = 7` (vs 16 sans bonus → bonus appliqué, machine_count réduit).

**Limitations S3 reportées** :
- **CONSTAT API 2.0 (beacons, suite fluids S2a)** : `supply_area_distance` INACCESSIBLE pour beacon (ni proto ni instance ne l'exposent, contrairement aux electric-poles) → fallback fixture 3.0 (stable, FFF#409 n'a changé que distribution_effectivity). `module_slots`/`max_energy_usage` INACCESSIBLES (proto) → fixtures. `distribution_effectivity` ACCESSIBLE=1.5 (PAS 0.5 wiki 1.1, Factorio 2.0 rebalancé ×3). `allowed_effects` ACCESSIBLE (table). `get_module_inventory()` ACCESSIBLE sur instance beacon (round-trip insert+scan VALIDÉ, retourne 2 slots count=1 chacun pas agrégé). `crafting_speed` electric-furnace ACCESSIBLE=2.
- **Mesure live portée beacon bloquée** : beacon non alimenté (b.energy=0), EEI n'a pas généré, m.effects vide → trusté wiki supply_area=3.0.
- **`beacons_per_stage` = nombre total de beacons côté +u pour l'étage** (pas "par machine"). FactoryBuilder calcule depuis la densité voulue. Côté -u (double couverture "8 beacons") = future S3d.
- **`energy_bonus` non utilisé** : audit seulement (dimensionnement électrique = autre module).
- **Productivité + co-produits S2b-1** : `ing_rate = eff*ing_amount/(rc_item*effective_productivity)` interagit avec `result_count_for(item)` multi-produits. `rc_item` inchangé = base non multipliée par prod. Validé en S3a test + back-compat S2b.

**Décisions S3d figées** (plan approuvé `spicy-gliding-dawn.md`, Option A « Miroir + stage_gap étendu ») :
1. **D1 — `beacons_neg_per_stage: int = 0`** dans `LayoutConstraints` (après `beacons_per_stage`). 0 = pas de beacon -u (back-compat S3c). FactoryBuilder met 4 pour "8 beacons" (4 +u + 4 -u). Le bonus agrégé (Option A S3a, `compute_module_effect`) est calculé par FactoryBuilder sur le total `beacons_per_stage + beacons_neg_per_stage` et injecté dans `ProductionRequest.module_effects` — le solveur est inchangé.
2. **D2 — Placement miroir** : `u_beacon_neg_pos = u_machine - offset_out_u - 1.0 - beacon_half_u` (= u_machine-5.5 pour machine 3×3, miroir de `u_beacon_pos` +u). Rangée le long de v, mêmes `row_first`/`row_last` que +u, `n_beacons_neg = max(1, int(constraints.beacons_neg_per_stage))`. Modules insérés (`entities[idx].modules = [module_tier]*modules_per_beacon`). `totals[beacon_tier] += n_beacons_neg`.
3. **D3 — Gate collision belts_in courant** : `beacon_neg_active` indépendant de `beacon_active`. Avant de poser, vérifie l'absence de chevauchement (bounding-box 2 axes u ET v) du candidat -u contre les entités existantes. Si collision → skip + note `beacon_neg_collision:<node.item>`. Multi-ingrédients : belt ing1 (long-handed reach 2.0) à u_machine-5.5 = position miroir → collision → skip (seuls les étages 1-ingrédient — furnaces, assembling 1-ing — ont la double couverture).
4. **D4 — Réservation `u_next` inter-étage** : `u_next = max(u_next, cur_max_edge + 6.5)` où `cur_max_edge = (u_beacon_pos+beacon_half_u)` si `beacon_active` sinon `(u_machine+offset_out_u+0.5)`. Le 6.5 = décalage bord -u du beacon -u suivant (prev_u_next-5.5) + gap 1.0 → garantit pas de collision inter-étage. Back-compat : `beacons_neg_per_stage=0` → pas d'extension → `u_next` S3c/S2 inchangé.

**Chaîne de test S3d** : `iron-plate` via electric-furnace (smelting) + 4 beacons +u + 4 beacons -u.
`iron-plate@10/s (electric-furnace, smelting, speed=2, module_slots=2) <- iron-ore (electric-mining-drill)`. Bonus solveur (Option A, formule 2.0 FFF#409) : 8 beacons speed-module-3 (4+u + 4-u) → `compute_module_effect(8,"speed-module-3")` = 1.5*sqrt(8)*0.5 = 2.121 speed_bonus → `effective_speed = 2*(1+2.121) = 6.243` → `per_machine = 1*1*6.243/3.2 = 1.951` → `count = ceil(10/1.951) = 6` (vs 7 S3c avec 4 beacons, vs 16 sans bonus). La chaîne iron-plate a un seul étage `_place_stage` (smelting) dont le -u fait face aux drills (côté -u libre, pas de transition belt) → 8 beacons posés (4+u + 4-u), pas de skip.

**Limitations S3d reportées** :
- **Multi-ingrédient → skip beacon -u** (D3) : les étages 2+ ingrédients (assembling 2 ings) n'ont pas de beacon -u (collision belt ing1). Seuls les étages 1 ingrédient (furnaces, assembling 1-ing) ont la double couverture. Documenté (note `beacon_neg_collision`). Le bonus solveur (Option A) reste cohérent : FactoryBuilder calcule `compute_module_effect` sur le total demandé ; si le layout skip des -u, le bonus réel est inférieur → FactoryBuilder doit n'activer `beacons_neg_per_stage` que pour les étages 1-ing (responsabilité FactoryBuilder, hors-scope S3d).
- **CONSTAT probe — 1er étage machine après mining** : la réservation D4 étend `u_next` mais les transition belts (placées par `plan()` entre `_place_stage` calls) remplissent le gap créé → le beacon -u des étages suivants collisionne avec les transition belts. Donc seul le 1er étage machine (face aux drills, -u libre) obtient les -u beacons ; les étages suivants (face à un étage machine) sont skippés. Limitation acceptée (le mécanisme de réservation ne touche que `_place_stage`, pas les transition belts de `plan()`).
- **`beacons_neg_per_stage` = nombre total de beacons -u pour l'étage** (pas "par machine"), sémantique identique à `beacons_per_stage` (S3c). FactoryBuilder calcule depuis la densité.

---

## 7. Décisions figées

1. **Déterministe pur** — pas de LLM. Arbitrage (gisement cible, tiers, replan
   lourd) = FactoryBuilder. Comme le solveur.
2. **Source données = RCON** (`describe` étendu) + cache + injection fixture (DIP).
3. **Terrain = entrée** (scan RCON). Pas d'ancrage absolu arbitraire : foreuses
   sur le gisement, usine pousse depuis `anchor` selon `facing`. Adaptation obstacles.
4. **Logistique CALCULÉE** (comme `machine_count` au solveur) :
   `belts_per_stage = ceil(rate/belt_speed)`,
   `inserters_in/out_per_machine = ceil(debit/inserter_throughput)`. Pas un paramètre.
5. **Tiers = entrée** (arbitrage FactoryBuilder, comme `machine_tiers` du solveur) :
   belt_tier, inserter_tier, pole_tier.
6. **Layout défaut = bande (manifold)**. Alternatifs = S1+.
7. **Directions Factorio** : `0=N, 2=E, 4=S, 6=W` (S0).
8. **Throughputs** : belt_speed hardcodé (15/30/45), inserter_throughput fixture
   S0 (mesuré S0b). Dépendance distance = S1+ (affine).

### Décisions S1 (T1-T7, figées 2026-07-24)

- **T1 — Belts de transition physiques** : si `belts_out(étage n) == belts_in(étage n+1)`,
  aligner `v_next` (belt_in_first en face de belt_out_last) + poser belts le long de +u sur
  `stage_gap` tuiles. Cas 1→1 (chaîne fer) : adjacence, préservé exact (back-compat). Déséquilibre
  → note `belt_mismatch_S1a`, résolu en S1b par splitter/merger tree.
- **T2 — Multi-ingrédients (plafonné à 2)** : empilement en u côté -u. Ingrédient 0 =
  `inserter_tier` (reach 1.0) ; ingrédient 1 = `long-handed-inserter` (reach 2.0, swing 4.0),
  belts décalées en -u (pas de collision). Dimensionnement **par ingrédient**
  (`StageLogistics.ingredients: dict[ing -> {belts_in, inserters_in_pm, inserter_name, swing, tp}]`).
  `max_ingredients_per_stage=2` (défaut) ; >2 → note `too_many_ingredients`.
- **T3 — Splitters/mergers (arbre binaire)**. Itération S1d (2026-07-24) a affiné les
  orientations (validé unitaire 101/101) :
  - **Merger = `splitter` 2x1 orienté FACING_DIR_V** (corrigé : couvre 2 lanes côte à côte
    en u, flux +v ; l'orientation U de S1b couvrait 2 tuiles en v le long d'UNE lane — bug).
    Position `(u_a+0.5, v_a+1)` (en +v du bout, tuile `v_a+1` → ne chevauche pas le
    belt_out à `v_a`). Arbre binaire pair/impair, M-1 mergers. Validé M=2 isolé
    (`test_merger_output`, direction 4).
  - **Splitter = `splitter` 2x1 orienté FACING_DIR_V** (corrected S1e, volet A validé). API
    `target_idx` ajoutée à `_build_split_tree` : `None` → branche bande S1b conservée (orient
    FACING_DIR_U, relatif au bus, back-compat) ; fourni → `_split_subtree` récursif (arbre
    binaire équilibré en `-v`, feuilles = belts_in consommateur à `(u_target..+N, v_target)`,
    nœuds à `v_target-1,-3,...`, splitter orient V entrée `-v`/2 sorties `+v`) + `_route_bus_to_target`
    (transition `+u` à `v=v_target-2D` de `u_bus+1` à `u_root_entry`, **sideload Factorio** bus
    `+v`→belt `+u`). Dump gear@22 : 2 splitters orient V à u=86.5/87.5 (alignés consommateur),
    88 belts transition (4 collisions croisements bus lanes → S1f underground). Validation live
    sideload perpendiculaire splitter reportée (nécessite serveur Factorio).
  - **Feed main bus (CONSTAT, revert S1e → reporté S1f)** : merger feed (`v_out_prod+1`, côté
    étage) collisionne les belts_in du consommateur (étages adjacents alignés en v). Tentative
    S1e `feed_side="bus"` (routing -u + merger V sur lanes virtuelles côté bus) **géométriquement
    cassée** : les belts_out sont dir `-u` (vers le bus) mais le merger orient V attend des lanes
    dir `+v` (entrée `-v`) → le merger ne reçoit pas le flux (directions incohérentes) ; + 187
    `feed_liaison_collision` (belts_out S1c **déjà** longés en `-u` vers le bus, re-routing
    collisionne à 100%). Revert → feed S1d (côté étage, CONSTAT). Feed correct = virage `-u`→`+v`
    + merger hors-zone belts_in + re-route vers lane, avec collisions structurelles (merger sur
    lane bus + croisements) → **underground belts (S1f)**. Collisions totales 194→10.
  1 splitter = 1→2 ; N belts_in → arbre binaire (N-1 splitters). Merger = symétrique
  (M→1, M-1 mergers). Rôles `LayoutEntity.role` : `"splitter"`, `"merger"`. Modèle
  "1 bus inter-étage le long de u" : en queue d'étage producteur, merger tree M→1 ; en tête de
  consommateur, splitter tree 1→N. En chaîne pure 1→1, pas d'arbre (back-compat).
- **T4 — Throughput distance-affine (fonction, k=0 en S1a)** : `inserter_throughput(name, swing)
  = base - k*(swing - swing_ref)` dans `knowledge.py`, `swing = pickup_distance + drop_distance`.
  `INSERTER_AFFINE` avec k=0 pour tous en S1a → back-compat stricte S0
  (`inserter_throughput("burner-inserter", 2.0) == 0.6 == THROUGHPUTS["burner-inserter"]`).
  k non-nul activé après mesure live (S1d : k=0 conservé, mesure dynamique = extension future).
  DIP via `LayoutRequest.inserter_throughput_fn`. `THROUGHPUTS` conservé pour les belts.
- **T5 — Main bus (layout alternatif, défaut off)** : bus perpendiculaire au facing (longe v),
  lanes empilées en u (1 par item intermédiaire = produit par un étage ET consommé par un autre).
  Étages alignés en u qui **tappent** (splitter prélève sur la lane) / **feedent** (merger
  réinjecte dans la lane). Activé par `LayoutConstraints.bus_layout=True` (défaut False).
  Branche additive `if constraints.bus_layout:` dans `plan()` — n'altère pas la bande.
- **T6/T7 — Champs ajoutés (tous avec défauts → back-compat stricte)** :
  `LayoutConstraints` + `swing_distance` (2.0), `max_ingredients_per_stage` (2), `bus_layout`
  (False), `bus_distance` (3) ; `StageLogistics` + `swing_used`, `inserter_tp_effective`,
  `ingredients` (dict), `splitters`, `mergers` ; `LayoutRequest` + `inserter_throughput_fn` ;
  `LayoutEntity.role` + `"splitter"`, `"merger"`, `"bus-belt"`.

---

## 8. Données géométriques — source RCON vs hardcode (constat API Factorio 2.0)

Investigation 2026-07-24 (doc `lua-api.factorio.com` + `verify_layout_data.py` en
jeu, 15/15 OK sur `size`) : en Factorio 2.0, `prototypes.entity[name]` renvoie un
`LuaEntityPrototype` qui **n'expose PAS au runtime** les géométries fines. Tentatives
épuisées (toutes en `pcall`, toutes échouent silencieusement → clé absente) :
- `proto.pickup_distance`, `proto.pickup_position`, `proto.insert_position` (inserter)
- `proto.max_wire_distance`, `proto.supply_area_distance` (propriétés pole)
- `proto:get_max_wire_distance()`, `proto:get_supply_area_distance()` (méthodes pole)
- `proto.mining_area` (drill)

→ **Seul `size` (via `collision_box`) est lisible au runtime.** Donc :
- `size` + `direction` = source RCON (source de vérité, validée en jeu).
- `belt_speed`, `pickup_reach`/`drop_reach`, `wire_reach`/`supply_area`, `mining_area`
  = **hardcode Python** (valeurs wiki stables, ne dépendent pas d'un mod).

Le marker sentinelle `_layoutMark = "tools_v2"` (ajouté dans `tools.lua` describe)
confirme à chaque appel que le mod est bien rechargé.

Validation en jeu : `python/verify_layout_data.py` (15/15 sur `size`).
Validation S0b (valeurs hardcodées) : mesure in-game — poser l'entité et lire sa zone
réelle (ex. `surface.find_entities_filtered` autour d'un pole pour vérifier
`supply_area` ; mesurer la zone minée d'un drill posé sur un gisement).

Voir [[rcon-donnees-exposees]] (describe marche sans setup/avatar).

---

## 9. Intégration avec l'existant

```
Coordinator (LLM)
   │  "produire iron-gear-wheel @ 5/s"
   ▼
FactoryBuilder (LLM, arbitre tiers + choisit gisement/zone)
   │  ProductionRequest + zone/gisement cible
   ▼
ProductionSolver (déterministe)  →  ProductionPlan (BOM + débits effectifs)
   │
   ▼  + Terrain (scan RCON) + tiers belts/inserters/poles
LayoutPlanner (déterministe)  →  LayoutPlan (blueprint dimensionné au débit)
   │  place_entity_at(name, x, y, dir) pour chaque LayoutEntity (ordre topo)
   ▼
mod fl_ops (exécution)
```

- ProductionSolver + LayoutPlanner = services déterministes couche 0.
- FactoryBuilder invoque solveur puis layout, arbitre tiers et gisement.
- Le `LayoutPlan` est consommé par l'exécution mod (1 tâche `place_entity_at`
  par `LayoutEntity`, dans l'ordre topologique des connexions).

Voir : [[factorybuilder-decoupage-productionsolver]],
[[s0-productionsolver-implante]], [[s0b-validation-live]],
[[rcon-donnees-exposees]], [[layoutplanner-spec]].