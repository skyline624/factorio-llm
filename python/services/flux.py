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
    # Dernière tuile de belt VALIDE avant la rupture. C'est elle qu'il faut retourner
    # quand le chemin part de travers : `rupture` désigne la tuile visée à tort, pas la
    # coupable. Sans cette distinction, on répare l'endroit où le problème se voit au
    # lieu de celui où il est — exactement le travers que ce module corrige ailleurs.
    derniere: Optional[tuple[float, float]] = None

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
    derniere: Optional[tuple[float, float]] = None

    for _ in range(garde):
        autour = _entites(api, x, y, 1.5)
        belt = next((e for e in autour
                     if e.get("type") == "transport-belt" and _ici(e, x, y)), None)
        if belt is not None:
            n += 1                       # la tuile courante fait partie du chemin
            vues.add((x, y))
            derniere = (x, y)

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
                f"de {cible_nom}", derniere)

        if belt is None:
            if not n:
                return RapportFlux(False, 0, INTERROMPUE, (x, y),
                                   "aucune belt au départ du flux", None)
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
                    f"({proche.get('pickupX')},{proche.get('pickupY')}), pas sur la belt",
                    derniere)
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
                else "plus rien à cette position", derniere)

        dx, dy = AVANCE.get(str(belt.get("direction")), (0.0, 0.0))
        if (dx, dy) == (0.0, 0.0):
            return RapportFlux(False, n, MAL_ORIENTEE, (x, y),
                               f"direction illisible : {belt.get('direction')!r}", derniere)
        x, y = x + dx, y + dy
        if (x, y) in vues:
            return RapportFlux(False, n, MAL_ORIENTEE, (x, y),
                               "deux segments se renvoient l'un à l'autre", derniere)

    return RapportFlux(False, n, MAL_ORIENTEE, (x, y),
                       f"chemin toujours ouvert après {garde} tuiles : il tourne en rond",
                       derniere)

# ---------------------------------------------------------------------------
# RÉPARER ce qui a été diagnostiqué
# ---------------------------------------------------------------------------

def _direction_vers(depuis: tuple[float, float], vers: tuple[float, float]) -> str:
    dx, dy = vers[0] - depuis[0], vers[1] - depuis[1]
    if abs(dx) >= abs(dy):
        return "east" if dx > 0 else "west"
    return "south" if dy > 0 else "north"


def reparer_flux(api, depart: tuple[float, float], cible_nom: str,
                 cible_pos: Optional[tuple[float, float]] = None,
                 belt: str = "transport-belt", bras: str = "burner-inserter",
                 essais: int = 5) -> tuple[bool, str]:
    """Répare une chaîne rompue, et VÉRIFIE que la réparation a servi.

    Chaque tentative est jugée sur la mesure qui suit, jamais sur le fait qu'elle ait
    été appliquée : on re-suit le flux, et une action qui ne fait pas progresser le
    parcours est un échec, même si la pose a « réussi ». C'est la leçon la plus chère du
    projet — presque tous ses défauts ont été des actions acceptées sans erreur et sans
    effet.

    On boucle parce qu'une chaîne peut être cassée à plusieurs endroits : réparer le
    premier trou révèle le suivant. La progression du nombre de tuiles parcourues sert de
    garde-fou — si elle stagne, on rend la main plutôt que de s'acharner.
    """
    from services.site_finder import can_place, place_inserter_vers

    journal: list[str] = []
    progres = -1
    for _ in range(essais):
        r = suivre_flux(api, depart, cible_nom, cible_pos)
        if r.continu:
            return True, (f"chaîne rétablie sur {r.tuiles} tuile(s)"
                          + (f" — {' ; '.join(journal)}" if journal else ""))
        if r.tuiles <= progres:
            journal.append(f"{r.cause} : aucune progression, on rend la main")
            break
        progres = r.tuiles

        if r.cause in (INTERROMPUE, MAL_ORIENTEE) and r.derniere and r.rupture:
            # Deux réparations possibles, et c'est la MESURE qui tranche : soit la
            # dernière belt valide pointe de travers (on la retourne), soit il manque
            # bel et bien un segment (on le pose). Essayer la rotation d'abord évite
            # d'empiler des belts pour compenser une direction fautive.
            avant = r.tuiles
            # L'orientation d'origine est relue AVANT d'essayer, et remise si aucune
            # direction n'améliore. Sans cette restauration, la belt reste tournée vers
            # le dernier candidat essayé et la chaîne se retrouve cassée en AMONT du
            # trou qu'on venait réparer — on aggrave en croyant corriger.
            origine = next((e.get("direction") for e in
                            _entites(api, r.derniere[0], r.derniere[1], 0.4)
                            if e.get("type") == "transport-belt"), None)
            for cand in _voisines(r.derniere):
                d = _direction_vers(r.derniere, cand)
                api.run_action(api.rotate_entity_at, r.derniere[0], r.derniere[1], d,
                               belt, timeout=20.0)
                if suivre_flux(api, depart, cible_nom, cible_pos).tuiles > avant:
                    journal.append(f"belt {r.derniere} retournée vers {d}")
                    break
            else:
                if origine:
                    api.run_action(api.rotate_entity_at, r.derniere[0], r.derniere[1],
                                   origine, belt, timeout=20.0)
                x, y = r.rupture
                d = _direction_vers((x, y), cible_pos or (x + 1, y))
                if can_place(api, belt, x, y, d):
                    api.run_action(api.place_entity_at, belt, x, y, d, None, timeout=20.0)
                    journal.append(f"belt posée en ({x},{y}) vers {d}")
                else:
                    journal.append(f"({x},{y}) inconstructible")
                    break

        elif r.cause in (BRAS_ABSENT, BRAS_MAL_ORIENTE, BRAS_DEPOSE_VIDE):
            # Un bras mal placé se RETIRE avant d'en reposer un : le laisser occuperait
            # la seule tuile depuis laquelle un bras correct aurait pu travailler.
            if r.cause != BRAS_ABSENT and r.rupture:
                api.run_action(api.remove_entity_at, r.rupture[0], r.rupture[1], bras,
                               timeout=20.0)
                journal.append(f"bras mal posé retiré en {r.rupture}")
            if cible_pos is None or r.derniere is None:
                journal.append("cible ou fin de belt inconnue : bras non replaçable")
                break
            pose = place_inserter_vers(api, cible_pos, r.derniere, cible_nom, nom=bras)
            if pose is None:
                journal.append(f"aucun emplacement de {bras} n'atteint {cible_nom}")
                break
            journal.append(f"{bras} posé en {pose[:2]} ({pose[2]})")
            api.run_action(api.move_items_at, "coal", bras, pose[0], pose[1], 5, True,
                           timeout=20.0)
        else:
            journal.append(f"{r.cause} : aucune réparation connue")
            break

    final = suivre_flux(api, depart, cible_nom, cible_pos)
    return final.continu, (f"{final.cause} après réparation — "
                           f"{' ; '.join(journal) or 'rien tenté'}")


def _voisines(t: tuple[float, float]) -> list[tuple[float, float]]:
    return [(t[0] + 1, t[1]), (t[0] - 1, t[1]), (t[0], t[1] + 1), (t[0], t[1] - 1)]