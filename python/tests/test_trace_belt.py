"""Tests unitaires : une belt ne réutilise pas les tuiles d'un autre flux.

Le défaut que ces tests figent était invisible entité par entité. Chaque élément avait
l'air juste — le bras avait du courant, la belt était orientée, la source produisait —
et pourtant vingt-trois engrenages ont été fabriqués sans qu'un seul n'arrive.

La cause : `place_belt_line` RETOURNE les belts déjà présentes sur son tracé, pour les
aligner sur elle. C'est le bon geste quand on prolonge sa propre ligne (sans quoi la
dernière tuile de l'ancien tronçon envoie le flux dans le vide). C'est désastreux quand
on croise la ligne d'un voisin : la sortie des engrenages a croisé la belt qui amenait
le fer, l'a retournée, et les pièces sont reparties vers l'assembleuse dont elles
venaient. Une boucle fermée ne se voit sur aucune entité prise isolément — seul le tracé
complet la révèle.

`tracer_en_l` est donc une fonction PURE, éprouvable sans serveur : c'est là que la
décision se prend.

Lancement :
    cd python
    python -m tests.test_trace_belt
"""

from __future__ import annotations

import sys

from services.site_finder import tracer_en_l

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:100]}")


def test_le_trace_relie_le_depart_a_l_arrivee() -> None:
    """Le cas nominal : un L, chaque tuile adjacente à la suivante."""
    tuiles, propre = tracer_en_l((0.5, 0.5), (4.5, 3.5))
    contigu = all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1.0
                  for a, b in zip(tuiles, tuiles[1:]))
    ok = propre and tuiles[0] == (0.5, 0.5) and contigu and len(tuiles) == 7
    rec("test_le_trace_relie_le_depart_a_l_arrivee", ok,
        f"{len(tuiles)} tuile(s), contiguës={contigu}, propre={propre}")
    assert ok


def test_le_trace_contourne_la_ligne_d_un_autre_flux() -> None:
    """Une tuile occupée par un autre flux n'est jamais empruntée.

    C'est le cœur du défaut : la belt de sortie des engrenages passait sur celle qui
    amenait le fer, la retournait, et refermait la boucle.
    """
    barrage = {(2.5, 0.5), (2.5, 1.5), (2.5, 2.5), (2.5, 3.5)}
    tuiles, propre = tracer_en_l((0.5, 0.5), (4.5, 3.5), eviter=barrage)
    ok = propre and tuiles and not any(t in barrage for t in tuiles)
    rec("test_le_trace_contourne_la_ligne_d_un_autre_flux", ok,
        f"{len(tuiles)} tuile(s), aucune sur les {len(barrage)} interdites : {propre}")
    assert ok


def test_le_trace_bascule_quand_le_premier_L_est_barre() -> None:
    """Deux formes de L sont essayées avant de décaler le coude.

    Barrer la branche horizontale du premier L doit faire prendre l'autre — sans
    contournement compliqué là où un simple changement d'ordre suffit.
    """
    direct, _ = tracer_en_l((0.5, 0.5), (3.5, 3.5))
    barrage = {t for t in direct if t[1] == 0.5 and t[0] > 0.5}
    tuiles, propre = tracer_en_l((0.5, 0.5), (3.5, 3.5), eviter=barrage)
    ok = propre and tuiles and not any(t in barrage for t in tuiles)
    rec("test_le_trace_bascule_quand_le_premier_L_est_barre", ok,
        f"branche horizontale barrée ({len(barrage)} tuiles) -> {len(tuiles)} tuile(s), "
        f"propre={propre}")
    assert ok


def test_un_trace_impossible_se_declare() -> None:
    """Encerclée, la fonction le DIT au lieu de rendre un chemin qui casse un flux.

    `propre=False` laisse l'appelant refuser de poser : mieux vaut ne rien construire
    que détourner une ligne qui marchait — c'est exactement ce qui est arrivé en jeu.
    """
    # Tout le voisinage du départ est pris : aucun L ne peut sortir proprement.
    barrage = {(x + 0.5, y + 0.5) for x in range(-2, 6) for y in range(-2, 6)
               if (x, y) != (0, 0)}
    tuiles, propre = tracer_en_l((0.5, 0.5), (4.5, 3.5), eviter=barrage)
    ok = not propre
    rec("test_un_trace_impossible_se_declare", ok,
        f"encerclé -> propre={propre} ({len(tuiles)} tuile(s) rendues pour information)")
    assert ok


def test_sans_interdiction_le_trace_est_celui_d_avant() -> None:
    """Back-compat stricte : sans tuiles à éviter, le chemin ne change pas.

    Toutes les lignes déjà posées par le projet — transmission du charbon, desserte des
    chaînes — dépendent de ce tracé en L, horizontal puis vertical.
    """
    tuiles, propre = tracer_en_l((0.5, 0.5), (3.5, 2.5))
    attendu = [(0.5, 0.5), (1.5, 0.5), (2.5, 0.5), (3.5, 0.5), (3.5, 1.5)]
    ok = propre and tuiles == attendu
    rec("test_sans_interdiction_le_trace_est_celui_d_avant", ok,
        f"{tuiles} == {attendu}")
    assert ok


def test_le_contournement_passe_la_ou_aucun_L_ne_passe() -> None:
    """Quand tous les L sont barrés, un vrai chemin contourne.

    C'est le défaut mesuré en jeu : trois inserters sur l'axe du cuivre. Déclarés
    franchissables, la ligne se coupait ; déclarés infranchissables, plus aucun L ne
    passait et l'on ne posait rien du tout. Un trajet qui ne sait tourner qu'une fois ne
    suffit pas dans un corridor déjà emprunté.
    """
    from services.site_finder import tracer_chemin
    # Un mur vertical complet, avec une seule ouverture très au nord : aucun L simple
    # ne peut l'emprunter, un contournement oui.
    mur = {(2.5, y + 0.5) for y in range(-6, 6) if y != -6}
    tuiles, propre = tracer_en_l((0.5, 0.5), (4.5, 0.5), eviter=mur)
    direct, trouve = tracer_chemin((0.5, 0.5), (4.5, 0.5), eviter=mur)
    contigu = all(abs(a[0] - b[0]) + abs(a[1] - b[1]) == 1.0
                  for a, b in zip(direct, direct[1:]))
    ok = (propre and tuiles and not any(t in mur for t in tuiles)
          and trouve and contigu and not any(t in direct for t in mur))
    rec("test_le_contournement_passe_la_ou_aucun_L_ne_passe", ok,
        f"mur de {len(mur)} tuiles -> {len(tuiles)} tuile(s) par tracer_en_l "
        f"(propre={propre}), {len(direct)} par le contournement (contiguës={contigu})")
    assert ok


def test_le_contournement_declare_l_impossible() -> None:
    """Encerclé, il rend `False` au lieu d'un chemin qui traverse.

    Un tracé qui passe par une tuile de trop ne se voit nulle part — sinon par un flux
    qui s'arrête sans rien signaler. Mieux vaut refuser que livrer une ligne fausse.
    """
    from services.site_finder import tracer_chemin
    cage = {(x + 0.5, y + 0.5) for x in range(-2, 3) for y in range(-2, 3)
            if (x, y) != (0, 0)}
    tuiles, trouve = tracer_chemin((0.5, 0.5), (6.5, 6.5), eviter=cage)
    ok = not trouve and not tuiles
    rec("test_le_contournement_declare_l_impossible", ok,
        f"depart encerclé -> trouvé={trouve}, {len(tuiles)} tuile(s)")
    assert ok


def main() -> int:
    for t in (test_le_trace_relie_le_depart_a_l_arrivee,
              test_le_trace_contourne_la_ligne_d_un_autre_flux,
              test_le_trace_bascule_quand_le_premier_L_est_barre,
              test_un_trace_impossible_se_declare,
              test_sans_interdiction_le_trace_est_celui_d_avant,
              test_le_contournement_passe_la_ou_aucun_L_ne_passe,
              test_le_contournement_declare_l_impossible):
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
