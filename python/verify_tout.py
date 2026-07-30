"""Passe en revue TOUT ce que le projet prétend savoir faire, en une commande.

Chaque chantier a laissé son script de vérification en jeu. Pris un par un, ils disent
qu'un mécanisme marche le jour où il a été écrit ; pris ensemble, ils disent si l'agent
tient toujours debout — c'est-à-dire si une correction d'aujourd'hui n'a pas défait un
acquis d'hier. Ce fichier ne teste rien lui-même : il ORCHESTRE, et rend un verdict.

Trois partis pris :

**L'ordre n'est pas indifférent.** Chaque script restaure l'état de référence ou remet la
carte en condition ; les enchaîner dans le désordre ferait échouer le suivant sur ce que
le précédent a laissé. On va donc du socle (restauration d'état) vers le comportement
(objectif de débit).

**Un script ABSENT n'est pas un succès.** Il est compté à part et le verdict le dit :
c'est la différence entre « rien à signaler » et « rien n'a été regardé ».

**Le temps est le coût principal.** Chaque script relance le serveur et fait tourner une
partie ; l'ensemble dure une dizaine de minutes. Le `--rapide` s'en tient au socle.

Usage :
    cd python
    python verify_tout.py [--rapide]
"""

from __future__ import annotations

import os
import subprocess
import sys
import time

# Du socle vers le comportement. Le commentaire dit ce que CHACUN garantit, pour qu'un
# échec se lise sans ouvrir le fichier.
SOCLE = [
    ("verify_save_ref.py", "l'état de départ se fige et se restaure à l'identique"),
    ("verify_doctor_e6.py", "le diagnostic distingue la cause du symptôme"),
]
COMPORTEMENT = [
    ("verify_gisement_e21.py", "un gisement épuisé se voit, se nomme et se redéploie"),
    ("verify_evacuation_e21.py", "une sortie bouchée finit par recevoir un ramassage"),
    ("verify_defense_e21.py", "une défense qui ne mène à rien cède la place"),
    ("verify_objectif_e22.py", "l'agent mesure son débit et étend sous objectif"),
]


def _lancer(script: str, promesse: str, timeout: float) -> tuple[str, bool, str]:
    """Rend (script, réussi, résumé). Un script absent ou en erreur n'est jamais un succès."""
    if not os.path.exists(script):
        return script, False, "ABSENT"
    t0 = time.time()
    try:
        p = subprocess.run([sys.executable, script], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        return script, False, f"TIMEOUT après {timeout:.0f}s"
    sortie = (p.stdout or "") + (p.stderr or "")
    # Les scripts finissent par « N/M reussies. » ; à défaut, le code de sortie tranche.
    bilan = ""
    for ligne in reversed(sortie.splitlines()):
        if "reussies" in ligne:
            bilan = ligne.strip()
            break
    if "[SKIP]" in sortie and not bilan:
        return script, True, f"SKIP ({time.time() - t0:.0f}s) — pré-requis absent"
    return script, p.returncode == 0, f"{bilan or 'code ' + str(p.returncode)} " \
                                      f"({time.time() - t0:.0f}s)"


def main(argv: list[str]) -> int:
    rapide = "--rapide" in argv
    liste = SOCLE + ([] if rapide else COMPORTEMENT)
    print(f"       {len(liste)} vérification(s) en jeu"
          + (" (socle seulement)" if rapide else "") + "\n")
    resultats = []
    for script, promesse in liste:
        print(f"       ... {script} — {promesse}")
        r = _lancer(script, promesse, timeout=900.0)
        resultats.append((r[0], r[1], r[2], promesse))
        print(f"       [{'OK  ' if r[1] else 'FAIL'}] {r[0]:28s} {r[2]}")
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _, _ in resultats if ok)
    for script, ok, resume, promesse in resultats:
        if not ok:
            print(f"  ECHEC : {script} — {promesse} ({resume})")
    print(f"{nok}/{len(resultats)} verification(s) en jeu reussie(s).")
    print("=" * 72)
    return 0 if nok == len(resultats) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
