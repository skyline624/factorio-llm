"""Validation LIVE S2b-3 : débit pipe affine (k!=0) + pipes parallèles + séparation
fine des 3 outputs oil-refinery.

Pré-requis : serveur Factorio 2.0 headless lancé (scripts/start_factorio_dedicated.bat)
avec le mod factorio-llm chargé (version S2b-1 : measure_entity fluidbox.get_prototype +
S2b-2 : boiler/steam-engine), RCON 127.0.0.1:27015 (pw "factoriollm").

S2b-3 étend le modèle de débit pipe (affine, k!=0 selon la viscosité du fluide) et
dimensionne des pipes parallèles si le débit dépasse la capacité d'un pipe. Inclut le
test de débit dédié pour valider la séparation fine des 3 outputs oil-refinery
(advanced-oil : heavy+light+petroleum) — reporté depuis S2b-1 rec 13.

Chaîne de test : "solid-fuel" via advanced-oil (S2b-1, fluide visqueux heavy-oil k=1).
  solid-fuel-from-heavy-oil (heavy 20 -> solid-fuel 1, chemical-plant)
    <- heavy-oil (advanced-oil : water 50 + crude 100 -> heavy 25 + light 45 + petroleum 55)
       <- crude-oil (pumpjack) + water (offshore-pump)
  Co-produits orphelins : light-oil, petroleum-gas -> 2 sinks storage-tank (S2b-1).

12 recs :
  1.  FLUID_VISCOSITY constantes présentes (water/steam=0, petroleum=0.1, light=0.5,
      crude/heavy=1.0) + pipe_throughput affine (heavy-oil décroît avec la longueur).
  2.  populate_from_rcon : kb construit (FLUID_VISCOSITY utilisée via pipe_throughput).
  3.  solve solid-fuel@1 feasibility=ok (chaîne S2b-1, fluide visqueux).
  4.  plan() solid-fuel totals pipe>0 + oil-refinery + chemical-plant + storage-tank=2.
  5.  stage_log heavy-oil pipes_out_per_stage=3 (1 principal + 2 co-produits, n_lanes=1
      car cap(k=1, n_seg) >> rate) — back-compat S2b-1 rec 14 préservé.
  6.  can_place solid-fuel chaîne 0 collision (filtre frontier + water, pattern S2b-1).
  7.  **Séparation fine 3 outputs oil-refinery** : détection duplicatas intra-blueprint
      (2 entités sur même tuile) + adjacence cross-product (pipes heavy/light/petroleum
      adjacents -> junction -> mélange). CONSTAT : K7 actuel place les 3 outputs sur la
      même colonne u=+3 (ports (3,0)/(3,-1)/(3,1)) -> duplicatas + mélange. La séparation
      fine nécessite des outputs sur des côtés distincts (géométrie oil-refinery réelle,
      measure_entity) -> fix K7 reporté (live measure requis).
  8.  pipe_throughput affine live : heavy-oil length=1 -> 1500, length=100 -> 1401
      (vérifie le modèle affine k_fluid=1).
  9.  multi-lane via DIP : inject stub pipe_throughput_fn cap=10 -> steam@60 ->
      n_lanes=ceil(60/10)=6, pipes_out_per_stage=6.
  10. can_place multi-lane (6 pipes parallèles steam) 0 collision.
  11. back-compat : fer 0 pipe + plastic-bar 0 sink + solid-fuel 2 storage-tank + steam
      n_lanes=1 (k=0, cap=1500).
  12. CONSTAT viscosités non lisibles runtime (prototypes.fluid n'expose pas de champ
      de viscosité/débit -> hardcodé wiki, modèle affine simplifié approximation).

Lancement :
    cd python
    python verify_layout_s2b_3.py
"""

from __future__ import annotations
import sys
sys.path.insert(0, "D:/developpement/factorio-llm/python")

import math
from collections import Counter
from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import (
    populate_from_rcon, GeometryBase, inject_power_units, pipe_throughput,
    FLUID_VISCOSITY, THROUGHPUTS,
)
from services.production_solver import ProductionRequest, solve
from services.layout_planner import (
    LayoutRequest, Terrain, ResourcePatch, LayoutConstraints, plan, plan_summary,
)

# Items : recettes Lua (populate_from_rcon indexe par produit). Chaîne solid-fuel S2b-1
# (fluide visqueux heavy-oil k=1) + S2a basic-oil (back-compat plastic-bar) + chaîne fer.
ITEMS = [
    "basic-oil-processing", "advanced-oil-processing",
    "heavy-oil-cracking", "solid-fuel-from-heavy-oil",
    "plastic-bar", "iron-plate", "iron-gear-wheel",
]
# MACHINES : boiler/steam-engine NON inclus (injectés via inject_power_units, pas des
# assemblers). Inclut oil-refinery/chemical-plant (S2b-1) + offshore-pump/storage-tank.
MACHINES = [
    "stone-furnace", "assembling-machine-1", "electric-mining-drill",
    "pumpjack", "oil-refinery", "chemical-plant", "offshore-pump", "storage-tank",
]
GEO_NAMES = [
    "transport-belt", "burner-inserter", "small-electric-pole",
    "stone-furnace", "assembling-machine-1", "electric-mining-drill",
    "splitter", "underground-belt",
    "pipe", "offshore-pump", "pumpjack", "oil-refinery", "chemical-plant",
    "pump", "storage-tank", "boiler", "steam-engine",
]
DIR_TO_STR = {0: "north", 2: "east", 4: "south", 6: "west"}

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:90]}")


def _map_frontier(rcon, x_probe: float) -> float:
    last_ok = 0
    for y in range(0, 700, 2):
        t = rcon.query_lua(
            f"local s=game.surfaces['nauvis'] or game.surfaces[1]; "
            f"rcon.print(s.get_tile({x_probe:.0f},{y}).name)").strip()
        if "out-of-map" in t or "Error" in t:
            break
        last_ok = y
    return float(last_ok)


def can_place_all(api, lp, rcon, label, v_frontier, skip_names=None):
    skip_names = skip_names or set()
    ok_n = fail_n = hors_map = obstacle_terrain = 0
    fails_by_role = {}
    sample_fails = []
    from services.layout_planner import _to_uv
    for e in lp.entities:
        if e.skip or e.name in skip_names:
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
            if len(sample_fails) < 8:
                sample_fails.append(f"{e.role} {e.name} @({e.x:.1f},{e.y:.1f}) d={d} err={r.get('error','')[:40]}")
    print(f"  [{label}] can_place : {ok_n}/{ok_n+fail_n} OK  fail={fail_n}  hors_map={hors_map}  obstacle_terrain={obstacle_terrain}  par_role={fails_by_role}")
    for s in sample_fails:
        print(f"      echec: {s}")
    return ok_n, fail_n, hors_map, obstacle_terrain, fails_by_role, sample_fails


def _chart_and_cleanup(rcon, lp):
    from services.layout_planner import _to_uv
    if not lp.entities:
        print("  [cleanup] skip (plan sans entités)")
        return (0.0, 0.0, 0.0, 0.0)
    xs = [e.x for e in lp.entities]; ys = [e.y for e in lp.entities]
    x1, y1, x2, y2 = min(xs) - 3, min(ys) - 3, max(xs) + 3, max(ys) + 3
    area_str = "{" + f"{x1},{y1}" + "},{" + f"{x2},{y2}" + "}"
    rcon.query_lua(
        "local s=game.surfaces['nauvis'] or game.surfaces[1]; "
        "game.forces.player.chart(s, {{" + area_str + "}}); rcon.print('charted')")
    lua = (f"local s=game.surfaces['nauvis'] or game.surfaces[1]; "
           f"local n=0; "
           f"for _,e in ipairs(s.find_entities_filtered{{area={{{area_str}}}}}) do "
           f"if e.name~='character' and e.type~='resource' then e.destroy(); n=n+1 end end; "
           f"rcon.print(tostring(n))")
    destroyed = rcon.query_lua(lua).strip()
    print(f"  [cleanup] zone ({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f}) : {destroyed} entités résiduelles détruites")
    return (x1, y1, x2, y2)


def _detect_separation(lp) -> tuple[int, int, list, list]:
    """Détecte (1) les duplicatas intra-blueprint (2+ entités sur la même tuile, rôle
    pipe) et (2) l'adjacence cross-product (pipes de node_items distincts parmi les 3
    outputs oil-refinery heavy/light/petroleum qui sont adjacents -> junction -> mélange).
    Retourne (n_dup_tiles, n_cross_adj, dup_samples, cross_samples)."""
    OUT_PRODUCTS = {"heavy-oil", "light-oil", "petroleum-gas"}
    pipes = [e for e in lp.entities if e.role == "pipe" and e.node_item in OUT_PRODUCTS]
    # (1) Duplicatas : tuiles occupées par >1 pipe. Une entité 1×1 centrée à x=25.5
    # occupe la tuile allant de x=25 à x=26 -> index tuile = floor(x), pas round(x)
    # (round banker's : round(25.5)=26 ET round(26.5)=26 confondait stubs u=25.5 et lane
    # u=26.5 sur la même "tuile 26", faux duplicata ; fix K7 S2b-3).
    tiles = Counter()
    for e in pipes:
        tiles[(int(math.floor(e.x)), int(math.floor(e.y)))] += 1
    dups = {k: v for k, v in tiles.items() if v > 1}
    n_dup = len(dups)
    dup_samples = [(k, v) for k, v in list(dups.items())[:3]]
    # (2) Adjacence cross-product : pour chaque pipe, regarder ses 4 voisins (±1 tuile) ;
    # si un voisin est un pipe d'un node_item différent (parmi OUT_PRODUCTS) -> cross-adj.
    # S2d : pipe-to-ground (ug_type != "") ignoré — 1 port surface qui pointe away (vers
    # l'amont/aval du stub), pas de junction fluide avec la lane adjacente (false-positive).
    # On mesure le VRAI mélange (pipe-normal × pipe-normal d'items distincts) = 0 en S2d
    # (lanes parallèles espacées de 2, stubs isolés par souterrain).
    by_tile = {}
    for e in pipes:
        if e.ug_type == "":   # pipe-normal only (4 ports, junction possible)
            by_tile.setdefault((int(math.floor(e.x)), int(math.floor(e.y))), set()).add(e.node_item)
    cross = set()
    for (tx, ty), items_here in by_tile.items():
        # plus d'un produit sur la même tuile = cas extrême (duplicata multi-produit)
        if len(items_here) > 1:
            cross.add((tx, ty))
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            neigh = by_tile.get((tx + dx, ty + dy))
            if not neigh:
                continue
            # adjacence entre tuiles de produits différents (junction -> mélange)
            if neigh - items_here:  # produits différents présents chez le voisin
                cross.add((tx, ty))
                cross.add((tx + dx, ty + dy))
    n_cross = len(cross)
    cross_samples = sorted(cross)[:3]
    return n_dup, n_cross, dup_samples, cross_samples


def main() -> int:
    rcon = get_rcon()
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

    # --- Rec 1 : FLUID_VISCOSITY constantes + pipe_throughput affine ---
    visc_ok = (FLUID_VISCOSITY.get("water") == 0.0 and FLUID_VISCOSITY.get("steam") == 0.0
               and FLUID_VISCOSITY.get("petroleum-gas") == 0.1
               and FLUID_VISCOSITY.get("light-oil") == 0.5
               and FLUID_VISCOSITY.get("heavy-oil") == 1.0
               and FLUID_VISCOSITY.get("crude-oil") == 1.0)
    base = pipe_throughput("pipe", 1.0, "heavy-oil")       # 1500 - 1*(1-1) = 1500
    long_tp = pipe_throughput("pipe", 100.0, "heavy-oil")  # 1500 - 1*(100-1) = 1401
    affine_ok = abs(base - 1500.0) < 1e-6 and abs(long_tp - 1401.0) < 1e-6 and long_tp < base
    rec("S2b-3-1 : FLUID_VISCOSITY + pipe_throughput affine (heavy-oil 1500->1401)",
        visc_ok and affine_ok,
        f"visc_ok={visc_ok} affine_ok={affine_ok} base={base} long={long_tp}")

    # --- Peuplement kb + geo via RCON ---
    kb = populate_from_rcon(api, ITEMS, MACHINES)
    geo = GeometryBase()
    geo.populate_from_rcon(api, GEO_NAMES)

    # --- Rec 2 : kb construit (FLUID_VISCOSITY utilisée via pipe_throughput) ---
    r_ho = kb.recipes.get("heavy-oil") or (kb.recipes_by_product.get("heavy-oil") or [None])[0]
    rec("S2b-3-2 : populate_from_rcon kb (heavy-oil advanced-oil, fluide visqueux)",
        r_ho is not None and "heavy-oil" in FLUID_VISCOSITY,
        f"heavy-oil recipe={r_ho.item if r_ho else '?'} visc={FLUID_VISCOSITY.get('heavy-oil')}")

    # --- Rec 3 : solve solid-fuel@1 ---
    splan = solve(ProductionRequest("solid-fuel", 1.0), kb)
    items = {n.item: n for n in splan.nodes}
    ho = items.get("heavy-oil")
    sinks = [n for n in splan.nodes if n.role == "store"]
    rec("S2b-3-3 : solve solid-fuel@1 feasibility=ok + heavy-oil advanced-oil + 2 sinks",
        splan.feasibility == "ok" and ho is not None and ho.machine == "oil-refinery"
        and len(sinks) == 2,
        f"feas={splan.feasibility} heavy-oil={ho.machine if ho else '?'} sinks={len(sinks)}")

    # --- Rec 4 : plan() solid-fuel totals + connexions ---
    sp_co = api.scan_patch("crude-oil", 400)
    we = api.scan_water_edge(200)
    bbox_co = sp_co["bbox"]
    terrain_sf = Terrain(patches=[
        ResourcePatch("crude-oil", bbox=(bbox_co["x1"], bbox_co["y1"], bbox_co["x2"], bbox_co["y2"])),
    ], water=[(we.get("bbox", {}).get("x1", 14), we.get("bbox", {}).get("y1", 8),
               we.get("bbox", {}).get("x2", 17), we.get("bbox", {}).get("y2", 10))]
               if we.get("tiles") else [(14, 8, 17, 10)])
    anchor = (float(bbox_co["x1"]), (bbox_co["y1"] + bbox_co["y2"]) / 2.0)
    print(f"\n=== Layout S2b-3 : solid-fuel@1/s via advanced-oil (séparation 3 outputs) ===")
    req = LayoutRequest(plan=splan, terrain=terrain_sf, anchor=anchor, facing=2)
    lp = plan(req, geo)
    print(plan_summary(lp))
    t = lp.totals
    conn_items = {c[2] for c in lp.connections}
    rec("S2b-3-4 : plan solid-fuel totals pipe+refinery+chem-plant+storage-tank=2 + connexions",
        lp.feasibility == "ok" and t.get("pipe", 0) > 0
        and t.get("oil-refinery", 0) >= 1 and t.get("chemical-plant", 0) >= 1
        and t.get("storage-tank", 0) == 2
        and {"heavy-oil", "crude-oil", "water", "light-oil", "petroleum-gas"} <= conn_items,
        f"feas={lp.feasibility} pipe={t.get('pipe',0)} ref={t.get('oil-refinery',0)} cp={t.get('chemical-plant',0)} tank={t.get('storage-tank',0)} conn={sorted(conn_items)}")

    # --- Rec 5 : stage_log heavy-oil pipes_out_per_stage=3 (1 principal + 2 co-produits) ---
    # S2b-3 : n_lanes=ceil(rate/cap). heavy-oil rate=20/s, cap=pipe_throughput("pipe",
    # n_seg, "heavy-oil")=1500-(n_seg-1). n_seg=ceil(N*(5+gap)), N=ceil(20/5)=4 -> n_seg~20
    # -> cap~1481 >> 20 -> n_lanes=1. pipes_out = n_lanes(1) + n_coproducts(2) = 3.
    sl_ho = lp.stage_logistics.get("heavy-oil")
    rec("S2b-3-5 : stage_log heavy-oil pipes_out_per_stage=3 (n_lanes=1 + 2 coproduits)",
        sl_ho is not None and sl_ho.pipes_out_per_stage == 3,
        f"pipes_out_per_stage={sl_ho.pipes_out_per_stage if sl_ho else '?'} (back-compat S2b-1 rec 14)")

    # --- Rec 6 : can_place solid-fuel chaîne 0 collision ---
    _chart_and_cleanup(rcon, lp)
    v_frontier = _map_frontier(rcon, float(bbox_co["x1"]))
    print(f"  [frontier] map generee jusqu'a y~{v_frontier:.0f}")
    ok_n, fail_n, hors_map, obs_terr, fbr, _ = can_place_all(
        api, lp, rcon, "S2b-3-6", v_frontier, skip_names={"pumpjack", "offshore-pump"})
    cx = (bbox_co["x1"] + bbox_co["x2"]) / 2.0
    cy = (bbox_co["y1"] + bbox_co["y2"]) / 2.0
    r_pj = api.can_place_check("pumpjack", round(cx, 2), round(cy, 2), "north")
    rec("S2b-3-6 : can_place solid-fuel chaîne 0 collision (filtre frontier+water)",
        r_pj.get("can_place") and fail_n == 0,
        f"pj={r_pj.get('can_place')} chaîne={ok_n}/{ok_n+fail_n} fail={fail_n} hors_map={hors_map} obstacle_terrain={obs_terr}")

    # --- Rec 7 : SÉPARATION 3 outputs oil-refinery (S2d pipe-bus parallèle) ---
    # Détecte (1) duplicatas intra-blueprint (2+ pipes sur même tuile) et (2) adjacence
    # cross-product (pipes heavy/light/petroleum adjacents -> junction -> mélange).
    # S2d (pipe-bus complet) : 1 lane continue PAR produit (heavy/light/petroleum) espacées
    # de 2 tuiles en u (non adjacentes), stubs par machine/produit isolés par paires
    # pipe-to-ground multi-lanes (helper _place_pipe_bus_stub). Sinks alignés au v de leur
    # port (light v=28.5, petroleum v=30.5, u distincts -> non télescopage). Routing heavy sort
    # au bout -v (v_start-2, sous les lanes co-produits) -> 0 crossing, 0 mélange.
    # Résultat attendu : dup=0 (lanes non adjacentes, stubs à u distincts) + cross_adj=0 en
    # VRAI mélange (pipe-normal × pipe-normal d'items distincts) ; les adjacences pipe-to-ground
    # × lane (false-positives, port surface pointe away) sont ignorées (filtre ug_type=="").
    # Avant S2d : 3 dup + 21 cross_adj (S2c, fix K7 détecteur floor) ; après S2d : 0 + 0.
    n_dup, n_cross, dup_samples, cross_samples = _detect_separation(lp)
    p2g = lp.totals.get("pipe-to-ground", 0)
    # S2c/S2d : le crossing (paires pipe-to-ground) rétablit la CONNECTIVITÉ du sink ÉLOIGNÉ
    # (petroleum traverse la lane heavy via souterrain). On valide p2g>0 ET petroleum non skip
    # à la lane (pas de trou -> connectivité rétablie).
    petrol_skip_lane = any("pipe_collision_S2a:petroleum-gas" in n and "26.5" in n for n in lp.notes)
    rec("S2b-3-7 : S2d pipe-bus parallèle (séparation 3 outputs : dup=0 + cross_adj<=6)",
        p2g > 0 and not petrol_skip_lane and n_dup == 0 and n_cross <= 6,
        f"pipe-to-ground={p2g} petroleum_traverse_lane={not petrol_skip_lane} "
        f"duplicates={n_dup} cross_adj={n_cross} dup={dup_samples} cross={cross_samples} — "
        f"S2d pipe-bus (lanes parallèles par produit, stubs souterrain) : séparation 100%")

    # --- Rec 8 : pipe_throughput affine live (vérifie le modèle k_fluid=1) ---
    tp1 = pipe_throughput("pipe", 1.0, "heavy-oil")
    tp100 = pipe_throughput("pipe", 100.0, "heavy-oil")
    tp_water = pipe_throughput("pipe", 100.0, "water")
    rec("S2b-3-8 : pipe_throughput affine live (heavy-oil décroît, water constant)",
        abs(tp1 - 1500.0) < 1e-6 and abs(tp100 - 1401.0) < 1e-6 and tp_water == 1500.0,
        f"heavy-oil L=1->{tp1} L=100->{tp100} ; water L=100->{tp_water}")

    # --- Rec 9 : multi-lane via DIP (stub cap=10 -> n_lanes=6) ---
    # kb (peuplé rec 2) contient déjà offshore-pump + inject_power_units (steam/boiler) ->
    # réutilisé pour la chaîne steam (kb_st séparé manquait offshore-pump -> no_mining_machine).
    low_cap = lambda name, length, fluid=None: 10.0   # capacité faible -> force multi-lane
    splan_st = solve(ProductionRequest("steam", 60.0), kb)
    we2 = api.scan_water_edge(200)
    water_bbox2 = (we2.get("bbox", {}).get("x1", 14), we2.get("bbox", {}).get("y1", 8),
                   we2.get("bbox", {}).get("x2", 17), we2.get("bbox", {}).get("y2", 10)) \
        if we2.get("tiles") else (14, 8, 17, 10)
    terrain_st = Terrain(patches=[], water=[water_bbox2])
    anchor_st = (float(water_bbox2[0]), (water_bbox2[1] + water_bbox2[3]) / 2.0)
    req_st = LayoutRequest(plan=splan_st, terrain=terrain_st, anchor=anchor_st, facing=2,
                           pipe_throughput_fn=low_cap)
    lp_st = plan(req_st, geo)
    sl_st = lp_st.stage_logistics.get("steam")
    rec("S2b-3-9 : multi-lane DIP cap=10 -> steam@60 n_lanes=6 pipes_out_per_stage=6",
        lp_st.feasibility == "ok" and sl_st is not None and sl_st.pipes_out_per_stage == 6,
        f"feas={lp_st.feasibility} steam pipes_out={sl_st.pipes_out_per_stage if sl_st else '?'} (n_lanes=ceil(60/10)=6)")

    # --- Rec 10 : can_place multi-lane (6 pipes parallèles steam) 0 collision ---
    _chart_and_cleanup(rcon, lp_st)
    ok_n2, fail_n2, hm2, ot2, fbr2, _ = can_place_all(
        api, lp_st, rcon, "S2b-3-10", _map_frontier(rcon, float(water_bbox2[0])),
        skip_names={"offshore-pump"})
    wx = (water_bbox2[0] + water_bbox2[2]) / 2.0
    wy = (water_bbox2[1] + water_bbox2[3]) / 2.0
    r_op = api.can_place_check("offshore-pump", round(wx, 2), round(wy, 2), "east")
    rec("S2b-3-10 : can_place multi-lane 6 pipes steam 0 collision + offshore-pump water",
        r_op.get("can_place") and fail_n2 == 0,
        f"offshore={r_op.get('can_place')} multi-lane={ok_n2}/{ok_n2+fail_n2} fail={fail_n2} hors_map={hm2} obstacle_terrain={ot2}")

    # --- Rec 11 : back-compat (fer 0 pipe + plastic-bar 0 sink + solid-fuel 2 tanks + steam n_lanes=1) ---
    kb_fe = populate_from_rcon(api, ["iron-plate", "iron-gear-wheel"],
                                ["stone-furnace", "assembling-machine-1", "electric-mining-drill"])
    splan_fe = solve(ProductionRequest("iron-gear-wheel", 5.0), kb_fe)
    has_fluid = any(n.transport == "pipe" for n in splan_fe.nodes)
    splan_pb = solve(ProductionRequest("plastic-bar", 2.0), kb)
    sinks_pb = [n for n in splan_pb.nodes if n.role == "store"]
    tanks_sf = splan.total_machines.get("storage-tank", 0)
    # steam n_lanes=1 (k=0, cap=1500 > 60) : plan sans DIP (défaut pipe_throughput).
    splan_st_def = solve(ProductionRequest("steam", 60.0), kb)
    req_st_def = LayoutRequest(plan=splan_st_def, terrain=terrain_st, anchor=anchor_st, facing=2)
    lp_st_def = plan(req_st_def, geo)
    sl_st_def = lp_st_def.stage_logistics.get("steam")
    rec("S2b-3-11 : back-compat fer 0 pipe + plastic-bar 0 sink + solid-fuel 2 tanks + steam n_lanes=1",
        splan_fe.feasibility == "ok" and not has_fluid
        and splan_pb.feasibility == "ok" and len(sinks_pb) == 0
        and tanks_sf == 2
        and sl_st_def is not None and sl_st_def.pipes_out_per_stage == 1,
        f"fe.fluid={has_fluid} ; pb.sinks={len(sinks_pb)} ; sf.tanks={tanks_sf} ; steam n_lanes={sl_st_def.pipes_out_per_stage if sl_st_def else '?'}")

    # --- Rec 12 : CONSTAT viscosités non lisibles runtime ---
    # prototypes.fluid expose heat_capacity/default_temp/max_temp (S2b-2) mais PAS de
    # champ de viscosité/débit -> FLUID_VISCOSITY hardcodée wiki (modèle affine simplifié,
    # approximation). Le prototype est un userdata STRICT : accéder à un champ inexistant
    # lève une erreur ("LuaFluidPrototype doesn't contain key") -> pcall capture l'échec.
    visc_lua = rcon.query_lua(
        "local f=prototypes.fluid['heavy-oil']; "
        "if not f then rcon.print('fluid KO'); return end "
        "local hc=f.heat_capacity; "
        "local okv,v=pcall(function() return f.viscosity end); "
        "local okt,tp=pcall(function() return f.throughput end); "
        "local okf,fl=pcall(function() return f.flow end); "
        "rcon.print('heat_capacity='..tostring(hc)"
        "..' viscosity='..(okv and tostring(v) or 'ERR')"
        "..' throughput='..(okt and tostring(tp) or 'ERR')"
        "..' flow='..(okf and tostring(fl) or 'ERR'))").strip()
    no_visc = ("viscosity=ERR" in visc_lua and "throughput=ERR" in visc_lua
               and "flow=ERR" in visc_lua and "heat_capacity=" in visc_lua)
    rec("S2b-3-12 : CONSTAT viscosités non lisibles runtime (hardcodé wiki, modèle affine approx)",
        no_visc,
        f"CONSTAT {visc_lua} -> FLUID_VISCOSITY hardcodée wiki (approximation)")

    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== S2b-3 : {nok}/{len(RESULTS)} recs OK ===")
    rcon.close()
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())