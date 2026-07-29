"""Quel gisement exploiter — l'énumération, pas la décision.

Jusqu'ici le choix était codé en dur : « le plus proche ». Ce n'est pas un calcul, c'est
un arbitrage, et il a été tranché sans jamais être posé.

Mesuré en jeu, sur une carte quelconque :

    fer, gisement A : 136 tuiles,  200 000 unités, à 174 tuiles
    fer, gisement B : 738 tuiles,  391 000 unités, à 280 tuiles

A est plus proche, B est cinq fois plus gros et durera cinq fois plus longtemps. Aucun
ne domine l'autre. Ajoutez qu'un gisement peut border un nid — la belt sera détruite
avant d'avoir servi — et le critère unique de distance devient franchement mauvais.

Ce module ÉNUMÈRE donc les options avec ce qui permet de les départager : taille,
richesse, distance, menace. Il n'en choisit aucune. Le déterministe garde le veto sur la
LÉGALITÉ (un gisement hors de portée d'une belt n'est pas une option), et le choix parmi
ce qui reste revient à l'arbitre — c'est le premier endroit du projet où plusieurs
réponses se valent réellement.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional


@dataclass
class Gisement:
    """Un gisement exploitable, avec de quoi le comparer aux autres."""
    resource: str
    x: float
    y: float
    tuiles: int
    reserve: int                 # somme des `amount` : combien on peut en tirer
    distance: float              # depuis le point d'exploitation visé
    nids: int = 0                # nids d'ennemis dans le rayon de garde
    nid_proche: Optional[float] = None    # distance du plus proche, si connu

    @property
    def sur(self) -> bool:
        """Aucun nid assez près pour menacer ce qu'on y bâtira."""
        return self.nids == 0

    def __str__(self) -> str:
        menace = ("aucun nid alentour" if self.sur
                  else f"{self.nids} nid(s), le plus proche à {self.nid_proche:.0f} tuiles")
        return (f"{self.resource} en ({self.x:.0f},{self.y:.0f}) : {self.tuiles} tuiles, "
                f"{self.reserve // 1000}k unités, à {self.distance:.0f} tuiles — {menace}")


def enumerer(api, resource: str, depuis: tuple[float, float],
             portee_max: float = 60.0, rayon_menace: float = 60.0,
             rayon_recherche: float = 300.0, evaluer_menace: bool = True
             ) -> list[Gisement]:
    """Les gisements LÉGAUX pour un point d'exploitation, du plus proche au plus lointain.

    `portee_max` est le veto du déterministe : au-delà, une belt coûte plus qu'elle ne
    rapporte et c'est un problème de train, pas de logistique locale. Proposer un
    gisement hors de portée reviendrait à offrir une option qui échouera — le contrat de
    l'arbitre promet des options exécutables.

    La menace est mesurée gisement par gisement (un appel chacun) : elle ne se déduit pas
    de la distance, et c'est justement ce qui peut renverser le classement.
    """
    brut = api.scan_patches(resource, rayon_recherche)
    patches = list((brut or {}).get("patches") or [])
    sortie: list[Gisement] = []
    for p in patches:
        try:
            gx, gy = float(p["x"]), float(p["y"])
        except (KeyError, TypeError, ValueError):
            continue
        d = math.hypot(gx - depuis[0], gy - depuis[1])
        if d > portee_max:
            continue
        g = Gisement(resource=resource, x=gx, y=gy,
                     tuiles=int(p.get("count", 0)), reserve=int(p.get("amount", 0)),
                     distance=d)
        if evaluer_menace:
            th = api.scan_threats(gx, gy, rayon_menace) or {}
            nids = list(th.get("nests") or [])
            g.nids = len(nids)
            if nids:
                g.nid_proche = min(float(n.get("dist", 1e9)) for n in nids)
        sortie.append(g)
    sortie.sort(key=lambda g: g.distance)
    return sortie