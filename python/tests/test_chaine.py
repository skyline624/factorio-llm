"""Tests unitaires : découvrir seul ce qu'il faut fabriquer pour obtenir un produit.

Le solveur sait dimensionner n'importe quelle chaîne, mais `populate_from_rcon` réclame
la LISTE des items — et personne ne la calculait. Il fallait donc la connaître d'avance,
c'est-à-dire écrire une recette par produit. `decouvrir_chaine` supprime ce préalable :
le produit devient un paramètre.

Ce qui est éprouvé ici, sans serveur, sur le catalogue RÉEL relevé en jeu :

  - la fermeture transitive des ingrédients est complète (rien ne manque en profondeur) ;
  - les FEUILLES sont nommées — ce qu'il faudra extraire, donc les gisements à prospecter ;
  - une recette qui boucle ne fait pas tourner le parcours indéfiniment ;
  - un catalogue vide ou un item inconnu rendent une feuille, pas une exception.

Lancement :
    cd python
    python -m tests.test_chaine
"""

from __future__ import annotations

import sys

from services.knowledge import decouvrir_chaine

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> bool:
    """Journalise ET REND le verdict : les appels s'écrivent `assert rec(...)`.

    Un test qui se contente de journaliser ne peut pas échouer sous pytest — il
    s'affiche vert quels que soient ses constats. Mesuré sur cette suite : 61 des
    326 fonctions `test_*` étaient dans ce cas. Rendre le booléen permet de garder
    la trace lisible en mode script ET de faire tomber le test en mode pytest.
    """
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:58s} {detail[:100]}")
    return ok


class FauxJeu:
    """Un catalogue de recettes au format `describe`, et rien d'autre.

    `decouvrir_chaine` est duck-typée sur `describe(name) -> dict`, comme
    `populate_from_rcon` : aucun serveur n'est nécessaire pour l'éprouver.
    """

    def __init__(self, recettes: dict):
        self.recettes = recettes
        self.appels: list[str] = []

    def describe(self, name: str):
        self.appels.append(name)
        r = self.recettes.get(name)
        if r is None:
            return {}
        return {"recipe": {"ingredients": [{"name": n, "amount": a} for n, a in r],
                           "energy": 0.5, "category": "crafting",
                           "products": [{"name": name, "amount": 1}]}}


# La chaîne réelle du flacon vert, relevée en jeu le 31/07/2026 — pas inventée.
FLACON_VERT = {
    "logistic-science-pack": [("transport-belt", 1), ("inserter", 1)],
    "transport-belt": [("iron-plate", 1), ("iron-gear-wheel", 1)],
    "inserter": [("iron-plate", 1), ("iron-gear-wheel", 1), ("electronic-circuit", 1)],
    "electronic-circuit": [("iron-plate", 1), ("copper-cable", 3)],
    "copper-cable": [("copper-plate", 1)],
    "iron-gear-wheel": [("iron-plate", 2)],
    "iron-plate": [("iron-ore", 1)],
    "copper-plate": [("copper-ore", 1)],
    # iron-ore et copper-ore n'ont pas de recette : ce sont les feuilles.
}


def test_chaine_complete_du_flacon_vert() -> None:
    """Les dix items de la chaîne, sans qu'on en souffle un seul."""
    items, feuilles = decouvrir_chaine(FauxJeu(FLACON_VERT), "logistic-science-pack")
    attendus = {"logistic-science-pack", "transport-belt", "inserter", "electronic-circuit",
                "copper-cable", "iron-gear-wheel", "iron-plate", "copper-plate",
                "iron-ore", "copper-ore"}
    assert rec("la chaîne du flacon vert est complète", set(items) == attendus,
        f"{len(items)} item(s) : {', '.join(items)}")


def test_les_feuilles_sont_les_gisements() -> None:
    """Ce qui n'a pas de recette est à EXTRAIRE — donc à prospecter."""
    _, feuilles = decouvrir_chaine(FauxJeu(FLACON_VERT), "logistic-science-pack")
    assert rec("les feuilles nomment les deux minerais", feuilles == ["copper-ore", "iron-ore"],
        f"feuilles : {feuilles}")


def test_profondeur_complete() -> None:
    """Le parcours descend jusqu'au bout, pas seulement d'un cran.

    `copper-ore` n'est atteignable qu'en quatre sauts depuis le flacon
    (inserter -> electronic-circuit -> copper-cable -> copper-plate -> copper-ore) :
    s'arrêter aux ingrédients directs le manquerait, et le solveur croirait la chaîne
    plus courte qu'elle n'est.
    """
    items, _ = decouvrir_chaine(FauxJeu(FLACON_VERT), "logistic-science-pack")
    assert rec("la profondeur ne s'arrête pas au premier cran", "copper-ore" in items,
        f"copper-ore {'trouvé' if 'copper-ore' in items else 'MANQUANT'} "
        f"(4 sauts sous la cible)")


def test_une_recette_qui_boucle_ne_tourne_pas_sans_fin() -> None:
    """La liquéfaction du charbon consomme du charbon : le parcours doit s'arrêter."""
    boucle = {"coal-liquefaction": [("coal", 10), ("steam", 50)],
              "steam": [("water", 60)],
              "coal": [("coal-liquefaction", 1)]}   # cycle volontaire
    items, _ = decouvrir_chaine(FauxJeu(boucle), "coal-liquefaction")
    assert rec("un cycle de recettes ne boucle pas", set(items) == {"coal-liquefaction", "coal",
                                                             "steam", "water"},
        f"{len(items)} item(s) : {', '.join(items)}")


def test_item_inconnu_est_une_feuille_pas_une_erreur() -> None:
    """Un minerai n'a pas de recette : c'est le cas NORMAL, pas une panne."""
    items, feuilles = decouvrir_chaine(FauxJeu({}), "iron-ore")
    assert rec("un item sans recette rend une feuille", items == ["iron-ore"]
        and feuilles == ["iron-ore"], f"items={items} feuilles={feuilles}")


def test_le_garde_borne_un_catalogue_aberrant() -> None:
    """Un catalogue qui engendre sans fin ne doit pas emporter l'agent avec lui."""
    infini = {f"i{n}": [(f"i{n + 1}", 1)] for n in range(1000)}
    items, _ = decouvrir_chaine(FauxJeu(infini), "i0", garde=25)
    assert rec("le garde borne le parcours", len(items) <= 25, f"{len(items)} item(s) (garde=25)")


def test_chaque_item_n_est_interroge_qu_une_fois() -> None:
    """`iron-plate` apparaît chez quatre parents : on ne le redemande pas quatre fois.

    Chaque `describe` est un aller-retour RCON. Sur une chaîne profonde, réinterroger
    les ingrédients communs coûterait plus que le solveur lui-même.
    """
    jeu = FauxJeu(FLACON_VERT)
    decouvrir_chaine(jeu, "logistic-science-pack")
    doublons = [n for n in set(jeu.appels) if jeu.appels.count(n) > 1]
    assert rec("aucun item n'est interrogé deux fois", not doublons,
        f"{len(jeu.appels)} appel(s), doublons : {doublons or 'aucun'}")


TESTS = [test_chaine_complete_du_flacon_vert,
         test_les_feuilles_sont_les_gisements,
         test_profondeur_complete,
         test_une_recette_qui_boucle_ne_tourne_pas_sans_fin,
         test_item_inconnu_est_une_feuille_pas_une_erreur,
         test_le_garde_borne_un_catalogue_aberrant,
         test_chaque_item_n_est_interroge_qu_une_fois]


def main() -> int:
    for t in TESTS:
        # En mode script on veut TOUS les constats : l'assertion sert à pytest, elle ne
        # doit pas cacher ceux qui suivent. `rec` a déjà journalisé avant de faire tomber.
        try:
            t()
        except AssertionError:
            pass
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{nok}/{len(RESULTS)} reussies.")
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
