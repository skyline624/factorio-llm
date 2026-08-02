"""Tests unitaires du MicroPlanner (chaîne bootstrap drill+inserter+furnace).

Aucun serveur, aucun LLM, aucun RCON requis : on injecte un ResourcePatch bidon
(iron-ore, tiles=[(0,0)], bbox) et on appelle plan_micro directement. Le micro-planner
est déterministe pur (pas de terrain check, pas de geometry requise au calcul) — les
positions tombent du calcul géométrique (FACING_UNIT + tailles réelles drill/furnace).

Vérifications (cf. plan spicy-gliding-dawn.md) :
  - 3 entités (drill, inserter, furnace), totals {…:1}, aucun pole/belt.
  - Positions exactes par facing (south/east/north/west) : drill → drop tile → inserter → furnace.
  - CHAÎNAGE géométrique : pickup de l'inserter == tuile de drop du drill, dépôt de
    l'inserter == bord amont du furnace (test_micro_chainage_pickup_drop). C'est LE test
    qui manquait : les positions exactes étaient vertes avec une géométrie fausse d'une
    tuile (drill supposé 3×3, drop supposé dans l'axe) et la chaîne ne produisait rien
    en jeu. Il rejoue les formules mesurées par `measure_entity`, pas celles du planner.
  - Aucun chevauchement (distance inter-centres ≥ 2).
  - Connections (drill→ins→furnace, item=patch.resource).
  - feasibility='ok' toujours ; 'missing_geometry' si facing hors {0,2,4,6}.
  - anchor override décale le bloc ; anchor non alignée -> snap sur grille légale.

Chaque test porte un `assert` : `rec()` seul n'échoue pas sous pytest (faux verts).

Lancement :
    cd python
    python -m tests.test_micro_planner
"""

from __future__ import annotations

import sys

from services.layout_planner import ResourcePatch, FACING_UNIT
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
    assert ok
    # totals : 1 de chaque tier.
    ok_tot = (mp.totals == {"burner-mining-drill": 1, "burner-inserter": 1, "stone-furnace": 1})
    rec("test_micro_totals", ok_tot, f"totals={mp.totals}")
    assert ok_tot


def test_sans_fonte_ni_four_ni_inserteur() -> None:
    """UN FOUR DERRIÈRE UN MINERAI QUI NE SE FOND PAS BOUCHE TOUTE LA CHAÎNE.

    Mesuré en jeu sur un gisement de charbon : la foreuse
    `waiting_for_space_in_destination` avec 33 charbons en sortie, l'inserteur bloqué
    derrière, le four `full_output` — trois machines arrêtées en cascade, et 66 tours sur
    120 passés à tenter de vider ce four.

    L'inserteur part avec le four, et ce n'est pas un détail : sans destination il
    pousserait dans le vide et bloquerait la foreuse tout aussi sûrement. Sans fonte, la
    chaîne se réduit à EXTRAIRE — ce que devient le minerai regarde `approvisionner` et
    `evacuer`, pas le planificateur.

    `fondre=True` par défaut : tous les plans existants sont inchangés, ce que le test
    voisin (`test_micro_3_entities`) continue de vérifier.
    """
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4, fondre=False))
    roles = _roles(mp)
    ok = (len(mp.entities) == 1 and roles == ["drill"]
          and not mp.connections and set(mp.totals) == {"burner-mining-drill"})
    rec("test_sans_fonte_ni_four_ni_inserteur", ok,
        f"n={len(mp.entities)} roles={roles} totals={mp.totals} "
        f"connexions={len(mp.connections)}")
    assert ok


def test_micro_facing_south_positions() -> None:
    # facing=4 (south, +y), drill/furnace 2×2 : drill(0,0) -> drop tile(0.5,1.5)
    # -> inserter(0.5,2.5) -> dépôt(0.5,3.5) -> furnace(0,4).
    # Le +0.5 en x n'est pas cosmétique : le drop d'une entité d'emprise paire est décalé
    # d'une demi-tuile de côté (mesuré). L'inserter doit suivre ce décalage, sinon il
    # ramasse à côté de la drop tile.
    # Inserter direction = OPPOSÉ de facing (pickup vers la drop tile du drill, côté -u) :
    # facing south -> inserter facing north (0). Mesuré (Factorio 2.0 : direction = pickup ;
    # inserter north -> pickup = centre+(0,-1), drop = centre+(0,+1.1)).
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    ok = (d.x == 0 and d.y == 0 and d.direction == 4
          and ins.x == 0.5 and ins.y == 2.5 and ins.direction == 0
          and fur.x == 0 and fur.y == 4 and fur.direction == 0)
    rec("test_micro_facing_south_positions", ok,
        f"drill=({d.x},{d.y})d{d.direction} ins=({ins.x},{ins.y})d{ins.direction} "
        f"furn=({fur.x},{fur.y})d{fur.direction}")
    assert ok


def test_micro_facing_east_positions() -> None:
    # facing=2 (east, +x) : drill(0,0) -> drop tile(1.5,-0.5) -> inserter(2.5,-0.5) -> furnace(4,0).
    # Inserter direction = OPPOSÉ de facing -> facing east (2) -> inserter facing west (6).
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=2))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    ok = (d.x == 0 and d.y == 0 and d.direction == 2
          and ins.x == 2.5 and ins.y == -0.5 and ins.direction == 6
          and fur.x == 4 and fur.y == 0 and fur.direction == 0)
    rec("test_micro_facing_east_positions", ok,
        f"drill=({d.x},{d.y})d{d.direction} ins=({ins.x},{ins.y})d{ins.direction} "
        f"furn=({fur.x},{fur.y})d{fur.direction}")
    assert ok


def test_micro_facing_north_west() -> None:
    # facing=0 (north, -y) : drill(0,0) -> inserter(-0.5,-2.5) -> furnace(0,-4).
    mp0 = plan_micro(MicroRequest(patch=_iron_patch(), facing=0))
    ins0, fur0 = _by_role(mp0, "inserter"), _by_role(mp0, "machine")
    ok0 = ins0.x == -0.5 and ins0.y == -2.5 and fur0.x == 0 and fur0.y == -4
    # facing=6 (west, -x) : drill(0,0) -> inserter(-2.5,0.5) -> furnace(-4,0).
    mp6 = plan_micro(MicroRequest(patch=_iron_patch(), facing=6))
    ins6, fur6 = _by_role(mp6, "inserter"), _by_role(mp6, "machine")
    ok6 = ins6.x == -2.5 and ins6.y == 0.5 and fur6.x == -4 and fur6.y == 0
    rec("test_micro_facing_north_west", ok0 and ok6,
        f"north ins=({ins0.x},{ins0.y}) furn=({fur0.x},{fur0.y}) | "
        f"west ins=({ins6.x},{ins6.y}) furn=({fur6.x},{fur6.y})")
    assert ok0 and ok6


def test_micro_chainage_pickup_drop() -> None:
    """Le maillon réel : pickup inserter == drop tile drill, dépôt inserter dans le furnace.

    On ne compare PAS aux formules du planner (ce serait tautologique) mais aux positions
    mesurées en jeu via `measure_entity` :
      drill 2×2  : drop_position = centre + u*1.25 + perp*0.5   (perp = (uy, -ux))
      inserter   : pickup = centre + v*1.0, drop = centre - v*1.1
      furnace2×2 : occupe [F-1, F+1] sur chaque axe.
    Le bug corrigé ici passait tous les autres tests : chaîne posée, drill working, mais
    l'inserter ramassait une tuile trop loin -> zéro plaque produite.
    """
    bad: list[str] = []
    for facing in (0, 2, 4, 6):
        mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=facing, anchor=(10.0, -6.0)))
        d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
        ux, uy = FACING_UNIT[facing]
        px, py = uy, -ux
        # 1. Tuile réellement visée par le drill (celle qui CONTIENT drop_position).
        drop = (d.x + ux * 1.25 + px * 0.5, d.y + uy * 1.25 + py * 0.5)
        drop_tile = (drop[0] // 1, drop[1] // 1)
        # 2. Tuile réellement ramassée par l'inserter (direction = pickup).
        vx, vy = FACING_UNIT[ins.direction]
        pick_tile = ((ins.x + vx) // 1, (ins.y + vy) // 1)
        if pick_tile != drop_tile:
            bad.append(f"facing={facing} pickup{pick_tile} != drop{drop_tile}")
        # 3. Tuile où l'inserter dépose -> doit tomber DANS l'emprise du furnace.
        put = (ins.x - vx * 1.1, ins.y - vy * 1.1)
        if not (fur.x - 1 <= put[0] <= fur.x + 1 and fur.y - 1 <= put[1] <= fur.y + 1):
            bad.append(f"facing={facing} dépôt{put} hors furnace@({fur.x},{fur.y})")
        # 4. L'inserter (1×1) ne doit chevaucher ni le drill ni le furnace.
        if abs(ins.x - d.x) < 1.5 and abs(ins.y - d.y) < 1.5:
            bad.append(f"facing={facing} inserter dans le drill")
        if abs(ins.x - fur.x) < 1.5 and abs(ins.y - fur.y) < 1.5:
            bad.append(f"facing={facing} inserter dans le furnace")
    rec("test_micro_chainage_pickup_drop", not bad, f"anomalies={bad or 'aucune'}")
    assert not bad, bad


def test_micro_positions_sur_grille_legale() -> None:
    """Positions déjà snappées : ce que teste `can_place_check` == ce que pose `create_entity`.

    Emprise paire -> entier ; 1×1 -> centre de tuile. Une anchor non alignée doit être
    ramenée sur la grille, sans quoi la pose dérive d'une demi-tuile (constat live).
    """
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4, anchor=(3.4, -7.6)))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    ok = (d.x % 1 == 0 and d.y % 1 == 0
          and abs(ins.x % 1) == 0.5 and abs(ins.y % 1) == 0.5
          and fur.x % 1 == 0 and fur.y % 1 == 0)
    rec("test_micro_positions_sur_grille_legale", ok,
        f"anchor=(3.4,-7.6) -> drill=({d.x},{d.y}) ins=({ins.x},{ins.y}) furn=({fur.x},{fur.y})")
    assert ok


def test_micro_emprises_impaires_coherentes() -> None:
    """Chaîne à emprises IMPAIRES (3×3) : grille légale et aucun chevauchement.

    La chaîne électrique à venir (electric-mining-drill, electric-furnace) est en 3×3,
    or toute la couverture actuelle est en 2×2 : la branche impaire de `_snap` et du
    décalage latéral n'était jamais exercée. Un plan qui se chevauche ou qui sort de la
    grille ne se pose pas — autant le savoir sans serveur.

    PORTÉE : ce test vérifie la cohérence INTERNE du plan. Les positions de ports d'un
    3×3 (drop du drill, notamment) n'ont pas encore été mesurées en jeu, contrairement
    au 2×2 — c'est `verify_factory_e5` qui les mesurera. On ne grave donc ici aucune
    constante de géométrie non mesurée : c'est précisément l'erreur qui avait coûté
    deux runs au premier jet du MicroPlanner.
    """
    bad: list[str] = []
    for taille in (2, 3):
        for facing in (0, 2, 4, 6):
            mp = plan_micro(MicroRequest(
                patch=_iron_patch(), facing=facing, anchor=(10.0, -6.0),
                drill_size=taille, furnace_size=taille,
                drill_tier="electric-mining-drill", inserter_tier="inserter",
                furnace_tier="electric-furnace"))
            d, ins, fur = (_by_role(mp, "drill"), _by_role(mp, "inserter"),
                           _by_role(mp, "machine"))
            # Grille : emprise paire -> entier, impaire -> centre de tuile ; 1×1 -> .5.
            attendu = 0.0 if taille % 2 == 0 else 0.5
            for e in (d, fur):
                if abs(e.x % 1) != attendu or abs(e.y % 1) != attendu:
                    bad.append(f"{taille}x{taille} f{facing}: {e.role} hors grille "
                               f"({e.x},{e.y})")
            if abs(ins.x % 1) != 0.5 or abs(ins.y % 1) != 0.5:
                bad.append(f"{taille}x{taille} f{facing}: inserter hors centre de tuile")
            # Chevauchement : demi-emprises + demi-emprise de l'inserter (1×1).
            for a, b, ta, tb in ((d, ins, taille, 1), (ins, fur, 1, taille),
                                 (d, fur, taille, taille)):
                if (abs(a.x - b.x) < (ta + tb) / 2 - 1e-6
                        and abs(a.y - b.y) < (ta + tb) / 2 - 1e-6):
                    bad.append(f"{taille}x{taille} f{facing}: {a.role} recouvre {b.role}")
    rec("test_micro_emprises_impaires_coherentes", not bad, f"anomalies={bad[:3] or 'aucune'}")
    assert not bad, bad


def test_micro_no_overlap() -> None:
    # Distance inter-centres drill→inserter ≥ 2, inserter→furnace ≥ 2 (facing south).
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    di = ((d.x - ins.x) ** 2 + (d.y - ins.y) ** 2) ** 0.5
    iff = ((ins.x - fur.x) ** 2 + (ins.y - fur.y) ** 2) ** 0.5
    ok = di >= 2.0 and iff >= 1.5
    rec("test_micro_no_overlap", ok, f"|drill-ins|={di} |ins-furn|={iff}")
    assert ok


def test_micro_connections() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    expected = [(0, 1, "iron-ore"), (1, 2, "iron-ore")]
    ok = mp.connections == expected
    rec("test_micro_connections", ok, f"conn={mp.connections}")
    assert ok


def test_micro_no_pole_no_belt() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    bad = [r for r in _roles(mp) if r in ("pole", "belt", "bus-belt", "splitter", "merger")]
    ok = not bad
    rec("test_micro_no_pole_no_belt", ok, f"roles={_roles(mp)} bad={bad}")
    assert ok


def test_micro_anchor_override() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4, anchor=(10.0, 10.0)))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    ok = (d.x == 10 and d.y == 10 and ins.x == 10.5 and ins.y == 12.5
          and fur.x == 10 and fur.y == 14)
    rec("test_micro_anchor_override", ok,
        f"drill=({d.x},{d.y}) ins=({ins.x},{ins.y}) furn=({fur.x},{fur.y})")
    assert ok


def test_micro_feasibility_ok() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    ok = mp.feasibility == "ok"
    rec("test_micro_feasibility_ok", ok, f"feasibility={mp.feasibility}")
    assert ok


def test_micro_invalid_facing() -> None:
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=5))
    ok = mp.feasibility == "missing_geometry" and len(mp.entities) == 0
    rec("test_micro_invalid_facing", ok,
        f"feasibility={mp.feasibility} n_entities={len(mp.entities)}")
    assert ok


def test_micro_drop_tile_hors_emprise() -> None:
    # Règle mémoire §2 : drop-direct drill→furnace IMPOSSIBLE (chevauchement).
    # Le micro-planner passe par un inserter : la drop tile est HORS emprise drill et le
    # furnace ne chevauche pas le drill. Emprises 2×2 mesurées : [c-1, c+1] par axe.
    mp = plan_micro(MicroRequest(patch=_iron_patch(), facing=4))
    d, ins, fur = _by_role(mp, "drill"), _by_role(mp, "inserter"), _by_role(mp, "machine")
    drill_y = (d.y - 1, d.y + 1)
    furn_y = (fur.y - 1, fur.y + 1)
    # Chevauchement y ? (bords jointifs tolérés : les collision box s'arrêtent à 0.9)
    overlap = drill_y[1] > furn_y[0] and furn_y[1] > drill_y[0]
    # Inserter entre les deux (sa tuile ne mord sur aucune des deux emprises).
    inserter_between = drill_y[1] <= ins.y - 0.5 and ins.y + 0.5 <= furn_y[0]
    ok = (not overlap) and inserter_between
    rec("test_micro_drop_tile_hors_emprise", ok,
        f"drill_y={drill_y} ins_y={ins.y} furn_y={furn_y} overlap={overlap}")
    assert ok


def _sample(tuiles) -> dict:
    """Un scan_patch réduit à ce qui compte ici : ses tuiles de minerai réelles."""
    return {"sample": [{"x": x, "y": y} for x, y in tuiles]}


def test_ancres_ordonnees_du_bord_aval() -> None:
    """La première ancre reste celle d'avant : la chaîne initiale ne bouge pas.

    `_anchor_on_ore` ne rendait qu'une ancre, la plus avancée côté `facing`. C'est le bon
    choix pour la PREMIÈRE chaîne, et c'est ce que quatre scripts de vérification
    attendent. La liste doit donc commencer exactement là.
    """
    from agents.factory_builder import FactoryBuilder
    sp = _sample([(0, 0), (0, 4), (0, 8)])          # facing=4 (sud) : y croissant
    liste = FactoryBuilder.ancres_sur_minerai(sp, 4)
    seule = FactoryBuilder._anchor_on_ore(sp, 4)
    ok = liste and liste[0] == seule and liste[0] == (0.0, 7.0)
    rec("test_ancres_ordonnees_du_bord_aval", bool(ok),
        f"{liste[:3]} | _anchor_on_ore={seule}")
    assert ok


def test_ancres_multiples_pour_etendre() -> None:
    """Il en faut PLUSIEURS : la meilleure est occupée dès la première chaîne posée.

    Mesuré en jeu : une extension reproposait invariablement (-26.5,-62.5) — l'emplacement
    de la chaîne existante — et échouait sur `can_place=False` trois fois de suite avant
    d'être abandonnée définitivement. L'inventaire était plein ; seule la place manquait.
    """
    from agents.factory_builder import FactoryBuilder
    sp = _sample([(0, 0), (0, 5), (0, 10), (6, 10), (12, 10)])
    liste = FactoryBuilder.ancres_sur_minerai(sp, 4, ecart=4)
    ok = len(liste) >= 3 and len(set(liste)) == len(liste)
    rec("test_ancres_multiples_pour_etendre", ok, f"{len(liste)} ancre(s) : {liste[:4]}")
    assert ok


def test_ancres_espacees_pour_ne_pas_reessayer_le_meme_endroit() -> None:
    """Douze tuiles voisines ne font pas douze emplacements.

    Une micro-chaîne occupe drill (3) + inserter (1) + four (3). Sans écart minimal, les
    candidats décrivent le même endroit et chaque tentative coûte un plan et un pré-vol
    pour rien.
    """
    from agents.factory_builder import FactoryBuilder
    serres = _sample([(0, 10), (1, 10), (2, 10), (3, 10), (0, 9), (1, 9)])
    liste = FactoryBuilder.ancres_sur_minerai(serres, 4, ecart=4)
    ok = len(liste) == 1
    rec("test_ancres_espacees_pour_ne_pas_reessayer_le_meme_endroit", ok,
        f"6 tuiles serrées -> {len(liste)} ancre(s) : {liste}")
    assert ok


def test_aucune_tuile_aucune_ancre() -> None:
    """Sans minerai, aucune ancre — et surtout pas une position inventée.

    Un mining-drill hors minerai est refusé à la pose (26/26 mesurés en live) : rendre
    une ancre par défaut ferait échouer la chaîne entière à l'exécution.
    """
    from agents.factory_builder import FactoryBuilder
    ok = (FactoryBuilder.ancres_sur_minerai({}, 4) == []
          and FactoryBuilder._anchor_on_ore({}, 4) is None)
    rec("test_aucune_tuile_aucune_ancre", ok, "liste vide et ancre None")
    assert ok


def main() -> int:
    tests = [
        test_micro_3_entities,
        test_micro_facing_south_positions,
        test_micro_facing_east_positions,
        test_micro_facing_north_west,
        test_micro_chainage_pickup_drop,
        test_micro_positions_sur_grille_legale,
        test_micro_emprises_impaires_coherentes,
        test_micro_no_overlap,
        test_micro_connections,
        test_micro_no_pole_no_belt,
        test_micro_anchor_override,
        test_micro_feasibility_ok,
        test_micro_invalid_facing,
        test_micro_drop_tile_hors_emprise,
        test_ancres_ordonnees_du_bord_aval,
        test_ancres_multiples_pour_etendre,
        test_ancres_espacees_pour_ne_pas_reessayer_le_meme_endroit,
        test_aucune_tuile_aucune_ancre,
        test_sans_fonte_ni_four_ni_inserteur,
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