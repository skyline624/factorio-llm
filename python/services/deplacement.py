"""Aller loin : générer le terrain, puis marcher par étapes.

`walk_to_entity` cherche une entité dans un rayon et demande un chemin. Cela suffit
pour un gisement à vingt tuiles, et cela échoue en silence pour tout ce qui est
au-delà de l'horizon généré : le pathfinding ne planifie pas à travers des chunks
qui n'existent pas encore, si bien que le personnage part, s'arrête beaucoup trop
tôt, et l'étape suivante se solde par « cible hors portée ».

C'est le mur sur lequel le bootstrap a buté : zéro tuile de charbon à moins de
soixante tuiles du gisement de fer, et la plus proche à **215 tuiles**. L'agent
savait quoi faire — miner du charbon — et n'arrivait simplement pas à s'en
approcher. Il fabriquait ses trois machines les mains vides puis tournait en rond,
faute de combustible pour les allumer.

Le remède était déjà dans la maison : `verify_walk_gen` et `verify_supply_prod`
franchissent 245 tuiles en générant devant eux et en marchant par bonds de soixante.
Il n'était écrit que dans des scripts de vérification — donc éprouvé, et hors de
portée des agents. On le remonte ici.

Deux garde-fous, tous deux payés d'expérience :

- **On rend la position RÉELLE, pas celle qu'on visait.** L'eau et les falaises ne
  se franchissent pas (leçon P2) ; un appelant qui croit être arrivé minerait le vide.
- **On s'arrête dès qu'un bond ne fait plus avancer.** Sans cela, un obstacle
  produit quarante allers-retours identiques et un timeout de plusieurs minutes.
"""

from __future__ import annotations

import math

# Un bond plus long qu'un chunk-et-demi sort de la zone générée avant que le
# pathfinding n'ait de quoi planifier ; plus court, on multiplie les allers-retours.
PAS = 60.0
RAYON_GEN = 60.0
TIMEOUT_MARCHE = 300.0
# En deçà, on considère qu'on est arrivé : la portée de minage du personnage est de
# l'ordre de quelques tuiles, et exiger l'exactitude ferait boucler sur un demi-pas.
TOLERANCE = 8.0


def position(api) -> tuple[float, float]:
    p = (api.get_state().get("character") or {}).get("position") or {}
    return float(p.get("x", 0.0)), float(p.get("y", 0.0))


def marcher_vers(api, x: float, y: float, bonds: int = 40,
                 interrompu_par=None, tolerance: float = TOLERANCE) -> tuple[float, float]:
    """Génère puis marche par bonds jusqu'à (x, y). Rend où l'on est VRAIMENT.

    `interrompu_par` permet d'ARRÊTER EN CHEMIN. Sans lui, un arrêt demandé restait sans
    effet tant que le personnage marchait : partie 28, le fer était à 109 tuiles, le joueur
    a écrit « alors arrête le », et deux minutes plus tard le chantier avançait encore. Les
    points de sortie posés jusque-là étaient dans la POSE puis dans la FORGE — or quand le
    gisement est loin, tout le temps se passe ici.

    On sort ENTRE deux bonds, jamais au milieu. Le personnage reste où il est arrivé : un
    état parfaitement valide, c'est ce que fait un joueur qui s'arrête en chemin.

    `tolerance` : à quelle distance on se déclare arrivé. HUIT TUILES SUFFISENT POUR ALLER
    QUELQUE PART, PAS POUR TOUCHER QUELQUE CHOSE. Le mod ne cherche la cible d'un minage
    que dans un rayon de CINQ tuiles ; entre cinq et huit, la marche réussit et le geste
    échoue toujours — mesuré partie 37, cinq `demonter` de suite en « cible hors portée »
    avec le personnage à 5,5 tuiles. Aller vers un endroit et venir toucher une machine ne
    demandent pas la même précision, et seul l'appelant sait ce qu'il fera en arrivant.
    """
    for _ in range(bonds):
        if interrompu_par is not None:
            try:
                if interrompu_par():
                    return position(api)
            except Exception:
                pass          # un interrupteur cassé n'immobilise personne
        cx, cy = position(api)
        reste = math.hypot(x - cx, y - cy)
        if reste <= tolerance:
            return cx, cy
        t = min(1.0, PAS / reste)
        ex, ey = cx + (x - cx) * t, cy + (y - cy) * t
        api.generate_terrain(ex, ey, RAYON_GEN)
        api.run_action(api.walk_to, ex, ey, timeout=TIMEOUT_MARCHE)
        nx, ny = position(api)
        if math.hypot(nx - cx, ny - cy) < 1.0:
            return nx, ny          # bloqué : eau, falaise, ou chemin introuvable
    return position(api)


def distance(api, x: float, y: float) -> float:
    cx, cy = position(api)
    return math.hypot(x - cx, y - cy)
