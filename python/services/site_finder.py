"""SiteFinder — choisir OÙ bâtir, et relier ce qui est bâti.

Extraction de code éprouvé, pas de code neuf : ces deux fonctions ont été écrites,
cassées et corrigées dans quatre scripts de vérification successifs (centrale,
chaîne électrique, diagnostic, coordinator). Les garder dupliquées, c'était garantir
que la prochaine correction n'atteindrait qu'une copie sur quatre.

Contrairement aux planners (`power_planner`, `micro_planner`), ce module TOUCHE le jeu :
choisir un site demande de savoir ce que le terrain accepte, et aucun calcul ne le
remplace. La frontière reste nette — ici on observe et on pose, on ne dimensionne pas.

Deux vérités de terrain qui ont chacune coûté un run et qu'on ne redécouvrira pas :

  - **L'offshore-pump se pose sur la RIVE**, une tuile de TERRE adjacente à l'eau, et sa
    direction pointe VERS l'eau. `scan_water_edge` renvoyant des tuiles d'*eau*, il faut
    prendre une de leurs voisines terrestres. Le poser sur l'eau échoue sur les 60
    premières tuiles et dans les 4 directions, sans qu'aucun message ne l'explique.

  - **Une ligne de poteaux se chaîne sur les positions RÉELLEMENT posées**, jamais sur
    le tracé théorique. Chaque poteau que le terrain refuse est décalé d'une ou deux
    tuiles ; deux décalages opposés créent un saut supérieur à la portée de fil et
    scindent le réseau en deux. Rien ne le signale : tous les poteaux sont posés, aucune
    erreur n'est levée, et c'est la machine au bout de la ligne qui reste sans courant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# Directions Factorio et vecteur unitaire associé.
DIRS: dict[int, tuple[float, float]] = {0: (0.0, -1.0), 2: (1.0, 0.0),
                                        4: (0.0, 1.0), 6: (-1.0, 0.0)}
DIR_NOM: dict[int, str] = {0: "north", 2: "east", 4: "south", 6: "west"}

POLE_PAS = 6.0        # espacement visé : sous la portée, pour encaisser les décalages
POLE_PORTEE = 7.5     # portée de fil d'un small-electric-pole (fixture, cf. power_planner)


@dataclass
class SitePower:
    """Emplacement retenu pour une centrale."""
    pompe: tuple[float, float]        # sur la rive
    direction: int                    # de la pompe, VERS l'eau
    origine: tuple[float, float]      # premier boiler, en retrait côté terre

    def distance_a(self, point: tuple[float, float]) -> float:
        return math.hypot(self.origine[0] - point[0], self.origine[1] - point[1])


def can_place(api, name: str, x: float, y: float, direction: str = "north") -> bool:
    c = api.can_place_check(name, x, y, direction)
    return isinstance(c, dict) and c.get("can_place") is True


def find_power_site(api, vers: tuple[float, float] = (0.0, 0.0),
                    rayon_eau: float = 250.0, candidats: int = 60,
                    reculs: tuple[float, ...] = (5.0, 7.0, 9.0),
                    preparer=None) -> Optional[SitePower]:
    """Cherche une rive où poser la pompe, avec du terrain sec derrière pour la centrale.

    Les tuiles d'eau sont essayées de la plus proche de `vers` à la plus lointaine :
    une centrale près de sa charge économise une ligne de poteaux.

    `preparer(x, y)` est appelé avant de tester un emplacement — c'est le point d'entrée
    pour générer les chunks et dégager la végétation, que l'appelant fait à sa façon.
    """
    we = api.scan_water_edge(rayon_eau)
    tuiles = list(we.get("tiles", []) if isinstance(we, dict) else [])
    tuiles.sort(key=lambda t: (t["x"] - vers[0]) ** 2 + (t["y"] - vers[1]) ** 2)

    for t in tuiles[:candidats]:
        wx, wy = math.floor(t["x"]) + 0.5, math.floor(t["y"]) + 0.5
        for d, (ux, uy) in DIRS.items():
            # Voisine TERRESTRE : à l'opposé de la direction visée, la pompe regardant l'eau.
            px, py = wx - ux, wy - uy
            if not can_place(api, "offshore-pump", px, py, DIR_NOM[d]):
                continue
            for recul in reculs:
                ox = math.floor(px - ux * recul) + 0.5
                oy = float(round(py - uy * recul))
                if preparer is not None:
                    preparer(ox, oy)
                # Deux emplacements de boiler et deux de moteur : de quoi loger la
                # plus petite centrale, sans quoi le site ne sert à rien.
                if (all(can_place(api, "boiler", ox + dx, oy) for dx in (0.0, 4.0))
                        and all(can_place(api, "steam-engine", ox, oy - dd)
                                for dd in (3.5, 8.5))
                        and can_place(api, "offshore-pump", px, py, DIR_NOM[d])):
                    return SitePower(pompe=(px, py), direction=d, origine=(ox, oy))
    return None


def place_pole_line(api, depart: tuple[float, float], arrivee: tuple[float, float],
                    pole: str = "small-electric-pole", pas: float = POLE_PAS,
                    portee: float = POLE_PORTEE, timeout: float = 20.0,
                    garde: int = 80) -> tuple[list[tuple[float, float]], bool]:
    """Pose une ligne de poteaux CONNEXE entre deux points. Retourne (positions, complète).

    Chaque poteau est visé à `pas` de la position réellement posée précédente, et tout
    candidat au-delà de `portee` est refusé : c'est ce qui garantit qu'aucun maillon ne
    dépasse la portée de fil, même quand le terrain impose des détours.

    `complète=False` signale un obstacle infranchissable — la ligne s'arrête là, et
    l'appelant sait que le courant n'ira pas plus loin.
    """
    cur = (math.floor(depart[0]) + 0.5, math.floor(depart[1]) + 0.5)
    poses: list[tuple[float, float]] = []
    for _ in range(garde):
        reste = math.hypot(arrivee[0] - cur[0], arrivee[1] - cur[1])
        if reste <= pas:
            return poses, True
        t = pas / reste
        vx = cur[0] + (arrivee[0] - cur[0]) * t
        vy = cur[1] + (arrivee[1] - cur[1]) * t
        pose = None
        for dx, dy in ((0.0, 0.0), (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
                       (1.0, 1.0), (-1.0, -1.0), (2.0, 0.0), (0.0, 2.0),
                       (-2.0, 0.0), (0.0, -2.0)):
            x = math.floor(vx + dx) + 0.5
            y = math.floor(vy + dy) + 0.5
            if math.hypot(x - cur[0], y - cur[1]) > portee:
                continue                      # couperait la ligne
            if not can_place(api, pole, x, y):
                continue
            r = api.run_action(api.place_entity_at, pole, x, y, "north", None,
                               timeout=timeout)
            if isinstance(r, dict) and r.get("ok"):
                pose = (x, y)
                break
        if pose is None:
            return poses, False
        poses.append(pose)
        cur = pose
    return poses, False


def place_supply_poles(api, machines, ancrage: tuple[float, float],
                       pole: str = "small-electric-pole",
                       portee: float = POLE_PORTEE,
                       ecarts: tuple[tuple[float, float], ...] = ((2.5, 0.0), (-2.5, 0.0),
                                                                  (0.0, 2.5), (0.0, -2.5),
                                                                  (2.5, 2.5), (-2.5, -2.5)),
                       timeout: float = 20.0) -> list[tuple[float, float]]:
    """Dessert des machines en poteaux, chacun restant à portée du précédent.

    Une machine doit être dans la ZONE DE FOURNITURE d'un poteau pour consommer ou
    injecter du courant — être « à côté » de la ligne ne suffit pas. Et le poteau de
    desserte doit lui-même rester relié, sinon la chaîne forme son propre réseau.
    """
    poses: list[tuple[float, float]] = []
    for m in machines:
        mx = getattr(m, "x", None)
        my = getattr(m, "y", None)
        if mx is None:
            mx, my = m[0], m[1]
        for dx, dy in ecarts:
            x = math.floor(mx + dx) + 0.5
            y = math.floor(my + dy) + 0.5
            if math.hypot(x - ancrage[0], y - ancrage[1]) > portee:
                continue
            if not can_place(api, pole, x, y):
                continue
            r = api.run_action(api.place_entity_at, pole, x, y, "north", None,
                               timeout=timeout)
            if isinstance(r, dict) and r.get("ok"):
                poses.append((x, y))
                ancrage = (x, y)
                break
    return poses