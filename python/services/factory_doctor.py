"""FactoryDoctor — pourquoi une usine ne produit pas. Déterministe.

Le benchmark Factorio Learning Environment (arXiv 2503.09617) identifie le débogage
systémique comme le premier mode d'échec des agents LLM : « focusing on individual
machines rather than topology ». Ils regardent la machine qui affiche une erreur, pas
celle qui la cause. La parade retenue par ce projet est de ne PAS confier ce diagnostic
à un modèle : lire l'état réel, classer, et remonter à la cause racine est un algorithme.

Deux principes :

1. **Distinguer la cause du symptôme.** Un four `waiting_for_source_items` n'a rien qui
   cloche : c'est le drill en amont, sans courant, qui est en faute. Réparer le four ne
   servirait à rien. Le diagnostic propage donc les pannes vers l'aval et ne retient
   comme causes que les machines dont le problème est *propre*.

2. **Une entité DÉBRANCHÉE n'est pas une entité sans courant.** `networkId` absent =
   personne ne l'a reliée ; `no_power` = elle est reliée à un réseau à sec. Ce sont deux
   réparations différentes (poser un poteau / agrandir la centrale), et c'est exactement
   le genre de nuance qu'un statut brut ne donne pas.

Sortie : des symptômes typés, triés par gravité, avec un texte lisible destiné au
journal d'un agent — le Coordinator décidera quoi faire, il n'a pas à interpréter des
statuts Factorio.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

# Statut renvoyé par le mod (utils_entity.status_name) -> (cause, gravité, explication).
# gravité : 2 = arrêté, 1 = ralenti ou en attente, 0 = normal.
# `other` est le fourre-tout du mod pour les statuts qu'il ne mappe pas : on ne peut
# rien en conclure, et prétendre le contraire produirait de faux diagnostics.
STATUTS: dict[str, tuple[str, int, str]] = {
    "working":                        ("ok", 0, "en production"),
    "normal":                         ("ok", 0, "en production"),
    "no_power":                       ("sans_courant", 2, "reliée à un réseau sans courant"),
    "low_power":                      ("courant_insuffisant", 1, "réseau sous-dimensionné"),
    "no_fuel":                        ("sans_combustible", 2, "réservoir vide"),
    "no_ingredients":                 ("entree_vide", 2, "aucun ingrédient"),
    "item_ingredient_shortage":       ("entree_vide", 2, "ingrédient manquant"),
    "waiting_for_source_items":       ("entree_vide", 2, "rien à traiter en entrée"),
    "no_input_fluid":                 ("entree_vide", 2, "aucun fluide en entrée"),
    "no_recipe":                      ("sans_recette", 2, "aucune recette réglée"),
    "full_output":                    ("sortie_bloquee", 1, "sortie pleine"),
    "waiting_for_space_in_destination": ("sortie_bloquee", 1, "sortie encombrée"),
    "disabled":                       ("desactivee", 2, "désactivée"),
    # Le foreur a vidé les tuiles sous son emprise. Mesuré en partie longue : 1 tuile et
    # 23 unités restantes sous la foreuse, pendant que 487 tuiles et 312 000 unités du
    # MÊME gisement attendaient à quelques pas. L'usine était morte de faim au milieu de
    # l'abondance, et le statut n'était pas dans cette table : personne ne l'a vu.
    "no_minable_resources":           ("gisement_epuise", 2, "plus de minerai sous l'emprise"),
    "other":                          ("indetermine", 0, "statut non interprété par le mod"),
}

# Causes dont la machine est elle-même responsable : elles ne viennent pas de l'amont.
CAUSES_PROPRES = frozenset({"sans_courant", "courant_insuffisant", "sans_combustible",
                            "sans_recette", "debranchee", "desactivee",
                            # Un gisement épuisé ne vient pas de l'amont : le foreur EST
                            # l'amont. C'est même la panne qui affame tout le reste, donc
                            # celle qui doit déclasser les « entrée vide » en aval.
                            "gisement_epuise"})

# Types dont l'état ne fait que REFLÉTER celui du voisinage. Un inserter passe sa vie à
# attendre le prochain objet : « entrée vide » y est un état normal, pas une panne. Le
# signaler comme cause noierait le diagnostic sous des symptômes qui ne se réparent pas.
# Ces entités ne peuvent être une cause racine que par une panne PROPRE (courant, etc.).
TYPES_TRANSIT = frozenset({"inserter", "transport-belt", "underground-belt", "splitter",
                           "loader", "pipe", "pipe-to-ground", "pump"})


@dataclass
class Symptome:
    """Un problème constaté sur une machine, avec ce qu'on en sait."""
    name: str
    x: float
    y: float
    cause: str
    gravite: int
    detail: str
    racine: bool = True        # False = conséquence probable d'une panne en amont

    def __str__(self) -> str:
        marque = "CAUSE " if self.racine else "effet "
        return f"{marque}{self.name}@({self.x},{self.y}) : {self.cause} — {self.detail}"


@dataclass
class Diagnostic:
    symptomes: list[Symptome] = field(default_factory=list)
    machines: int = 0
    en_panne: int = 0
    notes: list[str] = field(default_factory=list)

    @property
    def sain(self) -> bool:
        return self.en_panne == 0

    @property
    def causes(self) -> list[Symptome]:
        """Les problèmes à traiter, les conséquences étant écartées."""
        return [s for s in self.symptomes if s.racine and s.gravite > 0]

    def resume(self) -> str:
        if self.sain:
            return f"{self.machines} machine(s), aucune en panne"
        tetes = self.causes[:3]
        suite = "" if len(self.causes) <= 3 else f" (+{len(self.causes) - 3} autre(s))"
        return (f"{self.en_panne}/{self.machines} machine(s) en panne — "
                + " ; ".join(str(s) for s in tetes) + suite)


def _classer(status: Optional[str]) -> tuple[str, int, str]:
    """Statut du mod -> (cause, gravité, explication).

    Un statut ABSENT de la table n'est pas anodin : c'est un état que ce diagnostic n'a
    jamais rencontré, donc dont il ne sait rien. Le classer en gravité 0 le rendait
    MUET — mesuré, un `no_minable_resources` a laissé l'usine à l'arrêt 60 tours durant
    pendant que l'agent décidait « rien, tout va bien ». Une panne qu'on ne sait pas
    nommer doit se voir : gravité 1 et cause `inconnu`, ce qui la porte devant l'agent
    (et, s'il en a un, devant l'Enquêteur) au lieu de la faire disparaître.

    `other` reste à 0, et c'est différent : le mod l'emploie pour des états normaux
    d'inactivité, il ne signale rien en propre. On distingue « le mod ne sait pas
    interpréter » de « nous ne connaissons pas ce statut ».
    """
    if not status:
        return ("indetermine", 0, "statut illisible")
    return STATUTS.get(status, ("inconnu", 1, f"statut jamais rencontré : {status}"))


def diagnose(rows: list[dict], power: Optional[dict] = None) -> Diagnostic:
    """Diagnostique un ensemble de machines déjà observées.

    `rows` : entités telles que les rend `scan_area`/`scan_factory` (name, x, y, status).
    `power` : {(x, y): état get_power_state}, optionnel — il permet la distinction
    débranché / sans courant, que le statut seul ne donne pas.

    Fonction pure : aucun appel RCON, donc testable sans serveur. La collecte est faite
    par l'appelant (`diagnose_zone` ci-dessous).
    """
    diag = Diagnostic(machines=len(rows))
    power = power or {}

    for r in rows:
        name = str(r.get("name", "?"))
        x, y = float(r.get("x", 0.0)), float(r.get("y", 0.0))
        cause, gravite, detail = _classer(r.get("status"))

        # Une machine électrique sans réseau n'est pas « sans courant » : elle n'est
        # reliée à rien. La réparation diffère (poser un poteau vs agrandir la centrale).
        etat = power.get((x, y))
        if etat and etat.get("found") and cause in ("sans_courant", "courant_insuffisant"):
            if etat.get("networkId") is None:
                cause, gravite, detail = ("debranchee", 2,
                                          "aucun réseau électrique ne la dessert")
            else:
                sat = etat.get("satisfaction")
                prod = etat.get("productionKW")
                detail += (f" (réseau {etat.get('networkId')}, production {prod} kW"
                           + (f", satisfaction {sat}" if sat is not None else "") + ")")

        if gravite > 0:
            s = Symptome(name, x, y, cause, gravite, detail)
            # Un organe de transit qui attend ne signale rien : c'est son régime normal.
            # Seule une panne propre (courant, combustible) en fait une cause.
            if r.get("type") in TYPES_TRANSIT and cause not in CAUSES_PROPRES:
                s.racine = False
                s.gravite = min(s.gravite, 1)
                s.detail += " — organe de transit, état normal en l'absence de flux"
            diag.symptomes.append(s)

    # Propagation : si au moins une machine a une panne PROPRE, celles qui manquent
    # seulement d'entrée en sont probablement la conséquence. On ne les efface pas —
    # on les déclasse, pour que le lecteur voie la chaîne sans être noyé.
    propres = [s for s in diag.symptomes if s.cause in CAUSES_PROPRES]
    if propres:
        for s in diag.symptomes:
            if s.cause == "entree_vide" and s.racine:
                s.racine = False
                s.detail += " — probable conséquence d'une panne en amont"

    # UNE CENTRALE À SEC EXPLIQUE TOUT LE RÉSEAU. Même raisonnement d'un cran plus haut :
    # si une chaudière ou un générateur manque de combustible, les machines « sans
    # courant » n'ont pas chacune un problème — elles ont TOUTES le même, et il est en
    # amont. Sans ce déclassement, le diagnostic rendait quatre `renforcer_energie` de
    # gravité 3 qui masquaient le ravitaillement de la chaudière : l'agent renforçait
    # sans fin une centrale qui n'attendait qu'un seau de charbon.
    centrale_a_sec = [s for s in diag.symptomes
                      if s.cause in ("sans_combustible", "sans_eau")
                      and s.name in ("boiler", "steam-engine", "nuclear-reactor")]
    if centrale_a_sec:
        for s in diag.symptomes:
            if s.cause in ("sans_courant", "courant_insuffisant") and s.racine:
                s.racine = False
                s.gravite = min(s.gravite, 1)
                s.detail += (f" — conséquence : {centrale_a_sec[0].name} en amont "
                             f"({centrale_a_sec[0].cause})")
        diag.notes.append(
            f"{len(centrale_a_sec)} organe(s) de production d'énergie en panne : les "
            f"machines sans courant en sont la conséquence, pas la cause")
        diag.notes.append(
            f"{len(propres)} panne(s) propre(s) détectée(s) : les machines à l'entrée "
            f"vide sont traitées comme des conséquences, pas comme des causes")

    diag.en_panne = sum(1 for s in diag.symptomes if s.gravite > 0)
    diag.symptomes.sort(key=lambda s: (not s.racine, -s.gravite, s.name))
    return diag


def diagnose_zone(api, x: float, y: float, radius: float = 30.0,
                  types: tuple[str, ...] = ("mining-drill", "furnace", "assembling-machine",
                                            "inserter", "generator", "boiler"),
                  rows_sup=None) -> Diagnostic:
    """Observe une zone puis la diagnostique.

    LA ZONE EST BIEN (x, y) — elle ne l'était pas. Cette fonction passait par `scan_area`,
    centré sur le PERSONNAGE, et ses deux premiers paramètres ne servaient qu'à décrire
    une intention que le code ne tenait pas : le diagnostic suivait l'agent au lieu de
    surveiller l'usine. Il fallait donc « s'y téléporter ou s'y rendre au préalable », ce
    qu'aucun appelant ne peut garantir d'un agent qui va miner du charbon à deux cents
    tuiles — c'est-à-dire précisément ce qu'on lui demande de faire.

    Mesuré au banc d'endurance : la chaudière à sec était nommée au premier tour, puis
    plus jamais dès que l'agent s'était éloigné. Le diagnostic rendait « aucune cause »
    sur une usine morte, l'agent n'avait donc rien à réparer, et il partait chercher une
    technologie pendant que sa centrale s'éteignait.

    `rows_sup` ajoute des machines observées AILLEURS. Les centrales se posent au bord de
    l'eau, parfois à cent tuiles de l'usine : elles échappaient donc au diagnostic, et
    deux boilers à sec ont arrêté toute la production sans qu'aucune cause ne soit
    produite. Une machine déjà vue dans la zone n'est pas ajoutée deux fois — elle
    compterait double dans `machines` et produirait deux fois la même cause.
    """
    sa = api.inspect_at(x, y, radius)
    rows = [e for e in (sa.get("entities", []) if isinstance(sa, dict) else [])
            if e.get("type") in types]
    vues = {(e.get("name"), round(float(e.get("x", 0.0))), round(float(e.get("y", 0.0))))
            for e in rows}
    for e in (rows_sup or []):
        cle = (e.get("name"), round(float(e.get("x", 0.0))), round(float(e.get("y", 0.0))))
        if cle not in vues:
            vues.add(cle)
            rows.append(e)
    power: dict[tuple[float, float], dict] = {}
    for r in rows:
        cause, gravite, _ = _classer(r.get("status"))
        # On n'interroge le réseau que si le statut évoque l'électricité : chaque appel
        # est un aller-retour RCON.
        if cause in ("sans_courant", "courant_insuffisant"):
            rx, ry = float(r.get("x", 0.0)), float(r.get("y", 0.0))
            power[(rx, ry)] = api.get_power_state(rx, ry, 2.0)
    return diagnose(rows, power)