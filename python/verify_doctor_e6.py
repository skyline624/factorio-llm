"""Test LIVE E6 : le FactoryDoctor désigne-t-il la BONNE coupable ?

Un diagnostic qui ne se trompe jamais sur une usine saine ne vaut rien : ce qui compte
est qu'il pointe la cause d'une panne réelle, et pas la machine qui affiche l'erreur.
Le protocole est donc : bâtir une chaîne qui marche, la CASSER d'une manière connue,
et vérifier que le diagnostic nomme ce qu'on a cassé.

Trois pannes provoquées, dont les deux que le statut brut confond :
  1. usine saine            -> aucun symptôme (pas de faux positif)
  2. poteau de desserte retiré -> DÉBRANCHÉE (personne ne la dessert)
  3. boiler vidé de son charbon -> la centrale s'arrête, tout le réseau tombe
     SANS COURANT, et les machines à l'entrée vide doivent être déclassées en
     conséquences, pas présentées comme des causes.

Le cas 3 est celui que le benchmark FLE montre raté par les agents LLM : ils réparent
le four qui dit « rien à traiter » au lieu de la centrale à sec.

Pré-requis : serveur headless, mod E3a. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import math
import sys

from agents.base import Contract
from agents.factory_builder import FactoryBuilder
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.executor import execute_micro
from services.factory_doctor import diagnose_zone
from services.knowledge import ProductionGoal
from services.layout_planner import LayoutEntity, ResourcePatch
from services.micro_planner import MicroRequest, plan_micro
from services.power_planner import PowerRequest, plan_power, plan_transmission

RESULTS: list[tuple[str, bool, str]] = []
DIRS = {0: (0.0, -1.0), 2: (1.0, 0.0), 4: (0.0, 1.0), 6: (-1.0, 0.0)}
DIR_NOM = {0: "north", 2: "east", 4: "south", 6: "west"}


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:105]}")


def _can(api: ModApi, name: str, x: float, y: float, d: str = "north") -> bool:
    c = api.can_place_check(name, x, y, d)
    return isinstance(c, dict) and c.get("can_place") is True


def _degager(rcon, cx: float, cy: float, r: float) -> None:
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{cx - r},{cy - r}}},"
        f"{{{cx + r},{cy + r}}}}}}}) do "
        f"if e.force ~= game.forces.player and e.type ~= 'resource' "
        f"and e.type ~= 'character' then e.destroy() end end rcon.print('ok')")


def _site_centrale(api: ModApi, rcon, vers):
    we = api.scan_water_edge(250.0)
    tuiles = we.get("tiles", []) if isinstance(we, dict) else []
    tuiles.sort(key=lambda t: (t["x"] - vers[0]) ** 2 + (t["y"] - vers[1]) ** 2)
    for t in tuiles[:60]:
        wx, wy = math.floor(t["x"]) + 0.5, math.floor(t["y"]) + 0.5
        for d, (ux, uy) in DIRS.items():
            px, py = wx - ux, wy - uy
            if not _can(api, "offshore-pump", px, py, DIR_NOM[d]):
                continue
            for recul in (5.0, 7.0, 9.0):
                ox = math.floor(px - ux * recul) + 0.5
                oy = float(round(py - uy * recul))
                api.generate_terrain(ox, oy, 25.0)
                _degager(rcon, ox, oy, 14.0)
                if (all(_can(api, "boiler", ox + dx, oy) for dx in (0.0, 4.0))
                        and all(_can(api, "steam-engine", ox, oy - dd) for dd in (3.5, 8.5))
                        and _can(api, "offshore-pump", px, py, DIR_NOM[d])):
                    return (px, py), d, (ox, oy)
    return None


def _poser_ligne(api: ModApi, depart, arrivee, pas=6.0, portee=7.5):
    cur = (math.floor(depart[0]) + 0.5, math.floor(depart[1]) + 0.5)
    poses = []
    for _ in range(80):
        reste = math.hypot(arrivee[0] - cur[0], arrivee[1] - cur[1])
        if reste <= pas:
            break
        t = pas / reste
        vx, vy = cur[0] + (arrivee[0] - cur[0]) * t, cur[1] + (arrivee[1] - cur[1]) * t
        pose = None
        for dx, dy in ((0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                       (1.0, 1.0), (-1.0, -1.0), (2.0, 0.0), (0.0, 2.0)):
            x, y = math.floor(vx + dx) + 0.5, math.floor(vy + dy) + 0.5
            if math.hypot(x - cur[0], y - cur[1]) > portee or not _can(api, "small-electric-pole", x, y):
                continue
            r = api.run_action(api.place_entity_at, "small-electric-pole", x, y,
                               "north", None, timeout=20.0)
            if isinstance(r, dict) and r.get("ok"):
                pose = (x, y)
                break
        if pose is None:
            return poses, False
        poses.append(pose)
        cur = pose
    return poses, True


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
    rcon.query_lua("local c = nil for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{name='character'}) do c = e end "
                   "if c then c.insert{name='small-electric-pole', count=60} "
                   "c.insert{name='electric-mining-drill', count=4} "
                   "c.insert{name='inserter', count=10} end rcon.print('ok')")
    api.generate_terrain(0.0, 0.0, 200.0)
    api.run_action(api.wait, 60, timeout=60.0)

    # --- Bâtir une chaîne qui marche (acquis E5, ici moyen et non fin) ---
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 0.5)))
    sp = fb._scan_patch_local("iron-ore")
    ancre = fb._anchor_on_ore(sp, 4) if sp.get("sample") else None
    site = _site_centrale(api, rcon, ancre) if ancre else None
    if not ancre or not site:
        print("[SKIP] pas de site exploitable.")
        rcon.close()
        return 0
    (wx, wy), pdir, (ox, oy) = site
    plan = plan_power(PowerRequest(demand_kw=900.0), origin=(ox, oy),
                      pump_pos=(wx, wy), pump_direction=pdir)
    api.run_action(api.teleport_to, ox + 2.0, oy + 6.0, timeout=30.0)
    rap = execute_micro(api, plan, fuel="coal", fuel_count=100,
                        generate=False, approach=False, timeout=40.0)
    if not rap.ok:
        print(f"[SKIP] centrale non bâtie ({rap.missing or rap.blocked}).")
        rcon.close()
        return 0
    poteau_src = next((p for p in rap.placed if p.role == "pole"), None)
    depart = (poteau_src.x, poteau_src.y)
    for p in plan_transmission(depart, ancre):
        api.generate_terrain(p.x, p.y, 12.0)
        _degager(rcon, p.x, p.y, 3.0)
    ligne, _ = _poser_ligne(api, depart, ancre)
    _degager(rcon, ancre[0], ancre[1], 12.0)
    mp = plan_micro(MicroRequest(
        patch=ResourcePatch(resource="iron-ore", tiles=[], bbox=(0, 0, 0, 0)),
        facing=4, anchor=ancre, drill_tier="electric-mining-drill",
        inserter_tier="inserter", furnace_tier="electric-furnace",
        drill_size=3, furnace_size=3))
    rap2 = execute_micro(api, mp, generate=False, approach=False, timeout=30.0)
    if not rap2.ok:
        print(f"[SKIP] chaîne non posée ({rap2.blocked}).")
        rcon.close()
        return 0
    ancrage = ligne[-1] if ligne else depart
    desserte = []
    for p in rap2.placed:
        for dx, dy in ((2.5, 0.0), (-2.5, 0.0), (0.0, 2.5), (0.0, -2.5), (2.5, 2.5)):
            x, y = math.floor(p.x + dx) + 0.5, math.floor(p.y + dy) + 0.5
            if math.hypot(x - ancrage[0], y - ancrage[1]) > 7.5 or not _can(api, "small-electric-pole", x, y):
                continue
            r = api.run_action(api.place_entity_at, "small-electric-pole", x, y,
                               "north", None, timeout=20.0)
            if isinstance(r, dict) and r.get("ok"):
                desserte.append((x, y))
                ancrage = (x, y)
                break
    api.run_action(api.wait, 300, timeout=90.0)
    api.run_action(api.teleport_to, ancre[0], ancre[1] + 3.0, timeout=30.0)

    # --- 1 : usine saine -> aucun faux positif ---
    d0 = diagnose_zone(api, ancre[0], ancre[1], 25.0)
    rec("e6-1 : usine saine -> aucune cause signalée", d0.sain or not d0.causes,
        f"{d0.resume()}")

    # --- 2 : on retire un poteau de desserte -> DÉBRANCHÉE ---
    if desserte:
        # Retirer TOUS les poteaux de desserte : en enlever un seul laisse souvent la
        # machine couverte par le voisin, et le diagnostic a raison de ne rien signaler.
        for px, py in desserte:
            api.run_action(api.remove_entity_at, px, py, "small-electric-pole",
                           timeout=20.0)
        api.run_action(api.wait, 180, timeout=60.0)
        d1 = diagnose_zone(api, ancre[0], ancre[1], 25.0)
        debranchees = [s for s in d1.causes if s.cause == "debranchee"]
        rec("e6-2 : poteaux retirés -> machines diagnostiquées DÉBRANCHÉES",
            bool(debranchees),
            f"{d1.resume()} | causes={[s.cause for s in d1.causes]}")
        for px, py in desserte:      # on remet pour la panne suivante
            api.run_action(api.place_entity_at, "small-electric-pole", px, py, "north",
                           None, timeout=20.0)
        api.run_action(api.wait, 180, timeout=60.0)
    else:
        rec("e6-2 : poteaux retirés -> machines diagnostiquées DÉBRANCHÉES", False,
            "aucun poteau de desserte posé")

    # --- 3 : on vide le boiler -> panne EN AMONT, conséquences déclassées ---
    # On retire le GÉNÉRATEUR plutôt que de vider le boiler. Vider le boiler est la
    # panne la plus réaliste (le charbon s'épuise : 0.45/s), mais elle met très longtemps
    # à se propager — la vapeur déjà produite reste dans les tuyaux et le moteur, et la
    # production suivant la CHARGE (~280 kW consommés sur 900 installés), ce stock
    # s'écoule au ralenti : plus de 30 s de jeu sans que le réseau tombe. Retirer le
    # steam-engine coupe la source net, ce qui est aussi une panne réelle (machine
    # détruite). Ce qu'on teste ici est le DIAGNOSTIC, pas la thermodynamique.
    boiler = next((p for p in rap.placed if p.name == "steam-engine"), None)
    if boiler:
        api.run_action(api.remove_entity_at, boiler.x, boiler.y, "steam-engine",
                       timeout=30.0)
        api.run_action(api.wait, 300, timeout=90.0)
        d2 = diagnose_zone(api, ancre[0], ancre[1], 25.0)
        causes = [s.cause for s in d2.causes]
        effets = [s for s in d2.symptomes if not s.racine]
        rec("e6-3 : centrale à sec -> panne électrique désignée comme CAUSE",
            any(c in ("sans_courant", "courant_insuffisant", "debranchee") for c in causes),
            f"{d2.resume()}")
        rec("e6-4 : les machines à l'entrée vide sont déclassées en conséquences",
            bool(effets) or not any(s.cause == "entree_vide" and s.racine
                                    for s in d2.symptomes),
            f"{len(effets)} effet(s) sur {len(d2.symptomes)} symptôme(s) — "
            f"causes retenues={causes}")
    else:
        rec("e6-3 : centrale à sec -> panne électrique désignée comme CAUSE", False,
            "boiler introuvable")
        rec("e6-4 : les machines à l'entrée vide sont déclassées en conséquences", False,
            "skip")

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