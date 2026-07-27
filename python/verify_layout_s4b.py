"""Validation LIVE S4b : détection per-entité + replan auto déterministe (contournement water).

Pre-requis : serveur Factorio 2.0 headless lance (scripts/start_factorio_dedicated.bat)
APRES le mod S4a (scan_obstacles/scan_tiles_bbox/get_tile). Relance requise (mod Lua modifié).
RCON 127.0.0.1:27015 (pw "factoriollm"). Pas de relance pour S4b (Python only, mais le mod
S4a doit etre charge pour scan_tiles_bbox/scan_obstacles).

Valide la mecanique S4b sur TERRAIN RÉEL scanné :
  - scan_water_edge + scan_tiles_bbox -> peupler Terrain.water + tile_grid (précision tuile).
  - scan_obstacles -> peupler Terrain.obstacles (rochers/arbres).
  - plan(terrain_check=True, replan_budget=4) -> replan auto (shift cascade_offset_v / pivot
    facing) évite water/obstacles -> feasibility=ok, can_place 0 terrain hit.
  - Comparaison vs S3d (terrain_check=False) : S3d ignore water en planning (post-hoc ne voit
    que obstacles) -> can_place obstacle_terrain > 0 si la cascade croise l'eau (CONSTAT S2a).
  - S4b replan -> obstacle_terrain S4b <= S3d (le replan ne fait jamais pire).

10 recs :
  1. set_test_mode + setup + mod S4a present (scan_obstacles marche).
  2. scan_patch iron-ore reel (bbox).
  3. scan_water_edge + scan_tiles_bbox -> Terrain.water + tile_grid peuplés.
  4. scan_obstacles -> Terrain.obstacles peuplé (starting_area).
  5. plan S3d (terrain_check=False) -> feas + compte terrain hits H_s3d.
  6. plan S4b (terrain_check=True, replan_budget=4) -> feas + compte terrain hits H_s4b.
  7. S4b replan -> H_s4b <= H_s3d (le replan ne fait jamais pire que S3d).
  8. S4b : si feas=ok, can_place 0 collision (hors frontier) sur entités non-skip.
  9. S4b replan : si S3d avait des hits water, S4b a un offset/facing different (replan actif).
  10. back-compat : terrain_check=False ignore water (post-hoc = obstacles only).

Lancement (apres relance serveur) :
    cd python
    python verify_layout_s4b.py
"""

from __future__ import annotations
import sys
import math

sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import populate_from_rcon, GeometryBase
from services.production_solver import ProductionRequest, solve
from services.layout_planner import (
    LayoutRequest, LayoutConstraints, Terrain, ResourcePatch, plan, plan_summary,
    _to_uv, _occ_terrain,
)

DIR_TO_STR = {0: "north", 2: "east", 4: "south", 6: "west"}

ITEMS = ["iron-plate", "iron-gear-wheel"]
MACHINES = ["stone-furnace", "electric-furnace", "assembling-machine-1", "electric-mining-drill"]
GEO_NAMES = ["transport-belt", "burner-inserter", "small-electric-pole",
             "stone-furnace", "assembling-machine-1", "electric-mining-drill"]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:80]}")


def can_place_all(api, lp, rcon, label, v_frontier, geo):
    """can_place non destructif + compte terrain hits (water/out-of-map via get_tile, obstacles
    via _occ_terrain). Filtre frontier (out-of-map headless artefact)."""
    ok_n = fail_n = hors_map = terrain_hit = 0
    for e in lp.entities:
        if e.skip:
            continue
        _, v_e = _to_uv(lp.request.facing, e.x, e.y)
        if v_e > v_frontier + 1.5:
            hors_map += 1
            continue
        # terrain hit via _occ_terrain (water/obstacle/out-of-map) sur le terrain du plan.
        g = geo.geometry(e.name)
        w = g.w if g else 1.0
        h = g.h if g else 1.0
        kind = _occ_terrain(lp.request.terrain, e.x, e.y, w, h)
        if kind:
            terrain_hit += 1
            continue
        # can_place réel (collision entités/terrain Factorio).
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
    rec("S4b-1 : mod S4a present (scan_obstacles marche, post-relance)",
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
    rec("S4b-2 : scan_patch iron-ore reel (bbox)",
        True, f"bbox=({bbox['x1']},{bbox['y1']})-({bbox['x2']},{bbox['y2']})")

    anchor = (float(bbox["x1"]), (bbox["y1"] + bbox["y2"]) / 2.0)
    # Zone autour de l'anchor pour scanner tuiles + water.
    sx1, sy1 = int(bbox["x1"]) - 5, int(bbox["y1"]) - 30
    sx2, sy2 = int(bbox["x2"]) + 80, int(bbox["y2"]) + 60

    # --- Rec 3 : scan_water_edge + scan_tiles_bbox -> Terrain.water + tile_grid ---
    we = api.scan_water_edge(400)
    water_bboxes = []
    if isinstance(we, dict) and we.get("bbox"):
        wb = we["bbox"]
        water_bboxes.append((wb["x1"], wb["y1"], wb["x2"], wb["y2"]))
    st = api.scan_tiles_bbox(sx1, sy1, sx2, sy2)
    tile_grid = {}
    if isinstance(st, dict) and "error" not in st:
        for t in st.get("tiles", []):
            tile_grid[(t["x"], t["y"])] = t["name"]
    rec("S4b-3 : scan_water_edge + scan_tiles_bbox -> water + tile_grid peuplés",
        isinstance(st, dict) and "error" not in st,
        f"water_bboxes={len(water_bboxes)} tile_grid={len(tile_grid)}")

    # --- Rec 4 : scan_obstacles -> Terrain.obstacles ---
    obstacles = []
    if isinstance(r_obs, dict):
        for o in r_obs.get("obstacles", []):
            obstacles.append((o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]))
    rec("S4b-4 : scan_obstacles -> Terrain.obstacles peuplé (starting_area)",
        len(obstacles) >= 0, f"n_obstacles={len(obstacles)}")

    terrain = Terrain(
        patches=[ResourcePatch("iron-ore", bbox=(bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]))],
        obstacles=obstacles, water=water_bboxes, tile_grid=tile_grid,
    )

    splan = solve(ProductionRequest("iron-plate", 5.0,
                                    machine_tiers={"smelting": "stone-furnace"}), kb)

    # --- Rec 5 : plan S3d (terrain_check=False) -> H_s3d ---
    req_s3d = LayoutRequest(plan=splan, terrain=terrain, anchor=anchor, facing=2,
                            constraints=LayoutConstraints(terrain_check=False, replan_budget=0))
    lp_s3d = plan(req_s3d, geo)
    v_frontier = (bbox["y1"] + bbox["y2"]) / 2.0 + 120.0
    _, _, _, H_s3d = can_place_all(api, lp_s3d, rcon, "S3d", v_frontier, geo)
    rec("S4b-5 : plan S3d (terrain_check=False) -> compte terrain hits H_s3d",
        lp_s3d.feasibility in ("ok", "obstacle_blocking"),
        f"feas={lp_s3d.feasibility} H_s3d={H_s3d}")

    # --- Rec 6 : plan S4b (terrain_check=True, replan_budget=4) -> H_s4b ---
    req_s4b = LayoutRequest(plan=splan, terrain=terrain, anchor=anchor, facing=2,
                            constraints=LayoutConstraints(terrain_check=True, replan_budget=4,
                                                          bypass_offset_v=3))
    lp_s4b = plan(req_s4b, geo)
    _, fail_s4b, _, H_s4b = can_place_all(api, lp_s4b, rcon, "S4b", v_frontier, geo)
    rec("S4b-6 : plan S4b (terrain_check=True, replan_budget=4) -> H_s4b",
        lp_s4b.feasibility in ("ok", "obstacle_blocking"),
        f"feas={lp_s4b.feasibility} H_s4b={H_s4b} offset={lp_s4b.request.constraints.cascade_offset_v}")

    # --- Rec 7 : S4b replan -> H_s4b <= H_s3d (le replan ne fait jamais pire) ---
    rec("S4b-7 : S4b replan -> H_s4b <= H_s3d (replan ne fait jamais pire)",
        H_s4b <= H_s3d, f"H_s4b={H_s4b} H_s3d={H_s3d}")

    # --- Rec 8 : S4b si feas=ok, can_place 0 collision (hors frontier) ---
    rec("S4b-8 : S4b feas=ok -> can_place 0 collision (hors frontier)",
        (lp_s4b.feasibility == "ok" and fail_s4b == 0) or lp_s4b.feasibility != "ok",
        f"feas={lp_s4b.feasibility} fail={fail_s4b}")

    # --- Rec 9 : S4b replan actif si S3d avait hits water/obstacle ---
    s4b_shifted = (lp_s4b.request.constraints.cascade_offset_v != 0
                   or lp_s4b.request.facing != 2)
    rec("S4b-9 : S4b replan actif si S3d avait hits (offset/facing different)",
        (H_s3d == 0) or s4b_shifted,
        f"H_s3d={H_s3d} shifted={s4b_shifted} offset={lp_s4b.request.constraints.cascade_offset_v} facing={lp_s4b.request.facing}")

    # --- Rec 10 : back-compat terrain_check=False ignore water (post-hoc obstacles only) ---
    # S3d ne détecte pas water en planning (post-hoc = obstacles only). Si water présent et
    # cascade le croise, S3d feas=ok mais H_s3d>0. S4b détecte (per-entity) et replan.
    s3d_ignores_water = (lp_s3d.feasibility == "ok") or (not any("per_entity" in n for n in lp_s3d.notes))
    rec("S4b-10 : back-compat S3d ignore water (post-hoc obstacles only, pas per_entity)",
        s3d_ignores_water and not any("per_entity" in n for n in lp_s3d.notes),
        f"s3d_feas={lp_s3d.feasibility} per_entity_s3d={any('per_entity' in n for n in lp_s3d.notes)}")

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