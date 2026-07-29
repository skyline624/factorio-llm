"""Tests unitaires du Coordinator — le raisonnement, sans serveur.

`decide()` est pur : un état observé entre, une décision sort. Tout le curriculum est
donc testable hors ligne, ce qui est l'intérêt de l'avoir écrit sans LLM en V1.

Ce qui est vérifié est l'ORDRE des priorités et la traduction diagnostic -> action,
pas le contenu d'une table (le recopier ne prouverait rien) :

  - réparer passe avant construire (une usine arrêtée ne produit rien) ;
  - sans énergie, on bâtit l'énergie avant toute production ;
  - chaque cause connue donne une action qui la répare, et une cause inconnue donne
    « inspecter » plutôt qu'une action au hasard ;
  - une usine saine décide de NE RIEN FAIRE, et le dit.

Lancement :
    cd python
    python -m tests.test_coordinator
"""

from __future__ import annotations

import sys

from agents.coordinator import EtatUsine, decide
from services.factory_doctor import diagnose

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


def _m(name: str, x: float, y: float, status: str, type_: str = "machine") -> dict:
    return {"name": name, "x": x, "y": y, "status": status, "type": type_}


def _etat(rows=None, power=None, reseau=7, kw=900.0, machines=None) -> EtatUsine:
    diag = diagnose(rows or [], power)
    return EtatUsine(machines=machines if machines is not None else diag.machines,
                     diagnostic=diag, reseau=reseau, production_kw=kw)


def test_reparer_passe_avant_construire() -> None:
    """Une panne l'emporte sur toute construction, même sans énergie par ailleurs.

    Empiler une usine neuve devant une usine cassée est exactement ce que le benchmark
    FLE reproche aux agents LLM (« rarely refine designs after initial implementation »).
    """
    casse = _etat([_m("electric-furnace", 0, 0, "no_fuel")], reseau=None, kw=0.0)
    d = decide(casse)
    ok = d.action == "ravitailler" and d.priorite == 3
    rec("test_reparer_passe_avant_construire", ok, f"{d} (priorite {d.priorite})")
    assert ok


def test_energie_avant_production() -> None:
    """Sans réseau alimenté, inutile de bâtir des machines électriques."""
    d = decide(EtatUsine(machines=0, diagnostic=diagnose([]), reseau=None,
                         production_kw=0.0))
    ok = d.action == "batir_energie" and d.priorite == 2
    rec("test_energie_avant_production", ok, str(d))
    assert ok


def test_production_quand_energie_disponible() -> None:
    d = decide(EtatUsine(machines=0, diagnostic=diagnose([]), reseau=7,
                         production_kw=900.0))
    ok = d.action == "batir_production" and d.priorite == 1
    rec("test_production_quand_energie_disponible", ok, str(d))
    assert ok


def test_usine_saine_ne_fait_rien() -> None:
    """Ne rien faire est une décision légitime — et elle doit être explicite."""
    d = decide(_etat([_m("electric-furnace", 0, 0, "working"),
                      _m("electric-mining-drill", 0, 6, "working")]))
    ok = d.action == "rien" and d.priorite == 0 and "marche" in d.raison
    rec("test_usine_saine_ne_fait_rien", ok, str(d))
    assert ok


def test_chaque_cause_donne_une_reparation() -> None:
    """Diagnostic -> action : la traduction doit couvrir les causes qu'on sait produire."""
    attendu = {
        "no_fuel": "ravitailler",
        "no_recipe": "regler_recette",
        "full_output": "evacuer",
        "disabled": "reactiver",
    }
    mauvais = []
    for statut, action in attendu.items():
        d = decide(_etat([_m("electric-furnace", 0, 0, statut)]))
        if d.action != action:
            mauvais.append(f"{statut} -> {d.action} (attendu {action})")
    # Débranchée : demande l'état électrique pour être distinguée de « sans courant ».
    d_deb = decide(_etat([_m("electric-furnace", 0, 0, "no_power")],
                         power={(0.0, 0.0): {"found": True, "networkId": None}}))
    if d_deb.action != "relier":
        mauvais.append(f"debranchee -> {d_deb.action} (attendu relier)")
    d_sec = decide(_etat([_m("electric-furnace", 0, 0, "no_power")],
                         power={(0.0, 0.0): {"found": True, "networkId": 3}}))
    if d_sec.action != "renforcer_energie":
        mauvais.append(f"sans_courant -> {d_sec.action} (attendu renforcer_energie)")
    rec("test_chaque_cause_donne_une_reparation", not mauvais, f"{mauvais or 'aucun ecart'}")
    assert not mauvais, mauvais


def test_cause_inconnue_donne_inspecter() -> None:
    """Face à une cause sans réparation connue, on le dit — on n'agit pas au hasard."""
    from services.factory_doctor import Diagnostic, Symptome
    diag = Diagnostic(machines=1)
    diag.symptomes = [Symptome("mystere", 1, 2, "cause_jamais_vue", 2, "?")]
    diag.en_panne = 1
    d = decide(EtatUsine(machines=1, diagnostic=diag, reseau=7, production_kw=900.0))
    ok = d.action == "inspecter"
    rec("test_cause_inconnue_donne_inspecter", ok, str(d))
    assert ok


def test_la_cause_la_plus_grave_est_traitee() -> None:
    """Deux pannes : celle qui arrête la machine passe avant celle qui la ralentit."""
    d = decide(_etat([_m("electric-furnace", 0, 0, "full_output"),      # gravité 1
                      _m("electric-mining-drill", 0, 6, "no_fuel")]))   # gravité 2
    ok = d.action == "ravitailler" and d.cible is not None \
        and d.cible.name == "electric-mining-drill"
    rec("test_la_cause_la_plus_grave_est_traitee", ok, str(d))
    assert ok


def test_inserter_ne_declenche_pas_de_reparation() -> None:
    """Un organe de transit qui attend ne doit pas mobiliser le Coordinator.

    Sans cette règle (héritée du FactoryDoctor), la boucle passerait son temps à
    « réparer » des inserters parfaitement sains.
    """
    d = decide(_etat([_m("inserter", 0, 0, "waiting_for_source_items", "inserter"),
                      _m("electric-furnace", 0, 5, "working")]))
    ok = d.action == "rien"
    rec("test_inserter_ne_declenche_pas_de_reparation", ok, str(d))
    assert ok


class _CoordFactice:
    """Coordinator dont on scripte les observations, pour tester la boucle `run`.

    On ne simule pas le jeu : on remplace `observer` et `agir`, et on garde le vrai
    `decide` et le vrai `run`. C'est le comportement de la BOUCLE qu'on teste.
    """

    def __init__(self, etats, agir_ok=True):
        from agents.coordinator import Coordinator
        self.etats = list(etats)
        self.agir_ok = agir_ok
        self.journal: list[str] = []
        self.appels = 0
        self.run = Coordinator.run.__get__(self)
        self.tick = Coordinator.tick.__get__(self)

    def observer(self):
        self.appels += 1
        return self.etats[min(self.appels - 1, len(self.etats) - 1)]

    def agir(self, d):
        return (self.agir_ok and d.action != "rien"), "factice"


def test_run_sarrete_quand_tout_tourne() -> None:
    """La boucle s'arrête d'elle-même dès qu'il n'y a plus rien à faire."""
    casse = _etat([_m("electric-furnace", 0, 0, "no_fuel")])
    sain = _etat([_m("electric-furnace", 0, 0, "working")])
    c = _CoordFactice([casse, sain, sain])
    decisions = c.run(max_ticks=10)
    ok = (len(decisions) == 2 and decisions[0].action == "ravitailler"
          and decisions[-1].action == "rien")
    rec("test_run_sarrete_quand_tout_tourne", ok,
        f"{[d.action for d in decisions]}")
    assert ok


def test_run_sarrete_si_ca_ne_progresse_plus() -> None:
    """Une action qui échoue deux fois de suite arrête la boucle.

    Sans cette garde, un agent bute indéfiniment sur un problème qu'il ne sait pas
    résoudre — site introuvable, item manquant — en le rediagnostiquant à chaque tour.
    Rendre la main en le disant vaut mieux que tourner en rond.
    """
    casse = _etat([_m("electric-furnace", 0, 0, "no_fuel")])
    c = _CoordFactice([casse] * 10, agir_ok=False)
    decisions = c.run(max_ticks=10)
    ok = (len(decisions) == 2                       # deux tentatives, puis arrêt
          and any("ne progresse plus" in j for j in c.journal))
    rec("test_run_sarrete_si_ca_ne_progresse_plus", ok,
        f"{len(decisions)} tour(s), journal={c.journal[-1][:60] if c.journal else ''}")
    assert ok


def test_run_respecte_le_plafond() -> None:
    """Le plafond de tours est un filet, jamais la sortie normale : il doit tenir."""
    # Des pannes différentes à chaque tour : jamais deux échecs identiques d'affilée.
    etats = [_etat([_m("electric-furnace", 0, 0, "no_fuel")]),
             _etat([_m("electric-furnace", 0, 0, "no_recipe")])] * 6
    c = _CoordFactice(etats, agir_ok=False)
    decisions = c.run(max_ticks=4)
    ok = len(decisions) == 4
    rec("test_run_respecte_le_plafond", ok, f"{len(decisions)} tour(s) pour un plafond de 4")
    assert ok


def main() -> int:
    tests = [
        test_reparer_passe_avant_construire,
        test_energie_avant_production,
        test_production_quand_energie_disponible,
        test_usine_saine_ne_fait_rien,
        test_chaque_cause_donne_une_reparation,
        test_cause_inconnue_donne_inspecter,
        test_la_cause_la_plus_grave_est_traitee,
        test_inserter_ne_declenche_pas_de_reparation,
        test_run_sarrete_quand_tout_tourne,
        test_run_sarrete_si_ca_ne_progresse_plus,
        test_run_respecte_le_plafond,
    ]
    for t in tests:
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