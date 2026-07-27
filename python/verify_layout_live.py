"""Validation S0b du LayoutPlanner en jeu (headless).

Pont end-to-end : RCON -> KnowledgeBase + GeometryBase -> ProductionSolver ->
LayoutPlanner -> verification can_place sur chaque entite du blueprint (terrain
reel) + mesure in-game des geometries pour valider le hardcode Python.

Necessite : serveur Factorio lance avec le mod factorio-llm RECHARGE apres l'ajout
des commandes can_place_check / scan_patch / measure_entity (tools.lua + control.lua).
Si le mod n'est pas recharge, le probe initial echoue -> relancer le .bat.

Lancement (apres restart serveur) : python verify_layout_live.py
"""

from __future__ import annotations

import sys

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import (
    populate_from_rcon, GeometryBase,
    GEOMETRY_FIXTURE, THROUGHPUTS,
)
from services.production_solver import ProductionRequest, solve
from services.layout_planner import (
    LayoutRequest, Terrain, ResourcePatch, LayoutConstraints,
    plan, plan_summary,
)

# Chaîne fer S0 : ore -> plate -> gear.
ITEMS = ["iron-plate", "iron-gear-wheel"]
MACHINES = ["stone-furnace", "assembling-machine-1", "electric-mining-drill"]
GEO_NAMES = [
    "transport-belt", "burner-inserter", "fast-inserter", "small-electric-pole",
    "stone-furnace", "assembling-machine-1", "electric-mining-drill", "burner-mining-drill",
]

# Convention 8-dir du LayoutPlanner (0=N, 2=E, 4=S, 6=W) -> string cardinale du mod.
DIR_TO_STR = {0: "north", 2: "east", 4: "south", 6: "west"}

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:46s} {detail[:100]}")


def main() -> int:
    rcon = get_rcon()
    api = ModApi(rcon)

    # Garde : le mod doit etre recharge (can_place_check = nouvelle commande).
    try:
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"\n!! MOD NON RECHARGE (can_place_check absent : {e})")
        print("   -> relance scripts/start_factorio_dedicated.bat puis re-execute ce script.")
        rcon.close()
        return 1

    # Mode test headless + avatar (scan_patch a besoin d'un origin proche du gisement).
    api.set_test_mode(True)
    api.setup()

    # --- KB via RCON (describe) ---
    kb = populate_from_rcon(api, ITEMS, MACHINES)
    print(f"KB : recipes={list(kb.recipes)} machines={list(kb.machines)}")

    # --- Geometry : size via RCON (describe) + fines via fixture ---
    geo = GeometryBase()
    geo.populate_from_rcon(api, GEO_NAMES)
    sizes = {n: (geo.geometry(n).w, geo.geometry(n).h) for n in GEO_NAMES if geo.geometry(n)}
    print(f"Geometry (size RCON) : {sizes}")

    # --- Solveur ---
    splan = solve(ProductionRequest("iron-gear-wheel", 5.0), kb)
    print(f"Solver : feasibility={splan.feasibility} total_machines={splan.total_machines}")
    rec("solver : feasibility ok", splan.feasibility == "ok", splan.feasibility)
    rec("solver : asm-1=5", splan.total_machines.get("assembling-machine-1") == 5,
        str(splan.total_machines.get("assembling-machine-1")))
    rec("solver : stone-furnace=32", splan.total_machines.get("stone-furnace") == 32,
        str(splan.total_machines.get("stone-furnace")))
    rec("solver : electric-mining-drill=20", splan.total_machines.get("electric-mining-drill") == 20,
        str(splan.total_machines.get("electric-mining-drill")))

    # --- Scan d'un vrai patch iron-ore ---
    sp = api.scan_patch("iron-ore", 400)
    count = sp.get("count", 0)
    if count == 0:
        print(f"\n!! Aucun gisement iron-ore dans 400 tuiles (origin={sp.get('origin')})")
        print("   -> regenerer la map (start_factorio_dedicated.bat) ou agrandir le radius.")
        rcon.close()
        return 1
    bbox = sp["bbox"]
    print(f"Patch iron-ore reel : count={count} bbox={bbox} sample={sp.get('sample', [])[:4]}")
    rec("patch iron-ore trouve", count > 0, f"count={count}")

    terrain = Terrain(patches=[ResourcePatch(
        "iron-ore", bbox=(bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]))])

    # --- LayoutPlanner (anchor sur le gisement, facing E) ---
    anchor = (float(bbox["x1"]), (bbox["y1"] + bbox["y2"]) / 2.0)
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=anchor, facing=2,
                        constraints=LayoutConstraints())
    lp = plan(req, geo)
    print("\n" + plan_summary(lp))
    rec("layout : feasibility ok", lp.feasibility == "ok", lp.feasibility)
    sl = lp.stage_logistics.get("iron-gear-wheel")
    rec("layout : gear inserters_in/machine=4 (CALCUL)",
        sl and sl.inserters_in_per_machine == 4,
        f"in={sl.inserters_in_per_machine}" if sl else "?")
    rec("layout : inserter_insufficient signale",
        sl and sl.inserter_insufficient is True,
        str(sl.inserter_insufficient) if sl else "?")

    # --- can_place sur chaque entite du blueprint (terrain reel) ---
    print(f"\n--- can_place_check sur {len(lp.entities)} entites ---")
    ok_count = 0
    fail_count = 0
    fails_by_role: dict[str, int] = {}
    sample_fails: list[str] = []
    for e in lp.entities:
        d = DIR_TO_STR.get(e.direction, "north")
        r = api.can_place_check(e.name, round(e.x, 2), round(e.y, 2), d)
        if r.get("can_place"):
            ok_count += 1
        else:
            fail_count += 1
            fails_by_role[e.role] = fails_by_role.get(e.role, 0) + 1
            if len(sample_fails) < 10:
                sample_fails.append(f"{e.role} {e.name} @({e.x:.1f},{e.y:.1f}) dir={d} err={r.get('error','')}")
    total = len(lp.entities)
    rec(f"can_place : {ok_count}/{total} OK", ok_count == total,
        f"ok={ok_count} fail={fail_count} par_role={fails_by_role}")
    if sample_fails:
        print("  echecs (10 max) :")
        for s in sample_fails:
            print(f"    {s}")
    # Note de limite : can_place_check est independant par entite (ne voit pas les
    # autres entites du blueprint) -> valide terrain + obstacles, pas la collision
    # interne au blueprint (garantie par construction des offsets).
    print("  (limite : can_place est par-entite, ne detecte pas les collisions internes au blueprint)")

    # --- Mesure in-game des geometries (valide le hardcode Python) ---
    print("\n--- measure_entity (valide le hardcode, mode test : pose+mesure+detruit) ---")
    # Position de mesure loin du gisement (terrain libre).
    mx, my = 50.0, 50.0
    for name in ["burner-inserter", "electric-mining-drill", "small-electric-pole", "transport-belt"]:
        m = api.measure_entity(name, mx, my, "north")
        mx += 5.0
        fix = GEOMETRY_FIXTURE.get(name, {})
        print(f"  {name} : {m}")
        if m.get("error"):
            rec(f"mesure {name} (erreur)", False, m["error"])
            continue
        # size : toujours lisible (constat API 2.0).
        if m.get("size"):
            sz = m["size"]
            rec(f"mesure {name} size = {fix.get('w')}x{fix.get('h')}",
                sz.get("w") == fix.get("w") and sz.get("h") == fix.get("h"),
                f"mesure={sz['w']}x{sz['h']} hardcode={fix.get('w')}x{fix.get('h')}")
        # pickup/drop (inserters) : accessibles sur l'INSTANCE, mais en coords ABSOLUES
        # (map) -> reach = distance au centre de l'entite (m.x, m.y).
        cx, cy = m.get("x", 0.0), m.get("y", 0.0)
        if "pickup_position" in m and fix.get("pickup_distance"):
            pp = m["pickup_position"]
            reach = max(abs(pp.get("x", 0) - cx), abs(pp.get("y", 0) - cy))
            rec(f"mesure {name} pickup reach = {fix.get('pickup_distance')}",
                abs(reach - fix.get("pickup_distance", 0)) < 0.2,
                f"mesure={reach:.2f} hardcode={fix.get('pickup_distance')}")
        if "drop_position" in m and fix.get("drop_distance"):
            dp = m["drop_position"]
            reach = max(abs(dp.get("x", 0) - cx), abs(dp.get("y", 0) - cy))
            rec(f"mesure {name} drop reach = {fix.get('drop_distance')}",
                abs(reach - fix.get("drop_distance", 0)) < 0.2,
                f"mesure={reach:.2f} hardcode={fix.get('drop_distance')}")
        # Champs prototype (pcall : certains accessibles, d'autres non en 2.0).
        for key in ["belt_speed", "mining_drill_radius", "max_wire_distance", "supply_area_distance"]:
            if key in m and not str(m[key]).startswith("LuaEntityPrototype"):
                print(f"    {name}.{key} = {m[key]}")

    rcon.close()
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())