"""ThreatModel — faut-il se défendre, où, et à quel point. Déterministe.

Se défendre coûte du temps qui n'est pas passé à produire. La question n'est donc pas
« y a-t-il des ennemis » — il y en a toujours — mais **quand** l'investissement devient
justifié, et **de quel côté**.

LA RÈGLE QUI DÉCIDE : en Factorio, les vagues d'attaque partent quand le nuage de
pollution atteint un nid. Une usine qui ne pollue pas ne subit pas d'attaque, même
entourée de nids ; une usine qui pollue jusqu'aux nids sera attaquée même si elle est
loin. C'est donc **la pollution, pas la distance**, qui commande — et c'est ce qui
permet de ne pas fortifier trop tôt.

Deux exceptions traitées à part :
  - des unités déjà présentes autour de l'usine sont une menace immédiate, quelle que
    soit la pollution : elles sont là, la question du déclencheur ne se pose plus ;
  - `peaceful_mode` désactive les attaques : tout investissement défensif y est du
    temps perdu, et le dire évite de le gaspiller.

Le FRONT est la direction du nid le plus proche : c'est de là que viendront les vagues,
et fortifier le périmètre entier coûterait plusieurs fois plus pour rien.

Déterministe pur (règle agents-roadmap §3). L'arbitrage « produire ou se défendre »
n'est PAS ici : ce service dit ce qu'il en est, le Coordinator décide.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

# Niveaux de menace, du plus calme au plus urgent.
AUCUNE = 0      # rien à craindre (paix, ou aucun nid à portée)
LATENTE = 1     # des nids existent, mais la pollution ne les atteint pas encore
IMMINENTE = 2   # la pollution atteint ou approche les nids : les vagues vont partir
EN_COURS = 3    # des unités sont déjà sur l'usine

NIVEAU_NOM = {AUCUNE: "aucune", LATENTE: "latente", IMMINENTE: "imminente",
              EN_COURS: "en_cours"}

# Rayon au-delà duquel une unité ennemie n'est plus « sur l'usine ». Une unité qui
# rôde à 150 tuiles ne justifie pas la même réaction qu'une à 30.
RAYON_PROXIMITE = 60.0
# Pollution à partir de laquelle on considère que le nuage porte jusqu'aux nids.
# Mesuré à la position de l'usine : au-dessus, elle émet assez pour se faire remarquer.
SEUIL_POLLUTION = 10.0


@dataclass
class Menace:
    """Ce qu'on sait de la menace, et ce qu'il faut en faire."""
    niveau: int = AUCUNE
    raison: str = ""
    nids: int = 0
    unites: int = 0            # dans tout le rayon scanné (contexte)
    unites_proches: int = 0    # à moins de RAYON_PROXIMITE de l'usine (menace immédiate)
    pollution: float = 0.0
    distance_nid: Optional[float] = None
    front: Optional[tuple[float, float]] = None   # direction unitaire vers la menace
    front_nom: str = ""                           # "nord", "sud-est", ...
    notes: list[str] = field(default_factory=list)

    @property
    def agir(self) -> bool:
        """Faut-il investir dans la défense maintenant ?"""
        return self.niveau >= IMMINENTE

    def __str__(self) -> str:
        ou = f" au {self.front_nom}" if self.front_nom else ""
        return f"menace {NIVEAU_NOM.get(self.niveau, '?')}{ou} — {self.raison}"


_ROSE = [(0.0, -1.0, "nord"), (0.707, -0.707, "nord-est"), (1.0, 0.0, "est"),
         (0.707, 0.707, "sud-est"), (0.0, 1.0, "sud"), (-0.707, 0.707, "sud-ouest"),
         (-1.0, 0.0, "ouest"), (-0.707, -0.707, "nord-ouest")]


def _direction(dx: float, dy: float) -> tuple[tuple[float, float], str]:
    """Vecteur unitaire + nom de la direction la plus proche sur la rose des vents."""
    n = math.hypot(dx, dy)
    if n == 0:
        return (0.0, 0.0), ""
    ux, uy = dx / n, dy / n
    best = max(_ROSE, key=lambda r: ux * r[0] + uy * r[1])
    return (ux, uy), best[2]


def evaluer(scan: dict, usine: tuple[float, float] = (0.0, 0.0)) -> Menace:
    """Évalue la menace à partir d'un `scan_threats`. Fonction pure, testable seule.

    `usine` sert à orienter le front : la direction est calculée depuis l'usine à
    protéger, qui n'est pas forcément le point scanné.
    """
    if not isinstance(scan, dict) or scan.get("error"):
        return Menace(raison="menace non évaluable (scan illisible)",
                      notes=[str(scan)[:80]])

    m = Menace(
        nids=int(scan.get("nestCount") or 0),
        unites=int(scan.get("unitCount") or 0),
        pollution=float(scan.get("pollution") or 0.0),
    )

    if scan.get("peaceful") is True:
        m.niveau = AUCUNE
        m.raison = "mode pacifique : aucune attaque possible, ne rien investir ici"
        return m

    nids = scan.get("nests") or []
    if nids:
        proche = min(nids, key=lambda n: float(n.get("dist", 1e9)))
        m.distance_nid = float(proche.get("dist", 0.0))
        m.front, m.front_nom = _direction(float(proche.get("x", 0.0)) - usine[0],
                                          float(proche.get("y", 0.0)) - usine[1])

    # 1. Des unités PRÈS de l'usine : elles sont là, le déclencheur n'importe plus.
    #
    # On lit `unitsNear` (rayon restreint) et non `unitCount`. Mesuré en jeu : 39 unités
    # dans un rayon de 300 et 0 dans 60 — une carte normale grouille de biters au loin,
    # et les compter comme une attaque ferait fortifier en permanence. `nearest` ne
    # convient pas davantage : il rend l'ennemi le plus proche quel qu'il soit, nid
    # compris, et confond donc « un ver à 230 tuiles » avec « des biters sur l'usine ».
    m.unites_proches = int(scan.get("unitsNear") or 0)
    if m.unites_proches > 0:
        m.niveau = EN_COURS
        m.raison = (f"{m.unites_proches} unité(s) ennemie(s) à moins de "
                    f"{RAYON_PROXIMITE:.0f} tuiles de l'usine")
        return m

    if not m.nids:
        m.niveau = AUCUNE
        m.raison = "aucun nid dans le rayon scanné"
        return m

    # 2. La pollution est le déclencheur : sans elle, les nids restent inertes.
    if m.pollution >= SEUIL_POLLUTION:
        m.niveau = IMMINENTE
        m.raison = (f"pollution {m.pollution:.0f} et {m.nids} nid(s), le plus proche à "
                    f"{m.distance_nid:.0f} : les vagues vont partir")
    else:
        m.niveau = LATENTE
        m.raison = (f"{m.nids} nid(s), le plus proche à {m.distance_nid:.0f}, mais "
                    f"pollution {m.pollution:.0f} < {SEUIL_POLLUTION:.0f} : rien ne les "
                    f"déclenche encore")
        m.notes.append("fortifier maintenant serait du temps pris à la production")
    return m


def positions_defense(usine: tuple[float, float], menace: Menace, nombre: int = 3,
                      distance: float = 12.0, ecart: float = 4.0) -> list[tuple[float, float]]:
    """Emplacements de tourelles face au front, en arc devant l'usine.

    On ne ceinture pas l'usine : les vagues arrivent du côté des nids, et un périmètre
    complet coûterait plusieurs fois plus pour la même protection. Les positions sont
    réparties perpendiculairement au front, à `distance` de l'usine.
    """
    if not menace.front or nombre <= 0:
        return []
    ux, uy = menace.front
    px, py = -uy, ux                      # perpendiculaire au front
    centre = (usine[0] + ux * distance, usine[1] + uy * distance)
    debut = -(nombre - 1) / 2.0
    return [(round(centre[0] + px * (debut + i) * ecart, 1),
             round(centre[1] + py * (debut + i) * ecart, 1))
            for i in range(nombre)]