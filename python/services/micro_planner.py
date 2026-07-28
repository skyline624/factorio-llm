"""MicroPlanner — chaîne bootstrap compacte (drill + inserter + furnace).

Complément déterministe du LayoutPlanner pour le cas « production minimale bootstrap ».
Le LayoutPlanner produit des usines main-bus scalables : pour 0.3 iron-plate/s il émet
~40 entités (2 drills + 34 belts + 2 inserters + 1 furnace + 1 pole) car sa belt de
collecte longe TOUT le bord du patch (longueur = lv2-lv1, dimensionnée par la taille du
patch, pas par le débit). Le MicroPlanner produit une micro-chaîne compacte de 3 entités
(1 drill sur le gisement → 1 inserter → 1 furnace), sans belt de collecte, sans pole,
sans bus. À utiliser après le flux manuel de bootstrap (les iron-plates du flux manuel
servent à crafter l'inserter — chicken-egg résolu).

Déterministe pur (pas d'LLM) ; la vérification terrain (can_place) reste à l'executor
(Coordinator P2). Frontière §7 spec respectée : le planner calcule, l'executor adapte.

Règle mémoire feedback-production-bootstrap-p2-llm §2 : drop-direct drill→furnace
IMPOSSIBLE en Factorio 2.0. L'inserter au milieu est la solution ; le MicroPlanner
l'intègre par construction.

GÉOMÉTRIE MESURÉE (measure_entity, live 2026-07 — ne pas la ré-inventer)
-----------------------------------------------------------------------
burner-mining-drill : size 2×2, pose sur position ENTIÈRE ;
    drop_position = centre + u*1.25 + perp*0.5   (perp = (uy, -ux), rotation -90°)
    -> la TUILE de drop a pour centre  centre + u*1.5 + perp*0.5
    Le drop n'est donc PAS sur l'axe du drill : il est décalé d'une demi-tuile de côté.
burner-inserter     : 1×1, pose sur CENTRE de tuile (x.5) ;
    pickup = centre + v*1.0, drop = centre - v*1.1  (v = FACING_UNIT[direction])
stone-furnace       : size 2×2, pose sur position ENTIÈRE.

Le premier jet supposait drill 3×3 et un drop centré : la chaîne sortait décalée d'UNE
tuile. Constat live : le drill posé était `working` puis `waiting_for_space_in_destination`,
un `item-on-ground` s'accumulait sur sa vraie drop tile, l'inserter restait
`waiting_for_source_items` et le four ne fondait rien. Toutes les positions sont désormais
dérivées des mesures ci-dessus et SNAPPÉES sur la grille légale (`_snap`) : `can_place_check`
teste ainsi exactement la position où `create_entity` posera.

Layout (facing south, u = +y, perp = +x, drill/furnace 2×2) :
  drill (dx,dy) 2×2            [emprise dy-1..dy+1]
    ↓ drop tile (dx+0.5, dy+1.5)   [hors emprise, décalée de +0.5 en x]
  inserter (dx+0.5, dy+2.5) 1×1    [pickup = drop tile du drill, drop = tuile suivante]
    ↓ tuile de dépôt (dx+0.5, dy+3.5)
  furnace (dx, dy+4) 2×2       [emprise dy+3..dy+5 : son bord amont EST la tuile de dépôt]

Aucun chevauchement : drill dy-1..dy+1, inserter dy+2..dy+3, furnace dy+3..dy+5.

Parallèle :
  LayoutPlanner : BOM + terrain -> usine main-bus scalable (dimensionnée au débit + taille patch)
  MicroPlanner  : gisement       -> micro-chaîne 3 entités (dimensionnée par la géométrie, pas le débit)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from services.layout_planner import (
    LayoutEntity, FACING_UNIT, ResourcePatch,
)
from services.knowledge import GeometryBase

# Directions Factorio : 0=N(-Y), 2=E(+X), 4=S(+Y), 6=W(-X). (Y croît vers le sud.)


@dataclass
class MicroRequest:
    """Requête du MicroPlanner : un gisement + orientation + tiers (tout-burner par défaut).

    `drill_size` et `furnace_size` sont des emprises RÉELLES en tuiles, MESURÉES en jeu
    (`measure_entity` : size w=2 h=2 pour burner-mining-drill comme pour stone-furnace,
    et les deux se posent sur position entière — ce qui confirme l'emprise paire).
    Une taille paire pose sur position entière, une taille impaire sur centre de tuile
    (`_snap`) ; la parité pilote aussi le décalage latéral du drop du drill.
    Defaults = burner-mining-drill (2×2) + stone-furnace (2×2), tout-burner, aucun pole/belt.
    """
    patch: ResourcePatch                              # gisement (depuis scan_patch)
    facing: int = 4                                   # côté de drop du drill (0=N, 2=E, 4=S, 6=W)
    drill_tier: str = "burner-mining-drill"
    inserter_tier: str = "burner-inserter"            # burner = pas de pole
    furnace_tier: str = "stone-furnace"
    drill_size: int = 2                               # emprise RÉELLE drill (mesurée 2×2)
    furnace_size: int = 2                             # emprise RÉELLE furnace (mesurée 2×2)
    anchor: Optional[tuple[float, float]] = None      # position drill ; None = 1re tuile ore du patch


@dataclass
class MicroPlan:
    """Plan compact d'une micro-chaîne bootstrap (3 entités, tout-burner).

    Mêmes champs shape que LayoutPlan (entities/connections/totals/bbox/feasibility/notes)
    mais sans `request: LayoutRequest` (pas d'executor centralisé existant — l'executor P2
    itère `entities` directement). `feasibility` est toujours "ok" (pas de terrain check ;
    l'executor fait can_place_check + retry de position).
    """
    entities: list[LayoutEntity] = field(default_factory=list)
    connections: list[tuple[int, int, str]] = field(default_factory=list)  # (from_idx, to_idx, item)
    totals: dict[str, int] = field(default_factory=dict)        # {entity_name: count}
    bbox: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.0)
    feasibility: str = "ok"
    notes: list[str] = field(default_factory=list)


def _add(entities, name, x, y, direction, role, node_item="") -> int:
    """Append une LayoutEntity, retourne son idx (même pattern que layout_planner._add)."""
    entities.append(LayoutEntity(name, x, y, direction, role, node_item))
    return len(entities) - 1


def _snap(v: float, size: int) -> float:
    """Position légale sur la grille Factorio pour une entité d'emprise `size` tuiles.

    Emprise paire -> position entière (coin de 4 tuiles) ; impaire -> centre de tuile (x.5).
    `create_entity` snappe de toute façon ; on le fait ICI pour que `can_place_check` teste
    la position réellement occupée. Sans ça les deux divergent d'une demi-tuile et la chaîne
    se retrouve désalignée (constat live : inserter demandé en -69.0, posé en -68.5).
    """
    return float(round(v)) if size % 2 == 0 else math.floor(v) + 0.5


def plan_micro(request: MicroRequest, geometry: Optional[GeometryBase] = None) -> MicroPlan:
    """Planifie une micro-chaîne drill→inserter→furnace (3 entités, tout-burner).

    Déterministe pur. `feasibility='ok'` toujours (pas de terrain check — l'executor fait
    `can_place_check` + retry de position si collision). `geometry` est optionnel et conservé
    pour extension future (non requis au calcul : tailles via `MicroRequest.drill_size`/
    `furnace_size`, inserter 1×1 pickup/drop 1.0 constants).

    Paramètres
    ----------
    request : MicroRequest
        Gisement (`patch`), orientation (`facing` = côté de drop), tiers (tout-burner défaut).
    geometry : GeometryBase, optionnel
        Non requis au calcul (réservé extension/notes).

    Retourne
    --------
    MicroPlan
        3 entités alignées le long de FACING_UNIT[facing] + connections (drill→ins→furnace)
        + totals. `feasibility='missing_geometry'` si `facing` hors {0,2,4,6}.
    """
    facing = request.facing
    if facing not in FACING_UNIT:
        return MicroPlan(
            feasibility="missing_geometry",
            notes=[f"facing invalide: {facing} (attendu 0/2/4/6)"],
        )
    ux, uy = FACING_UNIT[facing]

    # 1. Position du drill (centre, coords map alignées). Anchor ou 1re tuile ore du patch.
    if request.anchor is not None:
        dx, dy = float(request.anchor[0]), float(request.anchor[1])
    elif request.patch.tiles:
        tx, ty = request.patch.tiles[0]
        dx, dy = float(tx), float(ty)
    else:
        x1, y1, x2, y2 = request.patch.bbox
        dx, dy = float((x1 + x2) / 2), float((y1 + y2) / 2)

    dx, dy = _snap(dx, request.drill_size), _snap(dy, request.drill_size)

    demi_d = request.drill_size / 2.0      # 2×2 -> 1.0 (emprise dy-1..dy+1)
    demi_f = request.furnace_size / 2.0
    # Décalage latéral du drop : mesuré à 0.5 sur les entités d'emprise PAIRE (leur centre
    # est un coin de tuiles, le drop se cale sur la colonne de tuiles d'un seul côté).
    # Emprise impaire (centre = centre de tuile) -> drop dans l'axe, décalage nul.
    lat_d = 0.5 if request.drill_size % 2 == 0 else 0.0
    lat_f = 0.5 if request.furnace_size % 2 == 0 else 0.0
    px, py = uy, -ux                       # perpendiculaire à u (rotation -90°)

    # 2. Centre de la TUILE de drop du drill = centre + u*(demi+0.5) + perp*lat.
    #    Mesuré : drop_position = centre + u*1.25 + perp*0.5 pour un drill 2×2 ; la tuile
    #    qui la contient a pour centre + u*1.5 + perp*0.5. Elle est hors emprise (demi=1).
    drop_x = dx + ux * (demi_d + 0.5) + px * lat_d
    drop_y = dy + uy * (demi_d + 0.5) + py * lat_d

    # 3. Inserter (1×1) : 1 tuile après la drop tile, de sorte que son pickup TOMBE dessus.
    #    En Factorio 2.0, `direction` = direction de PICKUP (mesuré : inserter north ->
    #    pickup = centre+(0,-1), drop = centre+(0,+1.1)). L'inserter doit ramasser la drop
    #    tile du drill (côté -u) et déposer côté furnace (+u) -> direction = OPPOSÉ de facing
    #    (il « regarde » vers le drill). Bug initial : FACING_DIR_U[facing] l'orientait vers
    #    +u -> il ramassait la tuile vide côté furnace et déposait sur la drop tile du drill.
    ins_x = _snap(drop_x + ux * 1.0, 1)
    ins_y = _snap(drop_y + uy * 1.0, 1)
    ins_dir = (facing + 4) % 8       # opposé de facing : pickup vers la drop tile du drill (-u)

    # 4. Furnace : sa tuile de bord amont doit être la tuile où l'inserter DÉPOSE
    #    (= ins + u*1, mesuré drop à u*1.1 donc même tuile). Centre = tuile de dépôt
    #    + u*(demi_f - 0.5), recentré latéralement sur l'axe du drill (-perp*lat_f).
    put_x, put_y = ins_x + ux * 1.0, ins_y + uy * 1.0
    furnace_x = _snap(put_x + ux * (demi_f - 0.5) - px * lat_f, request.furnace_size)
    furnace_y = _snap(put_y + uy * (demi_f - 0.5) - py * lat_f, request.furnace_size)

    # 5. Entités (drill orienté facing = drop côté facing ; furnace carré non orienté).
    entities: list[LayoutEntity] = []
    drill_idx = _add(entities, request.drill_tier, dx, dy, facing, "drill",
                     node_item=request.patch.resource)
    ins_idx = _add(entities, request.inserter_tier, ins_x, ins_y, ins_dir, "inserter",
                   node_item=request.patch.resource)
    furnace_idx = _add(entities, request.furnace_tier, furnace_x, furnace_y, 0, "machine",
                       node_item="")  # furnace = étage smelt (produit la plaque, pas le minerai)

    # 6. Connections (graphe de flux : drill -> inserter -> furnace).
    connections = [
        (drill_idx, ins_idx, request.patch.resource),
        (ins_idx, furnace_idx, request.patch.resource),
    ]

    # 7. Totals.
    totals = {request.drill_tier: 1, request.inserter_tier: 1, request.furnace_tier: 1}

    # 8. Bbox (sur les centres ; l'executor affinera avec les collisions réelles).
    xs = [e.x for e in entities]
    ys = [e.y for e in entities]
    bbox = (min(xs), min(ys), max(xs), max(ys))

    notes = [
        f"micro_chain: drill({request.drill_tier})@({dx},{dy}) facing={facing} "
        f"-> inserter@({ins_x},{ins_y}) -> furnace@({furnace_x},{furnace_y})",
        f"drop_tile={drop_x},{drop_y} -> pickup inserter ; dépôt inserter={put_x},{put_y} "
        f"-> bord amont furnace ; drill_size={request.drill_size} "
        f"furnace_size={request.furnace_size} (emprises mesurées, positions snappées)",
        "terrain_check=off (executor fait can_place_check + retry de position)",
    ]

    return MicroPlan(
        entities=entities, connections=connections, totals=totals,
        bbox=bbox, feasibility="ok", notes=notes,
    )