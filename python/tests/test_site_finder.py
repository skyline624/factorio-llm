"""Tests unitaires du SiteFinder — la connexité d'une ligne, sans serveur.

`place_pole_line` porte la logique qui a coûté le plus cher du chantier électricité :
une ligne dont un maillon dépasse la portée de fil scinde le réseau en deux, sans
qu'aucune pose n'échoue ni qu'aucune erreur ne soit levée. Elle mérite d'être tenue
par des tests plutôt que par la mémoire de ce qui s'est passé en jeu.

Le faux API simule un terrain : des positions refusées forcent les décalages, et c'est
exactement là que la ligne se cassait — chaîner sur le tracé théorique laisse deux
décalages opposés créer un saut trop long.

Lancement :
    cd python
    python -m tests.test_site_finder
"""

from __future__ import annotations

import math
import sys

from services.site_finder import POLE_PORTEE, place_pole_line, place_supply_poles

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:46s} {detail[:100]}")


class FakeApi:
    """Terrain simulé : `refuse` est l'ensemble des tuiles que le sol n'accepte pas."""

    def __init__(self, refuse=None):
        self.refuse = refuse or set()
        self.poses: list[tuple[float, float]] = []

    def can_place_check(self, name, x, y, direction="north"):
        return {"can_place": (x, y) not in self.refuse}

    def place_entity_at(self, name, x, y, direction="north", opts=None):
        self.poses.append((x, y))
        return {"ok": True}

    def run_action(self, fn, *args, timeout=None):
        return fn(*args)


def _sauts(poses):
    return [math.hypot(a[0] - b[0], a[1] - b[1]) for a, b in zip(poses, poses[1:])]


def test_ligne_droite_connexe() -> None:
    api = FakeApi()
    poses, complete = place_pole_line(api, (0.5, 0.5), (60.5, 0.5))
    sauts = _sauts(poses)
    ok = complete and poses and all(s <= POLE_PORTEE for s in sauts)
    rec("test_ligne_droite_connexe", ok,
        f"{len(poses)} poteaux, saut max {max(sauts) if sauts else 0:.2f} <= {POLE_PORTEE}")
    assert ok


def test_terrain_hostile_reste_connexe() -> None:
    """Le vrai test : un terrain qui refuse des tuiles force des détours.

    Chaîner sur le tracé théorique laisserait deux décalages opposés créer un saut de
    plus de 8 tuiles. En chaînant sur la position réellement posée, aucun maillon ne
    peut dépasser la portée — c'est vérifié ici sur chaque saut.
    """
    # Une bande de tuiles refusées en travers du trajet, tous les 12 en x.
    refuse = {(x + 0.5, y + 0.5) for x in range(0, 80, 12) for y in range(-3, 4)}
    api = FakeApi(refuse)
    poses, _ = place_pole_line(api, (0.5, 0.5), (72.5, 0.5))
    sauts = _sauts(poses)
    trop_longs = [s for s in sauts if s > POLE_PORTEE]
    ok = poses and not trop_longs
    rec("test_terrain_hostile_reste_connexe", ok,
        f"{len(poses)} poteaux, {len(trop_longs)} saut(s) trop long(s), "
        f"max {max(sauts) if sauts else 0:.2f}")
    assert ok, trop_longs


def test_obstacle_infranchissable_signale() -> None:
    """Un mur complet doit rendre `complète=False` : le courant n'ira pas plus loin.

    Le dire est ce qui compte — une ligne interrompue en silence se paie plus tard, sur
    une machine sans courant à l'autre bout.
    """
    refuse = {(x + 0.5, y + 0.5) for x in range(8, 40) for y in range(-30, 30)}
    api = FakeApi(refuse)
    poses, complete = place_pole_line(api, (0.5, 0.5), (60.5, 0.5))
    ok = complete is False
    rec("test_obstacle_infranchissable_signale", ok,
        f"complete={complete} après {len(poses)} poteau(x)")
    assert ok


def test_arrivee_proche_ne_pose_rien() -> None:
    """Départ et arrivée déjà à portée : rien à poser, et c'est un succès."""
    api = FakeApi()
    poses, complete = place_pole_line(api, (0.5, 0.5), (3.5, 0.5))
    ok = complete and not poses
    rec("test_arrivee_proche_ne_pose_rien", ok, f"{len(poses)} poteau(x), complete={complete}")
    assert ok


def test_desserte_reste_a_portee() -> None:
    """Chaque poteau de desserte doit rester relié au précédent, sinon îlot isolé."""
    api = FakeApi()
    machines = [(10.5, 0.5), (10.5, 5.5), (10.5, 10.5)]
    poses = place_supply_poles(api, machines, ancrage=(8.5, 0.5))
    ok = poses and all(s <= POLE_PORTEE for s in _sauts([(8.5, 0.5)] + poses))
    rec("test_desserte_reste_a_portee", ok,
        f"{len(poses)} poteau(x), sauts={[round(s, 1) for s in _sauts([(8.5, 0.5)] + poses)]}")
    assert ok


def main() -> int:
    tests = [
        test_ligne_droite_connexe,
        test_terrain_hostile_reste_connexe,
        test_obstacle_infranchissable_signale,
        test_arrivee_proche_ne_pose_rien,
        test_desserte_reste_a_portee,
    ]
    for t in tests:
        t()
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())