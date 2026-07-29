"""Test LIVE E4 : une machine ÉLECTRIQUE avec une RECETTE produit vraiment.

Jonction des deux chantiers précédents, sur le minimum d'entités qui la démontre :
la centrale d'E3b fournit le courant, et l'assembleur reçoit sa recette par les
options de pose d'E2 — celles que le planner remplit désormais (E4a).

Jusqu'ici le projet savait produire avec des machines à combustible (chaîne burner
drill -> four). Ici rien ne brûle du charbon sauf la centrale : l'assembleur est
alimenté par un réseau, et il ne produirait RIEN sans recette. Les deux verrous du
jalon « automatisation » sont donc testés ensemble et par leur résultat, pas par
leur pose.

Déroulé : centrale (plan_power + executor) -> assembleur posé AVEC sa recette dans
la zone de fourniture d'un poteau -> plaques injectées -> des engrenages sortent.

Pré-requis : serveur headless avec le mod E3a. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import math
import sys

from core.mod_api import ModApi
from core.rcon import get_rcon
from services.executor import execute_micro
from services.layout_planner import LayoutEntity
from services.micro_planner import MicroPlan
from services.power_planner import PowerRequest, describe_sizing, plan_power, size_power

RESULTS: list[tuple[str, bool, str]] = []
DIRS = {0: (0.0, -1.0), 2: (1.0, 0.0), 4: (0.0, 1.0), 6: (-1.0, 0.0)}
DIR_NOM = {0: "north", 2: "east", 4: "south", 6: "west"}
RECETTE = "iron-gear-wheel"


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:50s} {detail[:105]}")


def _can(api: ModApi, name: str, x: float, y: float, d: str = "north") -> bool:
    c = api.can_place_check(name, x, y, d)
    return isinstance(c, dict) and c.get("can_place") is True


def _clean(rcon, cx: float, cy: float, r: float = 22.0) -> int:
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{cx - r},{cy - r}}},"
        f"{{{cx + r},{cy + r}}}}}}}) do "
        f"if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return -1


def _site(api: ModApi, rcon):
    """(position de pompe, direction, origine sèche) — cf. E3b : la pompe va sur la RIVE."""
    we = api.scan_water_edge(200.0)
    for t in (we.get("tiles", []) if isinstance(we, dict) else [])[:80]:
        wx, wy = math.floor(t["x"]) + 0.5, math.floor(t["y"]) + 0.5
        for d, (ux, uy) in DIRS.items():
            px, py = wx - ux, wy - uy
            if not _can(api, "offshore-pump", px, py, DIR_NOM[d]):
                continue
            for recul in (5.0, 7.0, 9.0):
                ox = math.floor(px - ux * recul) + 0.5
                oy = float(round(py - uy * recul))
                api.generate_terrain(ox, oy, 25.0)
                _clean(rcon, ox, oy, 14.0)
                if (all(_can(api, "boiler", ox + dx, oy) for dx in (0.0, 4.0))
                        and all(_can(api, "steam-engine", ox, oy - dd) for dd in (3.5, 8.5))
                        and _can(api, "offshore-pump", px, py, DIR_NOM[d])):
                    return (px, py), d, (ox, oy)
    return None


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
    api.generate_terrain(0.0, 0.0, 150.0)
    api.run_action(api.wait, 30, timeout=30.0)

    site = _site(api, rcon)
    if site is None:
        print("[SKIP] aucun site de centrale trouvé.")
        rcon.close()
        return 0
    (wx, wy), pdir, (ox, oy) = site

    # --- 1 : la centrale (acquis E3b, ici moyen et non fin) ---
    req = PowerRequest(demand_kw=900.0, fuel="coal")
    plan = plan_power(req, origin=(ox, oy), pump_pos=(wx, wy), pump_direction=pdir)
    api.run_action(api.teleport_to, ox + 2.0, oy + 6.0, timeout=30.0)
    rap = execute_micro(api, plan, fuel="coal", fuel_count=50,
                        generate=False, approach=False, timeout=40.0)
    rec("e4-1 : centrale bâtie et allumée", rap.ok and not rap.blocked,
        f"{describe_sizing(size_power(req))} — posées={len(rap.placed)} fueled={rap.fueled}")
    if not rap.ok:
        rcon.close()
        return _verdict()
    api.run_action(api.wait, 180, timeout=60.0)

    # --- 2 : l'assembleur, posé AVEC sa recette, dans la zone d'un poteau ---
    # La recette voyage dans l'entité (E4a) et c'est l'executor qui la pose (E2) :
    # on ne la règle pas à la main, sinon le test ne vérifierait que set_recipe_at.
    poteau = next((p for p in rap.placed if p.role == "pole"), None)
    machine = None
    if poteau:
        for dx, dy in ((2.5, 0.0), (-2.5, 0.0), (0.0, 2.5), (0.0, -2.5), (2.5, 2.5)):
            mx, my = math.floor(poteau.x + dx) + 0.5, math.floor(poteau.y + dy) + 0.5
            if _can(api, "assembling-machine-1", mx, my):
                machine = (mx, my)
                break
    if machine is None:
        rec("e4-2 : assembleur posé avec sa recette", False,
            "aucune position libre dans la zone de fourniture d'un poteau")
        rcon.close()
        return _verdict()

    ent = LayoutEntity("assembling-machine-1", machine[0], machine[1], 0, "machine")
    ent.recipe = RECETTE
    mini = MicroPlan(entities=[ent], totals={"assembling-machine-1": 1}, feasibility="ok")
    rap2 = execute_micro(api, mini, generate=False, approach=False, timeout=30.0)
    rec("e4-2 : assembleur posé par l'executor", rap2.ok and len(rap2.placed) == 1,
        f"@{machine} ok={rap2.ok} bloqué={rap2.blocked}")
    for s in rap2.steps:
        print(f"       . {s}")
    if not rap2.ok:
        rcon.close()
        return _verdict()

    # --- 3 : la recette est bien SUR la machine (relue dans le jeu) ---
    api.run_action(api.wait, 60, timeout=30.0)
    sf = api.scan_factory()
    trouve = None
    for e in (sf.get("entities", []) if isinstance(sf, dict) else []):
        if (e.get("name") == "assembling-machine-1"
                and abs(float(e.get("x", 1e9)) - machine[0]) <= 1.6
                and abs(float(e.get("y", 1e9)) - machine[1]) <= 1.6):
            trouve = e
            break
    rec("e4-3 : la recette est posée sur la machine",
        bool(trouve) and trouve.get("recipe") == RECETTE,
        f"recipe={trouve.get('recipe') if trouve else None} (attendu {RECETTE})")

    # --- 4 : elle est ALIMENTÉE par la centrale (et pas simplement posée) ---
    etat = api.get_power_state(machine[0], machine[1], 2.0)
    moteur = next((p for p in rap.placed if p.name == "steam-engine"), None)
    centrale = api.get_power_state(moteur.x, moteur.y, 3.0) if moteur else {}
    rec("e4-4 : la machine est sur le réseau de la centrale",
        etat.get("networkId") is not None
        and etat.get("networkId") == centrale.get("networkId")
        and etat.get("status") != "no_power",
        f"networkId={etat.get('networkId')} (centrale {centrale.get('networkId')}) "
        f"status={etat.get('status')}")

    # --- 5 : PREUVE — des engrenages sortent d'une machine électrique ---
    api.run_action(api.move_items_at, "iron-plate", "assembling-machine-1",
                   machine[0], machine[1], 40, True, timeout=30.0)
    avant = api.get_state().get("inventory", {}).get(RECETTE, 0)
    api.run_action(api.wait, 420, timeout=90.0)
    api.run_action(api.move_items_at, RECETTE, "assembling-machine-1",
                   machine[0], machine[1], 0, False, timeout=30.0)
    apres = api.get_state().get("inventory", {}).get(RECETTE, 0)
    rec("e4-5 : la machine électrique PRODUIT (engrenages récupérés)", apres > avant,
        f"{RECETTE} {avant} -> {apres} (+{apres - avant}) — aucun combustible dans "
        f"cette machine, tout vient du réseau")

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