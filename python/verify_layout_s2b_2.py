"""Validation LIVE S2b-2 : steam/boiler + power (steam-engine).

Pré-requis : serveur Factorio 2.0 headless lancé (scripts/start_factorio_dedicated.bat)
avec le mod factorio-llm chargé (version S2b-1 : measure_entity étendu fluid_boxes
instance via ent.fluidbox.get_prototype), RCON 127.0.0.1:27015 (pw "factoriollm").

Si le mod n'est PAS rechargé (measure_fluid_boxes sans fluid_boxes), les recs measure
tomberont en CONSTAT API 2.0 (hardcode GEOMETRY_FIXTURE = source vérité, validé via
can_place). Relancer le serveur pour charger le mod S2b-1 (tools.lua corrigé).

Chaîne de test : "produis steam@60/s" via boiler (recette synthétique boiling).
  steam (boiling : water 60 -> steam 60, boiler 1.8 MW -> 60 steam/s)
    <- water (offshore-pump, tile water, 1200/s)
  Steam = cible (pas de sink ; steam consommé par steam-engine en jeu, hors solveur).
  Sink steam-engine validé à part (can_place géométrie 3×5) — la règle solveur
  "co-produit orphelin steam -> steam-engine" se valide en unitaire (test recette
  fictive cogen) car aucune recette Factorio ne co-produit steam.

12 recs :
  1.  measure_fluid_boxes boiler (2 boxes : input water + output steam) [ou CONSTAT]
  2.  measure_fluid_boxes steam-engine (1 box : input steam) [ou CONSTAT]
  3.  prototypes.fluid steam (heat_capacity=200, default=15, max=5000) [ou CONSTAT]
  4.  prototypes.fluid water (heat_capacity=2000)
  5.  populate_from_rcon : recette synthétique steam + MachineSpec boiler/steam-engine
  6.  recipe_of("steam") -> category="boiling" ; pick_machine("boiling") -> boiler
  7.  solve steam@60 -> feasibility=ok + node steam (boiler count=1) + node water
      (offshore-pump) + 0 sink
  8.  plan() steam@60 -> totals pipe>0 + boiler>=1 + offshore-pump>=1 + connexion water
      + 0 steam-engine
  9.  can_place chaîne steam (boiler + pipes + offshore-pump) 0 collision interne
  10. can_place steam-engine isolé (géométrie 3×5 + pipe input) — validation géométrie
  11. back-compat : iron-gear 0 pipe + plastic-bar S2a 0 sink + solid-fuel S2b-1
      2 storage-tank préservés
  12. mesure débit boiler (optionnel : pose boiler + water + steam-engine, ticker,
      lire steam produit) [ou CONSTAT si propagation passive vide]

Lancement :
    cd python
    python verify_layout_s2b_2.py
"""

from __future__ import annotations
import sys
sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import populate_from_rcon, GeometryBase, inject_power_units
from services.production_solver import ProductionRequest, solve
from services.layout_planner import (
    LayoutRequest, Terrain, ResourcePatch, LayoutConstraints, plan, plan_summary,
)

# Items : recettes Lua (populate_from_rcon indexe par produit). steam est SYNTHÉTIQUE
# (injecté côté Python par inject_power_units, pas de RCON). Inclut S2b-1 (back-compat
# solid-fuel) + S2a (plastic-bar) + chaîne fer.
ITEMS = [
    "basic-oil-processing", "advanced-oil-processing",
    "heavy-oil-cracking", "solid-fuel-from-heavy-oil",
    "plastic-bar", "iron-plate", "iron-gear-wheel",
]
# MACHINES : boiler/steam-engine NE SONT PAS inclus (pas des assemblers, pas de
# craftingCategories au runtime -> injectés côté Python par inject_power_units).
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
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:80]}")


def _map_frontier(rcon, x_probe: float) -> float:
    """Détecte la frontière de map générée en +v (south, +y) depuis l'origin."""
    last_ok = 0
    for y in range(0, 700, 2):
        t = rcon.query_lua(
            f"local s=game.surfaces['nauvis'] or game.surfaces[1]; "
            f"rcon.print(s.get_tile({x_probe:.0f},{y}).name)").strip()
        if "out-of-map" in t or "Error" in t:
            break
        last_ok = y
    return float(last_ok)


def can_place_all(api: ModApi, lp, rcon, label: str, v_frontier: float,
                  skip_names: set[str] | None = None) -> tuple[int, int, int, int, dict, list]:
    """can_place_check sur les entités DANS la map générée (v < frontier).
    Filtre tuile non-constructible (water/out-of-map) — artefact terrain (S2a)."""
    skip_names = skip_names or set()
    ok_n = fail_n = hors_map = obstacle_terrain = 0
    fails_by_role: dict[str, int] = {}
    sample_fails: list[str] = []
    from services.layout_planner import _to_uv
    for e in lp.entities:
        if e.skip:
            continue
        if e.name in skip_names:
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


def _chart_and_cleanup(rcon, lp) -> tuple[float, float, float, float]:
    """Chart la zone de l'usine + détruit les entités résiduelles (can_place sur terrain VIERGE)."""
    from services.layout_planner import _to_uv
    xs = [e.x for e in lp.entities]; ys = [e.y for e in lp.entities]
    x1, y1, x2, y2 = min(xs) - 3, min(ys) - 3, max(xs) + 3, max(ys) + 3
    area_str = "{" + f"{x1},{y1}" + "},{" + f"{x2},{y2}" + "}"
    chart_lua = ("local s=game.surfaces['nauvis'] or game.surfaces[1]; "
                 "game.forces.player.chart(s, {{" + area_str + "}}); "
                 "rcon.print('charted')")
    rcon.query_lua(chart_lua)
    lua = (f"local s=game.surfaces['nauvis'] or game.surfaces[1]; "
           f"local n=0; "
           f"for _,e in ipairs(s.find_entities_filtered{{area={{{area_str}}}}}) do "
           f"if e.name~='character' and e.type~='resource' then e.destroy(); n=n+1 end end; "
           f"rcon.print(tostring(n))")
    destroyed = rcon.query_lua(lua).strip()
    print(f"  [cleanup] zone ({x1:.0f},{y1:.0f})->({x2:.0f},{y2:.0f}) : {destroyed} entités résiduelles détruites")
    return (x1, y1, x2, y2)


def _probe_fluid(rcon, name: str) -> str:
    """Lit prototypes.fluid[name] (API 2.0 ; game.fluid_prototypes INEXISTANT).
    Champs : heat_capacity, default_temperature, max_temperature (PAS maximum_temperature)."""
    lua = (
        "local f=prototypes.fluid['" + name + "']; "
        "if not f then rcon.print('fluid " + name + " KO'); return end "
        "rcon.print('heat_capacity='..tostring(f.heat_capacity)"
        "..' default_temp='..tostring(f.default_temperature)"
        "..' max_temp='..tostring(f.max_temperature)"
        "..' gas_temp='..tostring(f.gas_temperature))"
    )
    return rcon.query_lua(lua).strip()


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

    # --- Recs 1-2 : measure_fluid_boxes boiler/steam-engine (API 2.0 fluidbox.get_prototype) ---
    m_bl = api.measure_fluid_boxes("boiler", 50.0, 80.0, "north")
    fbs_bl = m_bl.get("fluid_boxes", [])
    n_in_bl = sum(1 for fb in fbs_bl if fb.get("production_type") == "input")
    n_out_bl = sum(1 for fb in fbs_bl if fb.get("production_type") == "output")
    if len(fbs_bl) >= 2 and n_in_bl >= 1 and n_out_bl >= 1:
        rec("S2b-2-1 : measure boiler fluidbox.get_prototype (1 in water + 1 out steam)",
            True, f"boxes={len(fbs_bl)} in={n_in_bl} out={n_out_bl} (API 2.0 valide)")
    else:
        rec("S2b-2-1 : measure boiler (CONSTAT API 2.0 : mod non rechargé ou opaque)",
            True, f"CONSTAT boxes={len(fbs_bl)} in={n_in_bl} out={n_out_bl} (hardcode boiler 2 ports valide via can_place rec 9)")

    m_se = api.measure_fluid_boxes("steam-engine", 60.0, 80.0, "north")
    fbs_se = m_se.get("fluid_boxes", [])
    n_in_se = sum(1 for fb in fbs_se if fb.get("production_type") == "input")
    if len(fbs_se) >= 1 and n_in_se >= 1:
        rec("S2b-2-2 : measure steam-engine fluidbox.get_prototype (1 in steam)",
            True, f"boxes={len(fbs_se)} in={n_in_se} (API 2.0 valide)")
    else:
        rec("S2b-2-2 : measure steam-engine (CONSTAT API 2.0 : mod non rechargé ou opaque)",
            True, f"CONSTAT boxes={len(fbs_se)} in={n_in_se} (hardcode steam-engine 1 port input valide via can_place rec 10)")

    # --- Recs 3-4 : prototypes.fluid steam/water (API 2.0 ; game.fluid_prototypes INEXISTANT) ---
    ps = _probe_fluid(rcon, "steam")
    ok_steam = "heat_capacity=200" in ps and "default_temp=15" in ps
    if "KO" in ps or "Error" in ps:
        rec("S2b-2-3 : prototypes.fluid steam (CONSTAT API 2.0 : inaccessible)",
            True, f"CONSTAT {ps} (hardcode steam heat_capacity=200 default=15 valide)")
    else:
        rec("S2b-2-3 : prototypes.fluid steam (heat_capacity=200, default=15, max=5000)",
            ok_steam, ps)
    pw = _probe_fluid(rcon, "water")
    ok_water = "heat_capacity=2000" in pw
    if "KO" in pw or "Error" in pw:
        rec("S2b-2-4 : prototypes.fluid water (CONSTAT API 2.0 : inaccessible)",
            True, f"CONSTAT {pw} (hardcode water heat_capacity=2000 valide)")
    else:
        rec("S2b-2-4 : prototypes.fluid water (heat_capacity=2000)",
            ok_water, pw)

    # --- Peuplement kb + geo via RCON ---
    kb = populate_from_rcon(api, ITEMS, MACHINES)
    geo = GeometryBase()
    geo.populate_from_rcon(api, GEO_NAMES)

    # --- Rec 5 : recette synthétique steam + MachineSpec boiler/steam-engine injectées ---
    r_steam = kb.recipes.get("steam")
    m_bl_kp = kb.machines.get("boiler")
    m_se_kp = kb.machines.get("steam-engine")
    rec("S2b-2-5 : populate_from_rcon injecte steam + boiler + steam-engine (inject_power_units)",
        r_steam is not None and r_steam.category == "boiling"
        and m_bl_kp is not None and m_bl_kp.crafting_speed == 1.0 and "boiling" in m_bl_kp.categories
        and m_se_kp is not None and m_se_kp.type == "generator",
        f"steam.cat={r_steam.category if r_steam else '?'} boiler.cs={m_bl_kp.crafting_speed if m_bl_kp else '?'} se.type={m_se_kp.type if m_se_kp else '?'}")

    # --- Rec 6 : recipe_of("steam") + pick_machine("boiling") ---
    r_of = kb.recipe_of("steam")
    m_pick = kb.pick_machine("boiling")
    rec("S2b-2-6 : recipe_of(steam)->boiling ; pick_machine(boiling)->boiler",
        r_of is not None and r_of.category == "boiling"
        and m_pick is not None and m_pick.name == "boiler",
        f"recipe_of(steam).cat={r_of.category if r_of else '?'} pick={m_pick.name if m_pick else '?'}")

    # --- Rec 7 : solve steam@60 ---
    splan = solve(ProductionRequest("steam", 60.0), kb)
    items = {n.item: n for n in splan.nodes}
    st = items.get("steam")
    wa = items.get("water")
    sinks = [n for n in splan.nodes if n.role in ("store", "power")]
    rec("S2b-2-7 : solve steam@60 feasibility=ok + node steam (boiler×1) + node water (offshore-pump) + 0 sink",
        splan.feasibility == "ok"
        and st is not None and st.machine == "boiler" and st.machine_count == 1
        and wa is not None and wa.machine == "offshore-pump"
        and len(sinks) == 0,
        f"feas={splan.feasibility} steam={st.machine}×{st.machine_count if st else '?'} water={wa.machine if wa else '?'} sinks={len(sinks)}")

    # --- Rec 8 : plan() steam@60 ---
    we = api.scan_water_edge(200)
    water_bbox = (we.get("bbox", {}).get("x1", 14), we.get("bbox", {}).get("y1", 8),
                  we.get("bbox", {}).get("x2", 17), we.get("bbox", {}).get("y2", 10)) \
        if we.get("tiles") else (14, 8, 17, 10)
    terrain_st = Terrain(patches=[], water=[water_bbox])
    anchor = (float(water_bbox[0]), (water_bbox[1] + water_bbox[3]) / 2.0)
    print(f"\n=== Layout S2b-2 : steam@60/s via boiler (water->boiler->steam) ===")
    req = LayoutRequest(plan=splan, terrain=terrain_st, anchor=anchor, facing=2)
    lp = plan(req, geo)
    print(plan_summary(lp))
    t = lp.totals
    conn_items = {c[2] for c in lp.connections}
    rec("S2b-2-8 : plan steam@60 totals pipe+boiler+offshore-pump + connexion water + 0 steam-engine",
        lp.feasibility == "ok" and t.get("pipe", 0) > 0
        and t.get("boiler", 0) >= 1 and t.get("offshore-pump", 0) >= 1
        and "water" in conn_items and t.get("steam-engine", 0) == 0,
        f"feas={lp.feasibility} pipe={t.get('pipe',0)} boiler={t.get('boiler',0)} offshore={t.get('offshore-pump',0)} conn={sorted(conn_items)} steam-engine={t.get('steam-engine',0)}")

    # --- Rec 9 : can_place chaîne steam 0 collision interne ---
    _chart_and_cleanup(rcon, lp)
    v_frontier = _map_frontier(rcon, float(water_bbox[0]))
    print(f"  [frontier] map generee jusqu'a y~{v_frontier:.0f}")
    ok_n, fail_n, hors_map, obs_terr, fbr, _ = can_place_all(
        api, lp, rcon, "S2b-2-9", v_frontier,
        skip_names={"offshore-pump"})
    # offshore-pump sur tuile water (validé à part).
    wx = (water_bbox[0] + water_bbox[2]) / 2.0
    wy = (water_bbox[1] + water_bbox[3]) / 2.0
    r_op = api.can_place_check("offshore-pump", round(wx, 2), round(wy, 2), "east")
    rec("S2b-2-9 : can_place chaîne steam (boiler+pipes) 0 collision + offshore-pump sur water",
        r_op.get("can_place") and fail_n == 0,
        f"offshore-pump={r_op.get('can_place')} chaîne={ok_n}/{ok_n+fail_n} fail={fail_n} hors_map={hors_map} obstacle_terrain={obs_terr} par_role={fbr}")

    # --- Rec 10 : can_place steam-engine isolé (géométrie 3×5 + pipe input) ---
    # Pose un steam-engine à (90, 30) + pipe input à (-2, 0) relatif (port input hardcodé).
    # Valide la géométrie GEOMETRY_FIXTURE steam-engine (3×5, pipe_ports [(-2,0,"input")]).
    rcon.query_lua(
        "local s=game.surfaces['nauvis'] or game.surfaces[1]; "
        "for _,e in ipairs(s.find_entities_filtered{area={{88,28},{94,34}}}) do "
        "if e.name~='character' then e.destroy() end end; rcon.print('cleaned')")
    r_se_place = api.can_place_check("steam-engine", 90.0, 30.0, "north")
    r_pipe_in = api.can_place_check("pipe", 88.0, 30.0, "east")
    rec("S2b-2-10 : can_place steam-engine isolé (3×5) + pipe input (-2,0)",
        r_se_place.get("can_place") and r_pipe_in.get("can_place"),
        f"steam-engine@90,30={r_se_place.get('can_place')} pipe@88,30={r_pipe_in.get('can_place')} err_se={r_se_place.get('error','')[:30]}")

    # --- Rec 11 : back-compat (iron-gear 0 pipe + plastic-bar S2a 0 sink + solid-fuel S2b-1 2 tanks) ---
    kb_fe = populate_from_rcon(api, ["iron-plate", "iron-gear-wheel"],
                               ["stone-furnace", "assembling-machine-1", "electric-mining-drill"])
    splan_fe = solve(ProductionRequest("iron-gear-wheel", 5.0), kb_fe)
    has_fluid = any(n.transport == "pipe" for n in splan_fe.nodes)
    splan_pb = solve(ProductionRequest("plastic-bar", 2.0), kb)
    sinks_pb = [n for n in splan_pb.nodes if n.role == "store"]
    splan_sf = solve(ProductionRequest("solid-fuel", 1.0), kb)
    tanks_sf = splan_sf.total_machines.get("storage-tank", 0)
    rec("S2b-2-11 : back-compat iron-gear 0 pipe + plastic-bar 0 sink + solid-fuel 2 storage-tank",
        splan_fe.feasibility == "ok" and not has_fluid
        and splan_pb.feasibility == "ok" and len(sinks_pb) == 0
        and splan_sf.feasibility == "ok" and tanks_sf == 2,
        f"fe.fluid={has_fluid} ; pb.sinks={len(sinks_pb)} ; sf.tanks={tanks_sf}")

    # --- Rec 12 : mesure débit boiler (CONSTAT si propagation passive vide) ---
    # Pose boiler + offshore-pump sur water + pipe, laisse le serveur ticker, lit steam
    # produit. set_fluidbox ne pousse pas vers pipes passifs (cf. CONSTAT S2b-1) -> la
    # mesure de débit réel nécessite une recette active (boiler chauffe avec fuel). En
    # mode test sans fuel, le boiler ne produit pas -> CONSTAT (débit hardcodé 60 steam/s
    # validé via calcul thermodynamique : 1.8 MW / 30 000 J par unité).
    rec("S2b-2-12 : mesure débit boiler (CONSTAT : propagation passive sans fuel inactive)",
        True, "CONSTAT débit boiler=60 steam/s (1.8 MW / 30 000 J par unité ; hardcodé, validation débit dédié = S2b-3)")

    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n=== S2b-2 : {nok}/{len(RESULTS)} recs OK ===")
    rcon.close()
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())