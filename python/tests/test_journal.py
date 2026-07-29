"""Tests unitaires du journal persistant — sans serveur.

Ce qui est éprouvé : le journal survit à ce qui le menace. Une partie longue s'écrit
pendant des heures, et l'information qu'il porte n'a de valeur que si elle SURVIT à
l'incident qu'elle documente.

  - une écriture impossible ne doit jamais arrêter l'agent ;
  - un fichier tronqué doit rester relisible — la dernière ligne est souvent incomplète,
    et c'est justement celle qui précède l'incident ;
  - le journal se lit PENDANT qu'il s'écrit (une ligne par événement), sinon on ne peut
    pas surveiller sans interrompre.

Lancement :
    cd python
    python -m tests.test_journal
"""

from __future__ import annotations

import os
import sys
import tempfile

from services.journal import Journal, relire

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:46s} {detail[:100]}")


def _chemin() -> str:
    d = tempfile.mkdtemp(prefix="fl-journal-")
    return os.path.join(d, "partie.jsonl")


class _Decision:
    action = "ravitailler"
    raison = "boiler à sec"


def test_une_ligne_par_evenement() -> None:
    """Le journal doit être lisible PENDANT qu'il s'écrit, donc une ligne autonome."""
    p = _chemin()
    jr = Journal(p)
    jr.tour(1, 100, _Decision(), True, "ravitaillé")
    lu_avant_fin = relire(p)
    jr.tour(2, 200, _Decision(), False, "sans effet")
    lu = relire(p)
    ok = len(lu_avant_fin) == 1 and len(lu) == 2 and lu[1]["n"] == 2
    rec("test_une_ligne_par_evenement", ok,
        f"{len(lu_avant_fin)} ligne(s) lisible(s) en cours d'écriture, {len(lu)} au total")
    assert ok


def test_fichier_tronque_reste_relisible() -> None:
    """Une partie interrompue laisse une dernière ligne incomplète : elle ne doit pas tout perdre.

    C'est le cas qui compte : la ligne tronquée précède l'incident qu'on cherche.
    """
    p = _chemin()
    jr = Journal(p)
    for i in range(3):
        jr.mesure(i * 60, tour=i, machines=i)
    with open(p, "a", encoding="utf-8") as f:
        f.write('{"genre": "mesure", "tour": 3, "machi')      # coupée net
    lu = relire(p)
    ok = len(lu) == 3 and lu[-1]["tour"] == 2
    rec("test_fichier_tronque_reste_relisible", ok,
        f"{len(lu)} ligne(s) exploitable(s) malgré une 4e coupée")
    assert ok


def test_ecriture_impossible_ne_leve_pas() -> None:
    """Disque plein ou chemin interdit : on perd la trace, jamais la partie."""
    jr = Journal("/proc/interdit/partie.jsonl" if os.name != "nt"
                 else "Z:\\inexistant\\partie.jsonl")
    jr.ecrire("tour", n=1)
    jr.ecrire("tour", n=2)
    ok = jr.erreurs >= 1 and jr.lignes == 0 and "perdue" in jr.resume()
    rec("test_ecriture_impossible_ne_leve_pas", ok,
        f"{jr.erreurs} écriture(s) perdue(s), aucune exception — {jr.resume()[:50]}")
    assert ok


def test_compteurs_repondent_sans_relire() -> None:
    """« La partie s'est-elle bien passée ? » ne doit pas exiger de relire des heures."""
    p = _chemin()
    jr = Journal(p)
    jr.tour(1, 10, _Decision(), True, "ok")
    jr.tour(2, 20, _Decision(), True, "ok")
    jr.ecart(30, type("E", (), {"action": "approvisionner", "attendu": "charbon",
                                "observe": "rien"})())
    ok = jr.compteurs.get("tour") == 2 and jr.compteurs.get("ecart") == 1
    rec("test_compteurs_repondent_sans_relire", ok, jr.resume())
    assert ok


def test_filtrage_par_genre() -> None:
    """Sur des milliers de lignes, on veut la série des mesures sans le reste."""
    p = _chemin()
    jr = Journal(p)
    jr.tour(1, 10, _Decision(), True, "ok")
    for i in range(4):
        jr.mesure(i, tour=i, machines=i * 2)
    mesures = relire(p, genre="mesure")
    ok = len(mesures) == 4 and [m["machines"] for m in mesures] == [0, 2, 4, 6]
    rec("test_filtrage_par_genre", ok,
        f"{len(mesures)} mesure(s) isolée(s) sur {len(relire(p))} ligne(s)")
    assert ok


def test_horodatage_en_ticks_et_en_reel() -> None:
    """À `game.speed = 10`, le temps de jeu ne se déduit pas du temps réel."""
    p = _chemin()
    jr = Journal(p)
    jr.mesure(3600, tour=1, machines=1)
    e = relire(p)[0]
    ok = e.get("tick") == 3600 and "reel" in e
    rec("test_horodatage_en_ticks_et_en_reel", ok,
        f"tick={e.get('tick')} et reel={e.get('reel')}s enregistrés séparément")
    assert ok


def main() -> int:
    for t in (test_une_ligne_par_evenement, test_fichier_tronque_reste_relisible,
              test_ecriture_impossible_ne_leve_pas, test_compteurs_repondent_sans_relire,
              test_filtrage_par_genre, test_horodatage_en_ticks_et_en_reel):
        t()
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