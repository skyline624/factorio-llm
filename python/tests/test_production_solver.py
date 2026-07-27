"""Tests unitaires du ProductionSolver (P2).

Aucun serveur, aucun LLM, aucun RCON requis : on injecte une KnowledgeBase
fixture avec les valeurs MESURÉES en jeu (describe, 2026-07-24) :
  - iron-plate/copper-plate/stone-brick : energy=3.2, category=smelting
  - iron-gear-wheel : energy=0.5, category=crafting, 2 iron-plate -> 1
  - stone-furnace craftingSpeed=1, assembling-machine-1 craftingSpeed=0.5
  - electric-mining-drill miningSpeed=0.5, burner-mining-drill miningSpeed=0.25

Tests de cohérence (les ratios connus doivent tomber du calcul) :
  - 48 stone-furnaces / yellow belt (15 plate/s).
  - 5 iron-gear-wheel/s -> 5 asm-1 + 32 furnaces + 20 electric drills.

Lancement :
    cd python
    python -m tests.test_production_solver
"""

from __future__ import annotations

import math
import sys

from services.knowledge import KnowledgeBase, MachineSpec, Recipe
from services.production_solver import ProductionRequest, solve, plan_summary, ModuleEffect

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


# alias court utilisé dans les tests
rec = record


def sample_kb() -> KnowledgeBase:
    """KB fixture avec les valeurs mesurées en jeu (describe, 2026-07-24)."""
    kb = KnowledgeBase()
    kb.recipes = {
        "iron-plate":   Recipe("iron-plate",   [("iron-ore", 1)], 1, 3.2, "smelting"),
        "copper-plate": Recipe("copper-plate", [("copper-ore", 1)], 1, 3.2, "smelting"),
        "stone-brick":  Recipe("stone-brick",  [("stone", 2)], 1, 3.2, "smelting"),
        "iron-gear-wheel": Recipe("iron-gear-wheel", [("iron-plate", 2)], 1, 0.5, "crafting"),
    }
    kb.machines = {
        "stone-furnace":       MachineSpec("stone-furnace", 1.0, {"smelting"}, "furnace", "burner", 0.0),
        "steel-furnace":       MachineSpec("steel-furnace", 2.0, {"smelting"}, "furnace", "burner", 0.0),
        "assembling-machine-1": MachineSpec("assembling-machine-1", 0.5,
                                            {"crafting", "basic-crafting", "advanced-crafting",
                                             "electronics", "pressing"},
                                            "assembling-machine", "electric", 0.0),
        "electric-mining-drill": MachineSpec("electric-mining-drill", 0.0, set(),
                                             "mining-drill", "electric", 0.5),
        "burner-mining-drill":  MachineSpec("burner-mining-drill", 0.0, set(),
                                             "mining-drill", "burner", 0.25),
        # S3a : electric-furnace (3×3, smelting, electric, 2 module_slots).
        "electric-furnace":    MachineSpec("electric-furnace", 2.0, {"smelting"},
                                            "furnace", "electric", 0.0, module_slots=2),
    }
    return kb


def node_of(plan, item):
    return next((n for n in plan.nodes if n.item == item), None)


def test_yellow_belt_iron() -> None:
    print("\n[test] === COHÉRENCE : 48 stone-furnaces / yellow belt (15 plate/s) ===")
    kb = sample_kb()
    plan = solve(ProductionRequest("iron-plate", 15.0), kb)
    rec("feasibility ok", plan.feasibility == "ok", plan.feasibility)
    n = node_of(plan, "iron-plate")
    rec("iron-plate : 48 stone-furnaces",
        n and n.machine == "stone-furnace" and n.machine_count == 48,
        f"machine={n.machine if n else '?'} count={n.machine_count if n else '?'}")
    rec("iron-plate : effective >= 15/s",
        n and n.rate_effective >= 15.0,
        f"effective={n.rate_effective if n else '?'}")
    rec("iron-plate feuille = iron-ore (mine)",
        any(l.item == "iron-ore" and l.role == "mine" for l in plan.leaves),
        f"leaves={[(l.item, l.role) for l in plan.leaves]}")


def test_five_gears() -> None:
    print("\n[test] === EXEMPLE : 5 iron-gear-wheel/s (5 asm + 32 furnaces + 20 drills) ===")
    kb = sample_kb()
    plan = solve(ProductionRequest("iron-gear-wheel", 5.0), kb)
    rec("feasibility ok", plan.feasibility == "ok", plan.feasibility)
    gear = node_of(plan, "iron-gear-wheel")
    plate = node_of(plan, "iron-plate")
    ore = node_of(plan, "iron-ore")
    rec("gear : 5 assembling-machine-1",
        gear and gear.machine == "assembling-machine-1" and gear.machine_count == 5,
        f"machine={gear.machine if gear else '?'} count={gear.machine_count if gear else '?'}")
    rec("iron-plate : 32 stone-furnaces",
        plate and plate.machine == "stone-furnace" and plate.machine_count == 32,
        f"count={plate.machine_count if plate else '?'}")
    rec("iron-ore : 20 electric-mining-drills",
        ore and ore.machine == "electric-mining-drill" and ore.machine_count == 20,
        f"count={ore.machine_count if ore else '?'}")
    rec("total_machines cohérent",
        plan.total_machines == {"assembling-machine-1": 5, "stone-furnace": 32,
                                 "electric-mining-drill": 20},
        str(plan.total_machines))


def test_stone_brick_ratio2() -> None:
    print("\n[test] === stone-brick (2 stone -> 1) : ratio 2 consommé au débit effectif ===")
    kb = sample_kb()
    plan = solve(ProductionRequest("stone-brick", 15.0), kb)
    brick = node_of(plan, "stone-brick")
    stone = node_of(plan, "stone")
    # 15 brick/s -> 48 furnaces, effective = 15/s (pile). consomme 30 stone/s.
    rec("stone-brick : 48 stone-furnaces",
        brick and brick.machine_count == 48,
        f"count={brick.machine_count if brick else '?'}")
    # Ingrédient stone consommé au débit EFFECTIF : 15 * 2 / 1 = 30/s.
    rec("stone consommé @ 30/s (effectif)",
        stone and stone.rate_per_sec == 30.0,
        f"rate={stone.rate_per_sec if stone else '?'}")
    rec("stone : 60 electric-mining-drills (30 / 0.5)",
        stone and stone.machine_count == 60,
        f"count={stone.machine_count if stone else '?'}")


def test_tier_override() -> None:
    print("\n[test] === override tiers : steel-furnace + asm-2 + burner-drill ===")
    kb = sample_kb()
    kb.machines["assembling-machine-2"] = MachineSpec(
        "assembling-machine-2", 0.75, {"crafting"}, "assembling-machine", "electric", 0.0)
    plan = solve(ProductionRequest("iron-gear-wheel", 5.0, {
        "smelting": "steel-furnace", "crafting": "assembling-machine-2", "mine": "burner-mining-drill",
    }), kb)
    plate = node_of(plan, "iron-plate")
    gear = node_of(plan, "iron-gear-wheel")
    ore = node_of(plan, "iron-ore")
    # asm-2 speed=0.75 -> per_machine = 1*0.75/0.5 = 1.5 -> ceil(5/1.5)=4.
    #   effective = 4*1.5 = 6.0 gear/s (>= 5).
    rec("gear : 4 assembling-machine-2 (speed=0.75)",
        gear and gear.machine == "assembling-machine-2" and gear.machine_count == 4,
        f"count={gear.machine_count if gear else '?'}")
    # Ingrédient iron-plate consommé au EFFECTIF : 6.0 * 2 / 1 = 12/s (pas 10).
    # steel-furnace speed=2 -> per_machine = 1*2/3.2 = 0.625 -> ceil(12/0.625)=ceil(19.2)=20.
    rec("iron-plate : 20 steel-furnaces (effective 12/s, speed=2)",
        plate and plate.machine == "steel-furnace" and plate.machine_count == 20,
        f"machine={plate.machine if plate else '?'} count={plate.machine_count if plate else '?'} "
        f"rate={plate.rate_per_sec if plate else '?'}")
    # effective plate = 20*0.625 = 12.5/s -> iron-ore @ 12.5/s.
    # burner-drill speed=0.25 -> ceil(12.5/0.25)=50.
    rec("iron-ore : 50 burner-mining-drills (12.5/0.25, speed=0.25)",
        ore and ore.machine == "burner-mining-drill" and ore.machine_count == 50,
        f"count={ore.machine_count if ore else '?'}")


def test_edge_cases() -> None:
    print("\n[test] === cas limites ===")
    kb = sample_kb()
    rec("rate<=0 -> plan vide",
        solve(ProductionRequest("iron-plate", 0.0), kb).nodes == [],
        "nodes=[]")
    rec("item inconnu -> missing_recipe",
        solve(ProductionRequest("titanium-plate", 5.0), kb).feasibility.startswith("missing_recipe"),
        solve(ProductionRequest("titanium-plate", 5.0), kb).feasibility)


def test_arrondi_propagation_effectif() -> None:
    print("\n[test] === arrondi supérieur + propagation du débit EFFECTIF ===")
    kb = sample_kb()
    # gear @ 4.3/s : asm-1 per_machine=1.0 -> ceil(4.3)=5 asm, effective=5.0/s.
    # -> iron-plate consommé @ 5.0*2/1 = 10/s (PAS 4.3*2=8.6).
    plan = solve(ProductionRequest("iron-gear-wheel", 4.3), kb)
    gear = node_of(plan, "iron-gear-wheel")
    plate = node_of(plan, "iron-plate")
    rec("gear : ceil(4.3) = 5 asm",
        gear and gear.machine_count == 5,
        f"count={gear.machine_count if gear else '?'}")
    rec("gear effective = 5.0/s (>= 4.3)",
        gear and abs(gear.rate_effective - 5.0) < 1e-9,
        f"effective={gear.rate_effective if gear else '?'}")
    rec("iron-plate propagé au EFFECTIF (10/s, pas 8.6)",
        plate and abs(plate.rate_per_sec - 10.0) < 1e-9,
        f"plate_rate={plate.rate_per_sec if plate else '?'}")


def test_module_speed_bonus() -> None:
    print("\n[test] === S3a : module speed bonus (Option A, agrégé par machine) ===")
    kb = sample_kb()
    # gear @ 4.3/s : asm-1 per_machine=1.0 (speed=0.5, craft_time=0.5, result=1).
    # speed_bonus=1.0 -> effective_speed=1.0 -> per_machine=2.0 -> ceil(4.3/2.0)=3.
    plan = solve(ProductionRequest("iron-gear-wheel", 4.3,
                                   module_effects={"assembling-machine-1":
                                                   ModuleEffect(speed_bonus=1.0)}), kb)
    gear = node_of(plan, "iron-gear-wheel")
    rec("gear : speed x2 -> ceil(4.3/2.0)=3 asm (au lieu de 5)",
        gear and gear.machine_count == 3,
        f"count={gear.machine_count if gear else '?'}")
    rec("gear effective = 3*2.0 = 6.0/s",
        gear and abs(gear.rate_effective - 6.0) < 1e-9,
        f"effective={gear.rate_effective if gear else '?'}")
    rec("gear speed_bonus audité sur le nœud",
        gear and abs(gear.speed_bonus - 1.0) < 1e-9,
        f"speed_bonus={gear.speed_bonus if gear else '?'}")


def test_module_productivity_bonus() -> None:
    print("\n[test] === S3a : module productivity bonus (produits gratuits) ===")
    kb = sample_kb()
    # gear @ 4.3/s : asm-1 per_machine base=1.0. productivity_bonus=0.25 ->
    # effective_productivity=1.25 -> per_machine=1.25 -> ceil(4.3/1.25)=ceil(3.44)=4.
    # eff = 4*1.25 = 5.0 gear/s. iron-plate consommé = eff*2/(1*1.25) = 5.0*2/1.25 = 8.0/s
    # (PAS 10.0 : la productivité réduit la conso d'ingrédient par unité produite).
    plan = solve(ProductionRequest("iron-gear-wheel", 4.3,
                                   module_effects={"assembling-machine-1":
                                                   ModuleEffect(productivity_bonus=0.25)}), kb)
    gear = node_of(plan, "iron-gear-wheel")
    plate = node_of(plan, "iron-plate")
    rec("gear : prod +25% -> ceil(4.3/1.25)=4 asm",
        gear and gear.machine_count == 4,
        f"count={gear.machine_count if gear else '?'}")
    rec("gear effective = 4*1.25 = 5.0/s",
        gear and abs(gear.rate_effective - 5.0) < 1e-9,
        f"effective={gear.rate_effective if gear else '?'}")
    rec("iron-plate propagé = 5.0*2/1.25 = 8.0/s (prod = ingrédients gratuits)",
        plate and abs(plate.rate_per_sec - 8.0) < 1e-9,
        f"plate_rate={plate.rate_per_sec if plate else '?'}")


def test_no_module_backcompat() -> None:
    print("\n[test] === S3a : back-compat sans module_effects (== S2) ===")
    kb = sample_kb()
    # gear @ 4.3/s sans module_effects -> identique S2 : count=5, plate=10.0.
    plan = solve(ProductionRequest("iron-gear-wheel", 4.3), kb)
    gear = node_of(plan, "iron-gear-wheel")
    plate = node_of(plan, "iron-plate")
    rec("gear : sans module -> ceil(4.3)=5 asm (back-compat S2)",
        gear and gear.machine_count == 5,
        f"count={gear.machine_count if gear else '?'}")
    rec("iron-plate : 10.0/s (formule S2 inchangée)",
        plate and abs(plate.rate_per_sec - 10.0) < 1e-9,
        f"plate_rate={plate.rate_per_sec if plate else '?'}")
    rec("gear speed_bonus=0.0 par défaut (audit neutre)",
        gear and abs(gear.speed_bonus - 0.0) < 1e-9,
        f"speed_bonus={gear.speed_bonus if gear else '?'}")


def test_electric_furnace_tier() -> None:
    print("\n[test] === S3a : electric-furnace tier (speed=2, module_slots=2) ===")
    kb = sample_kb()
    # iron-plate @ 10/s avec machine_tiers smelting=electric-furnace (speed=2).
    # per_machine = 1*2/3.2 = 0.625 -> ceil(10/0.625)=ceil(16.0)=16.
    plan = solve(ProductionRequest("iron-plate", 10.0,
                                   machine_tiers={"smelting": "electric-furnace"}), kb)
    plate = node_of(plan, "iron-plate")
    rec("iron-plate : electric-furnace sélectionné via machine_tiers",
        plate and plate.machine == "electric-furnace",
        f"machine={plate.machine if plate else '?'}")
    rec("iron-plate : ceil(10/0.625)=16 electric-furnaces (speed=2)",
        plate and plate.machine_count == 16,
        f"count={plate.machine_count if plate else '?'}")


def recap() -> None:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


def main() -> int:
    test_yellow_belt_iron()
    test_five_gears()
    test_stone_brick_ratio2()
    test_tier_override()
    test_edge_cases()
    test_arrondi_propagation_effectif()
    test_module_speed_bonus()
    test_module_productivity_bonus()
    test_no_module_backcompat()
    test_electric_furnace_tier()
    return recap()


if __name__ == "__main__":
    sys.exit(main())