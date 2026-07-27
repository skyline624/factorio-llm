"""Validation LIVE S3d : beacons cote -u (double couverture "8 beacons" = 4 +u + 4 -u).

Pre-requis : serveur Factorio 2.0 headless lance (scripts/start_factorio_dedicated.bat)
avec le mod factorio-llm charge (version S3b : describe/measure beacon + PRODUCER_TYPES
etendu + STARTING_ITEMS beacon/modules/electric-furnace), RCON 127.0.0.1:27015
(pw "factoriollm"). Pas de relance requise pour S3d (modifs Python uniquement, comme S3c).

Chaine de test : iron-plate@10/s via electric-furnace (smelting) + 4 beacons +u + 4 beacons -u.
  iron-plate (electric-furnace, smelting, speed=2, module_slots=2)
    <- iron-ore (electric-mining-drill)
  Bonus solveur (Option A, compute_module_effect formule 2.0 FFF#409) :
    8 beacons speed-module-3 (4+u + 4-u) -> dist*sqrt(n)*mod = 1.5*sqrt(8)*0.5 = 2.121 speed_bonus.
    effective_speed = 2*(1+2.121) = 6.243 ; per_machine = 1*1*6.243/3.2 = 1.951.
    count = ceil(10/1.951) = 6 (vs 7 S3c avec 4 beacons, vs 16 sans bonus).

  Le beacon -u au miroir (u_machine - offset_out_u - 1.0 - beacon_half_u = u_machine - 5.5)
  couvre la machine (edge-to-edge 2.5 < supply_area 3.0, symetrique +u). La chaine iron-plate
  a un seul etage _place_stage (smelting) dont le -u fait face aux drills : pas de collision
  avec les transition belts drill->furnace (cote -u libre) -> 8 beacons poses (4+u + 4-u).

8 recs :
  1. set_test_mode + setup (kit S3b beacon/modules/electric-furnace).
  2. solve iron-plate@10 + electric-furnace + module_effects(8 beacons) -> feasibility ok.
  3. machine_count reduit par bonus (6 electric-furnaces vs 7 S3c, vs 16 sans bonus).
  4. plan() beacons_neg_per_stage=4 -> totals beacon = 8 (4+u + 4-u).
  5. beacons -u au miroir (u_machine - 5.5) + modules=[speed-module-3]*2.
  6. can_place chaine double-beaconnee 0 collision (filtre frontier+water, pattern S3c).
  7. LIVE place 1 beacon + insert 2 speed-module-3 + scan_factory -> get_module_inventory.
  8. back-compat : beacons_neg_per_stage=0 (defaut) -> 0 beacon -u (layout S3c inchange).

Lancement :
    cd python
    python verify_layout_s3d.py
"""

from __future__ import annotations
import sys
import math

sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import populate_from_rcon, GeometryBase
from services.production_solver import ProductionRequest, solve, compute_module_effect
from services.layout_planner import (
    LayoutRequest, LayoutConstraints, Terrain, ResourcePatch, plan, plan_summary, _to_uv,
)

DIR_TO_STR = {0: "north", 2: "east", 4: "south", 6: "west"}

ITEMS = ["iron-plate", "iron-gear-wheel"]
MACHINES = ["stone-furnace", "electric-furnace", "assembling-machine-1",
            "electric-mining-drill", "small-electric-pole", "beacon"]
GEO_NAMES = [
    "transport-belt", "burner-inserter", "small-electric-pole",
    "stone-furnace", "electric-furnace", "assembling-machine-1", "electric-mining-drill",
    "beacon",
]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:80]}")


def can_place_all(api, lp, rcon, label, v_frontier):
    """can_place non destructif sur chaque entity (filtre frontier + water, pattern S3c)."""
    ok_n = fail_n = hors_map = obstacle_terrain = 0
    fails_by_role: dict = {}
    for e in lp.entities:
        if e.skip:
            continue
        _, v_e = _to_uv(2, e.x, e.y)
        if v_e > v_frontier + 1.5:
            hors_map += 1
            continue
        tx, ty = int(e.x), int(e.y)
        tile = rcon.query_lua(
            f"local s=game.surfaces['nauvis'] or game.surfaces[1]; "
            f"rcon.print(s.get_tile({tx},{ty}).name)").strip()
        if tile in ("water", "deepwater", "out-of-map") or "Error" in tile:
            obstacle_terrain += 1
            continue
        d = DIR_TO_STR.get(e.direction, "north")
        r = api.can_place_check(e.name, round(e.x, 2), round(e.y, 2), d)
        if r.get("can_place"):
            ok_n += 1
        else:
            fail_n += 1
            fails_by_role[e.role] = fails_by_role.get(e.role, 0) + 1
    print(f"  [{label}] can_place : {ok_n}/{ok_n+fail_n} OK  fail={fail_n}  "
          f"hors_map={hors_map}  obstacle_terrain={obstacle_terrain}  par_role={fails_by_role}")
    return ok_n, fail_n, hors_map, obstacle_terrain


def main() -> int:
    rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
    api = ModApi(rcon)
    try:
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"!! MOD NON RECHARGE (can_place_check absent : {e})")
        print("   -> relance scripts/start_factorio_dedicated.bat puis re-execute.")
        rcon.close()
        return 1

    api.set_test_mode(True)
    api.setup()

    # --- Rec 1 : set_test_mode + setup (kit S3b) ---
    rec("S3d-1 : set_test_mode + setup (kit beacon/modules/electric-furnace)",
        True, "test_mode=True kit S3b")

    kb = populate_from_rcon(api, ITEMS, MACHINES)
    geo = GeometryBase()
    geo.populate_from_rcon(api, GEO_NAMES)

    # Terrain : patch iron-ore reel.
    sp = api.scan_patch("iron-ore", 400)
    if not sp.get("bbox"):
        print("!! aucun patch iron-ore dans rayon 400 -> spawn_test_resources requis.")
        rcon.close()
        return 1
    bbox = sp["bbox"]
    terrain = Terrain([ResourcePatch("iron-ore", bbox=(bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]))])
    anchor = (float(bbox["x1"]), (bbox["y1"] + bbox["y2"]) / 2.0)

    # --- Rec 2 : solve iron-plate@10 + electric-furnace + 8 beacons (module_effects) ---
    # 8 beacons speed-module-3 (4+u + 4-u) -> compute_module_effect(8, "speed-module-3") :
    # formule 2.0 FFF#409 = dist*sqrt(n)*mod = 1.5*sqrt(8)*0.5 = 2.121 speed_bonus.
    meff = compute_module_effect(8, "speed-module-3")
    module_effects = {"electric-furnace": meff}
    splan = solve(ProductionRequest("iron-plate", 10.0,
                                    machine_tiers={"smelting": "electric-furnace"},
                                    module_effects=module_effects), kb)
    rec("S3d-2 : solve iron-plate@10 + electric-furnace + 8 beacons (module_effects)",
        splan.feasibility == "ok",
        f"feasibility={splan.feasibility} speed_bonus={meff.speed_bonus:.3f}")

    # --- Rec 3 : machine_count reduit par bonus (6 vs 7 S3c, vs 16 sans bonus) ---
    # Sans bonus : per_machine = 1*2/3.2 = 0.625 -> count=ceil(10/0.625)=16.
    # S3c 4 beacons (speed_bonus=1.5) : effective_speed=5.0 -> per_machine=1.5625 -> count=7.
    # S3d 8 beacons (speed_bonus=2.121) : effective_speed=6.243 -> per_machine=1.951
    # -> count=ceil(10/1.951)=6.
    ef_node = next((n for n in splan.nodes if n.machine == "electric-furnace"), None)
    expected_count = 6
    ok_count = ef_node is not None and ef_node.machine_count == expected_count
    rec("S3d-3 : machine_count reduit par bonus (6 electric-furnaces vs 7 S3c)",
        ok_count,
        f"count={ef_node.machine_count if ef_node else '?'} speed_bonus={ef_node.speed_bonus if ef_node else '?'}")

    # --- Rec 4 : plan() beacons_neg_per_stage=4 -> totals beacon = 8 (4+u + 4-u) ---
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=anchor, facing=2,
                        constraints=LayoutConstraints(beacons_per_stage=4,
                                                     beacons_neg_per_stage=4))
    lp = plan(req, geo)
    print(plan_summary(lp))
    t = lp.totals
    beacons = [e for e in lp.entities if e.role == "beacon"]
    machines = [e for e in lp.entities if e.role == "machine"]
    # Separe +u (u > machine) et -u (u < machine).
    n_neg = 0
    n_pos = 0
    for b in beacons:
        ub, _ = _to_uv(2, b.x, b.y)
        umachines = [_to_uv(2, m.x, m.y)[0] for m in machines]
        if umachines and ub < min(umachines) + 0.1:
            n_neg += 1
        else:
            n_pos += 1
    rec("S3d-4 : plan totals beacon = 8 (4+u + 4-u)",
        lp.feasibility == "ok" and t.get("beacon", 0) == 8 and n_neg == 4 and n_pos == 4,
        f"feas={lp.feasibility} beacon={t.get('beacon',0)} +u={n_pos} -u={n_neg}")

    # --- Rec 5 : beacons -u au miroir (u_machine - 5.5) + modules=[speed-module-3]*2 ---
    beacons_neg = []
    for b in beacons:
        ub, _ = _to_uv(2, b.x, b.y)
        umachines = [_to_uv(2, m.x, m.y)[0] for m in machines]
        if umachines and ub < min(umachines) + 0.1:
            beacons_neg.append(b)
    ok_mods = len(beacons_neg) > 0 and all(
        e.modules == ["speed-module-3", "speed-module-3"] for e in beacons_neg)
    # position miroir : u_beacon_neg = u_machine - 5.5 (machine 3x3, offset_out_u=3.0)
    ok_u = False
    if beacons_neg and machines:
        du = min(abs(_to_uv(2, b.x, b.y)[0] - _to_uv(2, m.x, m.y)[0])
                 for b in beacons_neg for m in machines)
        ok_u = abs(du - 5.5) < 0.6
    rec("S3d-5 : beacons -u au miroir (u_machine-5.5) + modules=[speed-module-3]*2",
        ok_mods and ok_u,
        f"n_neg={len(beacons_neg)} du_min={du if beacons_neg and machines else '?'} modules[0]={beacons_neg[0].modules if beacons_neg else '?'}")

    # --- Rec 6 : can_place chaine double-beaconnee 0 collision ---
    v_frontier = (bbox["y1"] + bbox["y2"]) / 2.0 + 60.0
    ok_n, fail_n, hors_map, obs_terr = can_place_all(api, lp, rcon, "iron-plate+8beacons", v_frontier)
    rec("S3d-6 : can_place chaine double-beaconnee 0 collision (filtre frontier+water)",
        fail_n == 0,
        f"can_place={ok_n}/{ok_n+fail_n} fail={fail_n} hors_map={hors_map} obstacle_terrain={obs_terr}")

    # --- Rec 7 : LIVE place 1 beacon + insert 2 speed-module-3 + scan_factory ---
    # Valide que le beacon (cote -u) est fonctionnellement identique : create_entity + insert
    # + scan_factory get_module_inventory. Position claire (pas de collision avec la chaine).
    bx, by = 70.0, 70.0
    rcon.query_lua(
        "local s=game.surfaces['nauvis'] or game.surfaces[1]; "
        "for _,e in ipairs(s.find_entities_filtered{name='beacon',area={{69,69},{71,71}}}) do e.destroy() end"
    )
    placed = rcon.query_lua(
        f"local s=game.surfaces['nauvis'] or game.surfaces[1]; "
        f"local e=s.create_entity{{name='beacon',position={{{bx},{by}}},force='player'}}; "
        f"if e then e.insert{{name='speed-module-3',count=2}}; rcon.print('ok') else rcon.print('fail') end"
    ).strip()
    scan = api.scan_factory() if hasattr(api, "scan_factory") else None
    found_mods = None
    if isinstance(scan, dict):
        for row in scan.get("entities", []):
            if row.get("name") == "beacon" and abs(row.get("x", 0) - bx) < 1.0:
                found_mods = row.get("modules")
                break
    mods_ok = (placed == "ok" and found_mods is not None
              and sum(m.get("count", 0) for m in found_mods
                      if m.get("name") == "speed-module-3") >= 2)
    rec("S3d-7 : LIVE beacon + insert 2 speed-module-3 + scan_factory get_module_inventory",
        mods_ok, f"placed={placed} modules={found_mods}")
    rcon.query_lua(
        "local s=game.surfaces['nauvis'] or game.surfaces[1]; "
        "for _,e in ipairs(s.find_entities_filtered{name='beacon',area={{69,69},{71,71}}}) do e.destroy() end"
    )

    # --- Rec 8 : back-compat beacons_neg_per_stage=0 (defaut) -> 0 beacon -u ---
    splan_nob = solve(ProductionRequest("iron-plate", 10.0,
                                       machine_tiers={"smelting": "electric-furnace"}), kb)
    req_nob = LayoutRequest(plan=splan_nob, terrain=terrain, anchor=anchor, facing=2,
                            constraints=LayoutConstraints())  # defaut beacons_neg=0
    lp_nob = plan(req_nob, geo)
    beacons_nob = [e for e in lp_nob.entities if e.role == "beacon"]
    rec("S3d-8 : back-compat beacons_neg_per_stage=0 -> 0 beacon (layout S2/S3c defaut)",
        len(beacons_nob) == 0 and (lp_nob.totals or {}).get("beacon", 0) == 0,
        f"beacons={len(beacons_nob)} totals_beacon={lp_nob.totals.get('beacon',0)}")

    # --- Recap ---
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} recs OK")
    print("=" * 72)
    rcon.close()
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())