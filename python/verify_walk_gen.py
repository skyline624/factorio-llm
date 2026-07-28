"""Validation LIVE S4d : generate_terrain + walk_to (resout CONSTAT S1d/S1g out-of-map).

Cas d'usage reel couche P2 : l'IA genere les chunks autour de la cible AVANT de builder
au-dela de la starting_area, puis y envoie le character (walk_to, pathfinding natif).

Pre-requis :
  - serveur Factorio 2.0 + mod S4d lance (scripts/start_factorio_dedicated.bat) APRES
    la modif mod (generate_terrain dans tools.lua + control.lua). Relance requise.
  - UN JOUEUR CONNECTE (mode production : walk_to pilote le character du joueur, qui
    genere le terrain en marchant). Optionnel pour generate_terrain (marche aussi en
    headless : le terrain est genere par request_to_generate_chunks + force_generate,
    independamment du character).
  - RCON 127.0.0.1:27015 (pw "factoriollm").

Mecanisme (mod/scripts/tools.lua M.generate_terrain) :
  surface.request_to_generate_chunks({x,y}, r_chunks) + surface.force_generate_chunk_requests()
  -> genere SYNCHRONE les chunks autour de (x,y). Resout le CONSTAT : sans ceci, walk_to
  (pathfinding) ne peut pas planifier vers du out-of-map (tuiles non walkable) -> orniere.

Strategie (mix test/prod) :
  - BUILD en mode TEST (set_test_mode True) : le character headless est au spawn (terrain
    degage, gisement iron-ore proche -> feas=ok comme verify_layout_s4c.py). En mode prod,
    scan_patch chercherait autour du joueur (pos variable, terrain souvent encombre ->
    obstacle_blocking). Le blueprint est non destructif, calcule autour du spawn.
  - WALK en mode PROD (set_test_mode False) : le character = joueur connecte. walk_to
    utilise walking_state (marche reelle, pas teleport). Comme le terrain entre le joueur
    et le bout n'est pas integrement genere, on walk PAR ETAPES : a chaque etape, on
    generate_terrain au prochain waypoint (50 tuiles vers la cible) PUIS walk_to vers lui
    (le terrain etant genere, le pathfinding reussit). Itere jusqu'a la cible.

La cascade S4c (facing=2, u=x, v=y) s'etend en +u (x) et les etages s'empilent en +v (y).
Les entites hors_map (can_place faux negatif) sont au sud (y eleve), le long de toute la
cascade. On genere une BANDE de chunks le long de la cascade au sud (boucle de points
tous les 100 tuiles en x, radius 60 -> couvre ~100 tuiles entre points).

8 recs :
  1. setup + character (mode test, headless au spawn).
  2. scan_patch + build cascade S4c (autour du spawn, feas=ok).
  3. bbox cascade (x_min, x_max, y_max) + baseline can_place (fail = hors zone generee).
  4. generate_terrain bande sud (boucle points, generated==total par point).
  5. scan_tiles_bbox au coin extreme -> out-of-map apres generation (doit etre ~0).
  6. can_place apres generate_terrain -> fail < baseline (terrain genere -> hors_map baisse).
  7. set_test_mode(False) -> prod ; walk_to par etapes (generate+walk) vers le bout -> reached.
  8. distance parcourue par le joueur > 50 tuiles (marche reelle vers le bout).

Lancement (apres relance serveur S4d) :
    cd python
    python verify_walk_gen.py
"""

from __future__ import annotations
import sys

sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import populate_from_rcon, GeometryBase
from services.production_solver import ProductionRequest, solve
from services.layout_planner import _occ_terrain
from services import perception
from agents.base import Contract
from agents.factory_builder import FactoryBuilder
from services.knowledge import ProductionGoal

DIR_TO_STR = {0: "north", 2: "east", 4: "south", 6: "west"}

ITEMS = ["iron-plate", "iron-gear-wheel"]
MACHINES = ["stone-furnace", "assembling-machine-1", "electric-mining-drill"]
GEO_NAMES = ["transport-belt", "burner-inserter", "small-electric-pole",
             "stone-furnace", "assembling-machine-1", "electric-mining-drill"]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:80]}")


def char_pos(api: ModApi):
    gs = perception.snapshot(api)
    return gs.pos_tuple() if gs.character else None


def dist(a, b) -> float:
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


def can_place_all(api, lp, label, geo):
    """can_place non destructif sur TOUTES les entites (pas de filtre frontier).
    fail = entites sur out-of-map (hors zone generee) ou obstacle reel."""
    ok_n = fail_n = terrain_hit = 0
    for e in lp.entities:
        if e.skip:
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
    print(f"  [{label}] can_place={ok_n}/{ok_n + fail_n} fail={fail_n} terrain_hit={terrain_hit}")
    return ok_n, fail_n, terrain_hit


def main() -> int:
    rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
    api = ModApi(rcon)
    try:
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"!! MOD NON RECHARGE : {e}")
        print("   -> relance scripts/start_factorio_dedicated.bat puis re-execute.")
        rcon.close()
        return 1

    # Verifie que generate_terrain est expose (post-relance S4d).
    r_gt = api.generate_terrain(0, 0, 10.0)
    if not isinstance(r_gt, dict) or "error" in r_gt:
        print(f"!! generate_terrain absent/echec : {r_gt}")
        print("   -> relance le serveur (mod S4d modifie : tools.lua + control.lua).")
        rcon.close()
        return 1

    # --- Rec 1 : setup + character (mode test, headless au spawn) ---
    # BUILD en mode test : character headless au spawn (terrain degage, gisement proche
    # -> feas=ok comme verify_layout_s4c.py). En prod, scan_patch chercherait autour du
    # joueur (pos variable, terrain encombre -> obstacle_blocking). Le walk (rec 7-8)
    # bascule en prod APRES, quand le blueprint est deja calcule.
    api.set_test_mode(True)
    api.setup()
    pos0 = char_pos(api)
    rec("1: setup + character (mode test, headless au spawn)",
        pos0 is not None, f"pos={pos0}")
    if pos0 is None:
        print("!! aucun character (headless non cree) -> relance le serveur.")
        rcon.close()
        return 1

    kb = populate_from_rcon(api, ITEMS, MACHINES)
    geo = GeometryBase()
    geo.populate_from_rcon(api, GEO_NAMES)

    # --- Rec 2 : scan_patch + build cascade S4c ---
    sp = api.scan_patch("iron-ore", 400)
    if not sp.get("bbox"):
        print("!! aucun patch iron-ore -> marche vers un gisement naturel puis relance.")
        rcon.close()
        return 1
    bbox = sp["bbox"]
    splan = solve(ProductionRequest("iron-gear-wheel", 5.0,
                                    machine_tiers={"smelting": "stone-furnace"}), kb)
    zone = (float(bbox["x1"]), (bbox["y1"] + bbox["y2"]) / 2.0)
    contract = Contract(ProductionGoal("iron-gear-wheel", 5), zone=zone, replan_budget=4)
    fb = FactoryBuilder(api, contract)
    lp = fb.build_layout(splan, geo)
    feas = getattr(lp, "feasibility", None)
    n_ent = len(getattr(lp, "entities", []))
    rec("2: scan_patch + build cascade S4c (blueprint non destructif)",
        lp is not None and feas == "ok", f"feas={feas} entities={n_ent}")
    if not lp or feas != "ok":
        print("!! build_layout non ok -> pas de test.")
        rcon.close()
        return 1

    # --- Rec 3 : bbox cascade + baseline can_place ---
    es = [e for e in lp.entities if not e.skip]
    xs = [e.x for e in es]
    ys = [e.y for e in es]
    x_min, x_max = min(xs), max(xs)
    y_min, y_max = min(ys), max(ys)
    target = (x_max + 20.0, y_max + 20.0)
    ok0, fail0, th0 = can_place_all(api, lp, "baseline", geo)
    rec("3: bbox cascade + baseline can_place (avant generation)",
        True, f"x=[{x_min:.0f},{x_max:.0f}] y_max={y_max:.0f} baseline_fail={fail0}")

    # --- Rec 4 : generate_terrain le long de la cascade (bande sud) ---
    # Boucle de points le long de la cascade au sud (y = y_max + 10), pas 100 tuiles en x,
    # radius 60 (couvre ~100 tuiles entre points -> bande continue). generated==total par
    # point (tous les chunks demandes generes).
    y_gen = y_max + 10.0
    step_x = 100.0
    gen_radius = 60.0
    n_pts = 0
    all_ok = True
    x = x_min - 20.0
    while x < x_max + 40.0:
        rg = api.generate_terrain(x, y_gen, gen_radius)
        ok_pt = (isinstance(rg, dict) and "error" not in rg
                 and rg.get("generated") == rg.get("total"))
        all_ok = all_ok and ok_pt
        n_pts += 1
        print(f"  [gen] pt {n_pts}: ({x:.0f},{y_gen:.0f}) r={gen_radius:.0f} -> "
              f"generated={rg.get('generated')}/{rg.get('total')} ok={ok_pt}")
        x += step_x
    rec("4: generate_terrain bande sud (generated==total par point)",
        all_ok and n_pts > 0, f"points={n_pts} all_generated={all_ok}")

    # --- Rec 5 : scan_tiles_bbox au coin extreme (out-of-map apres generation) ---
    sx1, sy1 = int(x_max) - 10, int(y_max) - 10
    sx2, sy2 = int(target[0]) + 10, int(target[1]) + 10
    r_st = api.scan_tiles_bbox(sx1, sy1, sx2, sy2)
    oom = tot = 0
    if isinstance(r_st, dict) and "error" not in r_st:
        for t in r_st.get("tiles", []):
            tot += 1
            if t["name"] == "out-of-map":
                oom += 1
    rec("5: scan_tiles_bbox coin extreme (out-of-map apres generation)",
        oom == 0, f"tiles={tot} out-of-map={oom}")

    # --- Rec 6 : generate_terrain sur un point NON EXPLORE (valide l'API sur vrai out-of-map) ---
    # La cascade est autour du spawn (deja explore par le joueur) -> rec 4-5 ne prouvent
    # pas que generate_terrain genere du VRAI out-of-map (generated==total est idempotent
    # sur du deja-genere). On valide ici sur un point loin NON explore : get_tile avant =
    # None/erreur/"out-of-map", apres generate_terrain = vrai tile. On cherche parmi
    # plusieurs candidats lointains (un point deja genere par un run precedent n'est pas
    # retenu).
    CANDIDATES = [(1000.0, 1000.0), (2000.0, -2000.0), (-3000.0, 3000.0),
                  (4000.0, 4000.0), (-5000.0, -5000.0), (8000.0, 8000.0)]
    probe = None
    name_before = None
    for (px, py) in CANDIDATES:
        t = api.get_tile(int(px), int(py))
        nm = t.get("name") if isinstance(t, dict) else None
        if nm in (None, "out-of-map", "") or (isinstance(t, dict) and "error" in t):
            probe = (px, py)
            name_before = nm
            t_before = t
            break
    if probe is None:
        # tous les candidats sont deja generes (improbable) -> on prend le dernier.
        probe = CANDIDATES[-1]
        t_before = api.get_tile(int(probe[0]), int(probe[1]))
        name_before = t_before.get("name") if isinstance(t_before, dict) else None
    rg = api.generate_terrain(probe[0], probe[1], 60.0)
    t_after = api.get_tile(int(probe[0]), int(probe[1]))
    name_after = t_after.get("name") if isinstance(t_after, dict) else None
    was_unexplored = name_before in (None, "out-of-map", "") or "error" in (t_before or {})
    now_generated = (name_after not in (None, "out-of-map", "")
                     and "error" not in (t_after or {}))
    rec("6: generate_terrain sur point non explore (out-of-map -> terrain genere)",
        was_unexplored and now_generated
        and isinstance(rg, dict) and rg.get("generated") == rg.get("total"),
        f"probe={probe} avant '{name_before}' -> apres '{name_after}' gen={rg.get('generated')}/{rg.get('total')}")

    # --- Rec 7 : set_test_mode(False) -> prod ; walk_to par etapes (generate+walk) vers le SUD ---
    # Bascule en PRODUCTION : character = joueur connecte. walk_to = walking_state (marche
    # reelle, PAS teleport). Le terrain au sud du joueur (au-dela de son exploration) n'est
    # PAS genere -> walk_to direct vers le sud echouerait (pathfinding vers out-of-map).
    # On walk PAR ETAPES : generate_terrain au prochain waypoint (50 tuiles au sud) PUIS
    # walk_to vers lui (terrain genere -> pathfinding reussit). Itere. C'est le cas d'usage
    # P2 : l'IA genere un corridor devant le character puis l'y envoie (exploration).
    api.set_test_mode(False)
    posJ = char_pos(api)
    if posJ is None:
        rec("7: set_test_mode(False) -> prod ; walk_to par etapes (generate+walk) vers le sud",
            False, "aucun joueur connecte (set_test_mode False -> character None)")
        rec("8: walk_to distance parcourue > 50 tuiles (marche reelle)",
            False, "SKIP (pas de joueur)")
        print("!! aucun joueur connecte pour le walk_to -> connecte-toi puis relance.")
        rcon.close()
        return 1
    # Cible : 250 tuiles au sud du joueur (au-dela de l'exploration -> terrain non genere).
    walk_target = (posJ[0], posJ[1] + 250.0)
    print(f"  [walk] joueur a {posJ} -> cible sud {walk_target} "
          f"(dist {dist(posJ, walk_target):.0f} tuiles, terrain non explore)")
    cur = posJ
    iters = 0
    STEP = 50.0
    while dist(cur, walk_target) > 12.0 and iters < 25:
        # waypoint : STEP tuiles au sud (straight south, x constant).
        advance = min(STEP, walk_target[1] - cur[1])
        wp = (cur[0], cur[1] + advance)
        # genere le terrain au waypoint AVANT le walk (sinon pathfinding vers out-of-map
        # echoue). C'est le coeur du cas d'usage P2 (generate-then-walk).
        api.generate_terrain(wp[0], wp[1], 60.0)
        res = api.run_action(api.walk_to, wp[0], wp[1], timeout=120)
        new = char_pos(api)
        if new is None:
            break
        print(f"  [walk] iter {iters + 1}: wp {wp} -> arrive {new} "
              f"(dist cible {dist(new, walk_target):.0f}) res={res}")
        moved = dist(cur, new)
        cur = new
        iters += 1
        if moved < 1.5:
            # pas avance (bloque) -> on arrete (corridor genere mais pathfinding peut
            # refuser un wp trop proche d'un obstacle/edge).
            print("  [walk] pas d'avancee -> arret")
            break
    reached = dist(cur, walk_target) < 25.0
    rec("7: set_test_mode(False) -> prod ; walk_to par etapes (generate+walk) vers le sud",
        reached, f"joueur {posJ} -> {cur} (dist cible {dist(cur, walk_target):.0f}, {iters} iters)")

    # --- Rec 8 : distance parcourue par le joueur > 50 tuiles (marche reelle) ---
    parcourue = dist(posJ, cur)
    rec("8: walk_to distance parcourue > 50 tuiles (marche reelle vers le sud)",
        parcourue > 50, f"joueur depart {posJ} -> arrive {cur} = {parcourue:.0f} tuiles")

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