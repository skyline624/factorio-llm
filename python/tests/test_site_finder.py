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

from services.site_finder import (POLE_PORTEE, place_belt_line, place_inserter_vers,
                                  place_pole_line, place_supply_poles)

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


class FakeMondeInserter:
    """Un monde minimal où un inserter a un sens : il prend derrière et dépose devant.

    Le décalage est délibérément l'INVERSE de celui mesuré en jeu pour `north` — le but
    est justement de vérifier que `place_inserter_vers` ne code aucune convention en dur
    et se contente de lire `pickup`/`drop`.
    """

    DEVANT = {"north": (0.0, 1.0), "east": (-1.0, 0.0),
              "south": (0.0, -1.0), "west": (1.0, 0.0)}

    def __init__(self, cible, belt, refuse=None):
        self.cible = cible                  # (x, y, nom) de la machine à charger
        self.belt = belt                    # (x, y) de la tuile de belt
        self.refuse = refuse or set()
        self.inserters: dict[tuple[float, float], str] = {}
        self.retires: list[tuple[float, float]] = []

    def can_place_check(self, name, x, y, direction="north"):
        occupe = (x, y) == (self.cible[0], self.cible[1]) or (x, y) == self.belt
        return {"can_place": (x, y) not in self.refuse and not occupe}

    def place_entity_at(self, name, x, y, direction="north", opts=None):
        self.inserters[(x, y)] = direction
        return {"ok": True}

    def rotate_entity_at(self, x, y, direction, name=None):
        self.inserters[(x, y)] = direction
        return {"ok": True}

    def remove_entity_at(self, x, y, name=None):
        self.inserters.pop((x, y), None)
        self.retires.append((x, y))
        return {"ok": True}

    def inspect_at(self, x, y, radius=0.5):
        rows = []
        for (ix, iy), d in self.inserters.items():
            if math.hypot(ix - x, iy - y) <= radius:
                dx, dy = self.DEVANT[d]
                rows.append({"name": "inserter", "type": "inserter", "x": ix, "y": iy,
                             "pickupX": ix - dx, "pickupY": iy - dy,
                             "dropX": ix + dx, "dropY": iy + dy})
        if math.hypot(self.belt[0] - x, self.belt[1] - y) <= radius:
            rows.append({"name": "transport-belt", "type": "transport-belt",
                         "x": self.belt[0], "y": self.belt[1]})
        if math.hypot(self.cible[0] - x, self.cible[1] - y) <= radius:
            rows.append({"name": self.cible[2], "type": "boiler",
                         "x": self.cible[0], "y": self.cible[1]})
        return {"entities": rows}

    def run_action(self, fn, *args, timeout=None):
        return fn(*args)


class FakeMondeBelt:
    """Un monde où l'on peut poser des belts, les relire et les tourner."""

    def __init__(self):
        self.belts: dict[tuple[float, float], str] = {}
        self.rotations: list[tuple[float, float, str]] = []

    def can_place_check(self, name, x, y, direction="north"):
        # Poser une belt sur une belt est un REMPLACEMENT RAPIDE, que le jeu autorise :
        # `can_place` répond donc vrai sur une tuile déjà occupée. C'est ce qui rendait
        # la détection d'occupation muette.
        return {"can_place": True}

    def place_entity_at(self, name, x, y, direction="north", opts=None):
        self.belts[(x, y)] = direction
        return {"ok": True}

    def rotate_entity_at(self, x, y, direction, name=None):
        self.belts[(x, y)] = direction
        self.rotations.append((x, y, direction))
        return {"ok": True}

    def inspect_at(self, x, y, radius=0.5):
        rows = [{"name": "transport-belt", "type": "transport-belt", "x": bx, "y": by,
                 "direction": d}
                for (bx, by), d in self.belts.items()
                if abs(bx - x) <= radius and abs(by - y) <= radius]
        return {"entities": rows}

    def run_action(self, fn, *args, timeout=None):
        return fn(*args)


def test_prolongement_retourne_le_raccord() -> None:
    """Prolonger une ligne doit RETOURNER sa dernière tuile vers le nouveau tronçon.

    Sinon elle garde son ancienne direction et déverse dans une tuile vide : la ligne
    est complète à l'œil, aucune pose n'a échoué, et rien n'arrive au bout.
    """
    monde = FakeMondeBelt()
    place_belt_line(monde, (0.5, 0.5), (0.5, 5.5))      # descend vers le sud
    avant = monde.belts.get((0.5, 4.5))
    place_belt_line(monde, (0.5, 4.5), (3.5, 4.5))      # repart vers l'est
    apres = monde.belts.get((0.5, 4.5))
    ok = avant == "south" and apres == "east"
    rec("test_prolongement_retourne_le_raccord", ok,
        f"tuile de raccord : {avant} -> {apres}, {len(monde.rotations)} rotation(s)")
    assert ok, f"avant={avant} apres={apres}"


def test_inserter_oriente_vers_la_cible() -> None:
    """La direction retenue doit être celle dont le DÉPÔT tombe sur la machine."""
    monde = FakeMondeInserter(cible=(10.5, 10.5, "boiler"), belt=(10.5, 12.5))
    pose = place_inserter_vers(monde, (10.5, 10.5), (10.5, 12.5), "boiler")
    ok = pose is not None
    if ok:
        ix, iy, d = pose
        dx, dy = FakeMondeInserter.DEVANT[d]
        ok = (ix + dx, iy + dy) == (10.5, 10.5) and (ix - dx, iy - dy) == (10.5, 12.5)
    rec("test_inserter_oriente_vers_la_cible", ok,
        f"pose={pose} : dépose bien dans le boiler et prend bien sur la belt")
    assert ok


def test_emplacement_sans_issue_est_libere() -> None:
    """Un emplacement qui ne marche dans aucune direction ne doit pas rester posé.

    Sinon chaque tentative laisse un inserter orphelin sur la carte, et la position
    suivante est refusée par celui qu'on vient d'abandonner.
    """
    # Belt hors d'atteinte : aucune position ne peut à la fois prendre dessus et
    # déposer dans la cible.
    monde = FakeMondeInserter(cible=(10.5, 10.5, "boiler"), belt=(40.5, 40.5))
    pose = place_inserter_vers(monde, (10.5, 10.5), (10.5, 12.5), "boiler", essais=6)
    ok = pose is None and not monde.inserters and len(monde.retires) > 0
    rec("test_emplacement_sans_issue_est_libere", ok,
        f"pose={pose}, {len(monde.retires)} retrait(s), {len(monde.inserters)} orphelin(s)")
    assert ok


def main() -> int:
    tests = [
        test_ligne_droite_connexe,
        test_terrain_hostile_reste_connexe,
        test_obstacle_infranchissable_signale,
        test_arrivee_proche_ne_pose_rien,
        test_desserte_reste_a_portee,
        test_prolongement_retourne_le_raccord,
        test_inserter_oriente_vers_la_cible,
        test_emplacement_sans_issue_est_libere,
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