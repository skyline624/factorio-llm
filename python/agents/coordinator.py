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
from typing import Optional

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
        return self.reseau is not None and self.production_kw > 0


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


def decide(etat: EtatUsine) -> Decision:
    """Choisit la prochaine action. Fonction PURE : aucun appel RCON, testable seule.

    L'ordre des règles EST le curriculum :
      1. réparer ce qui est cassé (une usine arrêtée ne produit rien) ;
      2. produire du courant s'il n'y en a pas (rien d'électrique ne marchera sans) ;
      3. produire des objets s'il n'y a aucune machine ;
      4. sinon, ne rien faire — et le dire, plutôt que de s'agiter.
    """
    diag = etat.diagnostic
    causes = diag.causes if diag else []

    if causes:
        # La cause la plus grave d'abord ; à gravité égale, l'ordre du diagnostic
        # (déjà trié) départage.
        tete = causes[0]
        action, explication = REPARATION.get(tete.cause, ("inspecter", "cause inconnue"))
        return Decision(action=action,
                        raison=f"{tete.name} : {tete.cause} — {explication}",
                        priorite=PRIORITE["reparer"], cible=tete)

    if not etat.a_de_l_energie:
        return Decision(action="batir_energie",
                        raison=("aucun réseau alimenté : rien d'électrique ne "
                                "fonctionnera avant"),
                        priorite=PRIORITE["batir_energie"])

    if etat.machines == 0:
        return Decision(action="batir_production",
                        raison="du courant, mais aucune machine pour en profiter",
                        priorite=PRIORITE["batir_production"])

    return Decision(action="rien",
                    raison=f"{etat.machines} machine(s) en état de marche",
                    priorite=PRIORITE["rien"])


class Coordinator:
    """Boucle observe -> diagnostique -> décide -> agit -> vérifie.

    `observer` et `agir` touchent le jeu ; `decide` reste pur. Ce découpage permet de
    tester tout le raisonnement sans serveur, et de ne réserver le live qu'à ce qui
    ne peut pas être simulé.
    """

    def __init__(self, api, zone: tuple[float, float] = (0.0, 0.0), rayon: float = 30.0):
        self.api = api
        self.zone = zone
        self.rayon = rayon
        self.journal: list[str] = []

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

        Seules les réparations à portée d'une primitive sont traitées ici. Bâtir une
        centrale ou une chaîne relève du FactoryBuilder : le Coordinator décide QUOI,
        pas COMMENT — c'est la frontière posée par la roadmap.
        """
        if d.action == "rien":
            return False, "rien à faire"
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

    # ----- BOUCLE -----

    def tick(self) -> tuple[Decision, bool, EtatUsine]:
        """Un tour complet. Retourne (décision, a_agi, état APRÈS action.

        L'état rendu est relu après l'action : une décision n'est jugée que sur son
        effet, jamais sur le fait qu'elle ait été prise.
        """
        etat = self.observer()
        d = decide(etat)
        agi, detail = self.agir(d)
        self.journal.append(f"{d} -> {'agi' if agi else 'sans effet'} ({detail})")
        return d, agi, (self.observer() if agi else etat)