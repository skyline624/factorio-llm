"""Coordinator — la boucle qui décide quoi faire, et le fait.

C'est la pièce qui manquait pour que le projet cesse d'être une collection de services
appelés par des scripts : observer, diagnostiquer, décider, agir, vérifier.

    OBSERVE     perception + scan_factory + get_power_state
       |
    DIAGNOSE    FactoryDoctor (déterministe)
       |
    DECIDE      `decide()` — fonction PURE, testable sans serveur
       |
    AGIT        primitives du mod (ravitailler, relier, régler une recette…)
       |
    VERIFIE     on relit l'état ; l'échec renvoie au DIAGNOSTIC, pas à la décision

**V1 sans LLM, et c'est délibéré.** Le curriculum des premières heures est connu
d'avance — réparer ce qui est cassé, puis produire du courant, puis produire des
objets. Une machine à états y sera plus fiable et gratuite. Le modèle ne devient utile
que lorsque plusieurs chemins se valent réellement (défense contre expansion contre
recherche), ce qui n'arrive pas avant l'arrivée des menaces. La boucle doit tourner
sans lui : on branchera l'arbitrage LLM sur `decide()` quand il apportera quelque chose.

**Réparer passe avant construire.** Une usine arrêtée ne produit rien, et la remettre
en marche coûte presque toujours moins qu'en bâtir une autre. C'est aussi la faiblesse
que le benchmark FLE relève chez les agents LLM : « limited iterative improvement,
agents rarely refine designs after initial implementation » — ils empilent au lieu de
réparer.

Le diagnostic est déjà traduit en causes par le [FactoryDoctor] ; le Coordinator y
associe l'action qui répare, chacune correspondant à une primitive existante.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Optional, Protocol

from services import perception
from services.factory_doctor import Diagnostic, Symptome, diagnose_zone
from services.threat_model import EN_COURS, Menace, evaluer

# Cause diagnostiquée -> action qui la répare, et primitive correspondante.
# Une cause sans réparation connue devient "inspecter" : on préfère le dire plutôt
# que d'agir au hasard.
REPARATION: dict[str, tuple[str, str]] = {
    "debranchee":          ("relier", "poser un poteau à portée de la machine"),
    "sans_courant":        ("renforcer_energie", "la centrale ne suit pas la charge"),
    "courant_insuffisant": ("renforcer_energie", "réseau sous-dimensionné"),
    "sans_combustible":    ("ravitailler", "recharger le combustible"),
    "sans_recette":        ("regler_recette", "la machine n'a aucune recette"),
    "entree_vide":         ("alimenter", "rien n'arrive en entrée"),
    "sortie_bloquee":      ("evacuer", "la sortie n'est pas ramassée"),
    "desactivee":          ("reactiver", "machine désactivée"),
    "gisement_epuise":     ("redeployer_foreur", "plus de minerai sous l'emprise"),
}

# Ordre du curriculum : plus le nombre est grand, plus c'est urgent.
#
# `defendre` apparaît à DEUX niveaux, et c'est délibéré :
#   - des ennemis déjà sur l'usine passent avant tout, y compris les réparations : rien
#     ne sert de remettre un four en marche pendant qu'on le détruit ;
#   - une menace seulement imminente vaut `batir_energie`, donc entre en CONCURRENCE
#     avec la production. C'est le premier arbitrage du projet où deux options
#     défendables s'équivalent — et donc le premier endroit où un arbitre LLM aurait
#     quelque chose à apporter.
PRIORITE = {"defendre_urgence": 4, "reparer": 3, "batir_energie": 2, "defendre": 2,
            # `fabriquer` n'a pas de rang propre : il HÉRITE de celui de l'action qu'il
            # débloque (cf. `enumerer_options`). Une pièce qui manque pour une réparation
            # est urgente comme une réparation ; pour une extension, comme une extension.
            # Lui donner un rang fixe doublerait tout le curriculum d'un cran.
            "batir_production": 1, "etendre_production": 1, "rien": 0}

# Fenêtre minimale, en ticks de JEU, pour mesurer un débit. Deux observations rapprochées
# donnent un rapport dominé par le bruit : une plaque de plus sur trente ticks vaut
# 2 items/s, une de moins vaut zéro. On ne décide pas d'agrandir une usine sur ce
# genre de chiffre — en dessous, on garde la mesure précédente et on le dit.
FENETRE_DEBIT = 600

# Marge sous l'objectif en deçà de laquelle on considère qu'il n'est PAS tenu. Sans elle,
# une usine calibrée juste oscillerait autour de sa cible et l'agent l'agrandirait
# indéfiniment sur du bruit de mesure.
MARGE_OBJECTIF = 0.9

# PAS de seuil de satisfaction conditionnant l'extension, et c'est une décision prise
# CONTRE une intuition, sur mesure. Le raisonnement semblait solide : ne pas agrandir une
# usine que le courant ne suit plus, puisqu'une extension de trop faisait tomber le réseau
# entier (seize machines, zéro en marche). Essayé à 0.95 : l'agent a cessé de grandir tout
# court — quarante tours, quatre machines, `rien` trente et une fois, parce que la
# satisfaction oscille sous 1.0 en régime NORMAL (un boiler qui se recharge) sans que rien
# ne soit en panne.
#
# Le garde-fou existait déjà, ailleurs et mieux : si le courant manque vraiment, les
# machines passent `sans_courant`, ce qui est une RÉPARATION (priorité 3) et passe donc
# avant l'extension (priorité 1). Le curriculum s'en charge ; un veto de plus ne faisait
# que l'empêcher de vivre. `EtatUsine.satisfaction` reste mesurée — elle est utile à lire
# dans un journal — mais elle ne décide de rien.

# Au-delà de ce nombre d'interventions MANUELLES sur la même machine, on cesse de
# dépanner et on automatise. Remplir un réservoir est une réparation ; le remplir
# indéfiniment est l'aveu qu'il manque une chaîne d'approvisionnement. Le seuil vaut
# aux DEUX bouts de la machine : ce qu'on remplit sans fin (entrée) comme ce qu'on vide
# sans fin (sortie) désigne le même manque — un flux permanent qui n'a pas été bâti.
#
# Mesuré : un boiler brûle 0.45 charbon/s, soit ~110 s d'autonomie pour 50 unités. Un
# agent qui ne fait que ravitailler y passe sa vie et ne construit plus rien.
SEUIL_AUTOMATISATION = 2

# Au-delà de ce nombre d'échecs consécutifs sur la même action et la même cible, on cesse
# d'insister. Mesuré en partie longue : 559 tours sur 562 passés à retenter `alimenter`
# sur une machine dont l'ingrédient n'était pas extractible. La boucle ne plantait pas —
# elle « fonctionnait », et c'est pire : elle occupait tout son temps à ne rien faire.
#
# L'option n'est pas SUPPRIMÉE mais déclassée, comme celles dont le matériel manque :
# l'agent tente autre chose, et y reviendra si le contexte change.
SEUIL_ABANDON = 3

# Le combustible du bootstrap, et le stock en dessous duquel on cesse de dépanner à la
# main. Amorcer un foreur burner et ses deux bras coûte une trentaine d'unités : si on
# descend sous ce seuil, on perd la capacité de bâtir la chaîne qui rendrait le
# dépannage inutile. La réserve est donc protégée, même au prix d'une machine à l'arrêt.
COMBUSTIBLE = "coal"
RESERVE_AMORCE = 40

# Matériel indispensable à une action. Sans lui, elle échouera à l'exécution — la
# proposer serait mentir sur le contrat, qui promet des options LÉGALES.
#
# Révélé en confrontant l'arbitre à un vrai modèle : privé de toute tourelle, il
# choisissait quand même « defendre » trois fois sur trois, avec une justification
# solide sur la menace. Il n'avait aucun moyen de savoir que l'action était impossible —
# rien dans les options ne portait leur coût. Le défaut était dans l'interface, pas
# dans le modèle, et un déterministe qui propose l'infaisable trompe aussi bien un
# humain qu'une machine.
BESOINS: dict[str, tuple[tuple[str, int], ...]] = {
    "defendre": (("gun-turret", 1),),
    "ravitailler": (("coal", 1),),
    "relier": (("small-electric-pole", 1),),
    # Le cas nominal : un bras électrique et un coffre. `batir_evacuation` sait se
    # rabattre sur un burner-inserter, mais déclarer le cas nominal garde la
    # faisabilité honnête — une évacuation sans coffre où déposer n'aboutit jamais.
    "batir_evacuation": (("inserter", 1), ("wooden-chest", 1)),
}


@dataclass
class EtatUsine:
    """Photographie de l'usine, telle que le Coordinator la voit avant de décider."""
    machines: int = 0
    diagnostic: Optional[Diagnostic] = None
    reseau: Optional[int] = None          # networkId observé, None = aucun réseau
    production_kw: float = 0.0
    inventaire: dict = field(default_factory=dict)
    menace: Optional[Menace] = None       # None = menace non évaluée (pas « aucune »)
    # Combien de fois chaque machine a déjà été ravitaillée à la main, par position.
    # C'est la mémoire qui permet de distinguer un incident d'un besoin structurel.
    ravitaillements: dict = field(default_factory=dict)
    # Symétrique du précédent, pour la SORTIE : combien de fois chaque machine a été
    # vidée à la main. Mesuré en partie longue sur carte propre — `sortie_bloquee` est
    # revenu aux tours 152, 380, 608 et 831 sur le même four, chaque fois « réparé ».
    # Vider est une réparation ; vider indéfiniment est l'aveu qu'il manque un ramassage.
    evacuations: dict = field(default_factory=dict)
    # Ce que l'usine produit RÉELLEMENT, en items/s de jeu, et ce qu'on lui demande.
    # `debit=None` signifie « pas encore mesurable », ce qui n'est pas « zéro » : une
    # seule observation ne donne aucun débit, et conclure sur une mesure absente est
    # exactement ce qu'un agent ne doit pas faire.
    # `objectif=None` : aucun objectif fixé, l'agent se contente de maintenir — c'est le
    # comportement d'avant, conservé tel quel.
    debit: Optional[float] = None
    objectif: Optional[float] = None
    objectif_item: str = ""
    # Ce que le réseau arrive à fournir, entre 0 et 1. `None` = pas mesuré, ce qui n'est
    # pas « tout va bien » : on n'agrandit pas une usine sur une mesure absente.
    satisfaction: Optional[float] = None
    # Les machines qui PRODUISENT, à l'exclusion des organes d'énergie. Depuis que le
    # diagnostic embrasse les centrales (elles se posent au bord de l'eau, hors de la
    # zone), `machines` n'est plus jamais nul après `batir_energie` — et la condition de
    # `batir_production`, qui exige zéro machine, ne pouvait plus être vraie. Mesuré :
    # l'agent bâtissait sa centrale puis ne produisait RIEN, huit tours durant.
    # `None` = non renseigné : on retombe alors sur `machines`, comme avant.
    machines_production: Optional[int] = None
    # Le palier des machines qu'on sait poser. En BURNER, une centrale ne sert à rien :
    # foreuse, four et bras brûlent du charbon et se moquent du réseau. Bâtir l'énergie
    # d'abord — ce que le curriculum impose depuis E8 — n'a de sens que pour l'électrique.
    # `True` par défaut : c'est le comportement d'avant, celui d'un agent doté.
    electrique: bool = True
    # Ce que réclame une chaîne de production AU PALIER COURANT. `BESOINS` est une table
    # par action, et ne peut pas savoir qu'un agent sans recherche pose du burner.
    besoins_production: tuple = ()
    # Les FOREUSES, et elles seules. Une chaîne de production commence par une foreuse ;
    # un four posé à la main pour fondre trois plaques n'est pas une usine. Mesuré : les
    # fours de fusion du bootstrap comptaient comme machines de production, si bien que
    # `batir_production` ne se déclenchait jamais — l'agent tenait ses trois machines en
    # poche sans les poser. `None` = non renseigné, on retombe sur le compte général.
    foreuses: Optional[int] = None
    # Échecs consécutifs par (action, cible). Sans cette mémoire, une action impossible
    # est retentée indéfiniment : la boucle tourne sans avancer, ce qui ne se voit pas.
    echecs: dict = field(default_factory=dict)

    @property
    def a_de_l_energie(self) -> bool:
        """Un réseau existe-t-il ? — et NON « produit-il en ce moment ».

        Piège déjà rencontré en mesurant les centrales : un générateur ne produit que ce
        qui est consommé. Une centrale neuve qui n'alimente encore rien affiche 0 kW.
        Exiger `production_kw > 0` faisait conclure « pas d'énergie » et rebâtir une
        centrale à chaque tour, indéfiniment.

        Le manque RÉEL de courant n'est pas déduit ici : il est diagnostiqué sur les
        machines (`sans_courant`) et traité en réparation, ce qui est sa place.
        """
        return self.reseau is not None


@dataclass
class Attente:
    """Ce qui doit être VRAI après une action pour qu'elle ait servi à quelque chose.

    C'est la pièce qui manquait pour que l'agent sache qu'il a échoué. Jusqu'ici la
    boucle relisait bien l'état après avoir agi, mais ne le confrontait à rien : « j'ai
    agi » n'était jamais opposé à « ça a marché ». Une chaîne posée dont aucun charbon ne
    sortait était donc journalisée « chaîne bâtie », et le tour suivant passait à autre
    chose.

    L'attente est une MESURE, pas un raisonnement : une grandeur lue dans le jeu et un
    prédicat dessus. Elle est donc vérifiable hors ligne avec un faux api.
    """
    description: str
    mesurer: Callable[[Any], Any]
    satisfait: Callable[[Any], bool]
    delai_ticks: int = 0          # laisser le jeu réagir avant de conclure

    def evaluer(self, api) -> tuple[bool, str]:
        """(tenue, ce qui a été observé). Une mesure impossible n'est jamais un succès."""
        if self.delai_ticks:
            try:
                api.run_action(api.wait, self.delai_ticks,
                               timeout=max(30.0, self.delai_ticks / 10.0))
            except Exception:
                pass              # l'attente reste évaluable, simplement plus tôt
        try:
            valeur = self.mesurer(api)
        except Exception as e:
            return False, f"mesure impossible ({type(e).__name__})"
        try:
            return bool(self.satisfait(valeur)), str(valeur)[:120]
        except Exception as e:
            return False, f"critère illisible ({type(e).__name__}) sur {str(valeur)[:60]}"


@dataclass
class Ecart:
    """Une action qui a été menée à son terme sans produire l'effet attendu.

    C'est le signal qui n'existait pas, et sans lequel aucune enquête ne peut être
    déclenchée — on ne cherche pas la cause d'un problème qu'on n'a pas vu.
    """
    action: str
    attendu: str
    observe: str
    cible: Optional[Symptome] = None
    # Ce que la boucle SAIT déjà et qu'une enquête devrait ignorer : d'où part la chaîne
    # concernée, par exemple. Le redécouvrir coûterait des mesures à celui qui enquête,
    # et il n'aurait aucun moyen de le deviner.
    contexte: dict = field(default_factory=dict)

    def __str__(self) -> str:
        ou = f" @({self.cible.x},{self.cible.y})" if self.cible else ""
        return f"ÉCART {self.action}{ou} : attendu « {self.attendu} », observé {self.observe}"


@dataclass
class Arbitrage:
    """Combien de choix il y avait, et ce que l'arbitre en a fait.

    Sans cette trace, comparer une partie « avec modèle » à une partie « sans » ne
    mesure rien : `decide` n'appelle pas l'arbitre quand il n'y a qu'une option — le cas
    le plus fréquent — et l'on conclurait « le modèle n'apporte pas » sans le lui avoir
    demandé une seule fois. C'est le préalable à toute expérience A/B, et il coûte trois
    champs.
    """
    options: int = 0
    faisables: int = 0       # celles qui peuvent réellement aboutir
    appele: bool = False     # l'arbitre a-t-il eu la parole
    indice: int = 0          # ce qu'il a désigné (0 = comme le déterministe)
    diverge: bool = False    # a-t-il choisi autre chose que le déterministe

    @property
    def arbitrable(self) -> bool:
        """Y avait-il un VRAI choix — au moins deux options qui puissent aboutir."""
        return self.faisables >= 2


@dataclass
class Decision:
    """Ce que le Coordinator a décidé, et pourquoi — le « pourquoi » est la moitié utile."""
    action: str
    raison: str
    priorite: int = 0
    cible: Optional[Symptome] = None
    faisable: bool = True     # False = le matériel manque (cf. BESOINS)
    # Renseigné par `decide` : de quoi savoir, après coup, si le modèle a seulement eu
    # son mot à dire. Une décision sans arbitrage possible n'est pas un désaccord.
    arbitrage: Optional["Arbitrage"] = None
    # Ce qu'il faut fabriquer, et COMBIEN. Le nom seul ne suffit pas : `fabriquer` s'en
    # remettait à sa valeur par défaut (une unité), si bien qu'avec huit charbons en
    # poche sur les vingt que réclame une chaîne burner, le plan répondait « l'inventaire
    # en contient déjà assez » — neuf tours de suite, jusqu'à l'abandon définitif.
    item: str = ""
    quantite: int = 1

    def __str__(self) -> str:
        ou = f" @({self.cible.x},{self.cible.y})" if self.cible else ""
        return f"{self.action}{ou} — {self.raison}"


class Arbitre(Protocol):
    """Choisit une option parmi celles que le déterministe a jugées légales.

    Reçoit l'état et la liste ordonnée, rend un **indice**. Il ne peut donc pas
    proposer une action impossible — c'est tout l'intérêt du contrat : le benchmark FLE
    montre que les LLM échouent quand ils GÉNÈRENT librement (coordonnées, séquences)
    et non quand ils CHOISISSENT.
    """

    def __call__(self, etat: "EtatUsine", options: list["Decision"]) -> int: ...


def _machines_qui_produisent(etat: EtatUsine) -> int:
    """Combien de machines PRODUISENT, à l'exclusion des centrales.

    Une centrale n'est pas une usine : compter le boiler et le moteur à vapeur parmi les
    machines faisait croire à l'agent qu'il avait déjà de quoi produire, et
    `batir_production` — qui exige zéro machine — n'était plus jamais proposé. Mesuré
    après l'ajout des centrales au diagnostic : centrale bâtie au tour 1, puis huit tours
    de `rien` et d'`evacuer` sur une carte sans la moindre foreuse.

    `machines_production` non renseigné rend `machines` : les états construits à la main
    dans les tests gardent ainsi leur sens.
    """
    return (etat.machines_production if etat.machines_production is not None
            else etat.machines)


def _foreuses(etat: EtatUsine) -> int:
    """Combien de FOREUSES tournent — le seul compte qui dise s'il existe une chaîne.

    Un four posé à la main pour fondre trois plaques n'est pas une usine. Mesuré pendant
    le bootstrap : les deux fours de fusion faisaient croire à quatre machines de
    production, et `batir_production` — qui exige zéro machine — ne se déclenchait
    jamais. L'agent avait fabriqué sa foreuse, son four et son bras, et les gardait en
    poche faute de savoir qu'il n'avait encore rien bâti.

    Non renseigné, on retombe sur le compte général : les états construits à la main dans
    les tests gardent leur sens.
    """
    return etat.foreuses if etat.foreuses is not None else _machines_qui_produisent(etat)


def figer_pendant(api, actif: bool, fonction, *args):
    """Exécute une RÉFLEXION sans laisser le temps de jeu s'écouler.

    Mesuré : un appel au modèle coûte cinq secondes réelles, soit trois mille ticks de
    jeu à ×10 — cinquante secondes de partie pour une seule décision. Sur trente minutes
    de jeu, douze appels en emportent le tiers. L'effet est pervers : plus l'agent
    rencontre de dilemmes — ce qu'on a passé la journée à obtenir — moins il lui reste de
    temps pour agir, et une partie « avec modèle » se compare alors à une partie « sans »
    comme un agent lent à un agent rapide, pas comme deux stratégies.

    `game.tick_paused` fige le monde pendant la réflexion (vérifié : zéro tick sur trois
    secondes). On ne fige QUE la décision, jamais l'action : les tâches du mod sont
    asynchrones et ne progresseraient plus.

    Fonction de module et non méthode : elle est ainsi éprouvable seule, et un appelant
    qui n'a pas d'API réelle — les faux Coordinator des tests — la traverse sans rien
    savoir de `game.tick_paused`. La reprise est dans un `finally` : une exception
    pendant la réflexion laisserait sinon le monde figé.
    """
    if not actif:
        return fonction(*args)
    try:
        api.rcon.query_lua("game.tick_paused = true rcon.print(1)")
    except Exception:
        return fonction(*args)          # pas de pause possible : on réfléchit quand même
    try:
        return fonction(*args)
    finally:
        try:
            api.rcon.query_lua("game.tick_paused = false rcon.print(1)")
        except Exception:
            pass


def enumerer_options(etat: EtatUsine) -> list[Decision]:
    """Toutes les actions légales dans cet état, de la plus urgente à la moins.

    Fonction PURE. `options[0]` est ce que la V1 déterministe fait — l'ordre EST le
    curriculum :
      1. réparer ce qui est cassé (une usine arrêtée ne produit rien) ;
      2. produire du courant s'il n'y en a pas (rien d'électrique ne marchera sans) ;
      3. produire des objets s'il n'y a aucune machine ;
      4. sinon, ne rien faire — et le dire, plutôt que de s'agiter.

    Les suivantes sont les autres actions défendables. Elles n'existent que pour un
    arbitre : quand plusieurs pannes coexistent, laquelle traiter d'abord est un vrai
    choix, que la gravité seule ne tranche pas toujours (réparer un four à l'arrêt ou
    un drill qui ralentit toute la chaîne ?).
    """
    options: list[Decision] = []
    diag = etat.diagnostic
    causes = diag.causes if diag else []

    # Une option PAR cause, et non seulement la plus grave. Le tri du diagnostic
    # (gravité, puis conséquences en dernier) fixe l'ordre par défaut.
    for c in causes:
        action, explication = REPARATION.get(c.cause, ("inspecter", "cause inconnue"))
        # Un manque de combustible qui revient n'est pas un incident : c'est une chaîne
        # d'approvisionnement qui manque. On arrête de remplir et on construit.
        deja = etat.ravitaillements.get((c.name, round(c.x), round(c.y)), 0)
        stock = etat.inventaire.get(COMBUSTIBLE, 0)
        if action == "ravitailler" and deja >= SEUIL_AUTOMATISATION:
            action = "approvisionner"
            explication = (f"déjà ravitaillée {deja} fois à la main — il lui faut une "
                           f"chaîne, pas un remplissage de plus")
        elif action == "ravitailler" and stock < RESERVE_AMORCE:
            # Mesuré : deux remplissages manuels ont vidé le stock, et la chaîne qui
            # aurait rendu la machine autonome n'a plus pu être amorcée — l'agent avait
            # brûlé en dépannage le combustible qui le libérait. Ce qui reste vaut plus
            # comme amorce que comme dernier plein.
            action = "approvisionner"
            explication = (f"il ne reste que {stock} {COMBUSTIBLE} : les garder pour "
                           f"amorcer une chaîne plutôt que les brûler en un plein")
        # Le même raisonnement à l'autre bout de la machine. Une sortie qu'on vide sans
        # cesse n'est pas un incident qui se répète : c'est un ramassage qui manque.
        # Mesuré : 4 vidages manuels sur le même four en 952 tours, et la chaîne bouchée
        # à chaque fois entre-temps — le foreur en amont attendait `waiting_for_space`.
        vidages = etat.evacuations.get((c.name, round(c.x), round(c.y)), 0)
        if action == "evacuer" and vidages >= SEUIL_AUTOMATISATION:
            action = "batir_evacuation"
            explication = (f"déjà vidée {vidages} fois à la main — il lui faut un "
                           f"ramassage permanent, pas un vidage de plus")
        # Une action qui a échoué SEUIL_ABANDON fois de suite sur cette cible est
        # reléguée exactement comme celle dont le matériel manque : priorité nulle et
        # `faisable=False`. Le même mécanisme pour la même raison — proposer en tête ce
        # qui vient d'échouer trois fois trompe l'arbitre autant qu'un humain.
        rates = etat.echecs.get((action, c.name, round(c.x), round(c.y)), 0)
        renonce = rates >= SEUIL_ABANDON
        options.append(Decision(
            action=action,
            raison=(f"{c.name} : {c.cause} — {explication}"
                    + (f" — ÉCHOUÉ {rates} fois, on n'insiste plus" if renonce else "")),
            priorite=0 if renonce else PRIORITE["reparer"], cible=c,
            faisable=not renonce))

    # Défense : deux niveaux d'urgence, cf. PRIORITE. Le ThreatModel a déjà tranché
    # le « faut-il » (la pollution déclenche les vagues, pas la proximité des nids) ;
    # ici on n'ajoute que l'option correspondante.
    if etat.menace is not None and etat.menace.agir:
        urgent = etat.menace.niveau >= EN_COURS
        # `defendre` passe par la MÊME mémoire d'échecs que tout le reste. Elle y
        # échappait, et d'une façon particulièrement trompeuse : `tick` comptait bien ses
        # échecs — le journal affichait « ABANDON de defendre après 3 échecs » — mais
        # personne ne LISAIT ce compteur ici, si bien que l'option repartait faisable au
        # tour suivant. La mémoire était écrite et jamais relue.
        #
        # Mesuré en partie longue : 936 tours sur 952 à redécider `defendre`, dont 933
        # sans rien faire, tous sur le même motif « aucune position de tourelle libre au
        # nord » — dès le tour 13 et jusqu'à la fin. Sept tourelles avaient été posées,
        # les nids étaient à 290 tuiles, et il n'y avait plus rien à tenter.
        #
        # Le déclassement vaut aussi pour une menace EN COURS : si trois tentatives
        # consécutives n'ont rien donné, s'acharner ne sauve pas l'usine, alors que
        # réparer pendant ce temps a une valeur. Il se lève de lui-même au premier
        # succès, `tick` vidant le compteur.
        rates = etat.echecs.get(("defendre", "", 0, 0), 0)
        renonce = rates >= SEUIL_ABANDON
        options.append(Decision(
            action="defendre",
            raison=str(etat.menace) + (f" — ÉCHOUÉ {rates} fois, on n'insiste plus"
                                       if renonce else ""),
            priorite=0 if renonce else PRIORITE["defendre_urgence" if urgent
                                                else "defendre"],
            faisable=not renonce))

    # Les constructions passent par la MÊME mémoire d'échecs que les réparations.
    # Elles n'ont pas de cible, et la clé sans cible est ce qui les y rattache : sans
    # cela, `batir_energie` était retenté 1241 fois d'affilée — mesuré. Bâtir est
    # justement ce qui coûte le plus cher à recommencer pour rien.
    for action, condition, raison, prio in (
            # Une centrale n'a de sens que si l'on pose des machines ÉLECTRIQUES. Mesuré :
            # les mains vides, l'agent réclamait une centrale qu'il ne pouvait pas bâtir,
            # trois tours durant, alors que sa chaîne burner n'en avait aucun besoin.
            ("batir_energie", not etat.a_de_l_energie and etat.electrique,
             "aucun réseau alimenté : rien d'électrique ne fonctionnera avant",
             PRIORITE["batir_energie"]),
            # « Du courant » n'est une condition que pour des machines électriques : une
            # chaîne burner brûle du charbon et se moque du réseau. Mesuré les mains
            # vides : l'agent restait à `rien` parce qu'il attendait une électricité dont
            # sa première chaîne n'avait aucun besoin.
            ("batir_production",
             (etat.a_de_l_energie or not etat.electrique)
             and _foreuses(etat) == 0,
             ("du courant, mais aucune machine pour en profiter" if etat.electrique
              else "aucune machine : une chaîne burner n'attend pas le réseau"),
             PRIORITE["batir_production"]),
            # L'usine tourne, mais tient-elle son objectif ? Sans cette option, « 4
            # machines en état de marche » etait une raison de ne RIEN faire : mesuré,
            # `rien` occupait 100 tours sur 114 pendant que la production plafonnait.
            # Un agent qui maintient n'a jamais de dilemme ; un agent qui VISE doit
            # choisir entre réparer, se défendre et grandir — et c'est là seulement qu'un
            # arbitre a quelque chose à trancher.
            #
            # La condition exige un débit MESURÉ : proposer d'étendre sur `debit=None`
            # (première observation, lecture impossible) serait agir sans savoir.
            ("etendre_production",
             (etat.objectif is not None and etat.debit is not None
              and etat.machines > 0
              and etat.debit < etat.objectif * MARGE_OBJECTIF),
             (f"{etat.debit:.2f} {etat.objectif_item}/s produits pour "
              f"{etat.objectif:.2f} demandés : l'usine tient, elle ne suffit pas"
              if etat.debit is not None and etat.objectif is not None else ""),
             PRIORITE["etendre_production"])):
        if not condition:
            continue
        rates = etat.echecs.get((action, "", 0, 0), 0)
        renonce = rates >= SEUIL_ABANDON
        options.append(Decision(
            action=action,
            raison=raison + (f" — ÉCHOUÉ {rates} fois, on n'insiste plus" if renonce else ""),
            priorite=0 if renonce else prio, faisable=not renonce))

    if not options:
        options.append(Decision(action="rien",
                                raison=f"{etat.machines} machine(s) en état de marche",
                                priorite=PRIORITE["rien"]))
    # Faisabilité : une action dont le matériel manque est DÉCLASSÉE, pas supprimée.
    # La supprimer masquerait le besoin ; la garder en tête ferait échouer la boucle à
    # chaque tour. On la relègue et on dit pourquoi — c'est ce qui permet, plus tard, de
    # décider d'aller fabriquer ce qui manque.
    inv = etat.inventaire or {}
    a_faire: list[Decision] = []          # les fabrications que les manques appellent
    dejadits: set = set()                 # un item manquant ne se propose qu'une fois
    for o in options:
        # Les besoins d'une CONSTRUCTION dépendent du palier : `BESOINS` est une table par
        # action et ne peut pas savoir qu'un agent sans recherche pose du burner.
        besoins = (etat.besoins_production
                   if o.action in ("batir_production", "etendre_production")
                   and etat.besoins_production else BESOINS.get(o.action, ()))
        manquants = [f"{n} ({inv.get(n, 0)}/{c})"
                     for n, c in besoins if inv.get(n, 0) < c]
        if manquants:
            # La priorité d'ORIGINE, avant déclassement : c'est elle que la fabrication
            # hérite. Une pièce qui manque pour une réparation est plus urgente qu'une
            # pièce qui manque pour la défense, elle-même plus urgente qu'une pièce pour
            # une extension — sinon `fabriquer` doublerait tout le curriculum d'un cran.
            urgence = o.priorite
            o.priorite = 0
            o.faisable = False
            o.raison += f" — INFAISABLE, il manque : {', '.join(manquants)}"
            # Ce qui manque peut se FABRIQUER. Sans cette option, l'agent se contentait
            # de déclasser et d'attendre : il consommait une dotation qu'un humain lui
            # avait mise dans les poches, et s'arrêtait quand elle était vide. Déclarer
            # le besoin ne suffit pas — encore faut-il pouvoir y répondre.
            besoin, requis = next((n, c) for n, c in besoins if inv.get(n, 0) < c)
            if besoin not in dejadits:
                dejadits.add(besoin)
                a_faire.append(Decision(
                    action="fabriquer",
                    raison=f"{besoin} manque pour « {o.action} » — le fabriquer plutôt "
                           f"que d'attendre qu'il tombe du ciel",
                    priorite=urgence,
                    # La QUANTITÉ voyage avec la décision. Fabriquer « du charbon » sans
                    # dire combien revenait à en demander une unité : huit en poche
                    # suffisaient toujours, et la chaîne n'était jamais alimentée.
                    item=besoin, quantite=requis))

    options.extend(a_faire)

    # Tri STABLE par priorité décroissante : l'ordre relatif des réparations (déjà
    # trié par le diagnostic) est préservé, et la défense se glisse au bon rang.
    options.sort(key=lambda d: -d.priorite)
    return options


def decide(etat: EtatUsine, arbitre: Optional[Arbitre] = None) -> Decision:
    """Choisit la prochaine action. Fonction PURE : aucun appel RCON, testable seule.

    Sans `arbitre`, rend `enumerer_options(etat)[0]` — le comportement déterministe,
    inchangé. C'est le point d'insertion prévu pour un modèle, et le seul : c'est ici,
    et nulle part ailleurs, qu'il y a un arbitrage.

    Trois garde-fous, parce qu'un arbitre distant peut mal répondre ou ne pas répondre :
      - **une seule option -> il n'est pas appelé.** Inutile de payer un aller-retour
        pour choisir dans une liste d'un élément, et c'est le cas le plus fréquent ;
      - un indice hors bornes ou d'un mauvais type -> repli sur `options[0]` ;
      - une exception (réseau, délai, réponse illisible) -> repli, jamais de plantage.
        Un agent qui s'arrête parce que le modèle est indisponible ne vaut rien.
    """
    options = enumerer_options(etat)
    trace = Arbitrage(options=len(options),
                      faisables=sum(1 for o in options if o.faisable))
    # Toutes les options ont échoué ou sont infaisables : NE RIEN FAIRE est la bonne
    # réponse, et il faut la dire. Mesuré en partie longue : sans ce cas, la boucle
    # reprenait 598 fois de suite la seule action disponible, déjà abandonnée trois fois.
    # Elle « fonctionnait » — aucune erreur, aucun symptôme — et ne faisait rien.
    if options and not any(o.faisable for o in options):
        return Decision(
            action="rien",
            raison=("tout ce qui est réparable ici a déjà échoué : "
                    + " ; ".join(f"{o.action} sur {o.cible.name}" for o in options[:3]
                                 if o.cible is not None)),
            priorite=PRIORITE["rien"], arbitrage=trace)

    def _rendre(i: int) -> Decision:
        d = options[i]
        trace.indice = i
        trace.diverge = i != 0
        d.arbitrage = trace
        return d

    if arbitre is None or len(options) <= 1:
        return _rendre(0)
    trace.appele = True
    try:
        choix = arbitre(etat, options)
    except Exception:
        return _rendre(0)
    if not isinstance(choix, int) or isinstance(choix, bool):
        return _rendre(0)
    if not 0 <= choix < len(options):
        return _rendre(0)
    return _rendre(choix)


class Coordinator:
    """Boucle observe -> diagnostique -> décide -> agit -> vérifie.

    `observer` et `agir` touchent le jeu ; `decide` reste pur. Ce découpage permet de
    tester tout le raisonnement sans serveur, et de ne réserver le live qu'à ce qui
    ne peut pas être simulé.
    """

    def __init__(self, api, zone: tuple[float, float] = (0.0, 0.0), rayon: float = 30.0,
                 ressource: str = "iron-ore", demande_kw: float = 900.0,
                 combustible: str = "coal", builder=None,
                 arbitre: Optional[Arbitre] = None,
                 tourelle: str = "gun-turret", munition: str = "firearm-magazine",
                 ombre: bool = False, enqueteur=None,
                 objectif_par_s: Optional[float] = None,
                 objectif_item: str = "iron-plate",
                 pause_reflexion: bool = False):
        self.api = api
        self.zone = zone
        self.rayon = rayon
        self.ressource = ressource
        self.demande_kw = demande_kw
        self.combustible = combustible
        # Point d'insertion d'un arbitrage LLM : None = décision déterministe.
        # Il n'est consulté que lorsqu'il y a réellement plusieurs options.
        # `ombre=True` branche un arbitre LLM qui PROPOSE sans décider : le
        # déterministe garde la main et l'on mesure les divergences. C'est la seule
        # façon d'apprendre quelque chose sur le modèle sans rien risquer.
        if ombre and arbitre is None:
            try:
                from services.arbitre import ArbitreOmbre, LLMArbitre
                arbitre = ArbitreOmbre(LLMArbitre())
            except Exception:
                arbitre = None      # pas de modèle : la boucle tourne quand même
        self.arbitre = arbitre
        self.tourelle = tourelle
        self.munition = munition
        self.derniere_menace: Optional[Menace] = None
        self._ravitaillements: dict = {}
        # Le même compteur pour la sortie. Séparé du précédent : une machine peut être
        # ravitaillée souvent et vidée jamais, et confondre les deux ferait basculer la
        # mauvaise extrémité.
        self._evacuations: dict = {}
        # L'OBJECTIF, et de quoi mesurer s'il est tenu. Sans objectif, l'agent maintient
        # ce qui existe : c'est ce qu'il faisait, et c'est conservé tel quel. Avec, il
        # compare sa production réelle a ce qu'on lui demande — et « 4 machines en état
        # de marche » cesse d'être une raison de ne rien faire.
        self.objectif_par_s = objectif_par_s
        self.objectif_item = objectif_item
        # Le temps de reflexion du modele ne doit pas etre facture en temps de JEU.
        # Cf. `sans_ecoulement` : c'est un choix de protocole, donc explicite.
        self.pause_reflexion = pause_reflexion
        self._cumul: Optional[int] = None       # production cumulée à la dernière mesure
        self._tick_cumul: Optional[int] = None
        self._debit: Optional[float] = None     # dernier débit calculé, en items/s de jeu
        # Le bâti vu à la dernière observation : « étendre » se vérifie par une usine plus
        # GRANDE, et l'attente est construite après l'action, donc trop tard pour observer
        # l'avant. Compté depuis la ZONE et limité aux machines qui PRODUISENT — un
        # comptage pris depuis le personnage ne se compare pas au suivant, celui-ci ayant
        # été téléporté entre-temps.
        self._machines_posees = 0
        # D'où part la chaîne qui alimente chaque machine : la tuile de sortie du foreur.
        # Sans ce point de départ, on ne peut pas SUIVRE le flux, et donc pas vérifier
        # qu'une chaîne bâtie transporte réellement quelque chose.
        self._chaines: dict = {}
        # Échecs consécutifs par (action, cible) : la mémoire qui empêche l'acharnement.
        self._echecs: dict = {}
        self.journal: list[str] = []
        # Les actions menées à leur terme sans produire leur effet. C'est le signal sur
        # lequel une enquête pourra être déclenchée ; sans lui, l'agent est aveugle à
        # ses propres échecs.
        self.ecarts: list[Ecart] = []
        # Ce que les enquêtes ont établi, y compris les « inconnu ». Ce journal EST la
        # liste de travail : chaque cause qu'on ne sait pas encore réparer y figure au
        # lieu d'être redécouverte à la main au chantier suivant.
        self.constats: list = []
        # Tous les arbitrages, d'où qu'ils viennent. `Decision.arbitrage` ne trace que
        # ceux de `decide` ; le choix du gisement en est un autre, et l'oublier ferait
        # conclure « le modèle n'est jamais consulté » alors qu'il l'a été 44 fois.
        self.arbitrages: list = []
        # `ombre` branche AUSSI l'enquêteur : il observe les échecs et les nomme, sans
        # déclencher de réparation. Rien n'est risqué, et l'on mesure ce qu'il vaut.
        if ombre and enqueteur is None:
            try:
                from agents.enqueteur import Enqueteur
                enqueteur = Enqueteur()
            except Exception:
                enqueteur = None    # pas de modèle : la boucle constate sans expliquer
        self.enqueteur = enqueteur
        # Mémoire de la boucle : où raccorder la prochaine chaîne, et ce qui a été bâti.
        self.dernier_poteau: Optional[tuple[float, float]] = None
        self.derniere_centrale = None
        if builder is None:
            from agents.base import Contract
            from agents.factory_builder import FactoryBuilder
            from services.knowledge import ProductionGoal
            builder = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 0.5)))
        self.builder = builder

    # ----- OBSERVE -----

    def observer(self) -> EtatUsine:
        # Les centrales sont observées à part : elles se posent au bord de l'eau, donc
        # hors de la zone de l'usine, et le diagnostic ne les voyait jamais. Mesuré :
        # deux boilers à sec, quatre centrales muettes à cent tuiles, production nulle.
        diag = diagnose_zone(self.api, self.zone[0], self.zone[1], self.rayon,
                             rows_sup=perception.centrales(self.api))
        etat = EtatUsine(machines=diag.machines, diagnostic=diag)
        # Un point du réseau suffit à savoir s'il y a du courant : on interroge la
        # première machine observée plutôt que toutes (chaque appel est un aller-retour).
        sa = self.api.scan_area(self.rayon)
        rows = sa.get("entities", []) if isinstance(sa, dict) else []
        for r in rows:
            if r.get("type") in ("generator", "electric-pole", "mining-drill", "furnace"):
                ps = self.api.get_power_state(float(r.get("x", 0.0)),
                                              float(r.get("y", 0.0)), 2.0)
                if isinstance(ps, dict) and ps.get("networkId") is not None:
                    etat.reseau = ps.get("networkId")
                    etat.production_kw = float(ps.get("productionKW") or 0.0)
                    sat = ps.get("satisfaction")
                    etat.satisfaction = (float(sat) if isinstance(sat, (int, float))
                                         and not isinstance(sat, bool) else None)
                    break
        etat.inventaire = perception.inventory(self.api)
        etat.ravitaillements = dict(self._ravitaillements)
        etat.evacuations = dict(self._evacuations)
        etat.echecs = dict(self._echecs)
        etat.debit, etat.objectif = self._mesurer_debit(), self.objectif_par_s
        etat.objectif_item = self.objectif_item
        # Le palier conditionne DEUX choses : faut-il une centrale, et que faut-il avoir
        # en poche pour bâtir une chaîne. Les mains vides et sans recherche, la réponse
        # est « pas de centrale » et « trois machines burner ».
        t = self.tiers_micro()
        etat.electrique = t["nom"] == "électrique"
        etat.besoins_production = ((t["drill"], 1), (t["furnace"], 1), (t["inserter"], 1))
        if not etat.electrique:
            # Une chaîne burner BRÛLE : foreuse, four et bras réclament chacun leur
            # amorce, et l'executor refuse la pose sans elle. Mesuré : « chaîne non posée,
            # il manque : {'coal': 7} » alors que les trois machines étaient en poche.
            # Le compter parmi les besoins fait que le manque appelle un minage, au lieu
            # de laisser la construction échouer trois fois puis s'abandonner.
            etat.besoins_production += ((COMBUSTIBLE, self.AMORCE_CHAINE_BURNER),)
        # Compté depuis la ZONE, indépendamment d'où se trouve le personnage : c'est ce
        # que l'attente d'une extension comparera, et deux comptages ne se comparent que
        # s'ils sont pris du même endroit. Toujours mesuré — et pas seulement sous
        # objectif : c'est aussi ce qui dit s'il existe la moindre machine de production,
        # donc s'il faut en bâtir une.
        n = perception.compter_machines(self.api, self.zone[0], self.zone[1], self.rayon)
        if n >= 0:
            self._machines_posees = n
            etat.machines_production = n
        # Les foreuses à part : c'est ce compte, et non le général, qui dit s'il existe
        # une chaîne de production ou seulement des fours de fusion posés à la main.
        f = perception.compter_machines(self.api, self.zone[0], self.zone[1], self.rayon,
                                        types=("mining-drill",))
        if f >= 0:
            etat.foreuses = f
        # La menace est évaluée à CHAQUE tour : elle change sans qu'on y touche, alors
        # que l'usine ne change que quand on agit.
        etat.menace = evaluer(self.api.scan_threats(self.zone[0], self.zone[1], 300.0),
                              usine=self.zone)
        self.derniere_menace = etat.menace
        return etat

    def sans_ecoulement(self, fonction, *args):
        """Réflexion sans écoulement du temps de jeu — cf. `figer_pendant`."""
        return figer_pendant(self.api, getattr(self, "pause_reflexion", False),
                             fonction, *args)

    def _mesurer_debit(self) -> Optional[float]:
        """Le débit réel, en items/s de jeu, ou None s'il n'est pas encore mesurable.

        Un débit est une DIFFÉRENCE : il faut deux lectures et l'écart de ticks entre
        elles. La première observation d'une partie ne peut donc rien rendre, et c'est
        None qu'elle doit rendre — pas zéro, qui se lirait « l'usine ne produit rien » et
        déclencherait une extension sur une mesure qui n'existe pas.

        `game.speed` n'entre pas dans le calcul : accélérer le jeu ne change pas le nombre
        de ticks par seconde de jeu, seulement leur vitesse d'écoulement réelle.
        """
        if self.objectif_par_s is None:
            return None                       # personne ne demande rien : rien à mesurer
        cumul = perception.production_cumulee(self.api, self.objectif_item)
        if cumul < 0:
            return self._debit                # lecture impossible : on garde l'ancienne
        tick = self.api.get_tick()
        tick = int(tick.get("tick", 0)) if isinstance(tick, dict) else 0
        precedent, tick_precedent = self._cumul, self._tick_cumul
        if precedent is None or tick_precedent is None or tick <= tick_precedent:
            self._cumul, self._tick_cumul = cumul, tick
            return self._debit
        ecart = tick - tick_precedent
        if ecart < FENETRE_DEBIT:
            # Fenêtre trop courte : on ne remplace pas la mesure précédente par du bruit,
            # et l'on ne déplace pas non plus le repère — sinon deux tours rapprochés
            # empêcheraient toute mesure d'aboutir.
            return self._debit
        self._debit = (cumul - precedent) / (ecart / 60.0)
        self._cumul, self._tick_cumul = cumul, tick
        return self._debit

    # ----- CE QU'ON ATTEND D'UNE ACTION -----

    @staticmethod
    def _statut_de(api, nom: str, x: float, y: float) -> str:
        """Statut de la machine `nom` autour de (x, y), ou « absente ».

        « Absente » n'est pas une valeur de repli commode : c'est un constat qui compte.
        Une machine qu'on croyait avoir posée et qui n'est nulle part est un échec bien
        plus grave qu'une machine en panne, et il ne doit pas se confondre avec elle.
        """
        r = api.inspect_at(x, y, 1.5)
        for e in (r.get("entities", []) if isinstance(r, dict) else []):
            if e.get("name") == nom:
                return str(e.get("status", "?"))
        return "absente"

    def _attente(self, d: Decision) -> Optional[Attente]:
        """Ce qu'il faudra constater pour que `d` compte comme réussie.

        Toutes ces mesures se lisent avec l'existant (`inspect_at`, `get_power_state`,
        `suivre_flux`). Une action sans attente connue rend None : on ne prétend pas
        vérifier ce qu'on ne sait pas mesurer, et le silence vaut mieux qu'un faux
        satisfecit.
        """
        c = d.cible
        if d.action == "ravitailler" and c is not None:
            return Attente(
                f"{c.name} n'est plus à sec",
                lambda api: self._statut_de(api, c.name, c.x, c.y),
                lambda s: s not in ("no_fuel", "absente"),
                delai_ticks=30)

        if d.action == "approvisionner" and c is not None:
            from services.flux import suivre_flux
            depart = self._chaines.get((c.name, round(c.x), round(c.y)))
            if depart is None:
                return None                  # aucune chaîne bâtie : rien à suivre
            return Attente(
                f"le charbon atteint {c.name} par la chaîne",
                lambda api: suivre_flux(api, depart, c.name, (c.x, c.y)),
                lambda r: bool(getattr(r, "continu", False)),
                delai_ticks=120)

        if d.action == "etendre_production":
            # « Étendre » veut dire une usine plus GRANDE. Le critère n'est pas le débit :
            # il monte de lui-même avec le temps, une extension ratée passerait donc pour
            # une réussite. On compare au bâti vu à l'observation — l'attente étant
            # construite après l'action, c'est le seul « avant » disponible.
            #
            # Le comptage part de la ZONE et non du personnage : `scan_area` est centré
            # sur lui, et l'exécution vient de le téléporter près du chantier. Mesuré,
            # l'attente échouait à chaque extension pourtant réussie — un écart et une
            # enquête pour rien à chaque tour.
            vues = self._machines_posees
            return Attente(
                f"l'usine compte plus de {vues} machine(s) de production",
                lambda api: perception.compter_machines(api, self.zone[0], self.zone[1],
                                                        self.rayon),
                lambda n: isinstance(n, int) and n > vues,
                delai_ticks=180)

        if d.action == "redeployer_foreur" and c is not None:
            # Le critère n'est pas « une foreuse est posée » mais « une foreuse EXTRAIT ».
            # Reposer une foreuse sur une tuile aussi pauvre que la précédente donnerait
            # une pose réussie et une usine toujours à jeun.
            def _foreuses(api):
                r = api.inspect_at(self.zone[0], self.zone[1], self.rayon)
                lignes = r.get("entities", []) if isinstance(r, dict) else []
                dr = [e for e in lignes if "mining-drill" in str(e.get("name"))]
                return ", ".join(str(e.get("status")) for e in dr) or "aucune foreuse"

            return Attente(
                "une foreuse extrait à nouveau",
                _foreuses,
                lambda s: "working" in s or "normal" in s,
                delai_ticks=180)

        if d.action == "batir_evacuation" and c is not None:
            # Le critère n'est PAS « un coffre et un bras sont posés » — c'est exactement
            # ce qu'on croyait vérifier en posant des inserters qui ne transportaient
            # rien. Le seul fait qui compte est que la machine reparte, donc que sa
            # sortie cesse d'être pleine. On laisse un délai : le bras met quelques
            # secondes à sortir assez d'objets pour libérer la production.
            return Attente(
                f"la sortie de {c.name} ne bloque plus",
                lambda api: self._statut_de(api, c.name, c.x, c.y),
                lambda s: s not in ("full_output", "waiting_for_space_in_destination",
                                    "absente"),
                delai_ticks=180)

        if d.action == "relier" and c is not None:
            return Attente(
                f"{c.name} reçoit du courant",
                # `connected`, pas `networkId` : une machine branchée sur un réseau sans
                # générateur porte un identifiant et ne reçoit rien. L'attente validait
                # ainsi des raccordements à des îlots morts.
                lambda api: (api.get_power_state(c.x, c.y, 3.0) or {}).get("connected"),
                lambda b: b is True,
                delai_ticks=30)

        if d.action == "defendre":
            def _tourelles_sans_munitions(api):
                r = api.inspect_at(self.zone[0], self.zone[1], 16.0)
                lignes = r.get("entities", []) if isinstance(r, dict) else []
                tours = [e for e in lignes if e.get("name") == self.tourelle]
                return f"{len(tours)} tourelle(s), " + ", ".join(
                    str(e.get("status")) for e in tours[:6])
            return Attente(
                "les tourelles posées ont des munitions",
                _tourelles_sans_munitions,
                lambda s: "no_ammo" not in s and not s.startswith("0 "),
                delai_ticks=30)

        return None

    # ----- AGIT -----

    def agir(self, d: Decision) -> tuple[bool, str]:
        """Exécute une décision. Retourne (agi, détail).

        Les réparations ponctuelles sont traitées ici ; bâtir est DÉLÉGUÉ aux planners
        et à l'executor via `batir()`. Le Coordinator décide QUOI, pas COMMENT — c'est
        la frontière posée par la roadmap, et elle tient : aucune coordonnée n'est
        calculée dans ce fichier.
        """
        if d.action == "rien":
            return False, "rien à faire"
        if d.action in ("batir_energie", "batir_production"):
            return self.batir(d)
        if d.action == "fabriquer":
            # `decide` reste PURE : elle ne sait pas fabriquer, elle dit seulement ce
            # qu'il faudrait et en quelle quantité. Le repli sur la raison couvre les
            # décisions construites à la main (tests, arbitre) qui ne portent pas d'item.
            return self.fabriquer(d.item or d.raison.split(" ")[0], max(1, d.quantite))

        if d.action == "etendre_production":
            # Une chaîne de PLUS, sur du minerai que le planner ancre lui-même. Même
            # parti que `renforcer_energie` et `redeployer_foreur` : on ajoute au lieu de
            # retoucher, ce qui réutilise du code éprouvé au lieu d'en inventer.
            #
            # Traité ICI, parmi les actions sans cible : plus bas, le garde
            # `if d.cible is None` l'aurait renvoyée comme « déléguée » sans rien faire.
            ok, detail = self.batir(Decision(action="batir_production",
                                             raison="objectif de débit non tenu"))
            return ok, f"extension de la production : {detail}"
        if d.action == "defendre":
            return self.defendre()
        if d.action == "approvisionner" and d.cible is not None:
            return self.approvisionner(d.cible, self.combustible)
        if d.cible is None:
            return False, f"{d.action} : délégué (aucune cible ponctuelle)"

        c = d.cible
        if d.action == "ravitailler":
            r = self.api.run_action(self.api.move_items_at, self.combustible, c.name,
                                    c.x, c.y, 50, True, timeout=30.0)
            ok = isinstance(r, dict) and r.get("ok") is True
            if ok:
                cle = (c.name, round(c.x), round(c.y))
                self._ravitaillements[cle] = self._ravitaillements.get(cle, 0) + 1
            return ok, (f"ravitaillement de {c.name}@({c.x},{c.y}) "
                        f"(n°{self._ravitaillements.get((c.name, round(c.x), round(c.y)), 0)})")
        if d.action == "relier":
            return self.relier(c)

        if d.action == "regler_recette":
            # Une machine sans recette ne produit rien et n'appelle rien : elle attend.
            # La recette à poser n'est pas une devinette — c'est l'objectif du contrat,
            # c'est-à-dire ce que l'agent est venu fabriquer ici.
            objectif = getattr(getattr(getattr(self, "builder", None), "contract", None),
                               "goal", None)
            recette = getattr(objectif, "item", None)
            if not recette:
                return False, "aucun objectif de production : recette inconnue"
            # La machine sait-elle FAIRE cette recette ? Mesuré : poser « iron-plate »
            # (catégorie `smelting`) sur une assembleuse la fait refuser en silence — la
            # pose « réussit » et la machine reste sans recette. Une recette qui ne
            # correspond pas à la machine n'est pas un pis-aller, c'est une erreur : on
            # préfère le dire que de régler n'importe quoi pour faire tomber le symptôme.
            cat = (self.api.get_recipe(recette) or {}).get("category")
            permises = ((self.api.describe(c.name) or {}).get("entity")
                        or {}).get("craftingCategories") or []
            if cat and permises and cat not in permises:
                return False, (f"{c.name} ne fait pas de « {cat} » : « {recette} » lui "
                               f"est étrangère (elle sait : {', '.join(permises[:3])})")
            r = self.api.run_action(self.api.set_recipe_at, c.x, c.y, recette, c.name,
                                    timeout=20.0)
            ok = isinstance(r, dict) and r.get("ok") is True
            return ok, (f"recette « {recette} » réglée sur {c.name}@({c.x},{c.y})"
                        if ok else f"recette « {recette} » refusée par {c.name}")

        if d.action == "renforcer_energie":
            # Le réseau existe mais ne suit pas. On ne rafistole pas une centrale : on en
            # ajoute une, ce que `batir_energie` sait déjà faire. Le raccordement au
            # réseau existant se fait par la ligne de poteaux, comme pour la première.
            ok, detail = self.batir(Decision(action="batir_energie",
                                             raison="réseau sous-dimensionné"))
            return ok, f"renfort électrique : {detail}"

        if d.action == "evacuer":
            # Dépannage : on enlève ce qui bouche. Chaque vidage est COMPTÉ, car c'est
            # sa répétition qui distingue l'incident du manque structurel — au-delà de
            # SEUIL_AUTOMATISATION, `enumerer_options` propose `batir_evacuation`.
            r = self.api.run_action(self.api.empty_output_at, c.x, c.y, c.name,
                                    timeout=20.0)
            ok = isinstance(r, dict) and r.get("ok") is True
            if ok:
                cle = (c.name, round(c.x), round(c.y))
                self._evacuations[cle] = self._evacuations.get(cle, 0) + 1
            return ok, (f"sortie de {c.name}@({c.x},{c.y}) vidée "
                        f"(n°{self._evacuations.get((c.name, round(c.x), round(c.y)), 0)})"
                        if ok else f"sortie de {c.name} non vidable : {r}")

        if d.action == "batir_evacuation":
            return self.batir_evacuation(c)

        if d.action == "redeployer_foreur":
            # Le foreur a vidé ses tuiles ; le gisement, lui, ne l'est pas. Mesuré :
            # 23 unités sous l'emprise, 312 000 à quelques pas. On le RETIRE d'abord —
            # `mine_entity` rend l'item, `destroy` le perdrait (leçon E2) — puis on
            # laisse la construction choisir un ancrage sur du minerai réel.
            #
            # On ne rafistole pas sur place : c'est le même parti que `renforcer_energie`,
            # qui ajoute une centrale plutôt que de retoucher l'ancienne. Le code de
            # construction sait déjà ancrer sur du minerai ; le déplacer à la main
            # demanderait de refaire ce calcul, et de le refaire moins bien.
            # `remove_entity_at` vise une POSITION et rend les items ; `mine_entity`
            # cible par nom dans un rayon et retirerait n'importe quelle foreuse, y
            # compris une qui extrait très bien.
            r = self.api.run_action(self.api.remove_entity_at, c.x, c.y, c.name,
                                    timeout=30.0)
            retire = isinstance(r, dict) and r.get("ok") is True
            ok, detail = self.batir(Decision(action="batir_production",
                                             raison="gisement épuisé sous la foreuse"))
            return ok, (f"foreuse épuisée {'retirée' if retire else 'NON retirée'} "
                        f"@({c.x},{c.y}) — nouvelle chaîne : {detail}")

        if d.action == "reactiver":
            r = self.api.run_action(self.api.enable_entity_at, c.x, c.y, c.name,
                                    timeout=20.0)
            ok = isinstance(r, dict) and r.get("ok") is True
            return ok, (f"{c.name}@({c.x},{c.y}) réactivée" if ok
                        else f"{c.name} non réactivable : {r}")

        if d.action == "alimenter":
            # « Rien n'arrive en entrée » est le même problème que « plus de combustible » :
            # il manque une chaîne. On réutilise donc `approvisionner`, avec l'ingrédient
            # que la machine attend au lieu du charbon.
            besoin = self._ingredient_manquant(c)
            if besoin is None:
                return False, f"ingrédient attendu par {c.name} inconnu"
            # Un ingrédient qui a une RECETTE se fabrique, il ne s'extrait pas. Mesuré en
            # partie longue : l'assembleuse attendait `iron-plate`, l'agent a cherché un
            # gisement d'`iron-plate` — qui n'existe pas — et a recommencé 559 fois.
            # Approvisionner et produire sont deux problèmes ; les confondre condamne la
            # boucle à chercher un minerai de plaque de fer.
            # Un ingrédient qui a une recette se FABRIQUE : c'est `produire`, pas
            # `approvisionner`. Confondre les deux condamnait la boucle à chercher un
            # gisement de plaques de fer — mesuré, 559 tours d'affilée.
            if (self.api.get_recipe(besoin) or {}).get("ingredients"):
                return self.produire(c, besoin)
            return self.approvisionner(c, besoin)

        return False, f"{d.action} : pas encore automatisé"

    def _ingredient_manquant(self, c) -> Optional[str]:
        """Ce que la machine attend en entrée. Lu, pas supposé.

        Un four à minerai attend la ressource du contrat ; une machine à recette attend
        le premier ingrédient de celle-ci. Rendre None plutôt que deviner : approvisionner
        le mauvais item bâtirait une chaîne entière vers rien.
        """
        from services.site_finder import _entites_a
        ligne = next((e for e in _entites_a(self.api, c.x, c.y, 1.5)
                      if e.get("name") == c.name), None)
        recette = (ligne or {}).get("recipe")
        if recette and recette != "none":
            info = self.api.get_recipe(recette)
            ingredients = (info or {}).get("ingredients") or []
            if ingredients:
                premier = ingredients[0]
                return premier.get("name") if isinstance(premier, dict) else str(premier)
        # Un four n'a pas de recette réglée : il fond ce qu'on lui donne.
        if "furnace" in str((ligne or {}).get("type", "")) or "furnace" in c.name:
            return self.ressource
        return None

    # ----- APPROVISIONNER (automatiser ce qu'on remplissait à la main) -----

    # Au-delà, une belt d'approvisionnement coûte plus qu'elle ne rapporte : c'est un
    # problème de transport longue distance (trains), pas de logistique locale. Le dire
    # vaut mieux que poser 200 belts qui traverseront lacs et falaises.
    PORTEE_APPRO = 60.0

    # Un stack plein dans le foreur. Le bras de RETOUR, qui rendrait la chaîne
    # réellement perpétuelle, n'est pas toujours plaçable : la belt part du bord même du
    # foreur, et un inserter doit se tenir ENTRE sa source et sa cible. Quand la
    # géométrie le refuse, l'amorce est ce qui reste — 50 charbons tiennent une vingtaine
    # de minutes à 0.0375 charbon/s. La limite est dite en clair dans le compte rendu
    # plutôt que masquée par un « chaîne bâtie » qui laisserait croire l'affaire close.
    AMORCE = 50
    AMORCE_BRAS = 5
    # Ce qu'une chaîne tout-burner réclame en charbon pour démarrer : trois machines qui
    # brûlent, plus la marge que l'executor exige au pré-vol. Mesuré, il manquait 7
    # unités avec 8 en poche — 20 laisse de quoi poser sans revenir miner aussitôt.
    AMORCE_CHAINE_BURNER = 20

    def choisir_gisement(self, resource: str, depuis: tuple[float, float],
                         portee_max: float = 60.0):
        """Quel gisement exploiter. Le déterministe énumère, l'arbitre choisit.

        C'est le premier arbitrage du projet où plusieurs réponses se valent réellement :
        mesuré en jeu, un gisement de fer à 174 tuiles n'en contient que 136 quand un
        autre, à 280, en contient 738. Ni la distance ni la taille ne domine, et un
        gisement bordé d'un nid perdra sa belt avant d'avoir servi.

        Le veto du déterministe porte sur la LÉGALITÉ (portée d'une belt), jamais sur la
        préférence. L'option 0 reste « le plus proche » — le comportement historique,
        donc ce que fait la boucle si aucun arbitre n'est branché ou s'il défaille.
        """
        from services.gisements import enumerer
        options = enumerer(self.api, resource, depuis, portee_max=portee_max)
        if not options:
            return None
        if self.arbitre is None or len(options) == 1:
            return options[0]
        # On emprunte le contrat de l'arbitre tel quel : des `Decision` décrivant chacune
        # une option, et un indice en retour. Rien de neuf à apprendre pour le modèle.
        propositions = [Decision(action="exploiter_gisement", raison=str(g),
                                 priorite=PRIORITE["batir_production"])
                        for g in options]
        try:
            i = self.arbitre(self.observer_leger(), propositions)
        except Exception as e:
            self.journal.append(f"arbitrage du gisement en erreur : {type(e).__name__}")
            i = 0
        self.arbitrages.append(("gisement", len(options),
                                i if isinstance(i, int) and not isinstance(i, bool) else 0))
        if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(options):
            i = 0
        if i != 0:
            self.journal.append(f"gisement choisi hors du plus proche : {options[i]}")
        return options[i]

    def observer_leger(self) -> EtatUsine:
        """État minimal pour un arbitrage annexe, sans repayer un diagnostic complet."""
        return EtatUsine(machines=0, inventaire=perception.inventory(self.api),
                         menace=self.derniere_menace)

    def _relais_d_alimentation(self, cx: float, cy: float, bras: str,
                               exclure=()) -> Optional[tuple]:
        """Où faire ARRIVER la belt pour qu'un bras puisse charger la machine.

        La belt ne décide plus, le BRAS décide. C'est l'inversion qui manquait : on
        traçait la ligne d'abord, puis on cherchait un emplacement — après chaque recul,
        donc sur une belt encore en cours d'allongement et triée depuis un bout périmé.
        Le résultat dépendait de l'encombrement du terrain, ce qui est le signe d'un
        placement fragile et non d'un défaut isolé.

        On cherche donc d'abord le couple (bras, tuile amont) : le bras adjacent à la
        machine, la tuile juste derrière lui pour la belt. La belt visera CETTE tuile.

        Les distances sont essayées croissantes parce que l'emprise varie — un four 2×2
        et une assembleuse 3×3 n'offrent pas leurs bords au même endroit, et rien dans
        `entity_row` ne donne la bounding box.
        """
        import math
        from services.site_finder import can_place

        interdits = {(math.floor(x) + 0.5, math.floor(y) + 0.5) for x, y in exclure}
        for d in (1.5, 2.0, 2.5):
            for ux, uy in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
                bx = math.floor(cx + ux * d) + 0.5
                by = math.floor(cy + uy * d) + 0.5
                px, py = bx + ux, by + uy
                if (bx, by) in interdits or (px, py) in interdits:
                    continue
                if (can_place(self.api, bras, bx, by)
                        and can_place(self.api, "transport-belt", px, py)):
                    return (bx, by), (px, py)
        return None

    def _relais_de_retour(self, drill, depart, bras: str, foreur: str):
        """Une tuile par où faire passer la belt pour qu'un bras puisse réalimenter le foreur.

        Retourne la position de RELAIS (sur le trajet de la belt), ou None si aucune ne
        convient. Le bras se tiendra entre ce relais et le foreur.

        On cherche une tuile à DEUX pas du bord du foreur, avec une tuile libre entre les
        deux : c'est la seule configuration où un inserter peut à la fois puiser et
        redonner. Le drop du foreur est exclu — c'est là que la belt commence, et le bras
        n'y tiendrait pas.
        """
        import math
        from services.site_finder import can_place

        dx0, dy0 = math.floor(depart[0]) + 0.5, math.floor(depart[1]) + 0.5
        # Milieux des quatre côtés d'une emprise 2×2, puis la tuile juste au-delà.
        cotes = ((1.5, -0.5), (1.5, 0.5), (-1.5, -0.5), (-1.5, 0.5),
                 (-0.5, 1.5), (0.5, 1.5), (-0.5, -1.5), (0.5, -1.5))
        for ox, oy in cotes:
            bx = math.floor(drill.x + ox) + 0.5
            by = math.floor(drill.y + oy) + 0.5
            ux, uy = (1.0 if ox > 0 else -1.0, 0.0) if abs(ox) > abs(oy) \
                else (0.0, 1.0 if oy > 0 else -1.0)
            px, py = bx + ux, by + uy
            if (bx, by) == (dx0, dy0) or (px, py) == (dx0, dy0):
                continue                      # le drop : la belt y commence déjà
            if can_place(self.api, bras, bx, by) and can_place(self.api, "transport-belt",
                                                               px, py):
                return (px, py)
        return None

    # Quelle machine sait faire quoi. La catégorie vient de la recette elle-même
    # (`get_recipe`), pas d'une devinette sur le nom de l'item.
    MACHINE_POUR = {"smelting": "stone-furnace", "crafting": "assembling-machine-1",
                    "basic-crafting": "assembling-machine-1",
                    "advanced-crafting": "assembling-machine-1"}

    def produire(self, cible, item: str) -> tuple[bool, str]:
        """Bâtit de quoi FABRIQUER `item` et l'apporter à `cible`.

        C'est le verrou que la première partie longue a désigné : l'agent savait relier un
        gisement à une machine, jamais fabriquer ce qu'une machine attend. Une assembleuse
        réclamant des plaques restait donc à l'arrêt pendant qu'il cherchait — en vain —
        un gisement de plaques de fer.

        Le montage tient en trois gestes, tous déjà éprouvés séparément :
          1. poser la machine qui sait faire `item`, à portée de bras de la cible ;
          2. la relier à la cible par un inserter, vérifié par LECTURE ;
          3. l'approvisionner en son ingrédient — c'est `approvisionner`, inchangé.

        On ne traite qu'UN étage : si l'ingrédient se fabrique lui aussi, on le dit au
        lieu de descendre récursivement. Une chaîne à trois étages posée d'un coup est
        exactement ce que le benchmark FLE voit échouer chez les agents ; mieux vaut un
        étage qui marche et un constat clair sur le suivant.
        """
        import math
        from services import site_finder

        recette = self.api.get_recipe(item) or {}
        ingredients = recette.get("ingredients") or []
        if not ingredients:
            return False, f"« {item} » n'a pas de recette : il s'extrait, il ne se produit pas"
        categorie = str(recette.get("category", "crafting"))
        machine = self.MACHINE_POUR.get(categorie)
        if machine is None:
            return False, f"aucune machine connue pour la catégorie « {categorie} »"
        premier = ingredients[0]
        besoin = premier.get("name") if isinstance(premier, dict) else str(premier)
        if (self.api.get_recipe(besoin) or {}).get("ingredients"):
            return False, (f"« {item} » demande « {besoin} », qui se fabrique aussi : "
                           f"il faudrait deux étages, on n'en bâtit qu'un")

        inv = perception.inventory(self.api)
        if inv.get(machine, 0) < 1:
            return False, f"aucun {machine} en inventaire pour fabriquer « {item} »"

        # OÙ est la matière première : c'est elle qui commande la géométrie. Les trois
        # pièces doivent s'aligner — gisement, machine, cible — sinon la belt arrive du
        # mauvais côté et la machine se retrouve enfermée entre sa livraison et un mur.
        # Mesuré : four posé à l'est de l'assembleuse, gisement à l'ouest, belt contrainte
        # de contourner et s'arrêtant en diagonale. Chaîne complète, four à jeun.
        source = self.sans_ecoulement(self.choisir_gisement, besoin,
                                      (cible.x, cible.y), self.PORTEE_APPRO)
        if source is None:
            return False, (f"aucun gisement de « {besoin} » à moins de "
                           f"{self.PORTEE_APPRO:.0f} tuiles pour fabriquer « {item} »")
        gx, gy = source.x - cible.x, source.y - cible.y
        if abs(gx) >= abs(gy):
            vers_source = (1.0 if gx > 0 else -1.0, 0.0)
        else:
            vers_source = (0.0, 1.0 if gy > 0 else -1.0)

        # 1. La machine, à portée de bras de la cible — mais avec de la PLACE DERRIÈRE.
        #
        # Mesuré : posée à trois tuiles, le bras de livraison d'un côté et l'assembleuse
        # de l'autre, elle se retrouvait enfermée. Sa propre belt d'alimentation arrivait
        # à trois tuiles sans plus pouvoir la charger — chaîne complète, machine à jeun.
        # Une machine intermédiaire a DEUX faces à desservir, pas une.
        # La machine se pose de PRÉFÉRENCE du côté du gisement, avec deux tuiles libres
        # en amont pour sa belt et son bras d'entrée. Mais une position n'est retenue que
        # si un bras peut RÉELLEMENT livrer la cible depuis là : `can_place` ne le dit
        # pas. Mesuré — à trois tuiles, un four 2×2 et une assembleuse 2.4×2.4 se
        # touchent presque, et aucun inserter ne tient entre les deux. On pose, on teste,
        # on retire, on essaie la suivante ; abandonner au premier échec ne posait rien.
        ux, uy = vers_source
        perp = (-uy, ux)
        directions = [(ux, uy), perp, (-perp[0], -perp[1]), (-ux, -uy)]
        # 3 ou 4 tuiles, pas plus : un inserter ne relie que des VOISINS.
        candidats = [(vx * k, vy * k) for k in (3.0, 4.0) for vx, vy in directions]
        bras = "burner-inserter" if inv.get("burner-inserter", 0) else "inserter"

        pose, livraison = None, None
        essais: list[str] = []
        for exigeant in (True, False):        # d'abord l'idéal, puis ce qui reste
            for dx, dy in candidats:
                mx = math.floor(cible.x + dx) + 0.5
                my = math.floor(cible.y + dy) + 0.5
                if not site_finder.can_place(self.api, machine, mx, my):
                    continue
                vx = 0.0 if dx == 0 else (1.0 if dx > 0 else -1.0)
                vy = 0.0 if dy == 0 else (1.0 if dy > 0 else -1.0)
                if exigeant and not all(
                        site_finder.can_place(self.api, "transport-belt",
                                              mx + vx * j, my + vy * j)
                        for j in (2.0, 3.0)):
                    continue
                r = self.api.run_action(self.api.place_entity_at, machine, mx, my,
                                        "north", None, timeout=30.0)
                if not (isinstance(r, dict) and r.get("ok")):
                    continue
                if categorie != "smelting":
                    self.api.run_action(self.api.set_recipe_at, mx, my, item, machine,
                                        timeout=20.0)
                # Le bras qui livre : il PUISE dans la machine et DÉPOSE dans la cible —
                # l'inverse d'un bras d'alimentation, d'où `source_types`.
                livraison = site_finder.place_inserter_vers(
                    self.api, (cible.x, cible.y), (mx, my), cible.name, nom=bras,
                    source_types=(machine,))
                if livraison is not None:
                    pose = (mx, my)
                    break
                essais.append(f"({mx},{my}) : aucun bras ne livre")
                self.api.run_action(self.api.remove_entity_at, mx, my, machine,
                                    timeout=20.0)
            if pose is not None:
                break
        if pose is None:
            return False, (f"aucune place pour un {machine} qui puisse livrer "
                           f"{cible.name} — {' ; '.join(essais[:3])}")

        if bras == "burner-inserter":
            self.api.run_action(self.api.move_items_at, "coal", bras, livraison[0],
                                livraison[1], self.AMORCE_BRAS, True, timeout=20.0)
        # Un four brûle : sans amorce il ne fondra rien, et la boucle le verra `no_fuel`.
        if machine == "stone-furnace":
            self.api.run_action(self.api.move_items_at, "coal", machine, pose[0], pose[1],
                                self.AMORCE_BRAS * 2, True, timeout=20.0)

        # 4. L'entrée : c'est exactement le problème déjà résolu.
        faux_symptome = Symptome(name=machine, x=pose[0], y=pose[1],
                                 cause="entree_vide", gravite=2,
                                 detail=f"doit recevoir {besoin}")
        ok, detail = self.approvisionner(faux_symptome, besoin)
        return ok, (f"{machine}@{pose} fabrique « {item} » pour {cible.name} "
                    f"({bras}@{livraison[:2]}) — entrée : {detail}")

    def tiers_micro(self) -> dict:
        """Quelles machines poser : électriques si on peut, burner sinon.

        `batir_production` réclamait `electric-mining-drill` et `electric-furnace` EN
        DUR. Or, mesuré sur une carte neuve, ces recettes sont `enabled=false` : elles
        demandent une recherche non faite. Sans dotation, l'agent ne pouvait donc rien
        bâtir — il exigeait des machines qui n'existaient ni dans ses poches ni dans son
        arbre technologique.

        Le tout-burner est le début d'une partie de Factorio : foreuse à charbon, four de
        pierre, bras à charbon. Aucune ne demande de recherche, toutes se fabriquent, et
        le MicroPlanner sait déjà les placer (leur emprise est 2×2, contre 3×3 pour
        l'électrique — une erreur d'une tuile ici fait tomber le minerai par terre).

        On préfère l'électrique dès qu'il est POSSIBLE : soit en stock, soit fabricable.
        Une chaîne burner mange du charbon et demande à être ravitaillée ; c'est un
        départ, pas une fin.
        """
        inv = perception.inventory(self.api)
        electrique = (inv.get("electric-mining-drill", 0) > 0
                      or perception.recipe_of(self.api, "electric-mining-drill") is not None)
        if electrique:
            return {"drill": "electric-mining-drill", "inserter": "inserter",
                    "furnace": "electric-furnace", "drill_size": 3, "furnace_size": 3,
                    "nom": "électrique"}
        return {"drill": "burner-mining-drill", "inserter": "burner-inserter",
                "furnace": "stone-furnace", "drill_size": 2, "furnace_size": 2,
                "nom": "burner"}

    def fabriquer(self, item: str, combien: int = 1) -> tuple[bool, str]:
        """Se procure `item` : miner, fondre, crafter — dans cet ordre s'il le faut.

        C'est ce qui sépare un agent autonome d'un agent approvisionné. Jusqu'ici il
        consommait une dotation qu'un humain lui avait mise dans les poches (21 lots) et
        s'arrêtait quand elle était vide : il ne savait pas produire une foreuse de plus.

        Le plan vient de `knowledge.plan_production`, qui arbitre sur l'INVENTAIRE — ce
        qu'on possède déjà n'est pas refabriqué — et descend récursivement jusqu'au
        minerai. `BaseAgent.act` l'exécute en s'arrêtant à la première étape ratée.

        Une recette VERROUILLÉE est dite comme telle : `small-electric-pole`, `inserter`
        et `electric-mining-drill` sont `enabled=false` sur une carte neuve (mesuré).
        Ce n'est pas un trou du planificateur, c'est la recherche qui manque — et
        confondre les deux enverrait chercher au mauvais endroit.
        """
        from services.knowledge import ProductionGoal, plan_production, plan_summary

        inv = perception.inventory(self.api)
        try:
            steps = plan_production(ProductionGoal(item, combien), inv,
                                    lambda x: perception.recipe_of(self.api, x))
        except ValueError as e:
            # Une recette FERMÉE n'est pas une impasse : quelque chose l'ouvre. On ne
            # se contente donc plus de le constater — on va chercher la clé.
            if "VERROUILL" in str(e).upper():
                return self.ouvrir_la_recette(item)
            return False, f"{item} ne peut pas être fabriqué : {e}"
        if not steps:
            return False, f"{item} : rien à faire, l'inventaire en contient déjà assez"

        avant = inv.get(item, 0)
        resultats = self.builder.act(steps)
        # On ne croit pas `ok=True` : c'est l'INVENTAIRE qui tranche (leçon E1). Un craft
        # mis en file peut échouer plus tard, et une étape verte ne prouve rien.
        apres = perception.inventory(self.api).get(item, 0)
        gagne = apres - avant
        rates = [r for r in resultats if not (isinstance(r, dict) and r.get("ok") is True)]
        return gagne > 0, (f"{item} : {avant} -> {apres} ({gagne:+d}) en "
                           f"{len(steps)} étape(s) [{plan_summary(steps)[:70]}]"
                           + (f" — bloqué sur {str(rates[0])[:60]}" if rates else ""))

    def ouvrir_la_recette(self, recette: str) -> tuple[bool, str]:
        """Va chercher la technologie qui débloque `recette`, et la déclenche.

        Le début de l'arbre de Factorio 2.0 ne se paie pas en flacons mais en GESTES :
        `electronics` s'ouvre en fabriquant dix plaques de cuivre, et donne d'un coup
        `copper-cable`, `electronic-circuit`, `lab`, `inserter` et `small-electric-pole`.
        Un agent qui sait fondre peut donc ouvrir tout l'électrique de base sans posséder
        ni laboratoire ni flacon — mesuré en jeu : douze plaques de cuivre fondues, et
        les quatre recettes passent à `enabled`.

        Le piège, payé une fois : un déclencheur compte ce qu'on FABRIQUE, jamais ce
        qu'on possède. Avec trente plaques en poche, demander « fabrique-en dix » rend
        « l'inventaire en contient déjà assez », rien n'est produit, et la technologie
        reste fermée. `quantite_a_produire` vise donc le stock ACTUEL plus le compte du
        déclencheur.

        Quand la marche se paie en science, on le DIT au lieu de s'acharner : il faut
        alors un laboratoire et des flacons, c'est-à-dire un autre chantier.
        """
        from services import recherche

        arbre = recherche.lire(self.api)
        marche = arbre.pour_recette(recette)
        if marche is None:
            return False, (f"{recette} est verrouillée et aucune technologie à portée "
                           f"ne l'ouvre — il faut d'abord en chercher d'autres "
                           f"({len(arbre.marches)} marche(s) disponible(s))")
        if marche.declencheur is None or not marche.gratuite:
            return False, (f"{recette} s'ouvre par « {marche.nom} », qui se paie en "
                           f"science : {marche.unites} x "
                           + " + ".join(f"{c} {n}" for n, c in marche.cout)
                           + " — il faut un laboratoire alimenté, pas un craft")

        cible, vise = recherche.quantite_a_produire(
            marche, perception.inventory(self.api))
        if not cible:
            return False, (f"{recette} s'ouvre par « {marche.nom} », dont le "
                           f"déclencheur ({marche.declencheur[0]}) n'est pas un objet "
                           f"à fabriquer")

        ok, detail = self.fabriquer(cible, vise)
        # Même précaution que dans `chercher` : le déclencheur tombe au tick suivant la
        # production, jamais dans la foulée du craft.
        self.api.run_action(self.api.wait, 30, timeout=30.0)
        acquise = recherche.lire(self.api).acquises
        gagnee = marche.nom in acquise
        return gagnee, (f"pour ouvrir {recette} : {marche} — {detail}"
                        + ("" if gagnee else f" — « {marche.nom} » toujours pas acquise"))

    def poser_le_laboratoire(self) -> tuple[Optional[tuple[float, float]], str]:
        """Trouve un laboratoire en service, ou en pose un et le branche.

        Un laboratoire est ÉLECTRIQUE : posé hors de toute couverture, il consomme des
        flacons sans jamais chercher, et la recherche paraîtrait simplement lente. On le
        relie donc à la pose — même règle que pour les chaînes et les bras d'évacuation,
        apprise trois fois : ce qu'on pose, on l'alimente.
        """
        from services import site_finder

        deja = [e for e in site_finder._entites_a(self.api, self.zone[0], self.zone[1],
                                                  self.rayon)
                if e.get("name") == "lab"]
        if deja:
            x, y = float(deja[0].get("x", 0.0)), float(deja[0].get("y", 0.0))
            return (x, y), f"laboratoire déjà en place en ({x:.0f},{y:.0f})"

        inv = perception.inventory(self.api)
        if not inv.get("lab", 0):
            ok, detail = self.fabriquer("lab", 1)
            if not ok:
                return None, f"aucun laboratoire, et impossible d'en fabriquer un : {detail}"

        # On cherche autour d'un poteau ALIMENTÉ : poser d'abord et brancher ensuite
        # oblige à tirer une ligne, alors qu'il suffit de se poser là où le courant est.
        source = site_finder.poteau_alimente_le_plus_proche(
            self.api, self.zone[0], self.zone[1])
        centre = (source[0], source[1]) if source else self.zone
        for dx in range(-6, 7, 2):
            for dy in range(-6, 7, 2):
                x = float(int(centre[0] + dx)) + 0.5
                y = float(int(centre[1] + dy)) + 0.5
                if not site_finder.can_place(self.api, "lab", x, y):
                    continue
                r = self.api.run_action(self.api.place_entity_at, "lab", x, y,
                                        "north", None, timeout=20.0)
                pose = any(e.get("name") == "lab"
                           for e in site_finder._entites_a(self.api, x, y, 1.5))
                if (isinstance(r, dict) and r.get("ok")) or pose:
                    etat = self.api.get_power_state(x, y, 1.5) or {}
                    if etat.get("connected") is not True:
                        self.relier(Symptome(name="lab", x=x, y=y, cause="debranchee",
                                             gravite=1, detail="laboratoire posé à l'instant"))
                    apres = self.api.get_power_state(x, y, 1.5) or {}
                    return (x, y), (f"laboratoire posé en ({x:.0f},{y:.0f})"
                                    + ("" if apres.get("connected") is True
                                       else " — MAIS toujours sans courant"))
        return None, "aucune place libre pour un laboratoire près du réseau"

    def chercher(self, techno: str) -> tuple[bool, str]:
        """Obtient une technologie : par le geste qui la déclenche, ou en la payant.

        Deux régimes, et les confondre fait perdre la partie de deux façons opposées :
        s'acharner sur la file de recherche pour une technologie qui attend un craft, ou
        attendre un craft pour une technologie qui réclame vingt flacons dans un
        laboratoire.

        Le paiement est décomposé jusqu'au minerai par `fabriquer` : un flacon vaut une
        plaque de cuivre et un engrenage, donc vingt flacons valent vingt cuivres et
        quarante fers, que l'agent va miner et fondre s'il ne les a pas.
        """
        from services import recherche

        arbre = recherche.lire(self.api)
        if techno in arbre.acquises:
            return True, f"« {techno} » est déjà acquise"
        marche = next((m for m in arbre.marches if m.nom == techno), None)
        if marche is None:
            return False, (f"« {techno} » n'est pas à portée : soit elle est déjà "
                           f"acquise, soit il lui manque des prérequis")

        # Régime 1 : un geste suffit.
        if marche.declencheur is not None and marche.gratuite:
            cible, vise = recherche.quantite_a_produire(
                marche, perception.inventory(self.api))
            if cible:
                ok, detail = self.fabriquer(cible, vise)
                # Le jeu évalue les déclencheurs au tick SUIVANT la production. Relire
                # dans la foulée du craft fait conclure à l'échec sur une technologie
                # qui tombe un instant plus tard : mesuré au banc, `chercher` rendait
                # False pendant que le compteur d'acquises passait bien de 2 à 3.
                self.api.run_action(self.api.wait, 30, timeout=30.0)
                acquise = techno in recherche.lire(self.api).acquises
                return acquise, f"{marche} — {detail}"

        # Régime 2 : il faut payer. On fabrique les flacons AVANT de poser quoi que ce
        # soit : inutile de bâtir un laboratoire qu'on ne pourra pas alimenter.
        inv = perception.inventory(self.api)
        manques = []
        for nom, par_unite in marche.cout:
            besoin = par_unite * marche.unites
            if inv.get(nom, 0) < besoin:
                ok, detail = self.fabriquer(nom, inv.get(nom, 0) + besoin - inv.get(nom, 0))
                if not ok:
                    manques.append(f"{nom} ({inv.get(nom, 0)}/{besoin}) : {detail[:60]}")
        if manques:
            return False, f"« {techno} » non payée — {' ; '.join(manques)}"

        ou, detail_lab = self.poser_le_laboratoire()
        if ou is None:
            return False, f"« {techno} » : {detail_lab}"

        # Les flacons vont DANS le laboratoire : les garder en poche ne cherche rien.
        inv = perception.inventory(self.api)
        charges = []
        for nom, par_unite in marche.cout:
            combien = min(inv.get(nom, 0), par_unite * marche.unites)
            if combien > 0:
                self.api.run_action(self.api.move_items_at, nom, "lab", ou[0], ou[1],
                                    combien, True, timeout=20.0)
                charges.append(f"{combien} {nom}")

        r = self.api.run_action(self.api.research_technology, techno, timeout=180.0)
        acquise = techno in recherche.lire(self.api).acquises
        return acquise, (f"{marche} — {detail_lab}, chargé de {', '.join(charges) or 'rien'}"
                         + (f" — {r.get('detail')}" if isinstance(r, dict) else "")
                         + ("" if acquise else " — TOUJOURS PAS acquise"))

    def automatiser_la_science(self, flacon: str = "automation-science-pack",
                               provision: int = 50) -> tuple[bool, str]:
        """Fait couler la science toute seule : une assembleuse verse dans le laboratoire.

        Jusqu'ici chaque recherche coûtait une campagne : l'agent fabriquait ses flacons
        à la main, les portait au laboratoire, et recommençait à la suivante. Tant que
        c'est lui qui les porte, la recherche s'arrête dès qu'il regarde ailleurs — ce
        n'est pas une usine, c'est une corvée.

        On monte donc le maillon qui manque : une assembleuse réglée sur le flacon, un
        bras qui la vide dans le laboratoire, et de quoi tenir. Les ingrédients sont
        déposés en provision dans l'assembleuse ; ce qui suit — les alimenter par une
        chaîne plutôt qu'à la main — est le chantier d'après, et il ne change rien à
        celui-ci : ce qu'on éprouve ici est que le laboratoire reçoit des flacons que
        PERSONNE ne lui a portés.
        """
        from services import site_finder

        # 1. Savoir faire le FLACON avant de vouloir l'automatiser. Sans cette recette,
        # `set_recipe` passe sans erreur sur une assembleuse qui ne produira jamais rien,
        # et le chargement des ingrédients ne trouve aucune recette à lire : mesuré,
        # « assembleuse réglée sur automation-science-pack, chargée de rien » — tout
        # était posé, relié, alimenté, et l'entrée restait à zéro.
        if perception.recipe_of(self.api, flacon) is None:
            ok, detail = self.ouvrir_la_recette(flacon)
            if not ok:
                return False, f"le flacon {flacon} n'est pas fabricable : {detail}"

        # 2. L'assembleuse doit l'être aussi : c'est `automation` qui l'ouvre, et
        # `chercher` sait la payer. On ne suppose pas, on va la chercher.
        inv = perception.inventory(self.api)
        machine = "assembling-machine-1"
        if not inv.get(machine, 0) and perception.recipe_of(self.api, machine) is None:
            ok, detail = self.ouvrir_la_recette(machine)
            if not ok:
                return False, f"pas d'assembleuse possible : {detail}"

        # 2. On cherche un COUPLE de places alignées, et non une place puis l'autre.
        #
        # Poser le laboratoire d'abord le mettait au plus près du courant, donc au milieu
        # de ce qui est déjà bâti : aucune des quatre places axiales voisines n'était
        # libre, et l'assembleuse n'avait plus où aller. Le repli diagonal essayé ensuite
        # était pire — deux machines en diagonale ne se font pas face, et aucun bras ne
        # peut les relier : « réglée sur automation-science-pack, mais aucun bras ne
        # relie ». Une place libre qui ne peut pas être servie n'est pas une place.
        #
        # AXIAL, donc, et cherché à deux : deux 3x3 espacés de quatre tuiles laissent
        # exactement la tuile du bras entre elles.
        if not perception.inventory(self.api).get(machine, 0):
            ok, detail = self.fabriquer(machine, 1)
            if not ok:
                return False, f"assembleuse non fabriquée : {detail}"

        source = site_finder.poteau_alimente_le_plus_proche(
            self.api, self.zone[0], self.zone[1])
        centre = (source[0], source[1]) if source else self.zone

        def _libre(nom: str, x: float, y: float) -> bool:
            return bool(site_finder.can_place(self.api, nom, x, y))

        def _pose_ici(nom: str, x: float, y: float) -> bool:
            deja = any(e.get("name") == nom
                       for e in site_finder._entites_a(self.api, x, y, 1.5))
            if deja:
                return True
            r = self.api.run_action(self.api.place_entity_at, nom, x, y, "north",
                                    None, timeout=20.0)
            return ((isinstance(r, dict) and r.get("ok") is True)
                    or any(e.get("name") == nom
                           for e in site_finder._entites_a(self.api, x, y, 1.5)))

        couple = None
        for rayon in (0, 4, 8, 12):
            for ax in range(-rayon, rayon + 1, 4):
                for ay in range(-rayon, rayon + 1, 4):
                    ax_, ay_ = float(int(centre[0] + ax)) + 0.5, float(int(centre[1] + ay)) + 0.5
                    if not _libre(machine, ax_, ay_):
                        continue
                    for dx, dy in ((4, 0), (-4, 0), (0, 4), (0, -4)):
                        lx, ly = ax_ + dx, ay_ + dy
                        proche_lab = [e for e in site_finder._entites_a(self.api, lx, ly, 1.5)
                                      if e.get("name") == "lab"]
                        if proche_lab or _libre("lab", lx, ly):
                            couple = ((ax_, ay_), (lx, ly))
                            break
                    if couple:
                        break
                if couple:
                    break
            if couple:
                break
        if couple is None:
            return False, ("aucun couple de places alignées pour une assembleuse et un "
                           "laboratoire près du réseau — le terrain est trop encombré")

        pose, labo = couple
        if not perception.inventory(self.api).get("lab", 0) and not any(
                e.get("name") == "lab"
                for e in site_finder._entites_a(self.api, labo[0], labo[1], 1.5)):
            ok, detail = self.fabriquer("lab", 1)
            if not ok:
                return False, f"laboratoire non fabriqué : {detail}"
        if not _pose_ici("lab", labo[0], labo[1]):
            return False, f"laboratoire non posé en ({labo[0]:.0f},{labo[1]:.0f})"
        if not _pose_ici(machine, pose[0], pose[1]):
            return False, f"assembleuse non posée en ({pose[0]:.0f},{pose[1]:.0f})"

        # Les deux sont électriques : sans courant, l'une a une recette et l'autre des
        # flacons, et rien ne bouge.
        for nom, (x, y) in ((("lab"), labo), ((machine), pose)):
            etat = self.api.get_power_state(x, y, 1.5) or {}
            if etat.get("connected") is not True:
                self.relier(Symptome(name=nom, x=x, y=y, cause="debranchee",
                                     gravite=1, detail="posée à l'instant"))
        detail_labo = f"laboratoire en ({labo[0]:.0f},{labo[1]:.0f})"

        # 3. La recette, sans quoi l'assembleuse est un meuble.
        self.api.run_action(self.api.set_recipe_at, pose[0], pose[1], flacon,
                            timeout=20.0)

        # 5. De quoi tenir. Les ingrédients viennent du plan, donc du minerai si besoin.
        recette = perception.recipe_of(self.api, flacon) or []
        charges = []
        for ing in recette:
            nom = ing[0] if isinstance(ing, (tuple, list)) else str(ing)
            par_flacon = ing[1] if isinstance(ing, (tuple, list)) and len(ing) > 1 else 1
            besoin = par_flacon * provision
            if perception.inventory(self.api).get(nom, 0) < besoin:
                self.fabriquer(nom, besoin)
            combien = min(perception.inventory(self.api).get(nom, 0), besoin)
            if combien > 0:
                self.api.run_action(self.api.move_items_at, nom, machine,
                                    pose[0], pose[1], combien, True, timeout=20.0)
                charges.append(f"{combien} {nom}")

        # 6. Le bras qui verse dans le laboratoire. On ne DÉDUIT pas son sens : la pose
        # lit `pickup`/`drop` réels et tourne jusqu'à ce que les deux tombent où il faut.
        bras = ("inserter" if perception.inventory(self.api).get("inserter", 0)
                or perception.recipe_of(self.api, "inserter") is not None
                else "burner-inserter")
        if not perception.inventory(self.api).get(bras, 0):
            self.fabriquer(bras, 1)
        pont = site_finder.place_inserter_vers(
            self.api, labo, pose, "lab", nom=bras, source_types=(machine,))
        if pont is None:
            return False, (f"assembleuse en ({pose[0]:.0f},{pose[1]:.0f}) réglée sur "
                           f"{flacon}, mais aucun bras ne relie au laboratoire")
        if bras == "burner-inserter":
            self.api.run_action(self.api.move_items_at, "coal", bras, pont[0], pont[1],
                                self.AMORCE_BRAS, True, timeout=20.0)

        return True, (f"assembleuse ({pose[0]:.0f},{pose[1]:.0f}) réglée sur {flacon}, "
                      f"chargée de {', '.join(charges) or 'rien'}, "
                      f"{bras}@({pont[0]:.0f},{pont[1]:.0f}) verse dans le laboratoire "
                      f"({labo[0]:.0f},{labo[1]:.0f})")

    def alimenter_la_science(self, flacon: str = "automation-science-pack"
                             ) -> tuple[bool, str]:
        """Branche l'assembleuse de science sur ce qui PRODUIT ses ingrédients.

        `automatiser_la_science` a monté le maillon aval — l'assembleuse verse dans le
        laboratoire. Il reste l'amont : elle tourne sur une provision déposée à la main,
        donc elle s'arrête dès qu'elle l'a consommée. Ce qui suit ferme la boucle.

        Pour chaque ingrédient, on cherche une machine qui le produit ; s'il n'y en a
        pas, on bâtit ce qu'il faut — une chaîne sur le minerai pour une plaque, une
        assembleuse dédiée pour une pièce intermédiaire — puis on l'amène.
        """
        from services import site_finder

        assembleuses = [e for e in site_finder._entites_a(
            self.api, self.zone[0], self.zone[1], self.rayon)
            if e.get("name") == "assembling-machine-1"]
        cible = None
        for e in assembleuses:
            x, y = float(e.get("x", 0.0)), float(e.get("y", 0.0))
            rec = str(self.api.rcon.query_lua(
                f"local s = game.surfaces[1] local n = '' "
                f"for _, m in pairs(s.find_entities_filtered{{name='assembling-machine-1', "
                f"area={{{{{x - 0.6},{y - 0.6}}},{{{x + 0.6},{y + 0.6}}}}}}}) do "
                f"local ok, r = pcall(function() return m.get_recipe() end) "
                f"if ok and r then n = r.name end end rcon.print(n)")).strip()
            if rec == flacon:
                cible = (x, y)
                break
        if cible is None:
            return False, (f"aucune assembleuse réglée sur {flacon} — il faut d'abord "
                           f"automatiser la science")

        # BÂTIR D'ABORD, RELIER ENSUITE, et c'est l'ordre qui compte.
        #
        # Mesuré : une machine qui vient d'être posée n'est pas encore reconnaissable
        # comme source. Un four n'a de recette qu'une fois qu'on lui a donné du minerai ;
        # tant qu'il est vide, il ne « produit » rien aux yeux de `_source_de`. En
        # bâtissant et reliant ingrédient par ingrédient, on demandait donc à
        # l'assembleuse à engrenages de se brancher sur un four de fer qui n'existait
        # pour elle qu'un instant plus tard : elle restait à sec, et toute la chaîne
        # butait sur l'ingrédient manquant — l'entrée de la science servie en continu,
        # et un seul flacon produit en six fenêtres.
        recette = perception.recipe_of(self.api, flacon) or []
        besoins = [ing[0] if isinstance(ing, (tuple, list)) else str(ing)
                   for ing in recette]
        intermediaires, manques = [], []
        for nom in besoins:
            if self._source_de(nom) is not None:
                continue
            ou = self._batir_la_source_de(nom)
            if not ou:
                manques.append(f"{nom} (rien ne le produit et rien n'a pu être bâti)")
            elif isinstance(ou, tuple):
                intermediaires.append((nom, ou))

        # On laisse les sources DÉMARRER, et il en faut plus qu'on ne croit : une chaîne
        # fraîchement posée doit extraire, transporter et fondre avant que son four ait
        # une recette — donc avant d'être repérable comme source. Mesuré avec 180 ticks :
        # « rien ne produit iron-plate à portée » sur une chaîne de fer bâtie deux tours
        # plus tôt, qui n'avait simplement pas encore sorti sa première plaque.
        self.api.run_action(self.api.wait, 900, timeout=300.0)

        faits = []
        # UNE SEULE RÉSERVATION POUR TOUS LES FLUX. Chaque couloir tracé s'y inscrit, si
        # bien que le suivant ne peut plus le proposer — même au-delà des 64 tuiles que
        # l'observation du jeu sait voir. C'est ce qui manquait : les lignes se
        # disputaient un terrain que chacune croyait libre.
        reserve: set = set()

        # Les sources intermédiaires d'abord : une assembleuse à engrenages qui n'a pas
        # de plaques ne sert à rien en aval.
        for nom, ou in intermediaires:
            for sous in (perception.recipe_of(self.api, nom) or []):
                s_nom = sous[0] if isinstance(sous, (tuple, list)) else str(sous)
                ok, detail = self.amener(s_nom, ou, "assembling-machine-1",
                                         reserve=reserve)
                (faits if ok else manques).append(f"[pour {nom}] {detail}")

        # ON LAISSE LEUR CHANCE AUX CHAÎNES LOINTAINES. Celle du cuivre est à soixante-dix
        # tuiles : entre la pose et la première plaque, il faut extraire, transporter et
        # fondre. Une seule tentative concluait « toujours aucune source » sur une chaîne
        # qui démarrait — et l'agent renonçait définitivement à s'y brancher.
        # LES LIAISONS COURTES D'ABORD. Mesuré : la belt du cuivre — quatre-vingts tuiles
        # à travers la carte — était posée en premier et traversait la zone de l'usine ;
        # il ne restait plus de passage pour relier deux assembleuses distantes de six
        # tuiles, et l'on lisait « aucun tracé libre (85 tuiles déjà prises) ». Une ligne
        # longue occupe beaucoup ; on lui laisse le terrain en dernier.
        def _eloignement(nom: str) -> float:
            s = self._source_de(nom)
            return math.hypot(s[1] - cible[0], s[2] - cible[1]) if s else 1e9

        restants = sorted(besoins, key=_eloignement)
        for passe in range(3):
            encore = []
            for nom in restants:
                if self._source_de(nom) is None:
                    encore.append(nom)
                    continue
                ok, detail = self.amener(nom, cible, "assembling-machine-1",
                                         reserve=reserve)
                (faits if ok else manques).append(detail)
            restants = encore
            if not restants or passe == 2:
                break
            self.api.run_action(self.api.wait, 1800, timeout=400.0)
        for nom in restants:
            manques.append(f"{nom} : aucune source après trois tentatives")

        return bool(faits) and not manques, (
            f"science alimentée : {' ; '.join(faits)}"
            + (f" — MANQUE : {' ; '.join(manques)}" if manques else ""))

    def _batir_la_source_de(self, item: str):
        """Fait apparaître de quoi produire `item` : une chaîne, ou une assembleuse.

        Une plaque se fond au bout d'une chaîne sur son minerai ; une pièce se fabrique
        dans une assembleuse qu'on règle et qu'on branche. Les deux montages existent
        déjà, il ne manquait que d'en choisir un.

        Rend True pour une chaîne (elle s'alimente seule depuis son gisement), et la
        POSITION pour une assembleuse — celle-ci devra être reliée à ses propres
        ingrédients, ce que l'appelant fait dans un second temps.
        """
        from services import site_finder

        minerai = {"copper-plate": "copper-ore", "iron-plate": "iron-ore",
                   "stone-brick": "stone"}.get(item)
        if minerai is not None:
            # ON VA D'ABORD SUR LE GISEMENT. `batir_production` cherche ses ancres via
            # `scan_patch`, qui scanne autour de l'AVATAR et non d'un point arbitraire —
            # limitation connue du socle. Tant que l'agent reste près du fer, le cuivre à
            # soixante-treize tuiles lui est invisible : la chaîne n'est bâtie que les
            # fois où il s'y trouvait par hasard, pour un minage. C'est toute
            # l'intermittence qu'on observait — un run sur trois s'effondrait dès le
            # premier constat, faute d'une source que rien n'avait pu voir.
            from services import deplacement

            ou = perception.nearest(self.api, minerai)
            if ou is not None and ou[2] > 40:
                deplacement.marcher_vers(self.api, ou[0], ou[1])

            precedente = self.ressource
            try:
                self.ressource = minerai
                ok, _ = self.agir(Decision(action="batir_production",
                                           raison=f"il faut du {item} pour la science"))
            finally:
                self.ressource = precedente
            if not ok:
                return False

            # ET ON LA BRANCHE — cinquième fois que cette règle se rappelle à nous. Une
            # chaîne bâtie à soixante-treize tuiles de la centrale n'a aucun courant : sa
            # foreuse n'extrait rien, son four ne reçoit donc jamais de minerai et n'a
            # jamais de recette. Elle est invisible comme source, et l'agent conclut
            # « rien ne produit de copper-plate » devant une chaîne qu'il vient de poser.
            # Ce qu'on pose, on l'alimente.
            for machine in site_finder._entites_a(self.api, *(perception.nearest(
                    self.api, minerai) or (self.zone[0], self.zone[1], 0))[:2], 12.0):
                if machine.get("type") not in ("mining-drill", "furnace", "inserter"):
                    continue
                self.brancher(str(machine.get("name")),
                              float(machine.get("x", 0.0)), float(machine.get("y", 0.0)))
            return True

        # Une pièce intermédiaire : une assembleuse de plus, réglée sur elle.
        if perception.recipe_of(self.api, item) is None:
            return False
        if not perception.inventory(self.api).get("assembling-machine-1", 0):
            fait, _ = self.fabriquer("assembling-machine-1", 1)
            if not fait:
                return False
        source = site_finder.poteau_alimente_le_plus_proche(
            self.api, self.zone[0], self.zone[1])
        centre = (source[0], source[1]) if source else self.zone
        # ON LAISSE LA PLACE AUX LIAISONS. Mesuré : l'assembleuse à engrenages était
        # posée à six tuiles de celle de science, et la belt qui l'alimente en fer
        # occupait aussitôt tout le passage entre les deux — « aucun tracé libre entre
        # assembling-machine-1 et assembling-machine-1 ». Deux machines qui doivent être
        # reliées par une belt, et alimentées chacune par une autre, ont besoin de plus
        # que de leur propre encombrement.
        # Un écart MINIMAL, pas maximal : porté à neuf tuiles, il éloignait tant
        # l'assembleuse que sa propre liaison se coupait à son tour et que la chaîne de
        # cuivre n'était plus bâtie du tout — 1/6 au lieu de 3/6. Les places se disputent
        # dans les deux sens, et rien ne dit qu'un écart fixe soit la bonne réponse.
        autres = [(float(e.get("x", 0.0)), float(e.get("y", 0.0)))
                  for e in site_finder._entites_a(self.api, centre[0], centre[1], 24.0)
                  if e.get("name") == "assembling-machine-1"]
        for dx in range(-8, 9, 2):
            for dy in range(-8, 9, 2):
                x, y = float(int(centre[0] + dx)) + 0.5, float(int(centre[1] + dy)) + 0.5
                if any(math.hypot(x - ax, y - ay) < 5.0 for ax, ay in autres):
                    continue
                if not site_finder.can_place(self.api, "assembling-machine-1", x, y):
                    continue
                self.api.run_action(self.api.place_entity_at, "assembling-machine-1",
                                    x, y, "north", None, timeout=20.0)
                if not any(e.get("name") == "assembling-machine-1"
                           for e in site_finder._entites_a(self.api, x, y, 1.5)):
                    continue
                self.api.run_action(self.api.set_recipe_at, x, y, item, timeout=20.0)
                etat = self.api.get_power_state(x, y, 1.5) or {}
                if etat.get("connected") is not True:
                    self.relier(Symptome(name="assembling-machine-1", x=x, y=y,
                                         cause="debranchee", gravite=1,
                                         detail="assembleuse posée à l'instant"))
                # Elle sera alimentée dans un SECOND temps : ses ingrédients viennent de
                # machines qui, à cet instant, peuvent n'avoir encore rien produit — donc
                # être invisibles comme sources. On rend sa position et l'appelant relie
                # une fois tout bâti.
                return (x, y)
        return False

    def brancher(self, nom: str, x: float, y: float) -> bool:
        """Donne du courant à ce qu'on vient de poser. Rend True si le courant est là.

        Quatrième fois que ce geste manquait, et à chaque fois la panne s'est présentée
        autrement : une chaîne bâtie hors couverture, un bras d'évacuation posé sans
        courant que la boucle rediagnostiquait au tour suivant, un laboratoire qui
        consommait ses flacons sans rien chercher, et enfin un bras de chargement à
        soixante-dix tuiles de la centrale — belt déroulée, bras posés, et pas un seul
        objet transporté. Rien à la pose ne le signale : l'entité est là, elle a l'air
        juste, et elle ne fait rien.

        La règle, désormais écrite une fois : CE QU'ON POSE, ON L'ALIMENTE.
        """
        etat = self.api.get_power_state(x, y, 1.2) or {}
        if etat.get("connected") is True:
            return True
        self.relier(Symptome(name=nom, x=x, y=y, cause="debranchee", gravite=1,
                             detail="posée à l'instant"))
        apres = self.api.get_power_state(x, y, 1.2) or {}
        return apres.get("connected") is True

    def _demi_largeur(self, x: float, y: float, defaut: float = 1.5) -> float:
        """La demi-largeur RÉELLE de la machine posée là — sa bounding box, pas son nom.

        Deviner coûte cher : un bras n'atteint qu'une tuile, si bien qu'une belt posée
        d'une demi-tuile trop loin ne se charge jamais, et rien à la pose ne le signale.
        """
        brut = self.api.rcon.query_lua(
            f"local s = game.surfaces[1] local w = 0 "
            f"for _, e in pairs(s.find_entities_filtered{{force='player', "
            f"area={{{{{x - 0.4},{y - 0.4}}},{{{x + 0.4},{y + 0.4}}}}}}}) do "
            f"  if e.type ~= 'character' then "
            f"    local bb = e.bounding_box "
            f"    local d = math.max(bb.right_bottom.x - bb.left_top.x, "
            f"bb.right_bottom.y - bb.left_top.y) / 2 "
            f"    if d > w then w = d end end end rcon.print(w)")
        try:
            mesure = float(str(brut).strip())
        except ValueError:
            return defaut
        return mesure if mesure > 0 else defaut

    def _source_de(self, item: str, loin: float = 120.0
                   ) -> Optional[tuple[str, float, float]]:
        """Une machine qui PRODUIT `item`, la plus proche. Ou None.

        On cherche ce qui fabrique, pas ce qui contient : un coffre plein se vide, une
        chaîne qui tourne ne s'arrête pas. Un four est retenu sur ce qu'il a en sortie,
        une assembleuse sur la recette qu'on lui a donnée.
        """
        lua = (
            f"local s = game.surfaces[1] local best, bd = nil, 1e18 "
            f"for _, e in pairs(s.find_entities_filtered{{force='player', "
            f"area={{{{{self.zone[0] - loin},{self.zone[1] - loin}}},"
            f"{{{self.zone[0] + loin},{self.zone[1] + loin}}}}}}}) do "
            f"  local produit = false "
            f"  if e.type == 'furnace' or e.type == 'assembling-machine' then "
            f"    local ok, rec = pcall(function() return e.get_recipe() end) "
            f"    if ok and rec then "
            f"      for _, p in pairs(rec.products) do "
            f"        if p.name == '{item}' then produit = true end end end "
            f"    local out = e.get_output_inventory() "
            f"    if out and out.get_item_count('{item}') > 0 then produit = true end "
            f"  end "
            # UNE SOURCE DOIT ÊTRE ALIMENTÉE. Sans ce filtre, le premier four venu fait
            # illusion : le plan de fusion en pose un pour une fournée, le remplit à la
            # main, et il garde des plaques en sortie. `_source_de` le désignait alors
            # comme source de cuivre, l'agent renonçait à bâtir la vraie chaîne, et
            # l'alimentation tarissait dès la fournée consommée — « rien ne produit de
            # copper-plate » sur une carte qui portait pourtant un four plein.
            # Est pérenne ce qu'une FOREUSE ou un BRAS remplit tout seul.
            f"  if produit then "
            f"    local bb = e.bounding_box local servie = false "
            f"    for _, m in pairs(s.find_entities_filtered{{position=e.position, "
            f"radius=4, type={{'inserter', 'mining-drill'}}}}) do "
            f"      local p = m.drop_position "
            f"      if p and p.x >= bb.left_top.x - 0.1 and p.x <= bb.right_bottom.x + 0.1 "
            f"         and p.y >= bb.left_top.y - 0.1 and p.y <= bb.right_bottom.y + 0.1 "
            f"      then servie = true end end "
            f"    produit = servie end "
            f"  if produit then "
            f"    local d = (e.position.x - {self.zone[0]})^2 + (e.position.y - {self.zone[1]})^2 "
            f"    if d < bd then bd = d best = e end end end "
            f"if best then rcon.print(best.name .. ',' .. best.position.x .. ',' "
            f".. best.position.y) else rcon.print('') end")
        try:
            brut = self.api.rcon.query_lua(lua)
        except Exception:
            return None
        # UNE RÉPONSE ILLISIBLE N'EST PAS UNE ABSENCE. Mesuré : le premier appel rendait
        # None, et rejouer LA MÊME requête dans la foulée rendait
        # « electric-furnace,-15.5,-75.5 » — la réponse arrivait décalée. On concluait
        # donc « rien ne produit iron-plate à portée » sur un four qui fondait, et l'agent
        # renonçait à alimenter sa chaîne. Deux tentatives valent mieux qu'un faux vide.
        for tentative in range(2):
            morceaux = str(brut).strip().split(",")
            if len(morceaux) == 3:
                try:
                    return (morceaux[0], float(morceaux[1]), float(morceaux[2]))
                except ValueError:
                    return None
            if tentative == 0:
                try:
                    brut = self.api.rcon.query_lua(lua)
                except Exception:
                    return None
        return None

    def amener(self, item: str, vers: tuple[float, float], vers_nom: str,
               belt: str = "transport-belt",
               reserve: Optional[set] = None) -> tuple[bool, str]:
        """Fait venir `item` jusqu'à la machine en `vers` : bras, belt, bras.

        C'est ce qui sépare une usine d'une corvée. L'assembleuse de science tournait sur
        une provision déposée à la main : elle s'arrêtait dès qu'elle l'avait consommée,
        et il fallait revenir la remplir. Tant que c'est l'agent qui porte, rien ne tourne
        en son absence.

        Le montage réutilise ce qui est éprouvé : `place_inserter_vers` pose les bras en
        LISANT leur pickup et leur drop réels (un bras mal orienté se pose sans erreur et
        ne transporte rien), et `place_belt_line` oriente chaque segment vers l'aval (une
        seule tuile mal tournée arrête le flux sans que rien ne le signale).
        """
        from services import site_finder

        source = self._source_de(item)
        if source is None:
            return False, f"rien ne produit {item} à portée — il faut d'abord en bâtir la chaîne"
        nom_src, sx, sy = source
        distance = math.hypot(sx - vers[0], sy - vers[1])

        # Assez près pour un simple bras : inutile de dérouler une belt sur trois tuiles.
        if distance <= 4.5:
            pont = site_finder.place_inserter_vers(
                self.api, vers, (sx, sy), vers_nom, nom="inserter",
                source_types=(nom_src,))
            if pont is None:
                return False, (f"{nom_src} est à {distance:.0f} tuiles de {vers_nom}, "
                               f"mais aucun bras ne peut les relier")
            alimente = self.brancher("inserter", pont[0], pont[1])
            return alimente, (f"{item} : {nom_src}@({sx:.0f},{sy:.0f}) verse directement "
                              f"dans {vers_nom} ({distance:.0f} tuiles)"
                              + ("" if alimente else " — MAIS le bras est sans courant"))

        # Sinon : bras -> belt -> bras. L'écart se CALCULE sur la machine, il ne se
        # devine pas : un four de pierre fait 2x2 (bord à 1), une assembleuse 3x3 (bord à
        # 1.5). Un écart fixe convient donc à l'une et pas à l'autre — mesuré deux fois,
        # d'abord « SANS bras de déchargement » avec deux tuiles (la belt touchait
        # l'assembleuse), puis « SANS bras de chargement » avec trois (la belt était à
        # deux tuiles et demie du four, hors de portée d'un bras qui n'en atteint qu'une).
        # Il faut le bord, plus la tuile du bras, plus le demi-pas de la belt.
        ecart_src = self._demi_largeur(sx, sy) + 1.5
        ecart_cible = self._demi_largeur(vers[0], vers[1]) + 1.5
        # CHAQUE FLUX ARRIVE PAR SON PROPRE CÔTÉ. Calculé de la même façon pour tous, le
        # point d'arrivée était le même pour tous : la belt des engrenages débouchait sur
        # celle du cuivre une tuile avant la machine, la saturait — « (-21.5,-60.5)
        # [iron-gear-wheel x4, x4] » juste derrière huit tuiles de cuivre à l'arrêt — et
        # l'assembleuse restait en `item_ingredient_shortage` alors que les DEUX
        # ingrédients étaient à moins de deux tuiles d'elle. Deux flux qui desservent la
        # même machine ne doivent jamais se rejoindre : le côté choisi est réservé, et le
        # flux suivant en prendra un autre.
        cotes_cible = [(ecart_cible if sx > vers[0] else -ecart_cible, 0.0),
                       (0.0, ecart_cible if sy > vers[1] else -ecart_cible),
                       (-(ecart_cible if sx > vers[0] else -ecart_cible), 0.0),
                       (0.0, -(ecart_cible if sy > vers[1] else -ecart_cible))]
        vers_cible = (float(math.floor(vers[0] + cotes_cible[0][0])) + 0.5,
                      float(math.floor(vers[1] + cotes_cible[0][1])) + 0.5)
        for dx_t, dy_t in cotes_cible:
            essai_t = (float(math.floor(vers[0] + dx_t)) + 0.5,
                       float(math.floor(vers[1] + dy_t)) + 0.5)
            if reserve is not None and essai_t in reserve:
                continue
            occupe = any(e.get("type") == "transport-belt"
                         for e in site_finder._entites_a(self.api, essai_t[0], essai_t[1], 0.4))
            if occupe:
                continue
            vers_cible = essai_t
            break
        if reserve is not None:
            reserve.add(vers_cible)

        # LES QUATRE CÔTÉS DE LA SOURCE, et pas seulement celui qui regarde la cible.
        # Mesuré : un `wooden-chest` occupait exactement la première tuile — le ramassage
        # que la chaîne pose elle-même en sortie de son four. La belt n'y démarrait donc
        # jamais, et sans première tuile aucun bras ne peut charger : « 79 tuiles posées,
        # tracé INTERROMPU, SANS bras de chargement ». On ne dispute pas la place à son
        # propre ouvrage, on se pose à côté.
        cotes = [(ecart_src if vers[0] > sx else -ecart_src, 0.0),
                 (0.0, ecart_src if vers[1] > sy else -ecart_src),
                 (-(ecart_src if vers[0] > sx else -ecart_src), 0.0),
                 (0.0, -(ecart_src if vers[1] > sy else -ecart_src))]
        vers_src = (float(math.floor(sx + cotes[0][0])) + 0.5,
                    float(math.floor(sy + cotes[0][1])) + 0.5)
        # UN CÔTÉ NE VAUT QUE S'IL PERMET D'ACCROCHER LE BRAS. Poser la tuile de belt et
        # découvrir ensuite qu'aucun emplacement de bras ne convient laissait une ligne
        # de soixante-dix tuiles sans chargement — inutile de bout en bout. On essaie
        # donc chaque côté JUSQU'AU BRAS, et l'on ne retient que celui qui va au bout.
        # ON NE SORT PAS PAR OÙ L'ON ENTRE. Mesuré, et c'est la boucle qui a résisté le
        # plus longtemps : deux bras se faisaient face sur la même tuile — l'un sortait
        # les engrenages vers la belt, l'autre les y reprenait pour les remettre dans
        # l'assembleuse. Vingt-trois pièces produites, aucune arrivée, et les deux bras
        # en `waiting_for_space_in_destination`. Chaque entité, prise séparément, faisait
        # exactement son travail. Un côté déjà servi par un flux ENTRANT est donc écarté.
        entrants = set()
        for ins in site_finder._entites_a(self.api, sx, sy, 5.0):
            if ins.get("type") != "inserter" or ins.get("dropX") is None:
                continue
            if (abs(float(ins["dropX"]) - sx) <= 2.0
                    and abs(float(ins["dropY"]) - sy) <= 2.0):
                entrants.add((float(math.floor(float(ins.get("x", 0.0)))) + 0.5,
                              float(math.floor(float(ins.get("y", 0.0)))) + 0.5))

        charge = None
        for dx_c, dy_c in cotes:
            essai = (float(math.floor(sx + dx_c)) + 0.5, float(math.floor(sy + dy_c)) + 0.5)
            # La tuile du bras qui desservirait ce côté : entre la machine et la belt.
            milieu = (float(math.floor((sx + essai[0]) / 2)) + 0.5,
                      float(math.floor((sy + essai[1]) / 2)) + 0.5)
            if milieu in entrants:
                continue
            deja = any(e.get("type") == "transport-belt"
                       for e in site_finder._entites_a(self.api, essai[0], essai[1], 0.4))
            if not deja and not site_finder.can_place(self.api, belt, essai[0], essai[1]):
                continue
            if not deja:
                self.api.run_action(self.api.place_entity_at, belt, essai[0], essai[1],
                                    "north", None, timeout=20.0)
            pont = site_finder.place_inserter_vers(
                self.api, essai, (sx, sy), belt, nom="inserter",
                source_types=(nom_src,), cible_pos=essai)
            if pont is not None:
                vers_src, charge = essai, pont
                break
            # Ce côté ne mène à rien : on retire la tuile posée pour l'essayer. Laissée
            # là, elle devient une belt ISOLÉE — un morceau de convoyeur qui ne relie
            # rien, que le banc compte à juste titre comme un défaut de pose.
            if not deja:
                self.api.run_action(self.api.remove_entity_at, essai[0], essai[1],
                                    belt, timeout=20.0)

        # NE PAS DÉVERSER SUR LA VOIE D'UN AUTRE FLUX. Mesuré, et c'est la panne la plus
        # retorse rencontrée : le bras de sortie des engrenages déposait sur une tuile
        # déjà occupée par la belt qui AMÈNE le fer à cette même assembleuse. Les pièces
        # descendaient, tournaient à l'ouest et rentraient d'où elles venaient — vingt-
        # trois engrenages produits, zéro arrivé, et l'assembleuse en `full_output`
        # pendant que la science manquait d'ingrédient. Une boucle fermée ne se voit
        # nulle part : chaque entité prise séparément a l'air juste.
        #
        # Le tracé CONTOURNE les belts existantes : `place_belt_line` retourne celles
        # qu'elle croise pour les aligner sur elle — geste juste quand on prolonge sa
        # propre ligne, désastreux quand on traverse celle d'un voisin.
        # SEULEMENT LES BELTS. Une version a essayé d'éviter TOUT ce qui est bâti —
        # poteaux et coffres compris, puisque `degager_tuile` ne les ôte pas. Mesuré :
        # c'est pire (2/6 au lieu de 3/6). Trop de tuiles interdites ne laissent plus
        # aucun L praticable, et l'on ne pose alors rien du tout là où l'on posait une
        # ligne presque complète. Ce qu'il faut absolument éviter est la voie d'un AUTRE
        # FLUX — la retourner casse ce qui marchait ; un poteau, lui, ne fait qu'un trou.
        # LA RÉSERVATION EST TENUE EN MÉMOIRE, pas relue du jeu. `inspect_at` plafonne son
        # rayon à 64 tuiles : sur une ligne de soixante-quinze, les belts du bout étaient
        # tout simplement INVISIBLES, et chaque flux redécouvrait un terrain qu'il croyait
        # libre. C'est la raison de fond pour laquelle les tracés se disputaient l'espace
        # quoi qu'on fasse — on réservait à l'aveugle au-delà de l'horizon d'observation.
        # Un couloir se réserve AVANT de poser, et la réservation se transmet d'un flux au
        # suivant.
        occupees = set(reserve) if reserve is not None else set()
        occupees |= {(float(e.get("x", 0.0)), float(e.get("y", 0.0)))
                     for e in site_finder._entites_a(
                         self.api, (sx + vers[0]) / 2, (sy + vers[1]) / 2,
                         max(16.0, min(distance, 60.0)))
                     if e.get("type") == "transport-belt"}

        # ET L'EMPRISE DES DEUX MACHINES QU'ON RELIE. Mesuré : le tracé des engrenages
        # passait par le CENTRE de l'assembleuse dont il partait — « tracé INTERROMPU en
        # (-22,-64) », c'est-à-dire sur la machine elle-même. Aucune belt ne s'y pose,
        # la ligne garde un trou dès sa première tuile, et plus aucun bras ne peut la
        # charger. Interdire ces deux emprises-là suffit : les interdire TOUTES a été
        # essayé et donne moins bien (plus aucun L praticable).
        # TOUTES LES MACHINES, pas seulement les deux qu'on relie. Mesuré : la belt du
        # cuivre courait de (-32.5,-65.5) jusqu'à (-24.5,-65.5) puis s'arrêtait net —
        # elle butait sur l'assembleuse à engrenages posée en (-23,-65), qu'aucun tracé
        # ne peut traverser. Les objets s'accumulaient en amont, les bras de déchargement
        # attendaient en aval, et rien dans la pose ne le signalait.
        #
        # On interdit les MACHINES et rien d'autre : interdire tout ce qui est bâti,
        # poteaux et coffres compris, a été essayé et donne moins bien (plus aucun L
        # praticable). Une machine est infranchissable ; un poteau se contourne d'une
        # tuile.
        obstacles = [(float(e.get("x", 0.0)), float(e.get("y", 0.0)))
                     for e in site_finder._entites_a(
                         self.api, (sx + vers[0]) / 2, (sy + vers[1]) / 2,
                         max(16.0, min(distance, 60.0)))
                     # Les POTEAUX comptent aussi : ils n'occupent qu'une tuile, mais
                     # `degager_tuile` n'ôte que ce que la nature a mis là — jamais nos
                     # ouvrages. Mesuré : « tracé INTERROMPU en (-32,-62), (-22,-62),
                     # (-18,-62) », trois poteaux sur l'axe du cuivre, et une ligne en
                     # morceaux qui ne transportait rien. Une tuile qu'on ne peut ni
                     # occuper ni libérer doit être contournée à la planification.
                     # TOUT CE QUI OCCUPE DURABLEMENT UNE TUILE. Y compris les poteaux,
                     # coffres et bras : `degager_tuile` n'ôte que ce que la nature a mis
                     # là, jamais nos ouvrages. Les déclarer franchissables coupait la
                     # ligne ; les déclarer infranchissables ne laissait aucun L. C'est
                     # `tracer_en_l` qui tranche désormais — il CONTOURNE quand aucun L
                     # ne passe, au lieu de renoncer.
                     if e.get("type") in ("furnace", "assembling-machine", "lab",
                                          "mining-drill", "boiler", "generator",
                                          "electric-pole", "container", "inserter")]
        for cx, cy in [(sx, sy), (vers[0], vers[1])] + obstacles:
            demi = self._demi_largeur(cx, cy)
            # L'EMPRISE, PAS LA PÉRIPHÉRIE. Interdire une tuile de plus tout autour
            # revenait à poser deux carrés de 5x5 autour de machines distantes de six
            # tuiles : plus aucun passage entre elles, et « aucun tracé libre » sur une
            # liaison de six tuiles. On bloque la machine elle-même, rien de plus.
            pas = max(0, int(demi))
            for ex in range(-pas, pas + 1):
                for ey in range(-pas, pas + 1):
                    occupees.add((float(math.floor(cx + ex)) + 0.5,
                                  float(math.floor(cy + ey)) + 0.5))
        # ON S'ÉCARTE AVANT DE DÉROULER. `can_place` refuse une pose SOUS l'avatar, et
        # l'agent se tient justement là : il vient de miner le minerai qui alimente cette
        # source. La première tuile de la belt manquait donc, et sans elle aucun bras ne
        # pouvait charger — « 79 tuiles posées, tracé INTERROMPU, SANS bras de
        # chargement », pour deux trous sur de la terre ordinaire.
        from services import deplacement
        depuis = deplacement.position(self.api)
        if math.hypot(depuis[0] - vers_src[0], depuis[1] - vers_src[1]) < 4.0:
            self.api.run_action(self.api.walk_to, vers_src[0], vers_src[1] + 6.0,
                                timeout=60.0)

        # On CONNAÎT le tracé avant de poser : cela permet de dire ensuite quelles tuiles
        # manquent, et non le seul mot « INTERROMPU ». Une ligne coupée ne transporte
        # rien, et savoir OÙ elle est coupée est la moitié du diagnostic.
        # LES BRAS D'ABORD, LA BELT ENSUITE. Un bras est CONTRAINT — il doit toucher la
        # machine — tandis qu'une belt peut faire le tour. En déroulant la ligne en
        # premier, on saturait les abords des machines et il ne restait plus une tuile
        # pour les bras : « 6 tuiles de belt posées, SANS bras de chargement, SANS bras de
        # déchargement » sur une liaison de six tuiles. On pose donc les deux tuiles
        # d'extrémité, on accroche les bras, et la belt vient relier ce qui est acquis.
        # Le bras de chargement est deja accroche (choix du cote, plus haut). Reste
        # celui du dechargement, cote machine cible.
        if not any(e.get("type") == "transport-belt"
                   for e in site_finder._entites_a(self.api, vers_cible[0], vers_cible[1], 0.4)):
            self.api.run_action(self.api.place_entity_at, belt, vers_cible[0],
                                vers_cible[1], "north", None, timeout=20.0)
        decharge = site_finder.place_inserter_vers(
            self.api, vers, vers_cible, vers_nom, nom="inserter", source_types=(belt,))

        # SES PROPRES EXTRÉMITÉS NE SONT PAS DES OBSTACLES. On vient de poser une tuile
        # de belt à chaque bout pour y accrocher les bras ; les compter comme « déjà
        # prises » interdisait au tracé de partir de son propre départ — « aucun tracé
        # libre » partout, et cinq tuiles isolées en tout et pour tout.
        # NI SES EXTRÉMITÉS, NI LEURS ABORDS IMMÉDIATS. Une fois machines, poteaux,
        # coffres et bras déclarés infranchissables, la tuile d'arrivée se retrouve
        # ENCERCLÉE — le chemin ne peut plus y accéder, et l'on rend « aucun tracé libre »
        # alors qu'un contournement existe à une tuile près. On rouvre donc le voisinage
        # des deux bouts : c'est par là que la ligne doit entrer et sortir.
        for bout in (vers_src, vers_cible):
            occupees.discard(bout)
            for dx_v, dy_v in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
                occupees.discard((bout[0] + dx_v, bout[1] + dy_v))

        # ON TRACE JUSQU'À LA TUILE D'ARRIVÉE INCLUSE. `tracer_en_l` s'arrête avant sa
        # destination ; en visant `vers_cible`, la ligne s'interrompait donc une tuile
        # trop tôt et la tuile d'arrivée — posée d'avance pour le bras — restait ISOLÉE
        # dès que le tracé avait fait un détour. Trois convoyeurs orphelins au milieu de
        # l'usine, qui ne reliaient rien. On vise donc une tuile au-delà, vers la machine.
        # ON VISE LA TUILE D'ARRIVÉE ELLE-MÊME. Viser une tuile AU-DELÀ paraissait plus
        # sûr — la ligne engloberait l'arrivée — mais un trajet en L peut atteindre ce
        # point par un autre côté et laisser la tuile d'arrivée ORPHELINE : mesuré,
        # quatre belts isolées dont une chargée de sept objets, à une tuile d'une ligne
        # qui ne la touchait pas. `tracer_en_l` s'arrête juste avant sa cible : viser
        # l'arrivée garantit donc que la dernière tuile posée lui est ADJACENTE.
        attendues, _ = site_finder.tracer_en_l(vers_src, vers_cible, occupees)
        # Le couloir est RÉSERVÉ dès qu'il est tracé, avant même la pose : le flux suivant
        # ne le proposera plus, qu'il soit ou non déjà matérialisé sur le terrain.
        if reserve is not None:
            reserve.update(attendues)
        tuiles, complete = site_finder.place_belt_line(
            self.api, vers_src, vers_cible, belt=belt, eviter=occupees)
        trous = [t for t in attendues if t not in tuiles]

        # LE RACCORD FINAL. `tracer_en_l` s'arrête juste avant sa cible, mais un trajet
        # en L peut avoir contourné : la dernière tuile posée n'est alors pas voisine de
        # l'arrivée, qui reste ORPHELINE — mesuré, trois belts isolées dont celle du
        # cuivre, et l'assembleuse ne recevait que des engrenages. On comble le peu qui
        # sépare les deux plutôt que de laisser un flux coupé.
        if tuiles:
            bx, by = tuiles[-1]
            garde = 0
            while (abs(bx - vers_cible[0]) + abs(by - vers_cible[1]) > 1.0
                   and garde < 12):
                garde += 1
                if abs(bx - vers_cible[0]) >= abs(by - vers_cible[1]):
                    pas_x = 1.0 if vers_cible[0] > bx else -1.0
                    bx, d_r = bx + pas_x, ("east" if pas_x > 0 else "west")
                else:
                    pas_y = 1.0 if vers_cible[1] > by else -1.0
                    by, d_r = by + pas_y, ("south" if pas_y > 0 else "north")
                if abs(bx - vers_cible[0]) + abs(by - vers_cible[1]) < 0.1:
                    break
                if not any(e.get("type") == "transport-belt"
                           for e in site_finder._entites_a(self.api, bx, by, 0.4)):
                    self.api.run_action(self.api.place_entity_at, belt, bx, by, d_r,
                                        None, timeout=20.0)
                    tuiles.append((bx, by))

        # ON NE LAISSE PAS DE TUILE ORPHELINE. La tuile d'arrivée est posée d'avance pour
        # accrocher le bras ; si le tracé a finalement abouti ailleurs, elle reste seule
        # au milieu de rien — un convoyeur qui ne relie personne, que le banc compte à
        # juste titre comme un défaut de pose. On la retire quand aucune belt ne la
        # dessert et qu'elle-même ne verse nulle part.
        if vers_cible not in tuiles:
            voisine = any(
                e.get("type") == "transport-belt"
                for dxv, dyv in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0))
                for e in site_finder._entites_a(self.api, vers_cible[0] + dxv,
                                                vers_cible[1] + dyv, 0.4))
            if not voisine:
                self.api.run_action(self.api.remove_entity_at, vers_cible[0],
                                    vers_cible[1], belt, timeout=20.0)

        # LA TUILE D'ARRIVÉE DOIT REGARDER LA MACHINE. `tracer_en_l` s'arrête AVANT elle
        # — c'est nous qui l'avons posée d'avance, pour y accrocher le bras, avec
        # l'orientation par défaut. Mesuré : la belt du fer descendait chargée sur onze
        # tuiles, et la douzième pointait au NORD, à contre-courant. Les deux se faisaient
        # face, le flux s'arrêtait là, le bras de chargement passait en
        # `waiting_for_space_in_destination` pendant que celui du bout attendait des
        # objets qui n'arrivaient jamais. Une seule tuile à l'envers, et rien ne le dit.
        dx_f, dy_f = vers[0] - vers_cible[0], vers[1] - vers_cible[1]
        vers_aval = (("east" if dx_f > 0 else "west") if abs(dx_f) >= abs(dy_f)
                     else ("south" if dy_f > 0 else "north"))
        self.api.run_action(self.api.rotate_entity_at, vers_cible[0], vers_cible[1],
                            vers_aval, belt, timeout=20.0)
        if not tuiles:
            # ON NE LAISSE PAS DE MORCEAUX DERRIÈRE SOI. Les deux tuiles d'extrémité ont
            # été posées d'avance pour accrocher les bras ; si le tracé n'aboutit pas,
            # elles restent seules au milieu de rien — des convoyeurs qui ne relient
            # personne, que le banc compte à juste titre comme un défaut de pose.
            for bout in (vers_src, vers_cible):
                self.api.run_action(self.api.remove_entity_at, bout[0], bout[1], belt,
                                    timeout=20.0)
            return False, (f"aucun tracé libre entre {nom_src}@({sx:.0f},{sy:.0f}) et "
                           f"{vers_nom} ({distance:.0f} tuiles) sans emprunter une belt "
                           f"existante ({len(occupees)} tuile(s) déjà prises)")

        # Les bras ont déjà été posés plus haut, avant la belt : ils sont contraints,
        # elle ne l'est pas.
        # Les bras sont ÉLECTRIQUES, et celui du bout de la belt est aussi loin que la
        # source : soixante-dix tuiles de la centrale, donc hors de toute couverture.
        # Mesuré — belt déroulée, bras posés aux deux bouts, et pas un objet transporté.
        sans_courant = [f"({p[0]:.0f},{p[1]:.0f})"
                        for p in (charge, decharge) if p is not None
                        and not self.brancher("inserter", p[0], p[1])]
        # UNE LIGNE COUPÉE NE TRANSPORTE RIEN, et ne doit donc pas compter comme montée.
        # `amener` rendait `True` sur un tracé interrompu dès lors que les deux bras
        # étaient posés et alimentés : l'appelant croyait la liaison faite, ne réessayait
        # pas, et l'assembleuse attendait un ingrédient qui n'arriverait jamais. On exige
        # la CONTINUITÉ — c'est elle qu'on est venu chercher.
        ok = (charge is not None and decharge is not None and not sans_courant
              and complete)
        return ok, (f"{item} : {nom_src}@({sx:.0f},{sy:.0f}) -> {len(tuiles)} tuile(s) de "
                    f"belt -> {vers_nom} ({distance:.0f} tuiles"
                    + ("" if complete else
                       f", tracé INTERROMPU en {', '.join(f'({t[0]:.0f},{t[1]:.0f})' for t in trous[:4])}"
                       f"{' …' if len(trous) > 4 else ''}") + ")"
                    + ("" if charge is not None else " — SANS bras de chargement")
                    + ("" if decharge is not None else " — SANS bras de déchargement")
                    + (f" — bras SANS COURANT en {', '.join(sans_courant)}"
                       if sans_courant else ""))

    def _alimenter_la_chaine(self, ancre: tuple[float, float],
                             rayon: float = 8.0) -> int:
        """Vérifie que CHAQUE machine fraîchement posée reçoit du courant, et la relie.

        Une chaîne à moitié branchée ne produit rien et ne se voit pas : mesuré, le foreur
        tournait pendant que son inserter et son four étaient `no_power`, si bien que le
        minerai tombait par terre — `item-on-ground` au drop, foreur en
        `waiting_for_space_in_destination`, et trois chaînes ajoutées pour zéro plaque de
        plus. `place_supply_poles` avait pourtant posé ses poteaux : desservir n'est pas
        alimenter, et personne ne le vérifiait au moment où c'était le moins cher à
        corriger.

        On le fait donc À LA POSE, quand on sait exactement ce qu'on vient de bâtir, au
        lieu d'attendre que le diagnostic le redécouvre machine par machine.
        """
        r = self.api.inspect_at(ancre[0], ancre[1], rayon)
        lignes = r.get("entities", []) if isinstance(r, dict) else []
        relies = 0
        for e in lignes:
            if e.get("type") not in ("mining-drill", "furnace", "inserter",
                                     "assembling-machine"):
                continue
            x, y = float(e.get("x", 0.0)), float(e.get("y", 0.0))
            etat = self.api.get_power_state(x, y, 3.0) or {}
            if etat.get("connected") is True:
                continue
            faux = Symptome(name=str(e.get("name")), x=x, y=y, cause="debranchee",
                            gravite=2, detail="posée à l'instant, sans courant")
            ok, _ = self.relier(faux)
            relies += 1 if ok else 0
        return relies

    def _evacuer_la_chaine(self, ancre: tuple[float, float],
                           rayon: float = 8.0) -> str:
        """Pose le ramassage en sortie DÈS la construction, sans attendre le bouchon.

        La bascule d'E21 attend deux vidages manuels par machine avant de bâtir un
        ramassage. C'est le bon réflexe pour une machine qui se bouche par accident, et
        c'est trop lent pour une usine qui grandit : mesuré sur une partie de 63 tours,
        `evacuer` est revenu QUATORZE fois et `machine_pleine` a été conclue vingt fois,
        pendant que le débit oscillait entre 1.1 et 2.4 au lieu de tenir. Chaque nouvelle
        chaîne doit d'abord se boucher deux fois pour mériter son coffre.

        Un four qu'on vient de poser finira par se remplir — ce n'est pas un incident,
        c'est une certitude. On lui donne donc sa sortie tout de suite, au moment où l'on
        sait exactement où il est et où la place est libre.
        """
        r = self.api.inspect_at(ancre[0], ancre[1], rayon)
        lignes = r.get("entities", []) if isinstance(r, dict) else []
        poses, deja = 0, 0
        for e in lignes:
            if e.get("type") not in ("furnace", "assembling-machine"):
                continue
            x, y = float(e.get("x", 0.0)), float(e.get("y", 0.0))
            # Un coffre déjà à portée signifie que la sortie est servie : on ne double pas.
            voisins = self.api.inspect_at(x, y, 4.0)
            proches = voisins.get("entities", []) if isinstance(voisins, dict) else []
            if any(v.get("name") == "wooden-chest" for v in proches):
                deja += 1
                continue
            cible = Symptome(name=str(e.get("name")), x=x, y=y, cause="sortie_bloquee",
                             gravite=1, detail="posée à l'instant, sortie non ramassée")
            ok, _ = self.batir_evacuation(cible)
            poses += 1 if ok else 0
        return (f"{poses} ramassage(s) posé(s)"
                + (f", {deja} déjà servi(s)" if deja else ""))

    def relier(self, cible, pole: str = "small-electric-pole") -> tuple[bool, str]:
        """Rattache une machine au réseau — en tirant une LIGNE si le réseau est loin.

        Poser un poteau contre la machine suffit tant que le réseau est à portée de fil,
        et ne sert à rien dès qu'il ne l'est plus. C'est ce qui arrivait à chaque
        extension : mesuré en partie longue, l'agent bâtit ses nouvelles chaînes à quinze
        ou vingt tuiles de la centrale, l'Enquêteur concluait sept fois « le pôle le plus
        proche est sur le réseau 128 mais TROP LOIN », et la boucle reposait un poteau
        isolé de plus. Dix machines sur vingt restaient à l'arrêt.

        Le poteau local est donc posé d'abord — il alimente la machine —, puis on vérifie
        s'il a du courant, et sinon on tire la ligne DEPUIS le réseau JUSQU'À lui. C'est
        `place_pole_line`, déjà éprouvée sur 104 tuiles en E5, qui chaîne sur les
        positions réellement posées et refuse tout maillon au-delà de la portée de fil.
        """
        from services import site_finder

        def _pose_confirmee(x: float, y: float) -> bool:
            """Le poteau est-il RÉELLEMENT là ? La réponse de l'action ne suffit pas.

            Mesuré au banc : `place_entity_at` rend `ok=False` et le poteau apparaît
            quand même une fraction de seconde plus tard — la pose est asynchrone, et la
            réponse précède parfois l'effet. `relier` concluait donc à l'échec sur une
            pose réussie, puis le tour suivant retrouvait la place occupée et cherchait
            ailleurs. C'est la règle déjà payée par l'executor : ne jamais croire un
            verdict d'action, le confirmer sur le terrain.
            """
            return any(e.get("type") == "electric-pole"
                       for e in site_finder._entites_a(self.api, x, y, 0.6))

        # Les places CANDIDATES, triées par écart réel à la machine. L'arrondi sur une
        # tuile transforme un décalage de 2.5 en 3.0 selon le signe, et un petit poteau
        # n'alimente que 2.5 tuiles autour de lui : la première place essayée était
        # justement une de celles qui ne couvrent pas. On pose donc au plus près d'abord,
        # et l'on s'arrête sur le COURANT obtenu, non sur la pose réussie.
        # PLUS DE PLACES CANDIDATES. Autour d'une machine entourée de belts, les quatre
        # tuiles voisines sont souvent toutes prises : `relier` renonçait alors, et un
        # bras tout juste posé restait sans courant au milieu d'un réseau qui passait à
        # trois tuiles. Un poteau porte plus loin que la tuile d'à côté.
        candidates = sorted(
            {(float(int(cible.x + dx)) + 0.5, float(int(cible.y + dy)) + 0.5)
             for dx, dy in ((2.0, 0.0), (-2.0, 0.0), (0.0, 2.0), (0.0, -2.0),
                            (2.5, 0.0), (-2.5, 0.0), (0.0, 2.5), (0.0, -2.5),
                            (2.0, 2.0), (-2.0, 2.0), (2.0, -2.0), (-2.0, -2.0),
                            (3.0, 0.0), (-3.0, 0.0), (0.0, 3.0), (0.0, -3.0))},
            key=lambda p: math.hypot(p[0] - cible.x, p[1] - cible.y))

        def _poser_a_cote() -> Optional[tuple[float, float]]:
            """Pose jusqu'à ce que la machine soit ALIMENTÉE, et non jusqu'à la 1re pose.

            Un poteau posé n'est pas un poteau utile : `relier` s'arrêtait dès qu'une
            pose réussissait, y compris à trois tuiles de la machine — c'est-à-dire hors
            de sa portée d'alimentation. Il rendait alors « posé pour inserter » sur une
            machine toujours morte, et le tour suivant rediagnostiquait la même panne.
            """
            dernier = None
            for x, y in candidates:
                if not _pose_confirmee(x, y):
                    if not site_finder.can_place(self.api, pole, x, y):
                        continue
                    self.api.run_action(self.api.place_entity_at, pole, x, y, "north",
                                        None, timeout=20.0)
                    if not _pose_confirmee(x, y):
                        continue
                dernier = (x, y)
                etat = self.api.get_power_state(cible.x, cible.y, 3.0) or {}
                if etat.get("connected") is True:
                    return dernier
            return dernier

        local = _poser_a_cote()
        if local is None:
            # Le terrain n'a pas fini de se libérer. Mesuré au banc : `relier` arrive au
            # tour SUIVANT la pose d'une chaîne, les quatre places autour de la machine
            # sont encore refusées, et le même appel réussit quelques secondes plus tard
            # sans que rien n'ait bougé entre-temps. Une place occupée à l'instant où
            # l'on regarde n'est pas une place occupée.
            self.api.run_action(self.api.wait, 60, timeout=30.0)
            local = _poser_a_cote()
        if local is None:
            # Peut-être un poteau est-il déjà là — auquel cas ce n'est pas la place qui
            # manque, mais le courant, et la ligne reste à tirer.
            proches = [e for e in site_finder._entites_a(self.api, cible.x, cible.y, 3.0)
                       if e.get("type") == "electric-pole"]
            if not proches:
                return False, f"aucune position de poteau libre autour de {cible.name}"
            local = (float(proches[0].get("x", cible.x)), float(proches[0].get("y", cible.y)))

        # Le réseau électrique se recalcule APRÈS la pose, et la pose elle-même est
        # asynchrone. Lire dans la foulée fait constater « toujours pas de courant » sur
        # un poteau qui n'existe pas encore : mesuré au banc, `relier` échouait au tour
        # même où l'usine venait d'être bâtie, puis réussissait sur la MÊME scène quelques
        # secondes plus tard — sans que rien n'ait été posé entre les deux. On laisse donc
        # le jeu prendre acte avant de juger.
        self.api.run_action(self.api.wait, 30, timeout=30.0)

        # `connected` et NON `networkId` : la même confusion a produit les îlots qu'on
        # répare ici. Tout poteau posé reçoit un identifiant de réseau, fût-il isolé —
        # mesuré, cinq machines portaient fièrement `networkId=129` avec zéro générateur
        # dessus, `connected=false` et `bufferEnergy=0`. Un identifiant ne dit rien de ce
        # qui circule ; seul `connected` le dit.
        etat = self.api.get_power_state(cible.x, cible.y, 3.0) or {}
        if etat.get("connected") is True:
            return True, f"poteau posé en {local} pour {cible.name} — réseau atteint"

        # Le poteau local est isolé : il faut aller chercher le courant là où il est.
        source = site_finder.poteau_alimente_le_plus_proche(self.api, cible.x, cible.y)
        if source is None:
            return False, (f"poteau posé en {local} mais aucun réseau alimenté à portée "
                           f"de {cible.name} — il manque une centrale, pas une ligne")
        # Tirer une ligne d'un point vers LUI-MÊME ne pose rien et ne relie personne.
        # Cela arrivait quand aucune place n'était libre autour de la machine : on
        # retombait sur un poteau existant, qui se trouvait être la source, et le message
        # annonçait fièrement « ligne de 0 poteau(x) ». Le manque n'est pas une ligne mais
        # une PLACE à portée de la machine.
        if math.hypot(source[0] - local[0], source[1] - local[1]) < 1.0:
            return False, (f"{cible.name} n'est couverte par aucun poteau, et il n'y a "
                           f"aucune place libre autour d'elle pour en poser un")

        poses, complete = site_finder.place_pole_line(
            self.api, (source[0], source[1]), local, pole=pole)
        apres = self.api.get_power_state(cible.x, cible.y, 3.0) or {}
        relie = apres.get("connected") is True
        return relie, (f"ligne de {len(poses)} poteau(x) depuis le réseau {source[2]} "
                       f"en ({source[0]},{source[1]}) jusqu'à {cible.name}"
                       + ("" if complete else " — INTERROMPUE par un obstacle")
                       + ("" if relie else " — toujours sans réseau"))

    def batir_evacuation(self, cible, coffre: str = "wooden-chest") -> tuple[bool, str]:
        """Pose un ramassage PERMANENT en sortie : un coffre, et un bras qui l'y verse.

        Le pendant exact d'`approvisionner`, à l'autre bout de la machine. Mesuré sur une
        partie de 952 tours partie d'une carte propre : le même four est retombé en
        `full_output` aux tours 152, 380, 608 et 831, chaque fois « réparé » par un vidage
        manuel — et entre-temps le foreur en amont attendait `waiting_for_space`. Toute la
        chaîne s'arrêtait donc pour un ramassage de quelques secondes qui n'existait pas.

        Le montage réutilise ce qui est déjà éprouvé (E19b) : `place_inserter_vers` pose
        le bras, LIT son pickup et son drop réels, et tourne jusqu'à ce que les deux
        tombent où il faut. Un inserter mal orienté se pose sans erreur et ne transporte
        rien — le croire sur parole est précisément ce qui a coûté un run.

        On pose, on teste, on RETIRE et on essaie la position suivante : abandonner au
        premier échec ne posait rien du tout, autre leçon d'E19.
        """
        import math
        from services import site_finder

        inv = (self.api.get_state() or {}).get("inventory", {}) or {}
        # Électrique par défaut : la machine bouchée est sur le réseau, donc le courant
        # est là. Le burner ne sert que de repli — il faut le nourrir, et un bras à
        # nourrir est exactement le genre de dépendance qu'on cherche à supprimer ici.
        bras = "inserter" if inv.get("inserter", 0) else "burner-inserter"
        if not inv.get(bras, 0):
            return False, f"aucun bras disponible pour évacuer {cible.name}"
        if not inv.get(coffre, 0):
            return False, f"aucun {coffre} pour recevoir la sortie de {cible.name}"

        essais: list[str] = []
        # Les distances croissent parce que l'emprise varie : un four 2×2 et une
        # assembleuse 3×3 n'offrent pas leurs bords au même endroit, et rien dans la
        # ligne d'entité ne donne la bounding box (même raison qu'au relais d'entrée).
        for d in (2.5, 3.5, 4.5):
            for ux, uy in ((1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0)):
                kx = math.floor(cible.x + ux * d) + 0.5
                ky = math.floor(cible.y + uy * d) + 0.5
                if not site_finder.can_place(self.api, coffre, kx, ky):
                    continue
                r = self.api.run_action(self.api.place_entity_at, coffre, kx, ky,
                                        "north", None, timeout=20.0)
                if not (isinstance(r, dict) and r.get("ok")):
                    essais.append(f"({kx},{ky}) : coffre refusé")
                    continue
                # Le bras PUISE dans la machine et DÉPOSE dans le coffre : la machine
                # est la SOURCE, d'où `source_types`.
                pose = site_finder.place_inserter_vers(
                    self.api, (kx, ky), (cible.x, cible.y), coffre, nom=bras,
                    source_types=(cible.name,))
                if pose is not None:
                    if bras == "burner-inserter":
                        self.api.run_action(self.api.move_items_at, "coal", bras,
                                            pose[0], pose[1], self.AMORCE_BRAS, True,
                                            timeout=20.0)
                    else:
                        # Un bras ÉLECTRIQUE posé sans courant est une panne qu'on se
                        # fabrique à soi-même. Mesuré au banc : le ramassage posait son
                        # inserter hors de toute couverture, le tour suivant le
                        # diagnostiquait « débranchée », et l'agent passait son temps à
                        # réparer ce qu'il venait de construire au lieu de grandir.
                        # Même règle que pour les chaînes : ce qu'on pose, on l'alimente.
                        etat = self.api.get_power_state(pose[0], pose[1], 1.0) or {}
                        if etat.get("connected") is not True:
                            self.relier(Symptome(name=bras, x=pose[0], y=pose[1],
                                                 cause="debranchee", gravite=1,
                                                 detail="bras d'évacuation posé à l'instant"))
                    cle = (cible.name, round(cible.x), round(cible.y))
                    # La mémoire est REMISE À ZÉRO : la machine a désormais un ramassage,
                    # et un prochain bouchon serait un incident neuf, pas la suite de
                    # l'ancien. Sans cela, la bascule se redéclencherait au premier hoquet
                    # et l'on empilerait les coffres.
                    self._evacuations.pop(cle, None)
                    return True, (f"évacuation de {cible.name}@({cible.x},{cible.y}) : "
                                  f"{bras}@({pose[0]},{pose[1]}) verse dans un {coffre}"
                                  f"@({kx},{ky})")
                essais.append(f"({kx},{ky}) : aucun bras ne relie")
                self.api.run_action(self.api.remove_entity_at, kx, ky, coffre,
                                    timeout=20.0)
        return False, (f"aucune place pour évacuer {cible.name} — "
                       f"{' ; '.join(essais[:3]) if essais else 'aucun emplacement libre'}")

    def approvisionner(self, cible, item: str = "coal") -> tuple[bool, str]:
        """Bâtit une chaîne mine -> belt -> inserter vers une machine à combustible.

        C'est ce qui sépare une usine qui démarre d'une usine qui tient : mesuré, un
        boiler brûle 0.45 charbon/s, soit moins de deux minutes d'autonomie pour un
        plein. Tant que personne ne le réapprovisionne, tout ce qui a été bâti s'arrête.

        La chaîne n'est construite que si le gisement est assez proche ; au-delà, on
        rend la main en l'expliquant plutôt que de dérouler une belt interminable.
        """
        import math
        from services import site_finder
        from services.layout_planner import ResourcePatch
        from services.micro_planner import MicroRequest, plan_micro
        from services.executor import execute_micro

        # Quel gisement : une décision, pas un calcul. Cf. `choisir_gisement`.
        choix = self.sans_ecoulement(self.choisir_gisement, item, (cible.x, cible.y),
                                     self.PORTEE_APPRO)
        if choix is None:
            return False, (f"aucun gisement de {item} à moins de {self.PORTEE_APPRO:.0f} "
                           f"tuiles : c'est un problème de train, pas de belt")
        # On s'ancre sur une TUILE réelle du gisement retenu, jamais sur son centre : le
        # centre d'une boîte peut tomber sur un trou (piège déjà payé avec `scan_patch`).
        sp = self.builder._scan_patch_local(item)
        ancre = self.builder._anchor_on_ore(sp, 4) if sp.get("sample") else None
        if ancre is None:
            ancre = (choix.x, choix.y)
        distance = math.hypot(ancre[0] - cible.x, ancre[1] - cible.y)

        # 0. De quoi amorcer. Si la réserve a fondu, on va la reprendre à la main sur le
        #    gisement — c'est ce que fait un joueur, et c'est la seule sortie quand le
        #    stock est à zéro : sans amorce, ni le foreur ni les bras ne démarrent, et la
        #    chaîne est posée morte. Le minage manuel reste plus rapide qu'un foreur
        #    (mesuré au bootstrap), donc une trentaine d'unités coûtent quelques secondes.
        besoin = self.AMORCE + 2 * self.AMORCE_BRAS
        stock = perception.inventory(self.api).get(item, 0)
        if stock < besoin:
            self.api.run_action(self.api.walk_to, ancre[0], ancre[1], timeout=90.0)
            self.api.run_action(self.api.mine_entity, item, besoin - stock, timeout=90.0)
            stock = perception.inventory(self.api).get(item, 0)
            # Le minage a CREUSÉ le gisement à l'endroit même où l'on comptait poser :
            # une tuile épuisée disparaît, et `can_place_entity` en mode `manual` refuse
            # un foreur sans minerai dessous — là où le mode par défaut l'accepte. Le
            # symptôme est un `can_place=False` sur du sable nu, à côté d'un gisement de
            # 500 tuiles intactes. On reprend donc la mesure du gisement APRÈS l'avoir
            # entamé, au lieu de se fier à celle d'avant.
            sp = self.builder._scan_patch_local(item)
            ancre = self.builder._anchor_on_ore(sp, 4) if sp.get("sample") else ancre
            if ancre is None:
                return False, f"gisement de {item} épuisé là où il fallait le foreur"

        # 1. Le matériel. Pour le CHARBON, tout est burner — et ce n'est pas un repli
        #    faute de mieux, c'est la seule sortie d'une circularité : la première
        #    version posait un foreur électrique pour aller chercher le charbon dont la
        #    centrale avait besoin pour produire ce courant. Mesuré en jeu : foreur et
        #    inserter posés, belt complète, statut `no_power` des deux côtés, zéro
        #    charbon transporté. Un burner ne dépend que de ce qu'il extrait.
        # Burner ou électrique : la question n'est pas QUEL minerai, mais Y A-T-IL DU
        # COURANT. Le critère « item == coal » était la leçon d'E13 appliquée à moitié :
        # elle évitait bien la circularité du charbon, et posait un foreur électrique sur
        # du fer dans une usine sans réseau. Mesuré : `electric-mining-drill` en
        # `no_power`, chaîne complète de 39 belts, four à jeun.
        burner = (item == "coal")
        if not burner:
            reseau = (self.api.get_power_state(cible.x, cible.y, 25.0) or {}).get("networkId")
            if reseau is None and perception.inventory(self.api).get(
                    "burner-mining-drill", 0) > 0:
                burner = True
        foreur = "burner-mining-drill" if burner else "electric-mining-drill"
        bras = "burner-inserter" if burner else "inserter"
        taille = 2 if burner else 3

        self.api.generate_terrain(ancre[0], ancre[1], 25.0)
        mp = plan_micro(MicroRequest(
            patch=ResourcePatch(resource=item, tiles=[], bbox=(0, 0, 0, 0)),
            facing=4, anchor=ancre, drill_tier=foreur,
            inserter_tier=bras, furnace_tier="electric-furnace",
            drill_size=taille, furnace_size=3))
        mp.entities = [e for e in mp.entities if e.role == "drill"]
        mp.totals = {foreur: 1}
        # `approach=True` : en production le mod refuse toute pose au-delà de
        # `build_distance` (« walk closer first », mesuré à 10 tuiles). Le foreur est sur
        # le gisement, donc à des dizaines de tuiles de la machine qu'on alimente — il
        # FAUT y aller. En test_mode l'approche est un téléport, elle ne coûte rien.
        rap = execute_micro(self.api, mp, generate=False, approach=True, timeout=90.0)
        if not rap.ok or not rap.placed:
            return False, f"foreur non posé sur {item} : {rap.missing or rap.blocked[:1]}"
        drill = rap.placed[0]

        # 2. L'amorçage, ou le courant. Un burner doit recevoir de quoi extraire son
        #    premier charbon ; un électrique doit être relié.
        if burner:
            self.api.run_action(self.api.move_items_at, "coal", foreur, drill.x, drill.y,
                                self.AMORCE, True, timeout=20.0)
        else:
            ancrage = self.dernier_poteau or (drill.x, drill.y)
            site_finder.place_pole_line(self.api, ancrage, (drill.x, drill.y))
            site_finder.place_supply_poles(self.api, [drill], (drill.x, drill.y))

        # 3. La belt part du drop RÉEL du foreur, lu et non supposé : le décalage de
        #    sortie dépend du prototype et de l'orientation, et une belt posée une tuile
        #    à côté laisse le minerai tomber au sol sans que rien ne le signale.
        pose_drill = next((e for e in site_finder._entites_a(self.api, drill.x, drill.y, 1.5)
                           if e.get("type") == "mining-drill"), None)
        if pose_drill and pose_drill.get("dropX") is not None:
            depart = (pose_drill["dropX"], pose_drill["dropY"])
        else:
            depart = (drill.x, drill.y + 2.0)
        # La belt vise le RELAIS D'ENTRÉE, décidé avant elle : la tuile derrière le
        # bras qui chargera la machine. C'est l'inversion qui manquait — on traçait la
        # ligne d'abord, puis on cherchait un emplacement de bras après chaque recul,
        # donc sur une belt encore en cours d'allongement. Le résultat dépendait de
        # l'encombrement du terrain : chaîne complète et machine à jeun, au hasard.
        #
        # En `test_mode` le character headless bâtit à n'importe quelle distance : faire
        # marcher l'avatar le long de la belt ne servirait qu'à ralentir les tests.
        etat_mod = self.api.get_state()
        portee = 0.0 if etat_mod.get("test_mode") else 8.0

        relais = self._relais_de_retour(drill, depart, bras, foreur) if burner else None
        entree = self._relais_d_alimentation(cible.x, cible.y, bras,
                                             exclure=(depart,) + ((relais,) if relais else ()))
        if entree is None:
            return False, (f"aucun emplacement de {bras} ne peut charger {cible.name} : "
                           f"ses quatre côtés sont pris")
        pos_bras, arrivee = entree

        belts: list[tuple[float, float]] = []
        essais: list[str] = []
        origine = depart
        # Le tronçon de retour du foreur passe d'abord par son relais, s'il en a un.
        if relais is not None:
            seg0, _ = site_finder.place_belt_line(self.api, depart, relais, portee=portee)
            belts.extend(seg0)
            origine = relais
        seg, complete = site_finder.place_belt_line(self.api, origine, arrivee,
                                                    portee=portee)
        belts.extend(seg)
        essais.append(f"belt {origine} -> relais d'entrée {arrivee} : {len(seg)} segment(s)")
        if not belts:
            return False, f"aucune belt posée entre le foreur et {cible.name}"

        # La tuile d'arrivée elle-même : `place_belt_line` s'arrête AVANT elle, or c'est
        # précisément celle sur laquelle le bras doit puiser.
        if site_finder.can_place(self.api, "transport-belt", arrivee[0], arrivee[1]):
            from services.flux import _direction_vers
            d_fin = _direction_vers(belts[-1], arrivee) if belts else "east"
            r = self.api.run_action(self.api.place_entity_at, "transport-belt",
                                    arrivee[0], arrivee[1], d_fin, None, timeout=20.0)
            if isinstance(r, dict) and r.get("ok"):
                belts.append(arrivee)

        # 4. Le bras qui décharge, à la place RÉSERVÉE pour lui. `place_inserter_vers`
        #    vérifie par LECTURE que le dépôt tombe dans la machine.
        pose_ins = site_finder.place_inserter_vers(
            self.api, (cible.x, cible.y), arrivee, cible.name, nom=bras, essais=40)
        if pose_ins is None:
            return False, (f"belt posée jusqu'au relais {arrivee} mais aucun {bras} "
                           f"n'atteint {cible.name} — {' | '.join(essais)}")

        # 5. Le bras de RETOUR. C'est lui qui rend la chaîne perpétuelle : sans lui, le
        #    foreur épuise son amorce et s'arrête, et l'on aurait seulement déplacé le
        #    remplissage manuel du boiler vers le foreur.
        boucle = None
        if burner:
            jb: list = []
            boucle = site_finder.place_inserter_vers(
                self.api, (drill.x, drill.y), relais or belts[0], foreur, nom=bras,
                journal=jb)
            if boucle is None:
                essais.append("retour au foreur : " + " ; ".join(jb[:6]))
            for pos in (pose_ins, boucle):
                if pos is not None:
                    self.api.run_action(self.api.move_items_at, "coal", bras, pos[0],
                                        pos[1], self.AMORCE_BRAS, True, timeout=20.0)

        # La chaîne existe : on oublie l'historique de remplissage manuel de cette
        # machine, sinon la boucle voudrait l'automatiser à nouveau au prochain incident.
        cle = (cible.name, round(cible.x), round(cible.y))
        self._ravitaillements.pop(cle, None)
        # On retient d'où part le flux — la tuile de sortie du foreur. C'est ce qui
        # permettra de le SUIVRE et de constater qu'il n'arrive pas, plutôt que de
        # s'en tenir au fait que la chaîne a été posée.
        self._chaines[cle] = depart
        # Le compte rendu dit ce que la chaîne vaut RÉELLEMENT. Une belt trouée ne
        # transporte rien et un foreur sans réalimentation s'arrête quand son amorce est
        # brûlée : annoncer « chaîne bâtie » dans ces cas-là ferait croire le problème
        # réglé, et la boucle repartirait sur autre chose en laissant la machine à sec.
        reserves = []
        if not complete:
            reserves.append("belt INTERROMPUE — le flux s'arrêtera au trou")
        if burner and boucle is None:
            reserves.append("sans réalimentation du foreur — il s'arrêtera son amorce brûlée")
        return True, (f"chaîne {item} bâtie : {foreur}@({drill.x},{drill.y}) -> "
                      f"{len(belts)} belt(s) -> {bras}@{pose_ins[:2]} -> {cible.name}, "
                      f"{distance:.0f} tuiles"
                      + (f" | RÉSERVES : {' ; '.join(reserves)}" if reserves else ""))

    # ----- DÉFENDRE -----

    def defendre(self, nombre: int = 3, munitions: int = 20) -> tuple[bool, str]:
        """Pose des tourelles face au front et les munit.

        Une tourelle sans munitions est un décor : on l'approvisionne dans la foulée,
        et une pose qu'on n'a pas pu munir est signalée comme telle plutôt que comptée
        comme une défense.

        On ne ceinture pas l'usine — le ThreatModel donne la direction d'où viendront
        les vagues, et un périmètre complet coûterait plusieurs fois plus pour la même
        protection.
        """
        from services.threat_model import positions_defense
        from services.site_finder import can_place

        menace = self.derniere_menace
        if menace is None or not menace.front:
            return False, "aucun front identifié : rien à défendre de ce côté"

        inv = perception.inventory(self.api)
        if inv.get(self.tourelle, 0) < 1:
            return False, f"aucune {self.tourelle} en inventaire"

        posees, munies = 0, 0
        for (x, y) in positions_defense(self.zone, menace, nombre=nombre):
            if inv.get(self.tourelle, 0) - posees < 1:
                break
            place = None
            for dx, dy in ((0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                           (2.0, 0.0), (-2.0, 0.0)):
                px, py = float(int(x + dx)) + 0.5, float(int(y + dy)) + 0.5
                if not can_place(self.api, self.tourelle, px, py):
                    continue
                r = self.api.run_action(self.api.place_entity_at, self.tourelle,
                                        px, py, "north", None, timeout=20.0)
                if isinstance(r, dict) and r.get("ok"):
                    place = (px, py)
                    break
            if place is None:
                continue
            posees += 1
            rm = self.api.run_action(self.api.move_items_at, self.munition,
                                     self.tourelle, place[0], place[1], munitions,
                                     True, timeout=30.0)
            if isinstance(rm, dict) and rm.get("ok"):
                munies += 1
        if posees == 0:
            return False, f"aucune position de tourelle libre au {menace.front_nom}"
        return True, (f"{posees} tourelle(s) posée(s) au {menace.front_nom}, "
                      f"{munies} munie(s) — {menace.raison}")

    # ----- BÂTIR (délégué aux planners + executor) -----

    def batir(self, d: Decision) -> tuple[bool, str]:
        """Bâtit ce que la décision demande, en composant les services existants.

        `preparer` est le seul point où le Coordinator touche au terrain : générer les
        chunks est indispensable en headless (sans quoi `can_place` refuse tout sur du
        non-généré) et ne détruit rien. Le dégagement de la végétation, lui, reste à
        l'appelant : raser sans discernement détruit ce qu'on vient de bâtir.
        """
        from services.executor import execute_micro
        from services.micro_planner import MicroRequest, plan_micro
        from services.layout_planner import ResourcePatch
        from services.power_planner import PowerRequest, plan_power
        from services import site_finder

        def preparer(x, y):
            self.api.generate_terrain(x, y, 25.0)

        if d.action == "batir_energie":
            site = site_finder.find_power_site(self.api, vers=self.zone,
                                               preparer=preparer)
            if site is None:
                return False, "aucune rive exploitable pour une centrale"
            plan = plan_power(PowerRequest(demand_kw=self.demande_kw),
                              origin=site.origine, pump_pos=site.pompe,
                              pump_direction=site.direction)
            if not plan.ok:
                return False, f"centrale non planifiable : {plan.feasibility}"
            # 50 unités : ~2 minutes de marche à pleine charge (0.45 charbon/s par
            # boiler). En donner 100 vidait l'inventaire dès la première centrale et le
            # tour suivant échouait sur `missing`. Le ravitaillement est justement une
            # réparation que la boucle sait faire — inutile de tout donner d'un coup.
            rap = execute_micro(self.api, plan, fuel=self.combustible, fuel_count=50,
                                generate=False, approach=False, timeout=40.0)
            if not rap.ok:
                return False, (f"centrale non bâtie : missing={rap.missing} "
                               f"blocked={rap.blocked[:1]}")
            # Relier la centrale à la zone de travail, sans quoi elle n'alimente rien.
            depart = next(((p.x, p.y) for p in rap.placed if p.role == "pole"),
                          site.origine)
            ligne, complete = site_finder.place_pole_line(self.api, depart, self.zone)
            self.derniere_centrale = rap
            self.dernier_poteau = ligne[-1] if ligne else depart
            return True, (f"centrale bâtie ({len(rap.placed)} entités) à "
                          f"{site.distance_a(self.zone):.0f} tuiles, ligne de "
                          f"{len(ligne)} poteaux ({'complète' if complete else 'INTERROMPUE'})")

        # batir_production : micro-chaîne électrique ancrée sur du minerai réel.
        #
        # PLUSIEURS ancres sont essayées, et c'est ce qui rend une extension possible : la
        # meilleure tuile est occupée dès que la première chaîne y est posée. Mesuré, une
        # extension reproposait invariablement le même emplacement et échouait sur
        # `can_place=False` — trois fois, puis l'abandon définitif gelait la croissance.
        #
        # Boucler est sûr : le pré-vol de l'executor refuse le plan ENTIER avant de poser
        # quoi que ce soit, une ancre rejetée ne laisse donc aucun débris derrière elle.
        sp = self.builder._scan_patch_local(self.ressource)
        candidats = (self.builder.ancres_sur_minerai(
            sp, 4, ecart=self.builder.ECART_ANCRES) if sp.get("sample") else [])
        # Les tuiles NON BÂTIES du gisement viennent ensuite : le `sample` de scan_patch
        # ne donne que douze tuiles groupées, identiques à tout rayon, alors que le bbox
        # décrit parfois un gisement de trente tuiles de côté. Sans cela, l'agent
        # concluait « aucune place libre » au bord d'un gisement presque intact.
        if sp.get("bbox"):
            for a in site_finder.ancres_libres_sur_minerai(
                    self.api, sp["bbox"], self.zone,
                    ecart=self.builder.ECART_ANCRES,
                    # Ce que la boucle SURVEILLE est ce qu'elle peut réparer : une chaîne
                    # hors du rayon diagnostiqué n'existe pour elle qu'au moment où elle
                    # la pose.
                    distance_max=self.rayon):
                if a not in candidats:
                    candidats.append(a)
        if not candidats:
            return False, f"aucun gisement de {self.ressource} exploitable"
        essais: list[str] = []
        # Le palier est CHOISI, pas imposé : sans recherche ni dotation, l'électrique
        # n'existe tout simplement pas (recettes `enabled=false`).
        t = self.tiers_micro()
        for ancre in candidats[:6]:
            preparer(ancre[0], ancre[1])
            mp = plan_micro(MicroRequest(
                patch=ResourcePatch(resource=self.ressource, tiles=[], bbox=(0, 0, 0, 0)),
                facing=4, anchor=ancre, drill_tier=t["drill"],
                inserter_tier=t["inserter"], furnace_tier=t["furnace"],
                drill_size=t["drill_size"], furnace_size=t["furnace_size"]))
            # `approach=True` : en production le mod refuse toute pose au-delà de
            # `build_distance` (« walk closer first », mesuré à 10 tuiles). Le foreur est
            # sur le gisement, donc à des dizaines de tuiles de la machine qu'on alimente
            # — il FAUT y aller. En test_mode l'approche est un téléport, elle ne coûte
            # rien.
            rap = execute_micro(self.api, mp, generate=False, approach=True, timeout=90.0)
            if rap.ok:
                ancrage = self.dernier_poteau or ancre
                poteaux = site_finder.place_supply_poles(self.api, rap.placed, ancrage)
                relies = self._alimenter_la_chaine(ancre)
                vidange = self._evacuer_la_chaine(ancre)
                return True, (f"chaîne {t['nom']} posée ({len(rap.placed)} machines) sur "
                              f"{self.ressource} en {ancre}, {len(poteaux)} poteau(x) "
                              f"de desserte, {relies} raccordement(s), {vidange}"
                              + (f" — {len(essais)} ancre(s) occupée(s) écartée(s)"
                                 if essais else ""))
            # Un manque d'INVENTAIRE ne se résoudra pas en changeant d'endroit : insister
            # ferait payer six plans pour le même refus.
            if rap.missing:
                return False, f"chaîne non posée, il manque : {rap.missing}"
            essais.append(f"{ancre} : {rap.blocked[:1]}")
        return False, (f"aucune place libre sur {self.ressource} — "
                       f"{len(essais)} ancre(s) essayée(s) : {' ; '.join(essais[:2])}")

    # ----- BOUCLE -----

    def run(self, max_ticks: int = 10) -> list[Decision]:
        """Enchaîne les tours jusqu'à ce qu'il n'y ait plus rien à faire.

        Trois façons de s'arrêter, et la deuxième est la plus importante :

        1. **plus rien à faire** — la décision est `rien`, l'usine tourne ;
        2. **on n'avance plus** — la même action échoue deux fois de suite. Sans cette
           garde, un agent bute indéfiniment sur un problème qu'il ne sait pas résoudre
           (un site introuvable, un item manquant) en le rediagnostiquant à chaque tour.
           Mieux vaut rendre la main en le disant que tourner en rond ;
        3. **plafond de tours** — filet de sécurité, jamais la sortie normale.

        Retourne les décisions prises, dans l'ordre : c'est le compte rendu de ce que
        l'agent a fait pendant qu'on ne le regardait pas.
        """
        decisions: list[Decision] = []
        echecs_consecutifs = 0
        derniere_action = ""
        for _ in range(max_ticks):
            d, agi, _ = self.tick()
            decisions.append(d)
            if d.action == "rien":
                break
            if not agi and d.action == derniere_action:
                echecs_consecutifs += 1
                if echecs_consecutifs >= 1:      # deux tentatives identiques en vain
                    self.journal.append(
                        f"arrêt : « {d.action} » a échoué deux fois de suite, "
                        f"la boucle ne progresse plus")
                    break
            else:
                echecs_consecutifs = 0
            derniere_action = d.action
        return decisions

    def tick(self) -> tuple[Decision, bool, EtatUsine]:
        """Un tour complet. Retourne (décision, a_agi, état APRÈS action.

        L'état rendu est relu après l'action : une décision n'est jugée que sur son
        effet, jamais sur le fait qu'elle ait été prise.
        """
        etat = self.observer()
        d = figer_pendant(getattr(self, "api", None),
                          getattr(self, "pause_reflexion", False),
                          decide, etat, self.arbitre)
        agi, detail = self.agir(d)
        self.journal.append(f"{d} -> {'agi' if agi else 'sans effet'} ({detail})")

        # On RETIENT l'échec, par action et par cible. Un compteur remis à zéro au succès :
        # ce qui compte est l'acharnement, pas le total sur la partie.
        # Les actions SANS cible comptent aussi. Mesuré en partie longue : 1241 tours
        # d'affilée à retenter `batir_energie`, qui n'a pas de cible et échappait donc
        # entièrement au garde-fou. Bâtir est justement ce qui coûte le plus cher à
        # retenter pour rien.
        cle = ((d.action, d.cible.name, round(d.cible.x), round(d.cible.y))
               if d.cible is not None else (d.action, "", 0, 0))
        if agi:
            self._echecs.pop(cle, None)
        else:
            self._echecs[cle] = self._echecs.get(cle, 0) + 1
            if self._echecs[cle] == SEUIL_ABANDON:
                ou = (f" sur {d.cible.name}@({d.cible.x},{d.cible.y})"
                      if d.cible is not None else "")
                self.journal.append(f"ABANDON de « {d.action} »{ou} après "
                                    f"{SEUIL_ABANDON} échecs : {detail}")
        if not agi:
            return d, agi, etat

        # L'action a été menée à son terme. Reste à savoir si elle a SERVI — ce qui
        # n'est pas la même question, et c'est celle qu'on ne posait pas.
        attente = self._attente(d)
        if attente is not None:
            tenue, observe = attente.evaluer(self.api)
            if not tenue:
                contexte = {}
                if d.cible is not None:
                    depart = self._chaines.get(
                        (d.cible.name, round(d.cible.x), round(d.cible.y)))
                    if depart is not None:
                        contexte["depart_du_flux"] = list(depart)
                ecart = Ecart(d.action, attente.description, observe, d.cible, contexte)
                self.ecarts.append(ecart)
                self.journal.append(str(ecart))
                if not self.remettre_en_etat(ecart):
                    self.enqueter(ecart)
        return d, agi, self.observer()

    # ----- RÉPARER CE QU'ON A DIAGNOSTIQUÉ -----

    def remettre_en_etat(self, ecart: Ecart) -> bool:
        """Tente une réparation DÉTERMINISTE de la chaîne concernée. Vrai si elle a servi.

        Appelée avant l'enquête, et c'est délibéré : quand le suivi de flux sait à la
        fois nommer la rupture et la situer, la réparation est un algorithme et n'a
        aucune raison de coûter un aller-retour à un modèle. L'enquêteur reste pour ce
        que le déterministe ne couvre pas — c'est là qu'il apporte.

        `reparer_flux` juge chaque tentative sur la mesure qui suit, jamais sur le fait
        qu'elle ait été appliquée : cette méthode ne rend `True` que si le flux est
        réellement rétabli.
        """
        depart = (ecart.contexte or {}).get("depart_du_flux")
        c = ecart.cible
        if not depart or c is None:
            return False
        from services.flux import reparer_flux
        try:
            ok, detail = reparer_flux(self.api, (float(depart[0]), float(depart[1])),
                                      c.name, (c.x, c.y))
        except Exception as e:
            self.journal.append(f"remise en état en erreur : {type(e).__name__}")
            return False
        self.journal.append(f"RÉPARATION {c.name} -> "
                            f"{'rétablie' if ok else 'échouée'} ({detail})")
        return ok

    # ----- ENQUÊTE -----

    def enqueter(self, ecart: Ecart) -> Optional[Any]:
        """Cherche la cause d'un écart, et la consigne — sans encore la réparer.

        L'enquêteur observe et conclut ; il ne déclenche aucune réparation tant que le
        banc d'essai n'a pas montré ce qu'il vaut. C'est la même prudence que le mode
        ombre de l'arbitre, et pour la même raison : introduire un modèle dans une boucle
        autonome sans mesure préalable serait un pari.

        Ce que ce journal apporte dès maintenant, même sans réparation : un agent qui dit
        « le bras dépose en (261.7,-187.5) où il n'y a rien » vaut infiniment mieux qu'un
        agent qui écrit « chaîne bâtie ». La liste de ce qu'il ne sait pas réparer devient
        explicite, au lieu d'être reconstituée à la main après coup.
        """
        if self.enqueteur is None:
            return None
        try:
            constat = self.enqueteur(self.api, ecart)
        except Exception as e:                    # une enquête ne casse jamais la boucle
            self.journal.append(f"enquête en erreur : {type(e).__name__}")
            return None
        self.constats.append(constat)
        self.journal.append(f"ENQUÊTE {ecart.action} -> {constat}")
        return constat