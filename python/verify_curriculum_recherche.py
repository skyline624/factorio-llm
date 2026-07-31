"""Vérification en jeu : l'agent DÉCIDE de chercher, et enchaîne les technologies.

Depuis E24, l'agent savait tout faire : lire l'arbre, franchir un déclencheur, poser un
laboratoire, fabriquer ses flacons, lancer une recherche payante. Mais `chercher`
n'était dans AUCUN curriculum — rien ne le lui proposait jamais, et ces capacités
dormaient. Il fallait les appeler à la main depuis un banc.

Une capacité qu'aucune décision n'appelle n'existe pas.

Ce qui est éprouvé ici, sur une partie que l'agent mène seul :

  1. la boucle PROPOSE de chercher quand une marche est à portée ;
  2. elle ne le propose pas avant d'avoir une usine — chercher au lieu de bâtir sa
     première chaîne serait un raffinement avant l'essentiel ;
  3. `chercher` vaut autant qu'`etendre_production`, ce qui crée le premier vrai dilemme
     du projet : grandir en largeur ou en profondeur ;
  4. l'agent acquiert RÉELLEMENT des technologies, plusieurs, à la suite.

Le combustible de la centrale est renouvelé pendant la partie : ce banc éprouve la
DÉCISION de chercher, pas l'endurance d'une chaudière — celle-ci a son propre sujet.

Lancement (serveur Factorio + mod requis) :
    cd python
    python verify_curriculum_recherche.py
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator, PRIORITE, enumerer_options
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import deplacement, recherche, save_ref

TOURS = 8
RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:54s} {detail[:115]}", flush=True)
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
    coord = Coordinator(api, zone=deplacement.position(api), rayon=25.0)

    # --- Rec 1 : sans usine, on ne cherche pas ---
    # Symétrique du piège d'`etendre_production` : une technologie ne nourrit personne
    # tant qu'il n'y a pas de quoi la payer.
    etat0 = coord.observer()
    options0 = [o.action for o in enumerer_options(etat0)]
    rec("1: sans usine, chercher n'est pas proposé",
        "chercher" not in options0,
        f"{etat0.machines} machine(s) -> options {options0}")

    depart = set(recherche.lire(api).acquises)
    journal, marches_vues = [], set()
    for tour in range(TOURS):
        # La centrale est renourrie : ce banc éprouve la décision, pas l'endurance d'une
        # chaudière. Un boiler à sec arrêterait tout et l'on mesurerait autre chose.
        rcon.query_lua(
            "local s = game.surfaces[1] "
            "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
            "  local f = e.get_fuel_inventory() "
            "  if f and f.get_item_count() < 50 then f.insert{name='coal', count=200} end end "
            "rcon.print('ok')")
        d, agi, etat = coord.tick()
        journal.append((tour + 1, d.action, agi, etat.marche))
        if etat.marche:
            marches_vues.add(etat.marche)
        print(f"  t{tour + 1:<2} {d.action:20s} agi={agi} | marche={etat.marche}",
              flush=True)

    arrivee = set(recherche.lire(api).acquises)
    gagnees = arrivee - depart

    # --- Rec 2 : la boucle a PROPOSÉ de chercher ---
    proposes = [j for j in journal if j[1] == "chercher"]
    rec("2: la boucle décide de chercher d'elle-même", bool(proposes),
        f"{len(proposes)} tour(s) sur {TOURS} : {[j[0] for j in proposes]}")

    # --- Rec 3 : chercher et étendre ont le même rang ---
    # Un rang supérieur ferait chercher sans fin ; un rang inférieur ne ferait jamais
    # chercher. À égalité, c'est l'arbitre qui tranche.
    rec("3: chercher pèse autant qu'étendre — un vrai dilemme",
        PRIORITE.get("chercher") == PRIORITE.get("etendre_production"),
        f"chercher={PRIORITE.get('chercher')}, "
        f"etendre_production={PRIORITE.get('etendre_production')}")

    # --- Rec 4 : des technologies sont RÉELLEMENT acquises ---
    rec("4: l'agent acquiert des technologies, seul", len(gagnees) >= 1,
        f"{len(gagnees)} acquise(s) pendant la partie : {', '.join(sorted(gagnees)) or 'aucune'} "
        f"(départ {len(depart)} -> arrivée {len(arrivee)})")

    # --- Rec 5 : il ENCHAÎNE — la marche visée avance ---
    # Une seule technologie prouverait un coup de chance ; ce qui compte est que l'agent
    # passe à la suivante sans qu'on le lui dise.
    rec("5: il enchaîne — la marche visée change en cours de partie",
        len(marches_vues) >= 2,
        f"{len(marches_vues)} marche(s) successivement visée(s) : "
        f"{', '.join(sorted(marches_vues))}")

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
