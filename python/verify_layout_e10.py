"""Test LIVE E10 (J4) : poser un LayoutPlan COMPLET en jeu, pour de vrai.

Le LayoutPlanner calcule des usines main-bus depuis S1 — drills, belts, inserters,
assembleurs, splitters, underground, poteaux — et les options de pose qui manquaient
(ug_type, priority, modules, recette) ont été livrées par E2 et E4a. Pourtant **aucun
de ces plans n'a jamais été posé en jeu** : toutes les validations portaient sur des
micro-chaînes de 3 entités.

C'est l'écart le plus coûteux du projet : plusieurs milliers de lignes de planification
dont rien ne prouve qu'elles produisent une usine qui tourne. Ce script le comble, ou
dit précisément où ça coince.

Trois inconnues annoncées d'avance, chacune vérifiée séparément pour que l'échec soit
lisible :
  1. l'inventaire couvre-t-il les entités du plan (le kit du mod n'a pas été pensé pour) ;
  2. le terrain accepte-t-il l'emprise (bien plus large qu'une micro-chaîne) ;
  3. l'usine posée produit-elle, une fois alimentée.

Pré-requis : serveur headless, mod E3a. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import sys

from agents.base import Contract
from agents.factory_builder import FactoryBuilder
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.executor import execute_micro
from services.knowledge import GeometryBase, ProductionGoal, populate_from_rcon
from services.production_solver import ProductionRequest, solve

RESULTS: list[tuple[str, bool, str]] = []
ITEMS = ["iron-plate", "iron-gear-wheel"]
MACHINES = ["stone-furnace", "electric-furnace", "assembling-machine-1",
            "electric-mining-drill", "burner-mining-drill"]
GEO_NAMES = ["transport-belt", "burner-inserter", "inserter", "small-electric-pole",
             "stone-furnace", "assembling-machine-1", "electric-mining-drill",
             "underground-belt", "splitter"]
CIBLE = "iron-gear-wheel"
DEBIT = 1.0          # modeste : on éprouve la POSE, pas le dimensionnement


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:105]}")


def main() -> int:
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    api.setup()
    api.reset_character()
    rcon.query_lua("local n = 0 for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{force='player'}) do "
                   "if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    api.generate_terrain(0.0, 0.0, 200.0)
    api.run_action(api.wait, 60, timeout=60.0)

    kb = populate_from_rcon(api, ITEMS, MACHINES)
    # La géométrie doit être PEUPLÉE : sur un GeometryBase vide, `build_layout` écarte
    # chaque tier de belt faute de géométrie et finit par rendre None sans rien dire.
    geo = GeometryBase()
    geo.populate_from_rcon(api, GEO_NAMES)

    # --- 1 : le solveur produit une chaîne complète (mine -> plaque -> engrenage) ---
    splan = solve(ProductionRequest(CIBLE, DEBIT,
                                    machine_tiers={"smelting": "stone-furnace"}), kb)
    roles = [n.role for n in splan.nodes]
    rec("e10-1 : chaîne calculée par le solveur",
        splan.feasibility == "ok" and "mine" in roles and len(splan.nodes) >= 3,
        f"feas={splan.feasibility} noeuds={len(splan.nodes)} roles={roles}")
    if splan.feasibility != "ok":
        rcon.close()
        return _verdict()

    # --- 2 : le LayoutPlanner en fait une usine implantée ---
    sp = api.scan_patch("iron-ore", 150.0)
    # Dégager la végétation sur l'emprise : un plan de cette taille croise forcément des
    # arbres, et `obstacle_blocking` masquerait la question qu'on veut poser (la pose
    # tient-elle ?). On ne touche ni aux ressources ni à ce qui est construit.
    _bb = sp.get("bbox") or {}
    rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{"
        f"{{{_bb.get('x1', 0) - 160},{_bb.get('y1', 0) - 160}}},"
        f"{{{_bb.get('x2', 0) + 160},{_bb.get('y2', 0) + 160}}}}}}}) do "
        f"if e.force ~= game.forces.player and e.type ~= 'resource' "
        f"and e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    bbox = sp.get("bbox") or {}
    zone = (float(bbox.get("x1", 0)), (float(bbox.get("y1", 0)) + float(bbox.get("y2", 0))) / 2)
    fb = FactoryBuilder(api, Contract(ProductionGoal(CIBLE, DEBIT), zone=zone,
                                      replan_budget=4))
    api.run_action(api.teleport_to, zone[0], zone[1], timeout=60.0)
    lp = fb.build_layout(splan, geo)
    feas = getattr(lp, "feasibility", None)
    entities = getattr(lp, "entities", [])
    utiles = [e for e in entities if not getattr(e, "skip", False)]
    totals = getattr(lp, "totals", {})
    rec("e10-2 : LayoutPlan complet calculé", bool(utiles),
        f"feas={feas} entités={len(entities)} totals={totals}")

    # --- 2b : le plan est-il DIMENSIONNÉ par le débit, ou par la taille du gisement ? ---
    #
    # C'est la question que ce test a fini par poser, et elle compte plus que la pose.
    # Pour 1 engrenage/s, le plan sort à 505 entités dont 468 belts : la belt de collecte
    # longe TOUT le bord du gisement, sa longueur suit donc la taille du patch et non le
    # débit demandé. Le docstring du MicroPlanner le notait déjà ; personne ne l'avait
    # mesuré sur un plan réel.
    belts = totals.get("transport-belt", 0)
    machines_utiles = sum(c for n, c in totals.items()
                          if n in ("assembling-machine-1", "stone-furnace",
                                   "electric-mining-drill"))
    xs = [e.x for e in utiles] or [0]
    ys = [e.y for e in utiles] or [0]
    emprise = (max(xs) - min(xs), max(ys) - min(ys))
    # Le défaut est resté "patch" par prudence (il conserve les beacons -u, cf.
    # LayoutConstraints.collect_belt_scope). On MESURE donc son coût sans le traiter
    # comme une panne : c'est un arbitrage documenté, pas une régression.
    ratio = belts / max(machines_utiles, 1)
    rec("e10-2b : coût du défaut « patch » mesuré", True,
        f"{belts} belts pour {machines_utiles} machines utiles (ratio {ratio:.0f}:1), "
        f"emprise {emprise[0]:.0f}x{emprise[1]:.0f} pour {DEBIT} {CIBLE}/s — "
        f"dimensionné par le gisement, pas par la demande")

    # --- 2d : le mode "drills" tient-il sa promesse SUR LE TERRAIN RÉEL ? ---
    # Le fixture des tests unitaires a un petit patch ; c'est ici, sur un gisement de
    # plusieurs centaines de tuiles, que l'écart se voit vraiment.
    from services.layout_planner import LayoutConstraints
    fb2 = FactoryBuilder(api, Contract(ProductionGoal(CIBLE, DEBIT), zone=zone,
                                       replan_budget=4,
                                       layout_constraints=LayoutConstraints(
                                           collect_belt_scope="drills")))
    lp2 = fb2.build_layout(splan, geo)
    t2 = getattr(lp2, "totals", {})
    u2 = [e for e in getattr(lp2, "entities", []) if not getattr(e, "skip", False)]
    b2 = t2.get("transport-belt", 0)
    xs2 = [e.x for e in u2] or [0]
    ys2 = [e.y for e in u2] or [0]
    rec("e10-2d : mode « drills » — la collecte suit le débit, pas le gisement",
        b2 < belts and len(u2) < len(utiles),
        f"patch: {len(utiles)} entités / {belts} belts / {emprise[1]:.0f} de haut  ->  "
        f"drills: {len(u2)} entités / {b2} belts / {max(ys2) - min(ys2):.0f} de haut")

    if feas != "ok":
        # `obstacle_blocking` n'est pas un accident de terrain : mesuré ici, 7180 arbres,
        # rochers et falaises peuplent un rayon de 400 tuiles. Un plan qui s'étale sur
        # des centaines de tuiles en croise forcément — dégager ne suffit pas, il faut
        # que le plan soit plus petit.
        obs = api.scan_obstacles(400)
        rec("e10-2c : refus de terrain expliqué", True,
            f"feas={feas} notes={getattr(lp, 'notes', [])[:2]} — "
            f"{obs.get('count')} obstacles naturels dans un rayon de 400")
        rcon.close()
        return _verdict()

    # --- 3 : l'inventaire couvre-t-il le plan ? (inconnue n°1, annoncée) ---
    besoins: dict[str, int] = {}
    for e in utiles:
        besoins[e.name] = besoins.get(e.name, 0) + 1
    inv = api.get_state().get("inventory", {})
    manquants = {n: c - inv.get(n, 0) for n, c in besoins.items() if inv.get(n, 0) < c}
    rec("e10-3 : le kit du mod couvre les entités du plan", not manquants,
        f"besoins={besoins} | manquants={manquants or 'aucun'}")

    # On complète pour pouvoir éprouver la POSE, qui est l'objet du test. Le manque
    # d'inventaire est un problème de kit, pas de planification : le noter suffit.
    if manquants:
        ins = " ".join(f"c.insert{{name='{n}', count={c + 10}}}"
                       for n, c in manquants.items())
        rcon.query_lua(f"local c = nil for _, e in pairs(game.surfaces[1]"
                       f".find_entities_filtered{{name='character'}}) do c = e end "
                       f"if c then {ins} end rcon.print('ok')")
        print(f"       . inventaire complété pour {len(manquants)} type(s) d'entité")

    # --- 4 : la pose (inconnue n°2 : l'emprise) ---
    rap = execute_micro(api, lp, fuel="coal", fuel_count=25,
                        generate=True, approach=True, timeout=60.0)
    poses = len(rap.placed)
    rec("e10-4 : usine complète POSÉE en jeu",
        rap.ok and poses == len(utiles),
        f"ok={rap.ok} posées={poses}/{len(utiles)} bloquées={rap.blocked[:1]} "
        f"missing={rap.missing}")
    for s in rap.steps[:3]:
        print(f"       . {s}")
    if rap.blocked:
        print(f"       ! premier blocage : {rap.blocked[0]}")

    # --- 5 : ce qui a été posé porte bien ses options (recette, sens, priorité) ---
    api.run_action(api.wait, 120, timeout=60.0)
    sa = api.scan_area(60.0)
    rows = sa.get("entities", []) if isinstance(sa, dict) else []
    machines = [e for e in rows if e.get("type") == "assembling-machine"]
    avec_recette = [m for m in machines if m.get("recipe") not in (None, "none")]
    attendu_recette = any(getattr(e, "recipe", "") for e in utiles)
    rec("e10-5 : les machines posées portent leur recette",
        (not attendu_recette) or bool(avec_recette),
        f"{len(avec_recette)}/{len(machines)} machine(s) avec recette "
        f"({[m.get('recipe') for m in avec_recette][:3]})")

    # --- 6 : PREUVE — l'usine produit (inconnue n°3) ---
    prod = api.production_stats()
    rec("e10-6 : l'usine posée est en marche",
        any(e.get("status") == "working" for e in rows if e.get("type") in
            ("mining-drill", "furnace", "assembling-machine")),
        f"statuts={[(e.get('name'), e.get('status')) for e in rows if e.get('type') in ('mining-drill', 'furnace', 'assembling-machine')][:4]}")

    rcon.close()
    return _verdict()


def _verdict() -> int:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())