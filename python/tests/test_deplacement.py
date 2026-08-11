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


def test_l_arret_mord_aussi_entre_deux_etapes_de_fabrication() -> None:
    """SEPTIÈME ENDROIT — cette fois c'est le MINAGE qui ne s'arrête pas.

    Partie 29, mesuré pendant que le joueur regardait :

        arrêt demandé   17:45:27
        2 min 19 plus tard, le chantier tourne toujours
        position t0 (66,42) — t+6 s (66,42)        immobile
        iron-ore   t0 8     — t+6 s 11             +3 en six secondes
        machines posées : 0

    Il ne posait pas (H42), ne forgeait pas entre deux pièces (H50), ne marchait pas
    (H52) : il MINAIT. `batir_une_chaine` se fournit en exécutant un plan d'étapes —
    `find_nearest`, `walk_to_entity`, `mine_entity`, `place_furnace`… — et cette boucle-là
    ne croisait aucun point de sortie.

    Le joueur l'a vu avant nous : « pourtant il continue de miner a la main ».

    On sort donc ENTRE deux étapes. Une étape déjà commencée va au bout — un `mine_entity`
    interrompu au milieu laisserait un compte partiel qu'aucun état ne décrit — mais la
    suivante ne démarre pas, et l'appelant sait où l'on s'est arrêté.
    """
    from agents.base import BaseAgent
    from services.knowledge import Step

    faits = []
    stop = {"oui": False}

    class _Agent(BaseAgent):
        def __init__(self):
            self.interrompu_par = lambda: stop["oui"]
        def _execute(self, step):
            faits.append(step.kind)
            if len(faits) == 2:
                stop["oui"] = True          # le joueur demande l'arrêt à la 2e étape
            return {"ok": True}

    steps = [Step("find_nearest", {}), Step("walk_to_entity", {}),
             Step("mine_entity", {}), Step("place_furnace", {}), Step("craft_item", {})]
    res = _Agent().act(steps)

    sorti_tot = len(faits) < len(steps)
    a_fait_avant = len(faits) >= 2
    le_dit = any(isinstance(r, dict) and "interrompu" in str(r).lower() for r in res)

    ok = sorti_tot and a_fait_avant and le_dit
    rec("test_l_arret_mord_aussi_entre_deux_etapes_de_fabrication", ok,
        f"{len(faits)}/{len(steps)} étape(s) faites — dit={le_dit}")
    assert ok


def test_on_peut_marcher_plus_pres_que_la_tolerance_par_defaut() -> None:
    """HUIT TUILES DE TOLÉRANCE, CINQ TUILES DE PORTÉE : on n'arrive jamais.

    Partie 37, mesuré pendant que le joueur regardait :

        joueur en (-61,12)   foreuse en (-65,9)   distance = 5,5 tuiles
        demonter -> « cible hors portee / introuvable », cinq fois de suite

    Les deux nombres ne se parlaient pas. `marcher_vers` se déclare arrivée à `TOLERANCE`
    = 8 tuiles — assez fin pour aller quelque part, trop grossier pour TOUCHER quelque
    chose. Or le mod ne cherche la cible d'un minage que dans `MINING_REACH` = 5. Entre
    cinq et huit, la marche réussit et le geste échoue, toujours, sans que le motif le
    dise : « hors de portée » laisse croire à un problème de distance alors qu'on ne s'est
    simplement pas assez approché.

    La tolérance devient donc un paramètre : aller VERS un endroit et venir TOUCHER une
    machine ne demandent pas la même précision, et seul l'appelant sait ce qu'il fera en
    arrivant.
    """
    from services import deplacement

    bonds: list[tuple[float, float]] = []

    class _ApiPrecise:
        def get_state(self):
            # On avance d'une tuile par bond : la boucle doit continuer tant que la
            # tolérance demandée n'est pas atteinte.
            return {"character": {"position": {"x": float(len(bonds)), "y": 0.0}},
                    "tick": 1}
        def generate_terrain(self, *a, **kw):
            return {"ok": True}
        def walk_to(self, x, y, **kw):
            bonds.append((x, y))
            return {"ok": True}
        def run_action(self, fn, *a, **kw):
            return fn(*a, **kw)

    deplacement.marcher_vers(_ApiPrecise(), 10.0, 0.0)
    large = len(bonds)

    bonds.clear()
    deplacement.marcher_vers(_ApiPrecise(), 10.0, 0.0, tolerance=3.0)
    serre = len(bonds)

    ok = serre > large
    rec("test_on_peut_marcher_plus_pres_que_la_tolerance_par_defaut", ok,
        f"{large} bond(s) a tolerance 8, {serre} a tolerance 3")
    assert ok


def main() -> int:
    for t in (test_la_marche_s_arrete_quand_on_le_demande,
              test_l_arret_mord_aussi_entre_deux_etapes_de_fabrication,
              test_on_peut_marcher_plus_pres_que_la_tolerance_par_defaut):
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
