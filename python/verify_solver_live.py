"""S0b — validation en jeu du ProductionSolver (RCON → KB → solve).

Peuple une vraie KnowledgeBase via RCON (describe sur items + machines) contre
le serveur headless, lance le solveur, et compare les nombres aux attentes du
fixture (qui sont 20/20 OK en unitaire). Si ça matche : les valeurs mesurées sont
justes côté runtime ET le pont RCON→KB→solveur est validé end-to-end.

Aucune dépendance à setup() (échoue en headless) : describe marche sans avatar.

Lancement :
    cd python
    python verify_solver_live.py
"""

from __future__ import annotations

import json
import sys

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import populate_from_rcon
from services.production_solver import ProductionRequest, solve, plan_summary

ITEMS = ["iron-plate", "copper-plate", "stone-brick", "iron-gear-wheel"]
MACHINES = [
    "stone-furnace", "steel-furnace",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
    "electric-mining-drill", "burner-mining-drill",
]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


def node_of(plan, item):
    return next((n for n in plan.nodes if n.item == item), None)


def dump_kb(kb) -> None:
    print("\n=== KnowledgeBase peuplée depuis RCON ===")
    print(f"recipes : {sorted(kb.recipes)}")
    for it, r in sorted(kb.recipes.items()):
        print(f"  {it}: ingredients={r.ingredients} result_count={r.result_count} "
              f"craft_time={r.craft_time_sec} category={r.category!r}")
    print(f"machines : {sorted(kb.machines)}")
    for m, s in sorted(kb.machines.items()):
        cats = sorted(s.categories) if s.categories else []
        print(f"  {m}: type={s.type} speed={s.crafting_speed} mining={s.mining_speed} "
              f"energy={s.energy_source} cats={cats}")
    print(f"raw_resources : {sorted(kb.raw_resources)}")


def check_yellow_belt(kb) -> None:
    print("\n=== TEST live : iron-plate @ 15/s -> 48 stone-furnaces ===")
    plan = solve(ProductionRequest("iron-plate", 15.0), kb)
    print("  ", plan_summary(plan))
    rec("feasibility ok", plan.feasibility == "ok", plan.feasibility)
    n = node_of(plan, "iron-plate")
    rec("48 stone-furnaces (1 yellow belt)",
        n and n.machine == "stone-furnace" and n.machine_count == 48,
        f"machine={n.machine if n else '?'} count={n.machine_count if n else '?'}")
    rec("effective >= 15/s",
        n and n.rate_effective >= 15.0,
        f"effective={n.rate_effective if n else '?'}")


def check_five_gears(kb) -> None:
    print("\n=== TEST live : iron-gear-wheel @ 5/s -> 5 asm + 32 furnaces + 20 drills ===")
    plan = solve(ProductionRequest("iron-gear-wheel", 5.0), kb)
    print("  ", plan_summary(plan))
    rec("feasibility ok", plan.feasibility == "ok", plan.feasibility)
    gear = node_of(plan, "iron-gear-wheel")
    plate = node_of(plan, "iron-plate")
    ore = node_of(plan, "iron-ore")
    rec("5 assembling-machine-1",
        gear and gear.machine == "assembling-machine-1" and gear.machine_count == 5,
        f"machine={gear.machine if gear else '?'} count={gear.machine_count if gear else '?'}")
    rec("32 stone-furnaces",
        plate and plate.machine == "stone-furnace" and plate.machine_count == 32,
        f"count={plate.machine_count if plate else '?'}")
    rec("20 electric-mining-drills",
        ore and ore.machine == "electric-mining-drill" and ore.machine_count == 20,
        f"count={ore.machine_count if ore else '?'}")


def recap() -> int:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


def main() -> int:
    rcon = get_rcon()
    api = ModApi(rcon)
    try:
        kb = populate_from_rcon(api, ITEMS, MACHINES)
        dump_kb(kb)
        check_yellow_belt(kb)
        check_five_gears(kb)
    finally:
        rcon.close()
    return recap()


if __name__ == "__main__":
    sys.exit(main())