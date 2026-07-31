"""Planificateur de production déterministe — cœur de FactoryBuilder P1.

Transforme un objectif de production (item + quantité) en un plan ordonné
d'étapes exécutables par le mod (fl_ops), en arbitrant selon l'inventaire :
si l'avatar a déjà assez de l'item -> rien à faire (branche "déjà assez") ;
sinon -> produire l'ingrédient manquant, récursivement.

Aucune décision LLM (P1 = déterministe seul ; LLM en P1b). La classification des
items par mode de production est en données pures (ITEM_PROD), extensible en P2+
(copper, circuits, oil...).

Modes de production (chaîne fer en P1) :
  - "mine"  : resource à extraire (iron-ore, coal, copper-ore).
  - "smelt" : four (furnace) : 1 ore -> N plate (ratio). Le four vient du kit.
  - "craft" : assembler : recette via get_recipe. P1 = iron-gear-wheel.

La simulation d'inventaire (sim_inv) ne tracke que la PRODUCTION (crédits) pour
décider de l'arbitrage, pas la consommation (gérée réellement par le mod via
check_can_craft / furnace). Suffisant pour la chaîne linéaire de P1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable, Optional


# ===== Objectif et étapes planifiées =====

@dataclass
class ProductionGoal:
    """Objectif de production : produire `count` unités de `item`."""
    item: str
    count: int


@dataclass
class Step:
    """Étape exécutable par le mod (consommée par BaseAgent.act).

    kind : "find_nearest" | "walk_to_entity" | "mine_entity" | "place_furnace"
           | "move_items" | "wait" | "craft_item"
    args : dict paramétrant l'étape (voir BaseAgent._execute pour le mapping
           vers les méthodes ModApi).
    """
    kind: str
    args: dict = field(default_factory=dict)


# ===== Classification des modes de production (données pures, extensible) =====

ITEM_PROD: dict[str, dict] = {
    # Resources à miner.
    "iron-ore":      {"mode": "mine"},
    "copper-ore":    {"mode": "mine"},
    "coal":          {"mode": "mine"},
    "stone":         {"mode": "mine"},
    # Fours (smelting) : 1 ore -> ratio plate.
    "iron-plate":    {"mode": "smelt", "from": "iron-ore",   "ratio": 1},
    "copper-plate":  {"mode": "smelt", "from": "copper-ore", "ratio": 1},
    "stone-brick":   {"mode": "smelt", "from": "stone",      "ratio": 1},
    # Assemblers (craft) : 1 craft -> per unités. Recette via get_recipe.
    "iron-gear-wheel": {"mode": "craft", "per": 1},
    # P2+ : electronic-circuit, copper-wire, etc.
}

# Ce qu'un four de pierre brûle. Cinq unités par fournée : de quoi tenir le temps de la
# fusion sans immobiliser du charbon dont l'agent aura besoin ailleurs.
COMBUSTIBLE_FOUR = "coal"


# Lookup de recette injecté (DIP : le planificateur ne sait pas comment on
# obtient la recette — c'est perception.recipe_of côté agent).
RecipeLookup = Callable[[str], Optional[list[tuple[str, int]]]]


# ===== Helpers de simulation d'inventaire =====

def _credit(inv: dict[str, int], item: str, count: int) -> None:
    inv[item] = inv.get(item, 0) + count


def _debit(inv: dict[str, int], item: str, count: int) -> None:
    """Retire d'un inventaire SIMULÉ ce qu'une étape va consommer.

    Sans ce débit, la simulation croit garder ce qu'elle a déjà dépensé : mesuré en jeu,
    l'agent planifiait trois plaques pour sa foreuse, les créditait, puis planifiait trois
    engrenages en pensant les avoir encore — alors qu'ils sont faits DE ces plaques. Le
    plan s'arrêtait sur « manque iron-plate: 3/6 » après avoir tout miné et tout fondu.
    """
    inv[item] = max(0, inv.get(item, 0) - count)


def _smelt_ticks(ore_count: int) -> int:
    """Ticks d'attente pour smelter ore_count ore dans un stone-furnace.

    Mesuré en jeu (cf. test_full.py) : ~200 ticks/plaque (3.3s), pas les ~96
    théoriques (latence de démarrage du four + 1 plaque à la fois). On prend
    ore_count * 220 ticks (marge ~10%) avec un plancher 600 (10s).
    """
    return max(600, math.ceil(ore_count * 220))


# ===== Planification récursive =====

def plan_production(goal: ProductionGoal, inventory: dict[str, int],
                    recipe_lookup: RecipeLookup) -> list[Step]:
    """Plan déterministe pour produire goal.count de goal.item.

    Arbitre selon l'inventaire : have >= need -> [] (rien à faire). Sinon,
    produit le manquant (récursif sur les ingrédients). Ne modifie pas
    `inventory` (travaille sur une copie simulée).
    """
    if goal.count <= 0:
        return []
    return _plan(goal, dict(inventory), recipe_lookup)


def _plan(goal: ProductionGoal, sim_inv: dict[str, int],
          recipe_lookup: RecipeLookup) -> list[Step]:
    need = goal.count
    have = sim_inv.get(goal.item, 0)

    # Arbitrage "déjà assez" : on a déjà ce qu'il faut, rien à produire.
    if have >= need:
        return []

    missing = need - have
    spec = ITEM_PROD.get(goal.item)
    if spec is None:
        # Tout ce qui a une RECETTE se fabrique — inutile de tenir une liste à la main.
        # `ITEM_PROD` reste la table des cas qui ne se déduisent pas : ce qui se MINE (une
        # ressource n'a pas de recette) et ce qui se FOND (un four n'est pas un craft à la
        # main). Le reste s'en déduit, et c'est ce qui ouvre le bootstrap : `stone-furnace`,
        # `burner-mining-drill` et `burner-inserter` n'y figuraient pas, si bien que
        # l'agent ne pouvait planifier aucune des trois machines de sa première chaîne.
        #
        # `per=1` est un défaut prudent : quelques recettes en rendent deux (transport-belt,
        # copper-cable), auquel cas on en fabrique une de trop. Mieux vaut ce léger excès
        # qu'une chaîne qui s'arrête faute d'une pièce.
        if recipe_lookup(goal.item) is not None:
            spec = {"mode": "craft", "per": 1}
        else:
            raise ValueError(
                f"ni ressource, ni recette accessible pour {goal.item!r} — soit c'est une "
                f"matière première que le planificateur ne connaît pas, soit la recette "
                f"est VERROUILLÉE et il faut d'abord la rechercher")

    mode = spec["mode"]
    steps: list[Step] = []

    if mode == "mine":
        # Resource : trouver -> marcher -> miner.
        steps.append(Step("find_nearest", {"name": goal.item}))
        steps.append(Step("walk_to_entity", {"name": goal.item}))
        steps.append(Step("mine_entity", {"name": goal.item, "count": missing}))
        _credit(sim_inv, goal.item, missing)

    elif mode == "smelt":
        ore = spec["from"]
        ratio = int(spec.get("ratio", 1))
        ore_need = missing * ratio
        # Produire le ore (récursif — typiquement mine).
        steps += _plan(ProductionGoal(ore, ore_need), sim_inv, recipe_lookup)
        # LE COMBUSTIBLE FAIT PARTIE DU PLAN. Un four de pierre brûle : sans charbon il
        # ne fond rien, et l'étape `move_items coal` supposait qu'on en avait déjà.
        # Mesuré les mains vides : deux fours posés, `fuel=0`, trois minerais en attente
        # dans l'un d'eux, et le craft suivant échouant sur « manque iron-plate: 0/6 ».
        # Un plan qui consomme quelque chose doit dire d'où il vient.
        steps += _plan(ProductionGoal(COMBUSTIBLE_FOUR, 5), sim_inv, recipe_lookup)
        # Smelter dans un four du kit posé près du character.
        steps.append(Step("place_furnace", {}))
        steps.append(Step("move_items", {"item": "coal", "to_entity": True, "count": 5}))
        steps.append(Step("move_items", {"item": ore, "to_entity": True, "count": ore_need}))
        steps.append(Step("wait", {"ticks": _smelt_ticks(ore_need)}))
        steps.append(Step("move_items", {"item": goal.item, "to_entity": False, "count": missing}))
        # ON REPREND LE FOUR. Un four de fusion posé à la main n'est pas une usine : laissé
        # sur place, il tombe à sec et devient une « machine en panne » que le diagnostic
        # signale, que la boucle veut réparer — priorité 3 — et qui monopolise l'agent au
        # lieu de le laisser bâtir. Mesuré : quatre tours d'`approvisionner` sur des fours
        # de fusion abandonnés, pendant que les trois machines de la chaîne attendaient
        # dans les poches. Le reprendre évite le déchet ET rend la pierre pour la fournée
        # suivante.
        steps.append(Step("mine_entity", {"name": "stone-furnace", "count": 1}))
        _credit(sim_inv, "stone-furnace", 1)
        # Le four a mangé le minerai et le charbon : la simulation doit le savoir, sinon
        # la fournée suivante croit disposer d'un stock déjà brûlé.
        _debit(sim_inv, ore, ore_need)
        _debit(sim_inv, COMBUSTIBLE_FOUR, 5)
        _credit(sim_inv, goal.item, missing)

    elif mode == "craft":
        recipe = recipe_lookup(goal.item)
        if recipe is None:
            raise ValueError(f"aucune recette craftable pour {goal.item!r}")
        per = int(spec.get("per", 1))
        crafts = math.ceil(missing / per)
        # Produire chaque ingrédient (récursif — arbitre selon l'inventaire), PUIS le
        # retirer de la simulation : le craft le consomme, et le suivant ne doit pas
        # croire qu'il l'a encore.
        for ing_name, ing_amount in recipe:
            besoin = ing_amount * crafts
            steps += _plan(ProductionGoal(ing_name, besoin), sim_inv, recipe_lookup)
            _debit(sim_inv, ing_name, besoin)
        steps.append(Step("craft_item", {"item": goal.item, "count": crafts}))
        _credit(sim_inv, goal.item, crafts * per)

    return steps


# ===== Utilitaire de diagnostic (réutilisé par les tests/agents) =====

def plan_summary(steps: list[Step]) -> str:
    """Résumé compact d'un plan pour les logs."""
    return ", ".join(f"{i + 1}:{s.kind}" for i, s in enumerate(steps)) or "(vide)"


def has_mining(steps: list[Step]) -> bool:
    """True si le plan contient une étape de minage (diagnostic d'arbitrage)."""
    kinds = {s.kind for s in steps}
    return bool(kinds & {"find_nearest", "walk_to_entity", "mine_entity"})


# =====================================================================
# ProductionSolver (P2+) — base de connaissance par DÉBIT
# cf. docs/production-solver.md. Données pures lues via RCON (describe).
# Rétro-compatible avec P1 (ITEM_PROD / plan_production intacts ci-dessus).
# =====================================================================

# Resources extraites = feuilles du graphe de production. S2a : ajoute crude-oil
# (resource-entity gisement, miné par pumpjack). L'eau (water) est un TILE pompé par
# offshore-pump, gérée à part (FLUID_RAW_RESOURCES ci-dessous).
RAW_RESOURCES: frozenset[str] = frozenset({
    "iron-ore", "copper-ore", "coal", "stone", "crude-oil",
})

# S2a : resources fluides extraites (feuilles du graphe fluide).
# - "crude-oil" : resource-entity (gisement), miné par pumpjack (mining-drill, basic-fluid).
# - "water" : tile, pompé par offshore-pump (pas un resource-entity).
FLUID_RAW_RESOURCES: frozenset[str] = frozenset({"crude-oil", "water"})

# S2a : items fluides (transportés par pipe, pas par belt). Distingue les étages
# fluides des étages solides dans le solveur + le LayoutPlanner.
FLUID_ITEMS: frozenset[str] = frozenset({
    "crude-oil", "water", "petroleum-gas", "heavy-oil", "light-oil", "sulfuric-acid",
    "steam", "lubricant",
})

# S2a : catégories de recettes fluides (machines dédiées oil-refinery/chemical-plant).
# S2b-2 : "boiling" = recette synthétique steam (boiler, injectée côté Python — pas une
# recette Lua). Le boiler chauffe water -> steam (target_temperature=165). Non lisible
# via RCON (pas de craftingCategories sur boiler) -> MachineSpec hardcodée (inject_power_units).
FLUID_CATEGORIES: frozenset[str] = frozenset({"oil-processing", "chemistry", "boiling"})

# Machine par DÉFAUT par catégorie (tier 1). Override via request.machine_tiers.
# Le choix du tier est un arbitrage de FactoryBuilder (le solveur prend tier 1
# par défaut et accepte un override). Mining = drill par défaut.
# S2a : ajoute oil-processing (oil-refinery) + chemistry (chemical-plant).
CATEGORY_DEFAULT_MACHINE: dict[str, str] = {
    "smelting":          "stone-furnace",
    "crafting":          "assembling-machine-1",
    "basic-crafting":    "assembling-machine-1",
    "advanced-crafting": "assembling-machine-1",
    "electronics":       "assembling-machine-1",
    "pressing":          "assembling-machine-1",
    "oil-processing":    "oil-refinery",
    "chemistry":         "chemical-plant",
    # S2b-1 : cracking (heavy/light-oil-cracking) = catégorie organic-or-chemistry (Space
    # Age) -> chemical-plant. oil-processing/chemistry déjà présents (S2a) inchangés.
    "organic-or-chemistry": "chemical-plant",
    # S2b-2 : boiling = recette synthétique steam -> boiler (crafting_speed=1.0 hardcodé,
    # cf. inject_power_units ; boiler n'expose pas craftingCategories au runtime).
    "boiling":             "boiler",
}

# S2b-1 : préférence hardcodée pour l'arbitrage entre recettes candidates d'un même
# produit (sélecteur déterministe KnowledgeBase.recipe_of). Le FactoryBuilder (non
# implémenté) remplacera ça par un choix contextuel. Back-compat stricte :
#   - petroleum-gas[0] = "basic-oil-processing" -> plastic-bar S2a préservé (recette
#     mono-produit, pas de co-produit orphelin).
#   - Si 1 seule recette candidate, recipe_of la retourne sans consulter la préférence.
# L'ordre = priorité : 1er nom de la liste dont recipe.item matche un candidat.
RECIPE_PREFERENCE: dict[str, list[str]] = {
    "petroleum-gas": ["basic-oil-processing", "advanced-oil-processing",
                      "light-oil-cracking", "coal-liquefaction"],
    "heavy-oil": ["advanced-oil-processing", "coal-liquefaction"],
    "light-oil": ["advanced-oil-processing", "light-oil-cracking", "coal-liquefaction"],
    "solid-fuel": ["solid-fuel-from-heavy-oil", "solid-fuel-from-light-oil",
                   "solid-fuel-from-petroleum-gas"],
    "lubricant": ["lubricant"],
}
DEFAULT_MINING_MACHINE = "electric-mining-drill"
# S2a : machines de minage fluide par défaut (item-aware via mining_machine(item)).
DEFAULT_FLUID_MINING_MACHINE = "pumpjack"
DEFAULT_WATER_MACHINE = "offshore-pump"


@dataclass
class Recipe:
    """Recette craftable (donnée de référence mesurée via RCON).

    craft_time_sec = recipe.energy (secondes à crafting_speed=1).
    débit_machine = result_count × crafting_speed / craft_time_sec.

    S2a : `ingredients` (list[tuple[str,int]]) inchangé (back-compat stricte — le
    solveur S0/S1 le lit tel quel). Les champs fluides sont ADDITIFS et optionnels :
      - ingredient_types / product_types : {"item"|"fluid"} par nom d'ingrédient/produit.
      - fluid_ingredients / fluid_products : sous-liste des (name, amount) fluides.
    Défauts vides -> recettes solides inchangées (back-compat).
    """
    item: str
    ingredients: list[tuple[str, int]]   # [(ingredient, amount), ...]
    result_count: int = 1                # unités produites par craft (back-compat S0/S1)
    craft_time_sec: float = 0.0          # = recipe.energy
    category: str = ""                   # "smelting" | "crafting" | "electronics" | ...
    # S2a : types + sous-listes fluides (additifs, défauts vides).
    ingredient_types: dict[str, str] = field(default_factory=dict)   # {name: "item"|"fluid"}
    product_types: dict[str, str] = field(default_factory=dict)
    fluid_ingredients: list[tuple[str, int]] = field(default_factory=list)
    fluid_products: list[tuple[str, int]] = field(default_factory=list)
    # S2b-1 : comptes par produit (recettes multi-produits type advanced-oil-processing
    # qui produit heavy 25 + light 45 + petroleum 55). Additif, défaut vide -> recettes
    # mono-produit retombent sur result_count (back-compat S0/S1/S2a).
    result_counts: dict[str, float] = field(default_factory=dict)

    def result_count_for(self, item: str) -> float:
        """Nb d'unités de `item` produites par craft (recettes multi-produits).

        Back-compat : si result_counts vide (recette mono-produit S0/S1/S2a), retourne
        float(result_count). Sinon retourne result_counts[item] (défaut result_count si
        `item` absent — recette où item n'est pas un produit listé, ex. edge case).
        """
        if not self.result_counts:
            return float(self.result_count)
        return self.result_counts.get(item, float(self.result_count))


@dataclass
class MachineSpec:
    """Machine placeable productrice (furnace / assembler / drill / pumpjack / offshore-pump).

    S2a : `mining_kind` ("solid"|"fluid"|"water") distingue le minage solide (drill),
    fluide (pumpjack sur gisement crude-oil) et eau (offshore-pump sur tile water).
    `fluid_boxes` = connexions pipe de la machine (pour le LayoutPlanner fluide).
    Défauts "solid"/[] -> machines solides inchangées (back-compat).
    """
    name: str
    crafting_speed: float = 0.0          # furnaces/assemblers (0 pour drills)
    categories: set[str] = field(default_factory=set)  # crafting_categories
    type: str = ""                      # "furnace"|"assembling-machine"|"mining-drill"|"offshore-pump"|"boiler"|"generator"
    energy_source: str = ""              # "burner" | "electric" | ...
    mining_speed: float = 0.0           # drills uniquement (0 sinon)
    # S2a : minage fluide + connexions pipe (additifs, défauts back-compat).
    mining_kind: str = "solid"          # "solid" | "fluid" | "water"
    fluid_boxes: list[dict] = field(default_factory=list)  # [{production_type, pipe_connections:[{x,y,direction?}]}]
    # S3a : slots modules (0 = pas de modules ; 2 pour electric-furnace/assembling-machine-3).
    # Statique prototype (CONSTAT S3b : probablement inaccessible runtime -> hardcode).
    # Sert à FactoryBuilder pour vérifier la capacité de modules (pas au solveur).
    module_slots: int = 0


class KnowledgeBase:
    """Cache typé des recettes + machines, peuplé via RCON (describe) ou fixture.

    Source de vérité = le jeu (RCON). Le cache permet au solveur de tourner sans
    latence, et les tests unitaires injectent un fixture (pas de RCON/serveur).
    """

    def __init__(self):
        self.recipes: dict[str, Recipe] = {}
        self.machines: dict[str, MachineSpec] = {}
        self.raw_resources: set[str] = set(RAW_RESOURCES)
        # S2b-1 : index recettes par produit (multi-recettes par produit, ex. petroleum-gas
        # produit par basic-oil / advanced-oil / light-oil-cracking / coal-liquefaction).
        # Additif ; self.recipes (clé produit principal) PRESERVÉ (fallback back-compat).
        self.recipes_by_product: dict[str, list[Recipe]] = {}

    def recipe_of(self, item: str, request=None) -> Optional[Recipe]:
        """Sélecteur de recette pour produire `item` (S2b-1 : arbitrage multi-recettes).

        - `request` : ignoré en S2b (réservé FactoryBuilder futur, choix contextuel).
        - Candidates = recipes_by_product[item] (index S2b-1). Si vide -> fallback
          self.recipes[item] (back-compat fixtures S0/S1/S2a qui n'indexent que recipes).
        - 1 candidate -> retourne-la. >1 -> applique RECIPE_PREFERENCE[item] (1er nom de
          la liste dont recipe.item matche), défaut candidates[0].
        """
        candidates = self.recipes_by_product.get(item)
        if not candidates:
            # Back-compat : fixtures S0/S1/S2a n'indexent que self.recipes.
            return self.recipes.get(item)
        if len(candidates) == 1:
            return candidates[0]
        pref = RECIPE_PREFERENCE.get(item, [])
        for name in pref:
            for r in candidates:
                if r.item == name:
                    return r
        return candidates[0]

    def machine(self, name: str) -> Optional[MachineSpec]:
        return self.machines.get(name)

    def pick_machine(self, category: str,
                     machine_tiers: Optional[dict] = None) -> Optional[MachineSpec]:
        """Machine pour une catégorie de recette (override optionnel par catégorie).

        Politique : si machine_tiers fournit <category> -> machine_name, l'utiliser.
        Sinon défaut CATEGORY_DEFAULT_MACHINE[category]. Sinon chercher la machine
        la plus lente (tier 1) qui supporte la catégorie.
        """
        if machine_tiers and category in machine_tiers:
            return self.machines.get(machine_tiers[category])
        name = CATEGORY_DEFAULT_MACHINE.get(category)
        if name and name in self.machines:
            return self.machines[name]
        # Fallback : la plus lente (tier 1) qui supporte la catégorie.
        cands = [m for m in self.machines.values()
                 if category in m.categories and m.type != "mining-drill"]
        if not cands:
            return None
        return min(cands, key=lambda m: m.crafting_speed)

    def mining_machine(self, item: Optional[str] = None,
                       machine_tiers: Optional[dict] = None) -> Optional[MachineSpec]:
        """Machine de minage pour `item` (item-aware depuis S2a).

        - item="water" -> offshore-pump (tile water pompée, débit 1200/s).
        - item in FLUID_RAW_RESOURCES (crude-oil) -> pumpjack (resource-entity gisement).
        - sinon (iron-ore/coal/.../None) -> electric-mining-drill (back-compat S0/S1).

        Back-compat : l'ancien appel positionnel `mining_machine(machine_tiers)` (dict
        en 1er arg) est détecté et redirigé (item=None -> drill). Override via
        machine_tiers["mine"] (solide) / ["fluid-mine"] / ["water-mine"].
        """
        # Shim back-compat : ancien appel `mining_machine(some_dict)` positionnel.
        if isinstance(item, dict) and machine_tiers is None:
            machine_tiers = item
            item = None
        if item == "water":
            if machine_tiers and "water-mine" in machine_tiers:
                return self.machines.get(machine_tiers["water-mine"])
            return self.machines.get(DEFAULT_WATER_MACHINE)
        if item in FLUID_RAW_RESOURCES:
            if machine_tiers and "fluid-mine" in machine_tiers:
                return self.machines.get(machine_tiers["fluid-mine"])
            return self.machines.get(DEFAULT_FLUID_MINING_MACHINE)
        # Solide (back-compat S0/S1).
        if machine_tiers and "mine" in machine_tiers:
            return self.machines.get(machine_tiers["mine"])
        return self.machines.get(DEFAULT_MINING_MACHINE)


def _amount_of(d: dict, key: str, default: float = 1.0) -> float:
    """Lit `amount` ou `count` (incohérence describe vs get_recipe)."""
    return float(d.get(key, d.get("count", d.get("amount", default))))


def recipe_from_describe(item: str, d: dict) -> Optional[Recipe]:
    """Construit une Recipe depuis la sortie de fl_tools.describe(item).

    `d` = {recipe?: {ingredients, products, enabled, category, energy}, entity?}.
    Retourne None si pas de recette (item non craftable / verrouillé).

    S2a : lit `type` ("item"|"fluid") par ingrédient/produit (champ additif depuis
    tools.lua describe) et peuple les sous-listes fluides. Back-compat : recettes
    solides -> types/sous-listes vides (comportement S0/S1 inchangé).
    """
    r = d.get("recipe")
    if not r or not r.get("ingredients"):
        return None
    ings = [(i["name"], int(_amount_of(i, "amount")))
            for i in r["ingredients"] if i.get("name")]
    # S2a : types par ingrédient/produit + sous-listes fluides.
    ingredient_types: dict[str, str] = {}
    fluid_ingredients: list[tuple[str, int]] = []
    for i in r["ingredients"]:
        if not i.get("name"):
            continue
        tp = i.get("type", "item")
        ingredient_types[i["name"]] = tp
        if tp == "fluid":
            fluid_ingredients.append((i["name"], int(_amount_of(i, "amount"))))
    prods = r.get("products") or []
    product_types: dict[str, str] = {}
    fluid_products: list[tuple[str, int]] = []
    for p in prods:
        if not p.get("name"):
            continue
        tp = p.get("type", "item")
        product_types[p["name"]] = tp
        if tp == "fluid":
            fluid_products.append((p["name"], int(_amount_of(p, "amount", 1))))
    # result_count : amount du product dont le nom == item (défaut products[0]).
    result_count = 1
    for p in prods:
        if p.get("name") == item:
            result_count = int(_amount_of(p, "amount", 1))
            break
    else:
        if prods:
            result_count = int(_amount_of(prods[0], "amount", 1))
    # S2b-1 : result_counts = {product_name: amount} pour tous les produits (recettes
    # multi-produits type advanced-oil : heavy 25 + light 45 + petroleum 55). Additif,
    # défaut vide si 1 seul produit (recettes mono -> result_count_for retombe sur
    # result_count, back-compat S0/S1/S2a).
    result_counts: dict[str, float] = {}
    if len(prods) > 1:
        for p in prods:
            if p.get("name"):
                result_counts[p["name"]] = float(_amount_of(p, "amount", 1))
    return Recipe(
        item=item,
        ingredients=ings,
        result_count=result_count,
        craft_time_sec=float(r.get("energy", 0.0) or 0.0),
        category=r.get("category", ""),
        ingredient_types=ingredient_types,
        product_types=product_types,
        fluid_ingredients=fluid_ingredients,
        fluid_products=fluid_products,
        result_counts=result_counts,
    )


def machine_from_describe(name: str, d: dict) -> Optional[MachineSpec]:
    """Construit une MachineSpec depuis fl_tools.describe(name).

    S2a : lit `fluid_boxes` (machines fluides) + dérive `mining_kind` depuis
    resourceCategories (basic-fluid -> "fluid") ou type (offshore-pump -> "water").
    Back-compat : mining_kind="solid" par défaut (drills/assemblers solides inchangés).
    """
    e = d.get("entity")
    if not e:
        return None
    mtype = e.get("type", "")
    # S2a : mining_kind dérivé (solide par défaut, back-compat).
    mining_kind = "solid"
    if mtype == "offshore-pump":
        mining_kind = "water"
    elif mtype == "mining-drill":
        rcats = e.get("resourceCategories") or []
        if "basic-fluid" in rcats:
            mining_kind = "fluid"
    return MachineSpec(
        name=name,
        crafting_speed=float(e.get("craftingSpeed", 0.0) or 0.0),
        categories=set(e.get("craftingCategories") or []),
        type=mtype,
        energy_source=e.get("energySource", ""),
        mining_speed=float(e.get("miningSpeed", 0.0) or 0.0),
        mining_kind=mining_kind,
        fluid_boxes=list(e.get("fluid_boxes") or []),
    )


def inject_power_units(kb: KnowledgeBase) -> None:
    """S2b-2 : injecte les unités power (boiler, steam-engine) + recette synthétique steam.

    Steam n'est PAS une recette Lua (pas de RCON) : elle est produite par le boiler qui
    chauffe water -> steam (target_temperature=165). La recette "boiling" est synthétique,
    côté Python. Les MachineSpec boiler/steam-engine sont hardcodées car les propriétés
    fines (max_energy_usage=1.8 MW, max_energy_production=900 kW) sont INACCESSIBLES au
    runtime Factorio 2.0 (cf. CONSTAT API 2.0, docs/layout-planner.md §6).

    Débits validés (probes live S2b-2) :
      - boiler 1.8 MW, steam heat_capacity=200 J/°C, ΔT=150 (15->165) -> 30 000 J/unité
        -> 60 steam/s par boiler. recette steam : 60 water -> 60 steam, craft_time=1.0s,
        crafting_speed=1.0 -> per_machine = 60 steam/s.
      - steam-engine 900 kW, effectivity=1 -> consomme 30 steam/s (900 000 / 30 000).

    Idempotent (skip si déjà présent). Back-compat stricte : additive — n'ajoute rien aux
    chaînes solides existantes (S0/S1/S2a demandent des items solides : iron-gear, plastic-
    bar, solid-fuel ; steam/boiler/steam-engine ne sont jamais sollicités par ces chaînes).
    """
    # Recette synthétique steam (boiling). category="boiling" -> pick_machine -> boiler.
    if "steam" not in kb.recipes:
        r_steam = Recipe(
            item="steam",
            ingredients=[("water", 60)],
            result_count=60,
            craft_time_sec=1.0,
            category="boiling",
            ingredient_types={"water": "fluid"},
            product_types={"steam": "fluid"},
            fluid_ingredients=[("water", 60)],
            fluid_products=[("steam", 60)],
            result_counts={"steam": 60.0},
        )
        kb.recipes["steam"] = r_steam
        kb.recipes_by_product.setdefault("steam", []).append(r_steam)
    # MachineSpec boiler (3×2, burner). crafting_speed=1.0 -> 60 steam/s.
    if "boiler" not in kb.machines:
        kb.machines["boiler"] = MachineSpec(
            name="boiler", crafting_speed=1.0, categories={"boiling"},
            type="boiler", energy_source="burner",
        )
    # MachineSpec steam-engine (3×5, generator). Sink power (consomme 30 steam/s).
    # crafting_speed=0 (pas un assembler) ; categories vide (pas de recette -> pick_machine
    # ne la retourne jamais ; le solveur l'atteint uniquement via le sink steam orphelin).
    if "steam-engine" not in kb.machines:
        kb.machines["steam-engine"] = MachineSpec(
            name="steam-engine", crafting_speed=0.0, categories=set(),
            type="generator", energy_source="electric",
        )


def populate_from_rcon(api, items: list[str], machines: list[str]) -> KnowledgeBase:
    """Peuple une KnowledgeBase via RCON (describe pour chaque item + machine).

    `api` = ModApi (duck-typed : méthode describe(name)->dict). Source de vérité
    = le jeu. Échec d'un describe (item verrouillé/absent) -> l'item/machine est
    simplement absent de la KB (le solveur le signalera comme missing_recipe).
    """
    kb = KnowledgeBase()
    for it in items:
        d = api.describe(it)
        if not isinstance(d, dict):
            continue
        # `recette_de` et non `recipe_from_describe` : sans cela le solveur rendait
        # `missing_recipe:petroleum-gas` sur un fluide dont la recette existe bel et bien,
        # sous le nom du PROCÉDÉ qui le fabrique.
        r = recette_de(api, it)
        if r is not None:
            # S2a : indexer par produit principal (products[0]) si différent du nom de
            # recette. Back-compat : recettes solides (iron-plate, iron-gear-wheel) ont
            # nom de recette == produit principal -> clé inchangée. Recettes fluides :
            # basic-oil-processing -> produit petroleum-gas -> clé "petroleum-gas" (le
            # solveur cherche recipe_of(item_produit), pas recipe_of(nom_recette)).
            prods = (d.get("recipe") or {}).get("products") or []
            key = prods[0]["name"] if prods else it
            kb.recipes[key] = r
            # S2b-1 : indexer sous TOUS les produits (recettes multi-produits type
            # advanced-oil : heavy + light + petroleum). recipes_by_product[product]
            # accumule toutes les recettes produisant ce produit (arbitrage recipe_of).
            # Back-compat : recipes (clé produit principal) inchangé (lectures directes).
            for p in prods:
                pname = p.get("name")
                if pname:
                    kb.recipes_by_product.setdefault(pname, []).append(r)
    for m in machines:
        d = api.describe(m)
        if not isinstance(d, dict):
            continue
        spec = machine_from_describe(m, d)
        if spec is not None:
            kb.machines[m] = spec
    # S2b-2 : injecter les unités power (boiler, steam-engine) + recette synthétique steam.
    # Additif, idempotent, back-compat (chaînes solides existantes non affectées).
    inject_power_units(kb)
    return kb


def recette_de(api, item: str):
    """La recette qui FABRIQUE `item`, même quand elle ne porte pas son nom.

    LE NOM D'UNE RECETTE N'EST PAS SON PRODUIT : le gaz sort de `basic-oil-processing`,
    l'huile lourde d'`advanced-oil-processing`. Interroger le jeu sur le produit ne rendait
    rien, et l'appelant en concluait qu'il fallait le MINER — « petroleum-gas » figurait
    parmi les gisements à prospecter et toute la moitié chimique de l'arbre passait pour
    hors d'atteinte.

    Une RESSOURCE BRUTE, elle, ne se fabrique pas : le jeu lui rend une `entity` (son
    gisement), et l'on s'arrête là. Sans ce garde on cherche « qui produit du minerai de
    fer », on tombe sur des recettes de recyclage, et la chaîne d'un engrenage passe de
    trois items à quarante et un.

    Entre plusieurs producteurs, on retient ceux que l'agent sait déjà faire, puis les plus
    simples — ce qui écarte de soi-même les recyclages et les déballages de barils, qui
    « produisent » l'item sans le fabriquer.
    """
    d = api.describe(item)
    if not isinstance(d, dict):
        return None
    directe = recipe_from_describe(item, d)
    if directe is not None:
        return directe
    if "entity" in d:                      # ressource brute : elle s'extrait
        return None
    for p in sorted(d.get("recipes_producing") or [],
                    key=lambda r: (not r.get("enabled"), r.get("n_ingredients", 99),
                                   r.get("name", ""))):
        via = api.describe(p.get("name", ""))
        if isinstance(via, dict):
            r2 = recipe_from_describe(p.get("name", ""), via)
            if r2 is not None:
                return r2
    return None


def decouvrir_chaine(api, item: str, garde: int = 400) -> tuple[list[str], list[str]]:
    """Tout ce qu'il faut savoir fabriquer pour obtenir `item`, jusqu'aux feuilles.

    Rend `(items, feuilles)` : la fermeture transitive des ingrédients, et parmi eux ceux
    qui n'ont PAS de recette — minerais, eau, pétrole brut, c'est-à-dire ce qu'il faudra
    extraire plutôt que fabriquer.

    CETTE PIÈCE MANQUAIT, et elle rendait le solveur inutilisable en pratique.
    `populate_from_rcon` réclame la LISTE des items ; le solveur, lui, sait dimensionner
    n'importe quelle chaîne. Entre les deux, personne ne calculait la liste : il fallait
    la connaître d'avance, donc l'écrire à la main, donc coder une recette par produit.
    C'est ce qui a fait dériver l'agent vers un script — chaque nouveau produit était un
    chantier au lieu d'être un paramètre.

    Mesuré en jeu sur un flacon de science : dix items, deux feuilles (le fer et le
    cuivre), et le solveur rend un plan complet sans qu'on lui ait rien soufflé.

    `api` est duck-typé (méthode `describe(name) -> dict`), comme `populate_from_rcon` :
    les tests injectent un faux catalogue et n'ont besoin d'aucun serveur.

    Un item sans recette est une FEUILLE, pas une erreur : c'est le cas normal d'un
    minerai. On ne distingue donc pas « pas de recette » de « recette verrouillée » —
    ouvrir une recette est le sujet de `services.recherche`, pas celui-ci.

    `garde` borne le parcours : une recette qui boucle (la liquéfaction du charbon
    consomme du charbon) tournerait sinon indéfiniment. Le passage par `vus` suffit à
    couper les cycles ; le garde protège des catalogues aberrants.
    """
    vus: set[str] = set()
    feuilles: list[str] = []
    a_voir = [item]
    while a_voir and len(vus) < garde:
        courant = a_voir.pop()
        if courant in vus:
            continue
        vus.add(courant)
        recette = recette_de(api, courant)
        if recette is None:
            feuilles.append(courant)
            continue
        for nom, _ in (recette.ingredients or []):
            if nom and nom not in vus:
                a_voir.append(nom)
    return sorted(vus), sorted(feuilles)


def entites_par_type(api, types: tuple[str, ...] = ("assembling-machine", "furnace",
                                                    "mining-drill")) -> list[str]:
    """Les entités que le JEU connaît pour ces types, plutôt qu'une liste écrite à la main.

    Sert aux machines (défaut) comme à la logistique (`transport-belt`, `inserter`,
    `electric-pole`…) : ce sont des TYPES du moteur, jamais des noms de produits.

    Le solveur choisit une machine par catégorie de recette (`kb.pick_machine`), mais il
    faut d'abord lui en présenter. Coder ce catalogue en constante le figerait au jour où
    on l'écrit : une machine ajoutée par un mod, ou simplement oubliée, deviendrait
    invisible — et le solveur rendrait `no_machine_for_category` sur une usine où la
    machine existe. Le jeu, lui, sait toujours ce qu'il contient.

    Rendu trié pour que deux appels donnent le même ordre : `pick_machine` départage à
    tiers égal, et une usine ne doit pas changer de forme entre deux tours pour cette
    seule raison.
    """
    filtre = " or ".join(f"p.type == '{t}'" for t in types)
    try:
        brut = api.rcon.query_lua(
            "local out = {} "
            f"for name, p in pairs(prototypes.entity) do if {filtre} then "
            "  out[#out+1] = name end end "
            "table.sort(out) rcon.print(table.concat(out, ','))")
    except Exception:
        return []
    return [n for n in str(brut).strip().split(",") if n]


def populate_pour(api, item: str, machines: list[str]) -> tuple[KnowledgeBase, list[str]]:
    """Une KnowledgeBase prête pour `item`, sans avoir à énumérer sa chaîne.

    Commodité qui enchaîne `decouvrir_chaine` puis `populate_from_rcon`. Rend aussi les
    feuilles : l'appelant en a besoin pour savoir QUELS gisements prospecter — c'est
    exactement la liste des ressources à trouver sur le terrain.
    """
    items, feuilles = decouvrir_chaine(api, item)
    return populate_from_rcon(api, items, machines), feuilles


# ===== Géométrie des entités (LayoutPlanner) =====
# Constat API Factorio 2.0 (2026-07-24, docs/layout-planner.md §3/§8) : au runtime,
# `prototypes.entity[name]` (LuaEntityPrototype) n'expose PAS les géométries fines
# (pickup_position, max_wire_distance, supply_area_distance, mining_area, belt_speed)
# — ni propriété, ni getter utilisable. SEUL `size` (depuis collision_box) est
# lisible via RCON (validé 15/15 par verify_layout_data.py).
#
# Stratégie : `size` = RCON (source de vérité). Les géométries fines + débits sont
# HARDCODÉES Python (valeurs wiki stables, ne dépendent pas d'un mod), à valider en
# S0b par mesure in-game. DIP : un fixture injectable permet les tests sans serveur.

# Débits en régime permanent (items/s) — fixture S0 (cf. spec §3).
# Belts : yellow/red/blue. Inserters : burner/normal (même moteur), long, fast.
# Stack = S1+ (dépend du bonus de stack). Dépendance à la distance = S1+ (affine).
THROUGHPUTS: dict[str, float] = {
    "transport-belt": 15.0, "fast-transport-belt": 30.0, "express-transport-belt": 45.0,
    "burner-inserter": 0.6, "inserter": 0.6, "long-handed-inserter": 0.83,
    "fast-inserter": 2.7,
    # S2a : fluides (débit en units/s). pipe = capacité nominale Factorio (1500/s pour
    # un pipe court), offshore-pump = 1200/s, pump = 10000/s. Valeurs wiki stables,
    # à valider en S2a-live (verify_layout_s2a.py).
    "pipe": 1500.0, "offshore-pump": 1200.0, "pump": 10000.0,
}

# Throughput distance-affine des inserters (S1a, spec §3/§7.8).
# Le débit d'un inserter en régime permanent dépend du "swing" = distance totale
# pickup+drop parcourue par demi-cycle. Modèle affine :
#   throughput(name, swing) = base - k * (swing - swing_ref)
#   - base = débit de référence mesuré (cf. THROUGHPUTS, à swing_ref).
#   - swing_ref = swing auquel `base` a été mesuré (burner/normal/fast = 2.0 car
#     pickup+drop = 1.0+1.0 ; long-handed = 4.0 car 2.0+2.0).
#   - k = pente (items/s perdus par tuile de swing au-delà de swing_ref).
# En S1a : k = 0 pour tous (INSERTER_AFFINE ci-dessous) -> inserter_throughput
# retourne EXACTEMENT THROUGHPUTS[name], quelle que soit la distance (back-compat
# stricte avec S0). k non-nul activé après mesure live en S1d (verify_layout_s1.py),
# même démarche que S0b. Les tests unitaires testent la FONCTION (pas une valeur
# absolue) -> insensibles à l'ajustement S1d.
SWING_REF: dict[str, float] = {
    "burner-inserter": 2.0, "inserter": 2.0, "fast-inserter": 2.0,
    "long-handed-inserter": 4.0,
}

# k = pente affine (items/s perdus par tuile de swing au-delà de swing_ref).
# k = 0 en S1a (inactif) ; mesuré en S1d. Ex : burner k=0.05 -> à swing=4,
# throughput = 0.6 - 0.05*(4-2) = 0.5.
INSERTER_AFFINE: dict[str, float] = {
    "burner-inserter": 0.0, "inserter": 0.0, "fast-inserter": 0.0,
    "long-handed-inserter": 0.0,
}


def inserter_throughput(name: str, swing: float) -> float:
    """Débit d'un inserter (items/s) selon le swing (pickup_distance + drop_distance).

    Modèle affine : base - k*(swing - swing_ref). Back-compat : avec k=0 (S1a par
    défaut, INSERTER_AFFINE), retourne THROUGHPUTS[name] (débit S0 constant,
    indépendant de la distance). Retourne 0.0 si l'inserter est inconnu.
    """
    base = THROUGHPUTS.get(name, 0.0)
    if base <= 0.0:
        return 0.0
    ref = SWING_REF.get(name, swing)   # défaut : pas de correction si inconnu
    k = INSERTER_AFFINE.get(name, 0.0)
    return max(0.0, base - k * (swing - ref))

# S2a/S2b-3 : throughput distance-affine des pipes (fluide). Modèle affine :
#   pipe_throughput(name, length, fluid=None) = base - (k_name + k_fluid) * (length - length_ref)
#   - base = débit de référence (THROUGHPUTS[name], à length_ref).
#   - length_ref = longueur de référence (pipe = 1 tuile).
#   - k_name = pente propre à l'élément (PIPE_AFFINE, 0 par défaut).
#   - k_fluid = viscosité du fluide transporté (FLUID_VISCOSITY, 0 par défaut).
# En S2a : k = 0 -> pipe_throughput retourne EXACTEMENT THROUGHPUTS[name] quelle que
# soit la longueur (débit constant, back-compat). Le débit réel d'un pipe dépend de
# la longueur + viscosité du fluide ; l'ajustement affine (k non-nul) est calibré en
# S2b-3 (modèle simplifié, approximation : les viscosités sont hardcodées wiki, NON
# lisibles au runtime Factorio 2.0). Les tests unitaires testent la FONCTION (pas une
# valeur absolue) -> insensibles à l'ajustement S2b.
PIPE_LENGTH_REF: dict[str, float] = {
    "pipe": 1.0, "offshore-pump": 1.0, "pump": 1.0,
}

PIPE_AFFINE: dict[str, float] = {
    "pipe": 0.0, "offshore-pump": 0.0, "pump": 0.0,
}

# S2b-3 : viscosité des fluides (units/s perdus par tuile au-delà de length_ref).
# Hardcodée wiki (NON lisible au runtime Factorio 2.0 : prototypes.fluid n'expose pas
# de champ de viscosité/débit). Modèle affine simplifié (approximation). 0 = fluide
# non visqueux (water, steam) -> débit constant (back-compat S2a). Ordre croissant :
# petroleum-gas (très fluide) < light-oil < crude-oil = heavy-oil (visqueux).
FLUID_VISCOSITY: dict[str, float] = {
    "water": 0.0, "steam": 0.0,
    "petroleum-gas": 0.1, "light-oil": 0.5,
    "crude-oil": 1.0, "heavy-oil": 1.0,
    "lubricant": 1.0, "sulfuric-acid": 1.0,
}


def pipe_throughput(name: str, length: float, fluid: str | None = None) -> float:
    """Débit d'un pipe/élément fluide (units/s) selon la longueur du segment et le
    fluide transporté.

    Modèle affine : base - (k_name + k_fluid) * (length - length_ref).
    - k_name = PIPE_AFFINE[name] (0 par défaut, back-compat S2a).
    - k_fluid = FLUID_VISCOSITY[fluid] si `fluid` est donné, sinon 0 (back-compat :
      appels 2-args S2a inchangés -> débit constant THROUGHPUTS[name]).
    Retourne 0.0 si l'élément est inconnu. Back-compat stricte : signature étendue avec
    `fluid=None` par défaut, les appels existants `pipe_throughput(name, length)` sont
    préservés (k_fluid=0 -> throughput constant, identique à S2a).
    """
    base = THROUGHPUTS.get(name, 0.0)
    if base <= 0.0:
        return 0.0
    ref = PIPE_LENGTH_REF.get(name, length)   # défaut : pas de correction si inconnu
    k_name = PIPE_AFFINE.get(name, 0.0)
    k_fluid = FLUID_VISCOSITY.get(fluid, 0.0) if fluid else 0.0
    k = k_name + k_fluid
    return max(0.0, base - k * (length - ref))

# Géométries fines hardcodées (valeurs wiki stables) — complément de `size` (RCON).
# mining_area = (x1, y1, x2, y2) RELATIF au centre de la drill (zone d'extraction).
#   electric-mining-drill : 5x5 (centre ±2) ; burner-mining-drill : 2x2 (centre ±0.5
#   arrondi -> la burner extrait sous elle sur 2x2, offset 0.5).
# wire_reach / supply_area : portée fil / zone d'alimentation électrique (tuiles).
# pickup_distance / drop_distance : portée prise/dépose des bras (tuiles depuis le centre).
# Machines (furnaces/assemblers) : emprise wiki (size aussi lisible via RCON ; le
# fixture fournit une valeur de repli pour les tests sans serveur).
GEOMETRY_FIXTURE: dict[str, dict] = {
    # Machines productrices (emprise ; size confirmée via RCON en S0b).
    "stone-furnace":          {"w": 2, "h": 2},
    "steel-furnace":          {"w": 2, "h": 2},
    "assembling-machine-1":    {"w": 3, "h": 3},
    "assembling-machine-2":    {"w": 3, "h": 3},
    "assembling-machine-3":    {"w": 3, "h": 3},
    # Belts : taille 1x1 (size lu via RCON), débit dans THROUGHPUTS.
    "transport-belt":         {"w": 1, "h": 1},
    "fast-transport-belt":     {"w": 1, "h": 1},
    "express-transport-belt":  {"w": 1, "h": 1},
    "splitter":                {"w": 2, "h": 1},
    "underground-belt":        {"w": 1, "h": 1},
    # Inserters : 1x1, portée prise/dépose (tuiles).
    "burner-inserter":         {"w": 1, "h": 1, "pickup_distance": 1.0, "drop_distance": 1.0},
    "inserter":                {"w": 1, "h": 1, "pickup_distance": 1.0, "drop_distance": 1.0},
    "long-handed-inserter":    {"w": 1, "h": 1, "pickup_distance": 2.0, "drop_distance": 2.0},
    "fast-inserter":           {"w": 1, "h": 1, "pickup_distance": 1.0, "drop_distance": 1.0},
    # Potes électriques : portée fil + zone d'alimentation.
    "small-electric-pole":     {"w": 1, "h": 1, "wire_reach": 7.5,  "supply_area": 2.5},
    "medium-electric-pole":    {"w": 1, "h": 1, "wire_reach": 9.0,  "supply_area": 3.0},
    "big-electric-pole":       {"w": 2, "h": 2, "wire_reach": 30.0, "supply_area": 2.0},
    "substation":              {"w": 2, "h": 2, "wire_reach": 64.0, "supply_area": 9.0},
    # Foreuses : emprise + zone d'extraction (relative au centre).
    "burner-mining-drill":     {"w": 2, "h": 2, "mining_area": (-0.5, -0.5, 0.5, 0.5)},
    "electric-mining-drill":   {"w": 3, "h": 3, "mining_area": (-2.0, -2.0, 2.0, 2.0)},
    # S2a : machines fluides. pipe_ports = [(du, dv, "input"|"output")] dans le repère
    # (u,v) du LayoutPlanner (facing=2 : u=+x, v=+y), assumant la machine orientée
    # "input côté -u, output côté +u". du/dv = position (relative au centre) où poser
    # le pipe 1×1 de connexion (tuile adjacente hors emprise). Positions hardcodées
    # (valeurs wiki stables) — à valider via measure_entity en S2a-live (rec 8).
    "pipe":                    {"w": 1, "h": 1},   # junction auto aux 4 côtés (pas de pipe_ports)
    "pipe-to-ground":          {"w": 1, "h": 1},   # S2c : paire input/output pour crossings (1x1)
    "offshore-pump":           {"w": 2, "h": 1, "pipe_ports": [(2, 0, "output")]},
    "pumpjack":                {"w": 3, "h": 3, "mining_area": (-1.5, -1.5, 1.5, 1.5),
                                "pipe_ports": [(2, 0, "output")]},
    "oil-refinery":            {"w": 5, "h": 5,
                                # S2b-1/S2b-3 fix K7 : 5 ports (2 input + 3 output) pour
                                # advanced-oil (water+crude -> heavy+light+petroleum). Les 3
                                # outputs sur le côté +u à v=-2/0/+2 (2 tuiles d'écart ->
                                # non adjacents entre eux), positions mesurées via
                                # measure_entity S2b-1-live (fluidbox.get_prototype : box 3
                                # heavy=coins, box 4 light=milieux, box 5 petroleum=coins ;
                                # facing east direction 2 -> port actif index 1 : heavy
                                # (2,-2), light (2,0), petroleum (2,2) ; pipe posé à u=+3,
                                # 1 tuile adjacente au port u=+2). output_port_dv mappe
                                # produit -> offset v du port output : assignation correcte
                                # produit->port indépendante de l'ordre de fluid_products
                                # retourné par RCON (fix K7). 1er input = position S2a
                                # (back-compat basic-oil : 1 in crude, 1 out petroleum).
                                "pipe_ports": [(-3, 0, "input"), (-3, -1, "input"),
                                               (3, -2, "output"), (3, 0, "output"), (3, 2, "output")],
                                "output_port_dv": {"heavy-oil": -2, "light-oil": 0, "petroleum-gas": 2}},
    "chemical-plant":          {"w": 3, "h": 3,
                                # S2b-1 : 3 ports (2 input + 1 output) pour cracking
                                # (water+heavy -> light) et lubricant (heavy -> lubricant).
                                # 1er input = position S2a (back-compat plastic-bar :
                                # 1 in petroleum-gas, out plastic-bar = item pas pipe).
                                "pipe_ports": [(-2, 0, "input"), (-2, -1, "input"), (2, 0, "output")]},
    "storage-tank":            {"w": 3, "h": 3,
                                "pipe_ports": [(-2, 0, "input"), (2, 0, "output")]},
    "pump":                    {"w": 1, "h": 1,
                                "pipe_ports": [(-1, 0, "input"), (1, 0, "output")]},
    "boiler":                  {"w": 3, "h": 2,
                                "pipe_ports": [(-2, 0, "input"), (2, 0, "output")]},
    # S2b-2 : steam-engine (3×5, generator). Sink power : 1 port input steam (côté -u).
    # Pas de output (le steam est consommé -> électricité). Hardcodé (positions wiki
    # symétriques), à valider via measure_entity S2b-2-live.
    "steam-engine":            {"w": 3, "h": 5,
                                "pipe_ports": [(-2, 0, "input")]},
    # S3b : beacon (3×3). CONSTAT live (verify_layout_s3b.py) : supply_area_distance
    # INACCESSIBLE pour beacon (contrairement aux poles — ni proto ni instance ne l'exposent)
    # -> fallback fixture supply_area=3.0. module_slots INACCESSIBLE (proto) -> fixture=2.
    # distribution_effectivity ACCESSIBLE live=1.5 (proto.distribution_effectivity, override
    # RCON dans populate_from_rcon) — fixture 1.5 = valeur vanilla 2.0 (FFF #409 : ×3 vs 1.1).
    # Ces 3 champs vivent ICI (et non dans BEACON_FIXTURE) car populate_from_rcon lit `fix`
    # (GEOMETRY_FIXTURE) — sinon module_slots=0 (bug S3b-8). BEACON_FIXTURE reste l'alias
    # canonique pour compute_module_effect (production_solver).
    "beacon":                  {"w": 3, "h": 3, "supply_area": 3.0,
                                "module_slots": 2, "distribution_effectivity": 1.5},
    # S3b : electric-furnace (3×3, smelting, electric, 2 module_slots). crafting_speed=2 lisible
    # via proto.get_crafting_speed (describe). module_slots hardcodé (CONSTAT).
    "electric-furnace":        {"w": 3, "h": 3},
}


# S3b : BEACON_FIXTURE — caractéristiques des beacons (hardcode, CONSTAT API 2.0 :
# module_slots/distribution_effectivity probablement inaccessibles au runtime sur le
# prototype, cf. fluid_boxes S2a / max_energy_usage S2b-2). supply_area lisible via
# ent.prototype.supply_area_distance (override RCON dans populate_from_rcon). Valeurs
# wiki Factorio 2.0 stables. Source de vérité authoritative (validée via measure_entity S3b).
BEACON_FIXTURE: dict[str, dict] = {
    "beacon": {
        # S3b-live : distribution_effectivity ACCESSIBLE runtime=1.5 (proto.distribution_effectivity)
        # — valeur vanilla 2.0 (FFF #409 : ×3 vs 1.1 où 0.5). Qualité : 1.5(normal)..2.5(legendary).
        "distribution_effectivity": 1.5,
        "module_slots": 2,                 # slots modules du beacon (CONSTAT inaccessible runtime)
        "supply_area": 3.0,                # portée effet (CONSTAT inaccessible runtime -> fixture)
    },
}

# S3b : MODULE_FIXTURE — effets des modules (hardcode, CONSTAT API 2.0 : module_effects
# sur LuaItemPrototype inaccessible au runtime). Valeurs wiki Factorio 2.0 (base game).
# speed/productivity/energy = bonus fractionnaires (ex. speed-module-3 = +50% speed,
# +70% conso énergie). tier = niveau. Utilisé par compute_module_effect (production_solver).
MODULE_FIXTURE: dict[str, dict] = {
    "speed-module":          {"speed": 0.20, "productivity": 0.0,  "energy": 0.50, "tier": 1},
    "speed-module-2":        {"speed": 0.30, "productivity": 0.0,  "energy": 0.40, "tier": 2},
    "speed-module-3":        {"speed": 0.50, "productivity": 0.0,  "energy": 0.70, "tier": 3},
    "productivity-module":   {"speed": -0.05, "productivity": 0.04, "energy": 0.40, "tier": 1},
    "productivity-module-2": {"speed": -0.10, "productivity": 0.06, "energy": 0.60, "tier": 2},
    "productivity-module-3": {"speed": -0.15, "productivity": 0.10, "energy": 0.80, "tier": 3},
    "effectivity-module":    {"speed": 0.0,  "productivity": 0.0,  "energy": -0.30, "tier": 1},
    "effectivity-module-2":  {"speed": 0.0,  "productivity": 0.0,  "energy": -0.40, "tier": 2},
    "effectivity-module-3":  {"speed": 0.0,  "productivity": 0.0,  "energy": -0.50, "tier": 3},
}


@dataclass
class EntityGeometry:
    """Géométrie d'une entité placeable (machine, belt, inserter, pole, drill).

    `w`/`h` = emprise (tuiles), source RCON (collision_box). Les champs fins
    (portées, zones, mining_area) sont hardcodés (GEOMETRY_FIXTURE) car non
    lisibles au runtime Factorio 2.0 (cf. constat API ci-dessus).
    """
    name: str
    w: int = 1
    h: int = 1
    pickup_distance: float = 0.0     # inserters (tuiles, prise)
    drop_distance: float = 0.0      # inserters (tuiles, dépose)
    wire_reach: float = 0.0          # poles (tuiles, portée fil)
    supply_area: float = 0.0        # poles (tuiles, zone d'alimentation)
    mining_area: Optional[tuple[float, float, float, float]] = None  # drills (relatif centre)
    # S2a : machines fluides. pipe_ports = [(du, dv, "input"|"output")] dans le repère
    # (u,v) du LayoutPlanner (positions où poser les pipes de connexion, relatives au
    # centre). fluid_boxes = structure Lua (pipe_connections) lue via RCON (validation
    # live). Défauts vides -> entités solides inchangées (back-compat).
    pipe_ports: list[tuple] = field(default_factory=list)
    fluid_boxes: list[dict] = field(default_factory=list)
    # S2b-3 fix K7 : mappe produit -> offset v du port output (oil-refinery multi-produit),
    # pour assigner le bon port au produit principal (node.item) et aux co-produits
    # indépendamment de l'ordre de fluid_products retourné par RCON. Défaut vide -> fallback
    # ordre des pipe_ports output (back-compat).
    output_port_dv: dict = field(default_factory=dict)
    # S3b : beacons. supply_area = portée effet (tuiles, déjà champ poles réutilisé).
    # module_slots = capacité modules du beacon (hardcode, CONSTAT inaccessible runtime).
    # distribution_effectivity = rendement de transmission du beacon (hardcode).
    # Défauts 0 -> entités non-beacon inchangées (back-compat).
    module_slots: int = 0
    distribution_effectivity: float = 0.0


class GeometryBase:
    """Cache typé des géométries d'entités, peuplé via RCON (size) + fixture (fines).

    DIP : `populate_from_rcon` lit `size` via describe (source de vérité) et
    fusionne avec GEOMETRY_FIXTURE pour les champs fins. Les tests unitaires
    injectent directement `geometry(name) = EntityGeometry(...)` (pas de serveur).
    """

    def __init__(self) -> None:
        self._geo: dict[str, EntityGeometry] = {}

    def geometry(self, name: str) -> Optional[EntityGeometry]:
        """Renvoie la géométrie d'une entité, ou None si inconnue."""
        return self._geo.get(name)

    def set_geometry(self, g: EntityGeometry) -> None:
        """Injecte une géométrie (fixture tests / override)."""
        self._geo[g.name] = g

    def populate_from_rcon(self, api, names: list[str]) -> None:
        """Peuple les emprises (`size`) via RCON describe + fusionne le fixture.

        Pour chaque entité : lit `size {w,h}` depuis describe (source de vérité),
        complète avec les champs fins de GEOMETRY_FIXTURE (portées/zones/mining_area,
        non lisibles au runtime 2.0). Si describe échoue, retombe sur GEOMETRY_FIXTURE.
        """
        for name in names:
            fix = GEOMETRY_FIXTURE.get(name, {})
            w = fix.get("w", 1)
            h = fix.get("h", 1)
            fluid_boxes: list[dict] = []
            d = api.describe(name)
            if isinstance(d, dict) and isinstance(d.get("entity"), dict):
                e = d["entity"]
                size = e.get("size")
                if isinstance(size, dict):
                    w = int(size.get("w", w) or w)
                    h = int(size.get("h", h) or h)
                # S2a : fluid_boxes lues via RCON (source de vérité) pour validation live.
                fb = e.get("fluid_boxes")
                if isinstance(fb, list):
                    fluid_boxes = fb
            # S3b : beacon — supply_area_distance lisible via RCON (ent.prototype) override
            # le fixture ; module_slots/distribution_effectivity CONSTAT (probablement nil)
            # -> fallback fixture (BEACON_FIXTURE authoritative).
            supply_area = float(fix.get("supply_area", 0.0))
            module_slots = int(fix.get("module_slots", 0))
            distribution_effectivity = float(fix.get("distribution_effectivity", 0.0))
            if isinstance(d, dict) and isinstance(d.get("entity"), dict):
                bc = (d["entity"].get("beacon") or {})
                if isinstance(bc.get("supply_area_distance"), (int, float)):
                    supply_area = float(bc["supply_area_distance"])
                if isinstance(bc.get("module_slots"), int):
                    module_slots = int(bc["module_slots"])
                if isinstance(bc.get("distribution_effectivity"), (int, float)):
                    distribution_effectivity = float(bc["distribution_effectivity"])
            self._geo[name] = EntityGeometry(
                name=name, w=w, h=h,
                pickup_distance=float(fix.get("pickup_distance", 0.0)),
                drop_distance=float(fix.get("drop_distance", 0.0)),
                wire_reach=float(fix.get("wire_reach", 0.0)),
                supply_area=supply_area,
                mining_area=fix.get("mining_area"),
                pipe_ports=list(fix.get("pipe_ports") or []),
                fluid_boxes=fluid_boxes,
                output_port_dv=dict(fix.get("output_port_dv") or {}),
                module_slots=module_slots,
                distribution_effectivity=distribution_effectivity,
            )


def populate_geometry_fixture(names: list[str]) -> GeometryBase:
    """Construit un GeometryBase depuis le seul GEOMETRY_FIXTURE (sans RCON/serveur).

    Pour les tests unitaires : toutes les géométries viennent du hardcode (valeurs
    wiki stables), `size` inclus. Équivalent d'un populate_from_rcon sur un jeu où
    les emprises coïncident avec le fixture (cas nominal).
    """
    gb = GeometryBase()
    for name in names:
        fix = GEOMETRY_FIXTURE.get(name, {})
        gb.set_geometry(EntityGeometry(
            name=name, w=int(fix.get("w", 1)), h=int(fix.get("h", 1)),
            pickup_distance=float(fix.get("pickup_distance", 0.0)),
            drop_distance=float(fix.get("drop_distance", 0.0)),
            wire_reach=float(fix.get("wire_reach", 0.0)),
            supply_area=float(fix.get("supply_area", 0.0)),
            mining_area=fix.get("mining_area"),
            pipe_ports=list(fix.get("pipe_ports") or []),
            output_port_dv=dict(fix.get("output_port_dv") or {}),
            module_slots=int(fix.get("module_slots", 0)),
            distribution_effectivity=float(fix.get("distribution_effectivity", 0.0)),
        ))
    return gb