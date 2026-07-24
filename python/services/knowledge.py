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


# Lookup de recette injecté (DIP : le planificateur ne sait pas comment on
# obtient la recette — c'est perception.recipe_of côté agent).
RecipeLookup = Callable[[str], Optional[list[tuple[str, int]]]]


# ===== Helpers de simulation d'inventaire =====

def _credit(inv: dict[str, int], item: str, count: int) -> None:
    inv[item] = inv.get(item, 0) + count


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
        raise ValueError(f"item non couvert par le planificateur P1 : {goal.item!r}")

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
        # Smelter dans un four du kit posé près du character.
        steps.append(Step("place_furnace", {}))
        steps.append(Step("move_items", {"item": "coal", "to_entity": True, "count": 5}))
        steps.append(Step("move_items", {"item": ore, "to_entity": True, "count": ore_need}))
        steps.append(Step("wait", {"ticks": _smelt_ticks(ore_need)}))
        steps.append(Step("move_items", {"item": goal.item, "to_entity": False, "count": missing}))
        _credit(sim_inv, goal.item, missing)

    elif mode == "craft":
        recipe = recipe_lookup(goal.item)
        if recipe is None:
            raise ValueError(f"aucune recette craftable pour {goal.item!r}")
        per = int(spec.get("per", 1))
        crafts = math.ceil(missing / per)
        # Produire chaque ingrédient (récursif — arbitre selon l'inventaire).
        for ing_name, ing_amount in recipe:
            steps += _plan(ProductionGoal(ing_name, ing_amount * crafts),
                           sim_inv, recipe_lookup)
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