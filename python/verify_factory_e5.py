"""Test LIVE E5 : chaîne de production ENTIÈREMENT électrique, entrée automatisée.

Aboutissement des chantiers précédents. Jusqu'ici toute production passait par des
machines à combustible qu'un humain remplissait (chaîne burner drill -> four), ou par
un assembleur électrique dont on injectait les ingrédients à la main (E4). Ici :

    centrale (E3b) --- ligne de poteaux (E5a) ---> drill électrique
                                                        | (drop)
                                                   inserter électrique
                                                        v
                                                   four électrique -> plaques

Aucune de ces trois machines ne contient de combustible : tout vient du réseau, et
le minerai entre dans la chaîne sans intervention. C'est la différence entre « une
machine qui produit » et « une usine qui tourne toute seule ».

Le transport d'électricité n'est pas un détail de mise en scène : mesuré sur cette
carte, le premier plan d'eau est à ~108 tuiles du moindre gisement. Une centrale ne
sert à rien si son courant n'atteint pas les machines.

MISE EN CONDITION (par RCON, assumée) : le kit du mod contient 20 small-electric-pole,
or la ligne en demande 17 plus ceux de la centrale et des machines. On complète
l'inventaire — c'est du matériel, pas de la triche sur le résultat mesuré.

Pré-requis : serveur headless avec le mod E3a. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import math
import sys

from agents.base import Contract
from agents.factory_builder import FactoryBuilder
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.executor import execute_micro
from services.knowledge import ProductionGoal
from services.layout_planner import LayoutEntity, ResourcePatch
from services.micro_planner import MicroPlan, MicroRequest, plan_micro
from services.power_planner import PowerRequest, plan_power, plan_transmission

RESULTS: list[tuple[str, bool, str]] = []
DIRS = {0: (0.0, -1.0), 2: (1.0, 0.0), 4: (0.0, 1.0), 6: (-1.0, 0.0)}
DIR_NOM = {0: "north", 2: "east", 4: "south", 6: "west"}
FACING = 4                      # la chaîne descend vers le sud
RESSOURCE = "iron-ore"


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:105]}")


def _can(api: ModApi, name: str, x: float, y: float, d: str = "north") -> bool:
    c = api.can_place_check(name, x, y, d)
    return isinstance(c, dict) and c.get("can_place") is True


def _clean(rcon, cx: float, cy: float, r: float) -> int:
    """Dégage une zone AVANT d'y bâtir : tout sauf le personnage et les ressources."""
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{cx - r},{cy - r}}},"
        f"{{{cx + r},{cy + r}}}}}}}) do "
        f"if e.type ~= 'character' and e.type ~= 'resource' then e.destroy() n = n + 1 end "
        f"end rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return -1


def _degager(rcon, cx: float, cy: float, r: float) -> int:
    """Enlève les obstacles NATURELS, en épargnant ce qu'on a construit.

    Nuance qui a coûté un run : `_clean` rase tout sauf le personnage et les ressources.
    Utilisé pour dégager le couloir de la ligne électrique, il a détruit le poteau de la
    centrale qu'on venait de poser — le premier point du couloir tombant juste à côté.
    La ligne partait alors d'un poteau fantôme, à 8.49 tuiles du suivant (portée 7.5),
    et formait son propre réseau. Rien ne le signalait : aucune pose n'avait échoué.
    """
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{cx - r},{cy - r}}},"
        f"{{{cx + r},{cy + r}}}}}}}) do "
        f"if e.force ~= game.forces.player and e.type ~= 'resource' "
        f"and e.type ~= 'character' then e.destroy() n = n + 1 end "
        f"end rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return -1


def _site_centrale(api: ModApi, rcon, vers: tuple[float, float]):
    """Rive posable la plus proche de `vers`, avec du sec derrière pour la centrale."""
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
                _clean(rcon, ox, oy, 14.0)
                if (all(_can(api, "boiler", ox + dx, oy) for dx in (0.0, 4.0))
                        and all(_can(api, "steam-engine", ox, oy - dd) for dd in (3.5, 8.5))
                        and _can(api, "offshore-pump", px, py, DIR_NOM[d])):
                    return (px, py), d, (ox, oy)
    return None


def _poser_poteaux(api: ModApi, poteaux, timeout: float = 20.0) -> tuple[int, int]:
    """Pose des poteaux isolés (desserte), en décalant ceux que le terrain refuse."""
    poses, echecs = 0, 0
    for p in poteaux:
        place = False
        for dx, dy in ((0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                       (2.0, 0.0), (-2.0, 0.0), (0.0, 2.0), (0.0, -2.0)):
            x, y = p.x + dx, p.y + dy
            if not _can(api, p.name, x, y):
                continue
            r = api.run_action(api.place_entity_at, p.name, x, y, "north", None,
                               timeout=timeout)
            if isinstance(r, dict) and r.get("ok"):
                poses += 1
                place = True
                break
        if not place:
            echecs += 1
    return poses, echecs


def _poser_ligne(api: ModApi, depart: tuple[float, float], arrivee: tuple[float, float],
                 pas: float = 6.0, portee: float = 7.5, timeout: float = 20.0):
    """Pose une ligne connexe en CHAÎNANT depuis la position RÉELLEMENT posée.

    Suivre la ligne théorique ne suffit pas : chaque poteau que le terrain refuse est
    décalé de une ou deux tuiles, et deux décalages en sens opposés créent un saut
    supérieur à la portée de fil. Le réseau se coupe alors en deux — et rien ne le
    signale à la pose : les 16 poteaux du premier run étaient tous posés, `networkId`
    valait 15 côté machines et 11 côté centrale.

    On repart donc de la dernière position réelle, et on refuse tout candidat au-delà
    de la portée plutôt que de fermer les yeux.
    """
    cur = (math.floor(depart[0]) + 0.5, math.floor(depart[1]) + 0.5)
    poses: list[tuple[float, float]] = []
    garde = 0
    while garde < 80:
        garde += 1
        reste = math.hypot(arrivee[0] - cur[0], arrivee[1] - cur[1])
        if reste <= pas:
            break
        t = pas / reste
        vx = cur[0] + (arrivee[0] - cur[0]) * t
        vy = cur[1] + (arrivee[1] - cur[1]) * t
        pose = None
        for dx, dy in ((0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                       (1.0, 1.0), (-1.0, -1.0), (2.0, 0.0), (0.0, 2.0),
                       (-2.0, 0.0), (0.0, -2.0)):
            x = math.floor(vx + dx) + 0.5
            y = math.floor(vy + dy) + 0.5
            if math.hypot(x - cur[0], y - cur[1]) > portee:
                continue          # au-delà de la portée : couperait la ligne
            if not _can(api, "small-electric-pole", x, y):
                continue
            r = api.run_action(api.place_entity_at, "small-electric-pole", x, y,
                               "north", None, timeout=timeout)
            if isinstance(r, dict) and r.get("ok"):
                pose = (x, y)
                break
        if pose is None:
            return poses, False   # trou infranchissable : la ligne s'arrête là
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
    # Table rase du BÂTI. `reset_character` remet l'inventaire, pas la carte : les runs
    # précédents laissent centrales et lignes de poteaux un peu partout, et une nouvelle
    # ligne se raccorde alors à un ANCIEN réseau. Symptôme observé : ligne sur le réseau
    # 41 (un run d'avant) pendant que la centrale du jour était sur 59, sans qu'aucune
    # pose n'ait échoué. Un test live doit repartir d'une carte propre.
    efface = rcon.query_lua(
        "local n = 0 "
        "for _, e in pairs(game.surfaces[1].find_entities_filtered{force='player'}) do "
        "if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    print(f"       . table rase : {str(efface).strip()} entité(s) des runs précédents effacée(s)")
    api.generate_terrain(0.0, 0.0, 200.0)
    api.run_action(api.wait, 60, timeout=60.0)
    # Matériel : la ligne en demande plus que le kit n'en contient.
    rcon.query_lua("local p = game.players[1] "
                   "local c = p and p.character or nil "
                   "if not c then for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{name='character'}) do c = e break end end "
                   "if c then c.insert{name='small-electric-pole', count=60} "
                   "c.insert{name='electric-mining-drill', count=4} "
                   "c.insert{name='inserter', count=10} end rcon.print('ok')")

    # --- 1 : le gisement, et une ancre réellement sur du minerai ---
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 0.5)))
    sp = fb._scan_patch_local(RESSOURCE)
    ancre = fb._anchor_on_ore(sp, FACING) if sp.get("sample") else None
    rec("e5-1 : gisement localisé et ancre sur une tuile de minerai", ancre is not None,
        f"count={sp.get('count')} rayon={sp.get('_radius')} ancre={ancre}")
    if ancre is None:
        rcon.close()
        return _verdict()

    # --- 2 : centrale, au bord d'eau le plus proche du gisement ---
    site = _site_centrale(api, rcon, ancre)
    if site is None:
        rec("e5-2 : centrale bâtie près du gisement", False, "aucun site de centrale")
        rcon.close()
        return _verdict()
    (wx, wy), pdir, (ox, oy) = site
    dist = math.hypot(ox - ancre[0], oy - ancre[1])
    req = PowerRequest(demand_kw=900.0, fuel="coal")
    plan = plan_power(req, origin=(ox, oy), pump_pos=(wx, wy), pump_direction=pdir)
    api.run_action(api.teleport_to, ox + 2.0, oy + 6.0, timeout=30.0)
    rap = execute_micro(api, plan, fuel="coal", fuel_count=100,
                        generate=False, approach=False, timeout=40.0)
    rec("e5-2 : centrale bâtie et allumée", rap.ok and not rap.blocked,
        f"{len(rap.placed)} entités, fueled={rap.fueled}, à {dist:.0f} tuiles du gisement "
        f"| missing={rap.missing} blocked={rap.blocked[:1]} notes={rap.notes[:1]}")
    if not rap.ok:
        rcon.close()
        return _verdict()

    # --- 3 : la ligne de poteaux jusqu'au gisement ---
    poteau_src = next((p for p in rap.placed if p.role == "pole"), None)
    depart = (poteau_src.x, poteau_src.y) if poteau_src else (ox, oy)
    # Dégager le couloir : sur 100+ tuiles, arbres et rochers sont certains.
    for p in plan_transmission(depart, ancre):
        api.generate_terrain(p.x, p.y, 12.0)
        _degager(rcon, p.x, p.y, 3.0)      # arbres/rochers seulement : la centrale reste
    ligne, complete = _poser_ligne(api, depart, ancre)
    # Contrôle réel : tous les poteaux partagent-ils le réseau DE LA CENTRALE ? Un saut
    # trop long les scinde en deux réseaux sans rien signaler à la pose. Le réseau de
    # référence se lit sur le steam-engine — une position quelconque de la centrale peut
    # tomber sur le boiler, qui est à combustible et n'a pas de networkId.
    moteur0 = next((p for p in rap.placed if p.name == "steam-engine"), None)
    net_centrale = (api.get_power_state(moteur0.x, moteur0.y, 3.0).get("networkId")
                    if moteur0 else None)
    nets = {api.get_power_state(x, y, 1.5).get("networkId") for x, y in ligne}
    # Jonction de départ : c'est là que la ligne se raccroche (ou non) à la centrale.
    net_depart = api.get_power_state(depart[0], depart[1], 1.5)
    if ligne:
        d0 = math.hypot(ligne[0][0] - depart[0], ligne[0][1] - depart[1])
        print(f"       . jonction : poteau centrale@{depart} réseau={net_depart.get('networkId')}"
              f" (nom={net_depart.get('name')}) -> 1er poteau ligne@{ligne[0]} à {d0:.2f} tuiles")
    rec("e5-3 : ligne électrique CONNEXE jusqu'au gisement",
        complete and bool(ligne) and net_centrale is not None and nets == {net_centrale},
        f"{len(ligne)} poteaux, complète={complete}, réseaux traversés="
        f"{sorted(n for n in nets if n)} (centrale {net_centrale}) — trajet {dist:.0f} tuiles")

    # --- 4 : la micro-chaîne, tout électrique ---
    _degager(rcon, ancre[0], ancre[1], 12.0)
    patch = ResourcePatch(resource=RESSOURCE, tiles=[], bbox=(0, 0, 0, 0))
    mp = plan_micro(MicroRequest(
        patch=patch, facing=FACING, anchor=ancre,
        drill_tier="electric-mining-drill", inserter_tier="inserter",
        furnace_tier="electric-furnace", drill_size=3, furnace_size=3))
    rap2 = execute_micro(api, mp, generate=False, approach=False, timeout=30.0)
    rec("e5-4 : chaîne électrique posée (drill + inserter + four)",
        rap2.ok and len(rap2.placed) == 3,
        f"ok={rap2.ok} posées={[(p.name, p.x, p.y) for p in rap2.placed]} "
        f"bloquées={rap2.blocked[:1]}")
    for n in mp.notes[:1]:
        print(f"       . {n}")
    if not rap2.ok:
        rcon.close()
        return _verdict()

    # Poteaux de desserte : le MicroPlanner est tout-burner par conception, il n'en
    # pose pas. Chaque machine doit être dans une zone de fourniture (rayon 2.5).
    # Chaque poteau de desserte doit rester à portée de fil du dernier poteau de la
    # ligne (ou d'un autre poteau de desserte), sinon la chaîne forme son propre réseau.
    ancrage = ligne[-1] if ligne else depart
    desserte = []
    for p in rap2.placed:
        for dx, dy in ((2.5, 0.0), (-2.5, 0.0), (0.0, 2.5), (0.0, -2.5),
                       (2.5, 2.5), (-2.5, -2.5)):
            x, y = math.floor(p.x + dx) + 0.5, math.floor(p.y + dy) + 0.5
            if math.hypot(x - ancrage[0], y - ancrage[1]) > 7.5:
                continue
            if _can(api, "small-electric-pole", x, y):
                desserte.append(LayoutEntity("small-electric-pole", x, y, 0, "pole"))
                ancrage = (x, y)
                break
    _poser_poteaux(api, desserte)
    print(f"       . {len(desserte)} poteau(x) de desserte pour {len(rap2.placed)} machines")
    api.run_action(api.wait, 240, timeout=90.0)

    # --- 5 : les machines sont ALIMENTÉES par la centrale (même réseau) ---
    moteur = next((p for p in rap.placed if p.name == "steam-engine"), None)
    centrale = api.get_power_state(moteur.x, moteur.y, 3.0) if moteur else {}
    drill_p = next((p for p in rap2.placed if p.role == "drill"), None)
    four_p = next((p for p in rap2.placed if p.role == "machine"), None)
    e_drill = api.get_power_state(drill_p.x, drill_p.y, 2.0) if drill_p else {}
    e_four = api.get_power_state(four_p.x, four_p.y, 2.0) if four_p else {}
    meme = (e_drill.get("networkId") is not None
            and e_drill.get("networkId") == centrale.get("networkId")
            and e_four.get("networkId") == centrale.get("networkId"))
    rec("e5-5 : drill et four sur le réseau de la centrale", meme,
        f"centrale={centrale.get('networkId')} drill={e_drill.get('networkId')} "
        f"four={e_four.get('networkId')} statuts={e_drill.get('status')}/{e_four.get('status')}")

    # --- 6 : le drill mine, sans combustible ---
    sf = api.scan_factory()
    rows = sf.get("entities", []) if isinstance(sf, dict) else []

    def _at(name, x, y, tol=2.0):
        for e in rows:
            if e.get("name") == name and abs(float(e.get("x", 1e9)) - x) <= tol \
                    and abs(float(e.get("y", 1e9)) - y) <= tol:
                return e
        return None

    drill = _at("electric-mining-drill", drill_p.x, drill_p.y) if drill_p else None
    rec("e6-6 : le drill électrique mine du minerai",
        bool(drill) and drill.get("mining") == RESSOURCE
        and drill.get("status") in ("working", "waiting_for_space_in_destination"),
        f"mining={drill.get('mining') if drill else None} "
        f"status={drill.get('status') if drill else None} "
        f"oreUnder={drill.get('oreUnder') if drill else None}")

    # --- 7 : PREUVE — des plaques sortent, sans qu'on ait rien injecté ---
    api.run_action(api.wait, 600, timeout=120.0)
    avant = api.get_state().get("inventory", {}).get("iron-plate", 0)
    api.run_action(api.move_items_at, "iron-plate", "electric-furnace",
                   four_p.x, four_p.y, 0, False, timeout=30.0)
    apres = api.get_state().get("inventory", {}).get("iron-plate", 0)
    rec("e5-7 : des plaques sortent d'une chaîne SANS combustible", apres > avant,
        f"iron-plate {avant} -> {apres} (+{apres - avant}) — minerai jamais injecté à la main")

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