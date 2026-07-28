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

Règle mémoire feedback-production-bootstrap-p2-llm §2 : drop-direct drill→furnace 3×3
IMPOSSIBLE en Factorio 2.0 (le furnace 3×3 chevauche le drill 3×3). L'inserter au milieu
est la solution ; le MicroPlanner l'intègre par construction.

Layout (facing south, u = FACING_UNIT[4] = +y) :
  drill (dx,dy) 3×3            [emprise dy-1..dy+1]
    ↓ drop tile (dx, dy+2)     [bord aval + 1, hors emprise]
  inserter (dx, dy+3) 1×1      [pickup 1.0 atteint dy+2, drop 1.0 atteint dy+4]
    ↓
  furnace (dx, dy+5) 3×3       [emprise dy+4..dy+6, bord -u (dy+4) = drop inserter]

Aucun chevauchement : drill dy-1..dy+1, inserter dy+3, furnace dy+4..dy+6.

Parallèle :
  LayoutPlanner : BOM + terrain -> usine main-bus scalable (dimensionnée au débit + taille patch)
  MicroPlanner  : gisement       -> micro-chaîne 3 entités (dimensionnée par la géométrie, pas le débit)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from services.layout_planner import (
    LayoutEntity, FACING_UNIT, FACING_DIR_U, ResourcePatch,
)
from services.knowledge import GeometryBase

# Directions Factorio : 0=N(-Y), 2=E(+X), 4=S(+Y), 6=W(-X). (Y croît vers le sud.)


@dataclass
class MicroRequest:
    """Requête du MicroPlanner : un gisement + orientation + tiers (tout-burner par défaut).

    `drill_size` et `furnace_size` sont des tailles RÉELLES (tuiles de collision), pas les
    métadonnées `describe().size` (qui retournent w=2 h=2 pour le burner-mining-drill alors
    que sa collision est 3×3 en Factorio 2.0 — cf. mémoire feedback-production-bootstrap-p2-llm).
    Defaults = burner-mining-drill (3×3) + stone-furnace (3×3), tout-burner, aucun pole/belt.
    """
    patch: ResourcePatch                              # gisement (depuis scan_patch)
    facing: int = 4                                   # côté de drop du drill (0=N, 2=E, 4=S, 6=W)
    drill_tier: str = "burner-mining-drill"
    inserter_tier: str = "burner-inserter"            # burner = pas de pole
    furnace_tier: str = "stone-furnace"
    drill_size: int = 3                               # taille RÉELLE drill (2.0 = 3×3)
    furnace_size: int = 3                             # taille RÉELLE furnace (stone-furnace = 3×3)
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

    demi_d = request.drill_size // 2     # 3 // 2 = 1
    demi_f = request.furnace_size // 2   # 3 // 2 = 1

    # 2. Drop tile = bord aval du drill + 1 (tuile juste après l'emprise, hors emprise).
    #    Pour un drill 3×3 (demi 1), emprise dy-1..dy+1 -> drop à dy+2.
    drop_x = dx + ux * (demi_d + 1)
    drop_y = dy + uy * (demi_d + 1)

    # 3. Inserter (1×1, pickup 1.0, drop 1.0) : 1 tuile après la drop tile (côté +u).
    #    L'inserter est placé entre la drop tile (drill, côté -u) et le furnace (côté +u).
    #    En Factorio 2.0, `direction` = direction de PICKUP (validé live : inserter facing
    #    north pickup_position = y-1, drop_position = y+1). L'inserter doit ramasser la drop
    #    tile du drill (côté -u) et déposer côté furnace (+u) -> sa direction = OPPOSÉ de
    #    facing (il « regarde » vers le drill). Bug initial : FACING_DIR_U[facing] orientait
    #    l'inserter vers +u -> il ramassait la tuile vide côté furnace et déposait sur la
    #    drop tile du drill (bloquait le drill : waiting_for_space_in_destination).
    ins_x = drop_x + ux * 1.0
    ins_y = drop_y + uy * 1.0
    ins_dir = (facing + 4) % 8       # opposé de facing : pickup vers la drop tile du drill (-u)

    # 4. Furnace : bord -u (côté arrière) = drop inserter -> centre = ins + (1 + demi_f).
    #    Inserter drop à ins+1*u ; furnace bord -u = ins+1*u -> centre = ins+(1+demi_f)*u.
    furnace_x = ins_x + ux * (1 + demi_f)
    furnace_y = ins_y + uy * (1 + demi_f)

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
        f"drop_tile=({drop_x},{drop_y}) ; drill_size={request.drill_size} "
        f"(réel 2.0, describe w=2 h=2 = métadonnée) ; furnace_size={request.furnace_size}",
        "terrain_check=off (executor fait can_place_check + retry de position)",
    ]

    return MicroPlan(
        entities=entities, connections=connections, totals=totals,
        bbox=bbox, feasibility="ok", notes=notes,
    )