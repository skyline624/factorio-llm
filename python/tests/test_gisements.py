"""Tests unitaires du choix de gisement — l'énumération, sans serveur.

Ce qui est éprouvé n'est pas la préférence (elle appartient à l'arbitre) mais le CONTRAT
que le déterministe lui offre : des options toutes exécutables, décrites de quoi les
départager, et un ordre par défaut qui reste le comportement historique.

Le veto porte sur la légalité, jamais sur le goût : un gisement hors de portée d'une
belt n'est pas une option qu'on laisse choisir, c'est une option qui échouerait.

Lancement :
    cd python
    python -m tests.test_gisements
"""

from __future__ import annotations

import sys

from services.gisements import Gisement, enumerer

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


class FakeApi:
    """Rend des gisements scriptés, et des nids autour de certains d'entre eux."""

    def __init__(self, patches, nids=None):
        self.patches = list(patches)
        self.nids = dict(nids or {})     # (x, y) -> [distances]
        self.menaces_lues = 0

    def scan_patches(self, resource, radius=300.0, max_patches=8):
        return {"resource": resource, "patches": self.patches,
                "count": sum(p["count"] for p in self.patches),
                "groupes": len(self.patches)}

    def scan_threats(self, x, y, radius=60.0):
        self.menaces_lues += 1
        d = self.nids.get((x, y), [])
        return {"nests": [{"name": "biter-spawner", "dist": v} for v in d]}


PATCHES = [
    {"x": 10.0, "y": 0.0, "count": 136, "amount": 200_000},   # proche et petit
    {"x": 40.0, "y": 0.0, "count": 738, "amount": 391_000},   # loin et gros
    {"x": 500.0, "y": 0.0, "count": 900, "amount": 999_000},  # hors de portée
]


def test_hors_de_portee_nest_pas_une_option() -> None:
    """Un gisement qu'une belt ne peut pas atteindre ne doit jamais être proposé.

    Le contrat de l'arbitre promet des options EXÉCUTABLES : lui en offrir une qui
    échouera revient à lui mentir, et c'est le défaut qu'on avait déjà corrigé sur les
    actions sans matériel.
    """
    api = FakeApi(PATCHES)
    g = enumerer(api, "iron-ore", (0.0, 0.0), portee_max=60.0)
    ok = len(g) == 2 and all(x.distance <= 60.0 for x in g)
    rec("test_hors_de_portee_nest_pas_une_option", ok,
        f"{len(g)} option(s) sur 3 gisements, distances={[round(x.distance) for x in g]}")
    assert ok


def test_le_plus_proche_reste_loption_par_defaut() -> None:
    """L'option 0 est le comportement historique : sans arbitre, rien ne change."""
    g = enumerer(FakeApi(PATCHES), "iron-ore", (0.0, 0.0), portee_max=60.0)
    ok = g and g[0].distance == min(x.distance for x in g)
    rec("test_le_plus_proche_reste_loption_par_defaut", ok,
        f"option 0 à {g[0].distance:.0f} tuiles ({g[0].tuiles} tuiles de minerai)")
    assert ok


def test_les_options_portent_de_quoi_les_departager() -> None:
    """Sans taille ni réserve, « le plus proche » serait le seul critère possible.

    C'est ce qui rendait le choix indécidable : deux options qu'on ne peut pas comparer
    ne sont pas un arbitrage, c'est un tirage.
    """
    g = enumerer(FakeApi(PATCHES), "iron-ore", (0.0, 0.0), portee_max=60.0)
    proche, gros = g[0], g[1]
    ok = (proche.distance < gros.distance and proche.tuiles < gros.tuiles
          and proche.reserve < gros.reserve)
    rec("test_les_options_portent_de_quoi_les_departager", ok,
        f"aucune ne domine : {proche.tuiles} tuiles à {proche.distance:.0f} "
        f"contre {gros.tuiles} à {gros.distance:.0f}")
    assert ok


def test_la_menace_est_mesuree_par_gisement() -> None:
    """Un gisement bordé d'un nid perdra sa belt : la menace ne se déduit pas de la distance."""
    api = FakeApi(PATCHES, nids={(10.0, 0.0): [22.0, 45.0]})
    g = enumerer(api, "iron-ore", (0.0, 0.0), portee_max=60.0)
    proche = g[0]
    ok = (proche.nids == 2 and proche.nid_proche == 22.0 and not proche.sur
          and g[1].sur and api.menaces_lues == 2)
    rec("test_la_menace_est_mesuree_par_gisement", ok,
        f"proche : {proche.nids} nid(s) à {proche.nid_proche} ; lointain sûr={g[1].sur}")
    assert ok


def test_menace_desactivable_pour_ne_pas_payer_les_appels() -> None:
    """Une énumération sans menace ne doit interroger le jeu qu'une fois."""
    api = FakeApi(PATCHES, nids={(10.0, 0.0): [22.0]})
    g = enumerer(api, "iron-ore", (0.0, 0.0), portee_max=60.0, evaluer_menace=False)
    ok = len(g) == 2 and api.menaces_lues == 0 and all(x.sur for x in g)
    rec("test_menace_desactivable_pour_ne_pas_payer_les_appels", ok,
        f"{api.menaces_lues} appel(s) de menace")
    assert ok


def test_aucun_gisement_a_portee() -> None:
    """Rien à portée doit rendre une liste vide, pas une option impossible."""
    g = enumerer(FakeApi([PATCHES[2]]), "iron-ore", (0.0, 0.0), portee_max=60.0)
    ok = g == []
    rec("test_aucun_gisement_a_portee", ok, "aucune option, et c'est la bonne réponse")
    assert ok


def test_description_lisible() -> None:
    """La description EST ce que le modèle lit : elle doit porter les quatre critères."""
    texte = str(Gisement("coal", 12.0, -3.0, 521, 3_058_813, 12.4, 1, 30.0))
    ok = all(m in texte for m in ("coal", "521", "3058k", "12 tuiles", "nid"))
    rec("test_description_lisible", ok, texte)
    assert ok, texte


def main() -> int:
    for t in (test_hors_de_portee_nest_pas_une_option,
              test_le_plus_proche_reste_loption_par_defaut,
              test_les_options_portent_de_quoi_les_departager,
              test_la_menace_est_mesuree_par_gisement,
              test_menace_desactivable_pour_ne_pas_payer_les_appels,
              test_aucun_gisement_a_portee, test_description_lisible):
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