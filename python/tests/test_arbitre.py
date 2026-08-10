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
          # CE TEST EXIGEAIT L'INVERSE, et c'était le défaut : il vérifiait que
          # « bois-inutile » soit ABSENT, c'est-à-dire que le résumé filtre l'inventaire
          # sur une liste de noms écrite d'avance. Mesuré en jeu, cette liste a caché au
          # modèle les dix flacons qui payaient précisément la recherche qu'on lui
          # proposait. On ne décide pas à sa place de ce qui « aide à choisir » : il voit
          # ce qu'il a, et trie lui-même.
          and "bois-inutile=3" in p)
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
    """Même un arbitre qui lève ne doit pas interrompre la boucle.

    L'incident est consigné à part : une exception n'est PAS un désaccord. La ranger
    parmi les divergences gonflerait le seul chiffre qui doive rester propre — celui sur
    lequel on décidera si le modèle apporte quelque chose.
    """
    class _Explose:
        def __call__(self, etat, options):
            raise RuntimeError("boum")

    ombre = ArbitreOmbre(_Explose())
    i = ombre(_Etat(), _options())
    ok = (i == 0 and not ombre.divergences and ombre.replis == 1
          and any("en erreur" in x for x in ombre.incidents))
    rec("test_mode_ombre_survit_a_un_arbitre_qui_explose", ok,
        f"replis={ombre.replis} divergences={len(ombre.divergences)} "
        f"incidents={ombre.incidents}")
    assert ok


def test_un_repli_nest_pas_un_accord() -> None:
    """LA correction : un modèle qui n'a rien dit ne « valide » pas le déterministe.

    Le repli rend 0, ce qui est la bonne conduite — c'est la décision du moteur seul.
    Mais 0 « par défaut » et 0 « après réflexion » sont des faits opposés, et un seul
    entier les confondait : modèle injoignable, réponse illisible ou outil non appelé se
    lisaient tous « le modèle est d'accord », c'est-à-dire la conclusion la plus
    flatteuse de tout ce qui rate.
    """
    # Une réponse sans appel d'outil : le modèle n'a pas répondu dans le format demandé.
    ombre = ArbitreOmbre(_arbitre(_Reponse([])))
    for _ in range(3):
        ombre(_Etat(), _options())
    ok = (ombre.replis == 3 and ombre.accords == 0 and not ombre.divergences)
    rec("test_un_repli_nest_pas_un_accord", ok,
        f"replis={ombre.replis} accords={ombre.accords} "
        f"divergences={len(ombre.divergences)}")
    assert ok


def test_le_taux_ne_compte_que_les_tours_prononces() -> None:
    """Un modèle absent afficherait sinon 0 % — le chiffre d'un modèle parfaitement d'accord."""
    muet = ArbitreOmbre(_arbitre(_Reponse([])))
    for _ in range(5):
        muet(_Etat(), _options())
    parle = ArbitreOmbre(_arbitre(_Reponse([_Appel('{"indice": 2, "raison": "mieux"}')])))
    for _ in range(5):
        parle(_Etat(), _options())
    ok = (muet.taux_divergence == 0.0 and muet.accords + len(muet.divergences) == 0
          and parle.taux_divergence == 1.0)
    rec("test_le_taux_ne_compte_que_les_tours_prononces", ok,
        f"muet : {muet.replis} repli(s), 0 prononcé | parlant : "
        f"taux {parle.taux_divergence:.0%}")
    assert ok


def test_resumes_lisibles() -> None:
    ok = ("[1] defendre" in resumer_options(_options())
          and "machines en service" in resumer_etat(_Etat()))
    rec("test_resumes_lisibles", ok, resumer_etat(_Etat()).replace("\n", " | ")[:90])
    assert ok


def test_le_resume_ne_cache_rien_de_ce_qui_est_en_poche() -> None:
    """UNE LISTE EN DUR DÉCIDE DE CE QUE LE MODÈLE PEUT SAVOIR.

    Relevé en jeu, dernier rush : l'option proposée était « chercher automation — coût
    10 × automation-science-pack », l'agent en avait EXACTEMENT dix en poche, et son
    inventaire affiché s'arrêtait à `coal=37`. Le modèle ne pouvait pas savoir qu'il
    tenait déjà de quoi payer.

    La cause était six mots écrits il y a des semaines :

        utiles = ("coal", "gun-turret", "firearm-magazine", "iron-plate",
                  "small-electric-pole", "electric-mining-drill")

    Quand le modèle « choisit mal » dans ces conditions, on ne mesure pas son jugement :
    on mesure cette liste. C'est aussi ce qui rendait l'A/B de ce matin muette (+2 % en
    médiane, dans le bruit).

    La règle : **ce qu'une option met en jeu doit figurer dans l'état.** On montre donc
    l'inventaire tel qu'il est. Tronquer par QUANTITÉ reste permis — un écran a ses
    limites — mais jamais par une liste de noms choisie d'avance.
    """
    class _EtatRiche:
        machines = 3
        reseau = None
        production_kw = 0.0
        diagnostic = None
        menace = None
        # Le cas réel : les flacons qui paient la recherche, et le laboratoire.
        inventaire = {"coal": 37, "automation-science-pack": 10, "lab": 1,
                      "copper-plate": 3, "iron-gear-wheel": 24}

    texte = resumer_etat(_EtatRiche())
    montres = [n for n in _EtatRiche.inventaire if n in texte]
    ok_inv = set(montres) == set(_EtatRiche.inventaire)

    # Et le module lui-même ne doit plus porter de liste d'items en dur.
    import inspect as _inspect

    import services.arbitre as _arb
    src = _inspect.getsource(_arb)
    en_dur = [n for n in ("gun-turret", "firearm-magazine", "small-electric-pole",
                          "electric-mining-drill") if f'"{n}"' in src or f"'{n}'" in src]

    ok = ok_inv and not en_dur
    rec("test_le_resume_ne_cache_rien_de_ce_qui_est_en_poche", ok,
        f"{len(montres)}/{len(_EtatRiche.inventaire)} item(s) visible(s) ; "
        f"noms en dur restants : {en_dur or 'aucun'}")
    assert ok


class _ApiSonde:
    """Une API de lecture, qui note ce qu'on lui demande."""

    def __init__(self):
        self.appels: list = []

    def inspect_at(self, x, y, radius=0.5):
        self.appels.append(("inspect_at", round(x), round(y)))
        return {"entities": [{"name": "stone-furnace", "status": "full_output",
                              "x": x, "y": y}]}

    def get_power_state(self, x, y, radius=4.0):
        self.appels.append(("get_power_state", round(x), round(y)))
        return {"networkId": None, "connected": False}

    def mine_entity(self, x, y):                      # DESTRUCTIF : hors liste blanche
        self.appels.append(("mine_entity", round(x), round(y)))
        return {"ok": True}


class _ClientOutille:
    """Demande d'abord une mesure, puis choisit — comme le ferait un modèle outillé."""

    def __init__(self, scenario):
        self.scenario, self.tour, self.vus = scenario, 0, []
        client = self

        class _Completions:
            def create(self, **kw):
                client.vus.append(kw)
                r = client.scenario[min(client.tour, len(client.scenario) - 1)]
                client.tour += 1
                return r

        self.chat = type("Chat", (), {"completions": _Completions()})()


def _appel(nom, args, ident="c1"):
    f = type("F", (), {"name": nom, "arguments": args})()
    return type("A", (), {"id": ident, "type": "function", "function": f})()


def test_larbitre_peut_mesurer_avant_de_choisir() -> None:
    """L'AGENT QUI CONSTATE A SIX SONDES ; CELUI QUI DÉCIDE N'EN AVAIT AUCUNE.

    Relevé : `Enqueteur` expose `inspect_at`, `get_power_state`, `can_place_check`,
    `get_tile`, `scan_patch`, `suivre_flux` — et trouve cinq pannes sur six sans fausse
    piste. `LLMArbitre` n'exposait que `choisir` : il recevait un résumé poussé par le
    code et ne pouvait rien regarder. Impossible de lui demander « qu'y a-t-il dans ce
    four ? » avant de trancher entre le vider et bâtir autre chose.

    Le patron de l'enquêteur est repris tel quel — liste blanche, arguments filtrés,
    sortie tronquée — et les sondes restent en LECTURE SEULE : un arbitre qui minerait
    des entités ne serait plus un arbitre.
    """
    api = _ApiSonde()
    scenario = [
        _Reponse([_appel("inspect_at", '{"x": 10, "y": 4}')]),
        _Reponse([_appel("choisir", '{"indice": 1, "raison": "le four est plein"}')]),
    ]
    c = _ClientOutille(scenario)
    a = LLMArbitre(cfg=_Cfg(), client=c)
    i = a(_Etat(), _options(), api=api)

    mesure_faite = ("inspect_at", 10, 4) in api.appels
    resultat_rendu = any(
        any(m.get("role") == "tool" for m in kw.get("messages", []))
        for kw in c.vus)
    ok = i == 1 and mesure_faite and resultat_rendu and c.tour == 2
    rec("test_larbitre_peut_mesurer_avant_de_choisir", ok,
        f"mesures={api.appels} — résultat renvoyé au modèle={resultat_rendu} — indice={i}")
    assert ok


def test_un_outil_hors_liste_blanche_est_refuse_sans_etre_execute() -> None:
    """Une sonde est une LECTURE. Miner, poser, régler une recette n'en sont pas.

    Le modèle propose un nom d'outil ; rien ne garantit qu'il s'en tienne à ceux qu'on
    lui a décrits. Le refus doit précéder l'exécution — pas la constater après coup.
    """
    api = _ApiSonde()
    scenario = [
        _Reponse([_appel("mine_entity", '{"x": 3, "y": 3}')]),
        _Reponse([_appel("choisir", '{"indice": 0, "raison": "faute de mieux"}')]),
    ]
    a = LLMArbitre(cfg=_Cfg(), client=_ClientOutille(scenario))
    i = a(_Etat(), _options(), api=api)
    ok = i == 0 and api.appels == [] and any("refus" in j.lower() for j in a.journal)
    rec("test_un_outil_hors_liste_blanche_est_refuse_sans_etre_execute", ok,
        f"appels sur l'API={api.appels} (attendu aucun) — journal={a.journal[-1:]}")
    assert ok


def test_les_mesures_sont_bornees_et_le_repli_tient() -> None:
    """Un modèle qui mesure sans fin bloquerait la boucle.

    Le budget épuisé n'est pas une panne : on retombe sur `options[0]`, la décision du
    moteur seul — la règle « toute défaillance rend 0 » vaut aussi ici.
    """
    api = _ApiSonde()
    boucle = [_Reponse([_appel("inspect_at", '{"x": 1, "y": 1}')])] * 20
    a = LLMArbitre(cfg=_Cfg(), client=_ClientOutille(boucle), budget=3)
    i = a(_Etat(), _options(), api=api)
    ok = i == 0 and len(api.appels) <= 3 and a.replis == 1
    rec("test_les_mesures_sont_bornees_et_le_repli_tient", ok,
        f"{len(api.appels)} mesure(s) pour un budget de 3 — indice={i} replis={a.replis}")
    assert ok


def test_l_etat_donne_les_deux_couts_et_ne_conclut_pas() -> None:
    """UNE INFORMATION VRAIE PEUT ÊTRE TROMPEUSE SI ELLE EST SEULE.

    Partie 32, ce que l'agent lit avant de choisir :

        pour bâtir une chaîne il faut : burner-mining-drill×1, stone-furnace×1,
                                        burner-inserter×1, coal×20
        inventaire : burner-mining-drill=1, stone-furnace=1, wood=1

    Rien de faux : c'est bien le coût de `batir_une_chaine`. Mais c'est le SEUL coût
    affiché, et il conclut à sa place — il manque un bras et vingt charbons, donc rien
    n'est possible tout de suite. Or depuis que le four se pose sur la tuile de drop, une
    extraction ne demande ni bras ni combustible : il tenait exactement de quoi produire.

    On donne donc les deux coûts, et l'on s'arrête là. Ce qui est mesuré s'écrit ; ce qu'on
    en tire appartient à l'agent — même règle que pour les plafonds de fabrication et le
    nombre d'alimentations, retirés à la demande de l'utilisateur.
    """
    from services.arbitre import resumer_etat

    class _Etat:
        machines = 0
        besoins_production = (("burner-mining-drill", 1), ("stone-furnace", 1),
                              ("burner-inserter", 1), ("coal", 20))
        inventaire = {"burner-mining-drill": 1, "stone-furnace": 1, "wood": 1}

    texte = resumer_etat(_Etat())

    dit_l_usine = "burner-inserter" in texte
    dit_l_extraction = "extraction" in texte.lower()
    ne_conclut_pas = "tu dois" not in texte.lower() and "commence par" not in texte.lower()

    ok = dit_l_usine and dit_l_extraction and ne_conclut_pas
    rec("test_l_etat_donne_les_deux_couts_et_ne_conclut_pas", ok,
        f"usine={dit_l_usine} extraction={dit_l_extraction} neutre={ne_conclut_pas}")
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
        test_un_repli_nest_pas_un_accord,
        test_le_taux_ne_compte_que_les_tours_prononces,
        test_resumes_lisibles,
        test_le_resume_ne_cache_rien_de_ce_qui_est_en_poche,
        test_larbitre_peut_mesurer_avant_de_choisir,
        test_un_outil_hors_liste_blanche_est_refuse_sans_etre_execute,
        test_les_mesures_sont_bornees_et_le_repli_tient,
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