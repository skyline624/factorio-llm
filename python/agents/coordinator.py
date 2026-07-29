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
from typing import Optional, Protocol

from services import perception
from services.factory_doctor import Diagnostic, Symptome, diagnose_zone

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
PRIORITE = {"reparer": 3, "batir_energie": 2, "batir_production": 1, "rien": 0}


@dataclass
class EtatUsine:
    """Photographie de l'usine, telle que le Coordinator la voit avant de décider."""
    machines: int = 0
    diagnostic: Optional[Diagnostic] = None
    reseau: Optional[int] = None          # networkId observé, None = aucun réseau
    production_kw: float = 0.0
    inventaire: dict = field(default_factory=dict)

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
class Decision:
    """Ce que le Coordinator a décidé, et pourquoi — le « pourquoi » est la moitié utile."""
    action: str
    raison: str
    priorite: int = 0
    cible: Optional[Symptome] = None

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
        options.append(Decision(action=action,
                                raison=f"{c.name} : {c.cause} — {explication}",
                                priorite=PRIORITE["reparer"], cible=c))

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
                 arbitre: Optional[Arbitre] = None):
        self.api = api
        self.zone = zone
        self.rayon = rayon
        self.ressource = ressource
        self.demande_kw = demande_kw
        self.combustible = combustible
        # Point d'insertion d'un arbitrage LLM : None = décision déterministe.
        # Il n'est consulté que lorsqu'il y a réellement plusieurs options.
        self.arbitre = arbitre
        self.journal: list[str] = []
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
        return etat

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
        if d.cible is None:
            return False, f"{d.action} : délégué (aucune cible ponctuelle)"

        c = d.cible
        if d.action == "ravitailler":
            r = self.api.run_action(self.api.move_items_at, "coal", c.name, c.x, c.y,
                                    50, True, timeout=30.0)
            ok = isinstance(r, dict) and r.get("ok") is True
            return ok, f"ravitaillement de {c.name}@({c.x},{c.y}) : {r}"
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
        return False, f"{d.action} : pas encore automatisé"

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
        rap = execute_micro(self.api, mp, generate=False, approach=False, timeout=30.0)
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
        return d, agi, (self.observer() if agi else etat)