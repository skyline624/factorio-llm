"""Tests de l'arbitrage LLM — sans réseau, sans clé, sans modèle.

Le client est injecté : ce qu'on teste est le CONTRAT, pas un fournisseur.

Ce qui compte ici n'est pas qu'un modèle réponde bien — on ne le contrôle pas — mais
que l'agent survive à toutes ses façons de mal répondre. Un arbitrage distant est un
service qui tombe, renvoie du bruit, ou hallucine un indice ; à chaque fois, la boucle
doit continuer avec la décision du moteur.

Lancement :
    cd python
    python -m tests.test_arbitre
"""

from __future__ import annotations

import sys

from agents.coordinator import Decision
from services.arbitre import ArbitreOmbre, LLMArbitre, resumer_etat, resumer_options

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:100]}")


class _Cfg:
    openai_model = "modele-test"
    openai_base_url = "http://local"
    openai_api_key = "x"
    llm_enabled = True
    llm_timeout = 5
    llm_max_tokens = 128


class _Appel:
    def __init__(self, args):
        self.function = type("F", (), {"arguments": args})()


class _Reponse:
    def __init__(self, appels):
        msg = type("M", (), {"tool_calls": appels})()
        self.choices = [type("C", (), {"message": msg})()]


class _Client:
    """Client OpenAI-compatible factice : rend ce qu'on lui dit, ou lève."""

    def __init__(self, reponse=None, erreur=None):
        self.reponse, self.erreur = reponse, erreur
        self.appels = 0
        self.dernier_prompt = ""

        arbitre = self

        class _Completions:
            def create(self, **kw):
                arbitre.appels += 1
                arbitre.dernier_prompt = kw["messages"][-1]["content"]
                if arbitre.erreur:
                    raise arbitre.erreur
                return arbitre.reponse

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _options():
    return [Decision("ravitailler", "four à sec", 3),
            Decision("defendre", "menace imminente", 2),
            Decision("batir_production", "aucune machine", 1)]


class _Etat:
    machines = 3
    reseau = 7
    production_kw = 900.0
    diagnostic = None
    menace = None
    inventaire = {"coal": 42, "gun-turret": 8, "bois-inutile": 3}


def _arbitre(reponse=None, erreur=None):
    return LLMArbitre(cfg=_Cfg(), client=_Client(reponse, erreur))


def test_choix_valide_est_suivi() -> None:
    a = _arbitre(_Reponse([_Appel('{"indice": 1, "raison": "les biters arrivent"}')]))
    i = a(_Etat(), _options())
    ok = i == 1 and any("choix [1] defendre" in j for j in a.journal)
    rec("test_choix_valide_est_suivi", ok, f"indice={i} | {a.journal[-1] if a.journal else ''}")
    assert ok


def test_toutes_les_defaillances_replient_sur_zero() -> None:
    """Le point central : aucune façon de mal répondre ne doit arrêter l'agent."""
    cas = {
        "indice hors bornes": _arbitre(_Reponse([_Appel('{"indice": 9, "raison": "x"}')])),
        "indice négatif": _arbitre(_Reponse([_Appel('{"indice": -2, "raison": "x"}')])),
        "indice booléen": _arbitre(_Reponse([_Appel('{"indice": true, "raison": "x"}')])),
        "indice texte": _arbitre(_Reponse([_Appel('{"indice": "defendre"}')])),
        "JSON cassé": _arbitre(_Reponse([_Appel('{indice: 1,,}')])),
        "aucun tool_call": _arbitre(_Reponse(None)),
        "réponse vide": _arbitre(_Reponse([])),
        "modèle injoignable": _arbitre(erreur=TimeoutError("délai dépassé")),
        "client absent": LLMArbitre(cfg=_Cfg(), client=None),
    }
    mauvais = [nom for nom, a in cas.items() if a(_Etat(), _options()) != 0]
    # Chaque repli doit être motivé, sinon il est indébogable.
    muets = [nom for nom, a in cas.items() if not a.journal]
    rec("test_toutes_les_defaillances_replient_sur_zero", not mauvais and not muets,
        f"replis manqués={mauvais or 'aucun'} ; sans motif={muets or 'aucun'}")
    assert not mauvais and not muets, (mauvais, muets)


def test_aucune_option_ne_consulte_pas_le_modele() -> None:
    """Rien à choisir : on ne paie pas un aller-retour."""
    c = _Client(_Reponse([_Appel('{"indice": 0, "raison": "x"}')]))
    a = LLMArbitre(cfg=_Cfg(), client=c)
    i = a(_Etat(), [])
    ok = i == 0 and c.appels == 0
    rec("test_aucune_option_ne_consulte_pas_le_modele", ok, f"{c.appels} appel(s)")
    assert ok


def test_le_prompt_contient_letat_et_les_options_numerotees() -> None:
    """Le modèle doit voir de quoi choisir — et rien de plus.

    Il reçoit le résumé déterministe, pas l'état brut du jeu : c'est ce qui rend le
    prompt court et le raisonnement possible.
    """
    c = _Client(_Reponse([_Appel('{"indice": 0, "raison": "x"}')]))
    LLMArbitre(cfg=_Cfg(), client=c)(_Etat(), _options())
    p = c.dernier_prompt
    ok = ("[0] ravitailler" in p and "[1] defendre" in p and "[2] batir_production" in p
          and "machines en service : 3" in p and "coal=42" in p
          # Ce qui n'aide pas à choisir n'a rien à faire dans le prompt.
          and "bois-inutile" not in p)
    rec("test_le_prompt_contient_letat_et_les_options_numerotees", ok,
        f"{len(p)} caractères, options numérotées={'[1] defendre' in p}")
    assert ok


def test_mode_ombre_ne_laisse_jamais_la_main() -> None:
    """Le déterministe décide, le modèle n'est qu'observé — c'est tout l'intérêt."""
    ombre = ArbitreOmbre(_arbitre(_Reponse([_Appel('{"indice": 2, "raison": "je préfère"}')])))
    choix = [ombre(_Etat(), _options()) for _ in range(3)]
    ok = (choix == [0, 0, 0] and len(ombre.divergences) == 3
          and "aurait choisi [2]" in ombre.divergences[0]
          and ombre.taux_divergence == 1.0)
    rec("test_mode_ombre_ne_laisse_jamais_la_main", ok,
        f"choix={choix} divergences={len(ombre.divergences)} "
        f"taux={ombre.taux_divergence:.0%}")
    assert ok


def test_mode_ombre_compte_les_accords() -> None:
    """Quand le modèle est d'accord avec le moteur, ce n'est pas une divergence."""
    ombre = ArbitreOmbre(_arbitre(_Reponse([_Appel('{"indice": 0, "raison": "d accord"}')])))
    for _ in range(4):
        ombre(_Etat(), _options())
    ok = ombre.accords == 4 and not ombre.divergences and ombre.taux_divergence == 0.0
    rec("test_mode_ombre_compte_les_accords", ok,
        f"{ombre.accords} accord(s), {len(ombre.divergences)} divergence(s)")
    assert ok


def test_mode_ombre_survit_a_un_arbitre_qui_explose() -> None:
    """Même un arbitre qui lève ne doit pas interrompre la boucle."""
    class _Explose:
        def __call__(self, etat, options):
            raise RuntimeError("boum")

    ombre = ArbitreOmbre(_Explose())
    i = ombre(_Etat(), _options())
    ok = i == 0 and any("en erreur" in d for d in ombre.divergences)
    rec("test_mode_ombre_survit_a_un_arbitre_qui_explose", ok, f"{ombre.divergences}")
    assert ok


def test_resumes_lisibles() -> None:
    ok = ("[1] defendre" in resumer_options(_options())
          and "machines en service" in resumer_etat(_Etat()))
    rec("test_resumes_lisibles", ok, resumer_etat(_Etat()).replace("\n", " | ")[:90])
    assert ok


def main() -> int:
    tests = [
        test_choix_valide_est_suivi,
        test_toutes_les_defaillances_replient_sur_zero,
        test_aucune_option_ne_consulte_pas_le_modele,
        test_le_prompt_contient_letat_et_les_options_numerotees,
        test_mode_ombre_ne_laisse_jamais_la_main,
        test_mode_ombre_compte_les_accords,
        test_mode_ombre_survit_a_un_arbitre_qui_explose,
        test_resumes_lisibles,
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