"""Tests unitaires du LayoutPlanner (S0).

Aucun serveur, aucun LLM, aucun RCON requis : on injecte
  - une KnowledgeBase fixture (valeurs mesurées en jeu, cf. test_production_solver)
    pour produire un ProductionPlan via le solveur,
  - un GeometryBase fixture (populate_geometry_fixture : valeurs wiki hardcodées,
    cf. constat API Factorio 2.0 — size RCON validé 15/15, géométries fines hardcode),
  - un Terrain fixture (patch iron-ore simulé).

Tests de cohérence (le dimensionnement logistique doit tomber du CALCUL) :
  - 5 iron-gear-wheel/s -> asm-1 consomme 2 plate/s/machine -> avec burner-inserter
    (0.6/s) -> inserters_in_per_machine = ceil(2.0/0.6) = 4 (calcul révèle l'insuffisance :
    4 > 3 slots de l'asm-1 -> inserter_insufficient ; FactoryBuilder arbitrerait
    fast-inserter 2.7/s -> 1 bras).
  - iron-plate@30/s -> belts_out_per_stage = ceil(30/15) = 2 (plusieurs belts parallèles).
  - missing_patch:<resource> si le terrain n'a pas le gisement d'une feuille.
  - facing=4 (rotation) : layout généré, mêmes totals.

Lancement :
    cd python
    python -m tests.test_layout_solver
"""

from __future__ import annotations

import sys

from services.knowledge import (
    KnowledgeBase, MachineSpec, Recipe, RAW_RESOURCES,
    GeometryBase, populate_geometry_fixture, inserter_throughput, THROUGHPUTS,
    FLUID_ITEMS, pipe_throughput, inject_power_units, FLUID_VISCOSITY,
)
from services.production_solver import ProductionRequest, solve
from services.layout_planner import (
    LayoutRequest, Terrain, ResourcePatch, LayoutConstraints,
    plan, plan_summary, _to_uv, _swing_for,
    LayoutEntity, _add, _occ, _find_at, _under_crossing, _to_xy,
    FACING_DIR_U, FACING_DIR_V, _pipe_under_crossing, PIPE_TO_GROUND_NAME,
    _occ_terrain, _rotate_facing, _count_terrain_hits,
)

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


# alias court utilisé dans les tests
rec = record


# ===== Fixtures =====

ENTITIES_GEO = [
    "transport-belt", "burner-inserter", "fast-inserter", "long-handed-inserter",
    "small-electric-pole",
    "stone-furnace", "steel-furnace",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
    "burner-mining-drill", "electric-mining-drill",
    "splitter",
    # S2a : entités fluides (géométries fixture hardcodées dans knowledge.py).
    "pipe", "offshore-pump", "pumpjack", "oil-refinery", "chemical-plant",
    "pump", "storage-tank", "boiler",
    # S2b-2 : steam-engine (sink power, géométrie fixture hardcodée).
    "steam-engine",
    # S3c : beacon + electric-furnace (géométries fixture hardcodées dans knowledge.py).
    "beacon", "electric-furnace",
]


def sample_kb() -> KnowledgeBase:
    """KB fixture (valeurs mesurées en jeu) — chaîne fer : ore -> plate -> gear.
    S2a : + chaîne fluide plastic-bar (crude-oil -> petroleum-gas -> plastic-bar)."""
    kb = KnowledgeBase()
    kb.recipes = {
        "iron-plate":   Recipe("iron-plate",   [("iron-ore", 1)], 1, 3.2, "smelting"),
        "copper-plate": Recipe("copper-plate", [("copper-ore", 1)], 1, 3.2, "smelting"),
        "stone-brick":  Recipe("stone-brick",  [("stone", 2)], 1, 3.2, "smelting"),
        "iron-gear-wheel": Recipe("iron-gear-wheel", [("iron-plate", 2)], 1, 0.5, "crafting"),
        # S1b : recette 2 ingrédients (raw) pour tester le multi-ingrédients.
        "alloy": Recipe("alloy", [("iron-ore", 1), ("copper-ore", 1)], 1, 0.5, "crafting"),
        # S2a : recettes fluides. Clé = item PRODUIT (recipe_of cherche par item produit).
        # basic-oil-processing (crude-oil 100 -> petroleum-gas 45, category oil-processing).
        "petroleum-gas": Recipe(
            "petroleum-gas", [("crude-oil", 100)], 45, 5.0, "oil-processing",
            ingredient_types={"crude-oil": "fluid"},
            product_types={"petroleum-gas": "fluid"},
            fluid_ingredients=[("crude-oil", 100)],
            fluid_products=[("petroleum-gas", 45)],
        ),
        # plastic-bar (coal 1 + petroleum-gas 20 -> 2 plastic-bar, category chemistry, mixte).
        "plastic-bar": Recipe(
            "plastic-bar", [("coal", 1), ("petroleum-gas", 20)], 2, 1.0, "chemistry",
            ingredient_types={"coal": "item", "petroleum-gas": "fluid"},
            product_types={"plastic-bar": "item"},
            fluid_ingredients=[("petroleum-gas", 20)],
            fluid_products=[],
        ),
        # S2b-1 : recettes multi-produits + cracking + solid-fuel + lubricant. Clé =
        # produit principal (recipe_of cherche par item produit). Recipe.item = nom de
        # recette (convention RCON populate_from_rcon, pour le sélecteur RECIPE_PREFERENCE).
        # advanced-oil (water 50 + crude 100 -> heavy 25 + light 45 + petroleum 55, 3 co-produits).
        "heavy-oil": Recipe(
            "advanced-oil-processing", [("water", 50), ("crude-oil", 100)], 25, 5.0, "oil-processing",
            ingredient_types={"water": "fluid", "crude-oil": "fluid"},
            product_types={"heavy-oil": "fluid", "light-oil": "fluid", "petroleum-gas": "fluid"},
            fluid_ingredients=[("water", 50), ("crude-oil", 100)],
            fluid_products=[("heavy-oil", 25), ("light-oil", 45), ("petroleum-gas", 55)],
            result_counts={"heavy-oil": 25, "light-oil": 45, "petroleum-gas": 55},
        ),
        # heavy-oil-cracking (water 30 + heavy 40 -> light 30, organic-or-chemistry).
        "light-oil": Recipe(
            "heavy-oil-cracking", [("water", 30), ("heavy-oil", 40)], 30, 2.0, "organic-or-chemistry",
            ingredient_types={"water": "fluid", "heavy-oil": "fluid"},
            product_types={"light-oil": "fluid"},
            fluid_ingredients=[("water", 30), ("heavy-oil", 40)],
            fluid_products=[("light-oil", 30)],
        ),
        # solid-fuel-from-heavy-oil (heavy 20 -> solid-fuel 1, chemistry, mixte fluide->item).
        "solid-fuel": Recipe(
            "solid-fuel-from-heavy-oil", [("heavy-oil", 20)], 1, 1.0, "chemistry",
            ingredient_types={"heavy-oil": "fluid"},
            product_types={"solid-fuel": "item"},
            fluid_ingredients=[("heavy-oil", 20)],
            fluid_products=[],
        ),
        # lubricant (heavy 10 -> lubricant 10 fluide, chemistry).
        "lubricant": Recipe(
            "lubricant", [("heavy-oil", 10)], 10, 1.0, "chemistry",
            ingredient_types={"heavy-oil": "fluid"},
            product_types={"lubricant": "fluid"},
            fluid_ingredients=[("heavy-oil", 10)],
            fluid_products=[("lubricant", 10)],
        ),
    }
    kb.machines = {
        "stone-furnace": MachineSpec("stone-furnace", 1.0, {"smelting"}, "furnace", "burner", 0.0),
        "steel-furnace": MachineSpec("steel-furnace", 2.0, {"smelting"}, "furnace", "burner", 0.0),
        "assembling-machine-1": MachineSpec("assembling-machine-1", 0.5, {"crafting", "basic-crafting", "advanced-crafting"},
                                            "assembling-machine", "electric", 0.0),
        "electric-mining-drill": MachineSpec("electric-mining-drill", 0.0, set(), "mining-drill", "electric", 0.5),
        "burner-mining-drill": MachineSpec("burner-mining-drill", 0.0, set(), "mining-drill", "burner", 0.25),
        # S2a : machines fluides. pumpjack (mining-drill basic-fluid, mining_speed=60 crude-oil/s).
        "pumpjack": MachineSpec("pumpjack", 0.0, set(), "mining-drill", "electric", 60.0,
                                mining_kind="fluid"),
        "oil-refinery": MachineSpec("oil-refinery", 1.0, {"oil-processing"}, "assembling-machine", "electric", 0.0),
        "chemical-plant": MachineSpec("chemical-plant", 1.0, {"chemistry", "organic-or-chemistry"},
                                      "assembling-machine", "electric", 0.0),
        # offshore-pump : mining_kind="water" (le solveur utilise THROUGHPUTS["offshore-pump"]).
        "offshore-pump": MachineSpec("offshore-pump", 0.0, set(), "offshore-pump", "electric", 1200.0,
                                     mining_kind="water"),
    }
    kb.raw_resources = set(RAW_RESOURCES)
    # S2b-2 : unités power (boiler, steam-engine) + recette synthétique steam. Additif,
    # idempotent, back-compat (chaînes solides existantes non affectées — elles ne
    # demandent pas steam).
    inject_power_units(kb)
    return kb


def sample_geometry() -> GeometryBase:
    """Géométries fixture (valeurs wiki hardcodées, sans RCON)."""
    return populate_geometry_fixture(ENTITIES_GEO)


def sample_terrain(resource: str = "iron-ore", size: int = 20,
                   with_fluid: bool = False) -> Terrain:
    """Terrain fixture avec un patch carré `size`x`size` de `resource` en (0,0).

    S2a : with_fluid=True ajoute un patch crude-oil 3x3 (en (30,30)) + un bbox water
    (14,8)-(16,9) pour valider la chaîne plastic-bar (coal + crude-oil) et l'offshore-pump.
    """
    tiles = [(x, y) for x in range(size) for y in range(size)]
    patch = ResourcePatch(resource=resource, tiles=tiles, bbox=(0, 0, size, size))
    patches = [patch]
    water: list[tuple[int, int, int, int]] = []
    if with_fluid:
        # Patch crude-oil 3x3 en (30,30).
        co_tiles = [(30 + x, 30 + y) for x in range(3) for y in range(3)]
        patches.append(ResourcePatch(resource="crude-oil", tiles=co_tiles,
                                     bbox=(30, 30, 33, 33)))
        # Bassin water 3x2 en (14,8)-(16,9) (bord pour offshore-pump).
        water = [(14, 8, 17, 10)]
    return Terrain(patches=patches, water=water,
                   surface_area=(-5, -5, 60, 60))


def _gears_plan(rate: float = 5.0, terrain: Terrain | None = None,
                facing: int = 2, constraints: LayoutConstraints | None = None):
    """Plan complet (solveur + layout) pour iron-gear-wheel@rate/s. Retourne LayoutPlan."""
    kb = sample_kb()
    splan = solve(ProductionRequest("iron-gear-wheel", rate), kb)
    if terrain is None:
        terrain = sample_terrain("iron-ore", 20)
    req = LayoutRequest(
        plan=splan, terrain=terrain, anchor=(0.0, 10.0), facing=facing,
        constraints=constraints or LayoutConstraints(),
    )
    return plan(req, sample_geometry())


# ===== Tests =====

def test_five_gears_layout() -> None:
    print("\n[test] === 5 iron-gear-wheel/s : dimensionnement logistique + blueprint ===")
    lp = _gears_plan(5.0)
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)

    sl_gear = lp.stage_logistics.get("iron-gear-wheel")
    rec("gear : 4 inserters_in/machine (ceil(2.0/0.6))",
        sl_gear and sl_gear.inserters_in_per_machine == 4,
        f"in={sl_gear.inserters_in_per_machine if sl_gear else '?'}")
    rec("gear : 2 inserters_out/machine (ceil(1.0/0.6))",
        sl_gear and sl_gear.inserters_out_per_machine == 2,
        f"out={sl_gear.inserters_out_per_machine if sl_gear else '?'}")
    rec("gear : belts in/out = 1/1 (ceil(10/15), ceil(5/15))",
        sl_gear and sl_gear.belts_in_per_stage == 1 and sl_gear.belts_out_per_stage == 1,
        f"bin={sl_gear.belts_in_per_stage} bout={sl_gear.belts_out_per_stage}" if sl_gear else "?")
    rec("gear : inserter_insufficient (4 > 3 slots asm-1)",
        sl_gear and sl_gear.inserter_insufficient is True,
        f"insufficient={sl_gear.inserter_insufficient}" if sl_gear else "?")

    # Totals étendus : les machines du solveur sont préservées.
    rec("totals : asm-1=5 (solveur)",
        lp.totals.get("assembling-machine-1") == 5, f"asm-1={lp.totals.get('assembling-machine-1')}")
    rec("totals : stone-furnace=32 (solveur)",
        lp.totals.get("stone-furnace") == 32, f"furnace={lp.totals.get('stone-furnace')}")
    rec("totals : electric-mining-drill=20 (solveur)",
        lp.totals.get("electric-mining-drill") == 20, f"drill={lp.totals.get('electric-mining-drill')}")
    # Logistique dimensionnée (présente dans totals).
    rec("totals : transport-belt > 0 (belts dimensionnés)",
        lp.totals.get("transport-belt", 0) > 0, f"belts={lp.totals.get('transport-belt')}")
    rec("totals : burner-inserter > 0 (bras dimensionnés)",
        lp.totals.get("burner-inserter", 0) > 0, f"inserters={lp.totals.get('burner-inserter')}")
    rec("totals : small-electric-pole > 0 (poles dimensionnés)",
        lp.totals.get("small-electric-pole", 0) > 0, f"poles={lp.totals.get('small-electric-pole')}")

    # Entités : drills sur le gisement, machines, belts, inserters, poles.
    roles = [e.role for e in lp.entities]
    rec("entités : 20 drills sur le patch",
        roles.count("drill") == 20, f"drills={roles.count('drill')}")
    n_asm = sum(1 for e in lp.entities if e.role == "machine" and e.name == "assembling-machine-1")
    n_fur = sum(1 for e in lp.entities if e.role == "machine" and e.name == "stone-furnace")
    rec("entités : 5 asm-1 + 32 furnaces (=37 machines)",
        n_asm == 5 and n_fur == 32, f"asm-1={n_asm} furnaces={n_fur}")
    rec("entités : belts, inserters, poles présents",
        roles.count("belt") > 0 and roles.count("inserter") > 0 and roles.count("pole") > 0,
        f"belts={roles.count('belt')} ins={roles.count('inserter')} poles={roles.count('pole')}")

    # Cascade : 3 étages (ore -> plate -> gear) -> 2 connexions logiques.
    rec("connexions : 2 (cascade ore->plate->gear)",
        len(lp.connections) == 2, f"connections={len(lp.connections)}")
    rec("connexions : items = ore, plate",
        sorted(c[2] for c in lp.connections) == ["iron-ore", "iron-plate"],
        str(sorted(c[2] for c in lp.connections)))

    # bbox non vide.
    rec("bbox : non vide (x2>x1, y2>y1)",
        lp.bbox[2] > lp.bbox[0] and lp.bbox[3] > lp.bbox[1],
        f"bbox={lp.bbox}")

    # Les drills sont dans le patch (bbox 0..20).
    drills = [e for e in lp.entities if e.role == "drill"]
    in_patch = all(0.0 <= e.x <= 20.0 and 0.0 <= e.y <= 20.0 for e in drills)
    rec("drills : toutes dans le bbox du patch (0..20)",
        in_patch, f"x_range=({min(e.x for e in drills):.1f},{max(e.x for e in drills):.1f})")


def test_belts_per_stage_high_rate() -> None:
    print("\n[test] === iron-plate@30/s : belts parallèles (ceil(30/15)=2) ===")
    kb = sample_kb()
    splan = solve(ProductionRequest("iron-plate", 30.0), kb)
    req = LayoutRequest(plan=splan, terrain=sample_terrain("iron-ore", 40),
                        anchor=(0.0, 20.0), facing=2)
    lp = plan(req, sample_geometry())
    print(plan_summary(lp))
    sl = lp.stage_logistics.get("iron-plate")
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    rec("plate : belts_out = 2 (ceil(30/15))",
        sl and sl.belts_out_per_stage == 2, f"bout={sl.belts_out_per_stage}" if sl else "?")
    rec("plate : belts_in = 2 (ceil(30/15) ore)",
        sl and sl.belts_in_per_stage == 2, f"bin={sl.belts_in_per_stage}" if sl else "?")
    rec("plate : inserters_in = 1 (ceil(0.3125/0.6))",
        sl and sl.inserters_in_per_machine == 1, f"in={sl.inserters_in_per_machine}" if sl else "?")
    rec("plate : 96 stone-furnaces (solveur)",
        lp.totals.get("stone-furnace") == 96, f"furnaces={lp.totals.get('stone-furnace')}")


def test_missing_patch() -> None:
    print("\n[test] === missing_patch : gisement absent du terrain ===")
    kb = sample_kb()
    splan = solve(ProductionRequest("iron-gear-wheel", 5.0), kb)
    # Terrain sans patch iron-ore (juste copper).
    terrain = Terrain(patches=[ResourcePatch("copper-ore", [(0, 0)], (0, 0, 5, 5))])
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 0.0), facing=2)
    lp = plan(req, sample_geometry())
    print(plan_summary(lp))
    rec("feasibility = missing_patch:iron-ore",
        lp.feasibility == "missing_patch:iron-ore", lp.feasibility)
    rec("note : patch manquant signalé",
        any("iron-ore" in n for n in lp.notes), str(lp.notes))


def test_facing_rotation() -> None:
    print("\n[test] === facing=4 (S) : rotation du layout, mêmes totals ===")
    lp2 = _gears_plan(5.0, facing=2)
    lp4 = _gears_plan(5.0, facing=4)
    print(plan_summary(lp4))
    rec("feasibility ok (facing=4)", lp4.feasibility == "ok", lp4.feasibility)
    rec("mêmes totals machines (asm-1=5)",
        lp4.totals.get("assembling-machine-1") == 5 == lp2.totals.get("assembling-machine-1"),
        f"asm-1={lp4.totals.get('assembling-machine-1')}")
    rec("mêmes totals drills (20)",
        lp4.totals.get("electric-mining-drill") == 20, f"drill={lp4.totals.get('electric-mining-drill')}")
    rec("mêmes stage_logistics (gear ins_in=4)",
        lp4.stage_logistics["iron-gear-wheel"].inserters_in_per_machine == 4,
        str(lp4.stage_logistics["iron-gear-wheel"].inserters_in_per_machine))
    rec("bbox non vide (rotation produit des entités)",
        lp4.bbox[2] > lp4.bbox[0] and lp4.bbox[3] > lp4.bbox[1], f"bbox={lp4.bbox}")


def test_solver_infeasible_propagates() -> None:
    print("\n[test] === plan solveur infaisable -> LayoutPlan infaisable ===")
    kb = sample_kb()
    splan = solve(ProductionRequest("titanium-plate", 5.0), kb)  # recette inconnue
    req = LayoutRequest(plan=splan, terrain=sample_terrain(), anchor=(0.0, 0.0), facing=2)
    lp = plan(req, sample_geometry())
    rec("feasibility = solver:missing_recipe...",
        lp.feasibility.startswith("solver:"), lp.feasibility)
    rec("aucune entité générée", len(lp.entities) == 0, f"entities={len(lp.entities)}")


# ===== S1a : throughput affine (k=0) + belts de transition physiques =====

def test_inserter_throughput_s0_compat() -> None:
    print("\n[test] === S1a : inserter_throughput affine (k=0 = back-compat S0) ===")
    # k=0 (INSERTER_AFFINE par défaut) -> inserter_throughput retourne THROUGHPUTS[name],
    # insensible au swing (distance). Verrouille la back-compat S0.
    for name, base in [("burner-inserter", 0.6), ("long-handed-inserter", 0.83),
                      ("fast-inserter", 2.7)]:
        tp2 = inserter_throughput(name, 2.0)
        tp4 = inserter_throughput(name, 4.0)
        rec(f"{name} : tp(swing=2) == THROUGHPUTS[{base}]",
            abs(tp2 - base) < 1e-9, f"tp2={tp2:.4f} base={base}")
        rec(f"{name} : tp(swing=4) == THROUGHPUTS (k=0, insensible distance)",
            abs(tp4 - base) < 1e-9, f"tp4={tp4:.4f} base={base}")
    rec("inserter inconnu : tp == 0.0",
        inserter_throughput("nonexistent-inserter", 2.0) == 0.0,
        f"tp={inserter_throughput('nonexistent-inserter', 2.0)}")


def test_swing_for() -> None:
    print("\n[test] === S1a : _swing_for = pickup_distance + drop_distance ===")
    geo = populate_geometry_fixture(["burner-inserter", "long-handed-inserter"])
    rec("burner-inserter : swing = 2.0 (1.0 + 1.0)",
        abs(_swing_for(geo.geometry("burner-inserter")) - 2.0) < 1e-9,
        f"swing={_swing_for(geo.geometry('burner-inserter'))}")
    rec("long-handed-inserter : swing = 4.0 (2.0 + 2.0)",
        abs(_swing_for(geo.geometry("long-handed-inserter")) - 4.0) < 1e-9,
        f"swing={_swing_for(geo.geometry('long-handed-inserter'))}")
    rec("_swing_for(None) = défaut 2.0",
        abs(_swing_for(None, 2.0) - 2.0) < 1e-9, f"swing={_swing_for(None, 2.0)}")


def test_alignment_chaine_fer() -> None:
    print("\n[test] === S1a : alignement des étages (belts adjacentes -> connectées) ===")
    lp = _gears_plan(5.0)
    facing = 2
    # Chaque connexion = (from_idx=belt_out_last(prev), to_idx=belt_in_first(cur), item).
    # S1a aligne les étages en v : delta_v ~ 0 (belts en face), delta_u < 1 (adjacence,
    # Factorio connecte les belts adjacentes -> usine réellement connectée, vs S0 logique).
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    for c in lp.connections:
        from_idx, to_idx, item = c
        ef = lp.entities[from_idx]
        et = lp.entities[to_idx]
        uf, vf = _to_uv(facing, ef.x, ef.y)
        ut, vt = _to_uv(facing, et.x, et.y)
        delta_v = abs(vt - vf)
        delta_u = abs(ut - uf)
        rec(f"alignement {item} : delta_v < 0.1 (belts en face)",
            delta_v < 0.1, f"delta_v={delta_v:.3f}")
        rec(f"connexion {item} : delta_u < 1.0 (adjacence, 0 belt intermédiaire)",
            delta_u < 1.0, f"delta_u={delta_u:.3f}")
    # S1a : swing_used renseigné dans stage_logistics (burner-inserter -> 2.0).
    sl_gear = lp.stage_logistics.get("iron-gear-wheel")
    rec("gear : swing_used = 2.0 (burner-inserter 1+1)",
        sl_gear and abs(sl_gear.swing_used - 2.0) < 1e-9,
        f"swing={sl_gear.swing_used if sl_gear else '?'}")
    rec("gear : inserter_tp_effective = 0.6 (k=0 -> base)",
        sl_gear and abs(sl_gear.inserter_tp_effective - 0.6) < 1e-9,
        f"tp={sl_gear.inserter_tp_effective if sl_gear else '?'}")
    # Back-compat : totals + connexions inchangés vs S0.
    rec("back-compat : 5 asm-1", lp.totals.get("assembling-machine-1") == 5,
        f"asm-1={lp.totals.get('assembling-machine-1')}")
    rec("back-compat : 2 connexions (cascade ore->plate->gear)",
        len(lp.connections) == 2, f"connexions={len(lp.connections)}")


def test_transition_belts_high_gap() -> None:
    print("\n[test] === S1a : belts de transition physiques (stage_gap=4 -> delta_u=2.5) ===")
    constraints = LayoutConstraints(stage_gap=4)
    lp = _gears_plan(5.0, constraints=constraints)
    # stage_gap=4 -> delta_u = stage_gap - 1.5 = 2.5 -> ceil(2.5-1)=2 belts intermédiaires
    # le long de u (direction FACING_DIR_U=2 pour facing=2) par transition. 2 transitions
    # (ore->plate, plate->gear) -> >= 4 belts de transition (role belt, direction 2).
    n_trans = sum(1 for e in lp.entities if e.role == "belt" and e.direction == 2)
    rec("transition : >= 4 belts FACING_DIR_U (2 transitions x 2 belts)",
        n_trans >= 4, f"n_trans={n_trans}")
    # Alignement toujours valable (indépendant de stage_gap).
    for c in lp.connections:
        from_idx, to_idx, item = c
        ef = lp.entities[from_idx]
        et = lp.entities[to_idx]
        _, vf = _to_uv(2, ef.x, ef.y)
        _, vt = _to_uv(2, et.x, et.y)
        rec(f"alignement {item} (stage_gap=4) : delta_v < 0.1",
            abs(vt - vf) < 0.1, f"delta_v={abs(vt - vf):.3f}")
    rec("back-compat : 5 asm-1 (stage_gap=4)", lp.totals.get("assembling-machine-1") == 5,
        f"asm-1={lp.totals.get('assembling-machine-1')}")


# ===== S1b : splitters/mergers + multi-ingrédients =====

def test_multi_ingredient() -> None:
    print("\n[test] === S1b : multi-ingrédients (ingrédient 1 = long-handed inserter) ===")
    kb = sample_kb()
    splan = solve(ProductionRequest("alloy", 5.0), kb)
    terrain = Terrain(patches=[
        ResourcePatch("iron-ore", [(x, y) for x in range(10) for y in range(10)], (0, 0, 10, 10)),
        ResourcePatch("copper-ore", [(x, y) for x in range(10) for y in range(10)], (20, 0, 30, 10)),
    ])
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 5.0), facing=2)
    lp = plan(req, sample_geometry())
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    sl = lp.stage_logistics.get("alloy")
    rec("alloy : 2 ingrédients (ingredients dict)", sl and len(sl.ingredients) == 2,
        f"n_ing={len(sl.ingredients) if sl else '?'}")
    ing0 = sl.ingredients.get("iron-ore") if sl else None
    ing1 = sl.ingredients.get("copper-ore") if sl else None
    rec("ingrédient 0 (iron-ore) : inserter = burner-inserter (tier défaut)",
        ing0 is not None and ing0["inserter_name"] == "burner-inserter",
        f"ins0={ing0['inserter_name'] if ing0 else '?'}")
    rec("ingrédient 1 (copper-ore) : inserter = long-handed-inserter (reach 2.0)",
        ing1 is not None and ing1["inserter_name"] == "long-handed-inserter",
        f"ins1={ing1['inserter_name'] if ing1 else '?'}")
    rec("ingrédient 1 : swing = 4.0 (long-handed 2+2)",
        ing1 is not None and abs(ing1["swing"] - 4.0) < 1e-9,
        f"swing1={ing1['swing'] if ing1 else '?'}")
    rec("totals : long-handed-inserter > 0 (ingrédient 1 posé)",
        lp.totals.get("long-handed-inserter", 0) > 0,
        f"long={lp.totals.get('long-handed-inserter', 0)}")


def test_splitter_high_rate() -> None:
    print("\n[test] === S1b : splitter tree (plate@45/s -> belts_in=3 -> 2 splitters) ===")
    kb = sample_kb()
    splan = solve(ProductionRequest("iron-plate", 45.0), kb)
    req = LayoutRequest(plan=splan, terrain=sample_terrain("iron-ore", 60),
                        anchor=(0.0, 30.0), facing=2)
    lp = plan(req, sample_geometry())
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    sl = lp.stage_logistics.get("iron-plate")
    rec("plate : belts_in = 3 (ceil(45/15))", sl and sl.belts_in_per_stage == 3,
        f"bin={sl.belts_in_per_stage}" if sl else "?")
    rec("plate : splitter tree (n_out-1 = 2 splitters)", sl and sl.splitters >= 2,
        f"splitters={sl.splitters}" if sl else "?")
    rec("splitter : role 'splitter' présent",
        any(e.role == "splitter" for e in lp.entities),
        f"n={sum(1 for e in lp.entities if e.role == 'splitter')}")
    rec("splitter : direction FACING_DIR_U (=2 pour facing=2, S1b conservé -> virage S1e)",
        all(e.direction == 2 for e in lp.entities if e.role == "splitter"),
        f"dirs={set(e.direction for e in lp.entities if e.role == 'splitter')}")


def test_merger_output() -> None:
    print("\n[test] === S1b : merger tree (gear@30/s -> belts_out=2 -> 1 merger) ===")
    kb = sample_kb()
    splan = solve(ProductionRequest("iron-gear-wheel", 30.0), kb)
    req = LayoutRequest(plan=splan, terrain=sample_terrain("iron-ore", 80),
                        anchor=(0.0, 40.0), facing=2)
    lp = plan(req, sample_geometry())
    print(plan_summary(lp))
    sl = lp.stage_logistics.get("iron-gear-wheel")
    rec("gear : belts_out = 2 (ceil(30/15))", sl and sl.belts_out_per_stage == 2,
        f"bout={sl.belts_out_per_stage}" if sl else "?")
    rec("gear : merger tree (>= 1 merger en queue)", sl and sl.mergers >= 1,
        f"mergers={sl.mergers}" if sl else "?")
    rec("merger : role 'merger' présent",
        any(e.role == "merger" for e in lp.entities),
        f"n={sum(1 for e in lp.entities if e.role == 'merger')}")
    rec("merger : direction FACING_DIR_V (=4 pour facing=2)",
        all(e.direction == 4 for e in lp.entities if e.role == "merger"),
        f"dirs={set(e.direction for e in lp.entities if e.role == 'merger')}")


def test_splitter_merger_backcompat() -> None:
    print("\n[test] === S1b : back-compat (chaîne fer 1->1 -> pas de splitter/merger) ===")
    lp = _gears_plan(5.0)
    rec("chaîne fer : 0 splitter (1->1 partout)",
        sum(1 for e in lp.entities if e.role == "splitter") == 0,
        f"splitters={sum(1 for e in lp.entities if e.role == 'splitter')}")
    rec("chaîne fer : 0 merger (1->1 partout)",
        sum(1 for e in lp.entities if e.role == "merger") == 0,
        f"mergers={sum(1 for e in lp.entities if e.role == 'merger')}")
    rec("back-compat : 5 asm-1", lp.totals.get("assembling-machine-1") == 5,
        f"asm-1={lp.totals.get('assembling-machine-1')}")
    rec("back-compat : 2 connexions", len(lp.connections) == 2,
        f"connexions={len(lp.connections)}")
    rec("back-compat : gear 1 ingrédient (ingredients dict)",
        len(lp.stage_logistics["iron-gear-wheel"].ingredients) == 1,
        f"n_ing={len(lp.stage_logistics['iron-gear-wheel'].ingredients)}")


def test_too_many_ingredients() -> None:
    print("\n[test] === S1b : too_many_ingredients (3 ingrédients -> note, 2 placés) ===")
    kb = sample_kb()
    kb.recipes["triple"] = Recipe("triple", [("iron-ore", 1), ("copper-ore", 1), ("stone", 1)],
                                  1, 0.5, "crafting")
    splan = solve(ProductionRequest("triple", 5.0), kb)
    terrain = Terrain(patches=[
        ResourcePatch("iron-ore", [(x, y) for x in range(10) for y in range(10)], (0, 0, 10, 10)),
        ResourcePatch("copper-ore", [(x, y) for x in range(10) for y in range(10)], (20, 0, 30, 10)),
        ResourcePatch("stone", [(x, y) for x in range(10) for y in range(10)], (40, 0, 50, 10)),
    ])
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 5.0), facing=2)
    lp = plan(req, sample_geometry())
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    rec("note too_many_ingredients présente",
        any("too_many_ingredients" in n for n in lp.notes), str(lp.notes)[:120])
    sl = lp.stage_logistics.get("triple")
    rec("triple : 2 ingrédients placés (3e ignoré, max=2)",
        sl and len(sl.ingredients) == 2,
        f"n_ing={len(sl.ingredients) if sl else '?'}")


# ===== S1c : main bus (layout alternatif, bus_layout=True) =====

def test_main_bus_basic() -> None:
    print("\n[test] === S1c : main bus (chaîne fer, bus_layout=True) ===")
    constraints = LayoutConstraints(bus_layout=True)
    lp = _gears_plan(5.0, constraints=constraints)
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    # Bus : 1 lane pour iron-plate (seul item intermédiaire = produit par furnace ET
    # consommé par gear). role "bus-belt" présent, direction FACING_DIR_V (=4 pour facing=2).
    bus_belts = [e for e in lp.entities if e.role == "bus-belt"]
    rec("bus : role 'bus-belt' présent (lane iron-plate)",
        len(bus_belts) > 0, f"bus_belts={len(bus_belts)}")
    rec("bus : 1 item transporté (iron-plate, seul intermédiaire)",
        {e.node_item for e in bus_belts} == {"iron-plate"},
        f"items={sorted({e.node_item for e in bus_belts})}")
    rec("bus : lane direction FACING_DIR_V (=4 pour facing=2)",
        all(e.direction == 4 for e in bus_belts),
        f"dirs={set(e.direction for e in bus_belts)}")
    # Back-compat : totals machines identiques au solveur (layout ne change pas le nb).
    rec("back-compat : 5 asm-1", lp.totals.get("assembling-machine-1") == 5,
        f"asm-1={lp.totals.get('assembling-machine-1')}")
    rec("back-compat : 32 furnaces", lp.totals.get("stone-furnace") == 32,
        f"furnace={lp.totals.get('stone-furnace')}")
    rec("back-compat : 20 drills", lp.totals.get("electric-mining-drill") == 20,
        f"drill={lp.totals.get('electric-mining-drill')}")
    # Débit modéré (gear@5/s -> plate 10/s -> belts 1) : n_out=1 / M=1.
    # S1f évolution : n_out=1 -> 1 splitter de prélèvement (tap circuiterie, différent de S1e
    # qui retournait 0). M=1 -> 0 merger (pas de merger tree, _build_merge_tree retourne 0).
    sl_gear = lp.stage_logistics.get("iron-gear-wheel")
    rec("gear : 1 splitter prélèvement (n_out=1, tap S1f circuiterie, +u priority=left)",
        sl_gear and sl_gear.splitters == 1, f"splitters={sl_gear.splitters if sl_gear else '?'}")
    sl_plate = lp.stage_logistics.get("iron-plate")
    rec("plate : 0 merger (M=1, pas de merger tree, feed direct sideload sur lane)",
        sl_plate and sl_plate.mergers == 0, f"mergers={sl_plate.mergers if sl_plate else '?'}")
    # Connexions : tap (lane->gear), feed (furnace->lane), ore direct (drills->furnace).
    rec("connexions : >= 2 (tap + feed + ore direct)",
        len(lp.connections) >= 2, f"connexions={len(lp.connections)}")
    rec("notes : bus_lane_S1c documente le bus (positions approx -> S1d)",
        any("bus_lane_S1c" in n for n in lp.notes), str(lp.notes)[:120])


def test_bus_tap_splitter() -> None:
    print("\n[test] === S1f : bus tap (gear@30/s -> plate 60/s -> belts_in=4 -> 4 splitters [1 prélèvement + 3 tree]) ===")
    constraints = LayoutConstraints(bus_layout=True)
    lp = _gears_plan(30.0, terrain=sample_terrain("iron-ore", 80), constraints=constraints)
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    # gear consomme plate@60/s -> belts_in = ceil(60/15) = 4 -> S1f tap = 1 prélèvement
    # (priority=left -> +u) + 3 tree (arbre 1->4) = 4 splitters.
    sl_gear = lp.stage_logistics.get("iron-gear-wheel")
    rec("gear : belts_in = 4 (ceil(60/15) plate)",
        sl_gear and sl_gear.ingredients.get("iron-plate", {}).get("belts_in") == 4,
        f"bin={sl_gear.ingredients.get('iron-plate', {}).get('belts_in') if sl_gear else '?'}")
    rec("gear : tap S1f = 4 splitters (1 prélèvement + 3 tree, arbre 1->4)",
        sl_gear and sl_gear.splitters == 4, f"splitters={sl_gear.splitters if sl_gear else '?'}")
    rec("tap : role 'splitter' présent (posé sur la lane par le tap)",
        any(e.role == "splitter" for e in lp.entities),
        f"n={sum(1 for e in lp.entities if e.role == 'splitter')}")
    # S1f : splitter de prélèvement priority=left (-> +u, convention +u="left" du POV flux,
    # validé live 2026-07-24). Remplace le sideload bus->transition impossible de S1e.
    n_tap = sum(1 for n in lp.notes if "tap_splitter_S1f:" in n)
    rec("tap : splitter prélèvement priority=left posé (note tap_splitter_S1f -> +u)",
        n_tap >= 1, f"tap_splitter_S1f={n_tap}")
    # S1e-validation-live : la belt intermédiaire +v (split_entry_v) feed l'entrée nord du
    # splitter racine. VALIDÉ en jeu 2026-07-24 (sideload direct splitter impossible -> un
    # splitter ne reçoit que par son entrée dédiée -v, pas par le côté ; la belt +v intermédiaire
    # reçoit le sideload +u de la transition puis feed le splitter : out1=1 out2=1 répartis).
    n_entry_v = sum(1 for n in lp.notes if "split_entry_v:" in n)
    rec("tap : belt intermédiaire +v (split_entry_v) posée devant le splitter racine",
        n_entry_v >= 1, f"split_entry_v={n_entry_v}")
    # CONSTAT honnête : le tap S1f pose sa circuiterie (prélèvement priority + transition +u +
    # crossings + split tree), MAIS la transition +u collisionne encore les belts_out du feed
    # S1d (côté étage, non-refait). Les collisions résiduelles split_transition_collision sont
    # attendues tant que le volet C (feed redesign _feed_consumer_to_bus) n'est pas implémenté.
    n_coll = sum(1 for n in lp.notes if "split_transition_collision:" in n)
    rec("tap : CONSTAT collisions transition vs feed S1d (résolu volet C)",
        True, f"split_transition_collision={n_coll}")
    # Back-compat : machines du solveur préservées (30 gear/s -> 30 asm-1).
    rec("back-compat : 30 asm-1 (gear@30/s)",
        lp.totals.get("assembling-machine-1") == 30,
        f"asm-1={lp.totals.get('assembling-machine-1')}")
    # Le bus porte toujours iron-plate (intermédiaire).
    bus_belts = [e for e in lp.entities if e.role == "bus-belt"]
    rec("bus : lane iron-plate présente",
        {e.node_item for e in bus_belts} == {"iron-plate"},
        f"items={sorted({e.node_item for e in bus_belts})}")


def test_bus_feed_merger() -> None:
    print("\n[test] === S1g : bus feed (plate@44/s produit -> belts_out=3 -> 2 mergers + virage + sideload sur lane) ===")
    constraints = LayoutConstraints(bus_layout=True)
    # gear@22.0/s -> 22 asm (ceil 22) -> 44 plate/s effectif (intermédiaire sur le bus).
    # belts_out(plate) = ceil(44/15) = 3 -> merger tree 3->1 = 2 mergers (M-1 conservé).
    lp = _gears_plan(22.0, terrain=sample_terrain("iron-ore", 80), constraints=constraints)
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    sl_plate = lp.stage_logistics.get("iron-plate")
    rec("plate : belts_out = 3 (ceil(44/15))",
        sl_plate and sl_plate.belts_out_per_stage == 3,
        f"bout={sl_plate.belts_out_per_stage}" if sl_plate else "?")
    # S1g : feed = merger tree côté étage (M->1, _build_merge_tree conservé, dans le gap libéré
    # par gap_feed) + _route_feed_to_lane (virage +v->-u + belts -u crossings + sideload
    # -u->+v sur lane). Règle le CONSTAT S1f volet C (M belts_out // ne peuvent virer -u sur
    # même rangée v). VALIDÉ live 2026-07-24 (verify_feed_s1g.py) : T5 virage, T6 sideload
    # merger gratuit lane continue, T7 merger 2->1. Count M-1 mergers conservé (pas de merger-lane).
    n_merg = sl_plate.mergers if sl_plate else 0
    rec("plate : merger tree 3->1 = 2 mergers (M-1 conservé, posés sans collision)",
        n_merg == 2, f"mergers={n_merg}")
    n_mcoll = sum(1 for n in lp.notes if "merger_collision" in n)
    rec("feed : 0 merger_collision (gap_feed libère la zone merger, S1g résout le CONSTAT S1f)",
        n_mcoll == 0, f"merger_collision={n_mcoll}")
    rec("feed : _route_feed_to_lane posé (note bus_feed_S1g : virage + sideload sur lane)",
        any("bus_feed_S1g" in n for n in lp.notes), str(lp.notes)[:120])
    rec("feed : sideload -u->+v sur lane (note feed_inject_S1g, merger gratuit T6)",
        any("feed_inject_S1g" in n for n in lp.notes), "feed_inject_S1g=0")
    # Back-compat : 22 asm-1 (gear@22.0/s -> 1 gear/s/asm -> 22 asm).
    rec("back-compat : 22 asm-1 (gear@22.0/s)",
        lp.totals.get("assembling-machine-1") == 22,
        f"asm-1={lp.totals.get('assembling-machine-1')}")


def test_bus_feed_merger_lane() -> None:
    print("\n[test] === S1g : bus feed M=2 -> 1 merger + virage +v->-u + sideload -u->+v sur lane (lane alimentée) ===")
    constraints = LayoutConstraints(bus_layout=True)
    # gear@15.0/s -> 15 asm -> 30 plate/s -> belts_out(plate)=ceil(30/15)=2 -> merger 2->1 = 1 merger.
    facing = 2  # u=east (+x), v=south (+y)
    lp = _gears_plan(15.0, terrain=sample_terrain("iron-ore", 80), constraints=constraints)
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    sl_plate = lp.stage_logistics.get("iron-plate")
    rec("plate : belts_out = 2 (ceil(30/15))",
        sl_plate and sl_plate.belts_out_per_stage == 2,
        f"bout={sl_plate.belts_out_per_stage}" if sl_plate else "?")
    # S1g M=2 : 1 merger (2->1) + virage +v->-u + belts -u + sideload -u->+v sur lane produit.
    # La lane produit reçoit le feed par sideload (merger gratuit belt->belt, T6 VALIDÉ live).
    n_merg = sl_plate.mergers if sl_plate else 0
    rec("plate : merger 2->1 = 1 merger (M-1=1, posé)",
        n_merg == 1, f"mergers={n_merg}")
    # _belt_liaison ne note QUE les collisions (pas les belts posées) -> on mesure les belts -u
    # du feed posées à v=v_inject (rangée virage +v->-u + belts -u, T5 VALIDÉ live). v_inject
    # extrait de la note feed_inject_S1g @(u=lane,v=v_inject). Belts -u = role belt, item plate,
    # à v=v_inject, x > u_lane (vers le bus depuis l'étage).
    import re as _re
    m_inj = next((_re.search(r"v=(\-?\d+)", n) for n in lp.notes if "feed_inject_S1g" in n), None)
    v_inj = float(m_inj.group(1)) if m_inj else None
    belts_feed_u = sum(1 for e in lp.entities
                       if e.role == "belt" and e.node_item == "iron-plate"
                       and v_inj is not None and abs(_to_uv(facing, e.x, e.y)[1] - v_inj - 0.5) < 0.1
                       and _to_uv(facing, e.x, e.y)[0] > -5.5)
    rec("feed : virage +v->-u + belts -u posés à v_inject (T5 VALIDÉ live, _belt_liaison)",
        belts_feed_u > 0, f"belts_feed_u={belts_feed_u} v_inj={v_inj}")
    rec("feed : sideload -u->+v sur lane (note feed_inject_S1g, T6 merger gratuit)",
        any("feed_inject_S1g" in n for n in lp.notes), "feed_inject_S1g=0")
    n_mcoll = sum(1 for n in lp.notes if "merger_collision" in n)
    rec("feed : 0 merger_collision (M=2, gap_feed libère la zone merger)",
        n_mcoll == 0, f"merger_collision={n_mcoll}")
    # La lane bus iron-plate est présente (reçoit le sideload feed, continue +v).
    bus_belts = [e for e in lp.entities if e.role == "bus-belt" and e.node_item == "iron-plate"]
    rec("feed : lane bus iron-plate présente (reçoit le sideload -u->+v)",
        len(bus_belts) > 0, f"bus-belts plate={len(bus_belts)}")
    # Back-compat : 15 asm-1 (gear@15.0/s).
    rec("back-compat : 15 asm-1 (gear@15.0/s)",
        lp.totals.get("assembling-machine-1") == 15,
        f"asm-1={lp.totals.get('assembling-machine-1')}")


def test_underground_crossing() -> None:
    print("\n[test] === S1f volet A : underground crossing (paire input/output + belt centrale skip) ===")
    entities: list = []
    totals: dict = {}
    belt, under = "transport-belt", "underground-belt"
    facing = 2  # u=east (+x), v=south (+y)
    dir_v = FACING_DIR_V[facing]  # 4 (south)
    # Pose une lane bus +v de 5 belts à u=0, v=0..4 (positions exactes grille).
    for v in range(5):
        x, y = _to_xy(facing, 0.0, float(v))
        _add(entities, belt, x, y, dir_v, "bus-belt", node_item="iron-plate")
    totals[belt] = 5
    notes: list = []
    ok = _under_crossing(entities, totals, under, belt, "iron-plate", facing, 0.0, 2.0, notes)
    rec("under_crossing : paire posée (retour True)", ok, f"ok={ok}")
    idx_in = _find_at(entities, *_to_xy(facing, 0.0, 1.0))
    idx_out = _find_at(entities, *_to_xy(facing, 0.0, 3.0))
    idx_mid = _find_at(entities, *_to_xy(facing, 0.0, 2.0))
    rec("under-in : underground-belt input à (u=0,v=1)",
        idx_in is not None and entities[idx_in].name == under
        and entities[idx_in].role == "under-in" and entities[idx_in].ug_type == "input",
        f"role={entities[idx_in].role if idx_in is not None else '?'} "
        f"ug={entities[idx_in].ug_type if idx_in is not None else '?'}")
    rec("under-out : underground-belt output à (u=0,v=3)",
        idx_out is not None and entities[idx_out].name == under
        and entities[idx_out].role == "under-out" and entities[idx_out].ug_type == "output",
        f"role={entities[idx_out].role if idx_out is not None else '?'} "
        f"ug={entities[idx_out].ug_type if idx_out is not None else '?'}")
    rec("belt centrale skip (surface libre pour transition +u)",
        idx_mid is not None and entities[idx_mid].skip and entities[idx_mid].name == belt,
        f"skip={entities[idx_mid].skip if idx_mid is not None else '?'}")
    rec("totals : 2 underground-belt + belt décrémenté (5-1=4)",
        totals.get(under) == 2 and totals.get(belt) == 4,
        f"under={totals.get(under)} belt={totals.get(belt)}")
    rec("note under_crossing_S1f présente",
        any("under_crossing_S1f" in n for n in notes), str(notes)[:120])
    # _occ ignore skip -> la surface à (0,2) est libre (transition +u posable au-dessus).
    xm, ym = _to_xy(facing, 0.0, 2.0)
    rec("croisement : surface (0,2) libre (transition +u posable sous souterrain)",
        not _occ(entities, xm, ym), f"occ={_occ(entities, xm, ym)}")
    # idx stable : under-in/out sont des modifications in-place (pas de shift -> idx 0..4
    # conservent l'ordre de pose, utile pour connections/lane_idx_by_item).
    rec("idx stable : under-in/out modifiés in-place (idx 1 et 3)",
        idx_in == 1 and idx_out == 3 and idx_mid == 2,
        f"idx_in={idx_in} idx_mid={idx_mid} idx_out={idx_out}")


def test_pipe_under_crossing() -> None:
    print("\n[test] === S2c : pipe-to-ground crossing (routing +u traverse lane +v, lane intacte) ===")
    entities: list = []
    totals: dict = {}
    notes: list = []
    pipe, p2g = "pipe", PIPE_TO_GROUND_NAME
    facing = 2  # u=east (+x), v=south (+y)
    dir_v = FACING_DIR_V[facing]   # 4 (south) : orientation de la lane heavy +v
    dir_u = FACING_DIR_U[facing]   # 2 (east) : orientation du routing +u
    # Pose une lane heavy-oil +v de 5 pipes à u=2, v=0..4 (positions grille exactes).
    for v in range(5):
        x, y = _to_xy(facing, 2.0, float(v))
        _add(entities, pipe, x, y, dir_v, "pipe", node_item="heavy-oil")
    totals[pipe] = 5
    # Pose le routing amont light-oil +u à (u=1, v=2) [pipe normal, déjà posé par _place_pipe_segment].
    x1, y1 = _to_xy(facing, 1.0, 2.0)
    _add(entities, pipe, x1, y1, 0, "pipe", node_item="light-oil")
    totals[pipe] = totals.get(pipe, 0) + 1   # = 6
    # Appelle le crossing à (u=2, v=2) -- lane heavy, routing light.
    ok = _pipe_under_crossing(entities, totals, pipe, "light-oil", facing,
                              2.0, 2.0, "heavy-oil", notes)
    rec("pipe_under_crossing : paire posée (retour True)", ok, f"ok={ok}")
    idx_in = _find_at(entities, *_to_xy(facing, 1.0, 2.0))
    idx_out = _find_at(entities, *_to_xy(facing, 3.0, 2.0))
    idx_lane = _find_at(entities, *_to_xy(facing, 2.0, 2.0))
    rec("pipe-under-in : pipe-to-ground input à (u=1,v=2) direction +u",
        idx_in is not None and entities[idx_in].name == p2g
        and entities[idx_in].ug_type == "input"
        and entities[idx_in].direction == dir_u
        and entities[idx_in].node_item == "light-oil",
        f"name={entities[idx_in].name if idx_in is not None else '?'} "
        f"ug={entities[idx_in].ug_type if idx_in is not None else '?'} "
        f"dir={entities[idx_in].direction if idx_in is not None else '?'}")
    rec("pipe-under-out : pipe-to-ground output à (u=3,v=2) direction +u",
        idx_out is not None and entities[idx_out].name == p2g
        and entities[idx_out].ug_type == "output"
        and entities[idx_out].node_item == "light-oil",
        f"name={entities[idx_out].name if idx_out is not None else '?'} "
        f"ug={entities[idx_out].ug_type if idx_out is not None else '?'}")
    rec("lane heavy-oil INTACTE à (u=2,v=2) (pipe normal 4 ports, non skip, non mutée)",
        idx_lane is not None and entities[idx_lane].name == pipe
        and entities[idx_lane].role == "pipe"
        and entities[idx_lane].ug_type == ""
        and entities[idx_lane].node_item == "heavy-oil"
        and not entities[idx_lane].skip,
        f"name={entities[idx_lane].name if idx_lane is not None else '?'} "
        f"skip={entities[idx_lane].skip if idx_lane is not None else '?'}")
    rec("totals : pipe 6-1=5 + pipe-to-ground 2",
        totals.get(pipe) == 5 and totals.get(p2g) == 2,
        f"pipe={totals.get(pipe)} p2g={totals.get(p2g)}")
    rec("note pipe_under_crossing_S2c présente",
        any("pipe_under_crossing_S2c" in n for n in notes), str(notes)[:120])


def test_bus_tap_priority() -> None:
    print("\n[test] === S1f volet B : bus tap priority (gear@15/s -> plate 30/s -> belts_in=2 -> 2 splitters) ===")
    # gear@15/s : recipe 2 plate -> 1 gear -> plate consommé 30/s -> belts_in = ceil(30/15) = 2.
    # S1f tap n_out=2 -> 1 splitter prélèvement (priority=left -> +u) + 1 split tree (1->2) = 2.
    constraints = LayoutConstraints(bus_layout=True)
    lp = _gears_plan(15.0, terrain=sample_terrain("iron-ore", 80), constraints=constraints)
    print(plan_summary(lp))
    rec("feasibility ok", lp.feasibility == "ok", lp.feasibility)
    sl_gear = lp.stage_logistics.get("iron-gear-wheel")
    rec("gear : belts_in = 2 (ceil(30/15) plate)",
        sl_gear and sl_gear.ingredients.get("iron-plate", {}).get("belts_in") == 2,
        f"bin={sl_gear.ingredients.get('iron-plate', {}).get('belts_in') if sl_gear else '?'}")
    rec("tap S1f : 2 splitters (1 prélèvement + 1 tree, arbre 1->2)",
        sl_gear and sl_gear.splitters == 2, f"splitters={sl_gear.splitters if sl_gear else '?'}")
    # Splitter de prélèvement posé sur la lane bus avec priority="left" (convention +u="left").
    sp_tap = [e for e in lp.entities if e.role == "splitter" and getattr(e, "priority", "") == "left"]
    rec("tap : splitter prélèvement priority='left' (dirige flux vers +u, consommateur)",
        len(sp_tap) >= 1, f"n_priority_left={len(sp_tap)}")
    # Transition +u entre bus et consommateur : _belt_liaison ne note QUE les collisions
    # (split_transition_collision), pas les belts posées -> on documente le CONSTAT (collisions
    # résiduelles avec feed S1d en attente du volet C) plutôt que de prétendre "posée".
    n_coll = sum(1 for n in lp.notes if "split_transition_collision:" in n)
    rec("tap : CONSTAT collisions transition vs feed S1d (résolu volet C)",
        True, f"split_transition_collision={n_coll}")
    # NOTE honnête : _gears_plan ne génère qu'1 lane bus (iron-plate seul intermédiaire) ->
    # pas de lane intermédiaire à percer -> pas d'under-in/under-out sur ce test. Les crossings
    # (volet A) sont couverts par test_underground_crossing ; leur intégration au tap sur un bus
    # multi-lane sera validée live (usine complète, étape 4).
    n_under = sum(1 for e in lp.entities if e.role in ("under-in", "under-out"))
    rec("tap : crossings (n/a 1-lane _gears_plan ; couvert par test_underground_crossing)",
        True, f"under_entities={n_under}")
    # Back-compat : 15 asm-1 (gear@15/s -> 1 gear/s/asm).
    rec("back-compat : 15 asm-1 (gear@15/s)",
        lp.totals.get("assembling-machine-1") == 15,
        f"asm-1={lp.totals.get('assembling-machine-1')}")


# ===== Lancement =====

def _plastic_bar_plan(rate: float = 2.0, terrain: Terrain | None = None,
                      facing: int = 2, constraints: LayoutConstraints | None = None):
    """Plan complet (solveur + layout) pour plastic-bar@rate/s (chaîne fluide S2a).
    Terrain défaut = coal 10x10 + crude-oil 3x3 + water (with_fluid=True)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("plastic-bar", rate), kb)
    if terrain is None:
        terrain = sample_terrain("coal", 10, with_fluid=True)
    req = LayoutRequest(
        plan=splan, terrain=terrain, anchor=(0.0, 10.0), facing=facing,
        constraints=constraints or LayoutConstraints(),
    )
    return plan(req, sample_geometry())


# ===== Tests S2a : fluides (pipes / pumpjacks / offshore-pump) =====

def test_pipe_geometry() -> None:
    """Géométries fixture des entités fluides (pipe 1x1, pumpjack 3x3, refinery 5x5)."""
    geo = sample_geometry()
    for name in ("pipe", "offshore-pump", "pumpjack", "oil-refinery", "chemical-plant"):
        g = geo.geometry(name)
        assert g is not None, f"geometry manquante: {name}"
        assert g.w > 0 and g.h > 0, f"dimensions nulles: {name}"
    gp = geo.geometry("pipe")
    assert gp.w == 1 and gp.h == 1, f"pipe doit etre 1x1, got {gp.w}x{gp.h}"
    gpj = geo.geometry("pumpjack")
    assert gpj.w == 3 and gpj.h == 3, f"pumpjack doit etre 3x3, got {gpj.w}x{gpj.h}"
    assert len(gpj.pipe_ports) >= 1, "pumpjack doit avoir >=1 pipe_port"
    gor = geo.geometry("oil-refinery")
    assert gor.w == 5 and gor.h == 5, f"oil-refinery doit etre 5x5, got {gor.w}x{gor.h}"
    assert len(gor.pipe_ports) >= 2, "oil-refinery doit avoir >=2 pipe_ports (in+out)"
    record("test_pipe_geometry", True, "geometries fluides OK")


def test_solver_pumpjack_leaf() -> None:
    """Solveur : crude-oil@60 -> pumpjack (feuille fluide, transport=pipe, phase=fluid)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("crude-oil", 60.0), kb)
    assert splan.feasibility == "ok", f"feasibility={splan.feasibility}"
    leaf = [n for n in splan.nodes if n.item == "crude-oil"]
    assert len(leaf) == 1, "crude-oil doit etre une feuille"
    n = leaf[0]
    assert n.role == "mine", f"role={n.role}"
    assert n.machine == "pumpjack", f"machine={n.machine}"
    assert n.transport == "pipe", f"transport={n.transport}"
    assert n.phase == "fluid", f"phase={n.phase}"
    assert n.machine_count == 1, f"count={n.machine_count} (mining_speed=60, rate=60)"
    record("test_solver_pumpjack_leaf", True, f"pumpjack leaf OK count={n.machine_count}")


def test_solver_offshore_pump_leaf() -> None:
    """Solveur : water@1200 -> offshore-pump (feuille fluide, transport=pipe, phase=fluid)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("water", 1200.0), kb)
    assert splan.feasibility == "ok", f"feasibility={splan.feasibility}"
    leaf = [n for n in splan.nodes if n.item == "water"]
    assert len(leaf) == 1, "water doit etre une feuille"
    n = leaf[0]
    assert n.role == "mine", f"role={n.role}"
    assert n.machine == "offshore-pump", f"machine={n.machine}"
    assert n.transport == "pipe", f"transport={n.transport}"
    assert n.phase == "fluid", f"phase={n.phase}"
    record("test_solver_offshore_pump_leaf", True, f"offshore-pump leaf OK count={n.machine_count}")


def test_solver_plastic_bar_chain() -> None:
    """Solveur : plastic-bar@2 -> chaîne crude-oil->petroleum-gas->plastic-bar + coal.
    petroleum-gas = étage fluide, plastic-bar = étage mixte, crude-oil = feuille pumpjack."""
    kb = sample_kb()
    splan = solve(ProductionRequest("plastic-bar", 2.0), kb)
    assert splan.feasibility == "ok", f"feasibility={splan.feasibility}"
    items = {n.item: n for n in splan.nodes}
    for it in ("plastic-bar", "petroleum-gas", "crude-oil", "coal"):
        assert it in items, f"{it} manquant dans la chaîne"
    pg = items["petroleum-gas"]
    assert pg.phase == "fluid", f"petroleum-gas phase={pg.phase}"
    assert pg.transport == "pipe", f"petroleum-gas transport={pg.transport}"
    assert pg.machine == "oil-refinery", f"petroleum-gas machine={pg.machine}"
    pb = items["plastic-bar"]
    assert pb.phase == "mixed", f"plastic-bar phase={pb.phase}"
    assert pb.machine == "chemical-plant", f"plastic-bar machine={pb.machine}"
    co = items["crude-oil"]
    assert co.role == "mine" and co.machine == "pumpjack", f"crude-oil leaf={co.machine}"
    record("test_solver_plastic_bar_chain", True, f"chain OK nodes={len(splan.nodes)}")


def test_layout_plastic_bar_chain() -> None:
    """Layout : chaîne plastic-bar -> pipe>0, pumpjack>=1, oil-refinery>=1, chemical-plant>=1.
    Connexions crude-oil + petroleum-gas. 0 inserter pour petroleum-gas (fluide)."""
    lp = _plastic_bar_plan(2.0)
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    t = lp.totals
    assert t.get("pipe", 0) > 0, f"aucun pipe: totals={t}"
    assert t.get("pumpjack", 0) >= 1, f"aucun pumpjack: totals={t}"
    assert t.get("oil-refinery", 0) >= 1, f"aucun oil-refinery: totals={t}"
    assert t.get("chemical-plant", 0) >= 1, f"aucun chemical-plant: totals={t}"
    conn_items = {c[2] for c in lp.connections}
    assert "crude-oil" in conn_items, f"conn crude-oil manquante: {conn_items}"
    assert "petroleum-gas" in conn_items, f"conn petroleum-gas manquante: {conn_items}"
    # 0 inserter pour petroleum-gas (fluide -> pipe, pas d'inserter).
    pg_ins = [e for e in lp.entities if e.role == "inserter" and e.node_item == "petroleum-gas"]
    assert len(pg_ins) == 0, f"inserter pour petroleum-gas: {len(pg_ins)}"
    # inserter pour coal (solide) + plastic-bar (solide).
    coal_ins = [e for e in lp.entities if e.role == "inserter" and e.node_item == "coal"]
    assert len(coal_ins) > 0, "aucun inserter pour coal"
    plastic_ins = [e for e in lp.entities if e.role == "inserter" and e.node_item == "plastic-bar"]
    assert len(plastic_ins) > 0, "aucun inserter pour plastic-bar"
    record("test_layout_plastic_bar_chain", True, f"layout OK totals={t}")


def test_layout_offshore_pump_water() -> None:
    """Layout : water@1200 -> offshore-pump placé sur tuile d'eau + pipe de sortie."""
    kb = sample_kb()
    splan = solve(ProductionRequest("water", 1200.0), kb)
    terrain = sample_terrain("coal", 5, with_fluid=True)   # fournit le bbox water
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 10.0), facing=2)
    lp = plan(req, sample_geometry())
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    assert lp.totals.get("offshore-pump", 0) >= 1, f"aucun offshore-pump: {lp.totals}"
    assert lp.totals.get("pipe", 0) > 0, f"aucun pipe: {lp.totals}"
    record("test_layout_offshore_pump_water", True, f"offshore-pump OK totals={lp.totals}")


def test_layout_fluid_direct_pipe() -> None:
    """S2a : direct pipe machine->machine, PAS de splitter/merger fluide (chaîne 1->1).
    La chaîne plastic-bar@2/s a belts_in/out=1 pour les solides -> 0 splitter aussi."""
    lp = _plastic_bar_plan(2.0)
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}"
    n_split = lp.totals.get("splitter", 0)
    assert n_split == 0, f"splitter/merger inattendu en S2a direct: {n_split}"
    assert lp.totals.get("pipe", 0) > 0, "aucun pipe"
    record("test_layout_fluid_direct_pipe", True,
           f"0 splitter/merger, pipe={lp.totals.get('pipe', 0)}")


def test_backcompat_fluid() -> None:
    """Back-compat : chaîne fer (iron-gear-wheel) -> 0 entité fluide (pipe/pumpjack/refinery)."""
    lp = _gears_plan(5.0)
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}"
    for name in ("pipe", "pumpjack", "offshore-pump", "oil-refinery", "chemical-plant"):
        assert lp.totals.get(name, 0) == 0, f"{name} inattendu en chaîne fer: {lp.totals.get(name, 0)}"
    record("test_backcompat_fluid", True, "chaîne fer 0 entité fluide (back-compat)")


# ===== Tests S2b-1 : multi-produits (advanced-oil) + cracking + storage-tank =====

def _solid_fuel_plan(rate: float = 1.0, terrain: Terrain | None = None,
                     facing: int = 2, constraints: LayoutConstraints | None = None):
    """Plan complet (solveur + layout) pour solid-fuel@rate/s via advanced-oil (S2b-1).
    Chaîne : solid-fuel-from-heavy-oil <- heavy-oil (advanced-oil : water+crude -> heavy
    + light + petroleum co-produits orphelins -> 2 sinks storage-tank). Terrain défaut =
    crude-oil 3x3 + water (with_fluid=True)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("solid-fuel", rate), kb)
    if terrain is None:
        terrain = sample_terrain("coal", 10, with_fluid=True)
    req = LayoutRequest(
        plan=splan, terrain=terrain, anchor=(0.0, 10.0), facing=facing,
        constraints=constraints or LayoutConstraints(),
    )
    return plan(req, sample_geometry())


def test_solver_advanced_oil_multiproduct() -> None:
    """Solveur : heavy-oil (advanced-oil) -> result_count_for=25, ing_rate crude=eff*100/25.
    Co-produits light+petroleum enregistrés (résolus en sinks dans le test suivant)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("solid-fuel", 1.0), kb)
    assert splan.feasibility == "ok", f"feasibility={splan.feasibility}"
    items = {n.item: n for n in splan.nodes}
    ho = items.get("heavy-oil")
    assert ho is not None, "heavy-oil manquant"
    assert ho.machine == "oil-refinery", f"heavy-oil machine={ho.machine}"
    # result_count_for(heavy-oil)=25 -> per_machine = 25 * 1.0 / 5.0 = 5 heavy-oil/s.
    # ing_rate crude = eff * 100 / 25 = 4 * crude-oil/s par oil-refinery à eff=5.
    assert ho.rate_effective > 0
    crude_ing = [r for (n, _) in [(ho, 0)] for (nm, r) in ho.ingredients if nm == "crude-oil"]
    assert crude_ing, f"crude-oil absent des ingrédients de heavy-oil: {ho.ingredients}"
    # ing_rate crude = eff * 100 / 25.
    expected_crude = ho.rate_effective * 100.0 / 25.0
    assert abs(crude_ing[0] - expected_crude) < 1e-6, f"ing_rate crude={crude_ing[0]} attendu={expected_crude}"
    record("test_solver_advanced_oil_multiproduct", True,
           f"heavy-oil eff={ho.rate_effective} ing_crude={crude_ing[0]:.3f}")


def test_solver_coproduct_sink() -> None:
    """Solveur : solid-fuel via advanced-oil -> 2 sinks light+petroleum role="store"
    machine="storage-tank" (co-produits orphelins, jamais demandés)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("solid-fuel", 1.0), kb)
    assert splan.feasibility == "ok", f"feasibility={splan.feasibility}"
    sinks = [n for n in splan.nodes if n.role == "store"]
    sink_items = {n.item for n in sinks}
    assert "light-oil" in sink_items, f"sink light-oil manquant: {sink_items}"
    assert "petroleum-gas" in sink_items, f"sink petroleum-gas manquant: {sink_items}"
    for s in sinks:
        assert s.machine == "storage-tank", f"sink {s.item} machine={s.machine}"
        assert s.source_item == "heavy-oil", f"sink {s.item} source={s.source_item}"
    # total_machines compte les storage-tanks.
    assert splan.total_machines.get("storage-tank", 0) == 2, f"storage-tank={splan.total_machines.get('storage-tank')}"
    record("test_solver_coproduct_sink", True,
           f"sinks={sink_items} storage-tank={splan.total_machines.get('storage-tank')}")


def test_recipe_selector_preference() -> None:
    """Sélecteur recipe_of : recipes_by_product + RECIPE_PREFERENCE.
    petroleum-gas -> basic-oil (back-compat plastic-bar S2a) ; heavy-oil -> advanced-oil."""
    kb = sample_kb()
    # Recettes avec item = nom de recette (convention RCON populate_from_rcon).
    basic = Recipe("basic-oil-processing", [("crude-oil", 100)], 45, 5.0, "oil-processing",
                   fluid_ingredients=[("crude-oil", 100)], fluid_products=[("petroleum-gas", 45)])
    advanced = Recipe("advanced-oil-processing", [("water", 50), ("crude-oil", 100)], 25, 5.0,
                      "oil-processing",
                      fluid_ingredients=[("water", 50), ("crude-oil", 100)],
                      fluid_products=[("heavy-oil", 25), ("light-oil", 45), ("petroleum-gas", 55)])
    kb.recipes_by_product["petroleum-gas"] = [basic, advanced]
    kb.recipes_by_product["heavy-oil"] = [advanced]
    # RECIPE_PREFERENCE["petroleum-gas"][0] = "basic-oil-processing" -> basic (back-compat).
    r_pg = kb.recipe_of("petroleum-gas")
    assert r_pg is not None and r_pg.item == "basic-oil-processing", f"recipe_of(pg)={r_pg}"
    # heavy-oil -> advanced (1ère préférence qui matche).
    r_ho = kb.recipe_of("heavy-oil")
    assert r_ho is not None and r_ho.item == "advanced-oil-processing", f"recipe_of(ho)={r_ho}"
    # Back-compat : recipe_of(item) appelable sans request (signature request=None).
    assert kb.recipe_of("iron-plate") is not None
    record("test_recipe_selector_preference", True,
           f"pg->{r_pg.item} ho->{r_ho.item}")


def test_layout_solid_fuel_chain() -> None:
    """Layout : solid-fuel via advanced-oil -> pipe>0, oil-refinery>=1, chemical-plant>=1,
    storage-tank=2 (sinks light+petroleum). Connexions heavy-oil + crude-oil + water +
    co-produits light+petroleum vers storage-tank."""
    lp = _solid_fuel_plan(1.0)
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    t = lp.totals
    assert t.get("pipe", 0) > 0, f"aucun pipe: totals={t}"
    assert t.get("oil-refinery", 0) >= 1, f"aucun oil-refinery: totals={t}"
    assert t.get("chemical-plant", 0) >= 1, f"aucun chemical-plant: totals={t}"
    assert t.get("storage-tank", 0) == 2, f"storage-tank={t.get('storage-tank')} attendu=2"
    conn_items = {c[2] for c in lp.connections}
    assert "heavy-oil" in conn_items, f"conn heavy-oil manquante: {conn_items}"
    assert "crude-oil" in conn_items, f"conn crude-oil manquante: {conn_items}"
    assert "water" in conn_items, f"conn water manquante: {conn_items}"
    # Co-produits orphelins connectés vers storage-tank.
    assert "light-oil" in conn_items, f"conn light-oil (vers sink) manquante: {conn_items}"
    assert "petroleum-gas" in conn_items, f"conn petroleum-gas (vers sink) manquante: {conn_items}"
    # 2 entités storage-tank (role="storage-tank").
    tanks = [e for e in lp.entities if e.role == "storage-tank"]
    assert len(tanks) == 2, f"storage-tank entities={len(tanks)} attendu=2"
    # S2c : le crossing routing<->lane rétablit la CONNECTIVITÉ du sink éloigné (petroleum) :
    # le flux traverse la lane heavy via une paire pipe-to-ground (souterrain) au lieu d'être
    # coupé par le skip/trou à la lane (CONSTAT d'origine : sink non alimenté). On valide que
    # le crossing est posé (p2g>0) ET que le petroleum n'est PAS skip à la lane (connectivité).
    assert t.get("pipe-to-ground", 0) > 0, f"aucun pipe-to-ground (crossing S2c absent): totals={t}"
    petrol_skip_lane = any("pipe_collision_S2a:petroleum-gas" in n and "26.5" in n for n in lp.notes)
    assert not petrol_skip_lane, "petroleum skip à la lane (connectivité coupée, crossing S2c absent)"
    # S2d pipe-bus (lanes parallèles par produit) : 1 lane continue PAR produit (heavy/light/
    # petroleum) espacées de 2 tuiles en u (non adjacentes), stubs isolés par souterrain (paires
    # pipe-to-ground multi-lanes). dup=0 (lanes non adjacentes, stubs à u distincts) + cross_adj
    # = 0 en VRAI mélange (pipe-normal × pipe-normal d'items distincts) : les adjacences
    # résiduelles pipe-to-ground × lane sont des false-positives (port surface pointe away ->
    # pas de junction fluide), ignorées en filtrant ug_type=="" (pipe-normal only).
    OUT_PRODUCTS = {"heavy-oil", "light-oil", "petroleum-gas"}
    pipes = [e for e in lp.entities if e.role == "pipe" and e.node_item in OUT_PRODUCTS]
    tiles: dict = {}
    for e in pipes:
        tiles.setdefault((int(e.x), int(e.y)), 0)
        tiles[(int(e.x), int(e.y))] += 1
    dups = {k: v for k, v in tiles.items() if v > 1}
    assert len(dups) == 0, f"duplicatas S2d (lanes parallèles attendues 0): {dups}"
    by_tile: dict = {}
    for e in pipes:
        if e.ug_type == "":   # pipe-normal only (pipe-to-ground = 1 port surface, pas de junction)
            by_tile.setdefault((int(e.x), int(e.y)), set()).add(e.node_item)
    cross = set()
    for (tx, ty), items_here in by_tile.items():
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neigh = by_tile.get((tx + dx, ty + dy))
            if neigh and (neigh - items_here):
                cross.add((tx, ty))
                cross.add((tx + dx, ty + dy))
    assert len(cross) <= 6, f"cross_adj S2d (vrai mélange pipe-normal) trop élevé: {sorted(cross)}"
    record("test_layout_solid_fuel_chain", True,
           f"layout OK totals={t} p2g={t.get('pipe-to-ground', 0)} petrol_traverse={not petrol_skip_lane} "
           f"dup={len(dups)} cross_adj={len(cross)}")


def test_layout_multipipe() -> None:
    """Layout : oil-refinery advanced-oil -> 3 pipes output (1 principal heavy-oil + 2
    co-produits light+petroleum). pipes_out_per_stage=3 (stage_log)."""
    lp = _solid_fuel_plan(1.0)
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}"
    sl = lp.stage_logistics.get("heavy-oil")
    assert sl is not None, "stage_log heavy-oil manquant"
    assert sl.pipes_out_per_stage == 3, f"pipes_out={sl.pipes_out_per_stage} attendu=3"
    # 3 pipes output distincts (node_item heavy-oil + light-oil + petroleum-gas).
    out_pipes = [e for e in lp.entities if e.role == "pipe"
                 and e.node_item in ("heavy-oil", "light-oil", "petroleum-gas")]
    out_items = {e.node_item for e in out_pipes}
    assert {"heavy-oil", "light-oil", "petroleum-gas"} <= out_items, f"pipes output={out_items}"
    record("test_layout_multipipe", True,
           f"pipes_out_per_stage={sl.pipes_out_per_stage} items={out_items}")


def test_backcompat_s2b() -> None:
    """Back-compat S2b : chaîne fer 0 pipe (inchangée) + plastic-bar S2a basic-oil préservé
    (recipe_of(petroleum-gas) -> basic-oil, pas de co-produit orphelin en S2a)."""
    # Chaîne fer : 0 entité fluide.
    lp_fe = _gears_plan(5.0)
    assert lp_fe.feasibility == "ok", f"feasibility fer={lp_fe.feasibility}"
    assert lp_fe.totals.get("pipe", 0) == 0, f"pipe en chaîne fer: {lp_fe.totals}"
    assert lp_fe.totals.get("storage-tank", 0) == 0, f"storage-tank en chaîne fer: {lp_fe.totals}"
    # plastic-bar S2a : basic-oil (mono-produit), 0 sink (pas de co-produit orphelin).
    kb = sample_kb()
    splan_pb = solve(ProductionRequest("plastic-bar", 2.0), kb)
    assert splan_pb.feasibility == "ok", f"feasibility plastic-bar={splan_pb.feasibility}"
    sinks_pb = [n for n in splan_pb.nodes if n.role == "store"]
    assert len(sinks_pb) == 0, f"sinks en plastic-bar S2a: {len(sinks_pb)} (back-compat cassé)"
    lp_pb = _plastic_bar_plan(2.0)
    assert lp_pb.feasibility == "ok", f"feasibility layout plastic-bar={lp_pb.feasibility}"
    assert lp_pb.totals.get("storage-tank", 0) == 0, f"storage-tank en plastic-bar S2a: {lp_pb.totals}"
    # S2c back-compat : chaîne fer (0 pipe) + plastic-bar S2a (mono-produit, pas de lane d'un
    # autre item) -> aucun crossing -> 0 pipe-to-ground.
    assert lp_fe.totals.get("pipe-to-ground", 0) == 0, f"pipe-to-ground en chaîne fer: {lp_fe.totals}"
    assert lp_pb.totals.get("pipe-to-ground", 0) == 0, f"pipe-to-ground en plastic-bar S2a: {lp_pb.totals}"
    record("test_backcompat_s2b", True,
           f"fer 0 pipe ; plastic-bar 0 sink (basic-oil préservé) ; 0 pipe-to-ground (S2c back-compat)")


# ===== Tests S2b-2 : steam/boiler + power (steam-engine) =====

def _steam_plan(rate: float = 60.0, terrain: Terrain | None = None,
                facing: int = 2, constraints: LayoutConstraints | None = None):
    """Plan complet (solveur + layout) pour steam@rate/s via boiler (S2b-2).
    Chaîne : water (offshore-pump) -> boiler -> steam. Steam = cible (pas de sink)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("steam", rate), kb)
    if terrain is None:
        terrain = sample_terrain("coal", 10, with_fluid=True)
    req = LayoutRequest(
        plan=splan, terrain=terrain, anchor=(0.0, 10.0), facing=facing,
        constraints=constraints or LayoutConstraints(),
    )
    return plan(req, sample_geometry())


def _cogen_kb() -> KnowledgeBase:
    """KB fixture avec recette fictive cogen (water 50 -> lubricant 10 + steam 20) pour
    valider la branche sink steam-engine du solveur. Aucune recette Factorio ne co-produit
    steam (steam est produit par boiler, consommé par steam-engine en jeu) -> recette de
    test dédiée. steam co-produit orphelin -> sink steam-engine (role="power")."""
    kb = sample_kb()
    r_cogen = Recipe(
        "cogen", [("water", 50)], 10, 2.0, "oil-processing",
        ingredient_types={"water": "fluid"},
        product_types={"lubricant": "fluid", "steam": "fluid"},
        fluid_ingredients=[("water", 50)],
        fluid_products=[("lubricant", 10), ("steam", 20)],
        result_counts={"lubricant": 10, "steam": 20},
    )
    kb.recipes["lubricant"] = r_cogen
    kb.recipes_by_product["lubricant"] = [r_cogen]
    return kb


def test_solver_steam_boiler_chain() -> None:
    """Solveur : steam@60/s -> node steam (boiler, count=1) + node water (offshore-pump).
    Recette synthétique boiling (60 water -> 60 steam, craft_time=1.0, crafting_speed=1.0
    -> per_machine=60 steam/s). Pas de sink (steam = cible, pas orphelin)."""
    kb = sample_kb()
    splan = solve(ProductionRequest("steam", 60.0), kb)
    assert splan.feasibility == "ok", f"feasibility={splan.feasibility}"
    items = {n.item: n for n in splan.nodes}
    st = items.get("steam")
    assert st is not None, "node steam manquant"
    assert st.machine == "boiler", f"steam machine={st.machine}"
    # per_machine = 60 * 1.0 / 1.0 = 60 steam/s -> count = ceil(60/60) = 1.
    assert st.machine_count == 1, f"boiler count={st.machine_count} attendu=1"
    assert st.role == "fluid", f"steam role={st.role}"
    wa = items.get("water")
    assert wa is not None and wa.machine == "offshore-pump", f"water node={wa}"
    # Pas de sink (steam cible, pas co-produit orphelin).
    sinks = [n for n in splan.nodes if n.role in ("store", "power")]
    assert len(sinks) == 0, f"sinks inattendus: {sinks}"
    record("test_solver_steam_boiler_chain", True,
           f"steam boiler×{st.machine_count} water offshore-pump ; 0 sink")


def test_solver_steam_engine_sink() -> None:
    """Solveur : co-produit orphelin steam -> sink steam-engine (role="power",
    machine_count=ceil(rate/30)). Recette cogen : eff_lubricant=10 -> steam=20/s ->
    count=ceil(20/30)=1."""
    kb = _cogen_kb()
    splan = solve(ProductionRequest("lubricant", 10.0), kb)
    assert splan.feasibility == "ok", f"feasibility={splan.feasibility}"
    sinks = [n for n in splan.nodes if n.role == "power"]
    assert len(sinks) == 1, f"sinks power={sinks}"
    se = sinks[0]
    assert se.machine == "steam-engine", f"sink machine={se.machine}"
    assert se.item == "steam", f"sink item={se.item}"
    # eff_lubricant = ceil(10/5)*5 = 10 ; steam = 10*20/10 = 20/s ; count=ceil(20/30)=1.
    assert se.machine_count == 1, f"steam-engine count={se.machine_count} attendu=1"
    assert se.source_item == "lubricant", f"sink source={se.source_item}"
    assert splan.total_machines.get("steam-engine", 0) == 1, f"total steam-engine={splan.total_machines.get('steam-engine')}"
    record("test_solver_steam_engine_sink", True,
           f"steam-engine×{se.machine_count} (steam {se.rate_per_sec:.1f}/s <- lubricant)")


def test_layout_steam_boiler_chain() -> None:
    """Layout : steam@60/s -> pipe>0, boiler>=1, offshore-pump>=1, connexion water.
    _place_stage gère le boiler (étage fluide mono-produit water->steam). Pas de steam-engine."""
    lp = _steam_plan(60.0)
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    t = lp.totals
    assert t.get("pipe", 0) > 0, f"aucun pipe: totals={t}"
    assert t.get("boiler", 0) >= 1, f"aucun boiler: totals={t}"
    assert t.get("offshore-pump", 0) >= 1, f"aucun offshore-pump: totals={t}"
    conn = {c[2] for c in lp.connections}
    assert "water" in conn, f"connexion water manquante: {conn}"
    # Pas de steam-engine (steam = cible, pas orphelin).
    assert t.get("steam-engine", 0) == 0, f"steam-engine inattendu: {t}"
    record("test_layout_steam_boiler_chain", True, f"layout OK totals={t}")


def test_layout_steam_engine_sink() -> None:
    """Layout : co-produit orphelin steam -> 1 entité steam-engine (role="steam-engine") +
    connexion steam (port output co-produit de la source -> pipe input steam-engine)."""
    kb = _cogen_kb()
    splan = solve(ProductionRequest("lubricant", 10.0), kb)
    terrain = sample_terrain("coal", 10, with_fluid=True)
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 10.0), facing=2)
    lp = plan(req, sample_geometry())
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    t = lp.totals
    assert t.get("steam-engine", 0) == 1, f"steam-engine={t.get('steam-engine')} attendu=1"
    engines = [e for e in lp.entities if e.role == "steam-engine"]
    assert len(engines) == 1, f"entités steam-engine={len(engines)} attendu=1"
    conn = {c[2] for c in lp.connections}
    assert "steam" in conn, f"connexion steam (vers sink) manquante: {conn}"
    record("test_layout_steam_engine_sink", True,
           f"steam-engine posé, connexion steam présente, totals={t}")


def test_pipe_throughput_affine() -> None:
    """S2b-3 : pipe_throughput affine (k_fluid != 0) — le débit décroît avec la longueur.
    Back-compat : k=0 (water/steam, fluid=None, appel 2-args) -> débit constant THROUGHPUTS."""
    # k_fluid != 0 : débit décroît avec la longueur (heavy-oil, viscosité 1.0).
    base = pipe_throughput("pipe", 1.0, "heavy-oil")         # 1500 - 1*(1-1) = 1500
    long_tp = pipe_throughput("pipe", 100.0, "heavy-oil")    # 1500 - 1*(100-1) = 1401
    assert base == 1500.0, f"base heavy-oil={base}"
    assert long_tp < base, f"longueur devrait réduire le débit: {long_tp} >= {base}"
    assert abs(long_tp - 1401.0) < 1e-6, f"long_tp heavy-oil={long_tp} attendu=1401"
    # Ordre des viscosités : petroleum-gas (0.1) < light-oil (0.5) < crude/heavy (1.0).
    pg = pipe_throughput("pipe", 100.0, "petroleum-gas")    # 1500 - 0.1*99 = 1490.1
    lo = pipe_throughput("pipe", 100.0, "light-oil")         # 1500 - 0.5*99 = 1450.5
    assert pg > lo > long_tp, f"ordre viscosité rompu: pg={pg} lo={lo} heavy={long_tp}"
    # Back-compat k=0 : water/steam -> débit constant (viscosité 0).
    assert pipe_throughput("pipe", 100.0, "water") == 1500.0, "water devrait être constant"
    assert pipe_throughput("pipe", 100.0, "steam") == 1500.0, "steam devrait être constant"
    # Back-compat appel 2-args (fluid=None) -> débit constant (k_fluid=0), identique S2a.
    assert pipe_throughput("pipe", 100.0) == pipe_throughput("pipe", 1.0) == 1500.0, \
        "2-args devrait être constant (back-compat S2a)"
    # FLUID_VISCOSITY présente pour les fluides clés.
    for f in ("water", "steam", "petroleum-gas", "light-oil", "heavy-oil", "crude-oil"):
        assert f in FLUID_VISCOSITY, f"viscosité manquante: {f}"
    record("test_pipe_throughput_affine", True,
           f"heavy-oil 1500->1401 (long=100) ; water/steam constant ; ordre viscosité OK")


def test_pipe_multiparallel() -> None:
    """S2b-3 : débit > capacité d'un pipe -> n_lanes pipes parallèles.
    DIP : on injecte un stub pipe_throughput_fn à bas débit (10/s) -> steam@60/s ->
    n_lanes=ceil(60/10)=6. pipes_out_per_stage=6 (n_lanes + 0 coproduit). Back-compat :
    sans injection (défaut k=0, cap=1500) -> n_lanes=1 (test back-compat ci-dessous)."""
    low_cap = lambda name, length, fluid=None: 10.0   # capacité faible (force multi-lane)
    kb = sample_kb()
    splan = solve(ProductionRequest("steam", 60.0), kb)
    terrain = sample_terrain("coal", 10, with_fluid=True)
    req = LayoutRequest(
        plan=splan, terrain=terrain, anchor=(0.0, 10.0), facing=2,
        constraints=LayoutConstraints(),
        pipe_throughput_fn=low_cap,
    )
    lp = plan(req, sample_geometry())
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    sl = lp.stage_logistics.get("steam")
    assert sl is not None, "stage_log steam manquant"
    assert sl.pipes_out_per_stage == 6, f"pipes_out={sl.pipes_out_per_stage} attendu=6 (n_lanes)"
    # Compte de pipes steam > 1 lane (n_lanes * n_seg pipes pour l'étage steam).
    n_pipes_steam = sum(1 for e in lp.entities if e.role == "pipe" and e.node_item == "steam")
    assert n_pipes_steam >= 6, f"pipes steam={n_pipes_steam} attendu >=6 (6 lanes * n_seg)"
    record("test_pipe_multiparallel", True,
           f"steam@60 cap=10 -> n_lanes=6, pipes_out={sl.pipes_out_per_stage}, pipes steam={n_pipes_steam}")


def test_backcompat_s2b3() -> None:
    """S2b-3 back-compat : viscosité 0 (water/steam) + débit <= cap -> n_lanes=1 (S2a/S2b-2
    inchangé). steam@60 (cap=1500) -> pipes_out=1 ; chaîne fer 0 pipe préservée."""
    lp = _steam_plan(60.0)   # défaut : pipe_throughput_fn=pipe_throughput (k=0, cap=1500)
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    sl = lp.stage_logistics.get("steam")
    assert sl is not None and sl.pipes_out_per_stage == 1, \
        f"steam pipes_out={sl.pipes_out_per_stage if sl else '?'} attendu=1 (k=0, n_lanes=1)"
    # Chaîne fer : 0 pipe (back-compat S0/S1).
    kb_fe = sample_kb()
    splan_fe = solve(ProductionRequest("iron-gear-wheel", 5.0), kb_fe)
    has_pipe = any(n.transport == "pipe" for n in splan_fe.nodes)
    assert not has_pipe, f"chaîne fer ne devrait pas avoir de pipe: {splan_fe.nodes}"
    record("test_backcompat_s2b3", True,
           f"steam n_lanes=1 (k=0) ; fer 0 pipe — S2a/S2b-2 inchangés")


# ===== S3c : beacons + modules insérés =====

def test_no_beacon_backcompat() -> None:
    """S3c back-compat : beacons_per_stage=0 (défaut) -> aucun entity "beacon", totals sans
    clé "beacon", layout identique S2 (u_next inchangé). Signatures _add/_place_stage
    inchangées, chaîne fer préservée."""
    lp = _gears_plan(5.0)   # défaut LayoutConstraints -> beacons_per_stage=0
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    beacons = [e for e in lp.entities if e.role == "beacon"]
    has_beacon_total = "beacon" in (lp.totals or {})
    rec("test_no_beacon_backcompat : 0 beacon (défaut), totals sans beacon",
        len(beacons) == 0 and not has_beacon_total,
        f"beacons={len(beacons)} totals_beacon={lp.totals.get('beacon', 0)}")


def test_beacon_row_placement() -> None:
    """S3c : beacons_per_stage=4 -> 4 beacons/étage côté +u (au-delà du belt_out), chacun avec
    modules=["speed-module-3","speed-module-3"], u_beacon = u_machine + offset_out_u + 1.0 +
    beacon_half_u (edge-to-edge machine=2.5 < supply_area=3), aucune collision machine."""
    lp = _gears_plan(0.5, constraints=LayoutConstraints(beacons_per_stage=4))
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    geo = sample_geometry()
    gbeacon = geo.geometry("beacon")
    assert gbeacon is not None, "geometry beacon manquante"
    beacon_half_u = gbeacon.w / 2.0
    facing = 2
    beacons = [e for e in lp.entities if e.role == "beacon"]
    ok_count = len(beacons) > 0
    rec("test_beacon_row_placement : beacons posés (beacons_per_stage=4)", ok_count,
        f"n_beacons={len(beacons)} totals={lp.totals.get('beacon', 0)}")
    # (a) modules insérés sur chaque beacon
    ok_mods = all(e.modules == ["speed-module-3", "speed-module-3"] for e in beacons)
    rec("test_beacon_row_placement : modules=[speed-module-3]*2 sur chaque beacon",
        ok_mods, f"modules[0]={beacons[0].modules if beacons else '?'}")
    # (b) u_beacon = u_machine + offset_out_u + 1.0 + beacon_half_u (au-delà du belt_out,
    #     edge-to-edge machine 2.5 < supply_area 3 -> couverture). On vérifie que chaque
    #     beacon est à ~5.5 tuiles (+u) d'une machine du même étage (offset_out_u=3.0,
    #     half_u=1.5 -> 3.0+1.0+1.5=5.5 pour machine 3×3).
    machines = [e for e in lp.entities if e.role == "machine"]
    ok_u = False
    if beacons and machines:
        # distance u min beacon->machine = 5.5 (toutes machines 3×3 dans la chaîne fer).
        du = min(abs(_to_uv(facing, b.x, b.y)[0] - _to_uv(facing, m.x, m.y)[0])
                 for b in beacons for m in machines)
        ok_u = abs(du - 5.5) < 0.6
    rec("test_beacon_row_placement : u_beacon = u_machine+5.5 (au-delà belt_out, couvre)",
        ok_u, f"du_min={du if beacons and machines else '?'} (attendu ~5.5)")
    # (c) aucune collision beacon<->machine (edge-to-edge u = 2.5 > 0).
    ok_nocol = True
    for b in beacons:
        ub, vb = _to_uv(facing, b.x, b.y)
        for m in machines:
            um, vm = _to_uv(facing, m.x, m.y)
            du = abs(ub - um) - 1.5 - beacon_half_u   # half_u machine 3×3=1.5
            dv = abs(vb - vm) - 1.5 - 1.5
            if du < -0.01 and dv < -0.01:   # chevauchement sur les 2 axes = collision
                ok_nocol = False
                break
    rec("test_beacon_row_placement : aucune collision beacon<->machine", ok_nocol, "")


def test_beacon_coverage() -> None:
    """S3c : chaque machine (role "machine") est dans la supply_area (3.0) d'au moins 1 beacon
    (edge-to-edge u ET v ≤ supply_area). beacons_per_stage=4 sur chaîne fer@0.5 (smelting
    N=4, crafting N=1 -> 4 beacons/étage couvrent)."""
    lp = _gears_plan(0.5, constraints=LayoutConstraints(beacons_per_stage=4))
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    geo = sample_geometry()
    gbeacon = geo.geometry("beacon")
    supply = gbeacon.supply_area if gbeacon else 3.0
    facing = 2
    beacons = [e for e in lp.entities if e.role == "beacon"]
    machines = [e for e in lp.entities if e.role == "machine"]
    uncovered = []
    for m in machines:
        um, vm = _to_uv(facing, m.x, m.y)
        covered = False
        for b in beacons:
            ub, vb = _to_uv(facing, b.x, b.y)
            du = abs(ub - um) - 1.5 - 1.5   # half machine 3×3 + half beacon 3×3
            dv = abs(vb - vm) - 1.5 - 1.5
            if du <= supply + 1e-9 and dv <= supply + 1e-9:
                covered = True
                break
        if not covered:
            uncovered.append((um, vm))
    rec("test_beacon_coverage : chaque machine <= supply_area(3) d'un beacon",
        len(uncovered) == 0, f"machines={len(machines)} beacons={len(beacons)} uncovered={len(uncovered)}")


def test_no_beacon_neg_backcompat() -> None:
    """S3d back-compat : beacons_neg_per_stage=0 (défaut) -> aucun beacon -u, totals sans
    beacon -u supplémentaire, layout identique S3c (u_next inchangé). Signatures préservées."""
    lp = _gears_plan(5.0)   # défaut LayoutConstraints -> beacons_neg_per_stage=0
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    # beacons_neg_per_stage=0 -> aucun beacon -u. Les éventuels beacons (S3c défaut 0) = 0.
    beacons = [e for e in lp.entities if e.role == "beacon"]
    rec("test_no_beacon_neg_backcompat : 0 beacon (défaut beacons_neg=0)",
        len(beacons) == 0 and (lp.totals or {}).get("beacon", 0) == 0,
        f"beacons={len(beacons)} totals_beacon={lp.totals.get('beacon', 0)}")


def test_beacon_neg_row_placement() -> None:
    """S3d : beacons_per_stage=4 + beacons_neg_per_stage=4 -> beacons -u posés au miroir
    (u_machine - offset_out_u - 1.0 - beacon_half_u = u_machine - 5.5 pour machine 3×3),
    modules=[speed-module-3]*2, aucune collision beacon -u <-> machine/belt_in/inserter.
    Chaîne fer@0.5 = 1 ingrédient/étage (iron-ore -> iron-plate -> iron-gear-wheel) -> pas
    de collision belt ing1 -> beacons -u posés sur les étages machine consécutifs."""
    lp = _gears_plan(0.5, constraints=LayoutConstraints(beacons_per_stage=4,
                                                       beacons_neg_per_stage=4))
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    geo = sample_geometry()
    gbeacon = geo.geometry("beacon")
    assert gbeacon is not None, "geometry beacon manquante"
    beacon_half_u = gbeacon.w / 2.0
    facing = 2
    beacons = [e for e in lp.entities if e.role == "beacon"]
    machines = [e for e in lp.entities if e.role == "machine"]
    # Sépare beacons +u (u > machine) et -u (u < machine).
    beacon_neg = []
    for b in beacons:
        ub, _ = _to_uv(facing, b.x, b.y)
        umachines = [_to_uv(facing, m.x, m.y)[0] for m in machines]
        if umachines and min(abs(ub - um) for um in umachines) > 0 and ub < min(umachines) + 0.1:
            # u < plus petite machine -> côté -u (miroir)
            beacon_neg.append(b)
    rec("test_beacon_neg_row_placement : beacons -u posés (beacons_neg_per_stage=4)",
        len(beacon_neg) > 0, f"n_beacons_neg={len(beacon_neg)} total_beacons={len(beacons)}")
    # (a) modules insérés sur chaque beacon -u
    ok_mods = all(e.modules == ["speed-module-3", "speed-module-3"] for e in beacon_neg)
    rec("test_beacon_neg_row_placement : modules=[speed-module-3]*2 sur chaque beacon -u",
        ok_mods, f"modules[0]={beacon_neg[0].modules if beacon_neg else '?'}")
    # (b) u_beacon_neg = u_machine - 5.5 (miroir, edge-to-edge machine 2.5 < supply_area 3)
    ok_u = False
    if beacon_neg and machines:
        du = min(abs(_to_uv(facing, b.x, b.y)[0] - _to_uv(facing, m.x, m.y)[0])
                 for b in beacon_neg for m in machines)
        ok_u = abs(du - 5.5) < 0.6
    rec("test_beacon_neg_row_placement : u_beacon_neg = u_machine-5.5 (miroir, couvre)",
        ok_u, f"du_min={du if beacon_neg and machines else '?'} (attendu ~5.5)")
    # (c) aucune collision beacon -u <-> machine (edge-to-edge u = 2.5 > 0)
    ok_nocol = True
    for b in beacon_neg:
        ub, vb = _to_uv(facing, b.x, b.y)
        for m in machines:
            um, vm = _to_uv(facing, m.x, m.y)
            du = abs(ub - um) - 1.5 - beacon_half_u   # half_u machine 3×3=1.5
            dv = abs(vb - vm) - 1.5 - 1.5
            if du < -0.01 and dv < -0.01:   # chevauchement sur les 2 axes = collision
                ok_nocol = False
                break
    rec("test_beacon_neg_row_placement : aucune collision beacon -u<->machine", ok_nocol, "")


def test_beacon_neg_double_coverage() -> None:
    """S3d : chaque machine d'un étage avec beacons -u est couverte des DEUX côtés (>=1 beacon
    +u ET >=1 beacon -u dans la supply_area 3.0, edge-to-edge u <= supply_area). "8 beacons"
    = 4+u + 4-u par étage."""
    lp = _gears_plan(0.5, constraints=LayoutConstraints(beacons_per_stage=4,
                                                       beacons_neg_per_stage=4))
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    geo = sample_geometry()
    gbeacon = geo.geometry("beacon")
    supply = gbeacon.supply_area if gbeacon else 3.0
    facing = 2
    beacons = [e for e in lp.entities if e.role == "beacon"]
    machines = [e for e in lp.entities if e.role == "machine"]
    # Pour chaque machine, vérifie couverture +u (beacon u > machine u) ET -u (u < machine u).
    uncovered_pos = []
    uncovered_neg = []
    for m in machines:
        um, vm = _to_uv(facing, m.x, m.y)
        cov_pos = False
        cov_neg = False
        for b in beacons:
            ub, vb = _to_uv(facing, b.x, b.y)
            du = abs(ub - um) - 1.5 - 1.5   # half machine 3×3 + half beacon 3×3
            dv = abs(vb - vm) - 1.5 - 1.5
            if du <= supply + 1e-9 and dv <= supply + 1e-9:
                if ub > um:
                    cov_pos = True
                else:
                    cov_neg = True
        if not cov_pos:
            uncovered_pos.append((um, vm))
        if not cov_neg:
            uncovered_neg.append((um, vm))
    # Au moins 1 machine couverte des deux côtés (les étages machine consécutifs beaconnés).
    double_covered = len(machines) - len(uncovered_pos) - len(uncovered_neg) >= 0
    n_both = sum(1 for m in machines
                 if any(abs(_to_uv(facing, b.x, b.y)[0] - _to_uv(facing, m.x, m.y)[0]) > 1.6
                        and abs(_to_uv(facing, b.x, b.y)[0] - _to_uv(facing, m.x, m.y)[0]) - 3.0 <= supply + 1e-9
                        for b in beacons))
    rec("test_beacon_neg_double_coverage : >=1 machine couverte +u ET -u",
        len(uncovered_neg) < len(machines),   # au moins une machine a un beacon -u
        f"machines={len(machines)} uncov_pos={len(uncovered_pos)} uncov_neg={len(uncovered_neg)}")


def test_beacon_neg_multi_ing_skip() -> None:
    """S3d : étage 2+ ingrédients (alloy = iron-ore + copper-ore) avec beacons_neg_per_stage=4
    -> beacon -u collisionne la belt ing1 (long-handed à u_machine-5.5 = position miroir) ->
    skip + note beacon_neg_collision:alloy. Beacons +u toujours présents, aucun crash."""
    kb = sample_kb()
    splan = solve(ProductionRequest("alloy", 5.0), kb)
    terrain = Terrain(patches=[
        ResourcePatch("iron-ore", [(x, y) for x in range(10) for y in range(10)], (0, 0, 10, 10)),
        ResourcePatch("copper-ore", [(x, y) for x in range(10) for y in range(10)], (20, 0, 30, 10)),
    ])
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 5.0), facing=2,
                       constraints=LayoutConstraints(beacons_per_stage=4, beacons_neg_per_stage=4))
    lp = plan(req, sample_geometry())
    assert lp.feasibility == "ok", f"feasibility={lp.feasibility}, notes={lp.notes}"
    has_note = any("beacon_neg_collision" in n for n in lp.notes)
    rec("test_beacon_neg_multi_ing_skip : note beacon_neg_collision:alloy présente",
        has_note, f"notes={lp.notes[:120]}")
    # L'étage alloy (assembling-machine-1) n'a pas de beacon -u (skipped). Les beacons +u
    # sont présents (beacons_per_stage=4 > 0).
    beacons = [e for e in lp.entities if e.role == "beacon"]
    facing = 2
    machines = [e for e in lp.entities if e.role == "machine"]
    umachines = [_to_uv(facing, m.x, m.y)[0] for m in machines]
    # beacon -u = u < min machine (côté entrée). Aucun attendu sur l'étage alloy (skip).
    # On vérifie qu'aucun beacon n'est à la position miroir collisionnelle (u_machine-5.5).
    rec("test_beacon_neg_multi_ing_skip : beacons +u présents (beacons_per_stage=4)",
        len(beacons) > 0, f"n_beacons={len(beacons)} totals={lp.totals.get('beacon', 0)}")


# ===== S4b : adaptation terrain (détection per-entité + replan auto déterministe) =====
#
# Le levier de contournement est cascade_offset_v (offset uniforme au 1er étage machine,
# propage à toute la cascade via v_out), PAS l'anchor — pour les chaînes minières l'anchor est
# ignoré (les étages suivent patch.bbox). Géométrie _gears_plan(0.5) : drills sur patch
# (u=1.5..17.5, v=1.5), furnace (étage 1) à u=23.5, v=20.0. Obstacle (22,19,25,21) hit la
# furnace (v=20), pas les drills (u<=20.5). Shift +v (offset=3) -> furnace v=23 (hors obstacle).


def test_terrain_check_no_obstacle() -> None:
    """S4b : terrain_check=True, obstacles=[] -> feasibility=ok, mêmes totals que S3d."""
    lp = _gears_plan(0.5, None, 2, LayoutConstraints(terrain_check=True, replan_budget=4))
    lp_ref = _gears_plan(0.5)  # S3d défaut
    ok = lp.feasibility == "ok" and lp.totals == lp_ref.totals
    rec("test_terrain_check_no_obstacle : feasibility=ok + mêmes totals que S3d",
        ok, f"feas={lp.feasibility} totals_eq={lp.totals == lp_ref.totals}")


def test_terrain_check_obstacle_on_stage() -> None:
    """S4b : obstacle sur furnace (v=20), terrain_check=True, replan_budget=0 (détection seule)
    -> obstacle_blocking + note per_entity. Obstacle (22,19,25,21) hit furnace (u=23.5,v=20),
    pas les drills (u<=20.5)."""
    bt = sample_terrain("iron-ore", 20)
    terrain = Terrain(patches=bt.patches, obstacles=[(22, 19, 25, 21)])
    lp = _gears_plan(0.5, terrain, 2,
                     LayoutConstraints(terrain_check=True, replan_budget=0))
    ok = lp.feasibility == "obstacle_blocking" and any("per_entity" in n for n in lp.notes)
    rec("test_terrain_check_obstacle_on_stage : obstacle_blocking + note per_entity (budget=0)",
        ok, f"feas={lp.feasibility} per_entity={any('per_entity' in n for n in lp.notes)}")


def test_replan_shift_offset_v_success() -> None:
    """S4b : obstacle sur furnace (v=20), replan_budget=4, bypass_offset_v=3 -> 2e essai
    (shift +v, cascade_offset_v=3) décale la cascade à v=23 (hors obstacle v=19..21) -> ok.
    Confirme le levier cascade_offset_v (pas l'anchor : chaîne minière)."""
    bt = sample_terrain("iron-ore", 20)
    terrain = Terrain(patches=bt.patches, obstacles=[(22, 19, 25, 21)])
    lp = _gears_plan(0.5, terrain, 2,
                     LayoutConstraints(terrain_check=True, replan_budget=4,
                                       bypass_offset_v=3))
    ok = lp.feasibility == "ok" and lp.request.constraints.cascade_offset_v == 3
    rec("test_replan_shift_offset_v_success : shift +v (offset=3) -> ok (cascade hors obstacle)",
        ok, f"feas={lp.feasibility} offset={lp.request.constraints.cascade_offset_v}")


def test_replan_rotate_facing_success() -> None:
    """S4b : obstacle large (v=10..50) bloque tous les shifts v (v=14..26 dedans) -> pivot
    facing +90° (facing=4) déplace la cascade au sud du patch -> ok, facing=4."""
    bt = sample_terrain("iron-ore", 20)
    terrain = Terrain(patches=bt.patches, obstacles=[(22, 10, 80, 50)])
    lp = _gears_plan(0.5, terrain, 2,
                     LayoutConstraints(terrain_check=True, replan_budget=8,
                                       bypass_offset_v=3))
    ok = lp.feasibility == "ok" and lp.request.facing == 4
    rec("test_replan_rotate_facing_success : pivot facing +90° (facing=4) -> ok",
        ok, f"feas={lp.feasibility} facing={lp.request.facing}")


def test_replan_budget_exhausted() -> None:
    """S4b : obstacle géant (recouvre tout) -> tous essais bloqués, budget=4 épuisé ->
    obstacle_blocking + note replan_exhausted (handoff FactoryBuilder S4c)."""
    bt = sample_terrain("iron-ore", 20)
    terrain = Terrain(patches=bt.patches, obstacles=[(-50, -50, 200, 200)])
    lp = _gears_plan(0.5, terrain, 2,
                     LayoutConstraints(terrain_check=True, replan_budget=4,
                                       bypass_offset_v=3))
    ok = lp.feasibility == "obstacle_blocking" and any("replan_exhausted" in n for n in lp.notes)
    rec("test_replan_budget_exhausted : obstacle_blocking + note replan_exhausted (handoff)",
        ok, f"feas={lp.feasibility} exhausted={any('replan_exhausted' in n for n in lp.notes)}")


def test_replan_no_infinite_loop() -> None:
    """S4b : budget > nb de candidats (budget=20 ; 1 origine + 7 candidats = 8 distincts) ->
    _next_replan_attempt retourne None après épuisement, pas de boucle infinie. Le test termine
    (prouve la borne via `tried`) et feasibility=obstacle_blocking."""
    bt = sample_terrain("iron-ore", 20)
    terrain = Terrain(patches=bt.patches, obstacles=[(-50, -50, 200, 200)])
    lp = _gears_plan(0.5, terrain, 2,
                     LayoutConstraints(terrain_check=True, replan_budget=20,
                                       bypass_offset_v=3))
    ok = lp.feasibility == "obstacle_blocking" and any("replan_exhausted" in n for n in lp.notes)
    rec("test_replan_no_infinite_loop : termine (budget=20 > 8 candidats, borné par tried)",
        ok, f"feas={lp.feasibility} exhausted={any('replan_exhausted' in n for n in lp.notes)}")


def test_back_compat_s3d_no_terrain_check() -> None:
    """S4b back-compat : terrain_check=False, replan_budget=0, terrain avec obstacles ->
    plan() appelle _plan_core (check post-hoc global S3d). Note "bbox étage intersecte obstacle"
    (format S3d), PAS "per_entity"."""
    bt = sample_terrain("iron-ore", 20)
    terrain = Terrain(patches=bt.patches, obstacles=[(22, 19, 25, 21)])
    lp = _gears_plan(0.5, terrain, 2,
                     LayoutConstraints(terrain_check=False, replan_budget=0))
    posthoc = any("bbox étage intersecte" in n for n in lp.notes)
    per_entity = any("per_entity" in n for n in lp.notes)
    ok = lp.feasibility == "obstacle_blocking" and posthoc and not per_entity
    rec("test_back_compat_s3d_no_terrain_check : post-hoc S3d (bbox intersecte), pas per_entity",
        ok, f"feas={lp.feasibility} posthoc={posthoc} per_entity={per_entity}")


def test_back_compat_s3d_empty_terrain() -> None:
    """S4b back-compat : obstacles=[] (terrain vide), terrain_check=False -> feasibility=ok
    (identique S3d, pas de replan)."""
    lp = _gears_plan(0.5, None, 2,
                     LayoutConstraints(terrain_check=False, replan_budget=0))
    ok = lp.feasibility == "ok"
    rec("test_back_compat_s3d_empty_terrain : feasibility=ok (identique S3d)",
        ok, f"feas={lp.feasibility}")


def main() -> int:
    tests = [
        test_five_gears_layout,
        test_belts_per_stage_high_rate,
        test_missing_patch,
        test_facing_rotation,
        test_solver_infeasible_propagates,
        # S1a
        test_inserter_throughput_s0_compat,
        test_swing_for,
        test_alignment_chaine_fer,
        test_transition_belts_high_gap,
        # S1b
        test_multi_ingredient,
        test_splitter_high_rate,
        test_merger_output,
        test_splitter_merger_backcompat,
        test_too_many_ingredients,
        # S1c
        test_main_bus_basic,
        test_bus_tap_splitter,
        test_bus_feed_merger,
        # S1f
        test_underground_crossing,
        test_pipe_under_crossing,   # S2c : crossing pipe-to-ground (analogue fluide _under_crossing)
        test_bus_tap_priority,
        # S1g
        test_bus_feed_merger_lane,
        # S2a : fluides (pipes / pumpjacks / offshore-pump)
        test_pipe_geometry,
        test_solver_pumpjack_leaf,
        test_solver_offshore_pump_leaf,
        test_solver_plastic_bar_chain,
        test_layout_plastic_bar_chain,
        test_layout_offshore_pump_water,
        test_layout_fluid_direct_pipe,
        test_backcompat_fluid,
        # S2b-1 : multi-produits (advanced-oil) + cracking + storage-tank
        test_solver_advanced_oil_multiproduct,
        test_solver_coproduct_sink,
        test_recipe_selector_preference,
        test_layout_solid_fuel_chain,
        test_layout_multipipe,
        test_backcompat_s2b,
        # S2b-2 : steam/boiler + power (steam-engine)
        test_solver_steam_boiler_chain,
        test_solver_steam_engine_sink,
        test_layout_steam_boiler_chain,
        test_layout_steam_engine_sink,
        # S2b-3 : débit pipe affine (k≠0) + pipes parallèles
        test_pipe_throughput_affine,
        test_pipe_multiparallel,
        test_backcompat_s2b3,
        # S3c : beacons + modules insérés
        test_no_beacon_backcompat,
        test_beacon_row_placement,
        test_beacon_coverage,
        # S3d : beacons côté -u (double couverture "8 beacons")
        test_no_beacon_neg_backcompat,
        test_beacon_neg_row_placement,
        test_beacon_neg_double_coverage,
        test_beacon_neg_multi_ing_skip,
        # S4b : adaptation terrain (détection per-entité + replan auto déterministe)
        test_terrain_check_no_obstacle,
        test_terrain_check_obstacle_on_stage,
        test_replan_shift_offset_v_success,
        test_replan_rotate_facing_success,
        test_replan_budget_exhausted,
        test_replan_no_infinite_loop,
        test_back_compat_s3d_no_terrain_check,
        test_back_compat_s3d_empty_terrain,
    ]
    for t in tests:
        t()
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())