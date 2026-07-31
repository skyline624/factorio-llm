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
                                     Terrain, FACING_UNIT, _to_uv, plan)
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


def _plan_chaine(rate: float = 0.5):
    """Un plan complet ore -> plate -> gear, la chaîne courte à un seul gisement."""
    splan = solve(ProductionRequest("iron-gear-wheel", rate), sample_kb())
    terrain = Terrain(patches=[ResourcePatch(
        "iron-ore",
        tiles=[(x, y) for x in range(40) for y in range(40)],
        bbox=(0, 0, 40, 40))])
    req = LayoutRequest(plan=splan, terrain=terrain, anchor=(0.0, 0.0), facing=2,
                        constraints=LayoutConstraints(collect_belt_scope="drills"))
    return plan(req, sample_geometry())


def test_chaque_bras_a_quelque_chose_sous_sa_prise() -> None:
    """Un bras qui prend dans le VIDE ne transporte rien, et rien ne le dit.

    Mesuré en jeu : `burner-inserter waiting_for_source_items pickup[] drop[transport-belt]`
    — le bras prenait dans le vide et déposait sur une belt, c'est-à-dire à l'envers de ce
    qu'il devait faire. Un bras d'entrée prend SUR LA BELT et dépose DANS LA MACHINE ;
    entre les deux il n'y a rien à inventer, mais tout à vérifier.

    On regarde ce qui se trouve à la position de prise, dans le repère du plan : une belt,
    une machine ou une foreuse. Le vide est le seul résultat interdit.
    """
    lp = _plan_chaine()
    f = lp.request.facing
    vivants = [e for e in lp.entities if not getattr(e, "skip", False)]
    bras = [e for e in vivants if getattr(e, "role", "") == "inserter"]
    # Tout ce dans quoi un bras peut puiser, indexé par tuile (u,v) arrondie.
    # PAR EMPRISE, pas par centre : une machine 3×3 occupe neuf tuiles, et la prise d'un
    # bras de sortie tombe dans la machine SANS tomber sur son centre. Indexer les seuls
    # centres faisait passer neuf bras corrects pour aveugles — le test accusait le plan
    # d'un défaut qui était le sien.
    geo = sample_geometry()
    sources = {}
    for e in vivants:
        if getattr(e, "role", "") not in ("belt", "bus-belt", "machine", "drill"):
            continue
        ge = geo.geometry(e.name)
        w = (ge.w if ge else 1) / 2.0
        h = (ge.h if ge else 1) / 2.0
        for dx in (-w + 0.5, 0.0, w - 0.5):
            for dy in (-h + 0.5, 0.0, h - 0.5):
                eu, ev = _to_uv(f, e.x + dx, e.y + dy)
                sources[(round(eu), round(ev))] = e.name

    aveugles = []
    for b in bras:
        g = geo.geometry(b.name)
        reach = (g.pickup_distance if g and g.pickup_distance else 1.0)
        # LA PRISE SE DÉDUIT DE LA DIRECTION POSÉE, pas de celle qu'on aurait voulue.
        # Mesuré en jeu sur les quatre orientations : un bras PREND du côté vers lequel il
        # pointe et dépose à l'opposé — l'inverse de ce que suppose le placement. Calculer
        # la prise « en arrière du bras » masquait donc exactement le défaut recherché.
        ux, uy = FACING_UNIT[b.direction]
        pu, pv = _to_uv(f, b.x + ux * reach, b.y + uy * reach)
        if (round(pu), round(pv)) not in sources:
            aveugles.append((b.name, round(b.x, 1), round(b.y, 1), b.direction))
    assert rec("chaque bras a une source sous sa prise", not aveugles,
               f"{len(bras)} bras, {len(aveugles)} sans source : {aveugles[:4]}")


def test_chaque_bras_puise_dans_la_bonne_source() -> None:
    """Trouver QUELQUE CHOSE sous la prise ne suffit pas : encore faut-il que ce soit ÇA.

    Un bras transporte un item — `node_item` le dit. Il n'y a donc que deux montages
    licites, et ils se distinguent par ce qu'on trouve sous la prise :

      - une BELT qui porte le même item : le bras alimente la machine devant lui ;
      - une MACHINE qui produit cet item : le bras l'évacue vers la belt devant lui.

    Tout le reste est un bras qui brasse du vent. Le cas mesuré en jeu est le second
    déguisé en premier : les bras d'ENTRÉE, orientés vers la machine, puisaient dedans au
    lieu de puiser sur la belt — un four ne produit pas le minerai qu'on doit lui donner,
    et il restait donc `no_ingredients` pendant que la belt saturait derrière.
    """
    lp = _plan_chaine()
    f = lp.request.facing
    geo = sample_geometry()
    vivants = [e for e in lp.entities if not getattr(e, "skip", False)]
    sources = {}
    for e in vivants:
        if getattr(e, "role", "") not in ("belt", "bus-belt", "machine", "drill"):
            continue
        ge = geo.geometry(e.name)
        w = (ge.w if ge else 1) / 2.0
        h = (ge.h if ge else 1) / 2.0
        for dx in (-w + 0.5, 0.0, w - 0.5):
            for dy in (-h + 0.5, 0.0, h - 0.5):
                eu, ev = _to_uv(f, e.x + dx, e.y + dy)
                sources[(round(eu), round(ev))] = e

    incoherents = []
    for b in [e for e in vivants if getattr(e, "role", "") == "inserter"]:
        g = geo.geometry(b.name)
        reach = (g.pickup_distance if g and g.pickup_distance else 1.0)
        ux, uy = FACING_UNIT[b.direction]
        pu, pv = _to_uv(f, b.x + ux * reach, b.y + uy * reach)
        src = sources.get((round(pu), round(pv)))
        porte = getattr(b, "node_item", "")
        if src is None or getattr(src, "node_item", "") != porte:
            incoherents.append((round(b.x, 1), round(b.y, 1), porte,
                                getattr(src, "name", "vide"),
                                getattr(src, "node_item", "-")))
    assert rec("chaque bras puise dans une source qui porte son item", not incoherents,
               f"{len(incoherents)} bras incohérent(s) : {incoherents[:3]}")


TESTS = [test_gisement_large_toutes_les_foreuses_atteignent_la_belt,
         test_gisement_etroit_aucune_foreuse_n_est_abandonnee_loin_de_la_belt,
         test_chaque_bras_a_quelque_chose_sous_sa_prise,
         test_chaque_bras_puise_dans_la_bonne_source]


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
