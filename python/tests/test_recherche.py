"""Tests unitaires : traduire « recette verrouillée » en « voilà quoi faire ».

Le planificateur s'arrêtait sur « ni ressource, ni recette accessible pour
`small-electric-pole` » — vrai, et sans issue. Ce qui est éprouvé ici, sans serveur, sur
l'arbre RÉEL d'une carte neuve (relevé en jeu le 30/07/2026) :

  - une recette fermée se relie à la technologie qui l'ouvre ;
  - le prix d'une technologie n'est pas forcément en flacons : les premières marches
    sont des GESTES, et ce sont les seules qu'un agent sans laboratoire peut viser ;
  - un déclencheur `craft-item` compte ce qu'on FABRIQUE, pas ce qu'on possède — s'en
    remettre à la quantité brute ne déclenche rien quand on en a déjà.

Lancement :
    cd python
    python -m tests.test_recherche
"""

from __future__ import annotations

import sys

from services.recherche import Arbre, Marche, lire, quantite_a_produire

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:100]}")


# L'arbre tel que le mod le rend sur une carte neuve — relevé en jeu, pas inventé.
BRUT_CARTE_NEUVE = {
    "acquises": ["steam-power"],
    "ouvertes": [
        {"name": "electronics", "pret": True, "unites": 1, "cout": [],
         "declencheur": {"type": "craft-item", "cible": "copper-plate", "count": 10},
         "debloque": ["copper-cable", "electronic-circuit", "lab", "inserter",
                      "small-electric-pole"]},
    ],
    "en_cours": None,
    "progres": 0,
}

# Plus tard dans la partie : une marche qui se paie, elle, en flacons.
BRUT_AVEC_SCIENCE = {
    "acquises": ["steam-power", "electronics", "automation-science-pack"],
    "ouvertes": [
        {"name": "automation", "pret": True, "unites": 10,
         "cout": [{"name": "automation-science-pack", "count": 1}],
         "debloque": ["assembling-machine-1", "long-handed-inserter"]},
        {"name": "logistics", "pret": True, "unites": 20,
         "cout": [{"name": "automation-science-pack", "count": 1}],
         "debloque": ["underground-belt", "splitter"]},
    ],
    "en_cours": None,
}


class _ApiFactice:
    def __init__(self, brut):
        self.brut = brut
        self.appels = 0

    def get_technologies(self, seulement_pretes: bool = True):
        self.appels += 1
        return self.brut


class _ApiMuette:
    def get_technologies(self, seulement_pretes: bool = True):
        raise RuntimeError("RCON injoignable")


def test_une_recette_fermee_se_relie_a_sa_technologie() -> None:
    """« Je ne sais pas faire » et « ce n'est pas débloqué » ne sont pas la même chose.

    C'est toute la valeur de ce module : sans lui, l'agent constate un mur ; avec lui,
    il sait quelle porte l'ouvre.
    """
    arbre = lire(_ApiFactice(BRUT_CARTE_NEUVE))
    m = arbre.pour_recette("small-electric-pole")
    inconnue = arbre.pour_recette("nuclear-reactor")
    ok = m is not None and m.nom == "electronics" and inconnue is None
    rec("test_une_recette_fermee_se_relie_a_sa_technologie", ok,
        f"small-electric-pole -> {m.nom if m else None} ; nuclear-reactor -> {inconnue}")
    assert ok


def test_la_premiere_marche_ne_coute_aucun_flacon() -> None:
    """Un agent sans laboratoire n'est pas bloqué : la première marche est un GESTE.

    `electronics` s'ouvre en fabriquant dix plaques de cuivre. Si l'on ne lisait que le
    coût en science, elle paraîtrait gratuite ET inaccessible, et l'agent attendrait un
    prix qui ne vient jamais.
    """
    arbre = lire(_ApiFactice(BRUT_CARTE_NEUVE))
    m = arbre.marches[0]
    ok = (m.gratuite and m.declencheur == ("craft-item", "copper-plate", 10)
          and arbre.sans_flacons == (m,) and "lab" in m.debloque)
    rec("test_la_premiere_marche_ne_coute_aucun_flacon", ok, f"{m}")
    assert ok


def test_le_declencheur_compte_ce_qu_on_fabrique_pas_ce_qu_on_a() -> None:
    """Mesuré en jeu, et c'est le piège qui a coûté le plus cher.

    Avec trente plaques de cuivre en poche, demander « fabrique 10 copper-plate » rend
    « l'inventaire en contient déjà assez » : rien n'est produit, et la technologie reste
    fermée. Il faut viser ce qu'on a DÉJÀ, plus ce que le déclencheur réclame.
    """
    m = lire(_ApiFactice(BRUT_CARTE_NEUVE)).marches[0]
    vide = quantite_a_produire(m, {})
    garni = quantite_a_produire(m, {"copper-plate": 30})
    ok = vide == ("copper-plate", 10) and garni == ("copper-plate", 40)
    rec("test_le_declencheur_compte_ce_qu_on_fabrique_pas_ce_qu_on_a", ok,
        f"les mains vides -> {vide} ; avec 30 en poche -> {garni}")
    assert ok


def test_une_marche_qui_se_paie_est_distinguee() -> None:
    """Plus loin dans l'arbre, le prix redevient des flacons — et il faut le voir.

    Confondre les deux régimes ferait envoyer l'agent « fabriquer » une technologie qui
    demande en réalité une usine de science.
    """
    arbre = lire(_ApiFactice(BRUT_AVEC_SCIENCE))
    auto = arbre.pour_recette("assembling-machine-1")
    ok = (auto is not None and not auto.gratuite and auto.declencheur is None
          and auto.cout == (("automation-science-pack", 1),) and auto.unites == 10
          and arbre.sans_flacons == ())
    rec("test_une_marche_qui_se_paie_est_distinguee", ok, f"{auto}")
    assert ok


def test_une_lecture_ratee_ne_rend_pas_l_agent_plus_bete() -> None:
    """RCON muet : on rend un arbre VIDE, pas une exception.

    Un arbre vide se lit comme « rien à chercher » — exactement le comportement d'avant
    ce module. Une lecture ratée ne doit pas casser une boucle qui tournait sans elle.
    """
    arbre = lire(_ApiMuette())
    ok = (arbre.marches == () and arbre.acquises == frozenset()
          and arbre.pour_recette("lab") is None)
    rec("test_une_lecture_ratee_ne_rend_pas_l_agent_plus_bete", ok,
        f"{len(arbre.marches)} marche(s), {len(arbre.acquises)} acquise(s)")
    assert ok


def test_un_arbre_malforme_ne_fait_pas_tomber_la_lecture() -> None:
    """Le mod peut rendre des lignes partielles ; on garde ce qui est exploitable."""
    arbre = lire(_ApiFactice({"acquises": ["steam-power"], "ouvertes": [
        {"name": "bonne", "debloque": ["x"], "cout": [{"name": "p", "count": 2}]},
        {"pas_de_nom": True},
        "pas un dictionnaire",
        {"name": "sans_rien"},
    ]}))
    noms = [m.nom for m in arbre.marches]
    ok = noms == ["bonne", "sans_rien"] and arbre.marches[0].cout == (("p", 2),)
    rec("test_un_arbre_malforme_ne_fait_pas_tomber_la_lecture", ok,
        f"retenu : {noms}")
    assert ok


def test_str_dit_quoi_faire_en_une_ligne() -> None:
    """Le journal se lit sans ouvrir le code : la marche dit son geste et son gain."""
    geste = str(Marche("electronics", ("lab", "inserter"),
                       ("craft-item", "copper-plate", 10)))
    flacons = str(Marche("automation", ("assembling-machine-1",), None,
                         (("automation-science-pack", 1),), 10))
    ok = ("fabriquer 10 copper-plate" in geste and "lab" in geste
          and "10 x 1 automation-science-pack" in flacons)
    rec("test_str_dit_quoi_faire_en_une_ligne", ok, f"{geste} | {flacons}")
    assert ok


def main() -> int:
    for t in (test_une_recette_fermee_se_relie_a_sa_technologie,
              test_la_premiere_marche_ne_coute_aucun_flacon,
              test_le_declencheur_compte_ce_qu_on_fabrique_pas_ce_qu_on_a,
              test_une_marche_qui_se_paie_est_distinguee,
              test_une_lecture_ratee_ne_rend_pas_l_agent_plus_bete,
              test_un_arbre_malforme_ne_fait_pas_tomber_la_lecture,
              test_str_dit_quoi_faire_en_une_ligne):
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
