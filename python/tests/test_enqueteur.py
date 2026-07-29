"""Tests unitaires de l'Enquêteur — les garde-fous, sans réseau ni modèle.

Ce qui est éprouvé ici n'est pas la finesse du diagnostic (elle se mesure en jeu, sur
des pannes dont on connaît la réponse) mais le CONTRAT : ce que le composant fait des
réponses aberrantes. Un enquêteur qui rend une cause plausible et fausse est pire
qu'inutile — il déclencherait une réparation sur un problème qui n'existe pas.

Quatre garanties :
  - un outil hors liste blanche n'est jamais exécuté ;
  - une cause hors vocabulaire est ramenée à `inconnu` ;
  - une conclusion sans preuve est ramenée à `inconnu` (une mesure, pas une opinion) ;
  - un modèle injoignable ou muet rend `inconnu`, jamais une exception.

Lancement :
    cd python
    python -m tests.test_enqueteur
"""

from __future__ import annotations

import json
import sys

from agents.enqueteur import CAUSES, Constat, Enqueteur, tronquer

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:50s} {detail[:100]}")


class _Appel:
    def __init__(self, nom, args):
        self.function = type("F", (), {"name": nom, "arguments": json.dumps(args)})()


class _Message:
    def __init__(self, appels):
        self.tool_calls = [_Appel(n, a) for n, a in appels] or None


class _Reponse:
    def __init__(self, appels):
        self.choices = [type("C", (), {"message": _Message(appels)})()]


class FauxClient:
    """Rejoue une suite de réponses scriptées, et retient ce qu'on lui a envoyé."""

    def __init__(self, tours):
        self.tours = list(tours)
        self.envois: list[list] = []
        self.chat = type("Chat", (), {"completions": self})()

    def create(self, **kwargs):
        self.envois.append(kwargs.get("messages", []))
        if not self.tours:
            return _Reponse([])
        return _Reponse(self.tours.pop(0))


class FauxApi:
    """Un api qui compte ses lectures et n'a AUCUNE méthode d'action."""

    def __init__(self):
        self.lectures: list[str] = []

    def inspect_at(self, x, y, radius=0.5):
        self.lectures.append(f"inspect_at({x},{y})")
        return {"entities": [{"name": "boiler", "x": x, "y": y, "status": "no_fuel"}]}

    def get_power_state(self, x, y, radius=4.0):
        self.lectures.append("get_power_state")
        return {"networkId": None}

    def remove_entity_at(self, x, y, name=None):        # piège : ne doit JAMAIS être appelé
        self.lectures.append("REMOVE")
        return {"ok": True}


class _Ecart:
    action = "ravitailler"
    attendu = "le boiler n'est plus à sec"
    observe = "no_fuel"
    cible = None


def _enq(tours, budget=None):
    return Enqueteur(cfg=type("C", (), {"openai_model": "test", "llm_max_tokens": 256})(),
                     client=FauxClient(tours), budget=budget)


def test_enquete_mesure_puis_conclut() -> None:
    """Le cas nominal : une mesure, puis une conclusion fondée dessus."""
    api = FauxApi()
    c = _enq([[("inspect_at", {"x": 3.0, "y": 4.0})],
              [("conclure", {"cause": "combustible_epuise",
                             "preuve": "boiler@(3,4) status=no_fuel", "x": 3.0, "y": 4.0})]])
    r = c(api, _Ecart())
    ok = (r.cause == "combustible_epuise" and r.outils_appeles == 1
          and r.position == (3.0, 4.0) and api.lectures == ["inspect_at(3.0,4.0)"])
    rec("test_enquete_mesure_puis_conclut", ok, str(r))
    assert ok, str(r)


def test_outil_hors_liste_blanche_refuse() -> None:
    """Un outil d'ACTION demandé par le modèle ne doit pas être exécuté.

    C'est la garantie qui autorise à laisser un modèle choisir : le pire coût d'une
    enquête est du temps, jamais une modification de la carte.
    """
    api = FauxApi()
    c = _enq([[("remove_entity_at", {"x": 1.0, "y": 1.0})],
              [("conclure", {"cause": "inconnu", "preuve": ""})]])
    r = c(api, _Ecart())
    ok = "REMOVE" not in api.lectures and r.cause == "inconnu"
    rec("test_outil_hors_liste_blanche_refuse", ok,
        f"lectures={api.lectures}, cause={r.cause}")
    assert ok


def test_cause_hors_vocabulaire_devient_inconnu() -> None:
    """Une cause inventée n'est pas exploitable par la boucle : on la refuse."""
    r = _enq([[("conclure", {"cause": "les_biters_ont_tout_mange",
                             "preuve": "j'en suis convaincu"})]])(FauxApi(), _Ecart())
    ok = r.cause == "inconnu" and "hors vocabulaire" in r.preuve
    rec("test_cause_hors_vocabulaire_devient_inconnu", ok, str(r))
    assert ok, str(r)


def test_conclusion_sans_preuve_refusee() -> None:
    """Sans mesure à l'appui, une cause est une opinion — et déclencherait une réparation.

    C'est le garde-fou le plus important : un modèle produit volontiers une explication
    cohérente et fausse. Exiger la valeur LUE est ce qui fait la différence.
    """
    r = _enq([[("conclure", {"cause": "belt_interrompue", "preuve": "   "})]])(
        FauxApi(), _Ecart())
    ok = r.cause == "inconnu" and "sans aucune mesure" in r.preuve
    rec("test_conclusion_sans_preuve_refusee", ok, str(r))
    assert ok, str(r)


def test_budget_borne_les_mesures() -> None:
    """Un modèle qui mesure sans fin doit rendre la main."""
    api = FauxApi()
    c = _enq([[("inspect_at", {"x": 1.0, "y": 1.0})]] * 10, budget=3)
    r = c(api, _Ecart())
    ok = r.cause == "inconnu" and r.outils_appeles <= 4 and len(api.lectures) <= 4
    rec("test_budget_borne_les_mesures", ok,
        f"{r.outils_appeles} mesure(s) pour un budget de 3")
    assert ok, str(r)


def test_modele_absent_rend_inconnu() -> None:
    """Sans modèle, la boucle continue de tourner : elle ne sait simplement pas.

    La configuration est fournie EXPLICITEMENT avec `llm_enabled=False`. Écrire
    `cfg=None` chargerait la vraie configuration et interrogerait Ollama s'il tourne :
    le test passerait pour de mauvaises raisons, en éprouvant le cas inverse de celui
    qu'il annonce.
    """
    sans_llm = type("C", (), {"openai_base_url": "", "llm_enabled": False,
                              "openai_model": "test"})()
    enq = Enqueteur(cfg=sans_llm, client=None)
    r = enq(FauxApi(), _Ecart())
    ok = (isinstance(r, Constat) and r.cause == "inconnu" and not r.concluant
          and r.outils_appeles == 0 and "aucun modèle" in r.preuve)
    rec("test_modele_absent_rend_inconnu", ok, str(r))
    assert ok, str(r)


def test_modele_muet_rend_inconnu() -> None:
    """Une réponse sans appel d'outil ne doit pas lever d'exception."""
    r = _enq([[]])(FauxApi(), _Ecart())
    ok = r.cause == "inconnu"
    rec("test_modele_muet_rend_inconnu", ok, str(r))
    assert ok, str(r)


def test_reponses_tronquees_avant_envoi() -> None:
    """Deux cents entités noieraient le signal et coûteraient cher pour rien."""
    gros = {"entities": [{"name": f"e{i}", "x": i, "y": 0} for i in range(200)]}
    court = tronquer(gros)
    ok = len(court) <= 1500 and "entities_tronquees" in court
    rec("test_reponses_tronquees_avant_envoi", ok,
        f"{len(court)} caractères envoyés pour 200 entités")
    assert ok


def test_vocabulaire_ferme_et_documente() -> None:
    """Chaque cause doit porter une explication : c'est elle que le modèle lit."""
    muettes = [k for k, v in CAUSES.items() if not v.strip()]
    ok = "inconnu" in CAUSES and not muettes
    rec("test_vocabulaire_ferme_et_documente", ok,
        f"{len(CAUSES)} causes, {len(muettes)} sans explication")
    assert ok


def main() -> int:
    for t in (test_enquete_mesure_puis_conclut, test_outil_hors_liste_blanche_refuse,
              test_cause_hors_vocabulaire_devient_inconnu,
              test_conclusion_sans_preuve_refusee, test_budget_borne_les_mesures,
              test_modele_absent_rend_inconnu, test_modele_muet_rend_inconnu,
              test_reponses_tronquees_avant_envoi, test_vocabulaire_ferme_et_documente):
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