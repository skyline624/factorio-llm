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
            "batir_production": 1, "rien": 0}

# Au-delà de ce nombre de ravitaillements MANUELS sur la même machine, on cesse de
# remplir et on automatise. Remplir un réservoir est une réparation ; le remplir
# indéfiniment est l'aveu qu'il manque une chaîne d'approvisionnement.
#
# Mesuré : un boiler brûle 0.45 charbon/s, soit ~110 s d'autonomie pour 50 unités. Un
# agent qui ne fait que ravitailler y passe sa vie et ne construit plus rien.
SEUIL_AUTOMATISATION = 2

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
class Decision:
    """Ce que le Coordinator a décidé, et pourquoi — le « pourquoi » est la moitié utile."""
    action: str
    raison: str
    priorite: int = 0
    cible: Optional[Symptome] = None
    faisable: bool = True     # False = le matériel manque (cf. BESOINS)

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
        options.append(Decision(action=action,
                                raison=f"{c.name} : {c.cause} — {explication}",
                                priorite=PRIORITE["reparer"], cible=c))

    # Défense : deux niveaux d'urgence, cf. PRIORITE. Le ThreatModel a déjà tranché
    # le « faut-il » (la pollution déclenche les vagues, pas la proximité des nids) ;
    # ici on n'ajoute que l'option correspondante.
    if etat.menace is not None and etat.menace.agir:
        urgent = etat.menace.niveau >= EN_COURS
        options.append(Decision(
            action="defendre",
            raison=str(etat.menace),
            priorite=PRIORITE["defendre_urgence" if urgent else "defendre"]))

    if not etat.a_de_l_energie:
        options.append(Decision(action="batir_energie",
                                raison=("aucun réseau alimenté : rien d'électrique ne "
                                        "fonctionnera avant"),
                                priorite=PRIORITE["batir_energie"]))
    elif etat.machines == 0:
        options.append(Decision(action="batir_production",
                                raison="du courant, mais aucune machine pour en profiter",
                                priorite=PRIORITE["batir_production"]))

    if not options:
        options.append(Decision(action="rien",
                                raison=f"{etat.machines} machine(s) en état de marche",
                                priorite=PRIORITE["rien"]))
    # Faisabilité : une action dont le matériel manque est DÉCLASSÉE, pas supprimée.
    # La supprimer masquerait le besoin ; la garder en tête ferait échouer la boucle à
    # chaque tour. On la relègue et on dit pourquoi — c'est ce qui permet, plus tard, de
    # décider d'aller fabriquer ce qui manque.
    inv = etat.inventaire or {}
    for o in options:
        manquants = [f"{n} ({inv.get(n, 0)}/{c})"
                     for n, c in BESOINS.get(o.action, ()) if inv.get(n, 0) < c]
        if manquants:
            o.priorite = 0
            o.faisable = False
            o.raison += f" — INFAISABLE, il manque : {', '.join(manquants)}"

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
    if arbitre is None or len(options) <= 1:
        return options[0]
    try:
        choix = arbitre(etat, options)
    except Exception:
        return options[0]
    if not isinstance(choix, int) or isinstance(choix, bool):
        return options[0]
    if not 0 <= choix < len(options):
        return options[0]
    return options[choix]


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
                 ombre: bool = False, enqueteur=None):
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
        # D'où part la chaîne qui alimente chaque machine : la tuile de sortie du foreur.
        # Sans ce point de départ, on ne peut pas SUIVRE le flux, et donc pas vérifier
        # qu'une chaîne bâtie transporte réellement quelque chose.
        self._chaines: dict = {}
        self.journal: list[str] = []
        # Les actions menées à leur terme sans produire leur effet. C'est le signal sur
        # lequel une enquête pourra être déclenchée ; sans lui, l'agent est aveugle à
        # ses propres échecs.
        self.ecarts: list[Ecart] = []
        # Ce que les enquêtes ont établi, y compris les « inconnu ». Ce journal EST la
        # liste de travail : chaque cause qu'on ne sait pas encore réparer y figure au
        # lieu d'être redécouverte à la main au chantier suivant.
        self.constats: list = []
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
        diag = diagnose_zone(self.api, self.zone[0], self.zone[1], self.rayon)
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
                    break
        etat.inventaire = perception.inventory(self.api)
        etat.ravitaillements = dict(self._ravitaillements)
        # La menace est évaluée à CHAQUE tour : elle change sans qu'on y touche, alors
        # que l'usine ne change que quand on agit.
        etat.menace = evaluer(self.api.scan_threats(self.zone[0], self.zone[1], 300.0),
                              usine=self.zone)
        self.derniere_menace = etat.menace
        return etat

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

        if d.action == "relier" and c is not None:
            return Attente(
                f"{c.name} appartient à un réseau",
                lambda api: (api.get_power_state(c.x, c.y, 3.0) or {}).get("networkId"),
                lambda n: n is not None,
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
            # Poteau au plus près de la machine débranchée, sur les quatre côtés.
            for dx, dy in ((2.5, 0.0), (-2.5, 0.0), (0.0, 2.5), (0.0, -2.5)):
                x, y = float(int(c.x + dx)) + 0.5, float(int(c.y + dy)) + 0.5
                chk = self.api.can_place_check("small-electric-pole", x, y, "north")
                if not (isinstance(chk, dict) and chk.get("can_place")):
                    continue
                r = self.api.run_action(self.api.place_entity_at, "small-electric-pole",
                                        x, y, "north", None, timeout=20.0)
                if isinstance(r, dict) and r.get("ok"):
                    return True, f"poteau posé en ({x},{y}) pour {c.name}"
            return False, f"aucune position de poteau libre autour de {c.name}"

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
            # Dépannage : on enlève ce qui bouche. Si la sortie se remplit à nouveau,
            # c'est qu'il manque une évacuation — même bascule que le ravitaillement,
            # mais elle n'est pas encore bâtie : on le dit plutôt que de boucler.
            r = self.api.run_action(self.api.empty_output_at, c.x, c.y, c.name,
                                    timeout=20.0)
            ok = isinstance(r, dict) and r.get("ok") is True
            return ok, (f"sortie de {c.name}@({c.x},{c.y}) vidée"
                        if ok else f"sortie de {c.name} non vidable : {r}")

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

        sp = self.builder._scan_patch_local(item)
        ancre = self.builder._anchor_on_ore(sp, 4) if sp.get("sample") else None
        if ancre is None:
            return False, f"aucun gisement de {item} exploitable"
        distance = math.hypot(ancre[0] - cible.x, ancre[1] - cible.y)
        if distance > self.PORTEE_APPRO:
            return False, (f"gisement de {item} à {distance:.0f} tuiles : trop loin pour "
                           f"une belt (limite {self.PORTEE_APPRO:.0f}), il faudrait un train")

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
        burner = (item == "coal")
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
        # La belt ne vise pas la machine : elle s'arrête EN RETRAIT, du côté d'où elle
        # arrive. Mesuré : visée sur la machine, elle bute dessus et le dernier segment
        # se colle à son bord — il ne reste alors aucune tuile libre entre la belt et la
        # machine, et un inserter doit se tenir ENTRE les deux. 36 segments posés, aucun
        # bras possible au bout. Le retrait dépend de la taille de la machine, qu'on ne
        # connaît pas ici : on part large et on rallonge d'une tuile tant que le bras ne
        # trouve pas sa place — trois essais suffisent du 1×1 au 3×3.
        # L'approche est ramenée à un AXE, jamais à une diagonale : un inserter dessert
        # les quatre côtés d'une machine, pas ses coins. Une arrivée oblique laisse la
        # belt décalée en x ET en y — 50 segments posés, et le seul emplacement libre se
        # trouvait en diagonale du boiler, d'où aucun dépôt n'est possible.
        # En `test_mode` le character headless bâtit à n'importe quelle distance : faire
        # marcher l'avatar le long de la belt ne servirait qu'à ralentir les tests.
        etat_mod = self.api.get_state()
        portee = 0.0 if etat_mod.get("test_mode") else 8.0

        # Un RELAIS pour le bras de retour, avant même de tracer la belt.
        #
        # Le foreur déverse sur la tuile qui touche son bord : la belt commence donc
        # collée à lui, et il ne reste aucune place pour un inserter qui devrait se tenir
        # ENTRE la belt et le foreur. Chercher mieux ne sert à rien — la géométrie
        # l'interdit. On fait donc passer la belt à DEUX tuiles du foreur en un point
        # choisi : le bras tient alors au milieu, prend sur la belt et redonne au foreur.
        # Sans lui la chaîne n'est pas perpétuelle, elle a juste une plus longue amorce.
        relais = self._relais_de_retour(drill, depart, bras, foreur) if burner else None

        vx, vy = depart[0] - cible.x, depart[1] - cible.y
        if abs(vx) >= abs(vy):
            ux, uy = (1.0 if vx > 0 else -1.0), 0.0
        else:
            ux, uy = 0.0, (1.0 if vy > 0 else -1.0)
        belts: list[tuple[float, float]] = []
        complete = False
        pose_ins = None
        essais: list[str] = []
        # Le premier tronçon passe par le RELAIS, s'il en existe un : c'est ce détour
        # de deux tuiles qui rend la chaîne perpétuelle.
        # Le second tronçon repart DU RELAIS, jamais de la dernière tuile posée :
        # `place_belt_line` s'arrête AVANT sa tuile d'arrivée, si bien que le relais
        # lui-même restait vide et que la suite le contournait. Le bras de retour ne
        # trouvait alors rien sur quoi puiser, alors que sa place existait bel et bien.
        origine = depart
        if relais is not None:
            seg0, _ = site_finder.place_belt_line(self.api, depart, relais, portee=portee)
            belts.extend(seg0)
            origine = relais

        for recul in (4.0, 3.0, 2.0):
            arrivee = (cible.x + ux * recul, cible.y + uy * recul)
            seg, complete = site_finder.place_belt_line(
                self.api, origine, arrivee, portee=portee)
            belts.extend(seg)
            origine = belts[-1] if belts else origine
            essais.append(f"recul {recul:.0f} -> {len(seg)} segment(s), "
                          f"bout {belts[-1] if belts else 'aucun'}")
            if not belts:
                continue
            # 4. Le bras qui décharge. `place_inserter_vers` vérifie par LECTURE que le
            #    dépôt tombe dans la machine — une orientation supposée ne suffit pas.
            jr: list = []
            pose_ins = site_finder.place_inserter_vers(
                self.api, (cible.x, cible.y), belts[-1], cible.name, nom=bras, journal=jr)
            essais.append(" ; ".join(jr[:6]))
            if pose_ins is not None:
                break
        if not belts:
            return False, f"aucune belt posée entre le foreur et {cible.name}"
        if pose_ins is None:
            return False, (f"belt posée ({len(belts)} segments) mais aucun inserter "
                           f"n'atteint {cible.name} depuis la belt — {' | '.join(essais)}")

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
        sp = self.builder._scan_patch_local(self.ressource)
        ancre = self.builder._anchor_on_ore(sp, 4) if sp.get("sample") else None
        if ancre is None:
            return False, f"aucun gisement de {self.ressource} exploitable"
        preparer(ancre[0], ancre[1])
        mp = plan_micro(MicroRequest(
            patch=ResourcePatch(resource=self.ressource, tiles=[], bbox=(0, 0, 0, 0)),
            facing=4, anchor=ancre, drill_tier="electric-mining-drill",
            inserter_tier="inserter", furnace_tier="electric-furnace",
            drill_size=3, furnace_size=3))
        # `approach=True` : en production le mod refuse toute pose au-delà de
        # `build_distance` (« walk closer first », mesuré à 10 tuiles). Le foreur est sur
        # le gisement, donc à des dizaines de tuiles de la machine qu'on alimente — il
        # FAUT y aller. En test_mode l'approche est un téléport, elle ne coûte rien.
        rap = execute_micro(self.api, mp, generate=False, approach=True, timeout=90.0)
        if not rap.ok:
            return False, f"chaîne non posée : {rap.missing or rap.blocked[:1]}"
        ancrage = self.dernier_poteau or ancre
        poteaux = site_finder.place_supply_poles(self.api, rap.placed, ancrage)
        return True, (f"chaîne posée ({len(rap.placed)} machines) sur {self.ressource}, "
                      f"{len(poteaux)} poteau(x) de desserte")

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
        d = decide(etat, self.arbitre)
        agi, detail = self.agir(d)
        self.journal.append(f"{d} -> {'agi' if agi else 'sans effet'} ({detail})")
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