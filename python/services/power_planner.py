"""PowerPlanner — dimensionner et implanter une centrale vapeur (jalon électricité).

Premier jalon où l'usine cesse de dépendre d'un humain qui verse du charbon dans
chaque machine : une centrale alimente un réseau, et les machines électriques n'ont
plus de combustible individuel. Le charbon ne disparaît pas — un boiler en brûle —
mais il n'y a plus qu'UN point d'alimentation au lieu d'un par machine.

Déterministe pur (règle agents-roadmap §3 : un algorithme -> service partagé, pas un
agent LLM). Le FactoryBuilder décidera *où* et *combien* ; ce module calcule *quoi*.

CE QUI EST MESURÉ, CE QUI EST FIXTURE
-------------------------------------
Le mod ne peut pas lire les puissances : `max_energy_production`, `supply_area_distance`
et `max_wire_distance` sont inaccessibles au runtime (constat E3a). Deux valeurs le sont
en revanche, et elles portent l'essentiel du dimensionnement :

    steam-engine.fluid_usage_per_tick = 0.5   -> 30 vapeur/s   (MESURÉ en jeu)
    offshore-pump.pumping_speed       = 20    -> 1200 eau/s    (MESURÉ en jeu)

Le reste se DÉDUIT de ces deux mesures et de deux seules constantes :

    STEAM_ENERGY_KJ  = 30    (vapeur à 165 °C : (165-15) x 200 J/°C)
    BOILER_POWER_KW  = 1800

d'où, sans autre hypothèse :
    puissance d'un steam-engine = 30 vapeur/s x 30 kJ            = 900 kW
    vapeur produite par boiler  = 1800 kW / 30 kJ                = 60 vapeur/s
    steam-engines par boiler    = 60 / 30                        = 2
    boilers par offshore-pump   = 1200 eau/s / 60 eau/s          = 20

Ces ratios (1 boiler -> 2 engines, 1 pompe -> 20 boilers) ne sont donc PAS des nombres
posés à la main : ils tombent du calcul. Un fixture faux se verrait immédiatement, car
`verify_power_e3b.py` compare la capacité calculée ici au `productionKW` réellement lu
sur le réseau par `get_power_state`. C'est la différence avec le GEOMETRY_FIXTURE de S2,
qui n'avait aucun moyen de se contredire.

GÉOMÉTRIE (mesurée en jeu, E3a — facing north, relatif au centre réel)
    offshore-pump 2x2 : sortie (0,0)      -> voisin (0,+1)
    boiler        3x2 : eau (-1,+0.5) -> (-2,+0.5) et (+1,+0.5) -> (+2,+0.5)
                        vapeur (0,-0.5)   -> voisin (0,-1.5)
    steam-engine  3x5 : entrées (0,+2) -> (0,+3) et (0,-2) -> (0,-3)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from services.layout_planner import FACING_UNIT, LayoutEntity

# ===== Caractéristiques (cf. docstring : mesuré vs fixture) =====

ENGINE_STEAM_PER_S = 30.0      # MESURÉ : fluid_usage_per_tick 0.5 x 60 ticks/s
PUMP_WATER_PER_S = 1200.0      # MESURÉ : pumping_speed 20 x 60 ticks/s
STEAM_ENERGY_KJ = 30.0         # fixture : (165 - 15) °C x 200 J/°C
BOILER_POWER_KW = 1800.0       # fixture
FUEL_MJ = {"coal": 4.0, "solid-fuel": 12.0, "wood": 2.0}   # fixture (valeur énergétique)

# Dérivés — aucun de ces nombres n'est saisi à la main.
ENGINE_POWER_KW = ENGINE_STEAM_PER_S * STEAM_ENERGY_KJ          # 900
BOILER_STEAM_PER_S = BOILER_POWER_KW / STEAM_ENERGY_KJ          # 60
BOILER_WATER_PER_S = BOILER_STEAM_PER_S                         # 1 eau -> 1 vapeur
ENGINES_PER_BOILER = BOILER_STEAM_PER_S / ENGINE_STEAM_PER_S    # 2
BOILERS_PER_PUMP = PUMP_WATER_PER_S / BOILER_WATER_PER_S        # 20


@dataclass
class PowerRequest:
    """Demande de puissance : combien de kW, avec quel combustible, quels tiers."""
    demand_kw: float
    fuel: str = "coal"
    pump_tier: str = "offshore-pump"
    boiler_tier: str = "boiler"
    engine_tier: str = "steam-engine"
    pole_tier: str = "small-electric-pole"
    # Marge : une centrale dimensionnée au plus juste décroche au moindre pic.
    margin: float = 0.0


@dataclass
class PowerSizing:
    """Dimensionnement d'une centrale vapeur — le QUOI, avant le OÙ.

    `capacity_kw` est la puissance réellement installée (multiple de 900 kW) : elle
    dépasse la demande dès que celle-ci n'est pas un multiple exact. `fuel_per_s`
    est la consommation induite, celle qu'il faudra automatiser pour que la centrale
    tienne sans intervention — sans quoi on a juste déplacé le problème du charbon.
    """
    engines: int = 0
    boilers: int = 0
    pumps: int = 0
    capacity_kw: float = 0.0
    demand_kw: float = 0.0
    fuel: str = "coal"
    fuel_per_s: float = 0.0            # combustible consommé par seconde
    water_per_s: float = 0.0
    steam_per_s: float = 0.0
    feasibility: str = "ok"
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.feasibility == "ok"


def size_power(request: PowerRequest) -> PowerSizing:
    """Dimensionne une centrale vapeur pour `request.demand_kw`.

    Chaîne : offshore-pump -> boiler(s) -> steam-engine(s). On part du besoin en
    puissance et on remonte : engines (la puissance), puis boilers (la vapeur qu'ils
    consomment), puis pompes (l'eau que les boilers consomment).

    `feasibility` = "no_demand" si la demande est nulle ou négative, "unknown_fuel" si
    le combustible n'a pas de valeur énergétique connue, "ok" sinon.
    """
    demande = float(request.demand_kw) * (1.0 + max(0.0, request.margin))
    if demande <= 0:
        return PowerSizing(demand_kw=float(request.demand_kw), fuel=request.fuel,
                           feasibility="no_demand",
                           notes=[f"demande non exploitable: {request.demand_kw} kW"])
    if request.fuel not in FUEL_MJ:
        return PowerSizing(demand_kw=float(request.demand_kw), fuel=request.fuel,
                           feasibility="unknown_fuel",
                           notes=[f"valeur energetique inconnue pour {request.fuel!r} "
                                  f"(connus: {sorted(FUEL_MJ)})"])

    # Remontée de la chaîne. ceil partout : une demi-machine ne produit rien.
    engines = math.ceil(demande / ENGINE_POWER_KW)
    boilers = math.ceil(engines / ENGINES_PER_BOILER)
    pumps = math.ceil(boilers / BOILERS_PER_PUMP)

    steam = engines * ENGINE_STEAM_PER_S
    water = boilers * BOILER_WATER_PER_S
    # Un boiler ne brûle QUE ce qu'il transforme : sa puissance divisée par la valeur
    # energetique du combustible. Coal 4 MJ -> 0.45 charbon/s par boiler a plein régime.
    fuel_per_s = boilers * (BOILER_POWER_KW / 1000.0) / FUEL_MJ[request.fuel]

    s = PowerSizing(
        engines=engines, boilers=boilers, pumps=pumps,
        capacity_kw=engines * ENGINE_POWER_KW,
        demand_kw=float(request.demand_kw),
        fuel=request.fuel,
        fuel_per_s=round(fuel_per_s, 4),
        water_per_s=water,
        steam_per_s=steam,
    )
    s.notes.append(
        f"{pumps} {request.pump_tier} -> {boilers} {request.boiler_tier} -> "
        f"{engines} {request.engine_tier} = {s.capacity_kw:.0f} kW "
        f"pour {demande:.0f} kW demandes")
    s.notes.append(
        f"consommation induite : {s.fuel_per_s:.2f} {request.fuel}/s, "
        f"{water:.0f} eau/s (a automatiser, sinon la centrale s'arrete)")
    surplus = s.capacity_kw - demande
    if surplus > 0:
        s.notes.append(f"surcapacite {surplus:.0f} kW (granularite "
                       f"{ENGINE_POWER_KW:.0f} kW par {request.engine_tier})")
    return s


def fuel_autonomy_s(sizing: PowerSizing, fuel_count: int) -> float:
    """Combien de secondes `fuel_count` unités de combustible tiennent, à plein régime.

    Sert à répondre concrètement à « combien de temps avant que ça s'arrête ? ». Le
    bootstrap burner posait 5 charbons par machine, soit ~2 minutes : c'est cette
    grandeur-là qu'il faut regarder pour juger une alimentation, pas le fait que la
    machine tourne à l'instant du contrôle.
    """
    if sizing.fuel_per_s <= 0:
        return math.inf
    return round(fuel_count / sizing.fuel_per_s, 1)


def describe_sizing(sizing: PowerSizing) -> str:
    """Résumé lisible d'un dimensionnement (journal d'agent, rapports de test)."""
    if not sizing.ok:
        return f"centrale non dimensionnable ({sizing.feasibility})"
    return (f"{sizing.capacity_kw:.0f} kW installes "
            f"({sizing.pumps}p/{sizing.boilers}b/{sizing.engines}e) "
            f"pour {sizing.demand_kw:.0f} kW demandes, "
            f"{sizing.fuel_per_s:.2f} {sizing.fuel}/s")


# ===== Implantation (le OÙ) =====

# Pas d'implantation, déduits des emprises mesurées (E3a) :
BOILER_PITCH = 4.0     # Le pas 3 (emprise du boiler) chaîne les boilers SANS tuyau —
                       # élégant, mais il rend les colonnes de moteurs jointives et ne
                       # laisse aucune place aux poteaux. Or un générateur doit être dans
                       # la zone de fourniture d'un poteau pour injecter son courant.
                       # Au pas 4, une colonne d'une tuile reste libre entre les moteurs :
                       # elle accueille le poteau, et un tuyau intercalaire relie les
                       # ports d'eau (voisin droit du boiler i et voisin gauche du i+1
                       # tombent tous deux sur cette même tuile).
ENGINE_PITCH = 5.0     # emprise y du steam-engine : les moteurs s'enfilent bout à bout.
BOILER_TO_ENGINE = 3.5 # sortie vapeur (0,-0.5) -> voisin (0,-1.5) = port sud du moteur
                       # placé en (0,-3.5), dont le port sud est à (0,+2) de son centre.
POLE_WIRE_REACH = 7.5  # fixture : max_wire_distance illisible au runtime (constat E3a)
POLE_SUPPLY_RADIUS = 2.5


@dataclass
class PowerPlan:
    """Centrale implantée : entités posables + ce qu'elle coûte et rapporte."""
    entities: list[LayoutEntity] = field(default_factory=list)
    # Graphe de flux, comme MicroPlan/LayoutPlan : `execute_micro` le lit pour ordonner
    # la pose. Vide ici — une centrale n'a pas d'ordre imposé (les fluides circulent dès
    # que les entités sont adjacentes), l'executor retombe alors sur l'ordre de la liste.
    connections: list[tuple[int, int, str]] = field(default_factory=list)
    totals: dict[str, int] = field(default_factory=dict)
    sizing: Optional[PowerSizing] = None
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    feasibility: str = "ok"
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.feasibility == "ok"


def _add(entities: list, name: str, x: float, y: float, direction: int, role: str) -> int:
    entities.append(LayoutEntity(name, x, y, direction, role))
    return len(entities) - 1


def plan_power(request: PowerRequest, origin: tuple[float, float],
               pump_pos: Optional[tuple[float, float]] = None,
               pump_direction: int = 0) -> PowerPlan:
    """Implante une centrale vapeur, premier boiler ancré sur `origin`.

    Layout (tout orienté nord, u = -y) — colonnes de boilers alignées en x, moteurs
    empilés au nord de chaque boiler :

        engine (bx, by-8.5)      \\  2 moteurs par boiler : c'est exactement ce que
        engine (bx, by-3.5)      /   sa vapeur alimente (60 / 30 = 2)
        boiler (bx, by)  boiler (bx+3, by)  ...  chaînés par leurs ports d'eau
        ~~~~ eau ~~~~ (pipes depuis l'offshore-pump)

    `origin` est la position du PREMIER boiler ; elle est snappée à la grille légale
    (emprise 3 en x -> centre de tuile, 2 en y -> entier), sans quoi `can_place_check`
    et `create_entity` divergent d'une demi-tuile (leçon MicroPlanner).

    `pump_pos` / `pump_direction` : emplacement de l'offshore-pump. MESURÉ en jeu — la
    pompe se pose sur la RIVE (une tuile de terre adjacente à l'eau), pas sur l'eau, et
    sa direction pointe VERS l'eau. `scan_water_edge` renvoyant des tuiles d'*eau*, il
    faut prendre une de leurs voisines terrestres : c'est ce que fait l'appelant. Sa
    sortie tombe alors du côté opposé à la direction (elle prend devant, déverse
    derrière), et c'est de là que part le tuyau.

    Sans `pump_pos`, la pompe et l'amenée d'eau ne sont pas planifiées et `feasibility`
    passe à "no_water" — mieux vaut pas de plan qu'une centrale qui se poserait sans
    jamais démarrer.
    """
    sizing = size_power(request)
    if not sizing.ok:
        return PowerPlan(sizing=sizing, feasibility=sizing.feasibility,
                         notes=list(sizing.notes))

    # Grille : boiler 3(x) x 2(y) -> x sur centre de tuile, y sur entier.
    bx0 = math.floor(origin[0]) + 0.5
    by0 = float(round(origin[1]))

    entities: list[LayoutEntity] = []
    restants = sizing.engines
    for i in range(sizing.boilers):
        bx = bx0 + i * BOILER_PITCH
        _add(entities, request.boiler_tier, bx, by0, 0, "boiler")
        # Moteurs de cette colonne : 2 au plus, moins si le dernier boiler n'en porte
        # qu'un (demande non multiple de 1800 kW).
        for j in range(min(int(ENGINES_PER_BOILER), restants)):
            ey = by0 - BOILER_TO_ENGINE - j * ENGINE_PITCH
            _add(entities, request.engine_tier, bx, ey, 0, "steam-engine")
            restants -= 1
        # Tuyau reliant ce boiler au suivant (le pas 4 laisse une tuile entre eux).
        if i < sizing.boilers - 1:
            _add(entities, "pipe", bx + 2.0, by0 + 0.5, 0, "pipe")

    # Poteaux : dans les couloirs libres entre colonnes de moteurs, plus un de chaque
    # côté de la rangée. Portée de fourniture 2.5 -> chaque poteau couvre les colonnes
    # qui l'encadrent ; l'écart entre poteaux (4) reste sous POLE_WIRE_REACH, donc la
    # ligne forme un seul réseau.
    for i in range(sizing.boilers + 1):
        px = bx0 - 2.0 + i * BOILER_PITCH
        _add(entities, request.pole_tier, px, by0 - BOILER_TO_ENGINE, 0, "pole")

    plan = PowerPlan(entities=entities, sizing=sizing, notes=list(sizing.notes))

    # Amenée d'eau : sans elle la centrale ne démarre pas, on refuse de planifier.
    if pump_pos is None:
        plan.feasibility = "no_water"
        plan.notes.append("aucune position de pompe fournie : offshore-pump et tuyau non "
                          "planifies (scan_water_edge + voisine terrestre)")
    else:
        wx = math.floor(pump_pos[0]) + 0.5
        wy = math.floor(pump_pos[1]) + 0.5
        _add(entities, request.pump_tier, wx, wy, pump_direction, "offshore-pump")
        # Sortie de la pompe : la tuile OPPOSÉE à sa direction (elle puise devant, dans
        # l'eau, et déverse derrière, côté terre). Mesuré : facing north -> sortie (0,+1).
        ux, uy = FACING_UNIT.get(pump_direction, (0.0, -1.0))
        depart = (wx - ux, wy - uy)
        # Tuyau en L jusqu'au port d'eau GAUCHE du premier boiler, qui se raccorde sur
        # la tuile (bx0 - 2, by0 + 0.5).
        cible = (bx0 - 2.0, by0 + 0.5)
        for px, py in _pipe_run(depart, cible):
            _add(entities, "pipe", px, py, 0, "pipe")
        plan.notes.append(f"amenee d'eau : offshore-pump@({wx},{wy}) dir={pump_direction} "
                          f"-> sortie{depart} -> port boiler@({cible[0]},{cible[1]})")

    plan.totals = {}
    for e in entities:
        plan.totals[e.name] = plan.totals.get(e.name, 0) + 1
    xs = [e.x for e in entities]
    ys = [e.y for e in entities]
    plan.bbox = (min(xs), min(ys), max(xs), max(ys))
    return plan


def plan_transmission(start: tuple[float, float], end: tuple[float, float],
                      pole_tier: str = "small-electric-pole",
                      reach: float = POLE_WIRE_REACH,
                      margin: float = 0.5) -> list[LayoutEntity]:
    """Ligne de poteaux reliant `start` à `end`, espacés sous la portée de fil.

    Besoin structurel, pas un cas de test : sur une carte réelle le gisement et l'eau
    sont rarement voisins — mesuré ici, tous les minerais sont à plus de 100 tuiles du
    premier plan d'eau. Une centrale ne sert à rien si son courant n'atteint pas les
    machines, et `plan_power` ne câble que ses propres rangées.

    L'espacement retenu est `reach - margin` : à la portée exacte, la moindre tuile de
    décalage au moment de la pose (terrain, snap) couperait la ligne en deux réseaux —
    et une ligne coupée ne se voit pas au moment de poser, seulement quand la machine
    au bout reste sans courant.

    Les deux extrémités sont incluses ; les positions sont des centres de tuile
    (poteau 1×1). Retourne [] si `start` et `end` sont sur la même tuile.
    """
    x0, y0 = math.floor(start[0]) + 0.5, math.floor(start[1]) + 0.5
    x1, y1 = math.floor(end[0]) + 0.5, math.floor(end[1]) + 0.5
    distance = math.hypot(x1 - x0, y1 - y0)
    pas = max(1.0, reach - margin)
    n = int(math.ceil(distance / pas)) if distance > 0 else 0

    entities: list[LayoutEntity] = []
    vues: set[tuple[float, float]] = set()
    for i in range(n + 1):
        t = i / n if n else 0.0
        px = math.floor(x0 + (x1 - x0) * t) + 0.5
        py = math.floor(y0 + (y1 - y0) * t) + 0.5
        if (px, py) in vues:
            continue
        vues.add((px, py))
        entities.append(LayoutEntity(pole_tier, px, py, 0, "pole"))
    return entities


def _pipe_run(start: tuple[float, float], end: tuple[float, float]) -> list[tuple[float, float]]:
    """Tuyaux d'un trajet en L entre deux centres de tuile (horizontal puis vertical).

    Les DEUX extrémités sont incluses. `end` est la tuile de RACCORDEMENT du boiler
    (son `target_position`), pas son port : le port est à l'intérieur de l'entité, la
    tuile voisine doit donc porter un tuyau. Un premier jet l'excluait « puisque le
    boiler s'y raccorde tout seul » — le tuyau s'arrêtait une tuile trop tôt, l'eau
    n'arrivait jamais et le moteur restait `no_input_fluid` en jeu.
    """
    x0, y0 = math.floor(start[0]) + 0.5, math.floor(start[1]) + 0.5
    x1, y1 = math.floor(end[0]) + 0.5, math.floor(end[1]) + 0.5
    tuiles: list[tuple[float, float]] = []
    pas = 1.0 if x1 >= x0 else -1.0
    x = x0
    while abs(x - x1) > 1e-6:
        tuiles.append((x, y0))
        x += pas
    pas = 1.0 if y1 >= y0 else -1.0
    y = y0
    while abs(y - y1) > 1e-6:
        tuiles.append((x1, y))
        y += pas
    tuiles.append((x1, y1))
    return tuiles