"""LayoutPlanner — placement déterministe d'une chaîne de production (S0).

Sous-module déterministe de FactoryBuilder (cf. docs/layout-planner.md). Transforme
une BOM (sortie du ProductionSolver) + le TERRAIN réel (scanné via RCON) en un
blueprint positionné et adapté au terrain : liste d'entités {name, x, y, direction,
role} + connexions. Sans LLM : l'arbitrage (choix des tiers, gisement cible, replan
lourd) reste à FactoryBuilder ; le LayoutPlanner CALCULE la logistique (combien de
belts/inserters/poles) à partir des débits, comme le solveur calcule le nombre de
machines.

Parallèle :
  ProductionSolver : (item, rate)   -> BOM       (machines + quantités, débits effectifs)
  LayoutPlanner   : BOM + terrain   -> Blueprint (entités + positions + orientations + connexions, dimensionné au débit)

Périmètre S0 : chaîne linéaire single-product (mining -> smelt -> craft cible),
1 ingrédient principal par étage (chaîne fer), layout en bande (manifold), facing
géré (0/2/4/6 via rotation), foreuses sur le gisement réel, dimensionnement
logistique (belts/inserters/poles). Pas de fluides (S2), splitters (S1), beacons
(S3), contournement de terrain (S4), belts de transition physiques entre étages
(logiques en S0 -> S1).

Modèle S0 — repère local (u, v) :
  u = axe de la CASCADE (direction facing) : les étages s'enchaînent le long de u.
  v = axe de la RANGÉE (perpendiculaire à facing) : les machines d'un étage sont
      alignées le long de v ; les belts d'entrée/sortie longent la rangée (dir v).
  Les belts de TRANSITION entre étages sont logiques en S0 (connexion dans le graphe
  de flux, pas d'entité physique — S1 posera les belts de liaison).
  Conversion (u,v) -> (x,y) selon facing (rotation). Les machines sont posées non
  orientées (direction 0) car carrées en S0 (2x2, 3x3, 1x1) ; l'orientation des
  entités 2x1 (splitter, assembler orienté) = S1+.

Débit & logistique (régime permanent, spec §4.2) :
  belts_in_per_stage   = ceil(ing_rate_effectif       / belt_speed)   # belts parallèles (entrée)
  belts_out_per_stage  = ceil(rate_effective           / belt_speed)   # belts parallèles (sortie)
  inserters_in_pm      = ceil(debit_conso_machine       / inserter_tp)  # bras d'entrée par machine
  inserters_out_pm     = ceil(debit_prod_machine        / inserter_tp)  # bras de sortie par machine
Production jamais < demande (ceil), comme le solveur.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field, replace
from typing import Optional

from services.knowledge import (
    GeometryBase, EntityGeometry, THROUGHPUTS, inserter_throughput, pipe_throughput,
    FLUID_ITEMS,
)


# Directions Factorio : 0=N(-Y), 2=E(+X), 4=S(+Y), 6=W(-X). (Y croît vers le sud.)
# Repère local : u = sens cascade (facing), v = perpendiculaire (rangée, à gauche du flux).
# (ux,uy) = vecteur unitaire de u dans le repère map ; (vx,vy) = vecteur de v.
FACING_UNIT = {2: (1, 0), 4: (0, 1), 6: (-1, 0), 0: (0, -1)}
FACING_PERP = {2: (0, 1), 4: (-1, 0), 6: (0, -1), 0: (1, 0)}
FACING_DIR_U = {2: 2, 4: 4, 6: 6, 0: 0}     # direction int le long de +u (= facing)
FACING_DIR_V = {2: 4, 4: 6, 6: 0, 0: 2}     # direction int le long de +v


# ===== Entrées / sorties =====

@dataclass
class ResourcePatch:
    resource: str                                # "iron-ore" | "copper-ore" | "coal" | "stone"
    tiles: list[tuple[int, int]] = field(default_factory=list)
    bbox: tuple[int, int, int, int] = (0, 0, 0, 0)   # (x1, y1, x2, y2) en coords map


@dataclass
class Terrain:
    patches: list[ResourcePatch] = field(default_factory=list)
    obstacles: list[tuple[int, int, int, int]] = field(default_factory=list)  # bbox bloquants
    water: list[tuple[int, int, int, int]] = field(default_factory=list)
    surface_area: tuple[int, int, int, int] = (0, 0, 0, 0)
    # S4 : terrain étendu pour la détection per-entité + replan auto.
    # out_of_map = bbox frontière (artefact headless, scan_tiles_bbox retourne "out-of-map").
    # tile_grid = {(x,y): tile_name} peuplé par scan_tiles_bbox (précision tuile pour
    # water/out-of-map, vs bbox-vs-bbox imprécis). None = pas de grille (back-compat S3d).
    out_of_map: list[tuple[int, int, int, int]] = field(default_factory=list)
    tile_grid: Optional[dict] = None


@dataclass
class LayoutConstraints:
    """Tiers de logistique = ENTRÉE (arbitrage FactoryBuilder, comme machine_tiers du solveur).

    NB : inserters_per_machine et belts_per_stage NE SONT PAS ici — ils sont CALCULÉS
    par le LayoutPlanner à partir des débits (cf. StageLogistics).
    """
    belt_tier: str = "transport-belt"           # yellow/red/blue
    inserter_tier: str = "burner-inserter"      # burner/inserter/fast/long
    pole_tier: str = "small-electric-pole"      # small/medium/big/substation
    machine_gap: int = 1                        # tuiles de gap (inserter) entre machines
    stage_gap: int = 2                          # tuiles de gap entre étages
    # S1a : belts de transition physiques entre étages (alignement v + belts intermédiaires
    # si stage_gap grand). True par défaut ; S0 n'avait que des connexions logiques.
    transition_belts: bool = True
    # S1a : swing inserter de repli si la géométrie fine (pickup/drop) est absente.
    swing_distance: float = 2.0
    # S1b : nombre max d'ingrédients gérés par étage (empilement en u, long-handed au-delà).
    max_ingredients_per_stage: int = 2
    # S1c : main bus (layout alternatif, défaut off = bande manifold).
    bus_layout: bool = False
    bus_distance: int = 3
    # S2a : tiers fluide (pipes/pumps). pipe_tier pour les segments de connexion
    # machine->machine (direct pipe, chaîne 1->1) ; pump_tier pour les longues
    # distances (boost débit, S2b). Défauts "pipe"/"pump" (back-compat : étages
    # solides n'utilisent pas ces champs).
    pipe_tier: str = "pipe"
    pump_tier: str = "pump"
    # S3c : beacons + modules. beacons_per_stage = nombre de beacons posés côté +u le long
    # de v pour l'étage (0 = pas de beacons, back-compat S0/S1/S2). module_tier = module
    # inséré dans chaque beacon (ex. speed-module-3). modules_per_beacon = nb modules par
    # beacon (≤ module_slots du beacon, fixture=2). beacon_tier = nom du beacon.
    # CONSTAT S3b : supply_area_distance inaccessible runtime -> fixture supply_area=3.0
    # (GEOMETRY_FIXTURE). Placement : beacons à u_machine + offset_out_u + 1.0 + beacon_half_u
    # (juste au-delà du belt_out, edge-to-edge machine=2.5 < supply_area=3 -> couverture
    # garantie quelle que soit la taille machine, car offset_out_u = half_u+1.5). Poles
    # relocalisés au-delà des beacons (leur position back-compat collisionne avec le beacon).
    # Bonus agrégé calculé par FactoryBuilder via compute_module_effect (production_solver,
    # formule 2.0 FFF#409 dist*sqrt(n)*mod) puis injecté dans ProductionRequest.module_effects.
    beacon_tier: str = "beacon"
    module_tier: str = "speed-module-3"
    beacons_per_stage: int = 0
    modules_per_beacon: int = 2
    # S3d : beacons côté -u (double couverture "8 beacons" = 4 +u + 4 -u).
    # beacons_neg_per_stage = nombre de beacons posés côté -u (entrée ingrédients) le long de
    # v pour l'étage (0 = pas de beacon -u, back-compat S3c). Placement au MIROIR du +u :
    # u_beacon_neg_pos = u_machine - offset_out_u - 1.0 - beacon_half_u (edge-to-edge machine
    # 2.5 < supply_area 3 -> couverture garantie, symétrique +u). Gate collision : le candidat
    # -u est vérifié contre les entités existantes (belts_in/inserters de l'étage courant +
    # étages précédents) ; si collision (ex. étage 2+ ingrédients : belt ing1 à u_machine-4.5
    # chevauche le beacon u_machine-7..u_machine-4) -> skip + note beacon_neg_collision:<item>.
    # u_next étendu (réservation inter-étage) : cur_max_edge + 6.5 pour que le beacon -u de
    # l'étage suivant (bord -u = prev_u_next - 5.5) ne collisionne pas les +u beacons de l'étage
    # courant (stage_gap=2 insuffisant sinon). Back-compat : 0 -> aucun beacon -u, u_next S3c.
    beacons_neg_per_stage: int = 0
    # S4 : adaptation terrain (contournement + replan auto déterministe).
    # constructible_zone = bbox autorisée (coords map). None = pas de borne (back-compat S3d).
    # replan_budget = 0 = aucun replan auto (back-compat S3d) ; typique S4b = 4 (borné).
    # bypass_offset_v = shift anchor en v par essai (tuiles) ; bypass_max_offset_v = shift max
    #   cumulé avant de pivoter facing (garde-fou).
    # terrain_check = False = check post-hoc global S3d (inchangé) ; True = détection per-entité
    #   précise (passe avant le post-hoc, le post-hoc est alors skippé pour éviter les doublons).
    constructible_zone: Optional[tuple[int, int, int, int]] = None
    replan_budget: int = 0
    bypass_offset_v: int = 3
    bypass_max_offset_v: int = 12
    terrain_check: bool = False
    # S4b : offset perpendiculaire uniforme appliqué au 1er étage MACHINE (propage à toute la
    # cascade via v_out : av = v_out + half_v_next - 0.5). C'est le levier de contournement du
    # replan auto — NON l'anchor (pour les chaînes minières l'anchor est ignoré : les étages
    # suivent le patch.bbox, pas l'anchor). 0 = pas d'offset (back-compat S3d).
    cascade_offset_v: int = 0


@dataclass
class LayoutRequest:
    plan: object                                # ProductionPlan (du solveur)
    terrain: Terrain
    anchor: tuple[float, float]                 # point de raccordement au gisement (coords map)
    facing: int = 2                             # 0=N, 2=E, 4=S, 6=W
    constraints: LayoutConstraints = field(default_factory=LayoutConstraints)
    # S1a : fonction de débit inserter (DIP). Défaut = knowledge.inserter_throughput
    # (modèle affine, k=0 en S1a -> back-compat S0). Permet d'injecter une fonction
    # mesurée (S1d) ou un stub pour les tests.
    inserter_throughput_fn: object = field(default=inserter_throughput)
    # S2a : fonction de débit pipe (DIP). Défaut = knowledge.pipe_throughput (modèle
    # affine, k=0 en S2a -> débit constant, back-compat). Permet d'injecter une
    # fonction mesurée (S2b) ou un stub pour les tests.
    pipe_throughput_fn: object = field(default=pipe_throughput)


@dataclass
class LayoutEntity:
    name: str
    x: float
    y: float
    direction: int                              # 0=N, 2=E, 4=S, 6=W
    role: str                                   # "machine"|"belt"|"inserter"|"pole"|"drill"
                                                # S1b: +"splitter"|"merger" ; S1c: +"bus-belt"
                                                # S1f: +"under-in"|"under-out" (underground-belt)
                                                # S2a: +"pipe"|"pump"|"offshore-pump"|"pumpjack"|"bus-pipe"
                                                # S2b-1: +"storage-tank" (sink co-produit orphelin)
                                                # S2b-2: +"steam-engine" (sink power, steam orphelin)
                                                # S3c: +"beacon" (modules insérés, LayoutEntity.modules)
    node_item: str = ""                         # item produit (machine/drill) ou transporté (belt)
    in_port: tuple[float, float] = (0.0, 0.0)
    out_port: tuple[float, float] = (0.0, 0.0)
    # S2a : port fluide (offset u,v relatif au centre, pour connexion pipe machine).
    fluid_port: tuple[float, float] = (0.0, 0.0)
    # S1f : circuiterie main bus (underground crossings + tap/feed redesign).
    skip: bool = False                          # belt lane à RETIRER (libère la surface pour
                                                # splitter de tap/feed, transition +u, underground).
                                                # Le consommateur (mod/RCON) filtre not skip.
    ug_type: str = ""                           # underground-belt : "input"|"output" (create_entity
                                                # type=, validé live T1 2026-07-24).
    priority: str = ""                          # splitter_output_priority Factorio 2.0 :
                                                # "left"|"right"|"none" (STRING, validé live T2b).
                                                # Convention LayoutPlanner : +u = "left" (POV flux).
    # S3c : modules insérés dans un beacon (LayoutEntity role="beacon"). Liste de noms de
    # modules (ex. ["speed-module-3","speed-module-3"]). Le poseur RCON fait ent.insert après
    # create_entity. Vérifiable live via scan_factory -> get_module_inventory (S3b-7/11).
    modules: list = field(default_factory=list)


@dataclass
class StageLogistics:
    """Dimensionnement logistique d'un étage (sortie du CALCUL de débit, pour audit)."""
    item: str
    rate_effective: float                        # du solveur
    belts_in_per_stage: int                      # = ceil(ing_rate / belt_speed)
    belts_out_per_stage: int                     # = ceil(rate_effective / belt_speed)
    inserters_in_per_machine: int                # = ceil(debit_conso_machine / inserter_tp)
    inserters_out_per_machine: int              # = ceil(debit_prod_machine / inserter_tp)
    inserter_insufficient: bool = False          # inserters > slots machine (monter tier)
    belt_overflow: bool = False                  # belts > 4 (splitters S1)
    # S1a : swing inserter utilisé + débit effectif (affine, cf. inserter_throughput).
    swing_used: float = 0.0
    inserter_tp_effective: float = 0.0
    # S1b : dimensionnement par ingrédient (multi-ingrédients) + compte splitters/mergers.
    ingredients: dict = field(default_factory=dict)  # {ing_name: {belts_in, inserters_in_pm, inserter_name, swing, tp}}
    splitters: int = 0
    mergers: int = 0
    # S2a : phase fluide + pipes par étage. phase="solid"|"fluid"|"mixed".
    # pipes_in/out_per_stage = nombre de pipes de connexion (1 par port fluide utilisé).
    # Défauts "solid"/0 -> étages solides inchangés (back-compat).
    phase: str = "solid"
    pipes_in_per_stage: int = 0
    pipes_out_per_stage: int = 0


@dataclass
class LayoutPlan:
    request: LayoutRequest
    entities: list[LayoutEntity] = field(default_factory=list)
    connections: list[tuple[int, int, str]] = field(default_factory=list)  # (from_idx, to_idx, item)
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    stage_logistics: dict[str, StageLogistics] = field(default_factory=dict)
    totals: dict[str, int] = field(default_factory=dict)   # {entity_name: count} (étend total_machines)
    feasibility: str = "ok"
    notes: list[str] = field(default_factory=list)


# ===== Helpers géométriques =====

def _to_uv(facing: int, x: float, y: float) -> tuple[float, float]:
    """(x,y) map -> (u,v) local (u = facing, v = perpendiculaire). Base orthonormée."""
    ux, uy = FACING_UNIT[facing]
    vx, vy = FACING_PERP[facing]
    return (x * ux + y * uy, x * vx + y * vy)


def _to_xy(facing: int, u: float, v: float) -> tuple[float, float]:
    """(u,v) local -> (x,y) map."""
    ux, uy = FACING_UNIT[facing]
    vx, vy = FACING_PERP[facing]
    return (u * ux + v * vx, u * uy + v * vy)


def _depths(splan) -> dict[str, int]:
    """Profondeur topologique de chaque nœud (feuilles=0, cible=max). Pour l'ordre de placement."""
    by_item = {n.item: n for n in splan.nodes}
    depth: dict[str, int] = {}

    def d(item: str) -> int:
        if item in depth:
            return depth[item]
        n = by_item.get(item)
        if n is None or n.role == "mine" or not n.ingredients:
            depth[item] = 0
            return 0
        depth[item] = 1 + max((d(ing) for ing, _ in n.ingredients), default=0)
        return depth[item]

    for n in splan.nodes:
        d(n.item)
    return depth


def _drill_step(g: EntityGeometry) -> float:
    """Espacement entre drills pour couvrir le gisement (2× mining_radius, >= emprise)."""
    if g.mining_area:
        x1, y1, x2, y2 = g.mining_area
        radius = max((x2 - x1) / 2.0, (y2 - y1) / 2.0)
    else:
        radius = 0.0
    return float(max(g.w, g.h, math.ceil(2 * radius)))


def _swing_for(g_ins: Optional[EntityGeometry], default: float = 2.0) -> float:
    """Swing d'un inserter = pickup_distance + drop_distance (tuiles parcourues par
    demi-cycle). Défaut si la géométrie fine est absente (inserters inconnus du fixture)."""
    if g_ins is None:
        return default
    return g_ins.pickup_distance + g_ins.drop_distance


def _add(entities, name, x, y, direction, role, node_item="") -> int:
    entities.append(LayoutEntity(name, x, y, direction, role, node_item))
    return len(entities) - 1


# ===== Placement des foreuses (feuille mining) =====

def _place_drills(node, patch, geometry, constraints, facing, au, av,
                  entities, totals, stage_log, notes) -> Optional[tuple]:
    """Place les foreuses sur le patch réel + belt de collecte.

    Retourne (belt_in_first, belt_out_last, ing_name, out_item, u_next, v_next) ou None.
    Pour les drills : ing_name = out_item = la resource extraite.
    """
    g = geometry.geometry(node.machine)
    if g is None:
        notes.append(f"geometry manquante drill: {node.machine}")
        return None
    step = _drill_step(g)
    # patch bbox map -> local
    lu1, lv1 = _to_uv(facing, patch.bbox[0], patch.bbox[1])
    lu2, lv2 = _to_uv(facing, patch.bbox[2], patch.bbox[3])
    lu1, lu2 = min(lu1, lu2), max(lu1, lu2)
    lv1, lv2 = min(lv1, lv2), max(lv1, lv2)

    # Grille de drills sur le patch (local), à partir du coin (lu1, lv1).
    positions: list[tuple[float, float]] = []
    lv = lv1
    while lv + g.h <= lv2 + 0.01 and len(positions) < node.machine_count:
        lu = lu1
        while lu + g.w <= lu2 + 0.01 and len(positions) < node.machine_count:
            positions.append((lu + g.w / 2.0, lv + g.h / 2.0))
            lu += step
        lv += step
    if len(positions) < node.machine_count:
        notes.append(f"patch_trop_petit:{node.item} ({len(positions)}/{node.machine_count} drills dans le bbox)")
        # Prolonge la grille au-delà du bbox (layout débordant — S4 adaptera le terrain).
        lv = lv1
        while len(positions) < node.machine_count:
            lu = lu1
            while len(positions) < node.machine_count:
                positions.append((lu + g.w / 2.0, lv + g.h / 2.0))
                lu += step
            lv += step

    for (pu, pv) in positions:
        x, y = _to_xy(facing, pu, pv)
        _add(entities, node.machine, x, y, 0, "drill", node_item=node.item)
    totals[node.machine] = totals.get(node.machine, 0) + len(positions)

    # Belt de collecte : longe le bord aval du patch (u = lu2 + 0.5), direction +v,
    # longueur = (lv2 - lv1). belts_out parallèles si débit > 1 belt.
    belt = constraints.belt_tier
    belt_speed = THROUGHPUTS.get(belt, 0.0)
    belts_out = math.ceil(node.rate_effective / belt_speed) if belt_speed > 0 else 1
    n_seg = max(1, int(math.ceil(lv2 - lv1)))
    collect_u = lu2 + 0.5
    first_belt = None
    last_belt = None
    belt_out_end_idxs: list[int] = []
    for k in range(max(1, belts_out)):
        cu = collect_u + k
        lane_last = None
        for j in range(n_seg):
            cv = lv1 + 0.5 + j
            x, y = _to_xy(facing, cu, cv)
            idx = _add(entities, belt, x, y, FACING_DIR_V[facing], "belt", node_item=node.item)
            lane_last = idx
            if k == 0 and first_belt is None:
                first_belt = idx
        if lane_last is not None:
            belt_out_end_idxs.append(lane_last)
        last_belt = lane_last
    totals[belt] = totals.get(belt, 0) + max(1, belts_out) * n_seg

    stage_log[node.item] = StageLogistics(
        item=node.item, rate_effective=node.rate_effective,
        belts_in_per_stage=0, belts_out_per_stage=belts_out,
        inserters_in_per_machine=0, inserters_out_per_machine=0,
        belt_overflow=belts_out > 4,
    )

    u_next = collect_u + constraints.stage_gap
    v_next = (lv1 + lv2) / 2.0
    # S1b : belt_in_first par ingrédient (la mine a 1 "ingrédient" = la resource) +
    # belt_out_end_idxs (bouts des lanes de collecte, pour merger tree en queue).
    # S2a : 9e élément pipe_in_first_by_ing={} (feuille solide, pas de pipe in).
    return (first_belt, last_belt, node.item, node.item, u_next, v_next,
            {node.item: first_belt}, belt_out_end_idxs, {})


# ===== S2a : placement des pumpjacks / offshore-pump (feuilles fluides) =====

def _place_pumpjacks(node, patch, geometry, constraints, facing, au, av,
                     entities, totals, stage_log, notes) -> Optional[tuple]:
    """S2a : place les pumpjacks sur le patch crude-oil + pipe de sortie (direct pipe).

    Analogie _place_drills mais : 1 pumpjack 3x3 par emplacement (mining_area couvre le
    patch ; grille si machine_count>1), output = PIPE direct (1 pipe à pipe_ports[0],
    dir +u, pas d'inserter/belt). stage_log phase=fluid, pipes_out=1, belts_out=0,
    inserters=0. Retourne la même shape que _place_drills (belt_out_last = idx du 1er
    pipe de sortie, pour connexion aval via _place_pipe_segment). Chaîne 1->1 en S2a
    (pas de splitter/merger fluide ; machine_count couvre le débit).
    """
    g = geometry.geometry(node.machine)
    if g is None:
        notes.append(f"geometry manquante pumpjack: {node.machine}")
        return None
    # patch bbox map -> local
    lu1, lv1 = _to_uv(facing, patch.bbox[0], patch.bbox[1])
    lu2, lv2 = _to_uv(facing, patch.bbox[2], patch.bbox[3])
    lu1, lu2 = min(lu1, lu2), max(lu1, lu2)
    lv1, lv2 = min(lv1, lv2), max(lv1, lv2)

    # Grille de pumpjacks sur le patch (3x3 chacun), step = emprise (contigus).
    step = max(g.w, g.h)
    positions: list[tuple[float, float]] = []
    lv = lv1
    while lv + g.h <= lv2 + 0.01 and len(positions) < node.machine_count:
        lu = lu1
        while lu + g.w <= lu2 + 0.01 and len(positions) < node.machine_count:
            positions.append((lu + g.w / 2.0, lv + g.h / 2.0))
            lu += step
        lv += step
    # Prolonge au-delà du bbox si besoin (layout débordant — S4 adaptera le terrain).
    while len(positions) < node.machine_count:
        k = len(positions)
        positions.append((lu1 + g.w / 2.0 + (k % 3) * step,
                          lv1 + g.h / 2.0 + (k // 3) * step))

    pipe_name = constraints.pipe_tier
    dir_u = FACING_DIR_U[facing]
    # pipe_ports[0] = (du, dv, type) du port output (relatif au centre, repère u,v).
    # Le pipe de sortie se pose à (centre_u + du, centre_v + dv), direction +u.
    if g.pipe_ports:
        out_du, out_dv = g.pipe_ports[0][0], g.pipe_ports[0][1]
    else:
        out_du, out_dv = g.w // 2 + 1, 0
    pipe_out_idxs: list[int] = []
    first_pipe = None
    max_u_out = 0.0
    for (pu, pv) in positions:
        x, y = _to_xy(facing, pu, pv)
        _add(entities, node.machine, x, y, 0, "pumpjack", node_item=node.item)
        # Pipe de sortie à (pu + out_du, pv + out_dv), direction +u.
        px_u, px_v = pu + out_du, pv + out_dv
        px, py = _to_xy(facing, px_u, px_v)
        idx = _add(entities, pipe_name, px, py, dir_u, "pipe", node_item=node.item)
        pipe_out_idxs.append(idx)
        if first_pipe is None:
            first_pipe = idx
        if px_u > max_u_out:
            max_u_out = px_u
    totals[node.machine] = totals.get(node.machine, 0) + len(positions)
    totals[pipe_name] = totals.get(pipe_name, 0) + len(positions)

    stage_log[node.item] = StageLogistics(
        item=node.item, rate_effective=node.rate_effective,
        belts_in_per_stage=0, belts_out_per_stage=0,
        inserters_in_per_machine=0, inserters_out_per_machine=0,
        phase="fluid", pipes_in_per_stage=0, pipes_out_per_stage=1,
    )

    u_next = max_u_out + constraints.stage_gap
    v_next = (lv1 + lv2) / 2.0
    # belt_in_first=None (feuille, pas d'ingrédient) ; belt_out_last = 1er pipe.
    # S2a : 9e élément pipe_in_first_by_ing={} (feuille, pas d'ingrédient fluide in).
    return (None, first_pipe, node.item, node.item, u_next, v_next,
            {}, pipe_out_idxs, {})


def _place_offshore_pump(node, water_bbox, geometry, constraints, facing, au, av,
                         entities, totals, stage_log, notes) -> Optional[tuple]:
    """S2a : place 1 offshore-pump (2x1) sur une tuile d'eau au bord + pipe de sortie.

    Pas utile pour plastic-bar (basic-oil-processing n'a pas besoin d'eau) mais validé
    dans le socle fluide. L'offshore-pump se pose sur une tuile d'eau (bord du bbox
    water) ; son output pipe (pipe_ports[0]) alimente l'étage aval en +u.
    """
    g = geometry.geometry(node.machine)
    if g is None:
        notes.append(f"geometry manquante offshore-pump: {node.machine}")
        return None
    lu1, lv1 = _to_uv(facing, water_bbox[0], water_bbox[1])
    lu2, lv2 = _to_uv(facing, water_bbox[2], water_bbox[3])
    # Pose au coin du bbox water (tuile d'eau au bord).
    pu, pv = lu1 + g.w / 2.0, lv1 + g.h / 2.0
    x, y = _to_xy(facing, pu, pv)
    _add(entities, node.machine, x, y, 0, "offshore-pump", node_item=node.item)
    totals[node.machine] = totals.get(node.machine, 0) + 1
    # Pipe de sortie à (pu + out_du, pv + out_dv), direction +u.
    pipe_name = constraints.pipe_tier
    dir_u = FACING_DIR_U[facing]
    if g.pipe_ports:
        out_du, out_dv = g.pipe_ports[0][0], g.pipe_ports[0][1]
    else:
        out_du, out_dv = g.w // 2 + 1, 0
    px_u, px_v = pu + out_du, pv + out_dv
    px, py = _to_xy(facing, px_u, px_v)
    idx = _add(entities, pipe_name, px, py, dir_u, "pipe", node_item=node.item)
    totals[pipe_name] = totals.get(pipe_name, 0) + 1

    stage_log[node.item] = StageLogistics(
        item=node.item, rate_effective=node.rate_effective,
        belts_in_per_stage=0, belts_out_per_stage=0,
        inserters_in_per_machine=0, inserters_out_per_machine=0,
        phase="fluid", pipes_in_per_stage=0, pipes_out_per_stage=1,
    )
    u_next = px_u + constraints.stage_gap
    v_next = pv
    return (None, idx, node.item, node.item, u_next, v_next, {}, [idx], {})


def _place_fluid_sink(node, geometry, constraints, facing, au, av,
                      entities, totals, stage_log, notes) -> Optional[tuple]:
    """S2b-1/S2b-2 : place 1 sink fluide mono-input + 1 pipe input (puit co-produit orphelin).

    Sink role="store" (storage-tank, 3×3) ou role="power" (steam-engine, 3×5) : reçoit le
    co-produit orphelin via son pipe input (1er port input du GEOMETRY_FIXTURE). Pas de
    output (puit infini déterministe — décision utilisateur, pas de circuit/valve ; pour
    steam-engine le steam est consommé -> électricité). La connexion (port output co-produit
    de la source) -> sink input est posée par plan() via _place_pipe_segment(out_idx_by_item[cp],
    pipe_in). Générique : marche pour tout sink fluide dont la géométrie a au moins 1 port
    input. Back-compat : fonction NOUVELLE (S2b-1 _place_storage_tank, renommée S2b-2),
    n'affecte pas les étages solides/fluides mono-produit (S0/S1/S2a).
    """
    g = geometry.geometry(node.machine)
    if g is None:
        notes.append(f"geometry manquante sink fluide: {node.machine}")
        return None
    half_u = g.w / 2.0
    u_machine = au + half_u
    v0 = av
    x, y = _to_xy(facing, u_machine, v0)
    # S2b-2 : role sémantique selon le type de sink (storage-tank vs steam-engine).
    sink_role = "steam-engine" if node.role == "power" else "storage-tank"
    _add(entities, node.machine, x, y, 0, sink_role, node_item=node.item)
    totals[node.machine] = totals.get(node.machine, 0) + 1
    # Pipe input à un port input du hardcode (1er port input). dir +u (vers la source).
    pipe_name = constraints.pipe_tier
    in_ports = [p for p in (g.pipe_ports or []) if p[2] == "input"]
    if in_ports:
        in_du, in_dv = in_ports[0][0], in_ports[0][1]
    else:
        in_du, in_dv = -(g.w // 2 + 1), 0
    px_u, px_v = u_machine + in_du, v0 + in_dv
    px, py = _to_xy(facing, px_u, px_v)
    idx = _add(entities, pipe_name, px, py, FACING_DIR_U[facing], "pipe", node_item=node.item)
    totals[pipe_name] = totals.get(pipe_name, 0) + 1
    stage_log[node.item] = StageLogistics(
        item=node.item, rate_effective=node.rate_effective,
        belts_in_per_stage=0, belts_out_per_stage=0,
        inserters_in_per_machine=0, inserters_out_per_machine=0,
        phase="fluid", pipes_in_per_stage=1, pipes_out_per_stage=0,
    )
    u_next = u_machine + half_u + constraints.stage_gap
    v_next = v0
    # pipe_in_first_by_ing = {cp: idx} pour que plan() connecte out_idx_by_item[cp] -> idx.
    pipe_in_first_by_ing = {node.item: idx}
    return (None, None, node.item, node.item, u_next, v_next, {}, [], pipe_in_first_by_ing)


# ===== Placement d'un étage craft/smelt (bande manifold) =====

def _place_stage(node, geometry, constraints, belt_speed, inserter_tp, inserter_tp_fn,
                 facing, au, av, entities, totals, stage_log, notes,
                 coproduct_items: list[str] | None = None,
                 pipe_throughput_fn=pipe_throughput) -> Optional[tuple]:
    """Place un étage en bande (machines + belts in/out + inserters + poles).

    S1b : multi-ingrédients (empilement en u, long-handed pour l'ingrédient 1).
    Retourne (belt_in_first, belt_out_last, ing_name, out_item, u_next, v_next,
    belt_in_first_by_ing) ou None. belt_in_first_by_ing = {ing_name: idx du belt
    d'entrée k=0 de chaque ingrédient} (pour connecter chaque ingrédient à son
    producteur dans plan()).

    S2b-1 : `coproduct_items` = co-produits orphelins (recettes multi-produits type
    advanced-oil : heavy+light+petroleum, où light+petroleum non consommés -> sinks).
    Poser 1 pipe output par co-produit à un port output distinct du hardcode K7 (skip
    le 1er port = principal) + les enregistrer dans le 10e élément retourné
    `pipe_out_by_coproduct` ({cp: idx}) pour connexion vers storage-tank dans plan().
    Back-compat : coproduct_items vide/None -> pas de pipe co-produit (S0/S1/S2a inchangé).
    """
    g = geometry.geometry(node.machine)
    gbelt = geometry.geometry(constraints.belt_tier)
    gins = geometry.geometry(constraints.inserter_tier)
    gpole = geometry.geometry(constraints.pole_tier)
    if g is None or gbelt is None or gins is None:
        notes.append(f"geometry manquante étage: {node.machine}")
        return None

    N = node.machine_count
    size_u = g.w            # emprise machine sur l'axe cascade (u)
    size_v = g.h            # emprise machine sur l'axe rangée (v)
    half_u = size_u / 2.0
    half_v = size_v / 2.0
    gap = constraints.machine_gap

    # S1b : multi-ingrédients (empilement en u, côté -u). Ingrédient 0 = inserter_tier
    # (reach 1.0) ; ingrédient 1 = long-handed-inserter (reach 2.0, posé plus loin en
    # -u). > max_ingredients_per_stage -> note too_many_ingredients (non placé).
    ings = node.ingredients or []
    max_ing = constraints.max_ingredients_per_stage
    ingredients_log: dict = {}
    belt_in_first_by_ing: dict = {}
    belts_in_0 = 0
    ins_in_0 = 0
    swing_0 = _swing_for(gins, constraints.swing_distance)
    tp_0 = inserter_tp
    pipes_in_total = 0   # S2a : nombre de pipes d'entrée (1 par ingrédient fluide).
    for i, (ing_name, ing_rate) in enumerate(ings):
        if i >= max_ing:
            extras = [n for n, _ in ings[max_ing:]]
            notes.append(f"too_many_ingredients:{node.item} ({extras})")
            break
        # S2a : ingrédient fluide -> pipe in (pas de belt/inserter). Skip le
        # dimensionnement solide (belts_in_i=0, ins_in_i=0). belts_in_0 inchangé
        # si l'ingrédient 0 est fluide (étage fluide pur : pas de belts in).
        if ing_name in FLUID_ITEMS:
            ingredients_log[ing_name] = {
                "belts_in": 0, "inserters_in_per_machine": 0,
                "inserter_name": "", "swing": 0.0, "tp_effective": 0.0,
                "phase": "fluid", "pipes_in": 1,
            }
            pipes_in_total += 1
            continue
        ins_name = constraints.inserter_tier if i == 0 else "long-handed-inserter"
        g_ins_i = geometry.geometry(ins_name)
        swing_i = _swing_for(g_ins_i, constraints.swing_distance)
        tp_i = inserter_tp_fn(ins_name, swing_i)
        belts_in_i = math.ceil(ing_rate / belt_speed) if ing_rate > 0 and belt_speed > 0 else 0
        debit_conso_i = (ing_rate / N) if N > 0 else 0.0
        ins_in_i = math.ceil(debit_conso_i / tp_i) if debit_conso_i > 0 and tp_i > 0 else 0
        ingredients_log[ing_name] = {
            "belts_in": belts_in_i, "inserters_in_per_machine": ins_in_i,
            "inserter_name": ins_name, "swing": swing_i, "tp_effective": tp_i,
            "phase": "solid", "pipes_in": 0,
        }
        if i == 0:
            belts_in_0, ins_in_0, swing_0, tp_0 = belts_in_i, ins_in_i, swing_i, tp_i

    # S2b-3 : longueur du run de pipes/belts le long de v (dépend de N, size_v, gap).
    # Calculé ici (avant le dimensionnement) car n_lanes (pipes parallèles) en dépend.
    n_seg = max(1, int(math.ceil(N * (size_v + gap))))

    # --- Dimensionnement logistique (le CALCUL, comme le solveur) ---
    # S2a : produit fluide -> pipe out (pas de belt/inserter). Sinon solide (back-compat).
    # S2b-1 : recettes multi-produits -> 1 pipe output principal + 1 par co-produit
    # orphelin (vers storage-tank). Back-compat : coproduct_items vide -> pipes_out=1.
    # S2b-3 : pipes parallèles si le débit > capacité d'un pipe (viscosité fluide k≠0).
    # n_lanes = ceil(rate_effective / pipe_throughput(pipe_tier, n_seg, node.item)).
    # Back-compat : k_fluid=0 (water/steam) ou rate<=cap -> n_lanes=1 (S2a inchangé).
    out_fluid = node.item in FLUID_ITEMS
    n_coproducts = len(coproduct_items or [])
    if out_fluid:
        belts_out = 0
        ins_out = 0
        cap = pipe_throughput_fn(constraints.pipe_tier, float(n_seg), node.item) if pipe_throughput_fn else 0.0
        n_lanes = max(1, math.ceil(node.rate_effective / cap)) if cap > 0 else 1
        pipes_out = n_lanes + n_coproducts
    else:
        belts_out = math.ceil(node.rate_effective / belt_speed) if belt_speed > 0 else 1
        debit_prod_m = (node.rate_effective / N) if N > 0 else 0.0
        ins_out = math.ceil(debit_prod_m / inserter_tp) if debit_prod_m > 0 and inserter_tp > 0 else 0
        pipes_out = 0
        n_lanes = 0
    slots = max(1, int(size_v))   # slots inserter par côté de la machine
    insufficient = ins_in_0 > slots or ins_out > slots
    for il in ingredients_log.values():   # S1b : insufficient si un ingrédient > slots
        if il["inserters_in_per_machine"] > slots:
            insufficient = True
    overflow = belts_out > 4      # au-delà de 4 belts -> splitters (S1b)
    # S2a : phase de l'étage (solid/fluid/mixed) selon ingrédients + produit.
    has_fluid_ing = any(il.get("phase") == "fluid" for il in ingredients_log.values())
    if out_fluid and has_fluid_ing:
        stage_phase = "fluid"
    elif out_fluid or has_fluid_ing:
        stage_phase = "mixed"
    else:
        stage_phase = "solid"
    stage_log[node.item] = StageLogistics(
        item=node.item, rate_effective=node.rate_effective,
        belts_in_per_stage=belts_in_0, belts_out_per_stage=belts_out,
        inserters_in_per_machine=ins_in_0, inserters_out_per_machine=ins_out,
        inserter_insufficient=insufficient, belt_overflow=overflow,
        swing_used=swing_0, inserter_tp_effective=tp_0,
        ingredients=ingredients_log,
        phase=stage_phase, pipes_in_per_stage=pipes_in_total,
        pipes_out_per_stage=pipes_out,
    )
    if insufficient:
        notes.append(f"inserter_insufficient:{node.item} (in={ins_in_0} out={ins_out} slots={slots})")
    if overflow:
        notes.append(f"belt_overflow:{node.item} (belts_out={belts_out})")

    # --- Placement machines (rangée le long de v) ---
    u_machine = au + half_u
    v0 = av
    machine_v = [v0 + i * (size_v + gap) for i in range(N)]
    for vv in machine_v:
        x, y = _to_xy(facing, u_machine, vv)
        _add(entities, node.machine, x, y, 0, "machine", node_item=node.item)
    totals[node.machine] = totals.get(node.machine, 0) + N

    offset_out_u = half_u + 1.5    # belt_out à u_machine + offset_out_u

    # --- Belts + inserters d'ENTRÉE par ingrédient (empilement en u, côté -u) ---
    # Ingrédient i : inserters à iu_i = u_machine - half_u - 0.5 - i*1.5 (l'ingrédient
    # 1 plus loin en -u ; reach 2.0 du long-handed compense). Belts à bu_i = iu_i -
    # pickup_distance - k (pickup atteint la lane k=0). Back-compat : i=0 identique à
    # S0 (inserters à -0.5, belts à -1.5 - k).
    belt_in_first = None
    pipe_in_first_by_ing: dict = {}   # S2a : idx du premier pipe d'entrée fluide par ing
    for i, (ing_name, ing_rate) in enumerate(ings):
        if i >= max_ing:
            break
        il = ingredients_log[ing_name]
        # S2a : ingrédient fluide -> pipe de collecte input (pas de belt/inserter).
        # Pipe 1×1 le long de v à iu_i = u_machine - half_u - 0.5 (côté -u, port input
        # de la machine), dir +v. Junction auto Factorio (4 ports). Pas de splitter/merger.
        if il.get("phase") == "fluid":
            iu_i = u_machine - half_u - 0.5 - i * 1.5
            first_idx = None
            for j in range(n_seg):
                bv = v0 - half_v + 0.5 + j
                x, y = _to_xy(facing, iu_i, bv)
                idx = _add(entities, constraints.pipe_tier, x, y, FACING_DIR_V[facing],
                           "pipe", node_item=ing_name)
                if first_idx is None:
                    first_idx = idx
            if first_idx is not None:
                pipe_in_first_by_ing[ing_name] = first_idx
                if i == 0:
                    belt_in_first = first_idx   # point d'attache amont (pipe ou belt)
            totals[constraints.pipe_tier] = totals.get(constraints.pipe_tier, 0) + n_seg
            continue
        belts_in_i = max(1, il["belts_in"])
        ins_in_i = il["inserters_in_per_machine"]
        ins_name = il["inserter_name"]
        g_ins_i = geometry.geometry(ins_name)
        reach_i = g_ins_i.pickup_distance if g_ins_i else 1.0
        iu_i = u_machine - half_u - 0.5 - i * 1.5
        bu_i = iu_i - reach_i
        for k in range(belts_in_i):
            bu = bu_i - k
            for j in range(n_seg):
                bv = v0 - half_v + 0.5 + j
                x, y = _to_xy(facing, bu, bv)
                idx = _add(entities, constraints.belt_tier, x, y, FACING_DIR_V[facing],
                           "belt", node_item=ing_name)
                if k == 0 and ing_name not in belt_in_first_by_ing:
                    belt_in_first_by_ing[ing_name] = idx
                    if i == 0:
                        belt_in_first = idx
        totals[constraints.belt_tier] = totals.get(constraints.belt_tier, 0) + belts_in_i * n_seg
        for vv in machine_v:
            for s in range(min(ins_in_i, slots)):
                sv = vv - half_v + 0.5 + s
                x, y = _to_xy(facing, iu_i, sv)
                _add(entities, ins_name, x, y, FACING_DIR_U[facing], "inserter", node_item=ing_name)
        totals[ins_name] = totals.get(ins_name, 0) + N * min(ins_in_i, slots)

    # --- Sortie (produit) : S2a -> pipe si fluide, sinon belts+inserters (back-compat) ---
    belt_out_last = None
    belt_out_end_idxs: list[int] = []
    pipe_out_by_coproduct: dict[str, int] = {}   # S2b-1 : idx pipe output par co-produit
    sink_av_by_coproduct: dict[str, float] = {}  # S2d : av du sink par co-produit (aligné port)
    _u_next_min = 0.0   # S2d : push u_next au-delà de la dernière lane (pipe-bus parallèle)
    if out_fluid:
        # S2a/S2b-3 : pipe(s) de collecte output. Deux cas :
        #  - Mono-produit (S2a/steam, coproduct_items vide) OU multi-lane : lane continue
        #    le long de v à ou_i = u_machine + half_u + 0.5 (côté +u, port output
        #    principal), dir +v. n_lanes pipes parallèles si débit > capacité 1 pipe
        #    (viscosité k≠0, S2b-3). Back-compat : n_lanes=1 (water/steam, rate<=cap).
        #  - Multi-produit (fix K7, coproduct_items non vide et n_lanes=1) : séparer les
        #    3 outputs oil-refinery. Lane principale décalée en u=ou_i_base+2 (au-delà des
        #    ports à u=ou_i_base) ; stubs principal (u=ou_i_base, ou_i_base+1 au v du port
        #    principal) relient port (u=+2) -> lane (u=+5). Co-produits : 1 pipe par machine
        #    à u=ou_i_base (connecté au port u=+2), à v distincts (2 tuiles d'écart ->
        #    non adjacents entre eux ni aux stubs principal). Élimine les duplicatas
        #    intra-blueprint (lane u=ou_i_base+2 vs co-produits/stubs u=ou_i_base) + les
        #    adjacences cross-product (co-produits u=ou_i_base non adjacents à la lane
        #    u=ou_i_base+2, distance 2 ; stubs même fluide que la lane). Back-compat
        #    mono-produit inchangé. Séparation 100% du routing co-produit->storage-tank
        #    (éviter la lane) = S2c (underground crossings).
        ou_i_base = u_machine + half_u + 0.5
        out_port_dv = g.output_port_dv or {}   # fix K7 : produit -> offset v du port output
        if coproduct_items and n_lanes == 1:
            # --- S2d : pipe-bus fluide complet (lanes parallèles par produit) ---
            # 1 lane continue PAR produit (heavy=node.item + coproduct_items), parallèles en u
            # espacées de 2 tuiles (non adjacentes : adjacence = 1 tuile). Chaque lane collecte
            # les stubs de toutes les machines (via _place_pipe_bus_stub, crossings pipe-to-ground
            # multi-lanes) et court vers son sink aligné au v de son port. Élimine duplicatas
            # (lanes non adjacentes) + cross_adj résiduel (stubs isolés par souterrain, lanes
            # non adjacentes). Back-compat mono-produit (branche else) inchangé.
            g_out_ports = [p for p in (g.pipe_ports or []) if p[2] == "output"]
            p_dv = out_port_dv.get(node.item)
            if p_dv is None:
                p_dv = g_out_ports[0][1] if g_out_ports else 0
            # Produits = principal + co-produits, avec offset v de port (output_port_dv).
            cp_dvs: dict[str, float] = {}
            for k, cp in enumerate(coproduct_items):
                cp_dv = out_port_dv.get(cp)
                if cp_dv is None:
                    if k + 1 >= len(g_out_ports):
                        notes.append(f"too_many_coproducts:{node.item} (cp={cp} skip, ports épuisés)")
                        break
                    cp_dv = g_out_ports[k + 1][1]
                cp_dvs[cp] = cp_dv
            all_prods = [(node.item, p_dv)] + [(cp, cp_dvs[cp]) for cp in coproduct_items if cp in cp_dvs]
            # Lanes parallèles : lane k à ou_i_base + 2 + 2*k (espacées de 2, non adjacentes).
            lane_us = [ou_i_base + 2.0 + 2.0 * k for k in range(len(all_prods))]
            v_start = v0 - half_v + 0.5
            # Lane heavy (k=0) : pipe continu +v. belt_out_last = pipe au bout -v (v_start-2,
            # 2 tuiles sous la lane) via 2 connecteurs -> routing heavy->chem-plant sort en +u
            # à v=v_start-2, SOUS toutes les lanes co-produits (v_start-2 à v_start = 2 tuiles ->
            # non adjacent en v aux lanes qui démarrent à v_start) -> 0 crossing, 0 mélange.
            lane_heavy_first = None
            for j in range(n_seg):
                bv = v_start + j
                x, y = _to_xy(facing, lane_us[0], bv)
                idx = _add(entities, constraints.pipe_tier, x, y, FACING_DIR_V[facing],
                           "pipe", node_item=node.item)
                if j == 0:
                    lane_heavy_first = idx
            totals[constraints.pipe_tier] = totals.get(constraints.pipe_tier, 0) + n_seg
            # 2 connecteurs : lane heavy v_start -> v_start-2 (même fluide, junction +v/-v).
            belt_out_last = None
            for dv_below in (-1.0, -2.0):
                x, y = _to_xy(facing, lane_us[0], v_start + dv_below)
                idx = _add(entities, constraints.pipe_tier, x, y, FACING_DIR_V[facing],
                           "pipe", node_item=node.item)
                if dv_below == -2.0:
                    belt_out_last = idx
            if belt_out_last is not None:
                belt_out_end_idxs.append(belt_out_last)
            totals[constraints.pipe_tier] = totals.get(constraints.pipe_tier, 0) + 2
            # Lanes co-produits (k>=1) : pipe continu +v, idx au v du port de la dernière
            # machine (port_cp_last) -> pipe_out_by_coproduct[cp] (routing cp->sink aligné).
            for k, (cp, dv) in enumerate(all_prods[1:], start=1):
                j_cp_last = int(round((machine_v[-1] + dv) - v_start))
                cp_last_idx = None
                for j in range(n_seg):
                    bv = v_start + j
                    x, y = _to_xy(facing, lane_us[k], bv)
                    idx = _add(entities, constraints.pipe_tier, x, y, FACING_DIR_V[facing],
                               "pipe", node_item=cp)
                    if j == j_cp_last:
                        cp_last_idx = idx
                if cp_last_idx is not None:
                    pipe_out_by_coproduct[cp] = cp_last_idx
                totals[constraints.pipe_tier] = totals.get(constraints.pipe_tier, 0) + n_seg
                # Sink aligné au v du port du co-produit (input pipe à v=av_sink -> routing
                # cp->sink constant v, +u, propre). Retourné dans le 11e élément pour que
                # plan() place le sink au bon av (fallback av si absent -> mono-produit).
                sink_av_by_coproduct[cp] = machine_v[-1] + dv
            # Stubs par machine/produit : port (ou_i_base, vv+dv) -> lane (lane_us[k], vv+dv),
            # traversant les lanes intermédiaires (0..k-1) via une paire pipe-to-ground unique
            # (helper _place_pipe_bus_stub). heavy=0 crossing, light=1 paire (lane heavy),
            # petroleum=1 paire multi-lanes (heavy+light, distance 4 <= PIPE_UNDERGROUND_MAX).
            for vv in machine_v:
                for k, (prod, dv) in enumerate(all_prods):
                    v_port = vv + dv
                    intermediate = [(lane_us[m], all_prods[m][0]) for m in range(k)]
                    _place_pipe_bus_stub(entities, totals, constraints.pipe_tier, prod, facing,
                                         ou_i_base, lane_us[k], v_port, intermediate, notes)
            # u_next poussé au-delà de la dernière lane (sinks + étage suivant clairent le bus).
            _u_next_min = lane_us[-1] + constraints.stage_gap
        else:
            # --- Mono-produit (S2a/back-compat) OU multi-lane : lane à ou_i_base (comportement
            # inchangé). Co-produits éventuels (multi-lane multi-produit, edge case rare) à
            # ou_i_base sur ports distincts du K7 (CONSTAT : duplicatas/mélange possibles,
            # cas non couvert par le fix K7 n_lanes=1 ; séparation 100% = S2c).
            lane_last = None
            for lane in range(n_lanes):
                ou_i = ou_i_base + lane * 1.0
                for j in range(n_seg):
                    bv = v0 - half_v + 0.5 + j
                    x, y = _to_xy(facing, ou_i, bv)
                    idx = _add(entities, constraints.pipe_tier, x, y, FACING_DIR_V[facing],
                               "pipe", node_item=node.item)
                    lane_last = idx
            if lane_last is not None:
                belt_out_end_idxs.append(lane_last)
            belt_out_last = lane_last
            totals[constraints.pipe_tier] = totals.get(constraints.pipe_tier, 0) + n_lanes * n_seg
            if coproduct_items:
                g_out_ports = [p for p in (g.pipe_ports or []) if p[2] == "output"]
                for k, cp in enumerate(coproduct_items):
                    cp_dv = out_port_dv.get(cp)
                    if cp_dv is None:
                        if k + 1 >= len(g_out_ports):
                            notes.append(f"too_many_coproducts:{node.item} (cp={cp} skip, ports épuisés)")
                            break
                        cp_dv = g_out_ports[k + 1][1]
                    cpu = u_machine + 3   # port_du=3 (K7 output u=+3)
                    cpv = v0 + cp_dv
                    x, y = _to_xy(facing, cpu, cpv)
                    idx = _add(entities, constraints.pipe_tier, x, y, FACING_DIR_V[facing],
                               "pipe", node_item=cp)
                    pipe_out_by_coproduct[cp] = idx
                    totals[constraints.pipe_tier] = totals.get(constraints.pipe_tier, 0) + 1
    else:
        # --- Belts sortie (produit) : belts_out lignes, longe v, dir +v ---
        # S1b : on collecte belt_out_end_idxs (bout v_end de chaque lane k) pour que
        # plan() pose un merger tree en queue si belts_out > 1.
        for k in range(max(1, belts_out)):
            bu = u_machine + offset_out_u + k
            lane_last = None
            for j in range(n_seg):
                bv = v0 - half_v + 0.5 + j
                x, y = _to_xy(facing, bu, bv)
                idx = _add(entities, constraints.belt_tier, x, y, FACING_DIR_V[facing], "belt", node_item=node.item)
                lane_last = idx
            if lane_last is not None:
                belt_out_end_idxs.append(lane_last)
            belt_out_last = lane_last
        totals[constraints.belt_tier] = totals.get(constraints.belt_tier, 0) + max(1, belts_out) * n_seg

        # --- Inserters sortie : ins_out par machine (limité slots), direction +u ---
        for vv in machine_v:
            for s in range(min(ins_out, slots)):
                sv = vv - half_v + 0.5 + s
                iu = u_machine + half_u + 0.5
                x, y = _to_xy(facing, iu, sv)
                _add(entities, constraints.inserter_tier, x, y, FACING_DIR_U[facing], "inserter", node_item=node.item)
        totals[constraints.inserter_tier] = totals.get(constraints.inserter_tier, 0) + N * min(ins_out, slots)

    # --- S3c : beacons (côté +u, couvrent les machines). Calcul AVANT les poles car les
    # poles sont relocalisés au-delà des beacons quand ceux-ci sont actifs (leur position
    # back-compat u_machine+offset_out_u+2.0 collisionne avec le beacon). ---
    gbeacon = geometry.geometry(constraints.beacon_tier) if (constraints.beacons_per_stage > 0 or constraints.beacons_neg_per_stage > 0) else None
    beacon_active = (gbeacon is not None and N > 0 and not out_fluid and constraints.beacons_per_stage > 0)
    beacon_neg_active = (gbeacon is not None and N > 0 and not out_fluid and constraints.beacons_neg_per_stage > 0)
    u_beacon_pos = 0.0
    beacon_half_u = gbeacon.w / 2.0 if gbeacon is not None else 0.0
    if beacon_active:
        # Beacon juste au-delà du belt_out : u_machine + offset_out_u + 1.0 + beacon_half_u.
        # Edge-to-edge machine-beacon = (offset_out_u+1.0+beacon_half_u) - half_u - beacon_half_u
        # = offset_out_u + 1.0 - half_u = (half_u+1.5) + 1.0 - half_u = 2.5 < supply_area(3.0)
        # -> couverture garantie quelle que soit la taille machine (marge 0.5).
        u_beacon_pos = u_machine + offset_out_u + 1.0 + beacon_half_u

    # --- Poles : un tous les K machines (couverture supply_area) + un en fin ---
    n_poles = 0
    if gpole is not None and N > 0:
        supply = gpole.supply_area
        if supply > 0:
            K = max(1, int(math.ceil((supply * 2.0) / (size_v + gap))))
        else:
            K = max(1, N)
        # S3c : poles relocalisés au-delà des beacons quand actifs (sinon back-compat).
        if beacon_active:
            pole_half_u = gpole.w / 2.0
            pole_u = u_beacon_pos + beacon_half_u + 1.0 + pole_half_u
        else:
            pole_u = u_machine + offset_out_u + 2.0
        placed_idx = set()
        for i in range(0, N, K):
            vv = v0 + i * (size_v + gap)
            x, y = _to_xy(facing, pole_u, vv)
            _add(entities, constraints.pole_tier, x, y, 0, "pole")
            n_poles += 1
            placed_idx.add(i)
        if (N - 1) not in placed_idx:
            vv = v0 + (N - 1) * (size_v + gap)
            x, y = _to_xy(facing, pole_u, vv)
            _add(entities, constraints.pole_tier, x, y, 0, "pole")
            n_poles += 1
    totals[constraints.pole_tier] = totals.get(constraints.pole_tier, 0) + n_poles

    # --- S3c : placement beacons (rangée +u, le long de v, modules insérés) ---
    # beacons_per_stage = nombre exact de beacons à poser (FactoryBuilder calcule depuis la
    # densité voulue ; pour couvrir N machines : ceil(row_length/(2*supply_area))). Posés
    # uniformément le long de v de v0-half_v+0.5 (1er machine) à v0+(N-1)*(size_v+gap)+half_v
    # (dernière). Chaque beacon reçoit modules_per_beacon × module_tier (LayoutEntity.modules).
    n_beacons = 0
    if beacon_active:
        n_beacons = max(1, int(constraints.beacons_per_stage))
        row_first = v0 - half_v + 0.5
        row_last = v0 + (N - 1) * (size_v + gap) - half_v + 0.5
        for j in range(n_beacons):
            bv = row_first + (row_last - row_first) * (j / max(1, n_beacons - 1)) if n_beacons > 1 else v0
            x, y = _to_xy(facing, u_beacon_pos, bv)
            idx = _add(entities, constraints.beacon_tier, x, y, 0, "beacon", node_item=node.item)
            entities[idx].modules = [constraints.module_tier] * constraints.modules_per_beacon
        totals[constraints.beacon_tier] = totals.get(constraints.beacon_tier, 0) + n_beacons

    # --- S3d : placement beacons côté -u (double couverture "8 beacons") ---
    # Miroir du +u : u_beacon_neg_pos = u_machine - offset_out_u - 1.0 - beacon_half_u
    # (edge-to-edge machine 2.5 < supply_area 3 -> couverture garantie, symétrique +u).
    # Gate collision : le candidat -u est vérifié contre les entités existantes (belts_in/
    # inserters de l'étage courant + étages précédents). Si collision (ex. étage 2+ ings :
    # belt ing1 à u_machine-4.5 chevauche le beacon u_machine-7..u_machine-4) -> skip toute
    # la rangée -u + note beacon_neg_collision:<item>. Back-compat : beacons_neg_per_stage=0
    # -> beacon_neg_active=False -> aucun beacon -u, aucune vérification.
    n_beacons_neg = 0
    if beacon_neg_active:
        beacon_half_v = gbeacon.h / 2.0
        u_beacon_neg_pos = u_machine - offset_out_u - 1.0 - beacon_half_u
        row_first_neg = v0 - half_v + 0.5
        row_last_neg = v0 + (N - 1) * (size_v + gap) - half_v + 0.5
        n_beacons_neg = max(1, int(constraints.beacons_neg_per_stage))
        # Vérifie chaque candidat -u contre toutes les entités existantes (bounding-box en
        # coords u,v : overlap si edge-to-edge u ET v < 0). Skip 1er collision -> note unique.
        neg_collides = False
        for j in range(n_beacons_neg):
            bv = row_first_neg + (row_last_neg - row_first_neg) * (j / max(1, n_beacons_neg - 1)) if n_beacons_neg > 1 else v0
            for e in entities:
                if e.skip:
                    continue
                ge = geometry.geometry(e.name)
                if ge is None:
                    continue
                eu, ev = _to_uv(facing, e.x, e.y)
                du = abs(eu - u_beacon_neg_pos) - ge.w / 2.0 - beacon_half_u
                dv = abs(ev - bv) - ge.h / 2.0 - beacon_half_v
                if du < -0.01 and dv < -0.01:   # chevauchement sur les 2 axes = collision
                    neg_collides = True
                    break
            if neg_collides:
                break
        if neg_collides:
            notes.append(f"beacon_neg_collision:{node.item}")
            n_beacons_neg = 0
        else:
            for j in range(n_beacons_neg):
                bv = row_first_neg + (row_last_neg - row_first_neg) * (j / max(1, n_beacons_neg - 1)) if n_beacons_neg > 1 else v0
                x, y = _to_xy(facing, u_beacon_neg_pos, bv)
                idx = _add(entities, constraints.beacon_tier, x, y, 0, "beacon", node_item=node.item)
                entities[idx].modules = [constraints.module_tier] * constraints.modules_per_beacon
            totals[constraints.beacon_tier] = totals.get(constraints.beacon_tier, 0) + n_beacons_neg

    # u_next : cleared au-delà du +u le plus avancé (machine edge, ou beacons, ou poles
    # relocalisés). Back-compat : beacons_per_stage=0 -> u_next inchangé S2.
    u_next = max(u_machine + offset_out_u + constraints.stage_gap, _u_next_min)
    if beacon_active:
        u_next = max(u_next, u_beacon_pos + beacon_half_u + constraints.stage_gap)
    # S3d : réservation inter-étage. Quand beacons_neg_per_stage>0, l'étage courant réserve
    # la place pour le beacon -u de l'étage suivant (bord -u = prev_u_next - 5.5) afin qu'il
    # ne collisionne pas les +u beacons / belt_out de l'étage courant (stage_gap=2 insuffisant
    # sinon). cur_max_edge = bord +u le plus avancé (beacon +u si actif, sinon belt_out).
    # Back-compat : beacons_neg_per_stage=0 -> pas d'extension -> u_next S3c/S2 inchangé.
    if constraints.beacons_neg_per_stage > 0:
        cur_max_edge = (u_beacon_pos + beacon_half_u) if beacon_active else (u_machine + offset_out_u + 0.5)
        u_next = max(u_next, cur_max_edge + 6.5)
    v_next = v0
    ing_name_0 = ings[0][0] if ings else ""
    # S2b-1 : 10e élément pipe_out_by_coproduct (idx pipe output par co-produit orphelin).
    # S2d : 11e élément sink_av_by_coproduct (av du sink par cp, aligné au port).
    return (belt_in_first, belt_out_last, ing_name_0, node.item, u_next, v_next,
            belt_in_first_by_ing, belt_out_end_idxs, pipe_in_first_by_ing,
            pipe_out_by_coproduct, sink_av_by_coproduct)


# ===== bbox =====

def _bbox_of(entities) -> tuple[float, float, float, float]:
    if not entities:
        return (0.0, 0.0, 0.0, 0.0)
    xs = [e.x for e in entities]
    ys = [e.y for e in entities]
    return (min(xs), min(ys), max(xs), max(ys))


def _bbox_intersects(a, b) -> bool:
    return not (a[2] < b[0] or a[0] > b[2] or a[3] < b[1] or a[1] > b[3])


def _place_pipe_segment(from_idx: int, to_idx: int, facing: int,
                        entities: list, totals: dict, pipe_name: str,
                        item: str, notes: list) -> None:
    """S2a : pose des pipes 1×1 reliant la sortie fluide d'un étage à l'entrée fluide
    du suivant. Analogie _place_transition mais pour fluides.

    Pipes 1×1 à 4 ports : junction automatique Factorio (pas de splitter/merger, pas
    de direction de flux comme les belts). Chaîne 1->1 en S2a. Les étages fluides sont
    alignés en v (comme les solides) : pipe_out_last(étage n) et pipe_in_first(étage n+1)
    tombent au même v. En u, l'écart = stage_gap - 1.5 ; pour stage_gap=2, delta_u=0.5 ->
    ADJACENCE (0 pipe intermédiaire, Factorio connecte les pipes adjacentes). Pour
    stage_gap>2, on pose ceil(delta_u-1) pipes intermédiaires le long de u. Segment v si
    désalignement résiduel. Anti-collision : si occupé, le pipe est skip (note).
    """
    e_from = entities[from_idx]
    e_to = entities[to_idx]
    uf, vf = _to_uv(facing, e_from.x, e_from.y)   # pipe_out_last (sortie étage n)
    ut, vt = _to_uv(facing, e_to.x, e_to.y)       # pipe_in_first (entrée étage n+1)
    du = ut - uf
    dv = vt - vf

    def _occupied(x: float, y: float) -> bool:
        return any(abs(e.x - x) < 0.4 and abs(e.y - y) < 0.4 for e in entities)

    # Segment u : pipes intermédiaires le long de u (si écart > adjacence), au v aligné.
    # S2c : si le segment traverse une lane fluide +v d'un AUTRE item (crossing), le routing
    # plonge sous la lane via une paire pipe-to-ground INPUT/OUTPUT (la lane reste intacte) au
    # lieu d'être skip (trou + cross_adj résiduel). Détection : pipe occupant la tuile, node_item
    # différent, orienté +v (FACING_DIR_V), pipe normal (ug_type vide). Back-compat : mono-produit
    # (pas de lane d'un autre item) -> is_lane toujours False -> comportement S2a inchangé.
    pending_output = False   # S2c : si True, l'itération courante skip (OUTPUT déjà posé par _pipe_under_crossing à l'itération précédente)
    if abs(du) > 1.0:
        step = 1.0 if du > 0 else -1.0
        n_u = int(math.ceil(abs(du) - 1.0))
        for i in range(1, n_u + 1):
            cu = uf + step * i
            x, y = _to_xy(facing, cu, vf)
            # S2c : si l'itération précédente a posé un OUTPUT à cette position, skip la pose normale.
            if pending_output:
                pending_output = False
                continue
            if _occupied(x, y):
                # S2c : détecte un crossing de lane fluide (pipe d'un autre item, orienté +v).
                idx_occ = _find_at(entities, x, y)
                is_lane = (idx_occ is not None
                           and entities[idx_occ].name == pipe_name
                           and entities[idx_occ].role == "pipe"
                           and entities[idx_occ].ug_type == ""
                           and entities[idx_occ].node_item
                           and entities[idx_occ].node_item != item
                           and entities[idx_occ].direction == FACING_DIR_V[facing])
                if is_lane:
                    # S2c garde no_room : ne pas poser l'OUTPUT s'il dépasserait le sink
                    # (ut < u_cross+step = sink trop proche de la lane, ex. 1er sink 3x3 juste
                    # après le bus). Sinon l'OUTPUT est orphelin (dead end) et le segment v est
                    # déconnecté. Fallback skip (trou à la lane) = CONSTAT sink-proche.
                    out_u = cu + step
                    has_room = (out_u - ut) * step <= 0   # out_u <= ut si +u, >= ut si -u
                    if has_room:
                        ok = _pipe_under_crossing(entities, totals, pipe_name, item, facing,
                                                  cu, vf, entities[idx_occ].node_item, notes)
                        if ok:
                            pending_output = True   # itération i+1 : (cu+step, vf) déjà posé en OUTPUT
                            continue
                    else:
                        notes.append(f"pipe_under_crossing_no_room:{item} @(u={cu:.1f},v={vf:.1f}) "
                                     f"sink trop proche (ut={ut:.1f} < out_u={out_u:.1f})")
                # Fallback comportement S2a (skip + note) : autre collision (water, machine,
                # stub, routing d'un autre co-produit), crossing non résolu (amont absent), ou
                # sink trop proche (no_room). CONSTAT routing<->routing = S2d pipe-bus.
                notes.append(f"pipe_collision_S2a:{item} @({x:.1f},{y:.1f})")
                continue
            _add(entities, pipe_name, x, y, 0, "pipe", node_item=item)
            totals[pipe_name] = totals.get(pipe_name, 0) + 1
    # Segment v : pipes le long de v si désalignement résiduel (anormal avec alignement).
    if abs(dv) > 0.1:
        step = 1.0 if dv > 0 else -1.0
        n_v = int(math.ceil(abs(dv)))
        # partir de uf+du (coté entrée) si segment u posé, sinon uf.
        cu = ut if abs(du) > 1.0 else uf
        for i in range(1, n_v + 1):
            cv = vf + step * i
            x, y = _to_xy(facing, cu, cv)
            if _occupied(x, y):
                notes.append(f"pipe_collision_S2a:{item} @({x:.1f},{y:.1f})")
                continue
            _add(entities, pipe_name, x, y, 0, "pipe", node_item=item)
            totals[pipe_name] = totals.get(pipe_name, 0) + 1


def _pipe_under_crossing(entities, totals, pipe_name, item, facing,
                         u_cross, v_cross, lane_node_item, notes) -> bool:
    """S2c : libère la surface à (u_cross, v_cross) pour que le routing +u traverse une lane
    fluide +v (lane_node_item) sans la toucher. INVERSE de _under_crossing (S1f belts) : ici le
    routing plonge, la lane reste. Mute in-place le pipe routing à (u_cross-1, v_cross) [déjà
    posé pipe normal] en pipe-to-ground INPUT direction +u, et pose le pipe routing à
    (u_cross+1, v_cross) comme pipe-to-ground OUTPUT direction +u. (u_cross, v_cross) = lane,
    INTACT (pipe normal 4 ports). Le souterrain (canal séparé, distance 2 <= PIPE_UNDERGROUND_MAX
    =10) traverse la lane sans junction ; le port surface du INPUT pointe vers l'amont (ouest),
    PAS vers la lane (est) -> pas de junction surface.

    Retourne True si la paire a été posée. Retourne False si l'amont (u_cross-1, v_cross) est
    absent ou n'est pas un pipe normal du routing courant (edge case i==1 : routing arrive
    directement sur la lane, pas de tuile amont posée) -> l'appelant fallback vers skip + note
    pipe_collision_S2a (comportement S2a, trou).

    Portée S2c : n_lanes=1 géré (cas solid-fuel). Multi-lane (n_lanes>1, plusieurs lanes heavy
    côte à côte en u) NON couvert : le OUTPUT du 1er crossing est adjacent (port surface est) à
    la 2e lane -> cross_adj résiduel (CONSTAT S2d futur)."""
    dir_u = FACING_DIR_U[facing]
    x_prev, y_prev = _to_xy(facing, u_cross - 1.0, v_cross)
    x_next, y_next = _to_xy(facing, u_cross + 1.0, v_cross)
    idx_prev = _find_at(entities, x_prev, y_prev)
    if (idx_prev is None
            or entities[idx_prev].name != pipe_name
            or entities[idx_prev].role != "pipe"
            or entities[idx_prev].ug_type != ""
            or entities[idx_prev].node_item != item):
        notes.append(f"pipe_under_crossing_no_input:{item} @(u={u_cross - 1},v={v_cross}) "
                     f"amont absent/non-pipe-normal (i==1 ou routing arrive direct sur lane)")
        return False
    # 1. Mute amont (pipe normal routing) en pipe-to-ground INPUT direction +u.
    e = entities[idx_prev]
    e.name = PIPE_TO_GROUND_NAME
    e.role = "pipe"          # inchangé (garde la détection _detect_separation role=="pipe")
    e.ug_type = "input"
    e.direction = dir_u
    # 2. Pose aval comme pipe-to-ground OUTPUT direction +u (pas de check _occupied, comme
    #    _under_crossing ; la tuile existe dans le plan, idx stable via _add).
    idx_out = _add(entities, PIPE_TO_GROUND_NAME, x_next, y_next, dir_u, "pipe",
                   node_item=item)
    entities[idx_out].ug_type = "output"   # _add ne passe pas ug_type -> set après (mutation in-place)
    # 3. Totals : 1 pipe normal retiré (-1), 2 pipe-to-ground ajoutés (+2).
    totals[pipe_name] = totals.get(pipe_name, 0) - 1
    totals[PIPE_TO_GROUND_NAME] = totals.get(PIPE_TO_GROUND_NAME, 0) + 2
    notes.append(f"pipe_under_crossing_S2c:{item} @(u={u_cross},v={v_cross}) "
                 f"lane={lane_node_item} paire input/output (routing plonge, lane intacte)")
    return True


def _place_pipe_bus_stub(entities, totals, pipe_name, item, facing,
                         ou_i_base, lane_u, v_port, intermediate_lanes, notes) -> None:
    """S2d : pose le stub d'un produit reliant le port machine (ou_i_base, v_port) à sa lane
    (lane_u, v_port), traversant les lanes intermédiaires via une PAIRE pipe-to-ground unique
    (INPUT juste avant la 1re lane traversée, OUTPUT juste après la dernière). Le souterrain
    (distance = last_lane_u - first_lane_u + 2 <= PIPE_UNDERGROUND_MAX=10) traverse toutes les
    lanes intermédiaires sans junction surface : le port surface du INPUT pointe vers l'amont
    (ouest, -u, vers le port machine), PAS vers les lanes (est) -> pas de mélange (cf _pipe_under_crossing
    S2c, même sémantique de direction dir_u). Les lanes traversées restent INTACTES (pipe normal
    4 ports, surface non touchée).

    intermediate_lanes = liste triée (u_lane, lane_node_item) à traverser (u croissant).
    - Vide (heavy, k=0) : stub direct, pipes normaux ou_i_base+1..lane_u-1 (0 crossing).
    - 1 lane (light) : 1 paire pipe-to-ground (INPUT à first-1, OUTPUT à first+1=last+1).
    - 2 lanes (petroleum) : 1 paire pipe-to-ground multi-lanes (INPUT à first-1, OUTPUT à last+1,
      distance 4), traverse heavy+light d'un coup.

    Back-compat : signature additive, appelé uniquement par la branche S2d (coproduct_items +
    n_lanes=1). Signatures _add/_pipe_under_crossing inchangées."""
    dir_u = FACING_DIR_U[facing]
    dir_v = FACING_DIR_V[facing]
    # 1. Pipe au port (ou_i_base, v_port) - connexion au port machine (adjacence u=+0.5).
    x, y = _to_xy(facing, ou_i_base, v_port)
    _add(entities, pipe_name, x, y, dir_v, "pipe", node_item=item)
    totals[pipe_name] = totals.get(pipe_name, 0) + 1
    if not intermediate_lanes:
        # Stub direct (heavy, 0 crossing) : pipes normaux ou_i_base+1 .. lane_u-1.
        u = ou_i_base + 1.0
        while u < lane_u:
            x, y = _to_xy(facing, u, v_port)
            _add(entities, pipe_name, x, y, dir_v, "pipe", node_item=item)
            totals[pipe_name] = totals.get(pipe_name, 0) + 1
            u += 1.0
        return
    first_lane_u = intermediate_lanes[0][0]
    last_lane_u = intermediate_lanes[-1][0]
    # 2. Pipes normaux ou_i_base+1 .. first_lane_u-1 (avant la 1re lane traversée).
    u = ou_i_base + 1.0
    while u < first_lane_u:
        x, y = _to_xy(facing, u, v_port)
        _add(entities, pipe_name, x, y, dir_v, "pipe", node_item=item)
        totals[pipe_name] = totals.get(pipe_name, 0) + 1
        u += 1.0
    # 3. Crossing multi-lanes : mute (first_lane_u-1) en INPUT, pose OUTPUT à (last_lane_u+1).
    x_in, y_in = _to_xy(facing, first_lane_u - 1.0, v_port)
    idx_in = _find_at(entities, x_in, y_in)
    if (idx_in is None or entities[idx_in].name != pipe_name
            or entities[idx_in].node_item != item or entities[idx_in].ug_type != ""):
        notes.append(f"pipe_bus_stub_no_input:{item} @(u={first_lane_u - 1},v={v_port}) "
                     f"amont absent/non-pipe-normal")
        return
    e_in = entities[idx_in]
    e_in.name = PIPE_TO_GROUND_NAME
    e_in.ug_type = "input"
    e_in.direction = dir_u
    x_out, y_out = _to_xy(facing, last_lane_u + 1.0, v_port)
    idx_out = _add(entities, PIPE_TO_GROUND_NAME, x_out, y_out, dir_u, "pipe", node_item=item)
    entities[idx_out].ug_type = "output"
    totals[pipe_name] = totals.get(pipe_name, 0) - 1
    totals[PIPE_TO_GROUND_NAME] = totals.get(PIPE_TO_GROUND_NAME, 0) + 2
    lane_items = ",".join(li for _, li in intermediate_lanes)
    notes.append(f"pipe_bus_stub_S2d:{item} @(u={first_lane_u - 1}->{last_lane_u + 1},v={v_port}) "
                 f"cross={lane_items} (stub plonge, lanes intactes)")
    # 4. Pipes normaux last_lane_u+2 .. lane_u-1 (après la dernière lane traversée).
    u = last_lane_u + 2.0
    while u < lane_u:
        x, y = _to_xy(facing, u, v_port)
        _add(entities, pipe_name, x, y, dir_v, "pipe", node_item=item)
        totals[pipe_name] = totals.get(pipe_name, 0) + 1
        u += 1.0


def _place_transition(from_idx: int, to_idx: int, facing: int,
                       entities: list, totals: dict, belt_name: str,
                       item: str, notes: list) -> None:
    """S1a : pose des belts physiques reliant la sortie d'un étage à l'entrée du suivant.

    Les étages sont ALIGNÉS en v (cf. plan) : belt_out_last(étage n) et belt_in_first(étage
    n+1) tombent au même v. En u, l'écart = stage_gap - 1.5. Pour stage_gap=2 (défaut),
    delta_u=0.5 -> ADJACENCE (0 belt intermédiaire, Factorio connecte les belts adjacentes).
    Pour stage_gap>2 (delta_u>1), on pose ceil(delta_u-1) belts intermédiaires le long de u
    (direction FACING_DIR_U) dans le gap. Anti-collision simple : si une position est déjà
    occupée (à 0.4 près), le belt est skip (note). Route en L (segment v) si désalignement
    résiduel (ne devrait pas arriver avec l'alignement ; S1b/c gère les vrais déséquilibres).
    """
    e_from = entities[from_idx]
    e_to = entities[to_idx]
    uf, vf = _to_uv(facing, e_from.x, e_from.y)   # belt_out_last (sortie étage n)
    ut, vt = _to_uv(facing, e_to.x, e_to.y)       # belt_in_first (entrée étage n+1)
    du = ut - uf
    dv = vt - vf

    def _occupied(x: float, y: float) -> bool:
        return any(abs(e.x - x) < 0.4 and abs(e.y - y) < 0.4 for e in entities)

    # Segment u : belts intermédiaires le long de u (si écart > adjacence), au v aligné.
    if abs(du) > 1.0:
        step = 1.0 if du > 0 else -1.0
        n_u = int(math.ceil(abs(du) - 1.0))
        for i in range(1, n_u + 1):
            cu = uf + step * i
            x, y = _to_xy(facing, cu, vf)
            if _occupied(x, y):
                notes.append(f"transition_collision_S1a:{item} @({x:.1f},{y:.1f})")
                continue
            _add(entities, belt_name, x, y, FACING_DIR_U[facing], "belt", node_item=item)
            totals[belt_name] = totals.get(belt_name, 0) + 1
    # Segment v : route le long de v si désalignement résiduel (anormal avec alignement).
    if abs(dv) > 0.1:
        step = 1.0 if dv > 0 else -1.0
        n_v = int(math.ceil(abs(dv) - 0.1))
        for i in range(1, n_v + 1):
            cv = vf + step * i
            x, y = _to_xy(facing, ut, cv)
            if _occupied(x, y):
                notes.append(f"transition_collision_S1a:{item} @({x:.1f},{y:.1f})")
                continue
            _add(entities, belt_name, x, y, FACING_DIR_V[facing], "belt", node_item=item)
            totals[belt_name] = totals.get(belt_name, 0) + 1


# ===== S1b/S1d : splitters / mergers (arbre binaire, orientation FACING_DIR_V) =====
#
# Géométrie VALIDÉE en jeu (S1d, measure_entity + harness 8/8 can_place + pose) :
# - Les belts_out/in sont des LANES parallèles en u (côte à côte en u, espacées de 1),
#   longeant v (flux direction FACING_DIR_V), se terminant à v_out (bout de rangée).
# - Le splitter/merger Factorio = 2x1. Orienté FACING_DIR_V (direction S pour facing E),
#   il couvre 2 tuiles en u (2 lanes adjacentes) + 1 en v (profondeur). Centre u sur un
#   tile boundary (milieu des 2 lanes = u_lane_0 + 0.5), centre v sur tile center.
# - Merger M lanes -> 1 bus (en +v du bout) : arbre binaire, M-1 mergers. À chaque
#   niveau, merger par paire de flux adjacents en u ; belts de liaison si écart en u
#   (rapprochement) + prolongement en +v pour les impairs. Sortie (bus) en +v.
# - Splitter 1 bus -> N lanes (symétrique, en -v de la tête) : N-1 splitters.
# - Cas 1->1 : pas d'arbre (adjacence S1a, back-compat chaîne fer).
# Anti-collision _occupied sur chaque pose (note *_collision si chevauchement).
# Les positions sont EXACTES (alignées grille, snap Factorio gère le 0.5) ; can_place
# validé S1d. Back-compat COUNTS : M-1 mergers, N-1 splitters (tests unitaires).

SPLITTER_NAME = "splitter"   # même entité Factorio pour splitter (1->2) et merger (2->1)
UNDER_NAME = "underground-belt"  # S1f : paire input/output pour crossings (1x1)
PIPE_TO_GROUND_NAME = "pipe-to-ground"  # S2c : paire input/output pour crossings pipe (1x1)
PIPE_UNDERGROUND_MAX = 10               # Factorio 2.0 hardcodé (non lisible runtime)

# Direction le long de -u (opposé de FACING_DIR_U) pour les belts de liaison en u.
_FACING_DIR_MINUS_U = {2: 6, 4: 0, 6: 2, 0: 4}


def _occ(entities, x: float, y: float) -> bool:
    """Anti-collision : position déjà occupée par une entité (à 0.4 près). S1f : ignore les
    entités skip (surface libérée pour splitter/transition/underground)."""
    return any((not getattr(e, "skip", False)) and abs(e.x - x) < 0.4 and abs(e.y - y) < 0.4
              for e in entities)


# ===== S4 : adaptation terrain (détection per-entité + replan auto déterministe) =====
#
# Le planner pousse depuis `anchor` en +u fixe. _occ ne vérifie que les entités du blueprint,
# pas le terrain. Le check post-hoc global (bbox-vs-bbox, imprécis) détecte mais ne contourne
# jamais. S4b : détection per-entité précise (_occ_terrain) + replan auto déterministe (shift
# anchor en v / pivot facing, règles fixes) AVANT handoff obstacle_blocking -> FactoryBuilder.
# Frontière : replan auto = règles fixes déterministes (pas d'LLM dans le planner) ; replan
# lourd (changer de gisement/zone) = FactoryBuilder (S4c).

# Noms de tuiles d'eau (Factorio 2.0) pour _occ_terrain.
_WATER_TILE_NAMES = {"water", "deepwater", "water-green", "water-mud"}


def _occ_terrain(terrain: "Terrain", x: float, y: float, w: float = 1.0, h: float = 1.0) -> Optional[str]:
    """Détection per-entité de collision terrain. Retourne "obstacle"|"water"|"out-of-map"|None.

    x,y = centre de l'entité (Factorio positionne au centre), w,h = taille -> bbox étendue
    [x-w/2, y-h/2, x+w/2, y+h/2]. Obstacles organiques : bbox-vs-bbox (terrain.obstacles).
    Water/out-of-map : précision tuile si terrain.tile_grid peuplé (une entité 3x3 à côté
    d'un obstacle 1x1 ne faux-positive pas), sinon fallback bbox-vs-bbox (terrain.water/
    out_of_map). Per-entité (vs post-hoc global imprécis)."""
    half_w, half_h = w / 2.0, h / 2.0
    ent_bbox = (x - half_w, y - half_h, x + half_w, y + half_h)
    # Obstacles organiques (rochers/arbres/cliffs) : bbox-vs-bbox.
    for obs in terrain.obstacles:
        if _bbox_intersects(ent_bbox, (float(obs[0]), float(obs[1]), float(obs[2]), float(obs[3]))):
            return "obstacle"
    # Précision tuile si tile_grid peuplé (water/out-of-map au niveau tuile).
    if terrain.tile_grid:
        x1, y1 = int(math.floor(x - half_w)), int(math.floor(y - half_h))
        x2, y2 = int(math.ceil(x + half_w)), int(math.ceil(y + half_h))
        for ty in range(y1, y2):
            for tx in range(x1, x2):
                name = terrain.tile_grid.get((tx, ty))
                if name is None:
                    continue
                if name in _WATER_TILE_NAMES:
                    return "water"
                if name == "out-of-map":
                    return "out-of-map"
        return None
    # Pas de tile_grid : fallback bbox-vs-bbox sur water/out_of_map.
    for wl in terrain.water:
        if _bbox_intersects(ent_bbox, (float(wl[0]), float(wl[1]), float(wl[2]), float(wl[3]))):
            return "water"
    for om in terrain.out_of_map:
        if _bbox_intersects(ent_bbox, (float(om[0]), float(om[1]), float(om[2]), float(om[3]))):
            return "out-of-map"
    return None


def _shift_anchor(anchor: tuple[float, float], facing: int, dv: float) -> tuple[float, float]:
    """Décale l'anchor perpendiculairement à u (en v) -> décale toute la cascade (propagation
    alignement v S1a : av = v_out + half_v_next - 0.5 propage le décalage de l'étage 1 à tous
    les étages suivants). Les drills restent sur patch.bbox (indépendant de l'anchor) ; la belt
    de collecte drills->étage1 s'allonge (_place_transition gère les transitions longues)."""
    u, v = _to_uv(facing, anchor[0], anchor[1])
    return _to_xy(facing, u, v + dv)


def _rotate_facing(facing: int, steps: int) -> int:
    """Pivot facing de `steps` quarts de tour (±1 = ±90°, 2 = 180°). -> {0,2,4,6}."""
    return ((facing // 2 + steps) % 4) * 2


def _count_terrain_hits(entities, terrain, geometry) -> int:
    """Compte les entités (non skip) en collision terrain (choix du "best" layout au replan :
    on garde le layout avec le max d'entités placables)."""
    n = 0
    for e in entities:
        if getattr(e, "skip", False):
            continue
        g = geometry.geometry(e.name)
        w = g.w if g else 1.0
        h = g.h if g else 1.0
        if _occ_terrain(terrain, e.x, e.y, w, h) is not None:
            n += 1
    return n


def _belt_liaison(entities, totals, belt_name: str, item: str, facing: int,
                  u: float, v: float, direction: int, notes: list, tag: str) -> Optional[int]:
    """Pose un belt de liaison (anti-collision). Retourne l'idx ou None si collision."""
    x, y = _to_xy(facing, u, v)
    if _occ(entities, x, y):
        notes.append(f"{tag}_collision:{item} @({x:.1f},{y:.1f})")
        return None
    idx = _add(entities, belt_name, x, y, direction, "belt", node_item=item)
    totals[belt_name] = totals.get(belt_name, 0) + 1
    return idx


# ===== S1f : underground crossings + tap/feed redesign (bus circuiterie) =====
#
# Mécaniques VALIDÉES en jeu (probe live 2026-07-24, serveur headless) :
# - T1 : paire underground type="input"/"output" (create_entity type=) + croisement belt
#   perpendiculaire au-dessus (la paire circule, belt de croisement posée sans casser la paire).
# - T2b : splitter_output_priority = "left"/"right"/"none" (STRING Factorio 2.0, settable
#   runtime) dirige le flux vers la sortie prioritaire.
# - T3 : merger (splitter 1 entrée orienté +v) sortie +v -> lane +v connexion directe.
# - T4 : sideload virage +v -> +u (belt +v dépose sur belt +u à la tuile de dépôt +v).
# Convention LayoutPlanner : +u = "left" (POV flux) pour tous facings -> priority="left"
# dirige vers +u (consommateur), "right" continue la lane +v.
#
# Architecture : modifications in-place des belts lane (idx stable via _find_at -> pas de
# shift -> connections/lane_idx_by_item intacts). skip=True retire une belt (surface
# libérée, _occ l'ignore, le consommateur filtre not skip).


def _find_at(entities, x: float, y: float) -> Optional[int]:
    """Retourne l'idx de l'entité à (x,y) (à 0.4 près), ou None. S1f : pour modifier in-place
    une belt lane à remplacer (idx stable -> connections/lane_idx_by_item intacts)."""
    for i, e in enumerate(entities):
        if abs(e.x - x) < 0.4 and abs(e.y - y) < 0.4:
            return i
    return None


def _skip_belt_at(entities, u: float, v: float, facing: int) -> bool:
    """S1f : marque skip la belt à (u,v) pour poser un splitter de tap/feed à sa place.
    Retourne True si une entité a été skipée."""
    x, y = _to_xy(facing, u, v)
    idx = _find_at(entities, x, y)
    if idx is None:
        return False
    entities[idx].skip = True
    return True


def _under_crossing(entities, totals, under_name: str, belt_name: str, item: str,
                    facing: int, u_lane: float, v_cross: float, notes: list) -> bool:
    """S1f volet A : libère la surface à (u_lane, v_cross) sur une lane bus +v pour laisser
    passer une transition +u perpendiculaire. Modifie la lane in-place (idx stable) :
      - belt (u_lane, v_cross-1) -> underground INPUT (reçoit la lane amont, plonge +v)
      - belt (u_lane, v_cross+1) -> underground OUTPUT (ressort +v vers la lane aval)
      - belt (u_lane, v_cross)   -> skip (surface libre pour la transition +u)
    Le souterrain couvre v_cross (distance paire = 2, validé live T1). Retourne True si la
    paire a été posée. Note under_collision si une tuile nécessaire est absente/non-belt."""
    dir_v = FACING_DIR_V[facing]
    x_in, y_in = _to_xy(facing, u_lane, v_cross - 1.0)
    x_out, y_out = _to_xy(facing, u_lane, v_cross + 1.0)
    x_mid, y_mid = _to_xy(facing, u_lane, v_cross)
    idx_in = _find_at(entities, x_in, y_in)
    idx_out = _find_at(entities, x_out, y_out)
    idx_mid = _find_at(entities, x_mid, y_mid)
    ok = True
    if idx_in is not None and entities[idx_in].name == belt_name:
        e = entities[idx_in]
        e.name, e.role, e.ug_type, e.direction = under_name, "under-in", "input", dir_v
    else:
        notes.append(f"under_collision:{item} input @(u={u_lane},v={v_cross - 1}) absente/non-belt")
        ok = False
    if idx_out is not None and entities[idx_out].name == belt_name:
        e = entities[idx_out]
        e.name, e.role, e.ug_type, e.direction = under_name, "under-out", "output", dir_v
    else:
        notes.append(f"under_collision:{item} output @(u={u_lane},v={v_cross + 1}) absente/non-belt")
        ok = False
    if idx_mid is not None:
        entities[idx_mid].skip = True
    if ok:
        totals[under_name] = totals.get(under_name, 0) + 2
        if idx_mid is not None:
            totals[belt_name] = totals.get(belt_name, 0) - 1  # belt centrale retirée
        notes.append(f"under_crossing_S1f:{item} @(u={u_lane},v={v_cross}) paire input/output")
    return ok


def _merge_tree_stage(u_lanes: list[float], v_out: float, facing: int, entities, totals,
                      belt_name: str, item: str, notes) -> tuple[Optional[int], int]:
    """Corps du merge tree orient FACING_DIR_V sur M lanes (positions u_lanes) à v_out.

    M-1 mergers (back-compat count). Belts de liaison : rapprochement en u si écart +
    prolongement en +v pour les impairs. Retourne (bus_idx, n_mergers). M<=1 : pas de merger.
    Opère sur des POSITIONS (u_lanes) — pas d'idx d'entité — pour permettre à la branche
    feed_side="bus" de fournir des lanes virtuelles côté bus.
    """
    M = len(u_lanes)
    if M <= 1:
        return None, 0
    dir_v = FACING_DIR_V[facing]
    dir_u = FACING_DIR_U[facing]
    dir_mu = _FACING_DIR_MINUS_U[facing]
    n_mergers = 0
    bus_idx = None

    # flux = liste de (u, v) des bouts de flux à merger (init = les lanes à v_out).
    flux = [(u, v_out) for u in u_lanes]
    while len(flux) > 1:
        next_flux = []
        i = 0
        while i < len(flux):
            if i + 1 < len(flux):
                u_a, v_a = flux[i]
                u_b, v_b = flux[i + 1]
                # Rapprocher u_b vers u_a+1 (adjacent en u) si écart > 1 : belts de liaison en u.
                target = u_a + 1.0
                if abs(u_b - target) > 0.01:
                    step = 1.0 if u_b < target else -1.0
                    dir_step = dir_u if step > 0 else dir_mu
                    cur = u_b
                    for _ in range(int(round(abs(u_b - target)))):
                        cur += step
                        _belt_liaison(entities, totals, belt_name, item, facing,
                                      cur, v_b, dir_step, notes, "merger_liaison")
                    u_b = target
                # Merger à (u_a+0.5, v_a+1) direction FACING_DIR_V (centre u = milieu des 2 lanes,
                # en +v du bout, tuile v_a+1 -> nchevauche pas le belt_out bout à v_a).
                mu, mv = u_a + 0.5, v_a + 1.0
                x, y = _to_xy(facing, mu, mv)
                if _occ(entities, x, y):
                    notes.append(f"merger_collision:{item} @({x:.1f},{y:.1f})")
                else:
                    _add(entities, SPLITTER_NAME, x, y, dir_v, "merger", node_item=item)
                    totals[SPLITTER_NAME] = totals.get(SPLITTER_NAME, 0) + 1
                    n_mergers += 1
                # Sortie belt (bus aval) à (u_a, v_a+2) direction FACING_DIR_V.
                out_idx = _belt_liaison(entities, totals, belt_name, item, facing,
                                         u_a, v_a + 2.0, dir_v, notes, "merger_out")
                if out_idx is not None:
                    bus_idx = out_idx
                next_flux.append((u_a, v_a + 2.0))
                i += 2
            else:
                # Impair : prolonger le flux en +v de 2 tiles (aligne au niveau suivant).
                u_a, v_a = flux[i]
                _belt_liaison(entities, totals, belt_name, item, facing,
                              u_a, v_a + 1.0, dir_v, notes, "merger_prolong")
                _belt_liaison(entities, totals, belt_name, item, facing,
                              u_a, v_a + 2.0, dir_v, notes, "merger_prolong")
                next_flux.append((u_a, v_a + 2.0))
                i += 1
        flux = next_flux
    notes.append(f"merger_tree_S1d:{item} (M={M}, {n_mergers} mergers, orient FACING_DIR_V)")
    return bus_idx, n_mergers


def _build_merge_tree(source_idxs: list[int], facing: int, entities, totals,
                      belt_name: str, item: str, notes) -> tuple[Optional[int], int]:
    """M belts_out (lanes côte à côte en u, flux en v, bouts à v_out) -> 1 bus (en +v).

    Arbre binaire de mergers orientés FACING_DIR_V. M-1 mergers (back-compat count).
    Retourne (bus_idx, n_mergers). M<=1 : pas de merger.

    Merger à v_out+1 côté étage (position des belts_out). CONSTAT S1d : collision
    merger↔belts_in consommateur en main bus (étages alignés en v). Le feed correct
    (virage -u→+v des belts_out dir -u + merger hors-zone belts_in + re-route vers la
    lane bus) nécessite underground belts (collisions structurelles merger-sur-lane +
    croisements de bus lanes) -> reporté S1f. Tentative S1e feed_side="bus" (merger V
    sur lanes dir -u) était géométriquement cassée (directions incohérentes + 187
    collisions avec les belts_out dir -u déjà longés vers le bus par S1c) -> revert.
    """
    M = len(source_idxs)
    if M <= 1:
        return (source_idxs[0] if M == 1 else None), 0
    src_uv = [_to_uv(facing, entities[i].x, entities[i].y) for i in source_idxs]
    v_out = src_uv[0][1]
    u_lanes = sorted(su[0] for su in src_uv)
    return _merge_tree_stage(u_lanes, v_out, facing, entities, totals,
                             belt_name, item, notes)


def _split_subtree(u_lo: int, u_hi: int, v_out: float, facing: int, entities, totals,
                   belt_name: str, item: str, notes, v_target: float) -> int:
    """S1e : arbre binaire équilibré en -v (flux +v) pour les feuilles [u_lo, u_hi).

    Feuilles = belts_in déjà posés par _place_stage à (u, v_target) — jamais replacés.
    Nœuds à v_out+1 (amont en -v depuis v_target), splitter 2x1 orienté FACING_DIR_V,
    entrée -v (parent/bus), 2 sorties +v (enfants) à (u_mid-1, v_out+2) et (u_mid, v_out+2).
    u_mid=(u_lo+u_hi)//2. N non-puissance-de-2 : feuille peu profonde (n==1 avec v_out<v_target)
    reçoit prolongation +v de v_out+1 à v_target-1 pour combler le gap jusqu'au belt_in.
    Au dernier niveau (v_out+2 == v_target), les sorties SONT les belts_in -> pas de belt
    (le belt_in existant reçoit le flux directement du splitter). Retourne le # de splitters.
    N-1 splitters (back-compat count, prouvé par induction : N feuilles -> N-1 nœuds internes).
    """
    n = u_hi - u_lo
    dir_v = FACING_DIR_V[facing]
    if n == 1:
        # Feuille = belt_in à (u_lo, v_target). Prolongation +v si gap (feuille peu profonde).
        for v in range(int(v_out) + 1, int(v_target)):
            _belt_liaison(entities, totals, belt_name, item, facing,
                          u_lo, float(v), dir_v, notes, "split_leaf_prolong")
        return 0
    u_mid = (u_lo + u_hi) // 2          # left=[u_lo, u_mid), right=[u_mid, u_hi)
    v_split = v_out + 1.0
    # Splitter 2x1 orienté V (emprise 2u: u_mid-1, u_mid) à (u_mid-0.5, v_split).
    su, sv = u_mid - 0.5, v_split
    x, y = _to_xy(facing, su, sv)
    if _occ(entities, x, y):
        notes.append(f"splitter_collision:{item} @({x:.1f},{y:.1f})")
        n_split = 0
    else:
        _add(entities, SPLITTER_NAME, x, y, dir_v, "splitter", node_item=item)
        totals[SPLITTER_NAME] = totals.get(SPLITTER_NAME, 0) + 1
        n_split = 1
    # Sorties +v (entrées enfants) à (u_mid-1, v_out+2) et (u_mid, v_out+2). Au dernier niveau
    # (v_out+2 == v_target) les sorties SONT les belts_in -> pas de belt à poser.
    v_child = v_out + 2.0
    if v_child < v_target:
        _belt_liaison(entities, totals, belt_name, item, facing,
                      u_mid - 1, v_child, dir_v, notes, "split_out")
        _belt_liaison(entities, totals, belt_name, item, facing,
                      u_mid, v_child, dir_v, notes, "split_out")
    n_split += _split_subtree(u_lo, u_mid, v_child, facing, entities, totals,
                               belt_name, item, notes, v_target)
    n_split += _split_subtree(u_mid, u_hi, v_child, facing, entities, totals,
                               belt_name, item, notes, v_target)
    return n_split


def _tap_bus_to_consumer(bus_idx: int, target_idx: int, n_out: int, facing: int,
                          entities, totals, belt_name: str, under_name: str, item: str,
                          notes, bus_lanes_u: list) -> int:
    """S1f volet B : tap bus -> consommateur CIRCUITERIE (règle la lacune S1e).

    Splitter de prélèvement sur la lane bus (priority="left" -> sortie +u vers consommateur),
    virage +v->+u (sideload, validé T4), transition +u qui traverse les autres lanes bus via
    _under_crossing (paire underground, validé T1), puis feed la cible :
      - n_out>1 : belt +v split_entry_v (u_root_entry, v_root_in) -> splitter racine split tree
        S1e (aligné consommateur, _split_subtree).
      - n_out=1 : belt_in (u_target, v_target) direct (sideload +u->+v, la dernière belt +u dépose).

    Géométrie (facing=2 : u=east +x, v=south +y) :
      v_root_in = v_target - 2*D, D=ceil(log2(n_out)) ; u_root_entry = u_target + n_out//2 - 1.
      v_cible = v_root_in (n_out>1) | v_target (n_out=1) ; v_tap = v_cible - 2 (décalage virage +v->+u).
      Splitter (u_bus, v_tap)+(u_bus+1, v_tap) orient FACING_DIR_V, priority="left".
      Sortie prio (u_bus+1, v_tap+1) belt +v -> virage -> (u_bus+1, v_cible) belt +u (1re transition).
      Sortie non-prio (u_bus, v_tap+1) [right] -> belt lane +v continue (connexion directe).

    VALIDATION LIVE 2026-07-24 (serveur headless) : T1 paire underground + croisement, T2b
    splitter_output_priority="left" (STRING Factorio 2.0), T4 sideload +v->+u. Convention :
    +u = "left" (POV flux) pour tous facings -> priority="left" dirige vers +u (consommateur).

    Retourne le nombre de splitters de prélèvement posés (1 si OK, 0 si collision). Count main bus
    S1f : n_out = 1 prélèvement + (n_out-1) split tree (S1e) ; bande bus_layout=False inchangée
    (target_idx=None, _build_split_tree branche S1b). Différent de S1e (n_out<=1 -> 0 splitter).
    """
    u_bus, _ = _to_uv(facing, entities[bus_idx].x, entities[bus_idx].y)
    u_target, v_target = _to_uv(facing, entities[target_idx].x, entities[target_idx].y)
    D = max(1, math.ceil(math.log2(n_out))) if n_out > 1 else 1
    v_root_in = v_target - 2.0 * D
    u_root_entry = u_target + n_out // 2 - 1
    if n_out > 1:
        v_cible, u_cible = v_root_in, u_root_entry
    else:
        v_cible, u_cible = v_target, u_target
    v_tap = v_cible - 2.0
    dir_u = FACING_DIR_U[facing]
    dir_v = FACING_DIR_V[facing]
    n_tap = 0
    # 1. Splitter de prélèvement (priority="left" -> +u). Skip la belt lane à (u_bus, v_tap).
    _skip_belt_at(entities, u_bus, v_tap, facing)
    sx, sy = _to_xy(facing, u_bus + 0.5, v_tap)
    if not _occ(entities, sx, sy):
        sp = _add(entities, SPLITTER_NAME, sx, sy, dir_v, "splitter", node_item=item)
        entities[sp].priority = "left"
        totals[SPLITTER_NAME] = totals.get(SPLITTER_NAME, 0) + 1
        n_tap = 1
        notes.append(f"tap_splitter_S1f:{item} @(u={u_bus}+0.5,v={v_tap}) priority=left -> +u (prélèvement)")
    else:
        notes.append(f"tap_splitter_collision:{item} @(u={u_bus}+0.5,v={v_tap})")
    # 2. Crossings : perce les lanes bus intermédiaires (u_bus < u_lane < u_cible) à v=v_cible.
    for ul in bus_lanes_u:
        if u_bus + 0.5 < ul < u_cible - 0.5:
            _under_crossing(entities, totals, under_name, belt_name, item, facing,
                            float(ul), v_cible, notes)
    # 3. Transition +u de (u_bus+1, v_cible) [virage] à (u_cible-1, v_cible). Aux crossings la
    #    surface est libérée (skip) -> belt +u posée au-dessus du souterrain.
    for u in range(int(u_bus) + 1, int(u_cible)):
        _belt_liaison(entities, totals, belt_name, item, facing,
                      float(u), v_cible, dir_u, notes, "split_transition")
    # 4. Cible : n_out>1 -> split_entry_v (belt +v feed splitter racine) ; n_out=1 -> belt_in
    #    (déjà posé, la dernière belt +u dépose dessus par sideload +u->+v, pas de pose).
    if n_out > 1 and int(u_cible) > int(u_bus):
        idx_entry = _belt_liaison(entities, totals, belt_name, item, facing,
                                  float(u_cible), v_cible, dir_v, notes, "split_entry_v")
        if idx_entry is not None:
            notes.append(f"split_entry_v:{item} @(u={int(u_cible)},v={int(v_cible)}) "
                         f"-> feed splitter racine (S1f tap circulant)")
    return n_tap


def _feed_consumer_to_bus(source_idxs: list, facing: int, entities, totals,
                          belt_name: str, under_name: str, item: str, notes,
                          bus_lanes_u: list, bus_lane_u: float) -> int:
    """S1f volet C : feed étage producteur -> bus CIRCUITERIE (règle la lacune S1e feed).

    M belts_out étage (côte à côte en u, flux +v, bouts à v_out) sont virées -u via sideload
    +v->-u, traversent les lanes bus intermédiaires via _under_crossing (paire underground,
    validé T1), puis un arbre de mergers (M-1, _merge_tree_stage réutilisé sur lanes virtuelles
    à droite de la lane produit) fusionne les M belts_out en 1, et un merger-lane (splitter
    2x1 orient +v sur la lane produit) injecte le flux dans la lane bus : entrée gauche = lane
    amont (passthrough, la lane traverse), entrée droite = feed (sortie arbre), sortie +v =
    lane aval (T3 connexion directe, validé live).

    Count S1f : M mergers = (M-1) arbre + 1 merger-lane injection. Évolution vs S1d (M-1),
    symétrique du tap (+1 prélèvement). M<=1 : 0 merger (sideload -u->+v direct sur la lane).

    Géométrie (facing=2 : u=east +x, v=south +y) :
      v_feed_in = v_out + 1 (belts -u circulent à v_feed_in, entrées merger arbre).
      u_lanes_virt = [bus_lane_u+1 .. bus_lane_u+M] (entrées arbre, à droite de la lane produit
      -> les belts -u ne traversent PAS la lane produit, seulement les lanes intermédiaires).
      Sideload +v->-u : belt_out_k (u_étage_k, v_out, +v) dépose sur belt -u (u_étage_k, v_feed_in).
      Belts -u de (u_étage_k, v_feed_in) vers -u jusqu'à (bus_lane_u+1+k, v_feed_in), perçant les
      lanes bus intermédiaires (u_lane dans ]bus_lane_u, u_étage_k[ ∩ bus_lanes_u) via
      _under_crossing(u_lane, v_feed_in).
      Arbre mergers : _merge_tree_stage(u_lanes_virt, v_feed_in) -> M-1 mergers, sortie à
      (bus_lane_u+1, v_final), v_final = v_feed_in + 2*ceil(log2(M)).
      Merger-lane : splitter (bus_lane_u+0.5, v_final+1) orient +v. _skip_belt_at(bus_lane_u,
      v_final+1) libère la lane. Entrées (bus_lane_u, v_final)[amont] + (bus_lane_u+1, v_final)
      [feed arbre]. Sortie (bus_lane_u, v_final+2)[aval, T3].

    Retourne n_mergers (M si M>1, 0 si M<=1). Back-compat bande bus_layout=False inchangée
    (_build_merge_tree branche S1d, pas appelé ici). M-1 conservé en bande.
    """
    M = len(source_idxs)
    if M <= 0:
        return 0
    dir_u = FACING_DIR_U[facing]
    dir_mu = _FACING_DIR_MINUS_U[facing]
    dir_v = FACING_DIR_V[facing]
    src_uv = [(_to_uv(facing, entities[i].x, entities[i].y), entities[i]) for i in source_idxs]
    src_uv.sort(key=lambda t: t[0][0])  # par u croissant
    v_out = src_uv[0][0][1]
    v_feed_in = v_out + 1.0
    n_mergers = 0

    # 1. Sideload +v->-u : pour chaque belt_out, pose une belt -u à (u_étage_k, v_feed_in)
    #    qui reçoit le flux du bout belt_out (+v dépose sur -u à la tuile de dépôt +v).
    #    Puis belts -u vers -u jusqu'à (bus_lane_u+1+k, v_feed_in), perçant les lanes bus
    #    intermédiaires (u_lane dans ]bus_lane_u, u_étage_k[ ∩ bus_lanes_u) via _under_crossing.
    for k, ((u_e, _v_e), _e) in enumerate(src_uv):
        u_target = bus_lane_u + 1.0 + k  # entrée arbre (lane virtuelle k)
        # belt -u à (u_e, v_feed_in) : reçoit sideload +v du bout belt_out.
        _belt_liaison(entities, totals, belt_name, item, facing,
                      u_e, v_feed_in, dir_mu, notes, "feed_sideload_in")
        # belts -u de (u_e-1, v_feed_in) vers -u jusqu'à u_target.
        u = u_e - 1.0
        while u >= u_target - 0.01:
            # Percer la lane bus intermédiaire à (u, v_feed_in) si u est une lane bus et u>bus_lane_u.
            if any(abs(u - ul) < 0.01 for ul in bus_lanes_u) and u > bus_lane_u + 0.01:
                _under_crossing(entities, totals, under_name, belt_name, item, facing,
                                u, v_feed_in, notes)
            _belt_liaison(entities, totals, belt_name, item, facing,
                          u, v_feed_in, dir_mu, notes, "feed_belt_u")
            u -= 1.0

    # 2. Arbre de mergers (M-1) sur lanes virtuelles [bus_lane_u+1 .. bus_lane_u+M] à v_feed_in.
    if M > 1:
        u_lanes_virt = [bus_lane_u + 1.0 + k for k in range(M)]
        bus_idx, n_tree = _merge_tree_stage(u_lanes_virt, v_feed_in, facing, entities, totals,
                                            belt_name, item, notes)
        n_mergers += n_tree
        # Sortie arbre à (bus_lane_u+1, v_final). v_final = v_feed_in + 2*ceil(log2(M)).
        v_final = v_feed_in + 2.0 * max(1, math.ceil(math.log2(M)))
        # 3. Merger-lane : splitter 2x1 orient +v à (bus_lane_u+0.5, v_final+1).
        #    _skip_belt_at libère la lane à (bus_lane_u, v_final+1) pour le splitter.
        v_feed = v_final + 1.0
        _skip_belt_at(entities, bus_lane_u, v_feed, facing)
        mx, my = _to_xy(facing, bus_lane_u + 0.5, v_feed)
        if _occ(entities, mx, my):
            notes.append(f"feed_lane_collision:{item} @({mx:.1f},{my:.1f})")
        else:
            _add(entities, SPLITTER_NAME, mx, my, dir_v, "merger", node_item=item)
            totals[SPLITTER_NAME] = totals.get(SPLITTER_NAME, 0) + 1
            n_mergers += 1
        # Sortie lane aval à (bus_lane_u, v_feed+1) -> belt +v (lane continue, T3).
        _belt_liaison(entities, totals, belt_name, item, facing,
                      bus_lane_u, v_feed + 1.0, dir_v, notes, "feed_lane_out")
        notes.append(f"feed_merger_lane_S1f:{item} @(u={bus_lane_u:.0f}+0.5,v={v_feed:.0f}) "
                     f"merger-lane injection lane (T3), entrées lane amont + feed arbre")
    else:
        # M=1 : pas de merger. belt -u arrive à (bus_lane_u+1, v_feed_in) et sideload -u->+v
        # direct sur la lane produit à (bus_lane_u, v_feed_in). La lane continue.
        notes.append(f"feed_direct_S1f:{item} (M=1, sideload -u->+v sur lane, pas de merger)")
    notes.append(f"bus_feed_S1f:{item}->{item} ({n_mergers} mergers [{max(0, M - 1)} arbre + "
                 f"{1 if M > 1 else 0} merger-lane], feed circuiterie)")
    return n_mergers


def _route_feed_to_lane(src_idx: int, lane_idx: int, facing: int, entities, totals,
                        belt_name: str, under_name: str, item: str, notes,
                        bus_lanes_u: list, bus_lane_u: float) -> None:
    """S1g : route la 1 sortie merger étage -> lane produit bus (re-planification spatiale feed).

    Géométrie VALIDÉE live 2026-07-24 (verify_feed_s1g.py, serveur headless) : le merger tree
    côté étage (M->1, _build_merge_tree conservé) produit 1 sortie +v (src_idx à (u_src, v_final)).
    Le flux vire +v->-u (T5 : sideload +v->-u, belt +v dépose sur belt -u à la tuile de dépôt +v),
    traverse les lanes bus intermédiaires (u_lane dans ]bus_lane_u, u_src[ ∩ bus_lanes_u) via
    _under_crossing (paire underground, validé T1), puis la dernière belt -u (bus_lane_u+1,
    v_inject) dépose par sideload -u->+v sur la lane produit à (bus_lane_u, v_inject) (T6 :
    MERGER GRATUIT belt->belt, la lane amont continue +v, pas coupée par l'injection). Count =
    M-1 mergers conservé (pas de merger-lane : l'injection sideload est un merger gratuit,
    back-compat count N-1/M-1).

    Le gap entre étages (gap_feed dans _plan_bus) libère v_out+1..v_out+gap pour le merger tree
    + le virage + belts -u (qui collisionnaient les belts_in consommateur en S1d sans gap).
    Règle le CONSTAT S1f volet C (M belts_out // ne peuvent virer -u sur même rangée v).

    Géométrie (facing=2 : u=east +x, v=south +y) :
      v_inject = v_final + 1.0  (belt -u à (u_src, v_inject) reçoit sideload +v->-u de src_idx).
      Belts -u de (u_src-1, v_inject) vers -u jusqu'à (bus_lane_u+1, v_inject), crossings.
      Sideload -u->+v sur lane (bus_lane_u, v_inject) — la lane existe déjà (reçoit, continue +v).
    """
    dir_mu = _FACING_DIR_MINUS_U[facing]
    u_src, v_final = _to_uv(facing, entities[src_idx].x, entities[src_idx].y)
    v_inject = v_final + 1.0
    u_lane = bus_lane_u
    # 1. Virage +v->-u : belt -u à (u_src, v_inject) reçoit sideload +v->-u de la sortie merger.
    _belt_liaison(entities, totals, belt_name, item, facing,
                  u_src, v_inject, dir_mu, notes, "feed_virage")
    # 2. Belts -u de (u_src-1, v_inject) vers -u jusqu'à (u_lane+1, v_inject). Crossings sur les
    #    lanes bus intermédiaires (u_lane < ul < u_src) : _under_crossing perce la lane +v.
    u = u_src - 1.0
    while u >= u_lane + 1.01:
        if any(abs(u - ul) < 0.01 for ul in bus_lanes_u) and abs(u - u_lane) > 0.01:
            _under_crossing(entities, totals, under_name, belt_name, item, facing,
                            u, v_inject, notes)
        _belt_liaison(entities, totals, belt_name, item, facing,
                      u, v_inject, dir_mu, notes, "feed_belt_u")
        u -= 1.0
    # 3. Sideload -u->+v sur la lane produit : belt -u (u_lane+1, v_inject) dépose sur lane
    #    (u_lane, v_inject) déjà posée. Pas de pose (merger gratuit belt->belt, T6, lane continue).
    notes.append(f"feed_inject_S1g:{item} @(u={int(u_lane)},v={int(v_inject)}) "
                 f"sideload -u->+v sur lane (merger gratuit T6, lane continue)")


def _build_split_tree(in_idx: int, n_out: int, facing: int, entities, totals,
                      belt_name: str, item: str, notes,
                      target_idx: Optional[int] = None,
                      under_name: str = UNDER_NAME,
                      bus_lanes_u: Optional[list] = None) -> int:
    """1 bus (in_idx) -> n_out lanes.

    target_idx=None (défaut, branche bande S1b) : orientation FACING_DIR_U, pop-1re-tête,
    relatif au bus in_idx. Back-compat S1b (count correct, circuiterie approchée côté bande
    où stage_gap=2 -> sorties tombent sur belts_in). n_out<=1 -> 0 splitter (chaîne fer 1->1).

    target_idx fourni (S1f, branche main bus tap) : _tap_bus_to_consumer (splitter de
    prélèvement priority="left" -> +u, virage +v->+u, crossings underground, transition +u)
    feed le split tree S1e (arbre binaire équilibré en -v aligné consommateur, _split_subtree).
    Feuilles = belts_in déjà posés par _place_stage. Count main bus S1f = n_out (1 prélèvement
    + n_out-1 tree) ; n_out=1 -> 1 prélèvement (différent de S1e qui retournait 0). Règle la
    lacune S1e (tap bus->transition non-circuiterie) via splitter priority + underground crossings.
    """
    if n_out <= 1 and target_idx is None:
        return 0  # bande : pas de split tree (back-compat chaîne fer 1->1 = 0 splitter)
    if target_idx is None:
        # Branche S1b back-compat (orient U, pop-1re-tête, relatif au bus).
        u_bus, v_bus = _to_uv(facing, entities[in_idx].x, entities[in_idx].y)
        dir_u = FACING_DIR_U[facing]
        n_splitters = 0
        flux = [(u_bus, v_bus)]
        while len(flux) < n_out:
            u_a, v_a = flux.pop(0)
            su, sv = u_a + 1.0, v_a + 0.5
            x, y = _to_xy(facing, su, sv)
            if _occ(entities, x, y):
                notes.append(f"splitter_collision:{item} @({x:.1f},{y:.1f})")
            else:
                _add(entities, SPLITTER_NAME, x, y, dir_u, "splitter", node_item=item)
                totals[SPLITTER_NAME] = totals.get(SPLITTER_NAME, 0) + 1
                n_splitters += 1
            _belt_liaison(entities, totals, belt_name, item, facing,
                          u_a + 2.0, v_a, dir_u, notes, "splitter_out")
            _belt_liaison(entities, totals, belt_name, item, facing,
                          u_a + 2.0, v_a + 1.0, dir_u, notes, "splitter_out")
            flux.append((u_a + 2.0, v_a))
            flux.append((u_a + 2.0, v_a + 1.0))
        notes.append(f"splitter_tree_S1d:{item} (n_out={n_out}, {n_splitters} splitters, orient FACING_DIR_U, bande S1b back-compat)")
        return n_splitters
    # Branche S1f (target fourni) : tap circuiterie (_tap_bus_to_consumer) + split tree S1e.
    u_target, v_target = _to_uv(facing, entities[target_idx].x, entities[target_idx].y)
    D = max(1, math.ceil(math.log2(n_out)))
    v_root_in = v_target - 2.0 * D
    # Tap : splitter prélèvement priority="left" + virage +v->+u + crossings + transition +u.
    n_tap = _tap_bus_to_consumer(in_idx, target_idx, n_out, facing, entities, totals,
                                 belt_name, under_name, item, notes, bus_lanes_u or [])
    n_splitters = n_tap
    # Split tree S1e : arbre équilibré en -v aligné consommateur (n_out>1 seulement).
    if n_out > 1:
        n_splitters += _split_subtree(int(u_target), int(u_target) + n_out, v_root_in,
                                      facing, entities, totals, belt_name, item, notes, v_target)
    n_tree = n_splitters - n_tap
    notes.append(f"splitter_tree_S1e:{item} (n_out={n_out}, {n_splitters} splitters "
                 f"[1 prélèvement S1f + {n_tree} tree S1e], tap priority +u + crossings + arbre -v aligné consommateur)")
    return n_splitters


# ===== S1c : main bus (layout alternatif, défaut off) =====
#
# Modèle T5 : bus perpendiculaire au facing (longe v), lanes empilées en u (1 lane par
# item intermédiaire = produit par un étage ET consommé par un autre). Les étages
# (alignés en u, cascade) TAPENT (splitter prélève une portion de lane pour alimenter
# belts_in) leurs ingrédients et FEEDENT (merger réinjecte belts_out dans la lane) leur
# produit. Réutilise _build_split_tree (tap = splitter 1->N) et _build_merge_tree (feed
# = merger M->1) de S1b. Back-compat : bus_layout=False (défaut) -> branche bande S1a/b.
# Géométrie APPROXIMÉE en S1c (lanes posées côté -u des étages, taps/feeds le long de +u
# depuis la lane/belts_out) — belts de liaison physiques lane<->étage = S1d (can_place +
# offsets). Les tests unitaires vérifient COUNTS (n lanes, splitters/mergers), la
# préSENCE du role "bus-belt", la DIRECTION (FACING_DIR_V pour les lanes), la back-compat
# (totals machines identiques) — pas can_place (pas de serveur).

def _plan_bus_core(request: LayoutRequest, geometry: GeometryBase, splan, constraints,
              belt_speed: float, inserter_tp: float, tp_fn,
              facing: int, au: float, av: float,
              ordered: list, patches_by_res: dict, notes: list,
              feasibility: str) -> LayoutPlan:
    """Layout main bus (S1c). Bus persistant + étages qui tapent/feedent.

    1. Place les étages en cascade (réutilise _place_drills/_place_stage) SANS connecter
       inter-étages. 2. Pose le bus (lanes par item intermédiaire, longe v, empilées en u
       côté -u des étages). 3. TAP chaque ingrédient sur le bus (splitter tree 1->N).
       4. FEED chaque produit sur le bus (merger tree M->1). 5. Connexion directe
       drills->étage pour les items hors bus (ore). Retourne LayoutPlan.
    """
    entities: list[LayoutEntity] = []
    connections: list[tuple[int, int, str]] = []
    stage_log: dict[str, StageLogistics] = {}
    totals: dict[str, int] = {}

    # Items du bus = produits par un étage non-mine ET consommés par un étage.
    produced = {n.item for n in splan.nodes if n.role != "mine"}
    consumed: set[str] = set()
    for n in splan.nodes:
        if n.role != "mine":
            for ing, _ in (n.ingredients or []):
                consumed.add(ing)
    bus_items = sorted(produced & consumed)

    # 1. Placement des étages en cascade (SANS connexions inter-étages).
    au_cur, av_cur = au, av
    stage_info: dict[str, dict] = {}
    for i, node in enumerate(ordered):
        if node.role == "mine":
            patch = patches_by_res.get(node.item)
            if patch is None:
                notes.append(f"patch manquant: {node.item}")
                if feasibility == "ok":
                    feasibility = f"missing_patch:{node.item}"
                continue
            r = _place_drills(node, patch, geometry, constraints, facing, au_cur, av_cur,
                              entities, totals, stage_log, notes)
        else:
            r = _place_stage(node, geometry, constraints, belt_speed, inserter_tp, tp_fn,
                             facing, au_cur, av_cur, entities, totals, stage_log, notes)
        if r is None:
            continue
        (belt_in_first, belt_out_last, ing_name, out_item, u_next, v_next,
         belt_in_first_by_ing, belt_out_end_idxs, pipe_in_first_by_ing) = r[:9]
        stage_info[node.item] = {
            "belt_in_first_by_ing": belt_in_first_by_ing,
            "belt_out_end_idxs": belt_out_end_idxs,
            "belt_out_last": belt_out_last,
            "u_next": u_next, "v_next": v_next,
            "out_item": out_item, "node": node,
            "pipe_in_first_by_ing": pipe_in_first_by_ing,
        }
        # Alignement v suivant (ingrédient 0) — identique à la branche bande (back-compat).
        if belt_out_last is not None:
            _, v_out = _to_uv(facing, entities[belt_out_last].x, entities[belt_out_last].y)
            next_node = ordered[i + 1] if i + 1 < len(ordered) else None
            if next_node is not None:
                gnext = geometry.geometry(next_node.machine)
                gcur = geometry.geometry(node.machine)
                half_v_next = (gnext.h / 2.0) if gnext is not None else (
                    (gcur.h / 2.0) if gcur is not None else 1.0)
                # S1g : gap entre étages en main bus pour séparer le FEED (en +v du producteur,
                # merger tree M->1 + virage +v->-u + belts -u) du TAP (en -v du consommateur
                # suivant, splitter prélèvement + transition +u + split tree). Sans gap, le
                # feed (v_out+1..v_out+feed_depth) chevauche le tap (v_target-tap_depth..v_target)
                # -> collisions (CONSTAT S1d/S1f volet C). gap_feed couvre feed_depth + tap_depth.
                # feed_depth = 2*ceil(log2(M)) + 1 (merger tree + virage). tap_depth estimé =
                # 2*ceil(log2(N)) + 2 (prélèvement + split tree), N=belts_in consommateur. N
                # inconnu au placement du producteur -> heuristique N<=M (pessimiste, same item
                # bus équilibré) -> gap_feed = 4*ceil(log2(M)) + 6 (feed_depth + tap_depth + marge).
                # Uniquement si le stage courant produit un item SUR LE BUS (out_item in bus_items)
                # : les mines (ore, non-bus) ne feedent pas le bus -> pas de gap -> transition ore
                # S1a (adjacence drills->étage) préservée (sinon décalage casse la transition ore).
                M = len(belt_out_end_idxs) if belt_out_end_idxs else 1
                gap_feed = (4.0 * max(1, math.ceil(math.log2(M))) + 6.0) if out_item in bus_items else 0.0
                av_cur = v_out + gap_feed + half_v_next - 0.5
            else:
                av_cur = v_out
        au_cur = u_next

    # 2. bbox v global des étages (longueur du bus).
    if entities:
        v_min = min(_to_uv(facing, e.x, e.y)[1] for e in entities)
        v_max = max(_to_uv(facing, e.x, e.y)[1] for e in entities)
    else:
        v_min = v_max = av

    # Pose le bus : 1 lane par item intermédiaire, longe v (FACING_DIR_V), empilées en u
    # côté -u des étages (bus_u0 < au). n_seg belts par lane (couvre v_min..v_max).
    bus_distance = max(1, constraints.bus_distance)
    bus_u0 = au - (len(bus_items) + 1) * bus_distance
    lane_idx_by_item: dict[str, int] = {}
    n_seg = max(1, int(math.ceil(v_max - v_min)))
    for k, item in enumerate(bus_items):
        lane_u = bus_u0 + k * bus_distance
        lane_first = None
        for j in range(n_seg):
            cv = v_min + 0.5 + j
            x, y = _to_xy(facing, lane_u, cv)
            idx = _add(entities, constraints.belt_tier, x, y, FACING_DIR_V[facing],
                       "bus-belt", node_item=item)
            if j == 0:
                lane_first = idx
        lane_idx_by_item[item] = lane_first
        totals[constraints.belt_tier] = totals.get(constraints.belt_tier, 0) + n_seg
        notes.append(f"bus_lane_S1c:{item} (u={lane_u:.1f}, n_seg={n_seg}, positions approx -> S1d)")

    # u de toutes les lanes bus (S1f : _tap_bus_to_consumer perce les lanes intermédiaires
    # via _under_crossing à v=v_cible ; _feed_consumer_to_bus idem pour les belts_out -u).
    bus_lanes_u = [_to_uv(facing, entities[idx].x, entities[idx].y)[0]
                   for idx in lane_idx_by_item.values()]

    # 3. TAP : pour chaque étage consommateur, chaque ingrédient sur le bus ->
    #    splitter tree (1 lane -> belts_in_ing) depuis la lane. Compte splitters.
    for item, info in stage_info.items():
        node = info["node"]
        if node.role == "mine":
            continue
        for ing_name, _ in (node.ingredients or []):
            if ing_name not in lane_idx_by_item:
                continue
            lane = lane_idx_by_item[ing_name]
            target = info["belt_in_first_by_ing"].get(ing_name)
            if target is None:
                continue
            belts_in_ing = 1
            if node.item in stage_log:
                il = stage_log[node.item].ingredients.get(ing_name)
                if il:
                    belts_in_ing = max(1, il["belts_in"])
            connections.append((lane, target, ing_name))
            # S1f : tap circuiterie (splitter prélèvement priority +u + crossings + transition)
            # -> split tree S1e aligné consommateur. Count main bus = n_out (1 prélèvement +
            # n_out-1 tree) ; belts_in=1 -> 1 prélèvement (différent de S1e qui retournait 0).
            n_split = _build_split_tree(lane, belts_in_ing, facing, entities, totals,
                                        constraints.belt_tier, ing_name, notes,
                                        target_idx=target, bus_lanes_u=bus_lanes_u)
            if node.item in stage_log:
                stage_log[node.item].splitters += n_split
            notes.append(f"bus_tap_S1f:{ing_name}->{item} (n_out={belts_in_ing}, {n_split} splitters [1 prélèvement + {max(0, belts_in_ing - 1)} tree], tap circuiterie)")

    # 4. FEED S1g : pour chaque étage producteur d'un item sur le bus -> merger tree côté étage
    #    (M->1, _build_merge_tree conservé, dans le gap libéré par gap_feed) + _route_feed_to_lane
    #    (virage +v->-u + belts -u crossings + sideload -u->+v sur lane, merger gratuit).
    #    Règle le CONSTAT S1f volet C : en S1d le merger tree (v_out+1) collisionnait les belts_in
    #    consommateur (étages alignés sans gap) ; S1g ajoute le gap_feed et route la sortie merger
    #    vers la lane via virage +v->-u (T5) + crossings + sideload -u->+v (T6, merger gratuit).
    #    VALIDÉ live 2026-07-24 (verify_feed_s1g.py) : T5/T6/T7, lane continue, count M-1 conservé.
    for item, info in stage_info.items():
        node = info["node"]
        out_item = info["out_item"]
        if out_item not in lane_idx_by_item:
            continue  # terminal (non sur le bus) -> pas de feed
        belt_out_end_idxs = info["belt_out_end_idxs"]
        if not belt_out_end_idxs:
            continue
        lane = lane_idx_by_item[out_item]
        bus_idx, n_merg = _build_merge_tree(belt_out_end_idxs, facing, entities, totals,
                                            constraints.belt_tier, out_item, notes)
        src = bus_idx if bus_idx is not None else info["belt_out_last"]
        bus_lane_u = _to_uv(facing, entities[lane].x, entities[lane].y)[0]
        _route_feed_to_lane(src, lane, facing, entities, totals, constraints.belt_tier,
                            UNDER_NAME, out_item, notes, bus_lanes_u, bus_lane_u)
        if node.item in stage_log:
            stage_log[node.item].mergers += n_merg
        connections.append((src, lane, out_item))
        notes.append(f"bus_feed_S1g:{item}->{out_item} ({n_merg} mergers [M-1 arbre conservé] + "
                     f"virage +v->-u + belts -u crossings + sideload -u->+v sur lane, feed circuiterie)")

    # 5. Connexion directe drills -> étage pour les items NON sur le bus (ore brute).
    #    Adjacence S1a (back-compat chaîne fer : ore pas sur le bus).
    for item, info in stage_info.items():
        node = info["node"]
        if node.role != "mine":
            continue
        out_item = info["out_item"]
        if out_item in lane_idx_by_item:
            continue  # déjà géré par le feed (ore sur le bus)
        if info["belt_out_last"] is None:
            continue
        # Cherche l'étage consommateur de out_item.
        for other_item, other_info in stage_info.items():
            if other_item == item:
                continue
            on = other_info["node"]
            if on.role == "mine":
                continue
            target = other_info["belt_in_first_by_ing"].get(out_item)
            if target is not None:
                connections.append((info["belt_out_last"], target, out_item))
                _place_transition(info["belt_out_last"], target, facing, entities, totals,
                                  constraints.belt_tier, out_item, notes)
                break

    # S4b : détection per-entité (précise) si terrain_check. Passe AVANT le check post-hoc
    # global (imprécis). _occ_terrain couvre obstacles + water + out-of-map (superset du
    # post-hoc qui ne voit que obstacles en bbox-vs-bbox).
    if request.constraints.terrain_check and entities:
        hits = []
        for e in entities:
            if getattr(e, "skip", False):
                continue
            g = geometry.geometry(e.name)
            w = g.w if g else 1.0
            h = g.h if g else 1.0
            kind = _occ_terrain(request.terrain, e.x, e.y, w, h)
            if kind:
                hits.append((e, kind))
        if hits:
            notes.append(f"obstacle_blocking:per_entity:{len(hits)} hits kind={hits[0][1]}")
            if feasibility == "ok":
                feasibility = "obstacle_blocking"

    # Obstacles (S0 : note si bbox intersecte un obstacle — pas de contournement, S4).
    # S4b : skippé si terrain_check (la détection per-entité ci-dessus est un superset plus
    # précis ; évite les doublons de notes). Back-compat : terrain_check=False -> S3d inchangé.
    if (not request.constraints.terrain_check) and request.terrain.obstacles and entities:
        ent_bbox = _bbox_of(entities)
        for obs in request.terrain.obstacles:
            if _bbox_intersects(ent_bbox, (float(obs[0]), float(obs[1]), float(obs[2]), float(obs[3]))):
                notes.append(f"obstacle_blocking: bbox étage intersecte obstacle {obs}")
                if feasibility == "ok":
                    feasibility = "obstacle_blocking"
                break

    return LayoutPlan(request, entities, connections, _bbox_of(entities),
                      stage_log, totals, feasibility, notes)


# ===== Plan =====

def _plan_core(request: LayoutRequest, geometry: GeometryBase) -> LayoutPlan:
    """Produit le blueprint positionné + dimensionné au débit. Déterministe, sans LLM.

    S4b : corps du planner (back-compat S3d). Le dispatcher public `plan()` route vers
    ici (terrain_check=False/replan_budget=0) ou vers _plan_with_replan (replan auto)."""
    splan = request.plan
    constraints = request.constraints
    entities: list[LayoutEntity] = []
    connections: list[tuple[int, int, str]] = []
    stage_log: dict[str, StageLogistics] = {}
    totals: dict[str, int] = {}
    notes: list[str] = []
    feasibility = "ok"

    # Propager l'infaisabilité du solveur.
    if splan.feasibility != "ok":
        return LayoutPlan(request, entities, connections, (0.0, 0.0, 0.0, 0.0),
                          stage_log, totals, f"solver:{splan.feasibility}",
                          ["plan solveur infaisable"])

    belt_speed = THROUGHPUTS.get(constraints.belt_tier, 0.0)
    # S1a : débit inserter via la fonction affine (DIP). swing = pickup+drop de l'inserter
    # tier (géométrie fine). Avec k=0 (INSERTER_AFFINE par défaut), inserter_throughput
    # retourne THROUGHPUTS[inserter_tier] -> back-compat stricte S0.
    gins_global = geometry.geometry(constraints.inserter_tier)
    swing_global = _swing_for(gins_global, constraints.swing_distance)
    tp_fn = request.inserter_throughput_fn or inserter_throughput
    inserter_tp = tp_fn(constraints.inserter_tier, swing_global)
    if belt_speed <= 0:
        notes.append(f"throughput inconnu: {constraints.belt_tier}")
        feasibility = f"missing_geometry:{constraints.belt_tier}"
    if inserter_tp <= 0:
        notes.append(f"throughput inconnu: {constraints.inserter_tier}")
        if feasibility == "ok":
            feasibility = f"missing_geometry:{constraints.inserter_tier}"

    # Géométries requises (machines + tiers logistique).
    required = {constraints.belt_tier, constraints.inserter_tier, constraints.pole_tier}
    for n in splan.nodes:
        required.add(n.machine)
    for name in required:
        if geometry.geometry(name) is None:
            notes.append(f"geometry manquante: {name}")
            if feasibility == "ok":
                feasibility = f"missing_geometry:{name}"

    # Patches pour chaque feuille (resource extraite).
    patches_by_res = {p.resource: p for p in request.terrain.patches}
    for leaf in splan.leaves:
        # S2a : water est une tile (terrain.water), pas un resource-entity.
        if leaf.item == "water":
            if not request.terrain.water:
                notes.append(f"patch manquant: {leaf.item}")
                if feasibility == "ok":
                    feasibility = f"missing_patch:{leaf.item}"
        elif leaf.item not in patches_by_res:
            notes.append(f"patch manquant: {leaf.item}")
            if feasibility == "ok":
                feasibility = f"missing_patch:{leaf.item}"

    # Ordre topologique (feuilles d'abord, puis par profondeur croissante).
    # S2b-1 : sinks (role="store", co-produits orphelins) exclus du tri (leur depth=0 les
    # placerait avant leur source) et réinsérés juste après leur source (source_item) pour
    # que out_idx_by_item[cp] (port output co-produit de la source) existe quand on les
    # connecte vers le storage-tank.
    # S2b-2 : sinks role="power" (steam-engine, co-produit orphelin steam) même traitement.
    depth = _depths(splan)
    _ordered_main = [n for n in splan.nodes if n.role not in ("store", "power")]
    _ordered_main.sort(key=lambda n: (depth[n.item], n.item))
    _sinks_by_src: dict[str, list] = {}
    for n in splan.nodes:
        if n.role in ("store", "power"):
            _sinks_by_src.setdefault(n.source_item, []).append(n)
    ordered: list = []
    for n in _ordered_main:
        ordered.append(n)
        ordered.extend(_sinks_by_src.get(n.item, []))

    facing = request.facing
    au, av = _to_uv(facing, request.anchor[0], request.anchor[1])

    # S1c : dispatch main bus (layout alternatif). bus_layout=False (défaut) -> branche
    # bande S1a/S1b ci-dessous (inchangée, back-compat stricte).
    if constraints.bus_layout:
        return _plan_bus_core(request, geometry, splan, constraints, belt_speed, inserter_tp,
                         tp_fn, facing, au, av, ordered, patches_by_res, notes, feasibility)

    out_idx_by_item: dict[str, int] = {}   # S1b : bus aval (post-merger) par item produit
    sink_av_by_cp: dict[str, float] = {}   # S2d : av du sink par co-produit (aligné port)
    # S4b : cascade_offset_v appliqué une seule fois au 1er étage machine (propage à toute la
    # cascade via v_out). Flag pour éviter l'accumulation (l'offset est baké dans v_out -> se
    # propage naturellement aux étages suivants, pas besoin de le ré-ajouter).
    offset_applied = False

    for i, node in enumerate(ordered):
        if node.role in ("store", "power"):
            # S2b-1/S2b-2 : sink co-produit orphelin -> storage-tank (role="store") ou
            # steam-engine (role="power"). Connexion depuis le port output co-produit de
            # la source (out_idx_by_item[cp], enregistré par la source via
            # pipe_out_by_coproduct) vers le pipe input du sink.
            # S2d : av du sink aligné au v de son port (sink_av_by_cp, registered par la
            # source via le 11e élément). Fallback av (mono-produit / S2b-1 back-compat).
            av_sink = sink_av_by_cp.get(node.item, av)
            r = _place_fluid_sink(node, geometry, constraints, facing, au, av_sink,
                                  entities, totals, stage_log, notes)
            if r is not None:
                (belt_in_first, belt_out_last, ing_name, out_item, u_next, v_next,
                 belt_in_first_by_ing, belt_out_end_idxs, pipe_in_first_by_ing) = r[:9]
                cp = node.item
                prod_bus = out_idx_by_item.get(cp)
                target = pipe_in_first_by_ing.get(cp)
                if prod_bus is not None and target is not None:
                    connections.append((prod_bus, target, cp))
                    _place_pipe_segment(prod_bus, target, facing, entities, totals,
                                        constraints.pipe_tier, cp, notes)
                au = u_next
            continue
        if node.role == "mine":
            if node.transport == "pipe":
                # S2a : feuille fluide. water -> offshore-pump (sur tuile d'eau),
                # sinon pumpjack (sur patch crude-oil). Pas de belt/inserter.
                if node.item == "water":
                    water_bbox = request.terrain.water[0] if request.terrain.water else None
                    if water_bbox is None:
                        notes.append(f"no_water_patch:{node.item}")
                        continue
                    r = _place_offshore_pump(node, water_bbox, geometry, constraints,
                                             facing, au, av, entities, totals, stage_log, notes)
                else:
                    patch = patches_by_res.get(node.item)
                    if patch is None:
                        continue
                    r = _place_pumpjacks(node, patch, geometry, constraints, facing, au, av,
                                         entities, totals, stage_log, notes)
            else:
                patch = patches_by_res.get(node.item)
                if patch is None:
                    continue
                r = _place_drills(node, patch, geometry, constraints, facing, au, av,
                                  entities, totals, stage_log, notes)
        else:
            # S2b-1 : coproduct_items = co-produits orphelins de ce node source (sinks
            # insérés juste après dans ordered). _place_stage pose 1 pipe output par cp.
            # S4b : appliquer cascade_offset_v une seule fois, au 1er étage machine. Propage
            # à toute la cascade via v_out (av = v_out + half_v_next - 0.5 baké l'offset). Les
            # étages mine (drills/pumpjacks) sont sur patch.bbox (indépendant de l'anchor/av)
            # -> l'offset ne les affecte pas, seulement la cascade aval (contournement).
            if not offset_applied and constraints.cascade_offset_v != 0:
                av += constraints.cascade_offset_v
                offset_applied = True
            cp_items = [s.item for s in _sinks_by_src.get(node.item, [])]
            r = _place_stage(node, geometry, constraints, belt_speed, inserter_tp, tp_fn,
                             facing, au, av, entities, totals, stage_log, notes,
                             coproduct_items=cp_items,
                             pipe_throughput_fn=request.pipe_throughput_fn)
        if r is None:
            continue
        (belt_in_first, belt_out_last, ing_name, out_item, u_next, v_next,
         belt_in_first_by_ing, belt_out_end_idxs, pipe_in_first_by_ing) = r[:9]
        pipe_out_by_coproduct = r[9] if len(r) > 9 else {}
        sink_av_by_coproduct = r[10] if len(r) > 10 else {}

        # S1b : merger en queue si belts_out > 1 -> 1 belt "bus" le long de u.
        belts_out_n = stage_log[node.item].belts_out_per_stage if node.item in stage_log else 1
        if constraints.transition_belts and belts_out_n > 1 and belt_out_end_idxs:
            bus_idx, n_merg = _build_merge_tree(belt_out_end_idxs, facing, entities,
                                                 totals, constraints.belt_tier,
                                                 out_item, notes)
            if node.item in stage_log:
                stage_log[node.item].mergers += n_merg
        else:
            bus_idx = belt_out_last
        if bus_idx is not None:
            out_idx_by_item[out_item] = bus_idx
        # S2a : bus fluide = pipe de collecte output (déjà posé par _place_stage,
        # pas de merger fluide en S2a). On l'enregistre comme point d'attache aval.
        pipes_out_n = stage_log[node.item].pipes_out_per_stage if node.item in stage_log else 0
        if pipes_out_n > 0 and belt_out_last is not None:
            out_idx_by_item[out_item] = belt_out_last   # idx du pipe output
        # S2b-1 : enregistrer out_idx_by_item pour chaque co-produit orphelin (port
        # output distinct de la source) -> connexion vers storage-tank quand le sink
        # est traité (branch role="store" ci-dessus).
        for cp, cp_idx in pipe_out_by_coproduct.items():
            out_idx_by_item[cp] = cp_idx
        # S2d : enregistrer l'av aligné par port pour chaque co-produit -> sink dispatch.
        for cp, av_cp in sink_av_by_coproduct.items():
            sink_av_by_cp[cp] = av_cp

        # S1b : connexion PAR INGRÉDIENT (multi-ingrédients). Chaque ingrédient est
        # alimenté par le bus du producteur (out_idx_by_item[ing_name]). 1->1 :
        # adjacence S1a (back-compat). belts_in>1 : splitter tree en tête.
        # S2a : ingrédient fluide -> _place_pipe_segment (pas de splitter/merger fluide).
        if node.role != "mine":
            for ing_name, _ in (node.ingredients or []):
                ing_fluid = ing_name in FLUID_ITEMS
                if ing_fluid:
                    prod_bus = out_idx_by_item.get(ing_name)
                    target = pipe_in_first_by_ing.get(ing_name)
                else:
                    prod_bus = out_idx_by_item.get(ing_name)
                    target = belt_in_first_by_ing.get(ing_name)
                if prod_bus is None or target is None:
                    continue
                connections.append((prod_bus, target, ing_name))
                if ing_fluid:
                    # S2a : pipe direct machine->machine (chaîne 1->1, pas de merger).
                    _place_pipe_segment(prod_bus, target, facing, entities, totals,
                                        constraints.pipe_tier, ing_name, notes)
                    continue
                belts_in_ing = 1
                if node.item in stage_log:
                    il = stage_log[node.item].ingredients.get(ing_name)
                    if il:
                        belts_in_ing = max(1, il["belts_in"])
                if constraints.transition_belts:
                    if belts_in_ing > 1:
                        n_split = _build_split_tree(prod_bus, belts_in_ing, facing,
                                                     entities, totals,
                                                     constraints.belt_tier, ing_name, notes)
                        if node.item in stage_log:
                            stage_log[node.item].splitters += n_split
                    else:
                        # 1->1 : adjacence S1a (back-compat chaîne linéaire).
                        _place_transition(prod_bus, target, facing, entities, totals,
                                          constraints.belt_tier, ing_name, notes)

        # S1a : aligner l'étage SUIVANT sur l'ingrédient 0 (back-compat chaîne
        # linéaire). belt_in_first(next) doit tomber en face de belt_out_last(cur).
        if belt_out_last is not None:
            _, v_out = _to_uv(facing, entities[belt_out_last].x, entities[belt_out_last].y)
            next_node = ordered[i + 1] if i + 1 < len(ordered) else None
            if next_node is not None:
                gnext = geometry.geometry(next_node.machine)
                gcur = geometry.geometry(node.machine)
                half_v_next = (gnext.h / 2.0) if gnext is not None else (
                    (gcur.h / 2.0) if gcur is not None else 1.0)
                av = v_out + half_v_next - 0.5
            else:
                av = v_out
        au = u_next

    # S4b : détection per-entité (précise) si terrain_check. Passe AVANT le check post-hoc
    # global (imprécis). _occ_terrain couvre obstacles + water + out-of-map (superset du
    # post-hoc qui ne voit que obstacles en bbox-vs-bbox).
    if request.constraints.terrain_check and entities:
        hits = []
        for e in entities:
            if getattr(e, "skip", False):
                continue
            g = geometry.geometry(e.name)
            w = g.w if g else 1.0
            h = g.h if g else 1.0
            kind = _occ_terrain(request.terrain, e.x, e.y, w, h)
            if kind:
                hits.append((e, kind))
        if hits:
            notes.append(f"obstacle_blocking:per_entity:{len(hits)} hits kind={hits[0][1]}")
            if feasibility == "ok":
                feasibility = "obstacle_blocking"

    # Obstacles (S0 : note si une entité tombe dans un bbox bloquant — pas de contournement, S4).
    # S4b : skippé si terrain_check (la détection per-entité ci-dessus est un superset plus
    # précis ; évite les doublons de notes). Back-compat : terrain_check=False -> S3d inchangé.
    if (not request.constraints.terrain_check) and request.terrain.obstacles and entities:
        ent_bbox = _bbox_of(entities)
        for obs in request.terrain.obstacles:
            if _bbox_intersects(ent_bbox, (float(obs[0]), float(obs[1]), float(obs[2]), float(obs[3]))):
                notes.append(f"obstacle_blocking: bbox étage intersecte obstacle {obs}")
                if feasibility == "ok":
                    feasibility = "obstacle_blocking"
                break

    return LayoutPlan(request, entities, connections, _bbox_of(entities),
                      stage_log, totals, feasibility, notes)


# ===== S4b : dispatcher plan() + replan auto déterministe =====

def plan(request: LayoutRequest, geometry: GeometryBase) -> LayoutPlan:
    """Produit le blueprint positionné + dimensionné au débit. Déterministe, sans LLM.

    S4b : dispatcher public. Si terrain_check ou replan_budget>0 -> _plan_with_replan
    (replan auto déterministe : shift anchor en v / pivot facing, règles fixes, avant
    handoff obstacle_blocking -> FactoryBuilder S4c). Sinon -> _plan_core (back-compat S3d,
    check post-hoc global inchangé). Signature inchangée (back-compat stricte)."""
    c = request.constraints
    if c.terrain_check or c.replan_budget > 0:
        return _plan_with_replan(request, geometry)
    return _plan_core(request, geometry)


def _plan_with_replan(request: LayoutRequest, geometry: GeometryBase) -> LayoutPlan:
    """Replan auto déterministe : essaie shifts anchor en v puis rotations facing (règles
    fixes, budget borné) jusqu'à feasibility="ok". Si épuisé -> retourne le "best" layout
    (max d'entités placables) avec feasibility="obstacle_blocking" (handoff FactoryBuilder
    S4c). Déterministe pur (pas d'LLM) : l'arbitrage lourd = FactoryBuilder.

    Le contournement §4.4 (offset perpendiculaire) est réalisé par le décalage de
    cascade_offset_v (offset uniforme au 1er étage machine, propage à toute la cascade via
    v_out), PAS par un shift d'anchor — pour les chaînes minières l'anchor est ignoré (les
    étages suivent patch.bbox). cascade_offset_v s'applique au 1er étage machine et propage
    (alignement v S1a préservé : av = v_out + half_v_next - 0.5 baké l'offset). Les drills
    restent sur patch.bbox (indépendant de l'offset), la belt de collecte s'allonge."""
    budget = request.constraints.replan_budget
    tried: set[tuple[int, int]] = set()  # (facing, cascade_offset_v) — évite boucle infinie
    current = request
    best: Optional[LayoutPlan] = None
    for _attempt in range(budget + 1):
        core = _plan_bus_core if current.constraints.bus_layout else _plan_core
        lp = core(current, geometry)
        if lp.feasibility == "ok":
            return lp
        if lp.feasibility != "obstacle_blocking":
            return lp  # missing_geometry/patch/etc. — pas terrain, pas de replan
        best = lp if (best is None or len(lp.entities) > len(best.entities)) else best
        tried.add((current.facing, current.constraints.cascade_offset_v))
        nxt = _next_replan_attempt(request, tried)
        if nxt is None:
            break
        current = nxt
    # Règles épuisées ou budget atteint -> best layout (obstacle_blocking) + note de handoff.
    if best is not None:
        if best.feasibility == "obstacle_blocking":
            best.notes.append(f"replan_exhausted: {len(tried)} tentatives (handoff FactoryBuilder)")
        return best
    return _plan_core(request, geometry)


def _next_replan_attempt(request: LayoutRequest, tried: set) -> Optional[LayoutRequest]:
    """Règles fixes déterministes (ordre FIXE) pour le prochain essai de replan. Candidats
    ABSOLUS depuis la requête originale (pas cumulatifs) — chaque essai est une variation
    indépendante de (facing, cascade_offset_v) depuis l'origine, `tried` évite les revisites.
    1-4 : offsets cascade perpendiculaires (±bypass_offset_v, ±2*bypass_offset_v) —
          contournement. Levier = cascade_offset_v (PAS l'anchor : pour les chaînes minières
          l'anchor est ignoré, les étages suivent patch.bbox ; cascade_offset_v s'applique au
          1er étage machine et propage à toute la cascade via v_out).
    5-7 : pivots facing (±90°, 180°), offset d'origine — si les shifts v sont insuffisants.
    Retourne un nouveau LayoutRequest (facing/constraints.cascade_offset_v modifiés via
    dataclasses.replace) non déjà essayé, ou None si règles épuisées. Garde-fous :
    |offset| ≤ bypass_max_offset_v ; constructible_zone set -> rejette anchor hors zone.
    Évite boucle infinie via `tried` (clé = (facing, offset))."""
    c = request.constraints
    ov = c.bypass_offset_v
    facing0 = request.facing
    base_off = c.cascade_offset_v
    candidates = [
        ("shift+v", base_off + ov, facing0),
        ("shift-v", base_off - ov, facing0),
        ("shift+2v", base_off + 2 * ov, facing0),
        ("shift-2v", base_off - 2 * ov, facing0),
        ("rot+90", base_off, _rotate_facing(facing0, +1)),
        ("rot-90", base_off, _rotate_facing(facing0, -1)),
        ("rot180", base_off, _rotate_facing(facing0, 2)),
    ]
    for label, off, fac in candidates:
        key = (fac, off)
        if key in tried:
            continue
        # Garde-fou shift : |offset| ≤ bypass_max_offset_v.
        if label.startswith("shift") and abs(off) > c.bypass_max_offset_v:
            continue
        # constructible_zone : rejette anchor hors zone (garde-fou grossier ; les étages sont
        # à l'est du patch, la zone devrait les contenir — vérif fine = S4c FactoryBuilder).
        if c.constructible_zone is not None:
            z = c.constructible_zone
            if not (z[0] <= request.anchor[0] <= z[2] and z[1] <= request.anchor[1] <= z[3]):
                continue
        new_c = replace(c, cascade_offset_v=off)
        return replace(request, facing=fac, constraints=new_c)
    return None


# ===== Diagnostics =====

def plan_summary(lp: LayoutPlan) -> str:
    """Résumé texte du LayoutPlan (audit / logs)."""
    lines = [
        f"LayoutPlan: feasibility={lp.feasibility}",
        f"  entités: {len(lp.entities)} | connexions: {len(lp.connections)}",
        f"  bbox: ({lp.bbox[0]:.1f},{lp.bbox[1]:.1f}) -> ({lp.bbox[2]:.1f},{lp.bbox[3]:.1f})",
        "  totals: " + ", ".join(f"{k}={v}" for k, v in sorted(lp.totals.items())),
    ]
    if lp.stage_logistics:
        lines.append("  stage_logistics:")
        for item, sl in lp.stage_logistics.items():
            lines.append(
                f"    {item}: eff={sl.rate_effective:.2f}/s  phase={sl.phase}  "
                f"belts in/out={sl.belts_in_per_stage}/{sl.belts_out_per_stage}  "
                f"pipes in/out={sl.pipes_in_per_stage}/{sl.pipes_out_per_stage}  "
                f"ins in/out/machine={sl.inserters_in_per_machine}/"
                f"{sl.inserters_out_per_machine}"
                + ("  INSUFFISANT" if sl.inserter_insufficient else "")
                + ("  OVERFLOW" if sl.belt_overflow else "")
            )
    if lp.notes:
        lines.append("  notes: " + "; ".join(lp.notes))
    return "\n".join(lines)