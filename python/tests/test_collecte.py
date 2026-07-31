"""Tests unitaires : une foreuse doit atteindre la belt qui la collecte.

Une mine dont le minerai tombe par terre ne produit rien, et rien ne le dit : les foreuses
passent en `waiting_for_space_in_destination`, les fours restent `no_ingredients`, et toute
la chaîne s'arrête derrière sans qu'aucune erreur ne soit levée. Mesuré en jeu :
`drop -> [item-on-ground]`, foreuses en x = -16.5 et -12.5, belts en x = -6.5.

La cause est géométrique. La belt de collecte longe `v` à un `u` FIXE, calé après la
dernière foreuse ; les foreuses, elles, se rangent en colonnes successives dès que le
gisement est trop court pour les tenir toutes. La première colonne se retrouve alors à un
`_drill_step` de la belt — cinq tuiles pour une foreuse électrique, quand son drop porte à
moins de deux.

Ce qui est éprouvé ici, sans serveur :

  - chaque foreuse posée a une belt de collecte à portée de son drop ;
  - c'est vrai AUSSI quand le gisement est trop étroit pour les aligner toutes.

Lancement :
    cd python
    python -m tests.test_collecte
"""

from __future__ import annotations

import sys

from services.layout_planner import (LayoutConstraints, LayoutRequest, ResourcePatch,
                                     Terrain, _to_uv, plan)
from services.production_solver import ProductionRequest, solve
from tests.test_layout_solver import sample_geometry, sample_kb

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> bool:
    """Journalise ET rend le verdict : les appels s'écrivent `assert rec(...)`."""
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:110]}")
    return ok


# Portée du drop d'une foreuse, en tuiles depuis son centre. Au-delà, le minerai tombe
# au sol. Mesurée en jeu : centre (-16.5,-68.5) -> drop (-14.7,-68.5).
PORTEE_DROP = 2.5


def _plan_mine(largeur_v: int, machines: float = 5.0):
    """Un plan de mine sur un gisement de `largeur_v` tuiles en v.

    Étroit, il force le placement en plusieurs colonnes — le cas qui casse.
    """
    splan = solve(ProductionRequest("iron-ore", machines), sample_kb())
    terrain = Terrain(patches=[ResourcePatch(
        "iron-ore",
        tiles=[(x, y) for x in range(40) for y in range(largeur_v)],
        bbox=(0, 0, 40, largeur_v))])
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 0.0), facing=2,
                        constraints=LayoutConstraints(collect_belt_scope="drills"))
    return plan(req, sample_geometry())


def _ecarts_foreuse_belt(lp) -> list[float]:
    """Pour chaque foreuse, la distance en u à la belt de collecte la plus proche."""
    f = lp.request.facing
    drills = [e for e in lp.entities
              if getattr(e, "role", "") == "drill" and not getattr(e, "skip", False)]
    belts = [e for e in lp.entities
             if getattr(e, "role", "") in ("belt", "bus-belt") and not getattr(e, "skip", False)]
    ecarts = []
    for d in drills:
        du, dv = _to_uv(f, d.x, d.y)
        proches = [abs(_to_uv(f, b.x, b.y)[0] - du) for b in belts
                   if abs(_to_uv(f, b.x, b.y)[1] - dv) < 1.0]
        ecarts.append(min(proches) if proches else 1e9)
    return ecarts


def test_gisement_large_toutes_les_foreuses_atteignent_la_belt() -> None:
    """Cas nominal : le gisement tient toutes les foreuses sur une colonne."""
    lp = _plan_mine(largeur_v=40)
    ecarts = _ecarts_foreuse_belt(lp)
    assert rec("gisement large : chaque foreuse atteint sa belt",
               bool(ecarts) and all(e <= PORTEE_DROP for e in ecarts),
               f"{len(ecarts)} foreuse(s), écarts en u = {[round(e, 1) for e in ecarts]}")


def test_gisement_etroit_aucune_foreuse_n_est_abandonnee_loin_de_la_belt() -> None:
    """LE CAS QUI CASSE : gisement trop court, les foreuses passent en seconde colonne.

    Sans correction, la première colonne se retrouve à un `_drill_step` de la belt et son
    minerai tombe au sol — la mine est posée, elle ne produit rien.
    """
    lp = _plan_mine(largeur_v=6)
    ecarts = _ecarts_foreuse_belt(lp)
    hors = [round(e, 1) for e in ecarts if e > PORTEE_DROP]
    assert rec("gisement étroit : aucune foreuse hors de portée de sa belt",
               not hors,
               f"{len(ecarts)} foreuse(s), {len(hors)} hors de portée : {hors}")


TESTS = [test_gisement_large_toutes_les_foreuses_atteignent_la_belt,
         test_gisement_etroit_aucune_foreuse_n_est_abandonnee_loin_de_la_belt]


def main() -> int:
    for t in TESTS:
        try:
            t()
        except AssertionError:
            pass          # `rec` a déjà journalisé le constat tombé
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{nok}/{len(RESULTS)} reussies.")
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
