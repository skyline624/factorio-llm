"""Suivre un flux de matière, et dire OÙ il casse. Déterministe.

Le FactoryDoctor répond à « quelle machine est en panne, et de quoi souffre-t-elle » en
lisant des statuts. C'est la bonne question pour une machine, et la mauvaise pour une
CHAÎNE : un raccord de belt resté tourné vers le vide ne met personne en erreur. La
machine au bout affiche `no_fuel`, ce qui est exact et sans rapport avec la cause, et le
diagnostic par statut conclura « réservoir vide » indéfiniment.

Pire, `factory_doctor.TYPES_TRANSIT` écarte délibérément inserters et belts comme causes
racines — juste pour un diagnostic par statut (un bras passe sa vie à attendre), aveugle
pour un défaut de raccordement. Ce module est le complément exact de ce choix : il ne
regarde QUE les organes de transit, et il les regarde comme un chemin.

Mesuré en jeu (chantier E13, chaîne charbon -> boiler) :

  - 31 segments parcourus depuis le foreur, puis plus rien : la dernière tuile de
    l'ancien tronçon avait gardé sa direction et déversait dans une tuile vide, à une
    tuile du but. Aucune pose n'avait échoué ;
  - un bras posé à 2.5 tuiles de son boiler, `drop` du côté opposé, statut d'un bras qui
    attend. Il n'a jamais transporté un seul charbon.

Aucun de ces deux défauts n'est visible sur un statut. Tous deux se voient en suivant le
chemin de proche en proche — c'est tout ce que fait ce module.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

# Direction lue sur une belt -> vecteur d'avance d'une tuile.
AVANCE: dict[str, tuple[float, float]] = {
    "north": (0.0, -1.0), "east": (1.0, 0.0),
    "south": (0.0, 1.0), "west": (-1.0, 0.0),
}

# Vocabulaire FERMÉ des ruptures. Chaque valeur correspond à une réparation différente :
# une belt trouée se complète, une belt mal tournée se retourne, un bras mal placé se
# déplace. Les confondre reviendrait à ne rien diagnostiquer du tout.
OK = "ok"
INTERROMPUE = "belt_interrompue"
MAL_ORIENTEE = "belt_mal_orientee"
BRAS_ABSENT = "bras_absent"
BRAS_MAL_ORIENTE = "bras_mal_oriente"
BRAS_DEPOSE_VIDE = "bras_depose_dans_le_vide"


@dataclass
class RapportFlux:
    """Ce que le chemin a révélé. `continu` est la seule chose qui compte pour agir."""
    continu: bool
    tuiles: int                                   # segments parcourus depuis le départ
    cause: str = OK
    rupture: Optional[tuple[float, float]] = None  # là où ça casse
    detail: str = ""

    def __str__(self) -> str:
        if self.continu:
            return f"flux continu sur {self.tuiles} tuile(s) — {self.detail}"
        ou = f" en ({self.rupture[0]},{self.rupture[1]})" if self.rupture else ""
        return f"{self.cause}{ou} après {self.tuiles} tuile(s) — {self.detail}"


def _tuile(v: float) -> float:
    return math.floor(v) + 0.5


def _entites(api, x: float, y: float, rayon: float) -> list[dict]:
    r = api.inspect_at(x, y, rayon)
    return list(r.get("entities", [])) if isinstance(r, dict) else []


def _ici(e: dict, x: float, y: float, tol: float = 0.6) -> bool:
    return (abs(float(e.get("x", 1e9)) - x) < tol
            and abs(float(e.get("y", 1e9)) - y) < tol)


def suivre_flux(api, depart: tuple[float, float], cible_nom: str,
                cible_pos: Optional[tuple[float, float]] = None,
                garde: int = 80) -> RapportFlux:
    """Suit la belt depuis `depart` jusqu'à un bras qui charge `cible_nom`.

    Un seul `inspect_at` par tuile, de rayon 1.5 : il rend à la fois la belt de la tuile
    courante et les bras qui la bordent. Le chemin n'est jamais déduit d'un tracé
    théorique — on lit la direction RÉELLE de chaque segment et on avance dessus, ce qui
    est la seule façon de voir qu'un segment envoie ailleurs qu'on ne croit.

    Les tuiles déjà visitées sont mémorisées : y revenir signifie que deux segments se
    renvoient l'un à l'autre, ce qui est une boucle et non une interruption.
    """
    x, y = _tuile(depart[0]), _tuile(depart[1])
    vues: set[tuple[float, float]] = set()
    n = 0

    for _ in range(garde):
        autour = _entites(api, x, y, 1.5)
        belt = next((e for e in autour
                     if e.get("type") == "transport-belt" and _ici(e, x, y)), None)
        if belt is not None:
            n += 1                       # la tuile courante fait partie du chemin
            vues.add((x, y))

        # Un bras qui puise sur cette tuile termine le chemin, qu'il soit bien posé ou non.
        for e in autour:
            if e.get("type") != "inserter" or e.get("pickupX") is None:
                continue
            if not _ici({"x": e["pickupX"], "y": e["pickupY"]}, x, y):
                continue
            depose = [c for c in _entites(api, e["dropX"], e["dropY"], 0.3)
                      if c.get("name") == cible_nom]
            if depose:
                return RapportFlux(True, n, OK, None,
                                   f"{e.get('name')}@({e['x']},{e['y']}) charge {cible_nom}")
            return RapportFlux(
                False, n, BRAS_DEPOSE_VIDE, (float(e["x"]), float(e["y"])),
                f"{e.get('name')} dépose en ({e['dropX']},{e['dropY']}) où il n'y a pas "
                f"de {cible_nom}")

        if belt is None:
            if not n:
                return RapportFlux(False, 0, INTERROMPUE, (x, y),
                                   "aucune belt au départ du flux")
            # Un bras EXISTE-t-il ici sans puiser sur la belt ? La nuance vaut une
            # réparation : un bras qu'on tourne d'un quart de tour cesse de prendre sur
            # la belt, et le flux ne le rencontre alors jamais. Conclure « il manque un
            # bras » ferait en poser un second à côté du premier. Il faut le RETOURNER.
            proche = next((e for e in _entites(api, x, y, 2.0)
                           if e.get("type") == "inserter"), None)
            if proche is not None:
                return RapportFlux(
                    False, n, BRAS_MAL_ORIENTE,
                    (float(proche["x"]), float(proche["y"])),
                    f"{proche.get('name')} est là mais puise en "
                    f"({proche.get('pickupX')},{proche.get('pickupY')}), pas sur la belt")
            # Une belt qui s'arrête AU PIED de la cible n'est pas trouée : il lui manque
            # le bras qui déchargerait. Mesuré en E13, où le bras de retour vers le
            # foreur n'était plaçable nulle part et où la chaîne, autrement complète,
            # s'arrêtait à deux tuiles de son but.
            pres = (cible_pos is not None
                    and math.hypot(x - cible_pos[0], y - cible_pos[1]) <= 3.5)
            return RapportFlux(
                False, n, BRAS_ABSENT if pres else INTERROMPUE, (x, y),
                f"la belt s'arrête à {math.hypot(x - cible_pos[0], y - cible_pos[1]):.0f} "
                f"tuiles de {cible_nom}, sans bras pour décharger" if pres
                else "plus rien à cette position")

        dx, dy = AVANCE.get(str(belt.get("direction")), (0.0, 0.0))
        if (dx, dy) == (0.0, 0.0):
            return RapportFlux(False, n, MAL_ORIENTEE, (x, y),
                               f"direction illisible : {belt.get('direction')!r}")
        x, y = x + dx, y + dy
        if (x, y) in vues:
            return RapportFlux(False, n, MAL_ORIENTEE, (x, y),
                               "deux segments se renvoient l'un à l'autre")

    return RapportFlux(False, n, MAL_ORIENTEE, (x, y),
                       f"chemin toujours ouvert après {garde} tuiles : il tourne en rond")