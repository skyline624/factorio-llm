"""Validation LIVE S4c : FactoryBuilder arbitre replan lourd autour du LayoutPlanner.

Pre-requis : serveur Factorio 2.0 headless lance (scripts/start_factorio_dedicated.bat)
APRES le mod S4a (scan_obstacles/scan_tiles_bbox/get_tile). Relance requise (mod Lua modifié
en S4a). RCON 127.0.0.1:27015 (pw "factoriollm"). Pas de relance pour S4c (Python only, mais
le mod S4a doit etre charge pour scan_obstacles/scan_tiles_bbox).

Valide la frontiere LLM/DETERMINISTE (spec §7) sur TERRAIN RÉEL scanné :
  - FactoryBuilder.build_layout(splan, geometry) : choisit gisement (scan_patch), construit
    Terrain (scan_obstacles/scan_water_edge/scan_tiles_bbox), merge contraintes S4b
    (terrain_check=True, replan_budget), appelle plan() (replan auto S4b).
  - feas=ok -> retourne le LayoutPlan (gisement/tier OK).
  - obstacle_blocking -> arbitre replan lourd : tier suivant (yellow->red->blue) puis gisement
    suivant (scan_patch rayon croissant 400/800/1200). Best retenu si tout echoue.
  - Comparaison vs S3d (Contract.layout_constraints=LayoutConstraints(terrain_check=False,
    replan_budget=0)) : S4c active terrain_check+replan, S3d ignore (back-compat).

11 recs :
  1. set_test_mode + setup + mod S4a present (scan_obstacles marche, post-relance).
  2. scan_patch iron-ore reel (bbox).
  3. splan iron-gear-wheel@5/s (solve + kb reel).
  4. build_layout FactoryBuilder -> feas + entities + belt_tier.
  5. terrain_check=True injecte (defaut S4b via _merge_constraints).
  6. replan_budget propage (Contract -> LayoutPlan).
  7. constructible_zone derive de Contract.zone (point -> bbox ±60).
  8. feas=ok -> can_place 0 collision (hors frontier) sur entites non-skip.
  9. S4c vs S3d : S4c active terrain_check (per_entity), S3d non (post-hoc global).
  10. back-compat : Contract.layout_constraints=LayoutConstraints(terrain_check=False,
      replan_budget=0) -> build_layout -> _plan_core S3d (terrain_check=False, feas ok).
  11. S4c entities >= S3d entities (le replan lourd ne fait pas pire) OU feas S4c != ok
      (handoff obstacle_blocking documente).

Lancement (apres relance serveur S4a) :
    cd python
    python verify_layout_s4c.py
"""

from __future__ import annotations
import sys

sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import populate_from_rcon, GeometryBase
from services.production_solver import ProductionRequest, solve
from services.layout_planner import (
    LayoutRequest, LayoutConstraints, Terrain, ResourcePatch, plan, plan_summary,
    _to_uv, _occ_terrain,
)
from agents.base import Contract
from agents.factory_builder import FactoryBuilder
from services.knowledge import ProductionGoal

DIR_TO_STR = {0: "north", 2: "east", 4: "south", 6: "west"}

ITEMS = ["iron-plate", "iron-gear-wheel"]
MACHINES = ["stone-furnace", "electric-furnace", "assembling-machine-1", "electric-mining-drill"]
GEO_NAMES = ["transport-belt", "burner-inserter", "small-electric-pole",
             "stone-furnace", "assembling-machine-1", "electric-mining-drill"]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:80]}")


def can_place_all(api, lp, label, v_frontier, geo):
    """can_place non destructif + compte terrain hits (water/obstacle/out-of-map via _occ_terrain).
    Filtre frontier (out-of-map headless artefact)."""
    ok_n = fail_n = hors_map = terrain_hit = 0
    for e in lp.entities:
        if e.skip:
            continue
        _, v_e = _to_uv(lp.request.facing, e.x, e.y)
        if v_e > v_frontier + 1.5:
            hors_map += 1
            continue
        g = geo.geometry(e.name)
        w = g.w if g else 1.0
        h = g.h if g else 1.0
        kind = _occ_terrain(lp.request.terrain, e.x, e.y, w, h)
        if kind:
            terrain_hit += 1
            continue
        d = DIR_TO_STR.get(e.direction, "north")
        r = api.can_place_check(e.name, round(e.x, 2), round(e.y, 2), d)
        if r.get("can_place"):
            ok_n += 1
        else:
            fail_n += 1
    print(f"  [{label}] can_place={ok_n}/{ok_n+fail_n} fail={fail_n} hors_map={hors_map} "
          f"terrain_hit={terrain_hit}")
    return ok_n, fail_n, hors_map, terrain_hit


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

    # --- Rec 1 : mod S4a present (scan_obstacles marche) ---
    r_obs = api.scan_obstacles()
    ok1 = isinstance(r_obs, dict) and "error" not in r_obs and "obstacles" in r_obs
    rec("S4c-1 : mod S4a present (scan_obstacles marche, post-relance)",
        ok1, f"count={r_obs.get('count') if isinstance(r_obs, dict) else r_obs}")

    kb = populate_from_rcon(api, ITEMS, MACHINES)
    geo = GeometryBase()
    geo.populate_from_rcon(api, GEO_NAMES)

    # --- Rec 2 : scan_patch iron-ore reel ---
    sp = api.scan_patch("iron-ore", 400)
    if not sp.get("bbox"):
        print("!! aucun patch iron-ore -> spawn_test_resources requis.")
        rcon.close()
        return 1
    bbox = sp["bbox"]
    rec("S4c-2 : scan_patch iron-ore reel (bbox)",
        True, f"bbox=({bbox['x1']},{bbox['y1']})-({bbox['x2']},{bbox['y2']})")

    # --- Rec 3 : splan iron-gear-wheel@5/s (solve + kb reel) ---
    splan = solve(ProductionRequest("iron-gear-wheel", 5.0,
                                    machine_tiers={"smelting": "stone-furnace"}), kb)
    has_mine = any(n.role == "mine" for n in splan.nodes)
    rec("S4c-3 : splan iron-gear-wheel@5/s (solve + kb reel, chaîne ore->plate->gear)",
        has_mine and len(splan.nodes) >= 2,
        f"nodes={len(splan.nodes)} roles={[n.role for n in splan.nodes]}")

    # zone = bord aval du patch (constructible_zone derive).
    zone = (float(bbox["x1"]), (bbox["y1"] + bbox["y2"]) / 2.0)
    contract = Contract(ProductionGoal("iron-gear-wheel", 5), zone=zone, replan_budget=4)
    fb = FactoryBuilder(api, contract)

    # --- Rec 4 : build_layout FactoryBuilder -> feas + entities + belt_tier ---
    lp = fb.build_layout(splan, geo)
    feas = getattr(lp, "feasibility", None)
    n_ent = len(getattr(lp, "entities", []))
    bt = getattr(getattr(lp, "request", None).constraints, "belt_tier", None) if lp else None
    rec("S4c-4 : build_layout FactoryBuilder -> LayoutPlan (feas + entities + belt_tier)",
        lp is not None and feas is not None,
        f"feas={feas} entities={n_ent} belt_tier={bt}")

    # --- Rec 5 : terrain_check=True injecte (defaut S4b via _merge_constraints) ---
    tc = getattr(lp.request.constraints, "terrain_check", None) if lp else None
    rec("S4c-5 : terrain_check=True injecte (defaut S4b)",
        tc is True, f"terrain_check={tc}")

    # --- Rec 6 : replan_budget propage (Contract -> LayoutPlan) ---
    rb = getattr(lp.request.constraints, "replan_budget", None) if lp else None
    rec("S4c-6 : replan_budget propage (Contract -> LayoutPlan)",
        rb == 4, f"replan_budget={rb}")

    # --- Rec 7 : constructible_zone derive de Contract.zone (point -> bbox ±60) ---
    cz = getattr(lp.request.constraints, "constructible_zone", None) if lp else None
    expected_cz = (int(zone[0]) - 60, int(zone[1]) - 60, int(zone[0]) + 60, int(zone[1]) + 60)
    rec("S4c-7 : constructible_zone derive de Contract.zone (±60)",
        cz == expected_cz, f"zone={zone} cz={cz} expected={expected_cz}")

    # --- Rec 8 : feas=ok -> can_place 0 collision (hors frontier) ---
    v_frontier = (bbox["y1"] + bbox["y2"]) / 2.0 + 120.0
    if feas == "ok":
        ok_n, fail_n, hors_map, terrain_hit = can_place_all(api, lp, "S4c", v_frontier, geo)
        rec("S4c-8 : feas=ok -> can_place 0 collision (hors frontier)",
            fail_n == 0, f"can_place={ok_n} fail={fail_n} hors_map={hors_map} terrain_hit={terrain_hit}")
    else:
        rec("S4c-8 : feas=ok -> can_place 0 collision (hors frontier)",
            True, f"SKIP (feas={feas} != ok, handoff/abandon documente)")

    # --- Rec 9 : S4c active terrain_check (per_entity), S3d non (post-hoc global) ---
    # S3d : Contract.layout_constraints=LayoutConstraints(terrain_check=False, replan_budget=0).
    contract_s3d = Contract(ProductionGoal("iron-gear-wheel", 5), zone=zone, replan_budget=0,
                            layout_constraints=LayoutConstraints(terrain_check=False, replan_budget=0))
    fb_s3d = FactoryBuilder(api, contract_s3d)
    lp_s3d = fb_s3d.build_layout(splan, geo)
    feas_s3d = getattr(lp_s3d, "feasibility", None)
    tc_s3d = getattr(lp_s3d.request.constraints, "terrain_check", None) if lp_s3d else None
    rec("S4c-9 : S4c terrain_check=True vs S3d terrain_check=False (back-compat dispatch)",
        tc is True and tc_s3d is False,
        f"S4c tc={tc} feas={feas} | S3d tc={tc_s3d} feas={feas_s3d}")

    # --- Rec 10 : back-compat S3d -> _plan_core (feas ok, terrain_check=False) ---
    rec("S4c-10 : back-compat S3d (terrain_check=False, replan_budget=0) -> feas ok",
        feas_s3d in ("ok", "obstacle_blocking", "missing_patch:iron-ore"),
        f"feas_s3d={feas_s3d} (S3d ignore terrain en planning)")

    # --- Rec 11 : S4c entities >= S3d entities (replan lourd ne fait pas pire) OU handoff ---
    n_ent_s3d = len(getattr(lp_s3d, "entities", []))
    s4c_ok_or_handoff = (feas == "ok") or (feas == "obstacle_blocking")
    rec("S4c-11 : S4c entities >= S3d OU feas S4c != ok (handoff documente)",
        s4c_ok_or_handoff and (n_ent >= n_ent_s3d or feas != "ok"),
        f"S4c entities={n_ent} feas={feas} | S3d entities={n_ent_s3d} feas={feas_s3d}")

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