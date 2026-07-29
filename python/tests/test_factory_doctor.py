"""Tests unitaires du FactoryDoctor — distinguer la cause du symptôme.

`diagnose` est une fonction pure : on lui donne des entités telles que `scan_area` les
rend, elle rend un diagnostic. Aucun serveur.

Ce qui est vérifié n'est pas un mapping de statuts (ce serait recopier le code) mais le
comportement qui fait la valeur du service :

  - une machine sans courant est distinguée d'une machine DÉBRANCHÉE (deux réparations
    différentes : agrandir la centrale / poser un poteau) ;
  - quand une panne propre existe en amont, les machines à l'entrée vide sont déclassées
    en conséquences — c'est le mode d'échec n°1 des agents LLM mesuré par le benchmark
    FLE, qui réparent la machine qui affiche l'erreur plutôt que celle qui la cause ;
  - une usine saine ne produit aucun symptôme (pas de faux positif) ;
  - un statut que le mod n'interprète pas ne produit pas de diagnostic inventé.

Lancement :
    cd python
    python -m tests.test_factory_doctor
"""

from __future__ import annotations

import sys

from services.factory_doctor import diagnose

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


def _m(name: str, x: float, y: float, status: str, type_: str = "machine") -> dict:
    return {"name": name, "x": x, "y": y, "status": status, "type": type_}


def test_organe_de_transit_nest_pas_une_cause() -> None:
    """Un inserter qui attend est dans son régime NORMAL, pas en panne.

    Constat live : sur une chaîne parfaitement saine, l'inserter ressortait comme seule
    « cause » du diagnostic (`waiting_for_source_items` entre deux transferts). Un
    organe de transfert reflète l'état de ses voisins ; le signaler noie le diagnostic
    sous des symptômes qui ne se réparent pas. Il ne devient une cause que par une
    panne propre — sans courant, par exemple.
    """
    sain = diagnose([_m("inserter", 0, 0, "waiting_for_source_items", "inserter"),
                     _m("electric-furnace", 0, 5, "working")])
    prive = diagnose([_m("inserter", 0, 0, "no_power", "inserter")])
    ok = (not sain.causes                                   # rien à réparer
          and len(prive.causes) == 1                        # mais une panne propre compte
          and prive.causes[0].cause == "sans_courant")
    rec("test_organe_de_transit_nest_pas_une_cause", ok,
        f"sain -> {len(sain.causes)} cause(s) ; sans courant -> "
        f"{[s.cause for s in prive.causes]}")
    assert ok


def test_usine_saine_aucun_symptome() -> None:
    d = diagnose([_m("electric-mining-drill", 0, 0, "working"),
                  _m("electric-furnace", 0, 5, "working")])
    ok = d.sain and not d.symptomes and "aucune en panne" in d.resume()
    rec("test_usine_saine_aucun_symptome", ok, d.resume())
    assert ok


def test_debranchee_vs_sans_courant() -> None:
    """Deux pannes que le statut seul confond, et qui ne se réparent pas pareil."""
    rows = [_m("electric-furnace", 0, 0, "no_power"),
            _m("electric-mining-drill", 10, 0, "no_power")]
    power = {
        (0.0, 0.0): {"found": True, "networkId": None},          # reliée à rien
        (10.0, 0.0): {"found": True, "networkId": 7, "productionKW": 0.0},
    }
    d = diagnose(rows, power)
    causes = {s.name: s.cause for s in d.symptomes}
    ok = (causes.get("electric-furnace") == "debranchee"
          and causes.get("electric-mining-drill") == "sans_courant")
    rec("test_debranchee_vs_sans_courant", ok, f"{causes}")
    assert ok


def test_cause_distinguee_du_symptome() -> None:
    """Un four à l'entrée vide derrière un drill sans courant n'est PAS la cause.

    Réparer le four ne servirait à rien. C'est précisément l'erreur que le benchmark
    FLE observe chez les agents LLM.
    """
    d = diagnose([_m("electric-mining-drill", 0, 0, "no_power"),
                  _m("inserter", 0, 3, "waiting_for_source_items", "inserter"),
                  _m("electric-furnace", 0, 6, "waiting_for_source_items")])
    causes = d.causes
    ok = (len(causes) == 1 and causes[0].name == "electric-mining-drill"
          and causes[0].cause == "sans_courant"
          and d.en_panne == 3          # les trois sont bien signalées...
          and all(not s.racine for s in d.symptomes if s.cause == "entree_vide"))
    rec("test_cause_distinguee_du_symptome", ok,
        f"{len(causes)} cause(s) sur {d.en_panne} machine(s) en panne -> {causes[0] if causes else None}")
    assert ok


def test_entree_vide_seule_reste_une_cause() -> None:
    """Sans panne propre en amont, une entrée vide EST le problème (rien ne l'alimente)."""
    d = diagnose([_m("electric-furnace", 0, 0, "waiting_for_source_items")])
    ok = len(d.causes) == 1 and d.causes[0].cause == "entree_vide" and d.causes[0].racine
    rec("test_entree_vide_seule_reste_une_cause", ok, d.resume())
    assert ok


def test_statut_non_interprete_ne_produit_rien() -> None:
    """`other` est le fourre-tout du mod : on n'en conclut rien plutôt que d'inventer.

    Le mod l'emploie notamment pour l'inactivité normale — un four qui n'a rien à fondre
    n'est pas en panne. En faire un symptôme noierait le diagnostic sous du bruit.
    """
    d = diagnose([_m("stone-furnace", 0, 0, "other")])
    ok = d.sain and not d.symptomes
    rec("test_statut_non_interprete_ne_produit_rien", ok,
        f"sain={d.sain} symptomes={len(d.symptomes)}")
    assert ok


def test_statut_jamais_rencontre_se_voit() -> None:
    """Un statut ABSENT de la table doit se voir — le taire laisse l'usine mourir.

    Ce test dit l'inverse du précédent, et la distinction est le sujet : « le mod ne sait
    pas interpréter » (`other`) n'est pas « nous ne connaissons pas ce statut ».

    Mesuré : `no_minable_resources` ne figurait pas dans la table. Le foreur avait vidé
    ses tuiles — 23 unités sous l'emprise, 312 000 à quelques pas dans le même gisement —
    et le diagnostic rendait ZÉRO cause. L'agent a décidé « rien, tout va bien » pendant
    60 tours, production strictement plate. Une panne qu'on ne sait pas nommer doit être
    portée devant l'agent, pas effacée : gravité 1, et l'Enquêteur pourra s'en saisir.
    """
    d = diagnose([_m("assembling-machine-1", 5, 0, "statut-inexistant")])
    causes = d.causes
    ok = (len(causes) == 1 and causes[0].cause == "inconnu"
          and causes[0].gravite == 1 and "statut-inexistant" in causes[0].detail)
    rec("test_statut_jamais_rencontre_se_voit", ok,
        f"{[(s.cause, s.gravite, s.racine) for s in d.symptomes]}")
    assert ok


def test_gisement_epuise_est_une_cause_propre() -> None:
    """Le foreur à sec affame tout l'aval : c'est LUI la cause, pas les machines à jeun.

    Sans cela, l'agent traiterait le four « entrée vide » — c'est-à-dire la conséquence —
    et rebâtirait une alimentation vers une foreuse qui n'a plus rien à extraire.
    """
    d = diagnose([_m("electric-mining-drill", 0, 0, "no_minable_resources"),
                  _m("electric-furnace", 3, 0, "no_ingredients")])
    racines = [s for s in d.causes]
    ok = (len(racines) == 1 and racines[0].cause == "gisement_epuise"
          and racines[0].gravite == 2)
    rec("test_gisement_epuise_est_une_cause_propre", ok,
        f"{[(s.name, s.cause, s.racine) for s in d.symptomes]}")
    assert ok


def test_gravite_ordonne_le_diagnostic() -> None:
    """Les causes arrêtées passent avant les ralentissements, et avant les conséquences."""
    d = diagnose([_m("electric-furnace", 0, 0, "full_output"),          # ralenti
                  _m("electric-mining-drill", 0, 5, "no_fuel"),          # arrêté, propre
                  _m("inserter", 0, 9, "waiting_for_source_items")])     # conséquence
    ordre = [(s.cause, s.racine, s.gravite) for s in d.symptomes]
    ok = (ordre[0][0] == "sans_combustible" and ordre[0][2] == 2
          and ordre[-1][1] is False)
    rec("test_gravite_ordonne_le_diagnostic", ok, f"{ordre}")
    assert ok


def test_resume_lisible() -> None:
    d = diagnose([_m("electric-mining-drill", -18.5, -95.5, "no_power")])
    txt = d.resume()
    ok = "1/1" in txt and "sans_courant" in txt and "CAUSE" in txt
    rec("test_resume_lisible", ok, txt)
    assert ok


def main() -> int:
    tests = [
        test_usine_saine_aucun_symptome,
        test_organe_de_transit_nest_pas_une_cause,
        test_debranchee_vs_sans_courant,
        test_cause_distinguee_du_symptome,
        test_entree_vide_seule_reste_une_cause,
        test_statut_non_interprete_ne_produit_rien,
        test_statut_jamais_rencontre_se_voit,
        test_gisement_epuise_est_une_cause_propre,
        test_gravite_ordonne_le_diagnostic,
        test_resume_lisible,
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