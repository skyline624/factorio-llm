"""Test LIVE de l'Executor E1 en mode PRODUCTION : le personnage MARCHE et bâtit.

Pendant `verify_executor_live.py` le mod tourne en `test_mode` : character headless,
déplacement par teleport-step, et surtout **aucune contrainte de portée à la pose**.
Ici on coupe `test_mode` : le mod pilote le character du JOUEUR CONNECTÉ, avec la
physique réelle. Trois différences que le mode test masque entièrement
(cf. `mod/scripts/task_manager.lua`) :

  1. `state_walking` marche vraiment (walking_state + détection de blocage + recompute
     de chemin) au lieu de téléporter le character le long du chemin ;
  2. `state_placing_at` REFUSE la pose au-delà de `player.build_distance + 2`
     -> `"walk closer first"`. C'est le vrai juge de l'ordre approche/pose de
     l'executor : en test_mode il pourrait bâtir à 200 tuiles sans s'en apercevoir ;
  3. `create_entity` reçoit `player = p` (la pose compte comme une construction joueur).

PRÉ-REQUIS : serveur headless lancé ET **un joueur connecté** (scripts/start_factorio_client.bat).
Sans joueur, `get_ai_entity()` est nil : le script remet `test_mode` et SKIP (return 0),
comme les autres verify_*.

MISE EN CONDITION (assumée, et faite par RCON hors du mod) :
  - `reset_character` est REFUSÉ en production par design (operations.lua:220 — le mod ne
    touche jamais au character d'un joueur), et le kit du mod est idempotent via
    `storage.fl.kit_given`, flag interne au mod donc inaccessible depuis une commande RCON.
    Le script complète donc l'inventaire du joueur par `player.insert` : c'est l'équivalent
    prod de `reset_character`, nécessaire pour que le run soit reproductible.
  - La chaîne d'un run précédent est détruite autour de la bbox du plan, sinon la pose
    échoue sur ses propres entités (l'ancre est déterministe : même gisement, même position).

Ces deux étapes préparent le TERRAIN ; rien de ce qui est mesuré ensuite (marche, portée,
pose, production) n'est simulé.
"""

from __future__ import annotations

import sys

from agents.base import Contract
from agents.factory_builder import FactoryBuilder
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.executor import execute_micro
from services.knowledge import ProductionGoal

RESULTS: list[tuple[str, bool, str]] = []

# La marche est réelle : ~112 tuiles depuis le spawn jusqu'au gisement de fer, plus le
# pathfinding et les contournements. Le défaut de l'executor (20 s) est calibré pour le
# teleport-step du mode test et serait dépassé dès l'approche.
WALK_TIMEOUT = 300.0

# Complément d'inventaire = sous-ensemble du kit du mod (player.lua STARTING_ITEMS)
# strictement nécessaire à la micro-chaîne + son carburant.
KIT = (("burner-mining-drill", 4), ("stone-furnace", 4), ("burner-inserter", 10),
       ("coal", 100))


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:110]}")


def _rows(api: ModApi) -> list[dict]:
    sf = api.scan_factory()
    return sf.get("entities", []) if isinstance(sf, dict) else []


def _at(rows: list[dict], name: str, x: float, y: float, tol: float = 1.6) -> dict | None:
    """L'entité `name` posée AUTOUR de (x, y) — jamais par type seul.

    Les runs précédents laissent des machines sur la carte ; cibler par nom seul validerait
    les leurs. Tolérance = snap de grille (±0.5) + marge.
    """
    for r in rows:
        if r.get("name") != name:
            continue
        if abs(float(r.get("x", 1e9)) - x) <= tol and abs(float(r.get("y", 1e9)) - y) <= tol:
            return r
    return None


def _pos(api: ModApi) -> tuple[float, float]:
    ch = api.get_state().get("character") or {}
    p = ch.get("position") or {}
    return float(p.get("x", 0.0)), float(p.get("y", 0.0))


def main() -> int:
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    # --- Rec 1 : mode production actif ET un joueur connecté pour l'incarner ---
    api.set_test_mode(False)
    api.setup()
    st = api.get_state()
    if not st.get("ready"):
        api.set_test_mode(True)   # on ne laisse pas le serveur inutilisable pour les autres verify
        print("[SKIP] aucun joueur connecté : lance scripts/start_factorio_client.bat, "
              "rejoins le serveur, puis relance ce script (test_mode restauré).")
        rcon.close()
        return 0
    rec("prod-1 : mode production + joueur connecté",
        st.get("test_mode") is False and st.get("ready") is True,
        f"test_mode={st.get('test_mode')} ready={st.get('ready')} "
        f"position={(st.get('character') or {}).get('position')}")

    # --- Rec 2 : inventaire suffisant (complété par RCON, cf. docstring) ---
    ins = "".join(f"p.insert{{name='{n}', count={c}}} " for n, c in KIT)
    rcon.query_lua(f"local p = game.players[1] if p and p.character then {ins} end "
                   f"rcon.print('ok')")
    inv = api.get_state().get("inventory", {})
    kit_ok = (all(inv.get(n, 0) >= 1 for n in
                  ("burner-mining-drill", "burner-inserter", "stone-furnace"))
              and inv.get("coal", 0) >= 15)
    rec("prod-2 : kit de bâtisseur dans l'inventaire joueur", kit_ok,
        f"drill={inv.get('burner-mining-drill', 0)} ins={inv.get('burner-inserter', 0)} "
        f"furn={inv.get('stone-furnace', 0)} coal={inv.get('coal', 0)}")

    # --- Rec 3 : le plan (calcul seul, rien de posé) ---
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 0.5)))
    plan = fb.build_micro_layout("iron-ore")
    rec("prod-3 : plan_micro ancré sur du minerai -> 3 entités",
        plan.feasibility == "ok" and len(plan.entities) == 3,
        f"feas={plan.feasibility} entities="
        f"{[(e.role, e.name, e.x, e.y) for e in plan.entities]}")
    for note in plan.notes[:2]:
        print(f"       . {note}")
    if plan.feasibility != "ok":
        rcon.close()
        return _verdict()

    # Terrain propre : les entités d'un run précédent occupent exactement ces positions.
    x1, y1, x2, y2 = plan.bbox
    n_det = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{x1 - 6},{y1 - 6}}},"
        f"{{{x2 + 6},{y2 + 6}}}}}}}) do "
        f"local t = e.type "
        f"if t == 'mining-drill' or t == 'inserter' or t == 'furnace' or t == 'item-entity' "
        f"then e.destroy() n = n + 1 end end rcon.print(n)")
    print(f"       . terrain nettoyé autour de la bbox : {str(n_det).strip()} entité(s) détruite(s)")

    # --- Rec 4/5 : la MARCHE réelle + la pose sous contrainte de portée ---
    depart = _pos(api)
    report = execute_micro(api, plan, timeout=WALK_TIMEOUT)
    arrivee = _pos(api)
    parcouru = ((arrivee[0] - depart[0]) ** 2 + (arrivee[1] - depart[1]) ** 2) ** 0.5

    # Le personnage doit s'être VRAIMENT déplacé vers le chantier (pas de teleport en prod).
    cible = ((x1 + x2) / 2.0, (y1 + y2) / 2.0)
    reste = ((arrivee[0] - cible[0]) ** 2 + (arrivee[1] - cible[1]) ** 2) ** 0.5
    rec("prod-4 : le personnage a marché jusqu'au chantier",
        parcouru > 20.0 and reste < 12.0,
        f"départ=({depart[0]:.1f},{depart[1]:.1f}) arrivée=({arrivee[0]:.1f},{arrivee[1]:.1f}) "
        f"parcouru={parcouru:.1f} tuiles, reste {reste:.1f} du centre du chantier")

    rec("prod-5 : execute_micro -> 3 entités posées + alimentées",
        report.ok and len(report.placed) == 3 and not report.missing and not report.blocked,
        f"ok={report.ok} placed={[(p.name, p.x, p.y) for p in report.placed]} "
        f"missing={report.missing} blocked={report.blocked} fueled={report.fueled}")
    for step in report.steps:
        print(f"       . {step}")
    for note in report.notes:
        print(f"       ! {note}")

    # --- Rec 6 : aucune pose refusée pour cause de portée (le juge du mode production) ---
    hors_portee = [b for b in report.blocked if "walk closer" in str(b).lower()]
    rec("prod-6 : aucune pose refusée pour distance (build_distance)",
        not hors_portee,
        f"blocked={report.blocked or 'aucun'} — 'walk closer first' = l'executor a posé "
        f"avant d'être à portée (impossible à détecter en test_mode)")
    if not report.ok:
        rcon.close()
        return _verdict()

    by_role = {p.role: p for p in report.placed}
    drill_p = by_role.get("drill") or report.placed[0]
    furn_p = by_role.get("machine") or report.placed[-1]

    # --- Rec 7/8 : la chaîne produit pour de vrai ---
    api.run_action(api.wait, 300, timeout=60.0)
    rows = _rows(api)
    drill = _at(rows, drill_p.name, drill_p.x, drill_p.y)
    rec("prod-7 : drill posé sur iron-ore et status=working",
        bool(drill) and drill.get("mining") == "iron-ore" and drill.get("status") == "working",
        f"attendu@({drill_p.x},{drill_p.y}) trouvé={bool(drill)} "
        f"mining={drill.get('mining') if drill else None} "
        f"oreUnder={drill.get('oreUnder') if drill else None} "
        f"status={drill.get('status') if drill else None}")

    api.run_action(api.wait, 600, timeout=90.0)
    rows = _rows(api)
    furnace = _at(rows, furn_p.name, furn_p.x, furn_p.y)
    rec("prod-8 : furnace status=working (l'inserter l'alimente)",
        bool(furnace) and furnace.get("status") == "working",
        f"attendu@({furn_p.x},{furn_p.y}) trouvé={bool(furnace)} "
        f"status={furnace.get('status') if furnace else None}")

    # --- Rec 9 : PREUVE de production, récupérée à la main (move_items_at rayon 1.5) ---
    if furnace:
        fx, fy = float(furnace["x"]), float(furnace["y"])
        before = api.get_state().get("inventory", {}).get("iron-plate", 0)
        api.run_action(api.walk_to, fx, fy, timeout=WALK_TIMEOUT)
        api.run_action(api.move_items_at, "iron-plate", furnace["name"], fx, fy, 0, False,
                       timeout=30.0)
        after = api.get_state().get("inventory", {}).get("iron-plate", 0)
        rec("prod-9 : iron-plate produite et récupérée du four posé", after > before,
            f"four@({fx},{fy}) : iron-plate {before} -> {after} (+{after - before})")
    else:
        rec("prod-9 : iron-plate produite et récupérée du four posé", False,
            "skip (four introuvable à la position posée)")

    # --- Rec 10 : le personnage a survécu (en prod il encaisse vraiment les dégâts) ---
    ch = api.get_state().get("character") or {}
    hp, hp_max = ch.get("health"), ch.get("max_health")
    rec("prod-10 : personnage vivant en fin de run", bool(ch) and (hp or 0) > 0,
        f"health={hp}/{hp_max} (0 ou absent = mort en route, cf. biters peaceful=false)")

    rcon.close()
    return _verdict()


def _verdict() -> int:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    print("Le mod reste en mode PRODUCTION. Pour rebasculer : api.set_test_mode(True) "
          "(les autres verify_* l'appellent eux-mêmes).")
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())