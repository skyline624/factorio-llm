# ProductionSolver — solveur de chaîne de production par débit

Sous-module **déterministe** de `FactoryBuilder`. Pas de LLM : c'est de la
computation pure (DFS sur le graphe de dépendances des recettes + calcul de
débit). C'est la brique que FactoryBuilder invoque **avant** de décider quoi
poser et où — elle répond à « combien d'unités de production me faut-il, et
quelles sous-productions en amont, pour tenir X produit/sec ».

Document de conception **vivant** : on l'enrichit palier par palier au fur et
à mesure qu'on établit les valeurs (débits, craft_time) et qu'on élargit le
graphe (fer → cuivre → circuits → oil...).

## 1. Pourquoi un sous-module séparé

La charge de FactoryBuilder est trop lourde pour un seul agent. On découpe :

```
Coordinator
  └─ FactoryBuilder (décision : où, quand, quel layout — arbitrage LLM)
        ├─ ProductionSolver  (CE MODULE : décomposition de débit, déterministe)
        ├─ services/exec/*   (pose mécanique : mining, smelting, assembly, logistics)
        └─ services/geometer  (géométrie de placement)
```

**ProductionSolver** est le calcul pur : à partir d'un objectif de débit,
produit la **nomenclature complète** (BOM de production étendue au débit) :
liste de toutes les unités de production nécessaires (assemblers/furnaces/
miners) + leur quantité + les sous-débits en amont, récursivement jusqu'aux
resources extraites.

**Règle de l'architecture** (cf. agents-roadmap.md §1) : si c'est un algorithme
→ service partagé, pas d'agent LLM. ProductionSolver est un algorithme → c'est
un service, pas un agent. FactoryBuilder l'invoque et arbitre ensuite sur le
résultat (choix de patch, layout, allocation de ressources).

## 2. Interface

### Entrée

```python
@dataclass
class ProductionRequest:
    item: str               # item cible (ex. "iron-gear-wheel")
    rate_per_sec: float     # débit cible (ex. 5.0 = 5 gears/sec)
    # Optionnel (paliers +) :
    machine_tiers: dict = {}  # override des machines utilisées
                              # ex. {"smelt": "steel-furnace", "craft": "assembling-machine-2"}
```

### Sortie — ProductionPlan (BOM étendue au débit)

```python
@dataclass
class ProductionNode:
    item: str                       # item produit par ce nœud
    role: str                       # "craft" | "smelt" | "mine"  (mode de production)
    rate_per_sec: float             # débit demandé à ce nœud (avant arrondi)
    rate_effective: float           # débit réellement produit (après ceil des machines)
    machine: str                    # machine utilisée (ex. "assembling-machine-1")
    machine_count: int              # nombre de machines (math.ceil — jamais en dessous)
    craft_time_sec: float           # temps de craft d'1 unité à crafting_speed=1
    crafting_speed: float           # vitesse de la machine (ex. 0.5 pour asm-1)
    # Ingrédients consommés à ce débit :
    ingredients: list[tuple[str, float]]  # [(ingredient_item, rate_per_sec), ...]

@dataclass
class ProductionPlan:
    goal: ProductionRequest
    nodes: list[ProductionNode]     # tous les nœuds du graphe (cible + intermédiaires + leaves)
    leaves: list[ProductionNode]    # nœuds d'extraction (mining) — les resources à aller chercher
    total_machines: dict[str, int]  # récap {machine_name: count_arrondi} sur tout le graphe
    feasibility: str               # "ok" | "missing_recipe:<item>" | "missing_rate:<machine>"
    notes: list[str]               # avertissements (ex. coal fuel non compté, débit miné approximatif)
```

### Exemple

`ProductionSolver.solve(ProductionRequest("iron-gear-wheel", 5.0))` doit
retourner un graphe disant :

- **iron-gear-wheel** @ 5/sec : 2 iron-plate/sec consommés → N assemblers.
- **iron-plate** @ 10/sec (amont) : 10 iron-ore/sec → M furnaces.
- **iron-ore** @ 10/sec (leaf) : K miners.
- Récap machines : {assembling-machine-1: N, stone-furnace: M, burner-mining-drill: K}.

## 3. Modèle de données (connaissances du jeu)

ProductionSolver consomme des **données pures** (cf. `services/knowledge.py`
déjà existant, à étendre). Trois tables :

### 3.1 Recettes — `RECIPES: dict[str, Recipe]`

```python
@dataclass
class Recipe:
    item: str                                # résultat
    ingredients: list[tuple[str, int]]       # [(ingredient, amount), ...]
    result_count: int = 1                    # unités produites par craft
    craft_time_sec: float                     # temps à crafting_speed=1
    category: str                             # "crafting" | "smelting" | "mining" | ...
```

Valeurs **à valider en jeu** (le projet valide par expérimentation — cf.
`knowledge._smelt_ticks` mesuré à 220 ticks/plaque vs ~96 théoriques). Premières
recettes du palier P2 (chaîne fer + cuivre) :

| Item | Ingrédients | result_count | craft_time_sec | Source |
|---|---|---|---|---|
| iron-plate | 1 iron-ore | 1 | **à mesurer** (théo 3.2s, réel mesuré ~220 ticks ≈ 3.67s) | four |
| copper-plate | 1 copper-ore | 1 | **à mesurer** (théo 3.2s) | four |
| stone-brick | 1 stone | 1 | **à mesurer** (théo 3.2s) | four |
| iron-gear-wheel | 2 iron-plate | 1 | **à mesurer** (théo 0.5s) | assembler |
| copper-wire | 1 copper-plate | 2 | **à mesurer** (théo 0.5s) | assembler (P3) |
| electronic-circuit | 1 iron-plate, 3 copper-wire | 1 | **à mesurer** (théo 0.5s) | assembler (P3) |

> TODO P2 : lancer un test de mesure dédié (1 machine, 1 recette, timer) pour
> remplir `craft_time_sec` réel de chaque recette couverte. On réutilise le
> pattern de `test_full.py` (poser, alimenter, attendre, mesurer).

### 3.2 Vitesse des machines — `MACHINES: dict[str, MachineSpec]`

```python
@dataclass
class MachineSpec:
    name: str                 # ex. "assembling-machine-1"
    crafting_speed: float     # ex. 0.5
    category: str             # "crafting" | "smelting" | "mining"
    power_fuel: str | None    # "burner" | "electric" (P2 : coal fuel non compté, voir §6)
```

Valeurs **théoriques Factorio 2.0** (à confirmer via le wiki /Context7) :

| Machine | crafting_speed | category | power |
|---|---|---|---|
| stone-furnace | 1.0 | smelting | burner (coal) |
| steel-furnace | 2.0 | smelting | burner (coal) |
| assembling-machine-1 | 0.5 | crafting | electric |
| assembling-machine-2 | 0.75 | crafting | electric |
| assembling-machine-3 | 1.25 | crafting | electric |
| burner-mining-drill | 0.25 (mining_speed) | mining | burner (coal) |
| electric-mining-drill | 0.5 (mining_speed) | mining | electric |

> TODO : confirmer les `crafting_speed` exactes Factorio 2.0 (le wiki a pu
> bouger). Le mining speed est aussi influencé par le `mining_speed_modifier`
> de la force et le type d'ore — on modélise au plus simple au P2 (vitesse
> brute de la drill) et on affine.

### 3.3 Classification des feuilles — `RAW_RESOURCES: set[str]`

Items qui ne sont **pas** produits par une recette mais extraits (les feuilles
du graphe) :

```
iron-ore, copper-ore, coal, stone,   # solides (P2)
water, crude-oil,                     # fluids (P5, out du périmètre P2)
```

Le graphe s'arrête à ces items → nœud `mine` (ou `pump`, `offshore-pump`).

## 4. Algorithme

DFS sur le graphe de dépendances, accumulation des débits par passage. On ne
re-décompose pas un item déjà visité : on **additionne** les débits (un même
ingrédient peut être demandé par plusieurs nœuds — ex. iron-plate consommé
par gears ET par circuits).

```
solve(request):
    rates: dict[str, float] = {request.item: request.rate_per_sec}
    nodes: dict[str, ProductionNode] = {}
    queue = [request.item]
    while queue:
        item = queue.pop()
        rate = rates[item]
        spec = ITEM_PROD[item]              # mode (craft/smelt/mine)
        if spec.mode == "mine":
            # feuille : extraction
            nodes[item] = ProductionNode(role="mine", machine=MINING_DRILL,
                                        machine_count = rate / drill_rate,
                                        ingredients=[])
            continue
        recipe = RECIPES[item]              # (sinon erreur feasibility)
        machine = pick_machine(spec.mode, machine_tiers)
        mspeed = MACHINES[machine].crafting_speed
        # débit d'une machine = result_count * mspeed / craft_time
        per_machine = recipe.result_count * mspeed / recipe.craft_time_sec
        machine_count = rate / per_machine
        # ingrédients consommés à ce débit :
        for ing, amt in recipe.ingredients:
            ing_rate = rate * amt / recipe.result_count
            rates[ing] = rates.get(ing, 0) + ing_rate
            if ing not in nodes: queue.push(ing)
        nodes[item] = ProductionNode(role=spec.mode, machine=machine,
                                     machine_count=machine_count,
                                     ingredients=[(ing, ing_rate) for ing,_ in recipe.ingredients])
    return ProductionPlan(...)
```

**Arrondi au supérieur + propagation du débit effectif** (décision figée) :
- `machine_count = ceil(rate / per_machine)` à **chaque** nœud (production
  **toujours** ≥ demande, jamais en dessous — par choix utilisateur : mieux
  vaut une production légèrement supérieure qu'un goulot).
- On propage en amont le **débit effectif** (celui réellement produit par le
  count arrondi), pas la demande initiale : `rate_effective = machine_count *
  per_machine`. Les ingrédients sont dimensionnés sur `rate_effective`. Ainsi
  **toute la chaîne** surproduit cohéremment, pas seulement le dernier étage
  (sinon un étage intermédiaire non arrondi devient goulot).
- `machine_count` est donc un **int** dans le `ProductionNode` (déjà arrondi) ;
  on garde aussi `rate_effective` dans le nœud pour audit + calcul des
  ingrédients. Le solveur ne laisse plus de fractionnaire à la pose.

**Autres propriétés** :
- Terminaison garantie : le graphe est acyclique (chaque recette consomme des
  items de « rang » inférieur ; les resources sont les feuilles). On garde un
  `visited` pour la sûreté + détection de cycle.
- Détection d'erreur : `feasibility = "missing_recipe:<item>"` si un item
  intermédiaire n'a pas de recette et n'est pas une resource.

## 5. Exemple chiffré (objectif : 5 iron-gear-wheel/sec)

Avec craft_time_sec(iron-plate)=3.2, craft_time_sec(gear)=0.5, asm-1 speed=0.5,
stone-furnace speed=1.0, electric-mining-drill=0.5 ore/sec (hypothétique) :

- **gear** @ 5/sec : per_machine = 1 × 0.5 / 0.5 = 1.0 gear/sec → **5 asm-1**.
  consomme iron-plate @ 5 × 2 = **10 plate/sec**.
- **iron-plate** @ 10/sec : per_machine = 1 × 1.0 / 3.2 = 0.3125 plate/sec →
  **32 stone-furnaces**. consomme iron-ore @ 10 × 1 = **10 ore/sec**.
- **iron-ore** @ 10/sec (leaf) : drill 0.5 ore/sec → **20 electric-mining-drills**.

Récap : `{assembling-machine-1: 5, stone-furnace: 32, electric-mining-drill: 20}`.

> NOTE : les ratios « connus » de la communauté (ex. 48 stone-furnaces / yellow
  belt) doivent **tomber** de ce calcul si les `craft_time_sec` sont justes.
  C'est notre **test de cohérence** : si le solveur donne ~48 furnaces pour
  saturer une yellow belt (40 items/sec... à valider), on sait que les
  constantes sont bonnes.

## 6. Périmètre et limitations (P2 initial)

**Couvert au P2** : chaîne fer + cuivre, solides, production par recette +
extraction. Aucun fluide, aucun oil.

**Hors périmètre P2 (noté, traité plus tard)** :
- **Carburant** : les machines `burner` (stone-furnace, burner-mining-drill)
  consomment du coal. Le `coal_per_sec` total n'est PAS calculé par le solveur
  au P2 — c'est une dépendance énergétique, pas une dépendance de recette.
  TODO P3 : ajouter un nœud `fuel` (coal) dimensionné par la consommation des
  machines burner (W coal/s = Σ power_fuel_machine × count × coal_rate).
- **Biters/défense** : non compté (agent Defender).
- **Fluides / oil** : P5 (délégué à Refiner).
- **Modules/beacons** (productivity, speed) : modifient le débit effectif.
  TODO P4 : `effective_speed = base × (1 + speed_bonus) × (1 + productivity)`.
- **Mining rate réel** : dépend du patch (richness) et du `mining_speed_modifier`.
  Au P2 on utilise la vitesse brute de la drill (approximation).

## 7. Plan d'implémentation

| Étape | Livrable | Test |
|---|---|---|
| **S0** | `services/production_solver.py` + tables `RECIPES`/`MACHINES`/`RAW_RESOURCES` (chaîne fer seule, valeurs theo). | Unitaire : `solve(gear,5/sec)` → 5 asm / 32 furnaces / 20 drills (cohérence ratios). |
| **S1** | Test de **mesure** en jeu (`tests/test_rates.py`) : 1 machine, 1 recette, timer → `craft_time_sec` réel pour chaque recette couverte. | Remplit les tables avec valeurs réelles. |
| **S2** | Élargissement cuivre + copper-wire + electronic-circuit (P3). Test cohérence : circuit @ 1/sec → ratios connus. | Unitaires. |
| **S3** | Nœud `fuel` (coal) — dimensionnement énergétique (P3). | Unitaires + mesure en jeu. |
| **S4** | Modules/beacons — bonus de vitesse/productivité (P4). | Unitaires. |

**S0 est réalisable tout de suite**, sans serveur, sans LLM — c'est du pur
calcul Python testable unitairement. S1 nécessite un serveur Factorio
(mesure).

## 8. Lien avec le code existant

- `services/knowledge.py` : détient déjà `ITEM_PROD` (mode par item),
  `ProductionGoal`, `plan_production` (plan par **quantité** pour P1).
  ProductionSolver est l'homologue **par débit** : on réutilise `ITEM_PROD`
  et on ajoute les tables `RECIPES`/`MACHINES`. À terme, `knowledge.py` et
  `production_solver.py` pourraient fusionner ou `knowledge` devenir le
  module de données et `production_solver` le module de calcul — à décider
  en S0.
- `agents/factory_builder.py` : invoquera `ProductionSolver.solve` dans sa
  phase **Analyser** (pipeline axe 3, cf. doc agent-logic à venir) pour
  obtenir la BOM avant de planifier la pose.
- Aucun LLM impliqué : ProductionSolver est entièrement testable sans
  `openai`, sans RCON, sans serveur.

## 9. Décisions (figées / ouvertes)

1. **Séparation données / calcul** — FIGÉ. `knowledge.py` = base de
   connaissance (données pures : `ITEM_PROD`, `RECIPES`, `MACHINES`,
   `RAW_RESOURCES`, types `Recipe`/`MachineSpec`). `production_solver.py` =
   calcul par débit (logique pure, importe `knowledge`). `plan_production`
   (P1 par quantité) reste dans `knowledge` pour l'instant.
2. **Stockage des recettes** — RETENU : stocker les recettes **complètes**
   (ingrédients + `craft_time_sec` + `result_count`) dans `knowledge.py` comme
   données de référence **mesurées en jeu** (S1). Raison : `get_recipe` (RCON)
   n'expose pas `craft_time` ; le solveur en a besoin ; et ça rend la base de
   connaissance auto-portée (testable sans RCON). `RecipeLookup` RCON reste
   pour P1 en rétrocompat (à converger éventuellement plus tard).
3. **machine_count** — FIGÉ : `math.ceil(rate / per_machine)` dans le solveur
   (production jamais en dessous). On propage le **débit effectif** arrondi en
   amont (cf. §4). Pas de fractionnaire à la pose.
4. **Mining rate** — OUVERT (proposition : valeur brute de la drill au P2).
5. **Coal fuel** — OUVERT (proposition : ignoré au P2, nœud fuel en S3).

## 10. Extension mod Lua — exposer `craft_time` + `craftingCategories` (S0b)

Validé en jeu headless (2026-07-24, `verify_rcon_data.py` →
`logs/verify_rcon_data.out`) : `fl_tools` expose déjà ingredients, products,
category, craftingSpeed, miningSpeed, energySource. **Manquaient** le
`craft_time` des recettes et les `crafting_categories` des machines. Extension
**implémentée** dans `mod/scripts/tools.lua` :

### `describe(name)` — recette
Ajout du champ `energy` dans le bloc `result.recipe` :
```lua
result.recipe = { name, ingredients, products, enabled, category, energy = recipe.energy }
```
`recipe.energy` (LuaRecipe) = craft_time en secondes à crafting_speed=1.
→ débit_machine = `result_count × crafting_speed / energy`.

### `describe(name)` — entité
Ajout de `craftingCategories` pour `furnace` et `assembling-machine` :
```lua
local cats = proto.crafting_categories   -- set -> liste
entity.craftingCategories = { ... }
```
Permet au solveur de matcher `recette.category ∈ machine.craftingCategories`
→ déterminer quelle machine crafter quelle catégorie (ex. electronic-circuit
category=electronics nécessite un assembler qui supporte electronics).

### `get_recipe(item)` — aligné sur describe
Enrichi avec `products` + `category` + `energy` (en plus de `ingredients[].count`
qui reste pour rétrocompat `perception.recipe_of`). Désormais `describe` et
`get_recipe` exposent la même info recette ; `describe` reste préférable
(enumerate aussi l'entité).

### Validation
Rejouer `python verify_rcon_data.py` après **redémarrage du serveur** (le mod se
charge au démarrage). Vérifier : `energy` présent sur iron-plate/iron-gear-wheel,
`craftingCategories` présent sur assembling-machine-1/stone-furnace.