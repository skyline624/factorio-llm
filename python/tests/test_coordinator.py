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


# Inventaire par défaut des fixtures : un agent qui répare dispose du matériel
# courant. Sans lui, `BESOINS` déclasse à juste titre toute réparation — ce qui est le
# comportement voulu, mais pas le sujet de la plupart des tests.
INVENTAIRE = {"coal": 100, "gun-turret": 8, "firearm-magazine": 200,
              "small-electric-pole": 20}


def _etat(rows=None, power=None, reseau=7, kw=900.0, machines=None,
          inventaire=None) -> EtatUsine:
    diag = diagnose(rows or [], power)
    return EtatUsine(machines=machines if machines is not None else diag.machines,
                     diagnostic=diag, reseau=reseau, production_kw=kw,
                     inventaire=dict(INVENTAIRE if inventaire is None else inventaire))


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


def _menace(niveau, front=(0.0, -1.0), nom="nord"):
    from services.threat_model import Menace
    return Menace(niveau=niveau, raison="test", front=front, front_nom=nom)


def test_ennemis_sur_lusine_passent_avant_les_reparations() -> None:
    """Rien ne sert de remettre un four en marche pendant qu'on le détruit."""
    from services.threat_model import EN_COURS
    etat = _etat([_m("electric-furnace", 0, 0, "no_fuel")])
    etat.menace = _menace(EN_COURS)
    d = decide(etat)
    ok = d.action == "defendre" and d.priorite == 4
    rec("test_ennemis_sur_lusine_passent_avant_les_reparations", ok, str(d))
    assert ok


def test_menace_latente_najoute_aucune_option() -> None:
    """Des nids sans pollution : on ne propose même pas de fortifier.

    Le temps passé à se défendre n'est pas passé à produire ; proposer l'option
    reviendrait à laisser un arbitre la choisir sans raison.
    """
    from agents.coordinator import enumerer_options
    from services.threat_model import LATENTE
    etat = _etat([_m("electric-furnace", 0, 0, "working")])
    etat.menace = _menace(LATENTE)
    options = enumerer_options(etat)
    ok = all(o.action != "defendre" for o in options)
    rec("test_menace_latente_najoute_aucune_option", ok,
        f"{[o.action for o in options]}")
    assert ok


def test_menace_imminente_cree_un_vrai_choix() -> None:
    """LE cas qui motive un arbitre : deux options défendables, à égalité.

    Usine saine, aucune machine encore construite, et des vagues sur le point de
    partir : fortifier ou produire ? La priorité ne tranche pas (2 contre 2 — c'était
    l'intention), et aucune règle simple ne le ferait honnêtement. Jusqu'ici le
    curriculum était linéaire et le déterministe suffisait ; ici il choisit par défaut,
    faute de mieux.
    """
    from agents.coordinator import enumerer_options
    from services.threat_model import IMMINENTE
    etat = EtatUsine(machines=0, diagnostic=diagnose([]), reseau=7, production_kw=900.0)
    etat.menace = _menace(IMMINENTE)
    options = enumerer_options(etat)
    actions = [o.action for o in options]
    ok = ("defendre" in actions and "batir_production" in actions
          and len(options) >= 2)
    rec("test_menace_imminente_cree_un_vrai_choix", ok,
        f"{[(o.action, o.priorite) for o in options]}")
    assert ok


def test_option_sans_materiel_est_declassee() -> None:
    """Une action dont le matériel manque ne doit pas être proposée en tête.

    Révélé en confrontant l'arbitre à un vrai modèle : privé de toute tourelle, il
    choisissait « defendre » trois fois sur trois, avec une justification solide sur la
    menace. Il n'avait aucun moyen de savoir que l'action échouerait — rien dans les
    options ne portait leur coût. Un déterministe qui propose l'infaisable trompe aussi
    bien un humain qu'une machine.

    Elle est DÉCLASSÉE et non supprimée : l'effacer masquerait le besoin, alors qu'il
    faudra un jour décider d'aller fabriquer ce qui manque.
    """
    from agents.coordinator import enumerer_options
    from services.threat_model import IMMINENTE
    etat = EtatUsine(machines=0, diagnostic=diagnose([]), reseau=7,
                     production_kw=900.0, inventaire={"coal": 50})   # aucune tourelle
    etat.menace = _menace(IMMINENTE)
    options = enumerer_options(etat)
    defense = next((o for o in options if o.action == "defendre"), None)
    ok = (defense is not None and not defense.faisable and defense.priorite == 0
          and "INFAISABLE" in defense.raison and "gun-turret" in defense.raison
          # ... et la production, elle, reste faisable : elle passe donc devant.
          and options[0].action == "batir_production")
    rec("test_option_sans_materiel_est_declassee", ok,
        f"{[(o.action, o.priorite, o.faisable) for o in options]}")
    assert ok


def test_option_avec_materiel_reste_prioritaire() -> None:
    """Le pendant : avec le matériel, rien ne change — la règle ne pénalise pas à tort."""
    from agents.coordinator import enumerer_options
    from services.threat_model import IMMINENTE
    etat = _etat(machines=0)
    etat.menace = _menace(IMMINENTE)
    options = enumerer_options(etat)
    ok = options[0].action == "defendre" and options[0].faisable
    rec("test_option_avec_materiel_reste_prioritaire", ok,
        f"{[(o.action, o.priorite) for o in options]}")
    assert ok


def test_options_une_par_cause() -> None:
    """`enumerer_options` expose TOUTES les réparations légales, pas seulement la 1re.

    C'est ce qui donne un choix à un arbitre : quelle panne traiter d'abord quand
    plusieurs coexistent n'est pas toujours tranché par la gravité seule.
    """
    from agents.coordinator import enumerer_options
    etat = _etat([_m("electric-furnace", 0, 0, "no_fuel"),
                  _m("assembling-machine-1", 0, 6, "no_recipe"),
                  _m("electric-mining-drill", 0, 12, "full_output")])
    options = enumerer_options(etat)
    actions = [o.action for o in options]
    ok = (len(options) == 3 and set(actions) == {"ravitailler", "regler_recette", "evacuer"}
          # L'ordre par défaut reste le curriculum : les pannes graves d'abord.
          and options[-1].action == "evacuer")
    rec("test_options_une_par_cause", ok, f"{actions}")
    assert ok


def test_decide_sans_arbitre_prend_la_premiere() -> None:
    """Sans arbitre, la décision est exactement `options[0]` — le déterministe intact."""
    from agents.coordinator import enumerer_options
    etat = _etat([_m("electric-furnace", 0, 0, "no_fuel"),
                  _m("assembling-machine-1", 0, 6, "no_recipe")])
    ok = decide(etat).action == enumerer_options(etat)[0].action
    rec("test_decide_sans_arbitre_prend_la_premiere", ok, f"{decide(etat)}")
    assert ok


def test_arbitre_choisit_une_autre_option() -> None:
    """Un arbitre valide impose son choix — c'est le point d'insertion du LLM.

    On compare à `options[1]` plutôt qu'à une action nommée : à gravité égale, le
    diagnostic départage par nom d'entité, et figer cet ordre dans le test le rendrait
    faux au premier renommage. Ce qui compte est que l'arbitre l'emporte sur le défaut.
    """
    from agents.coordinator import enumerer_options
    etat = _etat([_m("electric-furnace", 0, 0, "no_fuel"),
                  _m("assembling-machine-1", 0, 6, "no_recipe")])
    options = enumerer_options(etat)
    d = decide(etat, arbitre=lambda e, opts: 1)
    ok = (len(options) == 2 and d.action == options[1].action
          and d.action != options[0].action)
    rec("test_arbitre_choisit_une_autre_option", ok,
        f"défaut={options[0].action} -> arbitre={d.action}")
    assert ok


def test_arbitre_defaillant_replie_sur_le_deterministe() -> None:
    """Indice hors bornes, mauvais type, ou exception : la boucle continue quand même.

    Un agent qui s'arrête parce que le modèle est indisponible ou répond n'importe quoi
    ne vaut rien. Le déterministe est le filet, pas l'exception.
    """
    etat = _etat([_m("electric-furnace", 0, 0, "no_fuel"),
                  _m("assembling-machine-1", 0, 6, "no_recipe")])
    attendu = decide(etat).action
    def _explose(e, opts):
        raise RuntimeError("modèle injoignable")
    cas = {
        "hors bornes": decide(etat, arbitre=lambda e, o: 99),
        "négatif": decide(etat, arbitre=lambda e, o: -1),
        "mauvais type": decide(etat, arbitre=lambda e, o: "ravitailler"),
        "booléen": decide(etat, arbitre=lambda e, o: True),
        "exception": decide(etat, arbitre=_explose),
        "None": decide(etat, arbitre=lambda e, o: None),
    }
    mauvais = [k for k, d in cas.items() if d.action != attendu]
    rec("test_arbitre_defaillant_replie_sur_le_deterministe", not mauvais,
        f"repli attendu={attendu} ; écarts={mauvais or 'aucun'}")
    assert not mauvais, mauvais


def test_arbitre_non_appele_sans_choix() -> None:
    """Une seule option -> pas d'appel : on ne paie pas un aller-retour pour rien.

    C'est le cas le plus fréquent (usine saine, ou panne unique), et c'est ce qui rend
    le coût d'un arbitrage LLM supportable : un appel par vrai choix, pas par tour.
    """
    appels = []

    def _compte(e, opts):
        appels.append(len(opts))
        return 0

    decide(_etat([_m("electric-furnace", 0, 0, "working")]), arbitre=_compte)  # « rien »
    decide(_etat([_m("electric-furnace", 0, 0, "no_fuel")]), arbitre=_compte)  # 1 panne
    sans_choix = len(appels)
    decide(_etat([_m("electric-furnace", 0, 0, "no_fuel"),
                  _m("assembling-machine-1", 0, 6, "no_recipe")]), arbitre=_compte)
    ok = sans_choix == 0 and len(appels) == 1
    rec("test_arbitre_non_appele_sans_choix", ok,
        f"{sans_choix} appel(s) sans choix, {len(appels)} au total")
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
        self.arbitre = None          # `tick` le lit ; ces tests portent sur la boucle
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
        test_ennemis_sur_lusine_passent_avant_les_reparations,
        test_menace_latente_najoute_aucune_option,
        test_menace_imminente_cree_un_vrai_choix,
        test_option_sans_materiel_est_declassee,
        test_option_avec_materiel_reste_prioritaire,
        test_options_une_par_cause,
        test_decide_sans_arbitre_prend_la_premiere,
        test_arbitre_choisit_une_autre_option,
        test_arbitre_defaillant_replie_sur_le_deterministe,
        test_arbitre_non_appele_sans_choix,
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