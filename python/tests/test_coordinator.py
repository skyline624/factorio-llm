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

import math
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


def test_ravitaillement_repete_devient_automatisation() -> None:
    """Remplir deux fois le même boiler, c'est une réparation ; trois, c'est un aveu.

    Un boiler brûle 0.45 charbon/s : moins de deux minutes d'autonomie par plein. Un
    agent qui se contente de remplir y passe sa vie et ne construit plus rien. Au-delà
    du seuil, la décision bascule de « ravitailler » à « approvisionner » — bâtir la
    chaîne qui manque.
    """
    from agents.coordinator import SEUIL_AUTOMATISATION
    rows = [_m("boiler", 10, 20, "no_fuel")]
    premiere = decide(_etat(rows))
    etat = _etat(rows)
    etat.ravitaillements = {("boiler", 10, 20): SEUIL_AUTOMATISATION}
    apres = decide(etat)
    ok = (premiere.action == "ravitailler" and apres.action == "approvisionner"
          and "chaîne" in apres.raison)
    rec("test_ravitaillement_repete_devient_automatisation", ok,
        f"1er passage={premiere.action} -> après {SEUIL_AUTOMATISATION} remplissages="
        f"{apres.action}")
    assert ok


def test_le_compteur_est_par_machine() -> None:
    """Avoir rempli un boiler ne dit rien d'un autre : le compteur suit la POSITION."""
    etat = _etat([_m("boiler", 10, 20, "no_fuel"), _m("boiler", 90, 90, "no_fuel")])
    etat.ravitaillements = {("boiler", 10, 20): 5}     # seul le premier est concerné
    from agents.coordinator import enumerer_options
    actions = {(o.cible.x, o.cible.y): o.action for o in enumerer_options(etat)
               if o.cible is not None}
    ok = actions.get((10, 20)) == "approvisionner" and actions.get((90, 90)) == "ravitailler"
    rec("test_le_compteur_est_par_machine", ok, f"{actions}")
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
    fabrique = next((o for o in options if o.action == "fabriquer"), None)
    ok = (defense is not None and not defense.faisable and defense.priorite == 0
          and "INFAISABLE" in defense.raison and "gun-turret" in defense.raison
          # Et le manque APPELLE désormais une fabrication, au rang de l'action qu'elle
          # débloque. C'est ce que ce test annonçait depuis le début — « il faudra un
          # jour décider d'aller fabriquer ce qui manque » : déclarer un besoin sans
          # pouvoir y répondre, c'était attendre qu'un humain remplisse les poches.
          and fabrique is not None and "gun-turret" in fabrique.raison
          and options[0].action == "fabriquer")
    rec("test_option_sans_materiel_est_declassee", ok,
        f"{[(o.action, o.priorite, o.faisable) for o in options]}")
    assert ok


def test_un_manque_appelle_une_fabrication() -> None:
    """Déclarer un besoin ne suffit pas : encore faut-il pouvoir y répondre.

    L'agent consommait une dotation de 21 lots posée par un humain et s'arrêtait quand
    elle était vide — il ne savait pas produire une foreuse de plus. « Autonome » restait
    un mot tant que le manque ne déclenchait rien.
    """
    from agents.coordinator import SEUIL_AUTOMATISATION, enumerer_options
    # Une sortie vidée deux fois appelle `batir_evacuation`, qui réclame un bras et un
    # coffre. Sans eux, la construction est déclassée — et doit désormais appeler leur
    # fabrication.
    etat = _etat([_m("electric-furnace", 10, 20, "full_output")], machines=2,
                 inventaire={"coal": 100})              # ni inserter, ni coffre
    etat.evacuations = {("electric-furnace", 10, 20): SEUIL_AUTOMATISATION}
    options = enumerer_options(etat)
    fab = [o for o in options if o.action == "fabriquer"]
    ok = len(fab) == 1 and "inserter" in fab[0].raison
    rec("test_un_manque_appelle_une_fabrication", ok,
        f"{[(o.action, o.raison.split(' ')[0]) for o in options]}")
    assert ok


def test_la_fabrication_herite_de_l_urgence_qu_elle_debloque() -> None:
    """Une pièce qui manque pour une RÉPARATION est urgente comme une réparation.

    Un rang fixe pour `fabriquer` doublerait tout le curriculum d'un cran : l'agent
    fabriquerait une tourelle avant de rallumer un four à l'arrêt.
    """
    from agents.coordinator import PRIORITE, SEUIL_AUTOMATISATION, enumerer_options
    etat = _etat([_m("electric-furnace", 10, 20, "full_output")], machines=2,
                 inventaire={"coal": 100})
    etat.evacuations = {("electric-furnace", 10, 20): SEUIL_AUTOMATISATION}
    fab = next((o for o in enumerer_options(etat) if o.action == "fabriquer"), None)
    ok = fab is not None and fab.priorite == PRIORITE["reparer"]
    rec("test_la_fabrication_herite_de_l_urgence_qu_elle_debloque", ok,
        f"priorite={fab.priorite if fab else None} (reparer={PRIORITE['reparer']})")
    assert ok


def test_rien_a_fabriquer_quand_l_inventaire_suffit() -> None:
    """Le pendant : avec le matériel, aucune fabrication n'est proposée."""
    from agents.coordinator import SEUIL_AUTOMATISATION, enumerer_options
    etat = _etat([_m("electric-furnace", 10, 20, "full_output")], machines=2,
                 inventaire=OUTILLE)
    etat.evacuations = {("electric-furnace", 10, 20): SEUIL_AUTOMATISATION}
    ok = not any(o.action == "fabriquer" for o in enumerer_options(etat))
    rec("test_rien_a_fabriquer_quand_l_inventaire_suffit", ok,
        f"{[o.action for o in enumerer_options(etat)]}")
    assert ok


def test_un_meme_manque_ne_se_propose_qu_une_fois() -> None:
    """Deux machines pleines ne demandent pas deux fois le même bras.

    Sans cette garde, une usine de dix machines bouchées produirait dix fabrications
    identiques, et l'agent passerait son tour à choisir entre des jumelles.
    """
    from agents.coordinator import SEUIL_AUTOMATISATION, enumerer_options
    etat = _etat([_m("electric-furnace", 10, 20, "full_output"),
                  _m("electric-furnace", 30, 40, "full_output")], machines=3,
                 inventaire={"coal": 100})
    etat.evacuations = {("electric-furnace", 10, 20): SEUIL_AUTOMATISATION,
                        ("electric-furnace", 30, 40): SEUIL_AUTOMATISATION}
    fab = [o for o in enumerer_options(etat) if o.action == "fabriquer"]
    ok = len(fab) == 1 and "inserter" in fab[0].raison
    rec("test_un_meme_manque_ne_se_propose_qu_une_fois", ok,
        f"{len(fab)} fabrication(s) pour 2 machines bouchees")
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


class _ApiJournal:
    """Une API qui note les ordres Lua reçus — de quoi vérifier la pause sans serveur."""

    def __init__(self, casse: bool = False):
        self.ordres: list[str] = []
        parent = self

        class _Rcon:
            def query_lua(self, code: str) -> str:
                if casse:
                    raise RuntimeError("rcon mort")
                parent.ordres.append("pause" if "= true" in code else "reprise")
                return "1"

        self.rcon = _Rcon()


def test_la_reflexion_ne_coute_pas_de_temps_de_jeu() -> None:
    """Le monde est figé pendant la décision, et relancé après.

    Mesuré : un appel au modèle coûte cinq secondes réelles, soit ~3000 ticks de jeu à
    ×10. Douze appels emportent le tiers d'une partie de trente minutes — donc plus
    l'agent a de dilemmes, moins il agit, et l'on compare un agent lent à un agent
    rapide plutôt que deux stratégies.
    """
    from agents.coordinator import figer_pendant
    api = _ApiJournal()
    r = figer_pendant(api, True, lambda: "décidé")
    ok = r == "décidé" and api.ordres == ["pause", "reprise"]
    rec("test_la_reflexion_ne_coute_pas_de_temps_de_jeu", ok, f"{api.ordres} -> {r}")
    assert ok


def test_le_monde_repart_meme_si_la_reflexion_echoue() -> None:
    """LA propriété critique : une exception ne doit pas laisser la partie figée."""
    from agents.coordinator import figer_pendant
    api = _ApiJournal()

    def _explose():
        raise RuntimeError("le modèle a planté")

    leve = False
    try:
        figer_pendant(api, True, _explose)
    except RuntimeError:
        leve = True
    ok = leve and api.ordres == ["pause", "reprise"]
    rec("test_le_monde_repart_meme_si_la_reflexion_echoue", ok,
        f"exception propagée={leve}, ordres={api.ordres}")
    assert ok


def test_sans_pause_ou_sans_rcon_on_reflechit_quand_meme() -> None:
    """Le protocole de mesure ne doit jamais empêcher l'agent de décider."""
    from agents.coordinator import figer_pendant
    muet = _ApiJournal()
    inactif = figer_pendant(muet, False, lambda: "ok")
    casse = figer_pendant(_ApiJournal(casse=True), True, lambda: "ok")
    absent = figer_pendant(None, True, lambda: "ok")
    ok = inactif == "ok" and casse == "ok" and absent == "ok" and muet.ordres == []
    rec("test_sans_pause_ou_sans_rcon_on_reflechit_quand_meme", ok,
        f"inactif={inactif} rcon_casse={casse} api_absente={absent}")
    assert ok


def test_sous_objectif_il_etend_au_lieu_de_ne_rien_faire() -> None:
    """Un agent qui VISE cesse de se satisfaire de « ça tourne ».

    Mesuré : « 4 machine(s) en état de marche » etait une raison de ne rien faire, et
    `rien` occupait 100 tours sur 114 pendant que la production plafonnait. Avec un
    objectif, la même usine devient insuffisante — et c'est là que des dilemmes
    apparaissent, donc qu'un arbitre a quelque chose à trancher.
    """
    etat = _etat(machines=4)
    etat.objectif, etat.debit, etat.objectif_item = 1.0, 0.4, "iron-plate"
    d = decide(etat)
    ok = d.action == "etendre_production" and "0.40" in d.raison
    rec("test_sous_objectif_il_etend_au_lieu_de_ne_rien_faire", ok,
        f"{d.action} — {d.raison[:70]}")
    assert ok


def test_une_centrale_ne_compte_pas_pour_une_usine() -> None:
    """Sinon l'agent croit produire dès qu'il a du courant, et ne bâtit jamais rien.

    Régression réelle, trouvée par la batterie de vérifications : depuis que le
    diagnostic embrasse les centrales — elles se posent au bord de l'eau, hors de la
    zone —, `machines` n'était plus jamais nul après `batir_energie`, et la condition de
    `batir_production` (zéro machine) ne pouvait plus être vraie. Mesuré en jeu :
    centrale bâtie au tour 1, puis huit tours de `rien` et d'`evacuer` sur une carte sans
    la moindre foreuse.
    """
    etat = EtatUsine(machines=2, diagnostic=diagnose([]), reseau=7, production_kw=900.0,
                     inventaire=dict(INVENTAIRE))
    etat.machines_production = 0            # deux organes de centrale, aucune usine
    d = decide(etat)
    ok = d.action == "batir_production"
    rec("test_une_centrale_ne_compte_pas_pour_une_usine", ok,
        f"{d.action} (2 machines vues, 0 qui produit)")
    assert ok


def test_sans_comptage_dedie_le_comportement_est_inchange() -> None:
    """`machines_production` non renseigné retombe sur `machines` — back-compat."""
    etat = EtatUsine(machines=3, diagnostic=diagnose([]), reseau=7, production_kw=900.0,
                     inventaire=dict(INVENTAIRE))
    d = decide(etat)
    ok = d.action == "rien" and etat.machines_production is None
    rec("test_sans_comptage_dedie_le_comportement_est_inchange", ok, f"{d.action}")
    assert ok


def test_compter_machines_rend_moins_un_si_illisible() -> None:
    """Zéro machine est un état PLAUSIBLE : le confondre avec une panne de mesure ferait
    conclure qu'une extension a échoué alors qu'on n'a rien pu lire.

    Même règle que `production_cumulee` : on distingue « il n'y en a pas » de « je ne
    sais pas ». C'est la sixième fois dans ce chantier qu'un zéro se fait passer pour une
    absence — le test est là pour que ce soit la dernière.
    """
    from services.perception import compter_machines

    class _Rcon:
        def __init__(self, r): self.r = r
        def query_lua(self, code):
            if isinstance(self.r, Exception):
                raise self.r
            return self.r

    class _Api:
        def __init__(self, r): self.rcon = _Rcon(r)

    lisible = compter_machines(_Api("7"), 0.0, 0.0, 25.0)
    vide = compter_machines(_Api("0"), 0.0, 0.0, 25.0)
    illisible = compter_machines(_Api("pas un nombre"), 0.0, 0.0, 25.0)
    mort = compter_machines(_Api(RuntimeError("rcon mort")), 0.0, 0.0, 25.0)
    ok = lisible == 7 and vide == 0 and illisible == -1 and mort == -1
    rec("test_compter_machines_rend_moins_un_si_illisible", ok,
        f"lisible={lisible} zero={vide} illisible={illisible} rcon_mort={mort}")
    assert ok


def test_une_satisfaction_basse_ne_bloque_pas_la_croissance() -> None:
    """Ce test dit l'inverse de ce que j'avais d'abord écrit, et la mesure a tranché.

    L'intuition semblait solide : ne pas agrandir une usine que le courant ne suit plus,
    puisqu'une extension de trop faisait tomber le réseau entier. Essayée à 0.95, la
    garde a fait cesser toute croissance — quarante tours, quatre machines, `rien`
    trente et une fois — parce que la satisfaction oscille sous 1.0 en régime NORMAL,
    sans que rien ne soit en panne.

    Le garde-fou existait déjà, ailleurs et mieux : un vrai manque de courant met les
    machines en `sans_courant`, ce qui est une RÉPARATION (priorité 3) et passe donc
    avant l'extension (priorité 1). Le curriculum s'en charge ; un veto de plus ne
    faisait que l'empêcher de vivre.
    """
    etat = _etat(machines=4)
    etat.objectif, etat.debit, etat.satisfaction = 2.0, 0.5, 0.60
    d = decide(etat)
    ok = d.action == "etendre_production"
    rec("test_une_satisfaction_basse_ne_bloque_pas_la_croissance", ok,
        f"{d.action} malgré une satisfaction de 0.60")
    assert ok


def test_le_courant_manquant_passe_avant_l_extension() -> None:
    """Et c'est bien le CURRICULUM qui protège : une panne électrique est prioritaire."""
    from agents.coordinator import enumerer_options
    etat = _etat([_m("electric-furnace", 0, 0, "no_power")], machines=4)
    etat.objectif, etat.debit = 2.0, 0.5
    options = [o.action for o in enumerer_options(etat)]
    ok = options[0] in ("renforcer_energie", "relier") and "etendre_production" in options
    rec("test_le_courant_manquant_passe_avant_l_extension", ok, f"{options}")
    assert ok


def test_objectif_tenu_il_ne_fait_rien() -> None:
    """Et il s'arrête quand l'objectif est tenu : agrandir sans fin n'est pas un but."""
    etat = _etat(machines=4)
    etat.objectif, etat.debit = 1.0, 1.2
    d = decide(etat)
    ok = d.action == "rien"
    rec("test_objectif_tenu_il_ne_fait_rien", ok, f"{d.action} — {d.raison[:60]}")
    assert ok


def test_debit_non_mesure_ne_declenche_rien() -> None:
    """Sans mesure, pas d'action : `debit=None` n'est pas « débit nul ».

    C'est la distinction qui a manqué ailleurs et coûté cher : une lecture impossible
    prise pour un zéro ferait agrandir une usine sur une mesure qui n'existe pas.
    """
    etat = _etat(machines=4)
    etat.objectif, etat.debit = 1.0, None
    d = decide(etat)
    ok = d.action == "rien"
    rec("test_debit_non_mesure_ne_declenche_rien", ok,
        f"{d.action} (objectif fixé, débit inconnu)")
    assert ok


def test_sans_objectif_le_comportement_est_inchange() -> None:
    """Back-compat stricte : sans objectif, l'agent maintient — comme avant."""
    etat = _etat(machines=4)
    etat.debit = 0.0                      # même un débit nul ne déclenche rien
    d = decide(etat)
    ok = d.action == "rien" and etat.objectif is None
    rec("test_sans_objectif_le_comportement_est_inchange", ok,
        f"{d.action} (aucun objectif fixé)")
    assert ok


def test_reparer_passe_avant_etendre() -> None:
    """Le curriculum ne change pas : une panne l'emporte sur l'agrandissement.

    Une usine cassée qu'on agrandit est exactement le reproche que le benchmark FLE
    adresse aux agents LLM — empiler du neuf devant du cassé.
    """
    from agents.coordinator import enumerer_options
    etat = _etat([_m("electric-furnace", 0, 0, "no_fuel")], machines=4)
    etat.objectif, etat.debit = 1.0, 0.1
    options = [o.action for o in enumerer_options(etat)]
    ok = options[0] == "ravitailler" and "etendre_production" in options
    rec("test_reparer_passe_avant_etendre", ok, f"{options}")
    assert ok


def test_defendre_abandonnee_apres_trois_echecs() -> None:
    """La défense n'échappe plus au garde-fou d'acharnement.

    Elle y échappait d'une façon trompeuse : `tick` comptait ses échecs et le journal
    annonçait « ABANDON de defendre après 3 échecs », mais `enumerer_options` ne lisait
    pas ce compteur — l'option repartait faisable au tour suivant. La mémoire était
    écrite et jamais relue.

    Mesuré : 936 tours sur 952 à redécider `defendre`, dont 933 sans rien faire, tous
    sur « aucune position de tourelle libre au nord ».
    """
    from agents.coordinator import SEUIL_ABANDON, enumerer_options
    from services.threat_model import IMMINENTE
    etat = _etat(machines=0)
    etat.menace = _menace(IMMINENTE)
    etat.echecs = {("defendre", "", 0, 0): SEUIL_ABANDON}
    o = next((x for x in enumerer_options(etat) if x.action == "defendre"), None)
    ok = (o is not None and not o.faisable and o.priorite == 0
          and "on n'insiste plus" in o.raison)
    rec("test_defendre_abandonnee_apres_trois_echecs", ok,
        f"faisable={o.faisable if o else None} priorite={o.priorite if o else None}")
    assert ok


def test_defendre_abandonnee_laisse_dire_rien() -> None:
    """Et l'agent DIT qu'il n'y a rien à faire, au lieu de retenter la même chose.

    C'est l'effet utile : `decide` sait déjà répondre `rien` quand aucune option n'est
    faisable. Encore fallait-il que la défense puisse cesser de l'être — sinon elle
    restait la seule option, donc choisie, indéfiniment.
    """
    from agents.coordinator import SEUIL_ABANDON
    from services.threat_model import IMMINENTE
    etat = EtatUsine(machines=3, diagnostic=diagnose([]), reseau=7, production_kw=900.0,
                     inventaire=dict(INVENTAIRE))
    etat.menace = _menace(IMMINENTE)
    etat.echecs = {("defendre", "", 0, 0): SEUIL_ABANDON}
    d = decide(etat)
    ok = d.action == "rien"
    rec("test_defendre_abandonnee_laisse_dire_rien", ok, f"{d.action} — {d.raison[:70]}")
    assert ok


def test_l_abandon_de_defendre_se_leve() -> None:
    """Il se lève dès que le compteur retombe : `tick` le vide au premier succès.

    Un garde-fou qui ne se relâche jamais transformerait un échec passager en renoncement
    définitif — les nids bougent, le matériel revient, le terrain se dégage.
    """
    from agents.coordinator import enumerer_options
    from services.threat_model import IMMINENTE
    etat = _etat(machines=0)
    etat.menace = _menace(IMMINENTE)
    etat.echecs = {}                      # ce que laisse un succès
    o = next((x for x in enumerer_options(etat) if x.action == "defendre"), None)
    ok = o is not None and o.faisable and o.priorite > 0
    rec("test_l_abandon_de_defendre_se_leve", ok,
        f"faisable={o.faisable if o else None} priorite={o.priorite if o else None}")
    assert ok


# L'inventaire de celui qui peut bâtir une évacuation : un bras et un coffre.
OUTILLE = dict(INVENTAIRE, **{"inserter": 10, "wooden-chest": 5})


def test_vidage_repete_devient_ramassage_permanent() -> None:
    """Vider deux fois la sortie d'un four est une réparation ; quatre, c'est un aveu.

    Mesuré sur une partie de 952 tours partie d'une carte propre : le même four est
    retombé en `full_output` aux tours 152, 380, 608 et 831, chaque fois « réparé » par
    un vidage manuel. Entre-temps le foreur en amont attendait `waiting_for_space` : la
    chaîne entière s'arrêtait faute d'un ramassage de quelques secondes. C'est le
    symétrique exact du ravitaillement — même seuil, autre bout de la machine.
    """
    from agents.coordinator import SEUIL_AUTOMATISATION
    rows = [_m("electric-furnace", 10, 20, "full_output")]
    premiere = decide(_etat(rows, inventaire=OUTILLE))
    etat = _etat(rows, inventaire=OUTILLE)
    etat.evacuations = {("electric-furnace", 10, 20): SEUIL_AUTOMATISATION}
    apres = decide(etat)
    ok = (premiere.action == "evacuer" and apres.action == "batir_evacuation"
          and "ramassage permanent" in apres.raison)
    rec("test_vidage_repete_devient_ramassage_permanent", ok,
        f"1er passage={premiere.action} -> après {SEUIL_AUTOMATISATION} vidages="
        f"{apres.action}")
    assert ok


def test_les_deux_compteurs_ne_se_confondent_pas() -> None:
    """Une machine remplie dix fois n'a pas pour autant une sortie à automatiser.

    Les deux mémoires visent la même machine et basculent vers deux constructions
    opposées — l'une en amont, l'autre en aval. Les confondre ferait bâtir une chaîne
    d'approvisionnement pour une sortie qui déborde, ce qui aggraverait le bouchon.
    """
    rows = [_m("electric-furnace", 10, 20, "full_output")]
    etat = _etat(rows, inventaire=OUTILLE)
    etat.ravitaillements = {("electric-furnace", 10, 20): 10}   # remplie, jamais vidée
    d = decide(etat)
    ok = d.action == "evacuer"
    rec("test_les_deux_compteurs_ne_se_confondent_pas", ok,
        f"{d.action} (ravitaillée 10 fois, vidée 0 fois)")
    assert ok


def test_evacuation_sans_coffre_est_declassee() -> None:
    """Sans coffre où déposer, l'évacuation n'aboutira pas : elle est dite infaisable.

    Même règle que pour la défense sans tourelle. Proposer en tête une construction dont
    le matériel manque trompe l'arbitre exactement comme un humain — et ici, la reléguer
    laisse `evacuer` reprendre la main, c'est-à-dire le dépannage qui, lui, est possible.
    """
    from agents.coordinator import SEUIL_AUTOMATISATION, enumerer_options
    etat = _etat([_m("electric-furnace", 10, 20, "full_output")],
                 inventaire={"coal": 100})            # ni bras ni coffre
    etat.evacuations = {("electric-furnace", 10, 20): SEUIL_AUTOMATISATION}
    o = next((x for x in enumerer_options(etat) if x.action == "batir_evacuation"), None)
    ok = (o is not None and not o.faisable and o.priorite == 0
          and "INFAISABLE" in o.raison and "wooden-chest" in o.raison)
    rec("test_evacuation_sans_coffre_est_declassee", ok,
        f"{o.action if o else None} faisable={o.faisable if o else None} "
        f"{o.raison[-60:] if o else ''}")
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
    # Une réparation par cause, PLUS les voies concurrentes du combustible — remplir,
    # bâtir la chaîne, aller extraire — qui sont un choix et non un verdict. Elles
    # n'apparaissent qu'UNE fois quel que soit le nombre de machines à sec, sans quoi dix
    # pannes donneraient trente options et noieraient l'arbitre.
    ok = (set(actions) >= {"ravitailler", "regler_recette", "evacuer"}
          and len(actions) == len(set(actions))          # aucun doublon
          # L'ordre par défaut reste le curriculum : les pannes graves d'abord, et la
          # conséquence (`evacuer`) en dernier.
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
    # `>= 2` et non `== 2` : une machine à sec ouvre désormais trois voies (remplir,
    # bâtir la chaîne, aller extraire) au lieu d'une seule tranchée par un seuil. Ce
    # que ce test protège est que l'arbitre L'EMPORTE sur le défaut, pas le compte.
    ok = (len(options) >= 2 and d.action == options[1].action
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
    # `no_recipe` et non `no_fuel` : depuis que le combustible ouvre trois voies
    # (remplir / bâtir la chaîne / aller extraire), une machine à sec n'est plus un
    # cas « sans choix ». La propriété testée — pas d'appel quand il n'y a rien à
    # arbitrer — est inchangée ; c'est la mise en scène qui devait suivre.
    decide(_etat([_m("assembling-machine-1", 0, 0, "no_recipe")]), arbitre=_compte)
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
        self.api = None
        self.ecarts: list = []
        self._echecs: dict = {}   # la mémoire d'acharnement, éprouvée à part
        self._acharnement: dict = {}     # son pendant par action seule, idem
        self._tour: int = 0              # la quarantaine des abandons, idem
        self._quarantaine: dict = {}
        self.run = Coordinator.run.__get__(self)
        self.tick = Coordinator.tick.__get__(self)

    def _attente(self, d):
        """Aucune attente ici : ces tests portent sur l'enchaînement des tours.

        La vérification des attentes est éprouvée séparément (`test_attente_*`), avec un
        faux api qui rend de vraies mesures — la mêler à la boucle rendrait les deux
        moins lisibles.
        """
        return None

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


class _ApiMesure:
    """Un api réduit à ce que les attentes lisent : des entités et un réseau."""

    def __init__(self, entites=(), network=None):
        self.entites = list(entites)
        self.network = network
        self.attentes_lues = 0

    def inspect_at(self, x, y, radius=0.5):
        # Recherche par AIRE, comme le mod : une entité est présente dès que son EMPRISE
        # touche la zone interrogée. Filtrer sur son centre — ce que faisait le mod avant
        # E13 — rendait invisible toute machine large sous le point demandé.
        self.attentes_lues += 1
        return {"entities": [
            e for e in self.entites
            if abs(e["x"] - x) <= radius + e.get("w", 0.5)
            and abs(e["y"] - y) <= radius + e.get("h", 0.5)]}

    def get_power_state(self, x, y, radius=4.0):
        return {"networkId": self.network}

    def run_action(self, fn, *args, timeout=None):
        return {"ok": True}

    def wait(self, ticks):
        return {"ok": True}


def _coord_mesure(api):
    from agents.coordinator import Coordinator
    c = Coordinator.__new__(Coordinator)
    c.api = api
    c.zone = (0.0, 0.0)
    c.tourelle = "gun-turret"
    # Le vrai Coordinator porte `combustible="coal"` depuis son __init__. Un double qui
    # ne copie pas le réel ne prouve rien : sans ce champ, un test passait au vert sur
    # un chemin que la production ne pouvait pas exécuter.
    c.combustible = "coal"
    c._chaines = {}
    return c


def test_attente_ravitailler_tenue_et_decue() -> None:
    """Un réservoir encore à sec après ravitaillement doit être CONSTATÉ.

    C'est le cas le plus simple, et pourtant celui que la boucle ne voyait pas : elle
    journalisait « ravitaillement de boiler » sans jamais relire le statut.
    """
    from agents.coordinator import Decision
    from services.factory_doctor import Symptome
    cible = Symptome(name="boiler", x=0.0, y=0.0, cause="sans_combustible",
                     gravite=2, detail="vide")
    d = Decision(action="ravitailler", raison="", cible=cible)

    plein = _coord_mesure(_ApiMesure([{"name": "boiler", "x": 0.0, "y": 0.0,
                                       "status": "working"}]))
    tenue, _ = plein._attente(d).evaluer(plein.api)

    sec = _coord_mesure(_ApiMesure([{"name": "boiler", "x": 0.0, "y": 0.0,
                                     "status": "no_fuel"}]))
    decue, observe = sec._attente(d).evaluer(sec.api)

    ok = tenue and not decue and "no_fuel" in observe
    rec("test_attente_ravitailler_tenue_et_decue", ok,
        f"machine alimentée -> {tenue}, machine encore à sec -> {decue} ({observe})")
    assert ok


def test_attente_machine_absente_est_un_echec() -> None:
    """Une machine introuvable ne doit jamais compter comme réparée.

    Sans cette garde, une entité qu'on croit avoir posée et qui n'existe pas produirait
    le même journal qu'une réparation réussie.
    """
    from agents.coordinator import Decision
    from services.factory_doctor import Symptome
    cible = Symptome(name="boiler", x=0.0, y=0.0, cause="sans_combustible",
                     gravite=2, detail="vide")
    c = _coord_mesure(_ApiMesure([]))
    tenue, observe = c._attente(Decision(action="ravitailler", raison="",
                                         cible=cible)).evaluer(c.api)
    ok = not tenue and "absente" in observe
    rec("test_attente_machine_absente_est_un_echec", ok, f"tenue={tenue} ({observe})")
    assert ok


def test_attente_approvisionner_suit_le_flux() -> None:
    """LE critère du chantier : une chaîne posée mais rompue doit être vue comme rompue.

    Trois des six défauts d'E13 (raccord retourné, bras qui dépose à côté, belt trouée)
    se manifestaient tous par « chaîne bâtie » suivi d'un boiler qui restait vide. Ici la
    chaîne EXISTE — 4 belts, un bras — mais le bras dépose dans le vide, et l'attente
    doit le refuser.
    """
    from agents.coordinator import Decision
    from services.factory_doctor import Symptome
    cible = Symptome(name="boiler", x=8.0, y=0.0, cause="sans_combustible",
                     gravite=2, detail="vide")
    d = Decision(action="approvisionner", raison="", cible=cible)

    belts = [{"name": "transport-belt", "type": "transport-belt", "x": 0.5 + i,
              "y": 0.5, "direction": "east"} for i in range(4)]
    bras_ok = {"name": "burner-inserter", "type": "inserter", "x": 4.5, "y": 0.5,
               "pickupX": 3.5, "pickupY": 0.5, "dropX": 7.6, "dropY": 0.5}
    bras_ko = dict(bras_ok, dropX=4.5, dropY=-1.7)     # dépose au nord, dans le vide
    boiler = {"name": "boiler", "type": "boiler", "x": 8.0, "y": 0.5,
              "w": 1.3, "h": 0.8}          # emprise 3x2, mesurée en jeu

    bon = _coord_mesure(_ApiMesure(belts + [bras_ok, boiler]))
    bon._chaines[("boiler", 8, 0)] = (0.5, 0.5)
    tenue, _ = bon._attente(d).evaluer(bon.api)

    casse = _coord_mesure(_ApiMesure(belts + [bras_ko, boiler]))
    casse._chaines[("boiler", 8, 0)] = (0.5, 0.5)
    decue, observe = casse._attente(d).evaluer(casse.api)

    ok = tenue and not decue and "bras_depose_dans_le_vide" in observe
    rec("test_attente_approvisionner_suit_le_flux", ok,
        f"chaîne saine -> {tenue}, bras qui dépose à côté -> {decue}")
    assert ok, observe


def test_ecart_journalise_quand_lattente_est_decue() -> None:
    """La boucle doit consigner l'écart, sinon il n'existe pas pour la suite."""
    from agents.coordinator import Coordinator, Decision, EtatUsine
    from services.factory_doctor import Symptome
    cible = Symptome(name="boiler", x=0.0, y=0.0, cause="sans_combustible",
                     gravite=2, detail="vide")
    api = _ApiMesure([{"name": "boiler", "x": 0.0, "y": 0.0, "status": "no_fuel"}])
    c = _coord_mesure(api)
    c.journal, c.ecarts, c.arbitre = [], [], None
    c.constats, c.enqueteur = [], None      # l'enquête est éprouvée à part
    c._echecs, c._acharnement = {}, {}
    c._tour, c._quarantaine = 0, {}
    c.remettre_en_etat = lambda e: False    # la réparation aussi
    c.observer = lambda: EtatUsine()
    c.agir = lambda d: (True, "factice")
    c.tick = Coordinator.tick.__get__(c)
    c._attente = Coordinator._attente.__get__(c)
    # `decide` sur un état vide rendrait « rien » : on force la décision à vérifier.
    import agents.coordinator as mod
    vrai_decide = mod.decide
    mod.decide = lambda etat, arbitre=None: Decision(action="ravitailler", raison="",
                                                     cible=cible)
    try:
        c.tick()
    finally:
        mod.decide = vrai_decide
    ok = len(c.ecarts) == 1 and c.ecarts[0].action == "ravitailler" \
        and any("ÉCART" in j for j in c.journal)
    rec("test_ecart_journalise_quand_lattente_est_decue", ok,
        f"{len(c.ecarts)} écart(s) : {c.ecarts[0] if c.ecarts else '-'}")
    assert ok


def test_acharnement_declasse_apres_trois_echecs() -> None:
    """Une action qui échoue sans cesse doit cesser d'être prioritaire.

    Mesuré en partie longue : 559 tours sur 562 passés à retenter la même action
    impossible. La boucle ne plantait pas — elle « fonctionnait », ce qui est pire :
    aucun symptôme, et tout son temps passé à ne rien faire.

    L'option n'est pas supprimée mais DÉCLASSÉE : le contexte peut changer, et une
    option retirée pour toujours ne reviendrait jamais.
    """
    from agents.coordinator import SEUIL_ABANDON, enumerer_options
    # `no_fuel` (gravité 2) passe avant `full_output` (gravité 1) : c'est cet ordre-là
    # que le déclassement doit renverser, sans quoi on ne mesurerait rien.
    lignes = [_m("stone-furnace", 0, 0, "no_fuel"),
              _m("electric-furnace", 0, 8, "full_output")]
    avant = enumerer_options(_etat(lignes))
    d_avant = decide(_etat(lignes))

    etat = _etat(lignes)
    etat.echecs = {("ravitailler", "stone-furnace", 0, 0): SEUIL_ABANDON}
    apres = enumerer_options(etat)
    d_apres = decide(etat)
    relegue = next(o for o in apres if o.action == "ravitailler")

    # Le COMPTE d'options n'est plus le critère : le combustible en ouvre plusieurs.
    # Ce qui doit tenir est que l'option ratée soit DÉCLASSÉE sans disparaître —
    # priorité nulle, `faisable=False`, et le moteur choisit autre chose.
    ok = (avant[0].action == "ravitailler" and d_avant.action == "ravitailler"
          and not relegue.faisable and relegue.priorite == 0
          and d_apres.action != "ravitailler"
          and len(apres) == len(avant))
    rec("test_acharnement_declasse_apres_trois_echecs", ok,
        f"{d_avant.action} -> {d_apres.action} après {SEUIL_ABANDON} échecs ; "
        f"l'option reste proposée en dernier ({len(apres)} options)")
    assert ok, [f"{o.action}/{o.priorite}/{o.faisable}" for o in apres]


def test_arbitrage_trace_meme_sans_arbitre() -> None:
    """Chaque décision doit dire s'il y avait un CHOIX, arbitre ou pas.

    Sans cette trace, comparer une partie « avec modèle » à une partie « sans » ne mesure
    rien : `decide` n'appelle pas l'arbitre quand il n'y a qu'une option, et l'on
    conclurait « le modèle n'apporte pas » sans le lui avoir demandé une seule fois.
    """
    # Une panne SANS voie concurrente : le combustible en ouvre trois depuis qu'on
    # énumère au lieu de trancher, il ne peut donc plus illustrer « une seule option ».
    seule = decide(_etat([_m("assembling-machine-1", 0, 0, "no_recipe")]))
    deux = decide(_etat([_m("assembling-machine-1", 0, 0, "no_recipe"),
                         _m("electric-furnace", 0, 8, "full_output")]))
    ok = (seule.arbitrage is not None and seule.arbitrage.options == 1
          and not seule.arbitrage.arbitrable and not seule.arbitrage.appele
          and deux.arbitrage.options == 2 and deux.arbitrage.arbitrable
          and not deux.arbitrage.appele)          # pas d'arbitre fourni
    rec("test_arbitrage_trace_meme_sans_arbitre", ok,
        f"1 option -> arbitrable={seule.arbitrage.arbitrable} ; "
        f"2 options -> arbitrable={deux.arbitrage.arbitrable}, appele={deux.arbitrage.appele}")
    assert ok


def test_arbitrage_note_lappel_et_la_divergence() -> None:
    """Appelé et divergent sont deux faits distincts : l'un mesure l'occasion, l'autre l'effet."""
    lignes = [_m("stone-furnace", 0, 0, "no_fuel"),
              _m("electric-furnace", 0, 8, "full_output")]
    accord = decide(_etat(lignes), arbitre=lambda e, o: 0)
    divergent = decide(_etat(lignes), arbitre=lambda e, o: 1)
    ok = (accord.arbitrage.appele and not accord.arbitrage.diverge
          and divergent.arbitrage.appele and divergent.arbitrage.diverge
          and divergent.arbitrage.indice == 1)
    rec("test_arbitrage_note_lappel_et_la_divergence", ok,
        f"accord : appele={accord.arbitrage.appele} diverge={accord.arbitrage.diverge} ; "
        f"divergent : indice={divergent.arbitrage.indice}")
    assert ok


def test_arbitre_defaillant_reste_trace_comme_appele() -> None:
    """Un modèle qui plante A EU la parole : le compter comme non consulté fausserait tout."""
    def casse(etat, options):
        raise RuntimeError("modèle injoignable")
    d = decide(_etat([_m("stone-furnace", 0, 0, "no_fuel"),
                      _m("electric-furnace", 0, 8, "full_output")]), arbitre=casse)
    ok = d.arbitrage.appele and not d.arbitrage.diverge and d.arbitrage.indice == 0
    rec("test_arbitre_defaillant_reste_trace_comme_appele", ok,
        f"appele={d.arbitrage.appele}, repli sur l'indice {d.arbitrage.indice}")
    assert ok


def test_industrialiser_ce_quon_refait_a_la_main() -> None:
    """L'agent doit finir par MECANISER ce qu'il repete, sans qu'on nomme un produit.

    `produire` ne s'offrait que dans une fenetre etroite : il fallait qu'une technologie
    reclame precisement l'item qu'aucune chaine ne fabriquait. Mesure sur quarante tours
    de jeu, l'agent ne l'a JAMAIS rencontree — la capacite existait sans jamais servir.

    Le second signal est celui qui compte : refaire trois fois le meme item a la main est
    une habitude, et une habitude se mecanise.
    """
    from agents.coordinator import EtatUsine, SEUIL_INDUSTRIALISATION, a_industrialiser

    rien = a_industrialiser(EtatUsine())
    rec("rien a industrialiser sans signal", rien == ("", ""), f"{rien}")

    sous = EtatUsine(fabrications={"engrenage": SEUIL_INDUSTRIALISATION - 1})
    rec("sous le seuil, on laisse faire a la main",
        a_industrialiser(sous)[0] == "", f"{a_industrialiser(sous)}")

    au = EtatUsine(fabrications={"engrenage": SEUIL_INDUSTRIALISATION})
    item, motif = a_industrialiser(au)
    rec("au seuil, l'habitude declenche l'industrialisation",
        item == "engrenage" and "habitude" in motif, f"{item} — {motif}")

    # Le plus repete l'emporte : c'est lui qui coute le plus cher a la main.
    plusieurs = EtatUsion = EtatUsine(fabrications={"a": 3, "b": 7, "c": 4})
    rec("le plus repete l'emporte", a_industrialiser(plusieurs)[0] == "b",
        f"{a_industrialiser(plusieurs)}")

    # La recherche reste prioritaire : ce qu'elle reclame bloque une marche entiere.
    melange = EtatUsine(a_fournir=("flacon",), marche="techno",
                        fabrications={"b": 9})
    rec("ce que la recherche reclame passe devant",
        a_industrialiser(melange)[0] == "flacon", f"{a_industrialiser(melange)}")
    assert all(ok for _, ok, _ in RESULTS[-5:]), "industrialisation"


def test_une_action_sans_effet_compte_comme_un_echec() -> None:
    """RÉUSSIR N'EST PAS SERVIR, et seul le second doit peser sur la suite.

    La boucle savait déjà constater qu'une attente n'était pas tenue : elle consignait
    un `Ecart`, tentait une remise en état, ouvrait une enquête. Mais `_echecs` ne
    bougeait pas — donc au tour suivant l'action repartait au même rang, avec le même
    attrait, et rien dans l'état ne disait qu'on venait de la jouer pour rien.

    Mesuré le 01/08/2026, A/B de trois manches par branche : le modèle arbitre a passé
    **50 tours sur 75 (66 %)** sur `evacuer`, qui pose son coffre, rend `ok=True` et
    laisse l'usine identique. Le déterministe n'y échappait que par un contournement
    nommé (`if action == "evacuer" and vidages >= SEUIL_AUTOMATISATION`) — une exception
    par action, là où il fallait une loi.
    """
    from agents.coordinator import Coordinator, Decision, EtatUsine
    from services.factory_doctor import Symptome
    cible = Symptome(name="boiler", x=0.0, y=0.0, cause="sans_combustible",
                     gravite=2, detail="vide")
    # Le réservoir est TOUJOURS à sec après l'action : l'attente ne peut pas être tenue.
    api = _ApiMesure([{"name": "boiler", "x": 0.0, "y": 0.0, "status": "no_fuel"}])
    c = _coord_mesure(api)
    c.journal, c.ecarts, c.arbitre = [], [], None
    c.constats, c.enqueteur = [], None
    c._echecs, c._acharnement = {}, {}
    c._tour, c._quarantaine = 0, {}
    c.remettre_en_etat = lambda e: False     # la réparation ne rattrape rien
    c.observer = lambda: EtatUsine()
    c.agir = lambda d: (True, "posé sans broncher")   # l'action RÉUSSIT
    c.tick = Coordinator.tick.__get__(c)
    c._attente = Coordinator._attente.__get__(c)
    import agents.coordinator as mod
    vrai_decide = mod.decide
    mod.decide = lambda etat, arbitre=None: Decision(action="ravitailler", raison="",
                                                     cible=cible)
    try:
        c.tick()
        c.tick()
    finally:
        mod.decide = vrai_decide
    cle = ("ravitailler", "boiler", 0, 0)
    ok = c._echecs.get(cle) == 2
    rec("test_une_action_sans_effet_compte_comme_un_echec", ok,
        f"deux tours réussis mais sans effet -> _echecs={c._echecs or 'vide'} "
        f"(attendu {{{cle}: 2}})")
    assert ok


def test_evacuer_est_juge_sur_son_effet() -> None:
    """Vider une machine à la main doit se mesurer, comme tout le reste.

    Sept actions ont une attente — `ravitailler`, `approvisionner`, `etendre_production`,
    `redeployer_foreur`, `batir_evacuation`, `relier`, `defendre`. `evacuer` n'en avait
    aucune : bâtir le coffre était jugé sur son effet, vider à la main jamais. C'est
    précisément l'action sur laquelle l'arbitre s'est enfermé.

    Le critère est le pendant exact de `ravitailler` : le réservoir n'est plus à sec /
    la sortie n'est plus pleine.
    """
    from agents.coordinator import Decision
    from services.factory_doctor import Symptome
    cible = Symptome(name="stone-furnace", x=0.0, y=0.0, cause="sortie_bloquee",
                     gravite=2, detail="pleine")
    d = Decision(action="evacuer", raison="", cible=cible)

    vide = _coord_mesure(_ApiMesure(
        [{"name": "stone-furnace", "x": 0.0, "y": 0.0, "status": "working"}]))
    pleine = _coord_mesure(_ApiMesure(
        [{"name": "stone-furnace", "x": 0.0, "y": 0.0, "status": "full_output"}]))

    a = vide._attente(d)
    b = pleine._attente(d)
    couverte = a is not None and b is not None
    tenue = couverte and a.evaluer(vide.api)[0]
    decue = couverte and not b.evaluer(pleine.api)[0]
    ok = couverte and tenue and decue
    rec("test_evacuer_est_juge_sur_son_effet", ok,
        f"attente définie : {couverte} — sortie vidée -> {tenue}, "
        f"toujours pleine -> déçue {decue}")
    assert ok


def test_lacharnement_se_compte_par_action_pas_seulement_par_cible() -> None:
    """Vingt-neuf machines, et l'action n'est jamais en cause — seulement ses cibles.

    `_echecs` est indexé par `(action, nom, x, y)` : `evacuer` épuise ses trois échecs
    sur un four, puis repart INTACTE sur le suivant. Il reste toujours une cible neuve,
    donc l'acharnement sur l'ACTION n'est jamais vu. Un second compteur, par action
    seule, le voit.
    """
    from agents.coordinator import Coordinator, Decision, EtatUsine
    from services.factory_doctor import Symptome
    api = _ApiMesure([{"name": "stone-furnace", "x": 0.0, "y": 0.0,
                       "status": "full_output"},
                      {"name": "stone-furnace", "x": 9.0, "y": 0.0,
                       "status": "full_output"}])
    c = _coord_mesure(api)
    c.journal, c.ecarts, c.arbitre = [], [], None
    c.constats, c.enqueteur = [], None
    c._echecs, c._acharnement = {}, {}
    c._tour, c._quarantaine = 0, {}
    c.remettre_en_etat = lambda e: False
    c.observer = lambda: EtatUsine()
    c.agir = lambda d: (True, "vidée pour rien")
    c.tick = Coordinator.tick.__get__(c)
    c._attente = Coordinator._attente.__get__(c)
    import agents.coordinator as mod
    vrai_decide = mod.decide
    try:
        # DEUX cibles différentes : par cible, chaque compteur reste à 1.
        for x in (0.0, 9.0):
            cible = Symptome(name="stone-furnace", x=x, y=0.0, cause="sortie_bloquee",
                             gravite=1, detail="pleine")
            mod.decide = lambda etat, arbitre=None, _c=cible: Decision(
                action="evacuer", raison="", cible=_c)
            c.tick()
    finally:
        mod.decide = vrai_decide
    par_cible = max(c._echecs.values()) if c._echecs else 0
    ok = par_cible == 1 and c._acharnement.get("evacuer") == 2
    rec("test_lacharnement_se_compte_par_action_pas_seulement_par_cible", ok,
        f"par cible {par_cible} (jamais abandonnée) — par action "
        f"{c._acharnement.get('evacuer')} (attendu 2)")
    assert ok


def test_lacharnement_est_inscrit_dans_la_raison_de_loption() -> None:
    """Ce qui est joué en vain doit se LIRE, pas seulement se classer.

    Le déterministe survit à l'ornière parce qu'il suit l'ORDRE des options : une option
    déclassée descend, il prend la première. L'arbitre LLM, lui, choisit dans la liste
    sans égard au rang — et rien dans le texte ne lui dit qu'il vient de jouer cette
    action dix-sept fois pour rien. Il ne voit que « [i] evacuer (priorité N) — raison ».

    Mesuré (A/B, trois manches par branche) : `evacuer` 51 tours sur 75, soit 68 %, et
    ce APRÈS que la loi « réussir n'est pas servir » l'ait fait compter comme un échec.
    Le compteur existait, le modèle ne le voyait pas.

    On inscrit donc le fait, et RIEN d'autre : aucune priorité touchée, aucune option
    retirée. Le déterministe garde le comportement déjà mesuré ; le modèle décide de ce
    qu'il fait de l'information. C'est un fait montré, pas une consigne donnée.
    """
    from agents.coordinator import enumerer_options
    lignes = [_m("stone-furnace", 0, 0, "no_fuel"),
              _m("electric-furnace", 0, 8, "full_output")]
    vierge = enumerer_options(_etat(lignes))
    etat = _etat(lignes)
    etat.acharnement = {"evacuer": 17}
    options = enumerer_options(etat)
    evac = next(o for o in options if o.action == "evacuer")
    autre = next(o for o in options if o.action != "evacuer")

    inscrit = "17" in evac.raison and "sans effet" in evac.raison
    epargne = "17" not in autre.raison
    # L'ordre et les priorités ne bougent PAS : la branche déterministe est déjà mesurée
    # et ce changement ne doit rien lui coûter.
    intact = ([o.action for o in options] == [o.action for o in vierge]
              and [o.priorite for o in options] == [o.priorite for o in vierge]
              and [o.faisable for o in options] == [o.faisable for o in vierge])
    ok = inscrit and epargne and intact
    rec("test_lacharnement_est_inscrit_dans_la_raison_de_loption", ok,
        f"evacuer -> « {evac.raison[-60:]} » ; ordre et priorités intacts : {intact}")
    assert ok


def test_un_abandon_se_perime_au_lieu_detre_definitif() -> None:
    """L'ABANDON EST ABSORBANT : on y entre, on n'en sort jamais.

    Une action atteint SEUIL_ABANDON echecs, elle est declassee (`faisable=False`). La
    seule sortie est de REUSSIR — mais pour reussir il faut etre choisie, et pour etre
    choisie il ne faut pas etre abandonnee. Aucune sortie n'existe.

    Mesure du 02/08, partie de 60 tours (les bancs a 25 tours s'arretaient sept tours
    trop tot pour le voir) : au tour 33 les cinq actions sont abandonnees, et l'agent
    passe les 28 tours suivants sur `rien` — trois options proposees, zero faisable.
    Defaut ANCIEN : avant E40 le mur tombait au tour 30, 51 % de chomage.

    Perimer « quand l'etat change » ne suffirait pas : un agent bloque ne change plus
    rien, donc rien ne se perimerait. La peremption doit venir du TEMPS. Un abandon met
    l'action en QUARANTAINE, il ne la supprime pas.
    """
    from agents.coordinator import (QUARANTAINE_TOURS, SEUIL_ABANDON, Coordinator,
                                    Decision, EtatUsine)
    from services.factory_doctor import Symptome
    cible = Symptome(name="boiler", x=0.0, y=0.0, cause="sans_combustible",
                     gravite=2, detail="vide")
    api = _ApiMesure([{"name": "boiler", "x": 0.0, "y": 0.0, "status": "no_fuel"}])
    c = _coord_mesure(api)
    c.journal, c.ecarts, c.arbitre = [], [], None
    c.constats, c.enqueteur = [], None
    c._echecs, c._acharnement = {}, {}
    c._tour, c._quarantaine = 0, {}
    c.remettre_en_etat = lambda e: False
    c.observer = lambda: EtatUsine()
    c.agir = lambda d: (False, "impossible")       # l'action ECHOUE, toujours
    c.tick = Coordinator.tick.__get__(c)
    c._attente = Coordinator._attente.__get__(c)
    cle = ("ravitailler", "boiler", 0, 0)

    import agents.coordinator as mod
    vrai_decide = mod.decide
    mod.decide = lambda etat, arbitre=None: Decision(action="ravitailler", raison="",
                                                     cible=cible)
    try:
        for _ in range(SEUIL_ABANDON):
            c.tick()
        abandonnee = c._echecs.get(cle, 0) >= SEUIL_ABANDON
        # L'agent continue de tourner. Aujourd'hui il retenterait indefiniment cette
        # action et la garderait indefiniment abandonnee : les deux sont des impasses.
        # Apres la quarantaine, le compteur doit avoir ete PURGE — l'action redevient
        # tentable, une fois, et sera re-abandonnee si elle echoue encore.
        for _ in range(QUARANTAINE_TOURS + 1):
            c.tick()
        levee = c._echecs.get(cle, 0) < SEUIL_ABANDON
    finally:
        mod.decide = vrai_decide

    ok = abandonnee and levee
    rec("test_un_abandon_se_perime_au_lieu_detre_definitif", ok,
        f"abandonnée après {SEUIL_ABANDON} échecs : {abandonnee} — "
        f"levée après {QUARANTAINE_TOURS} tours : {levee} (compteur {c._echecs.get(cle)})")
    assert ok


class _ApiEnergie:
    """Le strict nécessaire pour juger d'une cible : sa source d'énergie, et ce qu'on pose."""

    def __init__(self, sources: dict):
        self.sources = sources
        self.poses: list = []

    def get_state(self):
        # Le vrai jeu répond toujours à `get_state` : sans lui, `perception.inventory`
        # lève, et la mise en service ne peut plus juger de ce qu'elle a en poche.
        return {"inventory": {}, "tick": 1, "ready": True}

    def describe(self, name):
        return {"name": name, "entity": {"name": name,
                                         "energySource": self.sources.get(name, "electric")}}

    def place_entity_at(self, name, x, y, **kw):
        self.poses.append((name, x, y))
        return {"ok": True}

    def run_action(self, fn, *a, **kw):
        return fn(*a) if callable(fn) else {"ok": True}

    def inspect_at(self, x, y, radius=0.5):
        return {"entities": []}

    def get_power_state(self, x, y, radius=4.0):
        return {"networkId": None, "connected": False}

    def find_nearest(self, name):
        # Le vrai jeu répond toujours : sans cela le gavage ne peut pas aller miner.
        return {"name": name, "x": 40.0, "y": 0.0, "distance": 40}

    def walk_to(self, *a, **kw):
        return {"ok": True}

    def mine_entity(self, *a, **kw):
        return {"ok": True}

    def move_items_at(self, *a, **kw):
        return {"ok": True}


def test_on_ne_tire_pas_de_ligne_vers_une_machine_a_charbon() -> None:
    """UNE MACHINE BURNER NE SE BRANCHE PAS. Elle mange du charbon, pas des volts.

    Mesuré en jeu le 02/08, premier rush en mode PRODUCTION sur carte vierge : quatre
    tours, quatre `batir_production`, et au sol **90 poteaux électriques** pour 4
    `burner-mining-drill` et 4 `stone-furnace` — sans un seul générateur, boiler ou
    steam-engine. Personne ne consommait, rien ne produisait.

    La cause est mécanique : après la pose, `batir_chaine` relie toute machine dont
    `get_power_state` ne dit pas `connected`. Or une machine à charbon n'a AUCUNE
    connexion électrique — elle ne sera donc jamais `connected`, et l'agent lui déroule
    une ligne à chaque passage, indéfiniment.

    Le jeu donne déjà la réponse : `describe(nom)["entity"]["energySource"]` vaut
    « burner » ou « electric ». La garde va dans `relier` et non chez l'appelant : c'est
    une loi — on ne raccorde pas ce qui ne se raccorde pas — et elle vaut pour tous ceux
    qui relient, pas seulement pour la construction de chaîne.
    """
    from agents.coordinator import Coordinator
    from services.factory_doctor import Symptome
    api = _ApiEnergie({"burner-mining-drill": "burner", "stone-furnace": "burner",
                       "electric-mining-drill": "electric"})
    c = _coord_mesure(api)
    c.journal = []
    c.relier = Coordinator.relier.__get__(c)

    ok_b, motif = c.relier(Symptome(name="burner-mining-drill", x=10.0, y=0.0,
                                    cause="debranchee", gravite=1, detail=""))
    poses_apres_burner = list(api.poses)
    ok_f, _ = c.relier(Symptome(name="stone-furnace", x=14.0, y=0.0,
                                cause="debranchee", gravite=1, detail=""))

    refuse = (ok_b is False and ok_f is False)
    rien_pose = not poses_apres_burner and not api.poses
    ok = refuse and rien_pose and "burner" in motif.lower()
    rec("test_on_ne_tire_pas_de_ligne_vers_une_machine_a_charbon", ok,
        f"refusé : {refuse} — poteaux posés : {len(api.poses)} (attendu 0) — « {motif[:60]} »")
    assert ok


def test_lusine_est_aussi_grande_que_ce_quon_y_a_bati() -> None:
    """L'AGENT NE VOYAIT PAS CE QU'IL VENAIT DE CONSTRUIRE.

    `observer` compte les machines autour de `self.zone` dans `self.rayon` — tous deux
    figés à la construction du Coordinator. En `test_mode` le personnage est téléporté et
    bâtit près de son point de départ ; en PRODUCTION il marche vraiment jusqu'au
    gisement, à bien plus de 25 tuiles. Mesuré : 8 machines en terre, `machines=0` à
    chaque tour, donc l'agent rebâtit — quatre fois de suite.

    L'usine n'est pas un disque décidé d'avance : elle est aussi grande que ce qu'on y a
    bâti. Le rayon s'étend pour englober le chantier, le centre ne bouge pas (les bancs
    qui vérifient autour de la zone gardent leur repère), et l'extension est bornée —
    scanner sans limite coûterait plus cher que de ne rien voir.
    """
    from agents.coordinator import RAYON_USINE_MAX, Coordinator
    c = _coord_mesure(_ApiMesure([]))
    c.zone, c.rayon, c.journal = (0.0, 0.0), 25.0, []
    c._englober = Coordinator._englober.__get__(c)

    c._englober(10.0, 0.0)
    proche = c.rayon                       # déjà dedans : rien ne doit bouger
    c._englober(100.0, 0.0)
    loin = c.rayon
    c._englober(10_000.0, 0.0)
    borne = c.rayon

    ok = (proche == 25.0 and loin >= 100.0 and borne <= RAYON_USINE_MAX
          and c.zone == (0.0, 0.0))
    rec("test_lusine_est_aussi_grande_que_ce_quon_y_a_bati", ok,
        f"25 -> {proche} (proche) -> {loin} (chantier à 100) -> {borne} "
        f"(borné à {RAYON_USINE_MAX}), centre {c.zone}")
    assert ok


def test_toute_construction_elargit_lusine_pas_seulement_les_chaines() -> None:
    """L'AGENT BÂTISSAIT TROIS FOIS PLUS LOIN QU'IL NE REGARDAIT.

    Mesuré au rush en production sur carte vierge : 18 machines posées entre 69 et 84
    tuiles du spawn, pour un rayon d'observation de 25. L'agent n'en a vu AUCUNE — d'où
    `machines=0` à chaque tour, aucun symptôme, jamais de `ravitailler` malgré sept
    foreuses à sec, et `batir_production` reproposée jusqu'à l'abandon : 66 tours de
    `rien` sur 120.

    `_englober` existait déjà mais n'était appelé que dans `batir_chaine`. Or
    `batir_production` passe par `batir()` : sur 40 constructions, zéro élargissement.
    Ajouter l'appel dans `batir()` ne ferait qu'attendre le prochain chemin oublié — on
    l'ancre donc là où TOUS passent, dans `tick`, sur la position du personnage. En
    production il doit s'approcher pour poser : sa position EST le chantier.
    """
    from agents.coordinator import Coordinator, Decision, EtatUsine

    class _ApiLoin(_ApiMesure):
        """Un personnage à 80 tuiles — là où l'agent va réellement bâtir."""

        def get_state(self):
            return {"character": {"position": {"x": 80.0, "y": 0.0}}}

    c = _coord_mesure(_ApiLoin([]))
    c.zone, c.rayon = (0.0, 0.0), 25.0
    c.journal, c.ecarts, c.arbitre = [], [], None
    c.constats, c.enqueteur = [], None
    c._echecs, c._acharnement = {}, {}
    c._tour, c._quarantaine = 0, {}
    c.remettre_en_etat = lambda e: False
    c.observer = lambda: EtatUsine()
    c.agir = lambda d: (True, "chaîne posée au loin")
    c.tick = Coordinator.tick.__get__(c)
    c._attente = Coordinator._attente.__get__(c)
    c._englober = Coordinator._englober.__get__(c)

    import agents.coordinator as mod
    vrai_decide = mod.decide
    mod.decide = lambda etat, arbitre=None: Decision(action="batir_production", raison="")
    try:
        c.tick()
    finally:
        mod.decide = vrai_decide

    ok = c.rayon >= 80.0 and c.zone == (0.0, 0.0)
    rec("test_toute_construction_elargit_lusine_pas_seulement_les_chaines", ok,
        f"rayon 25 -> {c.rayon:.0f} après une construction à 80 tuiles "
        f"(centre inchangé : {c.zone})")
    assert ok


def test_le_diagnostic_trouve_lusine_ou_quelle_soit() -> None:
    """L'AGENT REGARDAIT UN DISQUE, PAS SON USINE.

    Mesuré au rush en production : 18 machines posées entre 92 et 97 tuiles du spawn, et
    `diagnose_zone(0, 0, r)` rend `machines=0` POUR TOUT r — y compris 150. Ce n'est pas
    le rayon du Coordinator qui est en cause : `inspect_at` PLAFONNE à 64 tuiles
    (`mod/scripts/tools.lua`, `math.min(radius, 64)`), et au-delà d'une soixantaine de
    tuiles le scan est de toute façon saturé par les arbres (269 arbres sur 293 lignes).
    Élargir le rayon ne pouvait donc rien donner — mon premier correctif visait la
    mauvaise cause.

    Le diagnostic fonctionne parfaitement quand on l'amène au bon endroit :
    `diagnose_zone(80, 46, 10)` rend 3 machines et nomme `sans_combustible`. Ce qui
    manque n'est pas de la portée mais un CENTRE : il faut demander au jeu où sont les
    machines avant de les inspecter.

    Le précédent existe déjà pour les centrales (`perception.centrales`, qui les trouve
    « OÙ QU'ELLES SOIENT » via `find_entities_filtered` puis les lit par `inspect_at`).
    On généralise ce patron au parc de production.
    """
    from services import perception

    class _ApiParc:
        """Le jeu connaît la position de ses machines ; le scan aveugle, non."""

        class _Rcon:
            def query_lua(self, code):
                # Le jeu répond la bbox du parc, loin du spawn.
                return "80,45,82,49"

        def __init__(self):
            self.rcon = self._Rcon()
            self.demandes: list = []

        def inspect_at(self, x, y, radius=0.5):
            self.demandes.append((round(x), round(y), round(radius)))
            # Le plafond du mod : au-delà de 64, on ne rend rien d'utile.
            if math.hypot(x, y) > 64 and radius > 64:
                return {"entities": []}
            return {"entities": [
                {"name": "burner-mining-drill", "type": "mining-drill",
                 "status": "no_fuel", "x": 81, "y": 45},
                {"name": "stone-furnace", "type": "furnace",
                 "status": "no_ingredients", "x": 81, "y": 49}]}

    api = _ApiParc()
    trouvees = perception.parc(api)
    types = {e.get("type") for e in trouvees}
    # On a bien interrogé AUTOUR DU PARC, pas autour du spawn.
    centre_vise = api.demandes[0][:2] if api.demandes else (0, 0)
    ok = (len(trouvees) == 2 and types == {"mining-drill", "furnace"}
          and math.hypot(centre_vise[0] - 81, centre_vise[1] - 47) < 5)
    rec("test_le_diagnostic_trouve_lusine_ou_quelle_soit", ok,
        f"{len(trouvees)} machine(s) {sorted(types)} — inspection centrée sur "
        f"{centre_vise} (le parc est en ~(81,47), le spawn en (0,0))")
    assert ok


def test_une_recherche_qu_on_ne_peut_automatiser_se_paie_a_la_main() -> None:
    """LA PREMIÈRE RECHERCHE NE PEUT PAS S'AUTOMATISER, ET L'AGENT REFUSAIT DE LA PAYER.

    Mesuré en jeu sur carte vierge — `chercher` échoue 6 fois sur 6, puis est abandonné :

        automatiser_la_science -> « pas d'assembleuse possible : assembling-machine-1
        s'ouvre par "automation", qui se paie en science : 10 × automation-science-pack »

    La boucle est fermée : pour chercher il veut automatiser la science ; pour automatiser
    il faut une assembleuse ; l'assembleuse exige `automation` ; `automation` exige dix
    flacons, donc de chercher. `alimenter_la_science` bute sur la même assembleuse — aucun
    chemin manuel n'existait.

    Or le jeu ouvre grand ce chemin, vérifié sur la carte : `automation-science-pack`,
    `lab`, `iron-gear-wheel`, `electronic-circuit`, `offshore-pump`, `boiler`,
    `steam-engine` sont TOUS fabricables d'office ; `assembling-machine-1` est le seul
    verrou. C'est exactement le parcours d'un joueur : miner, fondre, fabriquer un
    laboratoire, y déposer dix flacons faits à la main, lancer la première recherche.

    La règle n'est pas « traiter le cas automation » mais : **une recherche se paie en
    flacons, qu'ils viennent d'une chaîne ou de mes mains.** Automatiser reste préférable
    — on n'y renonce que lorsque c'est impossible.
    """
    from agents.coordinator import Coordinator, Decision
    from services import recherche as mod_recherche

    class _Marche:
        nom, gratuite = "automation", False

    class _EtatRecherche:
        marches = (_Marche(),)

    c = _coord_mesure(_ApiMesure([]))
    c.journal = []
    appels: list[str] = []
    c.automatiser_la_science = lambda *a, **k: (False, "pas d'assembleuse possible")
    c.alimenter_la_science = lambda *a, **k: (False, "aucune assembleuse réglée")
    c.payer_la_recherche = lambda marche: (appels.append(getattr(marche, "nom", "?"))
                                           or (True, "10 flacons déposés au laboratoire"))
    c.chercher = lambda t: (True, f"recherche {t} lancée")
    c.agir = Coordinator.agir.__get__(c)

    vrai_lire = mod_recherche.lire
    mod_recherche.lire = lambda api: _EtatRecherche()
    try:
        ok, detail = c.agir(Decision(action="chercher", raison="", item="automation"))
    finally:
        mod_recherche.lire = vrai_lire

    ok_test = ok and appels == ["automation"]
    rec("test_une_recherche_qu_on_ne_peut_automatiser_se_paie_a_la_main", ok_test,
        f"paiement manuel tenté pour {appels or 'AUCUNE techno'} — « {str(detail)[:60]} »")
    assert ok_test


def test_payer_la_recherche_fabrique_et_porte_les_flacons() -> None:
    """La méthode elle-même, pas seulement son câblage.

    Le test précédent remplace `payer_la_recherche` par un double : il prouve qu'on
    l'appelle, jamais qu'elle marche. Celui-ci l'exécute — sans quoi une méthode qui
    référence des fonctions inexistantes passerait tous les tests au vert.
    """
    from agents.coordinator import Coordinator

    class _Marche:
        nom = "automation"
        cout = (("automation-science-pack", 10),)

    class _ApiPorte(_ApiMesure):
        def __init__(self):
            super().__init__([])
            self.portes: list = []

        def move_items_at(self, item, entite, x, y, count=0, to=True):
            self.portes.append((item, entite, round(x), round(y), count))
            return {"ok": True}

        def run_action(self, fn, *a, timeout=None):
            return fn(*a) if callable(fn) else {"ok": True}

    api = _ApiPorte()
    c = _coord_mesure(api)
    c.journal = []
    crafts: list = []
    c.poser_le_laboratoire = lambda: ((12.0, 8.0), "laboratoire posé")
    c.fabriquer = lambda item, n=1: (crafts.append((item, n)) or (True, f"{item}×{n}"))
    c.payer_la_recherche = Coordinator.payer_la_recherche.__get__(c)

    import services.perception as perc
    vrai_inv = perc.inventory
    perc.inventory = lambda api: {}          # les mains vides : tout est à fabriquer
    try:
        ok, detail = c.payer_la_recherche(_Marche())
    finally:
        perc.inventory = vrai_inv

    ok_test = (ok and crafts == [("automation-science-pack", 10)]
               and api.portes == [("automation-science-pack", "lab", 12, 8, 10)])
    rec("test_payer_la_recherche_fabrique_et_porte_les_flacons", ok_test,
        f"fabriqué {crafts} — porté {api.portes} — « {str(detail)[:60]} »")
    assert ok_test


def test_sans_combustible_pour_amorcer_on_va_en_chercher() -> None:
    """IL REFUSAIT DE REMPLIR SA FOREUSE POUR GARDER UNE RÉSERVE QU'IL N'AVAIT PAS.

    Observé en jeu sur carte vierge, kit vanilla (1 foreuse, 1 four, 8 plaques) :

        burner-mining-drill -> no_fuel,  combustible dans la foreuse : 0
        en poche : coal=4
        l'agent mine 85 minerais de fer À LA MAIN pendant que sa chaîne dort

    Le mécanisme, et il se défend en régime établi : ravitailler avec moins de
    `RESERVE_AMORCE` (40) en poche devient `approvisionner` — « garder ce qui reste pour
    amorcer une chaîne plutôt que le brûler en un plein ». Mais `approvisionner` exige
    `AMORCE_CHAINE_BURNER` (20) pour amorcer justement cette chaîne. Avec 4 charbons, ni
    l'un ni l'autre n'est possible, et RIEN ne dit d'aller simplement en miner.

    L'agent sait pourtant le faire : `fabriquer` « se procure : miner, fondre, crafter »,
    et `verify_bootstrap_craft` le voit chercher son charbon à deux cents tuiles. La
    capacité existait, la décision manquait.

    La règle : quand le stock ne suffit même pas à amorcer, on va en chercher — remplir
    ou construire viendront après.
    """
    from agents.coordinator import (AMORCE_CHAINE_BURNER, COMBUSTIBLE, enumerer_options)
    lignes = [_m("burner-mining-drill", 0, 0, "no_fuel")]

    # LES MAINS VIDES, REMPLIR N'EST PAS UNE OPTION LÉGALE. Le déterministe garde la main
    # sur le POSSIBLE — c'est la part qui ne se délègue pas : proposer « remplir » sans
    # rien en poche ferait perdre un tour à l'arbitre, quel que soit son jugement.
    rien = enumerer_options(_etat(lignes, inventaire={COMBUSTIBLE: 0}))
    remplir_propose = any(o.action == "ravitailler" for o in rien)
    chercher = next((o for o in rien
                     if o.action == "fabriquer" and o.item == COMBUSTIBLE), None)

    ok = (not remplir_propose and chercher is not None
          and chercher.quantite >= AMORCE_CHAINE_BURNER)
    rec("test_sans_combustible_pour_amorcer_on_va_en_chercher", ok,
        f"0 en poche -> remplir proposé : {remplir_propose} (attendu False) ; "
        f"{chercher.action + '×' + str(chercher.quantite) if chercher else 'AUCUNE extraction'}")
    assert ok


def test_evacuer_s_approche_avant_de_vider() -> None:
    """ON NE VIDE PAS UNE MACHINE À TRENTE TUILES, ET LE MODE TEST LE CACHAIT.

    Le mod refuse toute interaction au-delà de `reach_distance + 2` — sauf en test :

        local function out_of_reach(char, target, kind)
          if player_mod.is_test_mode() then return false end   -- aucune portée
          max_d = p.reach_distance + 2                          -- ~10 tuiles en prod

    `evacuer` appelait `empty_output_at` sans jamais s'approcher. `verify_evacuation_e21`
    passe donc 7/7 en test, et en production l'action échoue à chaque fois : mesuré au
    rush, **66 tours sur 120** passés à tenter de vider un four hors de portée, jusqu'à
    l'abandon. Quatrième défaut que `test_mode` masque, après la portée de pose, le
    diagnostic et le plafond d'`inspect_at`.

    Le remède est celui que la pose applique depuis longtemps (`execute_micro(...,
    approach=True)`) : s'approcher d'abord. On ne marche que si c'est nécessaire — une
    machine déjà à portée ne doit pas coûter un déplacement.
    """
    from agents.coordinator import Coordinator, Decision
    from services import deplacement as mod_depl
    from services.factory_doctor import Symptome

    class _ApiVide(_ApiMesure):
        def __init__(self):
            super().__init__([])
            self.vidages: list = []

        def empty_output_at(self, x, y, nom):
            self.vidages.append((round(x), round(y), nom))
            return {"ok": True}

        def run_action(self, fn, *a, timeout=None):
            return fn(*a) if callable(fn) else {"ok": True}

    marches: list = []
    vrai_pos, vrai_marcher = mod_depl.position, mod_depl.marcher_vers
    mod_depl.position = lambda api: (0.0, 0.0)          # l'agent est au spawn
    mod_depl.marcher_vers = lambda api, x, y, **k: (marches.append((x, y)) or (x, y))
    try:
        # Cible LOINTAINE : il faut y aller.
        api = _ApiVide()
        c = _coord_mesure(api)
        c.journal, c._evacuations = [], {}
        c.agir = Coordinator.agir.__get__(c)
        loin = Symptome(name="stone-furnace", x=80.0, y=45.0, cause="sortie_bloquee",
                        gravite=1, detail="pleine")
        ok_loin, _ = c.agir(Decision(action="evacuer", raison="", cible=loin))
        marche_faite = list(marches)

        # Cible PROCHE : marcher serait du temps perdu.
        marches.clear()
        api2 = _ApiVide()
        c2 = _coord_mesure(api2)
        c2.journal, c2._evacuations = [], {}
        c2.agir = Coordinator.agir.__get__(c2)
        pres = Symptome(name="stone-furnace", x=3.0, y=2.0, cause="sortie_bloquee",
                        gravite=1, detail="pleine")
        ok_pres, _ = c2.agir(Decision(action="evacuer", raison="", cible=pres))
    finally:
        mod_depl.position, mod_depl.marcher_vers = vrai_pos, vrai_marcher

    ok = (ok_loin and marche_faite == [(80.0, 45.0)] and api.vidages
          and ok_pres and not marches and api2.vidages)
    rec("test_evacuer_s_approche_avant_de_vider", ok,
        f"cible à 92 tuiles -> marche {marche_faite or 'AUCUNE'} ; "
        f"cible à 4 tuiles -> marche {marches or 'aucune (correct)'}")
    assert ok


def test_le_combustible_offre_un_choix_au_lieu_dun_verdict() -> None:
    """ON NE LAISSAIT RIEN À CHOISIR, PUIS ON REPROCHAIT AU MODÈLE DE MAL CHOISIR.

    « J'ai 37 charbons, ma foreuse est à sec : je remplis, ou je garde pour amorcer une
    chaîne ? » est un ARBITRAGE. Le code y répondait par des seuils empilés — 20, puis 40
    — et ne proposait qu'une seule option, déjà tranchée. Le modèle ne voyait jamais
    l'alternative : ce n'est pas qu'il choisissait mal, on ne lui laissait rien.

    C'est contraire au contrat du projet — *le déterministe énumère les options légales,
    le modèle en désigne une* — et c'est une des raisons pour lesquelles l'A/B ne
    départageait rien (+2 % en médiane, dans le bruit).

    Mesuré en jeu, le blocage que produisait le seuil : 37 charbons en poche, foreuse à
    `fuel:0`, et `fabriquer` joué 108 tours sur 120 parce que remplir était refusé pour
    épargner une réserve... qu'il avait.

    Le déterministe garde la main sur le POSSIBLE — faisabilité, matériel, abandons — et
    l'ORDRE reste le sien : sans arbitre, `decide` choisit exactement comme avant.
    """
    from agents.coordinator import COMBUSTIBLE, enumerer_options
    lignes = [_m("burner-mining-drill", 0, 0, "no_fuel")]

    riche = _etat(lignes, inventaire={COMBUSTIBLE: 37})
    options = enumerer_options(riche)
    actions = [o.action for o in options if o.cible is not None or o.item == COMBUSTIBLE]

    a_remplir = any(o.action == "ravitailler" for o in options)
    a_batir = any(o.action == "approvisionner" for o in options)
    a_miner = any(o.action == "fabriquer" and o.item == COMBUSTIBLE for o in options)

    # Les faits doivent voyager avec l'option : un arbitre ne juge pas sur un verbe.
    chiffre = any("37" in (o.raison or "") for o in options)

    # SANS ARBITRE, RIEN NE CHANGE : la branche déterministe est déjà mesurée, ce
    # chantier ne doit pas lui coûter un point.
    d = decide(riche)
    ordre_tenu = d.action == options[0].action

    ok = a_remplir and a_batir and a_miner and chiffre and ordre_tenu
    rec("test_le_combustible_offre_un_choix_au_lieu_dun_verdict", ok,
        f"options={actions} — remplir={a_remplir} bâtir={a_batir} miner={a_miner} — "
        f"stock cité={chiffre} — déterministe inchangé={ordre_tenu} ({d.action})")
    assert ok


def test_les_ancres_sont_essayees_de_la_plus_proche_a_la_plus_lointaine() -> None:
    """MARCHER N'EST GRATUIT QU'EN `test_mode`, ET LE CODE A ÉTÉ ÉCRIT POUR LUI.

    `batir` essaie jusqu'à six ancrages sur le gisement — « la meilleure tuile est occupée
    dès que la première chaîne y est posée ». En test le personnage est TÉLÉPORTÉ : chaque
    essai est gratuit. En production, chacun coûte une traversée à pied.

    Mesuré à la deuxième partie d'Hermes, sur carte vierge : vingt-cinq minutes passées à
    marcher d'un candidat à l'autre, laboratoire et matériel complets en poche, et
    **aucune machine posée**. La construction n'a jamais abouti — non qu'elle échoue, mais
    parce qu'elle n'en finissait pas d'essayer.

    Cinquième défaut de la même famille que ceux d'hier : sain en test, ruineux en jeu.
    Celui-ci ne casse rien — il rend seulement la construction inatteignable.

    On ne change PAS quelles ancres sont candidates, seulement l'ordre : d'abord la plus
    proche de là où l'agent se trouve. Le choix du gisement reste au planificateur.
    """
    from agents.coordinator import ancres_par_proximite

    candidats = [(100.0, 0.0), (5.0, 0.0), (50.0, 0.0), (5.0, 5.0)]
    triees = ancres_par_proximite(candidats, depuis=(0.0, 0.0))

    ok = (triees[0] == (5.0, 0.0) and triees[-1] == (100.0, 0.0)
          # Aucune ancre perdue ni inventée : c'est un TRI, pas un filtre.
          and sorted(triees) == sorted(candidats))
    rec("test_les_ancres_sont_essayees_de_la_plus_proche_a_la_plus_lointaine", ok,
        f"depuis (0,0) -> {triees} (la plus proche d'abord, aucune perdue)")
    assert ok


def test_le_coffre_se_choisit_comme_les_autres_paliers() -> None:
    """UNE CONSTANTE LÀ OÙ IL FALLAIT UN CHOIX — et neuf minutes perdues pour un coffre.

    Mesuré à la cinquième partie d'Hermes, sur carte vierge :

        09:46:19 >> batir_une_chaine {'item': 'iron-plate'}
        09:55:44 << 565.2s ÉCHEC — 0 entité posée, missing={'wooden-chest': 1}

    Neuf minutes de travail, 18 belts, 5 inserteurs, 3 foreuses et 2 fours fabriqués —
    et le plan entier refusé faute d'UN coffre. Il avait pourtant dix plaques de fer en
    poche, de quoi faire un `iron-chest` (8 plaques) sur-le-champ.

    La cause : `LayoutConstraints.sink_tier = "wooden-chest"`, écrit en dur. Le projet
    choisit déjà ses paliers de foreuse, four et inserteur selon les moyens du moment
    (`tiers_micro`) ; le réceptacle avait été oublié dans cette logique.

    Aggravant : `wooden-chest` coûte 2 bois, et le bois n'a AUCUNE recette — il se
    récolte sur un arbre, ce que `fabriquer` ne sait pas faire. Le coffre en bois était
    donc hors d'atteinte, pas seulement absent.
    """
    from agents.coordinator import Coordinator

    class _Api:
        """Ce que le jeu répond sur les recettes, rien de plus."""

        def __init__(self, ouvertes, inventaire=None):
            self.ouvertes, self.inv = set(ouvertes), dict(inventaire or {})

    def _tier(ouvertes, inventaire=None):
        api = _Api(ouvertes, inventaire)
        c = _coord_mesure(api)
        c.tiers_micro = Coordinator.tiers_micro.__get__(c)
        import services.perception as perc
        vrai_inv, vrai_rec = perc.inventory, perc.recipe_of
        perc.inventory = lambda a: dict(a.inv)
        # LA FORME RÉELLE : `recipe_of` rend une LISTE DE COUPLES, pas un dict. Le
        # double rendait un dict et validait une forme qui n'existe pas — d'où
        # « 'list' object has no attribute 'get' » à chaque appel, une partie durant.
        # Un double qui ne copie pas le réel ne prouve rien.
        ingredients = {"wooden-chest": [("wood", 2)],
                       "iron-chest": [("iron-plate", 8)]}
        perc.recipe_of = lambda a, n: (ingredients.get(n, [])
                                       if n in a.ouvertes else None)
        try:
            return Coordinator.coffre_disponible(c)
        finally:
            perc.inventory, perc.recipe_of = vrai_inv, vrai_rec

    # Du bois en poche : le coffre en bois convient, il est le moins cher.
    bois = _tier({"wooden-chest", "iron-chest"}, {"wood": 4})
    # Pas de bois, mais des plaques : le coffre en FER, plutôt que d'échouer.
    fer = _tier({"wooden-chest", "iron-chest"}, {"iron-plate": 10})
    # Ni l'un ni l'autre : on le dit, on n'invente pas.
    rien = _tier(set(), {})

    ok = (bois == "wooden-chest" and fer == "iron-chest" and rien is None)
    rec("test_le_coffre_se_choisit_comme_les_autres_paliers", ok,
        f"avec du bois -> {bois} ; sans bois mais 10 plaques -> {fer} ; "
        f"sans rien -> {rien}")
    assert ok


def test_une_chaine_burner_est_approvisionnee_pas_branchee() -> None:
    """UNE CHAÎNE QUI DÉMARRE N'EST PAS UNE CHAÎNE QUI TIENT.

    10e partie Hermes, première chaîne réellement posée en jeu : 29 entités, la
    production démarre à 0,66 plaque/s — puis s'éteint. Deux minutes plus tard, les
    trois foreuses sont en `no_fuel`, plus une seule machine ne travaille, et la
    production est passée de +79 à +3 par fenêtre.

    Après la pose, `batir_chaine` fait deux choses : il RELIE au courant, et il
    ÉVACUE. Relier ne veut rien dire pour une machine à charbon — la garde de
    `relier` le refuse d'ailleurs, à juste titre. Il ne restait donc RIEN pour une
    chaîne tout-burner, et l'`AMORCE_BRAS` de cinq charbons est un démarrage, pas
    une alimentation : un foreur burner l'a brûlée en moins de deux minutes.

    Le pendant existe pourtant déjà : `approvisionner` bâtit la chaîne
    mine -> belt -> inserter qui amène le charbon. Personne ne l'appelait après une
    pose. La règle est symétrique et tient en une ligne : ce qui mange du courant se
    RELIE, ce qui mange du charbon s'APPROVISIONNE.
    """
    from agents.coordinator import Coordinator

    api = _ApiEnergie({"burner-mining-drill": "burner", "stone-furnace": "burner",
                       "electric-mining-drill": "electric"})
    c = _coord_mesure(api)
    c.journal = []
    relies, approvisionnes = [], []
    c.relier = lambda s: (relies.append(s.name), (True, "relié"))[1]
    c.approvisionner = lambda cible, item="coal", **kw: (approvisionnes.append(cible.name),
                                                   (True, "approvisionné"))[1]

    class _Pose:
        def __init__(self, name, x, y, role):
            self.name, self.x, self.y, self.role = name, x, y, role
            self.idx, self.direction = 0, 0

    poses = [_Pose("burner-mining-drill", 10.0, 0.0, "drill"),
             _Pose("stone-furnace", 14.0, 0.0, "machine"),
             _Pose("electric-mining-drill", 18.0, 0.0, "drill")]

    c._mettre_en_service = Coordinator._mettre_en_service.__get__(c)
    branchees, ravitaillees, _ = c._mettre_en_service(poses)

    ok = (sorted(approvisionnes) == ["burner-mining-drill", "stone-furnace"]
          and relies == ["electric-mining-drill"]
          and ravitaillees == 2 and branchees == 1)
    rec("test_une_chaine_burner_est_approvisionnee_pas_branchee", ok,
        f"approvisionnés {approvisionnes} / reliés {relies} "
        f"— {ravitaillees} ravitaillée(s), {branchees} branchée(s)")
    assert ok


def test_l_alimentation_fabrique_le_foreur_qui_lui_manque() -> None:
    """UNE CHAÎNE QUI CONSOMME TOUT SON STOCK NE PEUT PLUS S'ALIMENTER.

    Banc H14 en jeu, carte neuve : `batir_chaine` pose ses 29 entités, puis
    `approvisionner` est bien appelé (le rapport porte « 0 alimentée(s) en coal ») et
    échoue aussitôt :

        alimentation refusée : foreur non posé sur coal : {'burner-mining-drill': 1}

    `batir_chaine` fabrique EXACTEMENT les foreuses de la chaîne de fer, puis
    l'alimentation en réclame une de plus pour le charbon — il n'en reste aucune.
    Résultat mesuré : 6 machines en service à la pose, zéro 90 secondes plus tard, et
    la production figée à 159 plaques sur trois fenêtres.

    `batir_chaine` sait pourtant se procurer ce qui lui manque plutôt que de renoncer.
    L'alimentation doit en faire autant : c'est une pose comme une autre, et ce qui
    vaut pour la chaîne vaut pour ce qui la nourrit.
    """
    from agents.coordinator import Coordinator

    class _ApiStock:
        def __init__(self, inv):
            self.inv = dict(inv)

        def get_state(self):
            return {"inventory": dict(self.inv), "tick": 1, "ready": True}

    demandes = []

    def _coord(inv):
        c = _coord_mesure(_ApiStock(inv))
        c.journal = []
        c.fabriquer = lambda item, combien=1: (demandes.append((item, combien)),
                                               (True, f"{item} fabriqué"))[1]
        c._assurer_stock = Coordinator._assurer_stock.__get__(c)
        return c

    # Les mains vides : il FAUT fabriquer.
    ok_vide, _ = _coord({})._assurer_stock("burner-mining-drill", 1)
    apres_vide = list(demandes)
    # Déjà en poche : ne rien refabriquer, c'est du temps de jeu pur.
    ok_plein, _ = _coord({"burner-mining-drill": 2})._assurer_stock("burner-mining-drill", 1)
    apres_plein = list(demandes)

    ok = (ok_vide and ok_plein
          and apres_vide == [("burner-mining-drill", 1)]
          and apres_plein == apres_vide)
    rec("test_l_alimentation_fabrique_le_foreur_qui_lui_manque", ok,
        f"mains vides -> {apres_vide} ; deja en poche -> {apres_plein[len(apres_vide):]} (attendu aucune)")
    assert ok


def test_une_evacuation_qui_echoue_dit_pourquoi() -> None:
    """UN COMPTEUR À ZÉRO N'EST PAS UN DIAGNOSTIC.

    Partie 10 et banc H14 rendent tous deux « 0 sortie(s) évacuée(s) » sans un mot de
    plus. Impossible de savoir laquelle des deux causes joue : aucune machine de tête
    n'a été identifiée dans le plan, ou `batir_evacuation` a été refusée. Le motif
    était pourtant là — le code l'écartait d'un `ok_e, _ =`.

    C'est le défaut qui a coûté trois parties sous une autre forme : une action qui
    échoue sans dire pourquoi se lit comme une action qui n'avait rien à faire.
    """
    from agents.coordinator import Coordinator

    class _Ent:
        def __init__(self, role, node_item):
            self.role, self.node_item = role, node_item

    class _Pose:
        def __init__(self, idx, name="stone-furnace"):
            self.idx, self.name, self.x, self.y = idx, name, 0.0, 0.0

    class _Plan:
        def __init__(self, ents):
            self.entities = ents

    class _Rap:
        def __init__(self, poses):
            self.placed = poses

    c = _coord_mesure(None)
    c.journal = []
    c._evacuer_les_tetes = Coordinator._evacuer_les_tetes.__get__(c)

    # 1. Aucune machine de tête dans le plan : il faut le DIRE.
    c.batir_evacuation = lambda cible, coffre="wooden-chest": (True, "ok")
    vide, motif_vide = c._evacuer_les_tetes(
        _Plan([_Ent("drill", "iron-ore")]), _Rap([_Pose(0)]), "iron-plate")

    # 2. Une machine de tête, mais l'évacuation refusée : le motif doit remonter.
    c.batir_evacuation = lambda cible, coffre="wooden-chest": (False, "coffre introuvable")
    refuse, motif_refus = c._evacuer_les_tetes(
        _Plan([_Ent("machine", "iron-plate")]), _Rap([_Pose(0)]), "iron-plate")

    ok = (vide == 0 and "tête" in motif_vide
          and refuse == 0 and "coffre introuvable" in motif_refus)
    rec("test_une_evacuation_qui_echoue_dit_pourquoi", ok,
        f"sans tête : « {motif_vide[:45]} » — refusée : « {motif_refus[:45]} »")
    assert ok


def test_la_portee_d_alimentation_se_chiffre_en_plaques() -> None:
    """« TROP LOIN » DOIT ÊTRE UN CALCUL, PAS UN NOMBRE ROND.

    Banc H15 en jeu : l'alimentation refuse avec « aucun gisement de coal à moins de
    60 tuiles : c'est un problème de train, pas de belt ». Mesuré ensuite sur la carte :
    le charbon est à **65 tuiles** de l'usine. Il manquait cinq tuiles, soit 8 % — et
    toute l'usine s'est éteinte sur ce seuil frôlé.

    Le seuil de 60 n'était justifié que par « au-delà, une belt coûte plus qu'elle ne
    rapporte », sans chiffre. Or une belt vaut 1 plaque + 1 engrenage, soit **3 plaques
    de fer par tuile**. Les 65 tuiles coûtaient 195 plaques, que cette usine produit en
    cinq minutes à 0,66 plaque/s : très au-dessus du point de rentabilité.

    La portée est donc adossée à un COÛT explicite, et le nombre de belts nécessaires
    se calcule depuis la distance — sinon on étend la portée pour poser une ligne qu'on
    n'a pas de quoi fabriquer.
    """
    from agents.coordinator import Coordinator

    c = _coord_mesure(None)
    # La distance mesurée en jeu doit passer, avec de la marge.
    couvre_le_cas_mesure = Coordinator.PORTEE_APPRO >= 65.0
    # Et rester bornée par ce qu'un bootstrap peut payer.
    cout_max = Coordinator.PORTEE_APPRO * Coordinator.PLAQUES_PAR_BELT
    reste_payable = cout_max <= 400.0

    c._belts_pour = Coordinator._belts_pour.__get__(c)
    assez = c._belts_pour(65.0) >= 65        # jamais moins que la distance
    marge = c._belts_pour(65.0) <= 65 + 20   # ni un stock déraisonnable

    ok = couvre_le_cas_mesure and reste_payable and assez and marge
    rec("test_la_portee_d_alimentation_se_chiffre_en_plaques", ok,
        f"portée {Coordinator.PORTEE_APPRO:.0f} tuiles = {cout_max:.0f} plaques ; "
        f"65 tuiles -> {c._belts_pour(65.0)} belt(s)")
    assert ok


def test_on_gave_les_bruleurs_avant_de_leur_batir_une_belt() -> None:
    """L'ORDRE ÉTAIT INVERSÉ : on payait la belt avant d'allumer l'usine.

    Banc H17 : `_assurer_stock('transport-belt', 73)` lance la fabrication de 73 belts
    — 219 plaques à miner et fondre à la main. Mesuré en jeu : 217 tâches exécutées,
    18 belts sur 73, l'usine toujours éteinte. Une heure de jeu pour une ligne, pendant
    que trois foreuses sont en `no_fuel` avec quarante charbons dans les poches.

    Or c'est l'usine qui produit les plaques : lui faire payer sa belt AVANT de tourner
    est un ordre impossible. Un joueur verse d'abord ce qu'il a en poche — l'usine
    tourne, elle produit, et la belt devient payable.

    `AMORCE_BRAS` (5 charbons) tient 90 secondes, mesuré trois bancs de suite. Le stock
    en poche, lui, tient dix minutes : assez pour que la chaîne paye sa propre
    logistique. On répartit donc le charbon disponible entre les brûleurs posés, en
    gardant de quoi amorcer la foreuse à charbon elle-même.
    """
    from agents.coordinator import Coordinator

    versements = []

    class _ApiGave:
        def __init__(self, charbon):
            self.charbon = charbon

        def get_state(self):
            return {"inventory": {"coal": self.charbon}, "tick": 1, "ready": True}

        def describe(self, name):
            # Le vrai jeu répond « burner » ou « electric » ici, et la garde ne tranche
            # pas sur son propre silence : sans `describe`, TOUT passe pour électrique.
            source = "burner" if ("burner" in name or "stone-furnace" == name) else "electric"
            return {"name": name, "entity": {"name": name, "energySource": source}}

        def run_action(self, fn, *a, **kw):
            nom = getattr(fn, "__name__", "")
            if nom == "mine_entity":
                self.charbon += int(a[1]) if len(a) > 1 else 0
            elif nom == "move_items_at":
                versements.append(a)
            return {"ok": True}

        def find_nearest(self, name):
            return {"name": name, "x": 40.0, "y": 0.0, "distance": 40}

        def walk_to(self, *a, **kw):
            return {"ok": True}

        def mine_entity(self, *a, **kw):
            return {"ok": True}

        def move_items_at(self, *a, **kw):
            return {"ok": True}

    class _Pose:
        def __init__(self, name, x, y):
            self.name, self.x, self.y, self.role = name, x, y, "drill"

    c = _coord_mesure(_ApiGave(40))
    c.journal = []
    c._gaver_les_bruleurs = Coordinator._gaver_les_bruleurs.__get__(c)
    poses = [_Pose("burner-mining-drill", 0.0, 0.0),
             _Pose("burner-mining-drill", 2.0, 0.0),
             _Pose("stone-furnace", 4.0, 0.0)]

    verses = c._gaver_les_bruleurs(poses)
    parts = [a[4] for a in versements if len(a) > 4]
    total = sum(parts)
    ok = (verses == 3 and len(parts) == 3
          and total > 3 * 5                      # nettement plus que l'amorce
          and all(q >= 5 for q in parts))        # jamais moins qu'avant
    rec("test_on_gave_les_bruleurs_avant_de_leur_batir_une_belt", ok,
        f"{verses} brûleur(s) gavé(s), parts {parts} sur 40 charbons")
    assert ok


def test_gaver_va_miner_le_charbon_qui_manque() -> None:
    """RENONCER EN SILENCE FAUTE DE CHARBON, C'EST LAISSER MOURIR L'USINE.

    Banc H18 : le gavage calcule `part = disponible // brûleurs`, trouve moins que
    l'amorce et rend 0 sans un mot. Mesuré : l'usine monte à 6 machines en service,
    +51 plaques sur la deuxième fenêtre, puis s'éteint entre T2 et T3 — trois minutes.
    Le rapport disait pourtant « 50 coal » minés pendant la construction : l'exécuteur
    les avait déjà répartis en amorces de cinq à la pose.

    Le charbon est à 65 tuiles, il n'y a plus une seule belt en poche après la pose, et
    l'usine produit 159 plaques quand une ligne en coûte 195. Aucune sortie automatique
    n'existe à ce stade — mais un joueur, lui, va simplement en miner. C'est ce que fait
    déjà `approvisionner` à son étape zéro (« si la réserve a fondu, on va la reprendre
    à la main sur le gisement »), et c'est exactement ce qui manquait ici.

    Ce que ça achète : de quoi tourner assez longtemps pour financer la belt, au lieu de
    mourir trois minutes après la pose.
    """
    from agents.coordinator import Coordinator

    mines = []

    class _ApiSec:
        def __init__(self, charbon):
            self.charbon = charbon

        def get_state(self):
            return {"inventory": {"coal": self.charbon}, "tick": 1, "ready": True}

        def describe(self, name):
            source = "burner" if ("burner" in name or name == "stone-furnace") else "electric"
            return {"name": name, "entity": {"name": name, "energySource": source}}

        def run_action(self, fn, *a, **kw):
            if getattr(fn, "__name__", "") == "mine_entity":
                mines.append(a)
                self.charbon += int(a[1]) if len(a) > 1 else 0
            return {"ok": True}

        def mine_entity(self, *a, **kw):
            return {"ok": True}

        def move_items_at(self, *a, **kw):
            return {"ok": True}

        def walk_to(self, *a, **kw):
            return {"ok": True}

        def find_nearest(self, name):
            return {"name": name, "x": 60.0, "y": 0.0, "distance": 60}

    class _Pose:
        def __init__(self, name, x, y):
            self.name, self.x, self.y, self.role = name, x, y, "drill"

    api = _ApiSec(6)                       # presque rien : l'amorce a tout consommé
    c = _coord_mesure(api)
    c.journal = []
    c._gaver_les_bruleurs = Coordinator._gaver_les_bruleurs.__get__(c)
    poses = [_Pose("burner-mining-drill", 0.0, 0.0),
             _Pose("burner-mining-drill", 2.0, 0.0),
             _Pose("stone-furnace", 4.0, 0.0)]

    verses = c._gaver_les_bruleurs(poses)
    ok = bool(mines) and verses == 3
    rec("test_gaver_va_miner_le_charbon_qui_manque", ok,
        f"{len(mines)} minage(s) déclenché(s) -> {verses} brûleur(s) gavé(s) "
        f"(stock initial 6)")
    assert ok


def test_l_evacuation_fabrique_le_bras_qui_lui_manque() -> None:
    """MÊME DÉFAUT QUE L'ALIMENTATION, À L'AUTRE BOUT DE LA CHAÎNE.

    Banc H18, une fois que le rapport a cessé de se taire (H16) :

        évacuation refusée : aucun bras disponible pour évacuer stone-furnace

    `batir_evacuation` LIT l'inventaire et renonce. Or la chaîne vient de consommer ses
    cinq bras à la pose, exactement comme elle avait consommé ses foreuses — c'est le
    défaut réglé par `_assurer_stock` côté alimentation, resté ici.

    Ce que ça coûte : une machine de tête qui se remplit et bloque toute la mine
    derrière elle (`full_output` en amont, `waiting_for_space` sur les foreuses). Le
    même four est retombé en `full_output` aux tours 152, 380, 608 et 831 d'une partie
    de 952 tours, chaque fois « réparé » à la main.
    """
    from agents.coordinator import Coordinator

    demandes = []

    class _ApiVide:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiVide())
    c.journal = []
    c.fabriquer = lambda item, combien=1: (demandes.append((item, combien)),
                                           (False, "pas de fer"))[1]
    c._assurer_stock = Coordinator._assurer_stock.__get__(c)
    c.batir_evacuation = Coordinator.batir_evacuation.__get__(c)

    from services.factory_doctor import Symptome
    ok_e, motif = c.batir_evacuation(
        Symptome(name="stone-furnace", x=0.0, y=0.0, cause="sortie_pleine",
                 gravite=1, detail=""))

    # Il a tenté de forger un bras avant de renoncer, et le dit.
    ok = bool(demandes) and demandes[0][0].endswith("inserter")
    rec("test_l_evacuation_fabrique_le_bras_qui_lui_manque", ok,
        f"tentative(s) de fabrication : {demandes} — motif « {str(motif)[:50]} »")
    assert ok


def test_l_evacuation_fabrique_le_coffre_qui_lui_manque() -> None:
    """TROISIÈME FOIS LE MÊME DÉFAUT : on LIT l'inventaire au lieu de FORGER.

    Partie 11 d'Hermes, en direct. Il bâtit sa chaîne (29 entités, 10 machines en
    service, 200 plaques), diagnostique 4 machines sur 10 en panne, et appelle
    `reparer('batir_evacuation')` de lui-même — le comportement qu'on visait depuis le
    début. Réponse :

        ÉCHEC — aucun wooden-chest pour recevoir la sortie de machine

    Après la foreuse (H15) et le bras (H20), c'est le coffre. Le même schéma à chaque
    fois : une pièce absente, une méthode qui lit l'inventaire et renonce, alors que
    l'agent sait la fabriquer. Un `wooden-chest` coûte deux bûches, et le bois est
    récoltable depuis H11 — mesuré 5/5 en jeu le jour même.

    Ce que ça coûte : la machine de tête se remplit, `full_output`, et toute la mine
    s'arrête derrière elle.
    """
    from agents.coordinator import Coordinator

    demandes = []

    class _ApiVide:
        def get_state(self):
            return {"inventory": {"burner-inserter": 1}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiVide())
    c.journal = []
    c.fabriquer = lambda item, combien=1: (demandes.append((item, combien)),
                                           (False, "pas de bois"))[1]
    c._assurer_stock = Coordinator._assurer_stock.__get__(c)
    c.batir_evacuation = Coordinator.batir_evacuation.__get__(c)

    from services.factory_doctor import Symptome
    _, motif = c.batir_evacuation(
        Symptome(name="stone-furnace", x=0.0, y=0.0, cause="sortie_pleine",
                 gravite=1, detail=""))

    ok = any(str(i).endswith("chest") for i, _ in demandes)
    rec("test_l_evacuation_fabrique_le_coffre_qui_lui_manque", ok,
        f"tentative(s) : {demandes} — motif « {str(motif)[:55]} »")
    assert ok


def test_on_recolte_l_usine_avant_de_miner_a_la_main() -> None:
    """LE CERCLE VICIEUX DE LA SESSION, ET SA SORTIE.

    Partie 13, mesuré : l'usine a produit **444 plaques**, le joueur en a **4**. Hermes
    lance sa chaîne de charbon, qui échoue sur `missing={'burner-mining-drill': 2}` — et
    la fabrication de la foreuse échoue à son tour, deux fois, en 26 puis 20 étapes.

    Le raisonnement tourne en rond : sans plaques pas de foreuse, sans foreuse pas de
    chaîne de charbon, sans charbon les fours ne fondent plus, donc pas de plaques. Et
    440 plaques dorment dans les machines pendant ce temps.

    La sortie n'est pas de miner davantage — c'est de RÉCOLTER ce qui est déjà produit.
    `empty_output_at` vide une machine ; le mod le fait déjà pour `evacuer`. Une usine
    qu'on ne récolte pas est un stock qu'on n'a pas.

    On ne récolte que ce qui sert : viser l'item manquant évite de vider une chaîne
    voisine qui alimentait autre chose.
    """
    from agents.coordinator import Coordinator

    vidages = []

    class _ApiUsine:
        def __init__(self):
            self.stock = 4

        def get_state(self):
            return {"inventory": {"iron-plate": self.stock}, "tick": 1, "ready": True}

        def inspect_at(self, x, y, radius=0.5):
            return {"entities": [
                {"name": "stone-furnace", "type": "furnace", "x": 6.0, "y": 6.0,
                 "status": "waiting_for_space_in_destination"},
                {"name": "transport-belt", "type": "transport-belt", "x": 7.0, "y": 6.0,
                 "status": "working"}]}

        def run_action(self, fn, *a, **kw):
            if getattr(fn, "__name__", "") == "empty_output_at":
                vidages.append(a)
                self.stock += 50           # la machine rend ce qu'elle retenait
            return {"ok": True}

        def empty_output_at(self, *a, **kw):
            return {"ok": True}

    api = _ApiUsine()
    import services.perception as _perc
    _vrai_parc = _perc.parc
    _perc.parc = lambda api: [{"name": "stone-furnace", "type": "furnace",
                               "x": 0.0, "y": 0.0}]

    c = _coord_mesure(api)
    c.journal = []
    c.zone, c.rayon = (0.0, 0.0), 30.0
    c._recolter_la_production = Coordinator._recolter_la_production.__get__(c)

    gagne = c._recolter_la_production("iron-plate")
    ok = bool(vidages) and gagne > 0
    _perc.parc = _vrai_parc
    rec("test_on_recolte_l_usine_avant_de_miner_a_la_main", ok,
        f"{len(vidages)} machine(s) vidée(s), {gagne:+d} plaque(s) récupérée(s)")
    assert ok


def test_fabriquer_recolte_avant_de_planifier() -> None:
    """LE CORRECTIF ÉTAIT SUR LA BRANCHE QUE PERSONNE NE PRENAIT — deuxième fois.

    H23 branchait la récolte dans `_assurer_stock`. Or `batir_chaine` se procure ce qui
    lui manque en appelant `fabriquer` DIRECTEMENT : la récolte n'était donc jamais
    atteinte sur le seul chemin qui en avait besoin. Partie 14, mesuré : 395 plaques
    produites, zéro en poche, et « missing={'burner-mining-drill': 2} » avec une
    fabrication qui échoue en 23 puis 20 étapes.

    C'est l'erreur de H10 refaite à l'identique — un correctif juste, posé sur une
    branche que le cas réel n'emprunte pas. `fabriquer` est le point de passage
    universel : la récolte va là.

    On ne récolte QUE si l'on manque, et jamais deux fois de suite : vider les fours à
    chaque craft coûterait plus que cela ne rapporte.
    """
    from agents.coordinator import Coordinator

    recoltes = []

    class _ApiFab:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiFab())
    c.journal = []
    c.builder = None
    c._fabrications = {}
    c._recolte_faite = False
    c._recolter_la_production = lambda item: (recoltes.append(item), 0)[1]
    c.fabriquer = Coordinator.fabriquer.__get__(c)

    try:
        c.fabriquer("burner-mining-drill", 2)
    except Exception:
        pass          # le double n'ira pas jusqu'au bout ; seule la récolte nous occupe

    ok = bool(recoltes)
    rec("test_fabriquer_recolte_avant_de_planifier", ok,
        f"récolte(s) tentée(s) avant planification : {recoltes}")
    assert ok


def test_le_coffre_d_evacuation_essaie_aussi_les_diagonales() -> None:
    """QUATRE DIRECTIONS NE SUFFISENT PAS AUTOUR D'UNE MACHINE CEINTURÉE.

    Partie 14, motif enfin lisible une fois le journal détronqué :

        évacuation refusée : aucune place pour évacuer stone-furnace (96.5,26.5)

    Ce n'est plus « aucun coffre » — H21 le forge désormais — mais « aucune place ».
    La recherche n'essayait que les quatre axes cardinaux à trois distances, soit douze
    positions. Or un four de tête est précisément la machine la plus entourée de la
    chaîne : belt d'entrée, bras de chargement, belt de sortie. Ses quatre côtés sont
    pris, et le ramassage échoue là où il est le plus nécessaire.

    Les diagonales sont libres bien plus souvent — rien d'une chaîne en ligne ne les
    occupe. Vingt-quatre candidats au lieu de douze, sans changer ni l'ordre (du plus
    proche au plus loin) ni la vérification `can_place` qui précède chaque pose.
    """
    from agents.coordinator import Coordinator

    c = _coord_mesure(None)
    places = list(Coordinator._places_pour_coffre(c, 10.0, 10.0))

    cardinales = [(x, y) for x, y in places if x == 10.5 or y == 10.5]
    diagonales = [(x, y) for x, y in places if x != 10.5 and y != 10.5]
    # Du plus proche au plus loin : une place à côté vaut mieux qu'une place au large.
    dists = [max(abs(x - 10.0), abs(y - 10.0)) for x, y in places]
    ordonne = dists == sorted(dists)

    ok = len(places) >= 24 and bool(diagonales) and bool(cardinales) and ordonne
    rec("test_le_coffre_d_evacuation_essaie_aussi_les_diagonales", ok,
        f"{len(places)} candidat(s) : {len(cardinales)} cardinale(s), "
        f"{len(diagonales)} diagonale(s), ordonné du plus proche : {ordonne}")
    assert ok


def test_la_foreuse_a_charbon_recoit_un_bras_de_retour() -> None:
    """UNE FOREUSE ASSISE SUR SON PROPRE COMBUSTIBLE NE DEVRAIT JAMAIS TOMBER À SEC.

    Défaut trouvé par HERMES lui-même, inscrit dans sa skill après sa 10e partie :

        « Une foreuse burner sur un gisement de charbon devrait s'auto-alimenter. Mais
        la chaîne fait tomber le charbon sur un convoyeur qui part vers le sud — sans
        bras de retour vers le réservoir de la foreuse. »

    Vérifié dans le code : `approvisionner` amorce la foreuse avec `AMORCE` (50 unités)
    et pose la belt vers la machine à nourrir, mais rien ne remet du charbon DANS la
    foreuse. Les cinquante brûlées, elle s'arrête — sur un gisement de charbon, avec un
    convoyeur plein qui la longe.

    C'est ce qui condamnait l'agent à ravitailler à la main : mesuré partie 13, la même
    foreuse rechargée QUATRE fois en trois minutes.

    Le bras prend sur la belt et dépose dans la foreuse — l'inverse exact de
    l'évacuation, et le même helper, qui LIT `pickup`/`drop` réels au lieu de déduire
    l'orientation d'une convention.
    """
    from agents.coordinator import Coordinator

    appels = []

    class _ApiRetour:
        def get_state(self):
            return {"inventory": {"burner-inserter": 1}, "tick": 1, "ready": True}

        def run_action(self, fn, *a, **kw):
            return {"ok": True}

        def move_items_at(self, *a, **kw):
            return {"ok": True}

    c = _coord_mesure(_ApiRetour())
    c.journal = []
    c._boucler_le_charbon = Coordinator._boucler_le_charbon.__get__(c)

    import services.site_finder as sf
    vrai = sf.place_inserter_vers
    sf.place_inserter_vers = lambda api, cible, source, cible_nom, **kw: (
        appels.append({"cible": cible, "source": source, "cible_nom": cible_nom,
                       "bras": kw.get("nom")}), (5.0, 5.0, "north"))[1]
    try:
        pose = c._boucler_le_charbon((10.0, 10.0), "burner-mining-drill", (12.0, 10.0))
    finally:
        sf.place_inserter_vers = vrai

    ok = (bool(appels) and pose is not None
          and appels[0]["cible"] == (10.0, 10.0)          # il dépose DANS la foreuse
          and appels[0]["source"] == (12.0, 10.0)         # il prend sur la belt
          and appels[0]["cible_nom"] == "burner-mining-drill"
          and "burner" in str(appels[0]["bras"]))
    rec("test_la_foreuse_a_charbon_recoit_un_bras_de_retour", ok,
        f"{appels[:1]}")
    assert ok


def test_l_evacuation_s_approche_avant_de_poser_son_coffre() -> None:
    """H12, JAMAIS APPLIQUÉ À L'ÉVACUATION — troisième variante du même défaut.

    Partie 15, mesuré sur la carte : « aucune place pour évacuer stone-furnace
    (72.5,58.5) », premier essai « coffre refusé ». Or vingt des vingt-quatre
    emplacements candidats étaient LIBRES — vérifié un par un avec `can_place`.

    Le refus ne venait pas du terrain. `can_place_check` utilise bien
    `build_check_type = manual` et prédit donc la pose... sauf sur un point : il ne
    vérifie PAS la distance au joueur. C'est `state_placing_at` qui refuse au-delà de
    `build_distance`, et cette borne-là ne se lit nulle part dans le test préalable.

    Le joueur était à 7,5 tuiles du four ; les candidats sont à 2,5 à 4,5 tuiles DU
    FOUR, donc jusqu'à douze tuiles de lui. `execute_micro` s'approche de chaque pose
    depuis H12 — mais `batir_evacuation` pose son coffre par `place_entity_at` direct,
    sans passer par l'executor, et n'a jamais reçu le correctif.

    `_approcher` existe déjà et sert à `ravitailler` : même geste, même contrainte.
    """
    from agents.coordinator import Coordinator

    approches = []

    class _ApiLoin:
        def get_state(self):
            return {"inventory": {"burner-inserter": 1, "wooden-chest": 1},
                    "tick": 1, "ready": True,
                    "character": {"position": {"x": 76.5, "y": 52.2}}}

        def run_action(self, fn, *a, **kw):
            return {"ok": False, "detail": "walk closer first"}

        def can_place_check(self, name, x, y, direction="north"):
            # Le terrain est LIBRE — c'est le point du test : le refus vient de la
            # distance au joueur, que ce contrôle ne mesure pas.
            return {"name": name, "x": x, "y": y, "can_place": True}

        def place_entity_at(self, *a, **kw):
            return {"ok": False}

        def remove_entity_at(self, *a, **kw):
            return {"ok": True}

    c = _coord_mesure(_ApiLoin())
    c.journal = []
    c._evacuations = {}
    c._approcher = lambda x, y, **kw: (approches.append((x, y)), True)[1]
    c.batir_evacuation = Coordinator.batir_evacuation.__get__(c)

    from services.factory_doctor import Symptome
    c.batir_evacuation(Symptome(name="stone-furnace", x=72.5, y=58.5,
                                cause="sortie_pleine", gravite=1, detail=""))

    ok = approches and approches[0] == (72.5, 58.5)
    rec("test_l_evacuation_s_approche_avant_de_poser_son_coffre", bool(ok),
        f"approche(s) demandée(s) : {approches[:2]}")
    assert ok


def test_l_alimentation_reutilise_la_foreuse_deja_posee() -> None:
    """ON NE REPOSE PAS CE QU'ON A DÉJÀ BÂTI — surtout au même endroit.

    Partie 16, mesuré : `reparer('approvisionner')` échoue en 113 secondes sur

        foreur non posé sur coal :
        [(0, 'burner-mining-drill', -65.0, -79.0, 'occupe par burner-mining-drill')]

    L'ancre est DÉTERMINISTE — même gisement, même échantillon, même calcul — donc le
    second passage vise exactement la tuile où le premier a posé sa foreuse. Elle tourne,
    elle extrait, et l'agent la déclare « emplacement occupé » puis renonce à toute
    l'alimentation.

    Une foreuse déjà en terre sur le bon gisement est un ACQUIS, pas un obstacle : on
    branche la belt dessus. C'est le même principe que `_englober` — ce qu'on vient de
    bâtir doit entrer dans ce qu'on observe — appliqué à la pose.
    """
    from agents.coordinator import Coordinator

    class _ApiDeja:
        def inspect_at(self, x, y, radius=0.5):
            # Une foreuse de la force, déjà posée sur le gisement, en train d'extraire.
            return {"entities": [
                {"name": "burner-mining-drill", "type": "mining-drill",
                 "x": -65.0, "y": -79.0, "status": "working", "mining": "coal",
                 "dropX": -66.5, "dropY": -79.5}]}

    c = _coord_mesure(_ApiDeja())
    c.journal = []
    c._foreuse_existante = Coordinator._foreuse_existante.__get__(c)

    trouvee = c._foreuse_existante((-65.0, -79.0), "burner-mining-drill")
    # Et sur un gisement vierge, rien : on pose comme avant.
    class _ApiVierge:
        def inspect_at(self, x, y, radius=0.5):
            return {"entities": []}
    c2 = _coord_mesure(_ApiVierge())
    c2.journal = []
    c2._foreuse_existante = Coordinator._foreuse_existante.__get__(c2)
    vierge = c2._foreuse_existante((10.0, 10.0), "burner-mining-drill")

    ok = (trouvee is not None and abs(trouvee[0] - (-65.0)) < 0.6
          and abs(trouvee[1] - (-79.0)) < 0.6 and vierge is None)
    rec("test_l_alimentation_reutilise_la_foreuse_deja_posee", ok,
        f"deja posée -> {trouvee} ; gisement vierge -> {vierge}")
    assert ok


def test_la_portee_d_alimentation_n_est_pas_plafonnee_par_nous() -> None:
    """LE DÉTERMINISTE CALCULE LE COÛT, L'AGENT DÉCIDE S'IL LE PAIE.

    `BELTS_FABRICABLES = 20` était un arbitrage que le code s'arrogeait : mesuré que
    forger 73 belts prenait une heure, j'en avais fait une limite en dur. Or « est-ce
    que ça vaut 240 plaques de dérouler 80 tuiles de convoyeur ? » n'est pas un calcul,
    c'est un choix — et c'est celui de l'agent, comme le placement est celui du
    déterministe.

    Partie 16, la conséquence : « aucun gisement de coal à moins de 40 tuiles » alors
    que la chaîne de charbon tournait à 80 tuiles avec 671 unités extraites. Le charbon
    existait, l'usine mourait de faim, et le refus venait d'un seuil que personne
    n'avait choisi.

    L'écart entre gisements DÉPEND DE LA CARTE : si elle les éloigne, il n'y a pas
    d'alternative à une longue ligne. Refuser au nom d'un plafond revient à condamner
    l'usine pour une génération de terrain qu'on ne maîtrise pas.

    `budget_belts` rend donc la décision à l'appelant : sans lui, on reste prudent
    (comportement inchangé) ; avec, l'agent dit ce qu'il accepte de payer.
    """
    from agents.coordinator import Coordinator

    class _ApiStock:
        def get_state(self):
            return {"inventory": {"transport-belt": 20}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiStock())
    c.journal = []
    c._portee_appro = Coordinator._portee_appro.__get__(c)

    prudent = c._portee_appro()                       # 20 en poche + la marge par défaut
    choisi = c._portee_appro(budget_belts=100)        # l'agent accepte d'en forger 100

    ok = choisi >= 100 and choisi > prudent
    rec("test_la_portee_d_alimentation_n_est_pas_plafonnee_par_nous", ok,
        f"sans budget : {prudent:.0f} tuiles ; budget 100 belts : {choisi:.0f} tuiles")
    assert ok


def test_on_ne_pose_pas_de_coffre_sur_une_machine_deja_evacuee() -> None:
    """UN COFFRE DE PLUS SUR UNE SORTIE QUI MARCHE DÉJÀ NE SERT À RIEN.

    Vu à l'écran partie 16, puis mesuré sur la carte autour du four (6,-32) :

        burner-inserter (4.5,-32.5) pickup=belt  -> drop=four    ENTRÉE
        burner-inserter (7.5,-32.5) pickup=four  -> drop=belt    SORTIE

    Le planner pose déjà le bras qui vide la machine de tête vers un convoyeur. Lui
    ajouter un coffre et un second bras ne débouche rien : la sortie n'était pas
    bouchée. Quatre correctifs de la journée (H20, H21, H25, H27) ont servi à faire
    fonctionner un geste qui, sur une chaîne bâtie par le planner, n'a pas lieu d'être.

    `batir_evacuation` garde tout son sens pour `reparer` sur une machine ISOLÉE — un
    four de fusion posé à la main, une assembleuse en `full_output`. La garde va donc
    dans `_evacuer_les_tetes`, à l'endroit où l'on décide d'y recourir automatiquement,
    et non dans la méthode elle-même.

    Un inserteur qui PUISE dans la machine est la signature d'une sortie desservie —
    même règle que `en_service` côté mod, qui reconnaît une machine servie à ce qu'un
    bras pointe dessus.
    """
    from agents.coordinator import Coordinator

    appels = []

    class _Ent:
        def __init__(self, role, node_item):
            self.role, self.node_item = role, node_item

    class _Pose:
        def __init__(self, idx, x, y):
            self.idx, self.x, self.y, self.name = idx, x, y, "stone-furnace"

    class _Plan:
        def __init__(self, ents):
            self.entities = ents

    class _Rap:
        def __init__(self, poses):
            self.placed = poses

    class _ApiSortie:
        def inspect_at(self, x, y, radius=0.5):
            # Le bras de sortie du planner : il PUISE dans le four (6,-32).
            return {"entities": [
                {"name": "burner-inserter", "type": "inserter", "x": 7.5, "y": -32.5,
                 "pickupX": 6.5, "pickupY": -32.5, "dropX": 8.6, "dropY": -32.5}]}

    c = _coord_mesure(_ApiSortie())
    c.journal = []
    c.batir_evacuation = lambda cible, coffre="wooden-chest": (
        appels.append(cible.name), (True, "posé"))[1]
    c._evacuer_les_tetes = Coordinator._evacuer_les_tetes.__get__(c)

    vidées, motif = c._evacuer_les_tetes(
        _Plan([_Ent("machine", "iron-plate")]), _Rap([_Pose(0, 6.0, -32.0)]), "iron-plate")

    ok = appels == [] and "déjà" in motif
    rec("test_on_ne_pose_pas_de_coffre_sur_une_machine_deja_evacuee", ok,
        f"appels à batir_evacuation : {appels} — motif « {motif.strip()[:60]} »")
    assert ok


def test_un_refus_de_terrain_dit_ce_qui_bloque() -> None:
    """« OBSTACLE_BLOCKING » NE DIT NI QUOI NI OÙ — l'agent ne peut rien en faire.

    Partie 18, mesuré : `batir_une_chaine` refuse trois fois en six secondes sur
    « non implantable : obstacle_blocking ». Hermes fait alors ce qu'il faut — il se
    déplace jusqu'au gisement et appelle `regarder` — et ne trouve **aucune entité**.
    `can_place_check` répond d'ailleurs True pour une foreuse à la tuile visée.

    Le refus est pourtant légitime : le planner teste CHAQUE entité du plan contre le
    terrain, et une seule qui tombe sur un obstacle condamne les vingt-neuf. Mais il
    garde pour lui ce qu'il a vu. Ses notes portent
    « obstacle_blocking:per_entity:4 hits kind=water » — le nombre ET la nature — et
    rien de tout cela n'atteint l'agent.

    La différence est décisive pour lui : de l'eau, il faut changer de gisement ; des
    arbres, il faut dégager. Sans le savoir, il ne peut que réessayer à l'identique.
    """
    from agents.coordinator import Coordinator

    class _PlanParEntite:
        feasibility = "obstacle_blocking"
        notes = ["ratio:1.0", "obstacle_blocking:per_entity:4 hits kind=water"]
        entities = []

    # LE PLANNER A DEUX FORMATS DE NOTE, et n'en traiter qu'un laisse l'autre muet.
    # Mesuré partie 19 : le refus est reste « non implantable : obstacle_blocking »
    # tout court, parce que la branche `terrain_check=False` ecrit une bbox et non
    # un `kind=`. Un diagnostic qui ne couvre qu'un de ses propres formats se tait
    # exactement quand l'autre survient.
    class _PlanBbox:
        feasibility = "obstacle_blocking"
        notes = ["obstacle_blocking: bbox étage intersecte obstacle (12, -4, 15, -1)"]
        entities = []

    c = _coord_mesure(None)
    par_entite = Coordinator._motif_de_refus(c, _PlanParEntite(), "iron-plate")
    par_bbox = Coordinator._motif_de_refus(c, _PlanBbox(), "iron-plate")

    ok = ("water" in par_entite and "4" in par_entite
          and "12" in par_bbox and len(par_bbox) > 60)
    rec("test_un_refus_de_terrain_dit_ce_qui_bloque", ok,
        f"per_entity « {par_entite[:55]} » | bbox « {par_bbox[:55]} »")
    assert ok


def test_le_nombre_d_alimentations_revient_a_l_agent() -> None:
    """DEUXIÈME PLAFOND QUE JE M'ÉTAIS ARROGÉ — même faute que pour les belts.

    Partie 19, meilleur score de la session et sa limite dans la même ligne :

        4 alimentée(s) en coal — alimentation bornée à 4 : d'autres brûleurs restent à sec

    Vingt-trois machines, onze affamées. `ALIMENTATIONS_MAX = 4` venait d'une mesure
    juste — chaque alimentation coûtait une marche jusqu'au gisement — dont j'avais fait
    une règle au lieu d'une information.

    Cette mesure ne vaut d'ailleurs plus : depuis H29, les alimentations suivantes
    REPRENNENT la foreuse à charbon déjà posée au lieu d'en reposer une. Le coût marginal
    qui justifiait le plafond a disparu avec le correctif.

    Le plafond reste comme DÉFAUT prudent — sans consigne, on ne déroule pas dix lignes.
    Mais l'agent peut demander autre chose : c'est son arbitrage, comme le budget de
    belts depuis H29.
    """
    from agents.coordinator import Coordinator

    class _Pose:
        def __init__(self, i):
            self.name, self.x, self.y = "burner-mining-drill", float(i) * 4, 0.0
            self.role = "drill"

    def _compte(limite):
        servies = []
        api = _ApiEnergie({"burner-mining-drill": "burner"})
        c = _coord_mesure(api)
        c.journal = []
        c.approvisionner = lambda cible, item="coal", **kw: (servies.append(cible.name),
                                                       (True, "ok"))[1]
        c._gaver_les_bruleurs = lambda poses: 0
        c._mettre_en_service = Coordinator._mettre_en_service.__get__(c)
        c._mettre_en_service([_Pose(i) for i in range(9)], alimentations_max=limite)
        return len(servies)

    defaut = _compte(None)          # prudence inchangée
    demande = _compte(9)            # l'agent veut tout alimenter

    ok = defaut == Coordinator.ALIMENTATIONS_MAX and demande == 9
    rec("test_le_nombre_d_alimentations_revient_a_l_agent", ok,
        f"sans consigne : {defaut} ; demande 9 : {demande}")
    assert ok


def test_la_centrale_forge_ce_qui_lui_manque() -> None:
    """LE PALIER ÉLECTRIQUE ÉCHOUAIT SUR LE DÉFAUT DÉJÀ CORRIGÉ TROIS FOIS.

    Partie 19 : Hermes franchit enfin l'étape 7 — il débloque `electronics`, vérifie que
    `steam-power` est acquise, et appelle `batir_une_centrale` de lui-même. Réponse en
    une demi-seconde :

        ÉCHEC — centrale non bâtie : missing={'boiler': 1, 'coal': 7,
                'steam-engine': 1, 'small-electric-pole': 2, 'offshore-pump': 1}

    `batir_une_chaine` se procure ce qui lui manque depuis longtemps — c'est même écrit
    dans la skill : « elle fabrique elle-même ce qui lui manque, n'attends pas d'avoir
    assez de matériel ». La centrale, elle, lisait l'inventaire et renonçait.

    Quatrième occurrence du même motif après la foreuse (H15), le bras (H20) et le
    coffre (H21). Et la plus coûteuse : elle bloque la sortie du palier burner, donc la
    fin du problème de charbon sur lequel butent huit correctifs.
    """
    from agents.coordinator import Coordinator

    demandes = []

    class _ApiVide:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiVide())
    c.journal = []
    c.fabriquer = lambda item, combien=1: (demandes.append((item, combien)),
                                           (True, "forgé"))[1]
    c._assurer_stock = Coordinator._assurer_stock.__get__(c)
    c._recolte_faite = True          # la récolte est éprouvée ailleurs (H23/H24)

    manque = {"boiler": 1, "steam-engine": 1, "small-electric-pole": 2,
              "offshore-pump": 1, "coal": 7}
    c._forger_le_manque(manque)

    forges = {i for i, _ in demandes}
    pieces = {"boiler", "steam-engine", "small-electric-pole", "offshore-pump"} <= forges

    # `missing` DIT LE MANQUE, `fabriquer` VISE UN TOTAL — les confondre ne produit rien.
    # Mesuré partie 19 : « se_procurer coal : rien à faire, l'inventaire en contient déjà
    # assez », puis « centrale non bâtie : missing={'coal': 12} » à la seconde suivante.
    # L'agent en avait vingt en poche et il en fallait douze DE PLUS.
    demandes.clear()
    c2 = _coord_mesure(type("_A", (), {
        "get_state": lambda self: {"inventory": {"coal": 20}, "tick": 1, "ready": True}})())
    c2.journal = []
    c2.fabriquer = lambda item, combien=1: (demandes.append((item, combien)),
                                            (True, "forgé"))[1]
    c2._assurer_stock = Coordinator._assurer_stock.__get__(c2)
    c2._recolte_faite = True
    c2._forger_le_manque({"coal": 12})
    vise = dict(demandes).get("coal")

    ok = pieces and vise == 32          # 20 en poche + 12 manquants
    rec("test_la_centrale_forge_ce_qui_lui_manque", ok,
        f"pièces forgées : {sorted(forges)} — coal visé : {vise} (attendu 32)")
    assert ok


def test_on_recolte_a_nouveau_quand_l_usine_a_reproduit() -> None:
    """UNE SEULE RÉCOLTE PAR PARTIE NE SUFFIT PAS — l'usine, elle, continue.

    Partie 22, constat de l'utilisateur en regardant jouer : l'agent mine du fer à la
    main alors que son usine en a produit 381. Mesuré au même instant :

        produit par l'usine : 381 plaques
        en poche du joueur  :  10 plaques

    H23/H24 récolte bien les fours avant de miner, mais je l'avais bridé d'un drapeau
    `_recolte_faite` — une seule fois par partie — au motif que « vider les fours à
    chaque craft coûterait plus que cela ne rapporte ». Arbitrage non mesuré, et faux :
    la première récolte vide ce qui existait alors, l'usine produit encore pendant une
    heure, et l'agent repart à la pioche devant ses propres réserves.

    Le coût réel d'une récolte est un `inspect_at` et quelques `empty_output_at` : des
    secondes. Le coût de ne pas récolter est mesuré ici — une expédition minière alors
    que trois cent soixante-et-onze plaques dorment à trente tuiles.

    On garde un intervalle pour ne pas vider les fours à chaque craft d'une rafale, mais
    il se compte en secondes, pas en parties.
    """
    from agents.coordinator import Coordinator

    recoltes = []

    class _ApiUsine:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiUsine())
    c.journal = []
    c.builder = None
    c._fabrications = {}
    c._recolter_la_production = lambda item: (recoltes.append(item), 0)[1]
    c.fabriquer = Coordinator.fabriquer.__get__(c)

    faux_temps = [1000.0]
    import agents.coordinator as mod
    vrai = mod.time.monotonic if hasattr(mod, "time") else None

    for instant in (1000.0, 1005.0, 1200.0):      # tout de suite, 5 s après, 200 s après
        faux_temps[0] = instant
        c._horloge = lambda: faux_temps[0]
        try:
            c.fabriquer("burner-mining-drill", 2)
        except Exception:
            pass

    # Première récolte : oui. Cinq secondes après : non (rafale). Deux cents plus tard : OUI.
    ok = len(recoltes) >= 2 * len(Coordinator.MATIERES_RECOLTABLES)
    rec("test_on_recolte_a_nouveau_quand_l_usine_a_reproduit", ok,
        f"{len(recoltes)} récolte(s) sur trois tentatives espacées de 0 s, 5 s et 200 s")
    assert ok


def test_la_ligne_forge_ses_poteaux_et_ne_ment_pas_sur_la_cause() -> None:
    """LA CENTRALE DE LA PARTIE 22 ÉTAIT ISOLÉE : 40 tuiles, un seul poteau.

        11:49:24  batir_une_centrale  OK — centrale bâtie (12 entités) à 40 tuiles,
                                      ligne de 1 poteaux (INTERROMPUE)

    Vérifié dans le jeu au même instant : steam-engine `working`, boiler `full_output`
    avec 45 charbons, offshore-pump `working`, réseau id=1, production 0 kW. Ce n'est pas
    une panne — un générateur ne produit que ce qui est consommé, et rien n'était branché.

    Deux défauts, dont le second est le pire :

    1. `place_pole_line` ne regarde jamais l'inventaire. H35 a appris à la centrale à
       forger `boiler`, `steam-engine` et `offshore-pump` quand ils manquent, mais s'est
       arrêté aux pièces : les poteaux de la ligne, eux, ne sont pas forgés. L'agent avait
       deux poteaux en poche pour quarante tuiles à couvrir.

    2. Le rapport annonce « INTERROMPUE par un obstacle ». Il n'y avait pas d'obstacle :
       il n'y avait plus de poteaux. Une fausse cause envoie chercher un contournement de
       terrain qui n'existe pas — c'est le défaut que je reproche partout ailleurs.

    On exige donc : la ligne forge de quoi couvrir la distance AVANT de tracer, et quand
    elle s'arrête quand même, elle dit laquelle des deux causes l'a arrêtée.
    """
    forges = []

    class _ApiLigne:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiLigne())
    c.journal = []
    c.zone = (40.0, 0.0)                      # 40 tuiles de la centrale

    def _forge(nom, combien=1):
        forges.append((nom, combien))
        return True, ""
    c._assurer_stock = _forge

    import services.site_finder as sf
    vrai = sf.place_pole_line
    sf.place_pole_line = lambda api, dep, arr, **kw: ([(0.5, 0.5)], False)
    try:
        ligne, complete, cause = c._relier_la_centrale((0.0, 0.0))
    finally:
        sf.place_pole_line = vrai

    # Assez de poteaux pour 40 tuiles à 6 de pas : au moins 6, pas 2.
    forge = [n for n, _ in forges].count("small-electric-pole")
    assez = any(n == "small-electric-pole" and q >= 6 for n, q in forges)
    honnete = "poteau" in (cause or "").lower()

    ok = forge >= 1 and assez and honnete
    rec("test_la_ligne_forge_ses_poteaux_et_ne_ment_pas_sur_la_cause", ok,
        f"forgé={forges} cause={cause!r}")
    assert ok


def test_un_manque_se_dit_en_phrase_et_pas_en_dict_python() -> None:
    """LE MESSAGE D'ÉCHEC EST UNE INSTRUCTION — il doit se lire, pas se décoder.

    Partie 22, l'agent bute sur un charbon et les deux outils se parlent sans s'entendre :

        11:48:54  batir_une_centrale  ÉCHEC — missing={'coal': 1} blocked=[]
        11:48:56  se_procurer coal 5  ÉCHEC — rien à faire, l'inventaire en contient assez
        11:48:58  batir_une_centrale  ← il recommence

    Les deux disent vrai. `missing` annonce un MANQUE de 1, `se_procurer` attend un TOTAL
    de 5 ; il en avait déjà cinq, donc l'outil n'a rien fait et a répondu « tu en as assez »
    pendant que la centrale répétait qu'il en manquait. C'est le piège H36 exactement, mais
    subi par l'agent au lieu de l'auteur — et la sortie brute le lui tend.

    Un `repr` de dict oblige à savoir que la clé est un nom d'objet, que la valeur est un
    delta et non une cible, et que `blocked=[]` signifie « rien ne gêne ». Rien de tout
    cela n'est écrit. On le dit donc en toutes lettres, avec le TOTAL à viser — le nombre
    qu'il faut passer à `se_procurer` — plutôt que le delta qu'il faut convertir.
    """
    from agents.coordinator import Coordinator

    class _ApiManque:
        def get_state(self):
            return {"inventory": {"coal": 5}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiManque())
    phrase = Coordinator._dire_le_manque(c, {"coal": 1}, [])

    dit_quoi = "coal" in phrase
    dit_le_total = "6" in phrase              # 5 en poche + 1 manquant
    pas_de_jargon = "{" not in phrase and "missing=" not in phrase

    ok = dit_quoi and dit_le_total and pas_de_jargon
    rec("test_un_manque_se_dit_en_phrase_et_pas_en_dict_python", ok, repr(phrase))
    assert ok


def test_l_arret_mord_aussi_pendant_la_fabrication() -> None:
    """UN ARRÊT QUI N'ARRÊTE QUE LA POSE N'ARRÊTE PRESQUE RIEN.

    Partie 25, mesuré au journal :

        16:46:45  arreter_le_chantier          (le joueur venait de le corriger)
        16:47:55  chantier n°2 EN COURS depuis 4 min 17 s — arrêt demandé, il finit
                  sa pose en cours

    Une minute dix après la demande, il tourne toujours. Le point de sortie posé en H42 est
    dans la boucle de POSE de l'executor — or `batir_chaine` passe l'essentiel de son temps
    à FORGER ce qui lui manque, en minant et fondant. Tant qu'il fabrique, il ne croise
    aucun point d'arrêt.

    C'est le défaut de H10, H23 et H27 pour la quatrième fois : un correctif juste posé sur
    un seul des chemins qui y mènent. On le pose donc là où le temps se passe réellement,
    entre deux pièces forgées — et l'arrêt reste PROPRE : la pièce en cours se termine,
    ce qui est acquis reste acquis, la relance reprend où l'on s'était arrêté.
    """
    from agents.coordinator import Coordinator

    forges = []

    class _ApiForge:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiForge())
    c.journal = []
    stop = {"oui": False}
    c.interrompu_par = lambda: stop["oui"]

    def _forge(nom, combien=1):
        forges.append(nom)
        if len(forges) == 2:
            stop["oui"] = True          # le joueur demande l'arrêt à la 2e pièce
        return True, ""
    c._assurer_stock = _forge

    manque = {"burner-mining-drill": 2, "transport-belt": 4,
              "burner-inserter": 1, "stone-furnace": 1, "iron-chest": 1}
    reste = Coordinator._forger_le_manque(c, manque)

    sorti_tot = len(forges) < len(manque)
    a_forge_avant = len(forges) >= 2
    le_dit = "interrompu" in str(reste).lower() or "arrêt" in str(reste).lower()

    ok = sorti_tot and a_forge_avant and le_dit
    rec("test_l_arret_mord_aussi_pendant_la_fabrication", ok,
        f"{len(forges)}/{len(manque)} forgée(s) avant sortie — dit={le_dit} ({reste!r})")
    assert ok


def test_le_plafond_de_belts_ne_doit_pas_decider_a_la_place_de_l_agent() -> None:
    """« AUCUN GISEMENT DE COAL À MOINS DE 20 TUILES » — sur des cartes où il est à 90.

    Partie 34, rapport du chantier, après dix minutes de forge :

        29 entité(s) posée(s) … 0 alimentée(s) en coal
        — alimentation refusée : aucun gisement de coal à moins de 20 tuiles
          (0 belt(s) en poche)

    Ces vingt tuiles ne sont pas une distance mesurée : c'est `BELTS_FABRICABLES = 20`,
    un plafond que le code s'arroge. Le charbon est à 80-100 tuiles sur ces cartes, donc
    l'alimentation est refusée SYSTÉMATIQUEMENT, quelle que soit la partie. L'usine tourne
    alors sur le charbon porté à la main — la corvée même que l'objectif désigne comme la
    ligne d'arrivée : « tant que c'est toi qui portes le charbon, rien ne tient sans toi ».
    Zéro foreuse à charbon après deux chantiers complets, mesuré.

    Le message ajoute l'injure au dommage : « rappelle avec `budget_belts` si tu en veux
    davantage » — or `budget_belts` n'existe que sur `reparer`, pas sur `batir_une_chaine`.
    L'agent lit un remède qu'il ne peut pas appliquer.

    Ce que le joueur répète depuis deux jours : écrire le FAIT et son COÛT, jamais l'ordre
    qu'on en tire. Une ligne de 90 tuiles coûte 270 plaques — c'est une information, et
    l'arbitrage appartient à l'agent, qui seul sait s'il les a et s'il en veut.
    """
    from agents.coordinator import Coordinator

    class _ApiVide:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiVide())

    # Sans budget dit, le plafond ne doit plus enfermer : la portée suit ce que l'agent
    # possède, et le refus doit CHIFFRER ce qu'il faudrait plutôt que trancher.
    portee_defaut = Coordinator._portee_appro(c)
    portee_dite = Coordinator._portee_appro(c, budget_belts=90)

    ecoute_l_agent = portee_dite >= 90
    ne_plafonne_pas_a_20 = portee_defaut != 20.0

    ok = ecoute_l_agent and ne_plafonne_pas_a_20
    rec("test_le_plafond_de_belts_ne_doit_pas_decider_a_la_place_de_l_agent", ok,
        f"portée par défaut={portee_defaut} (20 = plafond arrogé) — "
        f"avec budget 90 = {portee_dite}")
    assert ok


def test_avoir_deja_ce_qu_on_demande_n_est_pas_un_echec() -> None:
    """« ÉCHEC — wood : rien à faire, l'inventaire en contient déjà assez ».

    Partie 35, mesuré : le joueur demande une foreuse et un coffre pour le charbon.
    L'agent fabrique la foreuse, puis demande le bois du coffre — et lit un ÉCHEC alors
    qu'il a déjà ce bois en poche. Rien n'a manqué, rien n'a raté : l'objectif était
    atteint avant l'appel.

    Le mot compte, parce qu'il décide de la suite. Un agent qui lit « échec » cherche une
    cause, une contournement, ou renonce — c'est ce que la skill lui demande de faire d'un
    échec. Ici il n'y a rien à contourner. La même phrase, classée réussite, lui dit
    simplement « passe à l'étape suivante ».

    C'est le pendant du piège H36/H41 : `se_procurer` vise un TOTAL, pas un delta. En
    demander autant qu'on possède déjà est donc le cas NORMAL quand on vérifie ses pièces
    avant d'agir — précisément ce qu'on lui recommande de faire.
    """
    from agents.coordinator import Coordinator

    class _ApiPleine:
        def get_state(self):
            return {"inventory": {"wood": 4}, "tick": 1, "ready": True}

    c = _coord_mesure(_ApiPleine())
    c.journal = []
    c._fabrications = {}
    c._derniere_recolte = 1e9          # pas de récolte parasite dans ce banc
    c._horloge = lambda: 1e9

    ok, detail = Coordinator.fabriquer(c, "wood", 1)

    reussite = ok is True
    le_dit = "déjà" in detail or "en poche" in detail
    pas_alarmant = "échec" not in detail.lower()

    resultat = reussite and le_dit and pas_alarmant
    rec("test_avoir_deja_ce_qu_on_demande_n_est_pas_un_echec", resultat,
        f"ok={ok} — « {detail} »")
    assert resultat


def test_la_recolte_va_CHERCHER_les_plaques_au_lieu_de_les_attendre() -> None:
    """CENT PLAQUES DANS LE FOUR, ET IL MINE DU MINERAI À LA MAIN À VINGT-SEPT TUILES.

    Partie 35, mesuré pendant que le joueur regardait :

        position : (20,-45) immobile — delta 10 s : iron-ore +5
        son four : (14,-18) no_ingredients, 100 PLAQUES en sortie

    H38 devait justement récolter les fours avant de miner. Il ne s'est pas déclenché :
    la récolte CHERCHE bien les machines autour de la zone d'usine, mais `empty_output_at`
    exige d'être à portée de bras — une dizaine de tuiles. À vingt-sept, elle ne vide
    rien, ne dit rien, et la fabrication enchaîne sur le minage.

    Le joueur l'avait déjà signalé partie 22 (« 381 plaques produites, 10 en poche »).
    J'avais corrigé pour un agent RESTÉ PRÈS DE SON USINE ; dès qu'un chantier l'emmène
    ailleurs — bâtir une centrale, par exemple — le correctif redevient inopérant. Même
    oubli que H12 et H27 : agir sans s'approcher.

    On va donc CHERCHER : si le four est hors de portée, on marche jusqu'à lui. Cent
    plaques valent quelques secondes de marche ; les miner à la pioche en coûte des
    minutes.
    """
    from agents.coordinator import Coordinator

    marches, vidages = [], []

    class _ApiLoin:
        def get_state(self):
            return {"inventory": {"iron-plate": 0},
                    "character": {"position": {"x": 20.0, "y": -45.0}}, "tick": 1}
        def inspect_at(self, x, y, r=0.5):
            return {"entities": [{"name": "stone-furnace", "type": "furnace",
                                  "x": 14.0, "y": -18.0}]}
        def empty_output_at(self, x, y, nom, **kw):
            vidages.append((x, y))
            return {"ok": True}
        def run_action(self, fn, *a, **kw):
            return fn(*a, **kw)

    import services.perception as _perc
    _vrai_parc = _perc.parc
    _perc.parc = lambda api: [{"name": "stone-furnace", "type": "furnace",
                               "x": 14.0, "y": -18.0}]

    c = _coord_mesure(_ApiLoin())
    c.zone = (14.0, -18.0)
    c.rayon = 25.0
    c._marcher = lambda x, y: (marches.append((x, y)), (x, y))[1]

    try:
        Coordinator._recolter_la_production(c, "iron-plate")
    finally:
        _perc.parc = _vrai_parc

    a_marche = bool(marches)
    a_vide = bool(vidages)

    ok = a_marche and a_vide
    rec("test_la_recolte_va_CHERCHER_les_plaques_au_lieu_de_les_attendre", ok,
        f"marches={marches} vidages={vidages}")
    assert ok


def test_la_recolte_ne_se_limite_pas_a_un_rayon_arbitraire() -> None:
    """POURQUOI LIMITER LA PORTÉE ? — la question du joueur, et elle est juste.

    La récolte cherchait les fours dans `self.rayon` (25 tuiles) autour de `self.zone`,
    une position FIGÉE à la création du Coordinator — celle du personnage au démarrage.
    Deux plafonds pour le prix d'un : au-delà de vingt-cinq tuiles elle ne voit rien, et
    si l'usine se bâtit ailleurs que là où l'on se tenait au début, elle ne voit rien non
    plus. Dans les deux cas, silence : ni plaques récoltées, ni motif donné.

    Rien ne justifie ce rayon. Ce n'est pas une contrainte du jeu — contrairement à la
    portée de bras, qui est réelle et vaut une dizaine de tuiles — c'est un nombre que le
    code s'arroge. Et `perception.parc()` liste déjà toutes les machines de la surface,
    sans limite.

    On cherche donc PARTOUT et l'on marche jusqu'à ce qu'on trouve. La seule vraie borne
    reste physique : il faut être à portée pour vider, d'où le déplacement de H62.
    """
    from agents.coordinator import Coordinator

    vidages = []

    class _ApiVaste:
        def get_state(self):
            return {"inventory": {"iron-plate": 0},
                    "character": {"position": {"x": 0.0, "y": 0.0}}, "tick": 1}
        def inspect_at(self, x, y, r=0.5):
            return {"entities": []}          # le rayon ne voit RIEN : four trop loin
        def empty_output_at(self, x, y, nom, **kw):
            vidages.append((x, y))
            return {"ok": True}
        def run_action(self, fn, *a, **kw):
            return fn(*a, **kw)

    import services.perception as perc
    vrai_parc = perc.parc
    # Le four est à 300 tuiles : hors de tout rayon, mais bien dans le parc.
    perc.parc = lambda api: [{"name": "stone-furnace", "type": "furnace",
                              "x": 300.0, "y": -200.0}]

    c = _coord_mesure(_ApiVaste())
    c.zone = (0.0, 0.0)
    c.rayon = 25.0
    c._marcher = lambda x, y: (x, y)
    try:
        Coordinator._recolter_la_production(c, "iron-plate")
    finally:
        perc.parc = vrai_parc

    ok = vidages == [(300.0, -200.0)]
    rec("test_la_recolte_ne_se_limite_pas_a_un_rayon_arbitraire", ok,
        f"vidages={vidages} (attendu le four a 300 tuiles)")
    assert ok


def test_ravitailler_donne_une_dose_et_ne_vide_pas_la_poche() -> None:
    """« TU AS PLACÉ TOUT LE CHARBON DANS LA FOREUSE » — le joueur, partie 36.

    Mesuré au même instant :

        foreuse : fuel = 35
        four    : fuel = 0, no_fuel, 28 minerais en attente
        joueur  : coal = 14

    Il avait quarante charbons. Le premier ravitaillement en a versé trente-cinq dans la
    foreuse — `move_items_at` avec un plafond de CINQUANTE — et il n'est rien resté pour
    le four. L'agent a conclu de bonne foi à une panne : « le four reste sans charbon
    malgré 3 ravitaillements ». Aucun outil ne mentait ; le premier servi avait tout pris.

    `_gaver_les_bruleurs` répartit pourtant déjà (`part = disponible // len(bruleurs)`).
    C'est `reparer ravitailler`, appelé machine par machine, qui ignorait les suivantes.

    Une dose suffit : `CHARBON_PAR_BRULEUR` vaut vingt-cinq, soit près d'une minute de
    marche pour un brûleur. Et l'on DIT ce qui reste en poche — l'agent saura rappeler,
    c'est son arbitrage, pas une limite qu'on lui impose.
    """
    from agents.coordinator import Coordinator
    from services.factory_doctor import Symptome

    verses = []

    class _ApiDose:
        def get_state(self):
            return {"inventory": {"coal": 40}, "tick": 1,
                    "character": {"position": {"x": 0.0, "y": 0.0}}}
        def move_items_at(self, item, nom, x, y, count, vers, **kw):
            verses.append(count)
            return {"ok": True}
        def run_action(self, fn, *a, **kw):
            return fn(*a, **kw)

    c = _coord_mesure(_ApiDose())
    c._ravitaillements = {}
    c._approcher = lambda x, y: True
    cible = Symptome(name="burner-mining-drill", x=0.0, y=0.0,
                     cause="sans_combustible", gravite=3, detail="")

    from agents.coordinator import Decision
    ok, detail = Coordinator.agir(c, Decision(action="ravitailler", raison="banc",
                                              cible=cible))

    une_dose = verses and verses[0] <= c.CHARBON_PAR_BRULEUR
    laisse_de_quoi = verses and verses[0] < 40
    dit_le_reste = "reste" in detail.lower() or "poche" in detail.lower()

    resultat = bool(une_dose and laisse_de_quoi and dit_le_reste)
    rec("test_ravitailler_donne_une_dose_et_ne_vide_pas_la_poche", resultat,
        f"versé={verses} (dose max {c.CHARBON_PAR_BRULEUR}) — « {detail} »")
    assert resultat


def test_batir_une_chaine_signale_ce_qu_elle_laisse_orphelin() -> None:
    """« POURQUOI IL A CRÉÉ UN AUTRE FOUR ? » — parce que le plan ignore l'existant.

    Partie 36, mesuré après que le joueur l'a vu :

        stone-furnace(-78,-16)  no_fuel  fuel=0   ← l'extraction, 28 minerais en attente
        stone-furnace(-74,-22)  working  fuel=4   ← posé par le chantier, six tuiles plus loin
        burner-mining-drill(-78,-18)  waiting_for_space_in_destination

    `FactoryBuilder` planifie sans savoir ce qui est déjà en terre : il pose son plan
    entier, et l'extraction minimale devient orpheline. Deux montages côte à côte, aucun
    complet, et une foreuse qui sature faute de pouvoir déposer.

    Intégrer l'existant au plan est un chantier de fond — le planificateur raisonne sur du
    terrain vierge. Ce qu'on peut faire sans mentir, c'est le DIRE : nommer les machines
    laissées de côté et leur position. L'agent décide alors s'il les démonte pour
    récupérer les pièces, les ravitaille, ou les laisse. C'est son arbitrage ; le nôtre
    est de ne pas le laisser croire que tout ce qui tourne appartient à sa chaîne.
    """
    from agents.coordinator import Coordinator

    class _ApiDeuxMontages:
        def get_state(self):
            return {"inventory": {}, "tick": 1}

    import services.perception as _perc
    _vrai = _perc.parc
    _perc.parc = lambda api: [
        {"name": "stone-furnace", "type": "furnace", "x": -78.0, "y": -16.0,
         "status": "no_fuel"},
        {"name": "burner-mining-drill", "type": "mining-drill", "x": -78.0, "y": -18.0,
         "status": "waiting_for_space_in_destination"},
    ]
    try:
        c = _coord_mesure(_ApiDeuxMontages())
        # Ce que le chantier vient de poser, loin des deux machines ci-dessus.
        class _P:
            def __init__(self, x, y, name):
                self.x, self.y, self.name, self.role = x, y, name, "machine"
        posees = [_P(-74.0, -22.0, "stone-furnace")]
        texte = Coordinator._dire_les_orphelines(c, posees)
    finally:
        _perc.parc = _vrai

    nomme = "stone-furnace" in texte and "-78" in texte
    dit_le_souci = "sature" in texte.lower() or "orphelin" in texte.lower()         or "hors de la chaîne" in texte.lower()

    ok = bool(nomme and dit_le_souci)
    rec("test_batir_une_chaine_signale_ce_qu_elle_laisse_orphelin", ok, repr(texte))
    assert ok


def test_repartir_de_zero_recupere_l_existant_avant_de_batir() -> None:
    """DEUX SITUATIONS OPPOSÉES, UN SEUL OUTIL — donc un CHOIX, et il revient à l'agent.

    Le joueur : « certaines fois il vaut mieux tout supprimer et repartir de zéro,
    notamment avec l'avancée technologique, mais d'autres fois c'est juste une extension ».

    Les deux cas sont réels. Passer du burner à l'électrique rend les anciennes machines
    non seulement inutiles mais nuisibles : elles occupent le gisement, réclament du
    charbon, et leurs pièces valent mieux dans la poche que dans le sol. À l'inverse,
    agrandir une usine qui tourne ne doit rien casser.

    Jusqu'ici le chantier faisait ni l'un ni l'autre : il bâtissait À CÔTÉ. Partie 36,
    mesuré — une extraction laissée orpheline, sa foreuse saturée, et un four neuf à six
    tuiles de là (cf. `_dire_les_orphelines`).

    `repartir_de_zero` tranche, et c'est l'agent qui tranche : on DÉMONTE les machines de
    production existantes — ce qui rend leurs pièces — puis on bâtit. Le défaut reste
    l'extension, parce que détruire ce qui produit est irréversible et ne doit jamais
    arriver par surprise.
    """
    from agents.coordinator import Coordinator

    demontees = []

    class _ApiRase:
        def get_state(self):
            return {"inventory": {}, "tick": 1,
                    "character": {"position": {"x": 0.0, "y": 0.0}}}
        def mine_entity(self, nom, count=1, **kw):
            demontees.append(nom)
            return {"ok": True}
        def run_action(self, fn, *a, **kw):
            return fn(*a, **kw)

    import services.perception as _perc
    _vrai = _perc.parc
    _perc.parc = lambda api: [
        {"name": "burner-mining-drill", "type": "mining-drill", "x": 4.0, "y": 0.0},
        {"name": "stone-furnace", "type": "furnace", "x": 4.0, "y": 2.0},
        {"name": "transport-belt", "type": "transport-belt", "x": 6.0, "y": 2.0},
    ]
    try:
        c = _coord_mesure(_ApiRase())
        c._marcher = lambda x, y: (x, y)
        rendu = Coordinator._raser_la_production(c)
    finally:
        _perc.parc = _vrai

    a_pris_les_machines = sorted(demontees) == ["burner-mining-drill", "stone-furnace"]
    dit_ce_qu_il_fait = "2" in rendu or "démont" in rendu.lower()

    ok = a_pris_les_machines and dit_ce_qu_il_fait
    rec("test_repartir_de_zero_recupere_l_existant_avant_de_batir", ok,
        f"démontées={demontees} — « {rendu} »")
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
        test_ravitaillement_repete_devient_automatisation,
        test_le_compteur_est_par_machine,
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
        test_attente_ravitailler_tenue_et_decue,
        test_attente_machine_absente_est_un_echec,
        test_attente_approvisionner_suit_le_flux,
        test_ecart_journalise_quand_lattente_est_decue,
        test_acharnement_declasse_apres_trois_echecs,
        test_arbitrage_trace_meme_sans_arbitre,
        test_arbitrage_note_lappel_et_la_divergence,
        test_arbitre_defaillant_reste_trace_comme_appele,
        test_industrialiser_ce_quon_refait_a_la_main,
        test_une_action_sans_effet_compte_comme_un_echec,
        test_evacuer_est_juge_sur_son_effet,
        test_lacharnement_se_compte_par_action_pas_seulement_par_cible,
        test_lacharnement_est_inscrit_dans_la_raison_de_loption,
        test_un_abandon_se_perime_au_lieu_detre_definitif,
        test_on_ne_tire_pas_de_ligne_vers_une_machine_a_charbon,
        test_une_chaine_burner_est_approvisionnee_pas_branchee,
        test_l_alimentation_fabrique_le_foreur_qui_lui_manque,
        test_une_evacuation_qui_echoue_dit_pourquoi,
        test_la_portee_d_alimentation_se_chiffre_en_plaques,
        test_on_gave_les_bruleurs_avant_de_leur_batir_une_belt,
        test_gaver_va_miner_le_charbon_qui_manque,
        test_l_evacuation_fabrique_le_bras_qui_lui_manque,
        test_l_evacuation_fabrique_le_coffre_qui_lui_manque,
        test_on_recolte_l_usine_avant_de_miner_a_la_main,
        test_fabriquer_recolte_avant_de_planifier,
        test_on_recolte_a_nouveau_quand_l_usine_a_reproduit,
        test_la_ligne_forge_ses_poteaux_et_ne_ment_pas_sur_la_cause,
        test_un_manque_se_dit_en_phrase_et_pas_en_dict_python,
        test_l_arret_mord_aussi_pendant_la_fabrication,
        test_le_plafond_de_belts_ne_doit_pas_decider_a_la_place_de_l_agent,
        test_avoir_deja_ce_qu_on_demande_n_est_pas_un_echec,
        test_la_recolte_va_CHERCHER_les_plaques_au_lieu_de_les_attendre,
        test_la_recolte_ne_se_limite_pas_a_un_rayon_arbitraire,
        test_ravitailler_donne_une_dose_et_ne_vide_pas_la_poche,
        test_batir_une_chaine_signale_ce_qu_elle_laisse_orphelin,
        test_repartir_de_zero_recupere_l_existant_avant_de_batir,
        test_le_coffre_d_evacuation_essaie_aussi_les_diagonales,
        test_la_foreuse_a_charbon_recoit_un_bras_de_retour,
        test_l_evacuation_s_approche_avant_de_poser_son_coffre,
        test_l_alimentation_reutilise_la_foreuse_deja_posee,
        test_la_portee_d_alimentation_n_est_pas_plafonnee_par_nous,
        test_on_ne_pose_pas_de_coffre_sur_une_machine_deja_evacuee,
        test_un_refus_de_terrain_dit_ce_qui_bloque,
        test_le_nombre_d_alimentations_revient_a_l_agent,
        test_la_centrale_forge_ce_qui_lui_manque,
        test_lusine_est_aussi_grande_que_ce_quon_y_a_bati,
        test_toute_construction_elargit_lusine_pas_seulement_les_chaines,
        test_le_diagnostic_trouve_lusine_ou_quelle_soit,
        test_une_recherche_qu_on_ne_peut_automatiser_se_paie_a_la_main,
        test_payer_la_recherche_fabrique_et_porte_les_flacons,
        test_sans_combustible_pour_amorcer_on_va_en_chercher,
        test_evacuer_s_approche_avant_de_vider,
        test_le_combustible_offre_un_choix_au_lieu_dun_verdict,
        test_les_ancres_sont_essayees_de_la_plus_proche_a_la_plus_lointaine,
        test_le_coffre_se_choisit_comme_les_autres_paliers,
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