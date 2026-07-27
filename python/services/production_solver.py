"""ProductionSolver — solveur de chaîne de production par DÉBIT (P2).

Sous-module déterministe de FactoryBuilder (cf. docs/production-solver.md).
Transforme un objectif de débit (item, rate/sec) en BOM complète : tous les
nœuds du graphe de dépendances (cible + intermédiaires + feuilles d'extraction),
avec le nombre de machines par nœud et les sous-débits en amont.

Aucun LLM, aucune décision d'arbitrage : c'est de la computation pure (DFS sur
le graphe de recettes + calcul de débit). Le choix des tiers de machines, des
patches, du layout reste à FactoryBuilder (arbitrage LLM). Le solveur reçoit
une `KnowledgeBase` (données) et un `ProductionRequest` (objectif + overrides).

Formule de débit (régime permanent Factorio) :
    débit_machine = result_count × crafting_speed / craft_time_sec
    nb_machines = ceil(rate / débit_machine)         # production jamais < demande
    rate_effective = nb_machines × débit_machine      # propagé aux ingrédients

Propagation du débit EFFECTIF (après arrondi) en amont : toute la chaîne
surproduit cohéremment, pas seulement le dernier étage (sinon un étage
intermédiaire non arrondi devient goulot).

Coal (carburant des machines burner) N'est PAS compté au P2 (cf. §6, nœud fuel
en S3). Les resources extraites (RAW_RESOURCES) sont les feuilles.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from services.knowledge import (
    KnowledgeBase, MachineSpec, Recipe, THROUGHPUTS, FLUID_RAW_RESOURCES, FLUID_ITEMS,
    BEACON_FIXTURE, MODULE_FIXTURE,
)

# S2b-2 : consommation steam par steam-engine (units/s). 900 kW / 30 000 J par unité de
# steam (heat_capacity=200 J/°C × ΔT=150 de 15°C à 165°C) = 30 steam/s. Hardcodé (probe
# live S2b-2 : max_energy_production inaccessible runtime). Utilisé pour le sink power
# (co-produit orphelin steam -> steam-engine, machine_count=ceil(rate/30)).
STEAM_ENGINE_CONSUMPTION: float = 30.0


# ===== Requête et sortie =====

@dataclass
class ProductionRequest:
    """Objectif de production : produire `item` à `rate_per_sec` unités/sec."""
    item: str
    rate_per_sec: float
    # Override optionnel des tiers de machines (arbitrage FactoryBuilder) :
    #   {"smelting": "steel-furnace", "crafting": "assembling-machine-2", "mine": "burner-mining-drill"}
    machine_tiers: dict = field(default_factory=dict)
    # S3a : bonus de modules agrégés par machine (clé = nom machine). FactoryBuilder
    # calcule depuis la densité de beacons (compute_module_effect) ; le solveur APPLIQUE
    # ces bonus sans les calculer (découplage solveur<->layout, évite la circularité
    # machine_count AVANT placement beacons). Défaut vide -> aucun bonus (back-compat
    # S0/S1/S2). Voir ModuleEffect ci-dessous.
    module_effects: dict = field(default_factory=dict)


@dataclass(frozen=True)
class ModuleEffect:
    """Bonus agrégés d'une machine entourée de beacons (S3a, Option A).

    Calculés par FactoryBuilder depuis la densité de beacons (formule Factorio 2.0 :
    total_bonus = beacon_count * distribution_effectivity * module_bonus). Le solveur
    APPLIQUE ces bonus à per_machine (production_solver.py:189) sans les calculer.

    - speed_bonus : multiplie crafting_speed -> (1 + speed_bonus). ex. 8 beacons
      speed-module-3 en 2.0 (FFF #409) : 1.5 * sqrt(8) * 0.5 = 2.12 (speed x3.12).
    - productivity_bonus : produits gratuits SANS consommer d'ingrédients
      supplémentaires. (1 + productivity_bonus) multiplie result_count ET divise
      la conso d'ingrédient par unité produite.
    - energy_bonus : multiplicateur conso électrique (audit seulement, solveur l'ignore
      -> dimensionnement électrique = autre module).
    """
    speed_bonus: float = 0.0
    productivity_bonus: float = 0.0
    energy_bonus: float = 0.0


def compute_module_effect(beacon_count: int, module_name: str,
                          beacon_name: str = "beacon") -> ModuleEffect:
    """Bonus agrégé d'une machine entourée de `beacon_count` beacons uniformément
    remplis de `module_name` (S3a/S3b, Option A).

    Formule Factorio 2.0 (FFF #409, rendements décroissants) : le bonus combiné de
    `n` beacons qui se chevauchent = distribution_effectivity * sqrt(n) * module_bonus
    (chaque beacon contribue dist/sqrt(n), donc n beacons -> dist*sqrt(n)). C'est la
    différence clé vs 1.1 (où c'était linéaire n*dist*mod avec dist=0.5). En 2.0,
    dist=1.5 (×3) compense la racine : 8 beacons speed-module-3 -> 1.5*sqrt(8)*0.5
    = 2.12 (speed x3.12), vs 1.1 : 8*0.5*0.5 = 2.0 (x3). Utilisé par FactoryBuilder
    (ou les tests) pour remplir ProductionRequest.module_effects[machine_name] -> le
    solveur APPLIQUE le bonus (ne le calcule pas, découplage solveur<->layout).

    CONSTAT : module_bonus (MODULE_FIXTURE) et distribution_effectivity (BEACON_FIXTURE)
    sont hardcodés (inaccessibles au runtime, cf. S2a fluids). distribution_effectivity
    EST accessible live (proto.distribution_effectivity=1.5, S3b-live) et override le
    fixture dans populate_from_rcon -> le caller doit reconstruire le fixture si RCON a
    surchargé la valeur.
    """
    beacon = BEACON_FIXTURE.get(beacon_name, {})
    mod = MODULE_FIXTURE.get(module_name, {})
    dist = float(beacon.get("distribution_effectivity", 0.0))
    # FFF #409 : rendements décroissants. n beacons -> dist * sqrt(n) * module_bonus.
    factor = dist * math.sqrt(max(0, beacon_count))
    return ModuleEffect(
        speed_bonus=factor * float(mod.get("speed", 0.0)),
        productivity_bonus=factor * float(mod.get("productivity", 0.0)),
        energy_bonus=factor * float(mod.get("energy", 0.0)),
    )


@dataclass
class ProductionNode:
    """Un nœud du graphe = produire `item` à un certain débit."""
    item: str
    role: str                       # "craft" | "smelt" | "mine" | "fluid"
    rate_per_sec: float             # débit demandé (avant arrondi)
    rate_effective: float           # débit réellement produit (après ceil)
    machine: str
    machine_count: int              # math.ceil — jamais en dessous
    craft_time_sec: float
    crafting_speed: float
    # Ingrédients consommés AU DÉBIT EFFECTIF (propagation) :
    ingredients: list[tuple[str, float]] = field(default_factory=list)
    # S2a : transport ("belt"|"pipe") et phase ("solid"|"fluid"|"mixed"). Défauts
    # "belt"/"solid" -> nœuds solides inchangés (back-compat). "phase" dérivée :
    # item fluide OU recette avec ingrédients/produits fluides -> "fluid"/"mixed".
    transport: str = "belt"
    phase: str = "solid"
    # S2b-1 : pour les sinks (role="store", co-produits orphelins), item de la node
    # source qui produit ce co-produit. Additif, défaut vide -> nodes non-sink inchangés
    # (back-compat). Permet au layout d'insérer le sink juste après sa source et de
    # connecter le port output co-produit de la source au storage-tank.
    source_item: str = ""
    # S3a : audit des bonus de modules appliqués (pour LayoutPlanner/FactoryBuilder).
    # Additifs, défauts 0.0 -> nodes sans module inchangés (back-compat).
    speed_bonus: float = 0.0
    productivity_bonus: float = 0.0


@dataclass
class ProductionPlan:
    """BOM complète étendue au débit (réponse du solveur)."""
    request: ProductionRequest
    nodes: list[ProductionNode]      # tous les nœuds (cible + intermédiaires + leaves)
    leaves: list[ProductionNode]     # nœuds d'extraction (mining)
    total_machines: dict = field(default_factory=dict)  # {machine_name: count}
    feasibility: str = "ok"          # "ok" | "missing_recipe:<item>" | ...
    notes: list[str] = field(default_factory=list)


# ===== Rôles =====

def _role_for(category: str) -> str:
    # S2a : catégories fluides (oil-refinery, chemical-plant) -> rôle "fluid".
    # smelting/crafting inchangés (back-compat S0/S1).
    # S2b-1 : organic-or-chemistry (cracking, Space Age) -> "fluid" (chemical-plant).
    # S2b-2 : boiling (recette synthétique steam) -> "fluid" (boiler).
    if category in ("oil-processing", "chemistry", "organic-or-chemistry", "boiling"):
        return "fluid"
    return "smelt" if category == "smelting" else "craft"


# ===== Solveur =====

def solve(request: ProductionRequest, kb: KnowledgeBase) -> ProductionPlan:
    """Décompose un objectif de débit en BOM complète.

    DFS sur le graphe de dépendances, accumulation des débits (un même ingrédient
    consommé par plusieurs nœuds s'additionne), arrondi supérieur + propagation
    du débit effectif à chaque nœud. Déterministe, sans effet de bord sur kb.
    """
    if request.rate_per_sec <= 0:
        return ProductionPlan(request, [], [], {}, feasibility="ok",
                              notes=["rate<=0 : rien à produire"])

    rates: dict[str, float] = {request.item: request.rate_per_sec}
    nodes: dict[str, ProductionNode] = {}
    queue: list[str] = [request.item]
    visited: set[str] = set()
    # S2b-1 : co-produits (recettes multi-produits type advanced-oil : heavy+light+
    # petroleum). Accumulés pendant le BFS, résolus en sinks (storage-tank) après BFS
    # pour les co-produits orphelins (jamais demandés comme ingrédient).
    coproducts: list[tuple[str, float, str]] = []

    while queue:
        item = queue.pop(0)
        if item in visited:
            continue
        visited.add(item)
        rate = rates.get(item, 0.0)

        # --- feuille : resource extraite ---
        # S2a : water n'est pas dans RAW_RESOURCES (tile, pas resource-entity) mais
        # est une feuille fluide (FLUID_RAW_RESOURCES) -> offshore-pump.
        if item in kb.raw_resources or item in FLUID_RAW_RESOURCES:
            # S2a : mining_machine item-aware (water->offshore-pump, crude-oil->
            # pumpjack, sinon drill). per_machine dépend du mining_kind.
            m = kb.mining_machine(item, request.machine_tiers)
            if m is None:
                return ProductionPlan(request, list(nodes.values()),
                                      [n for n in nodes.values() if n.role == "mine"],
                                      _totals(nodes), feasibility=f"no_mining_machine",
                                      notes=[f"pas de machine pour {item}"])
            # S2a : débit par machine selon le kind (water = offshore-pump, fluide =
            # pumpjack.mining_speed, solide = drill.mining_speed back-compat).
            if m.mining_kind == "water":
                per_machine = THROUGHPUTS.get("offshore-pump", 1200.0)
            else:
                per_machine = m.mining_speed
            if per_machine <= 0:
                return ProductionPlan(request, list(nodes.values()),
                                      [n for n in nodes.values() if n.role == "mine"],
                                      _totals(nodes), feasibility=f"no_mining_machine",
                                      notes=[f"debit nul pour {item} ({m.name})"])
            count = math.ceil(rate / per_machine)
            eff = count * per_machine
            # S2a : transport/phase selon mining_kind (fluide/eau -> pipe, solide -> belt).
            transport = "pipe" if m.mining_kind in ("fluid", "water") else "belt"
            phase = "fluid" if m.mining_kind in ("fluid", "water") else "solid"
            nodes[item] = ProductionNode(
                item=item, role="mine", rate_per_sec=rate, rate_effective=eff,
                machine=m.name, machine_count=count, craft_time_sec=0.0,
                crafting_speed=per_machine, ingredients=[],
                transport=transport, phase=phase,
            )
            continue

        # --- nœud de craft/smelt ---
        recipe = kb.recipe_of(item)
        if recipe is None:
            return ProductionPlan(request, list(nodes.values()),
                                  [n for n in nodes.values() if n.role == "mine"],
                                  _totals(nodes), feasibility=f"missing_recipe:{item}",
                                  notes=[f"item non couvert : {item!r}"])
        if recipe.craft_time_sec <= 0:
            return ProductionPlan(request, list(nodes.values()),
                                  [n for n in nodes.values() if n.role == "mine"],
                                  _totals(nodes),
                                  feasibility=f"invalid_craft_time:{item}",
                                  notes=[f"energy=0 pour {item!r}"])
        m = kb.pick_machine(recipe.category, request.machine_tiers)
        if m is None or m.crafting_speed <= 0:
            return ProductionPlan(request, list(nodes.values()),
                                  [n for n in nodes.values() if n.role == "mine"],
                                  _totals(nodes),
                                  feasibility=f"no_machine_for_category:{recipe.category}",
                                  notes=[f"aucune machine pour {item!r} (category={recipe.category})"])

        # S3a : bonus de modules (Option A : agrégé par FactoryBuilder via
        # ProductionRequest.module_effects, appliqué ici). speed_bonus multiplie
        # crafting_speed ; productivity_bonus multiplie result_count ET réduit la
        # conso d'ingrédient par unité produite (produits gratuits). Défaut
        # ModuleEffect() -> speed_bonus=0, productivity_bonus=0 -> per_machine S2
        # inchangé (back-compat).
        meff = request.module_effects.get(m.name, ModuleEffect())
        effective_speed = m.crafting_speed * (1.0 + meff.speed_bonus)
        effective_productivity = 1.0 + meff.productivity_bonus
        per_machine = (recipe.result_count_for(item) * effective_productivity
                        * effective_speed / recipe.craft_time_sec)
        count = math.ceil(rate / per_machine)
        eff = count * per_machine

        # S2a : phase/transport selon la recette (ingrédients/produits fluides).
        # - "fluid" : entrées ET sorties fluides (ex. basic-oil-processing : crude-oil
        #   -> petroleum-gas). transport="pipe".
        # - "mixed" : au moins un fluide côté entrée OU sortie, l'autre solide (ex.
        #   plastic-bar : coal solide + petroleum-gas fluide -> plastic-bar solide).
        # - "solid" : aucun fluide (back-compat S0/S1, transport="belt").
        has_fluid_in = bool(recipe.fluid_ingredients)
        has_fluid_out = bool(recipe.fluid_products)
        if has_fluid_in and has_fluid_out:
            phase = "fluid"
        elif has_fluid_in or has_fluid_out:
            phase = "mixed"
        else:
            phase = "solid"
        transport = "pipe" if (has_fluid_in or has_fluid_out) else "belt"

        # Ingrédients consommés au débit EFFECTIF (propagation).
        # S2b-1 : result_count_for(item) (recettes multi-produits) au lieu de result_count.
        ingredients: list[tuple[str, float]] = []
        rc_item = recipe.result_count_for(item)
        for ing_name, ing_amount in recipe.ingredients:
            # S3a : la productivité produit des unités gratuites SANS consommer
            # d'ingrédients supplémentaires -> divise la conso par effective_productivity.
            # Back-compat : effective_productivity=1 (module_effects vide) -> formule S2
            # inchangée (ing_rate = eff * ing_amount / rc_item).
            ing_rate = eff * ing_amount / (rc_item * effective_productivity)
            rates[ing_name] = rates.get(ing_name, 0.0) + ing_rate
            if ing_name not in visited:
                queue.append(ing_name)
            ingredients.append((ing_name, ing_rate))

        nodes[item] = ProductionNode(
            item=item, role=_role_for(recipe.category),
            rate_per_sec=rate, rate_effective=eff,
            machine=m.name, machine_count=count,
            craft_time_sec=recipe.craft_time_sec, crafting_speed=m.crafting_speed,
            ingredients=ingredients,
            transport=transport, phase=phase,
            speed_bonus=meff.speed_bonus, productivity_bonus=meff.productivity_bonus,
        )
        # S2b-1 : enregistrer les co-produits fluides (autres que l'item demandé) au
        # débit effectif de la source. Résolus en sinks après BFS si orphelins.
        for cp_name, cp_amount in recipe.fluid_products:
            if cp_name != item:
                coproducts.append((cp_name, eff * cp_amount / rc_item, item))

    # S2b-1 : sinks pour les co-produits orphelins (jamais demandés comme ingrédient).
    # Puit infini déterministe = storage-tank (décision utilisateur, pas de circuit).
    # sinks_by_source[src] = sinks alimentés par le node source `src` (pour l'ordre
    # topologique : sink inséré juste après sa source dans all_nodes).
    # S2b-2 : co-produit orphelin "steam" -> steam-engine (sink power, consomme 30 steam/s
    # par machine, cf. STEAM_ENGINE_CONSUMPTION). Autres fluides orphelins -> storage-tank.
    sinks_by_source: dict[str, list[ProductionNode]] = {}
    for cp, cp_rate, src in coproducts:
        if cp not in FLUID_ITEMS or rates.get(cp, 0.0) > 1e-9:
            continue
        if cp == "steam":
            count = max(1, math.ceil(cp_rate / STEAM_ENGINE_CONSUMPTION))
            sink = ProductionNode(
                item=cp, role="power", rate_per_sec=cp_rate, rate_effective=cp_rate,
                machine="steam-engine", machine_count=count, craft_time_sec=0.0,
                crafting_speed=0.0, ingredients=[],
                transport="pipe", phase="fluid",
                source_item=src,
            )
        else:
            sink = ProductionNode(
                item=cp, role="store", rate_per_sec=cp_rate, rate_effective=cp_rate,
                machine="storage-tank", machine_count=1, craft_time_sec=0.0,
                crafting_speed=0.0, ingredients=[],
                transport="pipe", phase="fluid",
                source_item=src,
            )
        sinks_by_source.setdefault(src, []).append(sink)

    # Ordre topologique : nodes (BFS) + sinks juste après leur source.
    all_nodes: list[ProductionNode] = []
    for n in nodes.values():
        all_nodes.append(n)
        all_nodes.extend(sinks_by_source.get(n.item, []))
    leaves = [n for n in all_nodes if n.role == "mine"]
    totals = _totals(nodes)
    for sinks in sinks_by_source.values():
        for s in sinks:
            totals[s.machine] = totals.get(s.machine, 0) + s.machine_count
    return ProductionPlan(
        request=request, nodes=all_nodes, leaves=leaves,
        total_machines=totals, feasibility="ok",
    )


def _totals(nodes: dict[str, ProductionNode]) -> dict:
    out: dict[str, int] = {}
    for n in nodes.values():
        out[n.machine] = out.get(n.machine, 0) + n.machine_count
    return out


# ===== Helpers de diagnostic (logs/tests) =====

def plan_summary(plan: ProductionPlan) -> str:
    """Résumé compact : 'feasibility | totals | nœuds'."""
    if plan.feasibility != "ok":
        return f"INFEASIBLE {plan.feasibility}"
    totals = ", ".join(f"{k}={v}" for k, v in sorted(plan.total_machines.items()))
    chains = ", ".join(f"{n.item}:{n.role}×{n.machine_count}" for n in plan.nodes)
    return f"totals[{totals}] chain[{chains}]"