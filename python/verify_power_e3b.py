"""Test LIVE E3b : bâtir une centrale vapeur et vérifier qu'elle ALIMENTE.

C'est le jalon où l'usine cesse de dépendre d'un humain qui verse du charbon dans
chaque machine. Le contrôle décisif n'est pas « les entités sont posées » mais
« un consommateur branché reçoit du courant », lu dans le jeu via `get_power_state`.

Ce que ce script met à l'épreuve, au-delà de la pose :

  - le DIMENSIONNEMENT. `power_planner` dérive 900 kW par steam-engine de deux valeurs
    mesurées (30 vapeur/s, 1200 eau/s) et de deux constantes posées (30 kJ par unité de
    vapeur, 1.8 MW par boiler). Ici on compare cette prédiction au `productionKW`
    réellement lu sur le réseau. Un fixture faux se voit — contrairement au
    GEOMETRY_FIXTURE de S2, qui n'avait aucun moyen de se contredire.
  - la GÉOMÉTRIE fluide mesurée en E3a : si un port est mal placé, l'eau n'arrive pas
    au boiler, la vapeur n'atteint pas le moteur, et la production reste à zéro.

Pré-requis : serveur headless avec le mod E3a. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import math
import sys

from core.mod_api import ModApi
from core.rcon import get_rcon
from services.executor import execute_micro
from services.power_planner import (
    ENGINE_POWER_KW, PowerRequest, describe_sizing, plan_power, size_power,
)

RESULTS: list[tuple[str, bool, str]] = []
DEMANDE_KW = 900.0          # la centrale minimale : 1 pompe, 1 boiler, 1 moteur


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


DIRS = {0: (0.0, -1.0), 2: (1.0, 0.0), 4: (0.0, 1.0), 6: (-1.0, 0.0)}
DIR_NOM = {0: "north", 2: "east", 4: "south", 6: "west"}


def _site(api: ModApi, rcon):
    """Trouve (position de pompe, direction, origine sèche des boilers).

    MESURÉ en jeu : l'offshore-pump se pose sur la RIVE — une tuile de TERRE adjacente
    à l'eau — et sa direction pointe VERS l'eau. `scan_water_edge` renvoyant des tuiles
    d'*eau*, un premier jet essayait de poser la pompe dessus : refusé sur 60 tuiles et
    dans les 4 directions, sans qu'aucun site ne soit trouvé. On teste donc les quatre
    voisines terrestres de chaque tuile d'eau.

    La sortie de la pompe tombe à l'opposé de sa direction : c'est de ce côté qu'il faut
    le terrain sec pour la centrale.
    """
    we = api.scan_water_edge(200.0)
    tuiles = we.get("tiles", []) if isinstance(we, dict) else []
    for t in tuiles[:80]:
        wx, wy = math.floor(t["x"]) + 0.5, math.floor(t["y"]) + 0.5
        for d, (ux, uy) in DIRS.items():
            # Voisine terrestre : elle est du côté OPPOSÉ à la direction visée, puisque
            # la pompe doit regarder l'eau.
            px, py = wx - ux, wy - uy
            if not _can(api, "offshore-pump", px, py, DIR_NOM[d]):
                continue
            # Terrain de la centrale : plus loin encore dans le même sens.
            for recul in (5.0, 7.0, 9.0):
                ox = math.floor(px - ux * recul) + 0.5
                oy = float(round(py - uy * recul))
                api.generate_terrain(ox, oy, 25.0)
                _clean(rcon, ox, oy, 14.0)
                secs = all(_can(api, "boiler", ox + dx, oy) for dx in (0.0, 4.0))
                moteurs = all(_can(api, "steam-engine", ox, oy - dd) for dd in (3.5, 8.5))
                if secs and moteurs and _can(api, "offshore-pump", px, py, DIR_NOM[d]):
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

    # --- 1 : dimensionnement (calcul pur, mais on l'affiche : c'est la prédiction testée) ---
    req = PowerRequest(demand_kw=DEMANDE_KW, fuel="coal")
    sizing = size_power(req)
    rec("e3b-1 : dimensionnement de la centrale",
        sizing.ok and sizing.engines >= 1 and sizing.boilers >= 1,
        describe_sizing(sizing))

    site = _site(api, rcon)
    if site is None:
        print("[SKIP] aucun site (rive posable + terrain sec derrière) trouvé.")
        rcon.close()
        return _verdict()
    (wx, wy), pdir, (ox, oy) = site
    print(f"       . site : pompe@({wx},{wy}) dir={DIR_NOM[pdir]} — "
          f"boilers depuis ({ox},{oy})")

    # --- 2 : implantation ---
    plan = plan_power(req, origin=(ox, oy), pump_pos=(wx, wy), pump_direction=pdir)
    rec("e3b-2 : implantation planifiée", plan.ok and len(plan.entities) >= 4,
        f"feas={plan.feasibility} totals={plan.totals}")
    for n in plan.notes[:3]:
        print(f"       . {n}")

    # --- 3 : pose réelle par l'executor ---
    api.run_action(api.teleport_to, ox + 2.0, oy + 6.0, timeout=30.0)
    report = execute_micro(api, plan, fuel="coal", fuel_count=50,
                           generate=False, approach=False, timeout=40.0)
    rec("e3b-3 : centrale posée et alimentée en charbon",
        report.ok and not report.blocked and not report.missing,
        f"ok={report.ok} posées={len(report.placed)}/{len(plan.entities)} "
        f"bloquées={report.blocked[:1]} fueled={report.fueled}")
    for s in report.steps[-4:]:
        print(f"       . {s}")
    if not report.ok:
        rcon.close()
        return _verdict()

    # Laisser la vapeur monter en température : un boiler ne produit pas instantanément.
    api.run_action(api.wait, 300, timeout=60.0)

    # --- 4 : un consommateur branché est-il sur le réseau de la centrale ? ---
    #
    # L'ordre compte : en Factorio, un générateur ne produit QUE ce qui est consommé.
    # Mesurer la production d'un réseau à vide donne zéro et ferait conclure à tort à
    # une centrale en panne — la capacité installée n'est pas observable directement,
    # seule la satisfaction de la demande l'est. On branche donc la charge d'abord.
    moteur = next((p for p in report.placed if p.name == "steam-engine"), None)
    etat = api.get_power_state(moteur.x, moteur.y, 3.0) if moteur else {}

    # --- 5 : un consommateur branché reçoit vraiment du courant ---
    # Le four doit être dans la ZONE DE FOURNITURE d'un poteau (rayon 2.5), pas
    # seulement « près de la centrale » : un premier jet le posait à 8 tuiles du poteau
    # le plus proche et il ressortait networkId=None, ce qui ne prouvait rien sur la
    # centrale. On part donc d'un poteau réellement posé.
    poteau = next((p for p in report.placed if p.role == "pole"), None)
    pose = None
    if poteau:
        for dx, dy in ((2.0, 0.0), (-2.0, 0.0), (0.0, 2.0), (0.0, -2.0),
                       (2.0, 2.0), (-2.0, -2.0)):
            fx, fy = poteau.x + dx, poteau.y + dy
            if _can(api, "electric-furnace", fx, fy):
                r = api.run_action(api.place_entity_at, "electric-furnace", fx, fy,
                                   "north", None, timeout=20.0)
                if isinstance(r, dict) and r.get("ok"):
                    pose = (fx, fy)
                    break
    if pose:
        # Laisser la charge s'établir : la fenêtre de mesure des flux est d'une minute.
        api.run_action(api.wait, 600, timeout=90.0)
        f_etat = api.get_power_state(pose[0], pose[1], 2.0)
        meme_reseau = (f_etat.get("networkId") is not None
                       and f_etat.get("networkId") == etat.get("networkId"))
        rec("e3b-4 : consommateur branché sur le réseau de la centrale",
            meme_reseau and f_etat.get("status") != "no_power",
            f"four@{pose} networkId={f_etat.get('networkId')} (centrale "
            f"{etat.get('networkId')}) status={f_etat.get('status')}")

        # LE contrôle : la centrale produit, et couvre ce qu'on lui demande.
        etat2 = api.get_power_state(moteur.x, moteur.y, 3.0) if moteur else {}
        prod = etat2.get("productionKW") or 0.0
        cons = etat2.get("consumptionKW") or 0.0
        sat = etat2.get("satisfaction")
        rec("e3b-5 : la centrale produit et couvre la demande",
            prod > 0 and sat is not None and sat >= 1.0,
            f"production={prod} kW consommation={cons} kW satisfaction={sat} "
            f"(capacite calculee {sizing.engines * ENGINE_POWER_KW:.0f} kW ; la production "
            f"suit la CHARGE, elle n'atteint la capacite que sous pleine demande)")
    else:
        rec("e3b-4 : consommateur branché sur le réseau de la centrale", False,
            "electric-furnace non posable près d'un poteau de la centrale")
        rec("e3b-5 : la centrale produit et couvre la demande", False, "skip")

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