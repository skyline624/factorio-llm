"""Tests de la marche par bonds — sans serveur.

Ce module porte peu de calcul et beaucoup de conséquences : c'est lui qui occupe le temps
d'une partie quand un gisement est loin. Un chantier vers du fer à cent tuiles passe
l'essentiel de sa vie ici, pas dans la pose ni dans la forge.

Lancement :
    cd python
    python -m tests.test_deplacement
"""

from __future__ import annotations

import sys

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:100]}")


def test_la_marche_s_arrete_quand_on_le_demande() -> None:
    """SIXIÈME FOIS QUE LE MÊME DÉFAUT REVIENT — le point d'arrêt n'est pas sur le chemin.

    Partie 28, le fer est à 109 tuiles. Le joueur écrit « alors arrête le », l'agent appelle
    `arreter_le_chantier` dans les dix secondes, et deux minutes plus tard le chantier
    tourne toujours. H42 avait posé le point de sortie dans la boucle de POSE, H50 dans la
    boucle de FORGE — mais quand le gisement est loin, le temps se passe à MARCHER, et la
    marche ne croise rien.

    La liste s'allonge : H10 sous un drapeau mis à False par l'appelant, H23 dans une
    méthode que `batir_chaine` ne traverse pas, H27 dans une branche jamais prise, H49 sur
    les chantiers mais pas les actions directes, H50 sur la pose mais pas la forge, et ici
    sur la pose et la forge mais pas la marche. Chaque fois un correctif JUSTE, posé là où
    l'exécution ne passe pas — et cru bon jusqu'à ce qu'on regarde une partie.

    On sort ENTRE deux bonds, jamais au milieu. Le personnage reste où il est arrivé, ce
    qui est un état parfaitement valide : c'est ce que fait un joueur qui s'arrête en
    chemin. Et sans interrupteur, la marche se comporte exactement comme avant.
    """
    from services import deplacement

    bonds: list[tuple[float, float]] = []
    stop = {"oui": False}

    class _ApiMarche:
        def get_state(self):
            # `character.POSITION.x`, pas `character.x` : un double approximatif rend la
            # position (0,0) à chaque relevé, le code croit n'avoir pas bougé et sort au
            # premier bond — on mesurerait ce blocage-là plutôt que l'interruption.
            return {"character": {"position": {"x": float(len(bonds) * 10), "y": 0.0}},
                    "tick": 1}

        def generate_terrain(self, *a, **kw):
            return {"ok": True}

        def walk_to(self, x, y, **kw):
            bonds.append((x, y))
            if len(bonds) == 3:
                stop["oui"] = True          # le joueur demande l'arrêt au 3e bond
            return {"ok": True}

        def run_action(self, fn, *a, **kw):
            return fn(*a, **kw)

    deplacement.marcher_vers(_ApiMarche(), 400.0, 0.0,
                             interrompu_par=lambda: stop["oui"])
    avec = len(bonds)

    bonds.clear()
    stop["oui"] = False
    deplacement.marcher_vers(_ApiMarche(), 400.0, 0.0)
    sans = len(bonds)

    sorti_tot = avec < sans
    a_marche = avec >= 3

    ok = sorti_tot and a_marche
    rec("test_la_marche_s_arrete_quand_on_le_demande", ok,
        f"{avec} bond(s) avec interrupteur, {sans} sans")
    assert ok


def main() -> int:
    for t in (test_la_marche_s_arrete_quand_on_le_demande,):
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
