"""Test LIVE E22 : l'agent VISE un debit, et cesse de se satisfaire de « ca tourne ».

Jusqu'ici le curriculum s'arretait a « l'usine tourne » : mesure en partie longue, `rien`
occupait 100 tours sur 114 pendant que la production plafonnait, et l'agent n'avait aucune
raison de faire mieux. Un agent qui maintient n'a jamais de dilemme -- ses choix se
tranchent par la priorite -- ce qui explique aussi que l'arbitre LLM soit d'accord avec le
determinisme 14 fois sur 14 : il n'y avait rien a arbitrer.

Ce qu'on verifie :
  1. l'agent MESURE son debit reel (statistique du jeu, pas son inventaire) ;
  2. une premiere observation ne rend AUCUN debit -- un debit est une difference, et
     conclure sur une mesure absente est precisement ce qu'il ne doit pas faire ;
  3. sous l'objectif, il decide `etendre_production` la ou il disait `rien` ;
  4. l'action agit : l'usine compte plus de machines qu'avant ;
  5. le debit mesure AUGMENTE apres l'extension ;
  6. objectif tenu -> il s'arrete. Agrandir sans fin n'est pas un but ;
  7. sans objectif, le comportement est inchange -- back-compat stricte.

Pre-requis : serveur headless + reference figee (`preparer_reference.py`).

Lancement :
    cd python
    python verify_objectif_e22.py
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator, decide
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import perception, save_ref

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:105]}")


def main() -> int:
    ok_ref, motif = save_ref.restaurer_reference()
    if not ok_ref:
        print(f"[SKIP] pas d'etat de reference ({motif}).")
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

    # La reference peut porter des nids (elle sert aussi aux tests de defense). Ici le
    # sujet est le debit : laisses en place, ils consomment les premiers tours en
    # `defendre` et l'usine n'est jamais batie -- mesure, six tours y sont passes et le
    # test concluait « pas de debit » alors qu'il n'y avait pas d'usine. On ecarte donc la
    # menace explicitement, plutot que d'esperer qu'elle se taise.
    otes = rcon.query_lua(
        "local s = game.surfaces[1] local n = 0 "
        "for _, e in pairs(s.find_entities_filtered{force='enemy'}) do "
        "e.destroy() n = n + 1 end rcon.print(n)")
    print(f"       {str(otes).strip()} entite(s) ennemie(s) ecartee(s) : ce test porte "
          f"sur le debit, pas sur la defense")

    # Un electric-mining-drill sort 0.5 minerai/s : une seule chaine ne peut pas tenir
    # 1.5 plaque/s, l'objectif est donc hors d'atteinte SANS extension. C'est ce qui rend
    # le test concluant plutot que complaisant.
    coord = Coordinator(api, zone=zone, rayon=25.0, objectif_par_s=1.5)

    # Le jeu est accelere pour que les fenetres de mesure passent vite ; remis a x1 a la
    # fin, car game.speed est enregistre DANS la save.
    rcon.query_lua("game.speed = 10 rcon.print('ok')")
    try:
        # --- 2 : la premiere observation ne rend aucun debit ---
        etat0 = coord.observer()
        rec("e22-2 : une seule observation ne rend aucun debit", etat0.debit is None,
            f"debit={etat0.debit} (il faut deux lectures et l'ecart de ticks)")

        # --- l'agent batit son usine, et on attend qu'elle PRODUISE ---
        # Le critere n'est pas « trois machines posees » : mesure, une chaine complete
        # avec un inserter debranche donne trois machines et zero plaque, et l'on mesurait
        # alors un debit nul en croyant mesurer une usine. On laisse donc la boucle
        # reparer ce qu'elle a bati jusqu'a ce que le compteur du jeu bouge.
        depart = perception.production_cumulee(api, "iron-plate")
        produit, machines = False, 0
        for tour in range(1, 15):
            d, agi, _ = coord.tick()
            api.run_action(api.wait, 300, timeout=120.0)
            machines = coord.observer().machines
            if perception.production_cumulee(api, "iron-plate") > depart:
                produit = True
                print(f"       usine productive apres {tour} tour(s) "
                      f"({machines} machines, derniere action {d.action})")
                break
        if not produit:
            print(f"[SKIP] l'usine ne produit pas apres 14 tours ({machines} machine(s)) "
                  f"— ce test suppose la chaine acquise (E19b), il ne l'eprouve pas.")
            print(f"       journal : {coord.journal[-1][:110] if coord.journal else ''}")
            return _verdict(rcon)

        # Laisser tourner : un debit se mesure sur une fenetre, pas sur un instant.
        api.run_action(api.wait, 900, timeout=180.0)
        coord.observer()
        api.run_action(api.wait, 900, timeout=180.0)
        etat = coord.observer()

        # --- 1 : le debit est mesure ---
        cumul = perception.production_cumulee(api, "iron-plate")
        rec("e22-1 : l'agent mesure son debit reel", etat.debit is not None and etat.debit > 0,
            f"debit={etat.debit} iron-plate/s | production cumulee={cumul}")
        if etat.debit is None:
            return _verdict(rcon)

        # --- 3 : sous l'objectif, il etend ---
        d = decide(etat)
        rec("e22-3 : sous l'objectif, il decide d'ETENDRE", d.action == "etendre_production",
            f"{d.action} — {d.raison[:70]}")

        # --- 4 : et l'usine grandit ---
        avant = etat.machines
        d2, agi2, apres_etat = coord.tick()
        rec("e22-4 : l'extension est menee et l'usine grandit",
            d2.action == "etendre_production" and agi2 and apres_etat.machines > avant,
            f"{d2.action} agi={agi2} — machines {avant} -> {apres_etat.machines}")

        # --- 5 : le debit augmente ---
        avant_debit = etat.debit
        api.run_action(api.wait, 900, timeout=180.0)
        coord.observer()
        api.run_action(api.wait, 900, timeout=180.0)
        apres_debit = coord.observer().debit
        rec("e22-5 : le debit mesure augmente apres l'extension",
            apres_debit is not None and avant_debit is not None
            and apres_debit > avant_debit,
            f"debit {avant_debit} -> {apres_debit} iron-plate/s")

        # --- 6 : objectif tenu -> il s'arrete ---
        coord.objectif_par_s = 0.05          # largement tenu par ce qui tourne deja
        etat6 = coord.observer()
        d6 = decide(etat6)
        # `machines > 0` fait partie du critere : sur une carte vide, « il n'etend pas »
        # serait vrai sans rien prouver.
        rec("e22-6 : objectif tenu, il s'arrete",
            d6.action != "etendre_production" and etat6.machines > 0,
            f"{d6.action} (debit {etat6.debit} pour 0.05 demandes, "
            f"{etat6.machines} machines)")

        # --- 7 : sans objectif, rien ne change ---
        coord.objectif_par_s = None
        etat7 = coord.observer()
        d7 = decide(etat7)
        rec("e22-7 : sans objectif, le comportement est inchange",
            d7.action != "etendre_production" and etat7.objectif is None,
            f"{d7.action}, objectif={etat7.objectif}")
    finally:
        rcon.query_lua("game.speed = 1 rcon.print('ok')")

    print("\n       --- journal du Coordinator ---")
    for j in coord.journal[-6:]:
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
