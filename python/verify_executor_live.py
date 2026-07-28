"""Test LIVE de l'Executor E1 : de l'objectif à la production réelle.

Ce script valide la boucle fermée `FactoryBuilder.run_micro_layout` :
objectif -> scan_patch -> plan_micro -> execute_micro -> **entités qui produisent**.
C'est la première fois que le projet bâtit une usine sans script de pose ad hoc :
verify_micro_live.py posait les 3 entités à la main (place_entity_at + wait(4.0)
arbitraire + move_items rayon 32) ; ici tout passe par `services/executor.py`
(pré-vol inventaire, décalage solidaire, run_action race-free, vérification par
l'inventaire, move_items_at ciblé par position).

Déroulé :
  1. set_test_mode + setup + reset_character -> character neuf avec le kit intégral
     (drill/furnace/inserter/coal), donc pré-vol inventaire satisfait et run reproductible.
  2. generate_terrain autour du spawn -> scan_patch voit un gisement (headless out-of-map).
  3. run_micro_layout("iron-ore") -> (MicroPlan, ExecutionReport).
  4. Preuve de PRODUCTION (et non de simple pose) via scan_factory.

TOUS les contrôles ciblent les entités par la POSITION issue de `report.placed`.
Le premier run de ce script a validé, lui, la première entité du bon type trouvée sur
la carte : c'étaient des machines laissées par verify_micro_live.py (drills à (-32,-49),
four à (3,3)) — 3 recs verts alors que la chaîne du run n'existait pas. `reset_character`
remet l'inventaire à neuf, pas la carte : ne jamais valider par type seul.

L'inserter n'apparaît pas dans scan_factory (PRODUCER_TYPES n'inclut pas inserter,
tools.lua:21) : il se vérifie indirectement, le four ne passe "working" que s'il est
alimenté en minerai — donc que si l'inserter transfère.

Pré-requis : serveur headless lancé (scripts/start_factorio_dedicated.bat), mod chargé.
SKIP (return 0) si le serveur est injoignable, comme les autres verify_*.
"""

from __future__ import annotations

import sys

from agents.base import Contract
from agents.factory_builder import FactoryBuilder
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.knowledge import ProductionGoal

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:46s} {detail[:110]}")


def _rows(api: ModApi) -> list[dict]:
    """Machines productrices de la force (scan_factory), liste vide si erreur."""
    sf = api.scan_factory()
    return sf.get("entities", []) if isinstance(sf, dict) else []


def _at(rows: list[dict], name: str, x: float, y: float, tol: float = 1.6) -> dict | None:
    """L'entité `name` posée AUTOUR de (x, y) — tolérance = snap de grille (±0.5) + marge.

    `create_entity` snappe la position (mesuré : 1×1 -> centre de tuile, 2×2 -> entier),
    donc la position rapportée par scan_factory peut différer de la demande d'une
    demi-tuile. On ne compare jamais à l'égalité.
    """
    for r in rows:
        if r.get("name") != name:
            continue
        if abs(float(r.get("x", 1e9)) - x) <= tol and abs(float(r.get("y", 1e9)) - y) <= tol:
            return r
    return None


def main() -> int:
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    # --- Rec 1 : character neuf + kit intégral (run reproductible) ---
    api.set_test_mode(True)
    api.setup()
    api.reset_character()   # synchrone (reply_ack immediat) : pas de run_action ici
    inv = api.get_state().get("inventory", {})
    kit_ok = all(inv.get(n, 0) >= 1 for n in
                 ("burner-mining-drill", "burner-inserter", "stone-furnace")) \
        and inv.get("coal", 0) >= 15
    rec("live-1 : reset_character -> kit intégral", kit_ok,
        f"drill={inv.get('burner-mining-drill', 0)} ins={inv.get('burner-inserter', 0)} "
        f"furn={inv.get('stone-furnace', 0)} coal={inv.get('coal', 0)}")

    # --- Rec 2 : terrain révélé (headless : hors chunks générés, scan_patch ne voit rien) ---
    api.generate_terrain(0.0, 0.0, 120.0)
    api.run_action(api.wait, 60, timeout=30.0)
    avant = len(_rows(api))
    rec("live-2 : terrain révélé autour du spawn", True,
        f"{avant} machine(s) déjà sur la carte (runs précédents) — ignorées, "
        f"les contrôles ciblent les positions posées ici")

    # --- Rec 3/4 : boucle fermée objectif -> plan -> pose ---
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 0.5)))
    plan, report = fb.run_micro_layout("iron-ore")

    rec("live-3 : plan_micro ancré sur du minerai -> 3 entités",
        plan.feasibility == "ok" and len(plan.entities) == 3,
        f"feas={plan.feasibility} entities="
        f"{[(e.role, e.name, e.x, e.y) for e in plan.entities]}")
    for note in plan.notes[:2]:
        print(f"       . {note}")

    rec("live-4 : execute_micro -> 3 entités posées + alimentées",
        report.ok and len(report.placed) == 3 and not report.missing and not report.blocked,
        f"ok={report.ok} placed={[(p.name, p.x, p.y) for p in report.placed]} "
        f"missing={report.missing} blocked={report.blocked} fueled={report.fueled}")
    for step in report.steps:
        print(f"       . {step}")
    for note in report.notes:
        print(f"       ! {note}")
    if not report.ok:
        rcon.close()
        return _verdict()

    by_role = {p.role: p for p in report.placed}
    drill_p = by_role.get("drill") or report.placed[0]
    furn_p = by_role.get("machine") or report.placed[-1]

    # --- Rec 5/6 : le drill mine réellement du minerai de fer ---
    # Laisser le temps au combustible de démarrer la chaîne (burner drill : ~4 s/minerai).
    api.run_action(api.wait, 300, timeout=60.0)
    rows = _rows(api)
    drill = _at(rows, drill_p.name, drill_p.x, drill_p.y)
    rec("live-5 : LE drill posé est sur une tuile iron-ore",
        bool(drill) and drill.get("mining") == "iron-ore",
        f"attendu@({drill_p.x},{drill_p.y}) trouvé={bool(drill)} "
        f"mining={drill.get('mining') if drill else None} "
        f"oreUnder={drill.get('oreUnder') if drill else None}")
    rec("live-6 : drill status=working (alimenté, extrait)",
        bool(drill) and drill.get("status") == "working",
        f"status={drill.get('status') if drill else None} "
        f"(no_fuel = alimentation KO ; waiting_for_space_in_destination = l'inserter "
        f"ne ramasse pas la drop tile -> géométrie de la chaîne)")

    # --- Rec 7 : le four fond (donc l'inserter transfère) ---
    api.run_action(api.wait, 600, timeout=90.0)
    rows = _rows(api)
    furnace = _at(rows, furn_p.name, furn_p.x, furn_p.y)
    rec("live-7 : furnace status=working (l'inserter l'alimente)",
        bool(furnace) and furnace.get("status") == "working",
        f"attendu@({furn_p.x},{furn_p.y}) trouvé={bool(furnace)} "
        f"status={furnace.get('status') if furnace else None} "
        f"(waiting_for_source_items = inserter KO, no_fuel = coal KO)")

    # --- Rec 8 : PREUVE de production : des plaques sortent de CE four ---
    if furnace:
        fx, fy = float(furnace["x"]), float(furnace["y"])
        before = api.get_state().get("inventory", {}).get("iron-plate", 0)
        api.run_action(api.walk_to, fx, fy, timeout=60.0)   # move_items_at : rayon 1.5
        api.run_action(api.move_items_at, "iron-plate", furnace["name"], fx, fy, 0, False,
                       timeout=30.0)
        after = api.get_state().get("inventory", {}).get("iron-plate", 0)
        rec("live-8 : iron-plate produite et récupérée du four posé", after > before,
            f"four@({fx},{fy}) : iron-plate {before} -> {after} (+{after - before})")
    else:
        rec("live-8 : iron-plate produite et récupérée du four posé", False,
            "skip (four introuvable à la position posée)")

    rcon.close()
    return _verdict()


def _verdict() -> int:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    print("Chaîne posée par l'executor (visible si tu te connectes en mode joueur).")
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())