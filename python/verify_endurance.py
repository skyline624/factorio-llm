"""Vérification en jeu : l'usine ne s'éteint pas quand le combustible manque.

C'est ce qui sépare une usine qui démarre d'une usine qui TIENT. Un boiler brûle son
charbon en moins de deux minutes ; sans réapprovisionnement, tout s'arrête — bras,
fours, assembleuses, laboratoire — et l'agent regarde une carte morte. Deux de nos
bancs entretenaient d'ailleurs la centrale à la main, faute de mieux.

Ce qui est éprouvé ici, sur la panne la plus banale et la plus fatale du jeu :

  1. le diagnostic nomme la BONNE cause — une chaudière à sec, et non les dix machines
     sans courant qui n'en sont que la conséquence ;
  2. l'agent va CHERCHER du charbon quand il n'en a plus, au lieu d'attendre ;
  3. il le porte à la chaudière, et le réseau REPART ;
  4. il recommence — c'est un cycle, pas un dépannage.

La panne est montée sans ménagement : chaudières vidées ET pas un charbon en poche.
C'est l'état où toutes les issues faciles sont fermées.

Lancement (serveur Factorio + mod requis) :
    cd python
    python verify_endurance.py
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import deplacement, save_ref

TOURS = 6
RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:115]}", flush=True)
    if not ok and len(detail) > 115:
        for suite in range(115, len(detail), 115):
            print(f"       {detail[suite:suite + 115]}", flush=True)


def main() -> int:
    ok, motif = save_ref.restaurer_reference()
    if not ok:
        print(f"[SKIP] pas d'état de référence ({motif}).")
        return 0
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    rcon.query_lua("game.speed = 30 rcon.print(1)")
    # `pause_reflexion=True` a été ESSAYÉ ICI, puis retiré : mesuré sur trois passages,
    # il ne change pas le résultat (2/4, 3/4, 3/4). Ce n'est donc pas la latence de la
    # décision qui tue la centrale — l'agent perd ses tours autrement, cf. plus bas.
    coord = Coordinator(api, zone=deplacement.position(api), rayon=25.0)

    # --- La scène : une usine qui tourne, puis la panne sèche ---
    for _ in range(2):
        coord.tick()
    boiler = str(rcon.query_lua(
        "local s = game.surfaces[1] "
        "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
        "  rcon.print(e.position.x .. ',' .. e.position.y) return end "
        "rcon.print('')")).strip().split("\n")[0]
    if "," not in boiler:
        print("[SKIP] aucune chaudière bâtie — ce banc suppose la centrale acquise (E5).")
        rcon.close()
        return 0
    bx, by = (float(v) for v in boiler.split(","))

    # Chaudières vidées ET poches vides : toutes les issues faciles sont fermées.
    rcon.query_lua(
        "local s = game.surfaces[1] "
        "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
        "  local f = e.get_fuel_inventory() if f then f.clear() end end "
        "local c for _, e in pairs(s.find_entities_filtered{name='character'}) do c = e end "
        "if c then c.get_inventory(defines.inventory.character_main)"
        ".remove{name='coal', count=100000} end rcon.print('a sec')")

    def _charbon() -> int:
        return int(dict(api.get_state().get("inventory") or {}).get("coal", 0))

    def _kw() -> float:
        return float((api.get_power_state(bx, by, 4.0) or {}).get("productionKW") or 0)

    # LE JEU DOIT PRENDRE ACTE. Observé dans la foulée du vidage, le boiler n'a pas
    # encore le statut `no_fuel` — le diagnostic rend alors « aucune cause » sur une
    # centrale qu'on vient d'assécher, et l'on croirait le diagnostic aveugle.
    api.run_action(api.wait, 180, timeout=90.0)
    print(f"       panne montée : chaudière@({bx:.0f},{by:.0f}), "
          f"{_charbon()} charbon en poche, réseau {_kw():.0f} kW", flush=True)

    # --- Rec 1 : le diagnostic nomme la chaudière, pas ses victimes ---
    # Sans ce déclassement, il rendait quatre `renforcer_energie` de gravité 3 qui
    # masquaient le ravitaillement : l'agent renforçait sans fin une centrale qui
    # n'attendait qu'un seau de charbon.
    etat = coord.observer()
    racines = [(s.name, s.cause) for s in (etat.diagnostic.causes if etat.diagnostic else [])]
    rec("1: le diagnostic nomme la chaudière à sec, pas ses victimes",
        any(n == "boiler" and c == "sans_combustible" for n, c in racines)
        and not any(c in ("sans_courant", "courant_insuffisant") for _, c in racines),
        f"causes racines : {racines}")

    # --- La boucle vit sa vie ---
    journal, pic_charbon, pic_kw = [], 0, 0.0
    for tour in range(TOURS):
        d, agi, _ = coord.tick()
        ch, kw = _charbon(), _kw()
        pic_charbon, pic_kw = max(pic_charbon, ch), max(pic_kw, kw)
        journal.append((tour + 1, d.action, agi, ch, kw))
        print(f"  t{tour + 1:<2} {d.action:20s} agi={agi} | charbon={ch} réseau={kw:.0f} kW",
              flush=True)

    # --- Rec 2 : il est allé CHERCHER du charbon ---
    # Il sait miner depuis E23 — encore fallait-il que quelque chose le lui propose.
    rec("2: il va chercher du charbon quand il n'en a plus", pic_charbon >= 20,
        f"parti de 0, il en a rapporté jusqu'à {pic_charbon} "
        f"({sum(1 for j in journal if j[1] == 'fabriquer' and j[2])} tour(s) de fabrication)")

    # --- Rec 3 : il l'a porté à la chaudière, et le réseau REPART ---
    ravitaille = [j for j in journal if j[1] == "ravitailler" and j[2]]
    rec("3: il ravitaille la chaudière et le réseau repart",
        bool(ravitaille) and pic_kw > 0,
        f"{len(ravitaille)} ravitaillement(s) réussi(s), production remontée à "
        f"{pic_kw:.0f} kW après être tombée à zéro")

    # --- Rec 4 : c'est un CYCLE, pas un dépannage ---
    # Une seule reprise prouverait un coup de chance ; ce qui fait tenir une usine est
    # que l'agent recommence de lui-même.
    # COMPTER LES RÉPARATIONS RÉCOMPENSERAIT LA FRAGILITÉ. Un premier critère exigeait
    # deux cycles en six tours ; il a échoué le jour où l'agent a fait MIEUX — un seul
    # ravitaillement, assez généreux pour tenir jusqu'au bout. Ce qu'il faut mesurer
    # n'est pas le nombre de dépannages, c'est que l'usine soit ENCORE VIVANTE à la fin.
    # ON LAISSE L'AGENT AUX COMMANDES. Attendre passivement neuf cents ticks mesurait une
    # usine ABANDONNÉE — et elle retombait à zéro, ce qui ne prouve rien : aucune usine
    # au charbon ne survit sans personne. Ce qu'on veut savoir est si l'agent la MAINTIENT
    # en vie, donc on continue de jouer.
    for _ in range(3):
        coord.tick()
    kw_final, charbon_final = _kw(), _charbon()
    tient = kw_final > 0
    rec("4: l'usine est encore vivante à la fin", cycles_ok := (tient and bool(ravitaille)),
        f"après {TOURS} tours puis 3 de plus, agent aux commandes : réseau à {kw_final:.0f} kW, "
        f"{charbon_final} charbon en poche — "
        f"{sum(1 for j in journal if j[1] == 'fabriquer' and j[2])} minage(s), "
        f"{len(ravitaille)} ravitaillement(s)")
    del cycles_ok

    rcon.query_lua("game.speed = 1 rcon.print(1)")
    rcon.close()

    print("\n" + "=" * 74)
    nok = sum(1 for _, o, _ in RESULTS if o)
    for name, o, detail in RESULTS:
        if not o:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 74)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
