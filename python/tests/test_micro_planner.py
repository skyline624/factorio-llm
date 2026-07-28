"""Tests unitaires du MicroPlanner (chaîne bootstrap drill+inserter+furnace).

Aucun serveur, aucun LLM, aucun RCON requis : on injecte un ResourcePatch bidon
(iron-ore, tiles=[(0,0)], bbox) et on appelle plan_micro directement. Le micro-planner
est déterministe pur (pas de terrain check, pas de geometry requise au calcul) — les
positions tombent du calcul géométrique (FACING_UNIT + tailles réelles drill/furnace).

Vérifications (cf. plan spicy-gliding-dawn.md) :
  - 3 entités (drill, inserter, furnace), totals {…:1}, aucun pole/belt.
  - Positions exactes par facing (south/east/north/west) : drill → drop tile → inserter → furnace.
  - Aucun chevauchement (distance inter-centres ≥ 2).
  - Connections (drill→ins→furnace, item=patch.resource).
  - feasibility='ok' toujours ; 'missing_geometry' si facing hors {0,2,4,6}.
  - anchor override décale le bloc.

Lancement :
    cd python
    python -m tests.test_micro_planner
"""

from __future__ import annotations

import sys

from services.layout_planner import ResourcePatch
from services.micro_planner import MicroRequest, MicroPlan, plan_micro

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


rec = record


def _iron_patch() -> ResourcePatch:
    return ResourcePatch(resource="iron-ore", tiles=[(0, 0)], bbox=(0, 0, 0, 0))


def _roles(mp: MicroPlan) -> list[str]:
    return [e.role for e in mp.entities]


def _by_role(mp: MicroPlan, role: str):
    return next((e for e in mp.entities if e.role == role), None)


def test_micro_3_entities() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    ok = len(mp.entities) == 3 and set(_roles(mp)) == {"drill", "inserter", "machine"}
    rec("test_micro_3_entities", ok,
        f"n={len(mp.entities)} roles={_roles(mp)} totals={mp.totals}")
    # totals : 1 de chaque tier.
    ok_tot = (mp.totals == {"burner-mining-drill": 1, "burner-inserter": 1, "stone-furnace": 1})
    rec("test_micro_totals", ok_tot, f"totals={mp.totals}")


def test_micro_facing_south_positions() -> None:
    # facing=4 (south, +y) : drill(0,0) -> drop(0,2) -> inserter(0,3) -> furnace(0,5).
    # Inserter direction = OPPOSÉ de facing (pickup vers la drop tile du drill, côté -u) :
    # facing south -> inserter facing north (0). Validé live (convention Factorio 2.0 :
    # direction = pickup ; inserter facing north pickup au nord, drop au sud).
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    ok = (d.x == 0 and d.y == 0 and d.direction == 4
          and ins.x == 0 and ins.y == 3 and ins.direction == 0
          and fur.x == 0 and fur.y == 5 and fur.direction == 0)
    rec("test_micro_facing_south_positions", ok,
        f"drill=({d.x},{d.y})d{d.direction} ins=({ins.x},{ins.y})d{ins.direction} "
        f"furn=({fur.x},{fur.y})d{fur.direction}")


def test_micro_facing_east_positions() -> None:
    # facing=2 (east, +x) : drill(0,0) -> drop(2,0) -> inserter(3,0) -> furnace(5,0).
    # Inserter direction = OPPOSÉ de facing -> facing east (2) -> inserter facing west (6).
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=2))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    ok = (d.x == 0 and d.y == 0 and d.direction == 2
          and ins.x == 3 and ins.y == 0 and ins.direction == 6
          and fur.x == 5 and fur.y == 0 and fur.direction == 0)
    rec("test_micro_facing_east_positions", ok,
        f"drill=({d.x},{d.y})d{d.direction} ins=({ins.x},{ins.y})d{ins.direction} "
        f"furn=({fur.x},{fur.y})d{fur.direction}")


def test_micro_facing_north_west() -> None:
    # facing=0 (north, -y) : drill(0,0) -> inserter(0,-3) -> furnace(0,-5).
    mp0 = plan_micro(MicroRequest(patch=_iron_patch(), facing=0))
    ins0, fur0 = _by_role(mp0, "inserter"), _by_role(mp0, "machine")
    ok0 = ins0.x == 0 and ins0.y == -3 and fur0.x == 0 and fur0.y == -5
    # facing=6 (west, -x) : drill(0,0) -> inserter(-3,0) -> furnace(-5,0).
    mp6 = plan_micro(MicroRequest(patch=_iron_patch(), facing=6))
    ins6, fur6 = _by_role(mp6, "inserter"), _by_role(mp6, "machine")
    ok6 = ins6.x == -3 and ins6.y == 0 and fur6.x == -5 and fur6.y == 0
    rec("test_micro_facing_north_west", ok0 and ok6,
        f"north ins=({ins0.x},{ins0.y}) furn=({fur0.x},{fur0.y}) | "
        f"west ins=({ins6.x},{ins6.y}) furn=({fur6.x},{fur6.y})")


def test_micro_no_overlap() -> None:
    # Distance inter-centres drill→inserter = 3, inserter→furnace = 2 (facing south).
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    di = ((d.x - ins.x) ** 2 + (d.y - ins.y) ** 2) ** 0.5
    iff = ((ins.x - fur.x) ** 2 + (ins.y - fur.y) ** 2) ** 0.5
    ok = di >= 2.0 and iff >= 2.0
    rec("test_micro_no_overlap", ok, f"|drill-ins|={di} |ins-furn|={iff}")


def test_micro_connections() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    expected = [(0, 1, "iron-ore"), (1, 2, "iron-ore")]
    ok = mp.connections == expected
    rec("test_micro_connections", ok, f"conn={mp.connections}")


def test_micro_no_pole_no_belt() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    bad = [r for r in _roles(mp) if r in ("pole", "belt", "bus-belt", "splitter", "merger")]
    ok = not bad
    rec("test_micro_no_pole_no_belt", ok, f"roles={_roles(mp)} bad={bad}")


def test_micro_anchor_override() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4, anchor=(10.0, 10.0)))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    ok = (d.x == 10 and d.y == 10 and ins.x == 10 and ins.y == 13
          and fur.x == 10 and fur.y == 15)
    rec("test_micro_anchor_override", ok,
        f"drill=({d.x},{d.y}) ins=({ins.x},{ins.y}) furn=({fur.x},{fur.y})")


def test_micro_feasibility_ok() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    ok = mp.feasibility == "ok"
    rec("test_micro_feasibility_ok", ok, f"feasibility={mp.feasibility}")


def test_micro_invalid_facing() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=5))
    ok = mp.feasibility == "missing_geometry" and len(mp.entities) == 0
    rec("test_micro_invalid_facing", ok,
        f"feasibility={mp.feasibility} n_entities={len(mp.entities)}")


def test_micro_drop_tile_hors_emprise() -> None:
    # Règle mémoire §2 : drop-direct drill→furnace 3×3 IMPOSSIBLE (chevauchement).
    # Le micro-planner passe par un inserter : la drop tile (dy+2) est HORS emprise drill
    # (dy-1..dy+1) et le furnace (dy+4..dy+6) ne chevauche pas le drill.
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    # Emprise drill 3×3 centrée (dx,dy) : y ∈ [dy-1, dy+1].
    drill_y = (d.y - 1, d.y + 1)
    # Emprise furnace 3×3 centrée (fx,fy) : y ∈ [fy-1, fy+1].
    furn_y = (fur.y - 1, fur.y + 1)
    # Chevauchement y ?
    overlap = not (drill_y[1] < furn_y[0] or furn_y[1] < drill_y[0])
    # Inserter entre les deux (y strictement entre).
    inserter_between = drill_y[1] < ins.y < furn_y[0]
    ok = (not overlap) and inserter_between
    rec("test_micro_drop_tile_hors_emprise", ok,
        f"drill_y={drill_y} ins_y={ins.y} furn_y={furn_y} overlap={overlap}")


def main() -> int:
    tests = [
        test_micro_3_entities,
        test_micro_facing_south_positions,
        test_micro_facing_east_positions,
        test_micro_facing_north_west,
        test_micro_no_overlap,
        test_micro_connections,
        test_micro_no_pole_no_belt,
        test_micro_anchor_override,
        test_micro_feasibility_ok,
        test_micro_invalid_facing,
        test_micro_drop_tile_hors_emprise,
    ]
    for t in tests:
        t()
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())