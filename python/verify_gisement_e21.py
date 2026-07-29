"""Test LIVE E21b : une foreuse à sec se voit, se nomme, et se redéploie.

C'est la panne qui a plafonné la dernière partie longue, et elle était MUETTE. Mesuré :
production de 621 à 1116 plaques jusqu'au tour 106, puis strictement plate pendant 60
tours ; 2 machines en marche sur 6 ; `ecarts=0`, `constats=0` ; et l'agent décidant
« rien » 157 fois sur 164. Sous la foreuse il restait 23 unités de minerai — et 312 000
à quelques pas, dans le même gisement. L'usine mourait de faim au milieu de l'abondance.

La cause du silence n'était pas le raisonnement de l'agent mais son vocabulaire :
`no_minable_resources` ne figurait pas dans la table du FactoryDoctor, et un statut
absent de cette table était classé en gravité 0, donc effacé.

Ce qu'on vérifie :
  1. l'usine produit — sans quoi la suite ne prouverait rien ;
  2. le minerai retiré sous l'emprise, le diagnostic NOMME `gisement_epuise` (il rendait
     zéro cause) et le porte en cause RACINE, pas en conséquence ;
  3. le four affamé en aval est déclassé en conséquence : c'est la foreuse qu'il faut
     traiter, pas la machine à jeun ;
  4. l'agent décide `redeployer_foreur` et AGIT ;
  5. une foreuse extrait à nouveau — le critère est l'extraction, pas la pose ;
  6. la production repart, mesurée par la statistique du jeu et non par un inventaire.

On provoque la panne comme le jeu la produit — en retirant le minerai — plutôt qu'en
forçant un statut : un statut écrit à la main ne prouverait rien du comportement réel.

Pré-requis : serveur headless + référence figée (`preparer_reference.py`).

Lancement :
    cd python
    python verify_gisement_e21.py
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator, enumerer_options
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import save_ref
from services.factory_doctor import diagnose_zone

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:105]}")


def _produites(rcon, item: str = "iron-plate") -> int:
    try:
        return int(str(rcon.query_lua(
            "local s = game.forces.player.get_item_production_statistics(game.surfaces[1]) "
            f"rcon.print(s.get_input_count('{item}'))")).strip())
    except (ValueError, TypeError, AttributeError):
        return -1


def _assecher(rcon, x: float, y: float, r: float = 3.0) -> int:
    """Retire le minerai sous l'emprise de la foreuse. Rend le nombre de tuiles ôtées.

    On ne touche qu'à l'emprise : le reste du gisement doit RESTER, sinon on testerait
    « il n'y a plus de fer nulle part », qui est un autre problème et n'a pas de
    réparation locale.
    """
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{type='resource', "
        f"area={{{{{x - r},{y - r}}},{{{x + r},{y + r}}}}}}}) do e.destroy() n = n + 1 end "
        f"rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return 0


def _minerai_autour(rcon, x: float, y: float, r: float = 25.0) -> int:
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local t = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{type='resource', "
        f"area={{{{{x - r},{y - r}}},{{{x + r},{y + r}}}}}}}) do t = t + e.amount end "
        f"rcon.print(t)")
    try:
        return int(str(out).strip())
    except ValueError:
        return 0


def main() -> int:
    ok_ref, motif = save_ref.restaurer_reference()
    if not ok_ref:
        print(f"[SKIP] pas d'état de référence ({motif}).")
        print("       le figer d'abord : python preparer_reference.py")
        return 0
    print(f"       {motif}")

    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    api.setup()
    pos = (api.get_state().get("character") or {}).get("position") or {}
    zone = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))
    coord = Coordinator(api, zone=zone, rayon=25.0)

    # --- l'agent bâtit son usine (il retente, comme la vraie boucle) ---
    drill, tours = None, 0
    while drill is None and tours < 8:
        tours += 1
        d, agi, _ = coord.tick()
        api.run_action(api.wait, 240, timeout=90.0)
        ins = api.inspect_at(zone[0], zone[1], 25.0)
        lignes = ins.get("entities", []) if isinstance(ins, dict) else []
        drill = next((e for e in lignes if "mining-drill" in str(e.get("name"))), None)
        print(f"       tour {tours} : {d.action} agi={agi} — "
              f"foreuse {'trouvée' if drill else 'pas encore'}")
    if drill is None:
        print(f"[SKIP] aucune foreuse bâtie en {tours} tours (E8 suppose la construction).")
        rcon.close()
        return 0
    dx, dy, dn = float(drill["x"]), float(drill["y"]), str(drill["name"])

    # --- 1 : l'usine produit ---
    api.run_action(api.wait, 600, timeout=180.0)
    avant = _produites(rcon)
    api.run_action(api.wait, 600, timeout=180.0)
    pendant = _produites(rcon)
    rec("e21b-1 : l'usine produit avant qu'on ne casse quoi que ce soit",
        pendant > avant, f"{dn}@({dx},{dy}) — production {avant} -> {pendant}")

    # --- 2 : le gisement est asséché SOUS l'emprise, pas ailleurs ---
    otees = _assecher(rcon, dx, dy)
    reste = _minerai_autour(rcon, dx, dy)
    api.run_action(api.wait, 120, timeout=60.0)
    diag = diagnose_zone(api, zone[0], zone[1], 25.0)
    epuise = next((s for s in diag.causes if s.cause == "gisement_epuise"), None)
    rec("e21b-2 : le diagnostic NOMME le gisement épuisé", epuise is not None,
        f"{otees} tuile(s) ôtées, {reste} unités restent autour — "
        f"{len(diag.causes)} cause(s) : {[s.cause for s in diag.causes]}")
    if epuise is None:
        return _verdict(rcon)
    rec("e21b-2b : c'est une cause RACINE, portée par la foreuse",
        epuise.racine and "drill" in epuise.name,
        f"{epuise}")

    # --- 3 : le four affamé est une conséquence, pas la cause à traiter ---
    fours = [s for s in diag.symptomes if "furnace" in s.name]
    rec("e21b-3 : la machine affamée en aval est déclassée en conséquence",
        all(not s.racine for s in fours) if fours else True,
        f"{[(s.name, s.cause, 'racine' if s.racine else 'conséquence') for s in fours]}"
        or "aucun four symptomatique")

    # --- 4 : l'agent décide de redéployer, et agit ---
    etat = coord.observer()
    options = enumerer_options(etat)
    choix = options[0] if options else None
    rec("e21b-4 : il décide de REDÉPLOYER la foreuse",
        choix is not None and choix.action == "redeployer_foreur",
        f"{choix.action if choix else '—'} parmi {[o.action for o in options]}")

    d, agi, _ = coord.tick()
    rec("e21b-4b : l'action est menée", d.action == "redeployer_foreur" and agi,
        f"{d.action} agi={agi} — {coord.journal[-1][-95:] if coord.journal else ''}")

    # --- 5 : une foreuse EXTRAIT à nouveau ---
    api.run_action(api.wait, 600, timeout=180.0)
    ins = api.inspect_at(zone[0], zone[1], 25.0)
    lignes = ins.get("entities", []) if isinstance(ins, dict) else []
    drills = [e for e in lignes if "mining-drill" in str(e.get("name"))]
    statuts = [str(e.get("status")) for e in drills]
    rec("e21b-5 : une foreuse extrait à nouveau",
        any(s in ("working", "normal") for s in statuts),
        f"{len(drills)} foreuse(s) : {statuts}")

    # --- 6 : et la production repart ---
    p1 = _produites(rcon)
    api.run_action(api.wait, 900, timeout=240.0)
    p2 = _produites(rcon)
    rec("e21b-6 : la production repart", p2 > p1,
        f"production {p1} -> {p2} après le redéploiement")

    print("\n       --- journal du Coordinator ---")
    for j in coord.journal[-5:]:
        print(f"       . {j[:115]}")
    return _verdict(rcon)


def _verdict(rcon) -> int:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    try:
        rcon.close()
    except Exception:
        pass
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
