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


def tracer_en_l(depart: tuple[float, float], arrivee: tuple[float, float],
                eviter: Optional[set] = None, garde: int = 200
                ) -> tuple[list[tuple[float, float]], bool]:
    """Le chemin d'une belt, en L, en CONTOURNANT les tuiles d'un autre flux.

    Fonction pure : aucun accès au jeu, donc éprouvable hors ligne — et c'est voulu, car
    le défaut qu'elle corrige était invisible entité par entité.

    UNE BELT NE TRAVERSE PAS LA LIGNE D'UN AUTRE. `place_belt_line` retourne les belts
    déjà présentes sur son tracé pour les aligner sur elle : excellent quand on prolonge
    SA propre ligne, désastreux quand on croise celle d'un voisin. Mesuré en jeu : la
    sortie des engrenages a croisé la belt qui amenait le fer, l'a retournée, et les
    pièces sont reparties vers l'assembleuse dont elles venaient — vingt-trois engrenages
    produits, zéro arrivé. Chaque entité, prise séparément, avait l'air juste.

    Deux formes de L sont essayées (horizontal d'abord, puis vertical d'abord), et à
    défaut on décale le coude. Rend (tuiles, propre) où `propre` dit qu'aucune tuile
    interdite n'est empruntée — l'appelant peut alors refuser plutôt que de casser un
    flux existant.
    """
    interdites = eviter or set()
    x0, y0 = math.floor(depart[0]) + 0.5, math.floor(depart[1]) + 0.5
    x1, y1 = math.floor(arrivee[0]) + 0.5, math.floor(arrivee[1]) + 0.5

    def _chemin(coude_x: float, coude_y: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        x, y = x0, y0
        while abs(x - coude_x) > 1e-6 and len(out) < garde:
            out.append((x, y))
            x += 1.0 if coude_x > x else -1.0
        while abs(y - coude_y) > 1e-6 and len(out) < garde:
            out.append((x, y))
            y += 1.0 if coude_y > y else -1.0
        while abs(x - x1) > 1e-6 and len(out) < garde:
            out.append((x, y))
            x += 1.0 if x1 > x else -1.0
        while abs(y - y1) > 1e-6 and len(out) < garde:
            out.append((x, y))
            y += 1.0 if y1 > y else -1.0
        return out

    candidats = [_chemin(x1, y0), _chemin(x0, y1)]
    # Coudes décalés, dans les QUATRE directions : une ligne qui barre tout un axe ne se
    # contourne que par au-delà de son extrémité. Mesuré par le test unitaire — un
    # barrage vertical complet entre le départ et l'arrivée n'était franchi par aucune
    # des deux formes de L, ni par un coude décalé sur le seul axe de départ.
    for pas in (1, -1, 2, -2, 3, -3, 4, -4, 5, -5, 6, -6):
        candidats.append(_chemin(x1, y0 + pas))
        candidats.append(_chemin(x0 + pas, y1))
        candidats.append(_chemin(x0, y1 + pas))
        candidats.append(_chemin(x1 + pas, y0))
    for chemin in candidats:
        if chemin and not any(t in interdites for t in chemin):
            return chemin, True
    # Aucun tracé propre : on rend le plus court, et l'appelant décide.
    plus_court = min((c for c in candidats if c), key=len, default=[])
    return plus_court, not any(t in interdites for t in plus_court)


def place_belt_line(api, depart: tuple[float, float], arrivee: tuple[float, float],
                    belt: str = "transport-belt", timeout: float = 20.0,
                    garde: int = 200, portee: float = 0.0,
                    eviter: Optional[set] = None
                    ) -> tuple[list[tuple[float, float]], bool]:
    """Pose une belt de `depart` vers `arrivee`, en L, chaque segment ORIENTÉ vers l'aval.

    L'orientation est ce qui distingue une belt d'une file d'objets décoratifs : une
    seule mal tournée et le flux s'arrête là, sans que rien ne le signale à la pose.
    On avance d'une tuile à la fois en réglant la direction sur le pas suivant, et le
    dernier segment pointe vers `arrivee`.

    Trajet en L (horizontal puis vertical) : suffisant pour relier une mine à une
    machine, et prévisible — un chemin optimal serait plus court mais impossible à
    déboguer à l'œil dans le jeu.
    """
    x1, y1 = math.floor(arrivee[0]) + 0.5, math.floor(arrivee[1]) + 0.5
    tuiles, propre = tracer_en_l(depart, arrivee, eviter, garde)
    if not propre:
        # Emprunter la voie d'un autre flux revient à le détourner (on retourne ses
        # tuiles plus bas). Mieux vaut ne rien poser que casser ce qui marchait.
        return [], False

    poses: list[tuple[float, float]] = []
    # `portee` > 0 : le personnage SUIT la belt au lieu de la dérouler sur place.
    #
    # Mesuré en production, joueur connecté : `build_distance` vaut 10, et le mod refuse
    # toute pose au-delà — « walk closer first ». Une ligne de quarante tuiles posée sans
    # bouger s'arrête donc au dixième segment. Le mode test masquait entièrement la
    # contrainte : le character headless bâtit à n'importe quelle distance.
    ancre = None
    for i, (bx, by) in enumerate(tuiles):
        if portee > 0.0:
            if ancre is None or math.hypot(bx - ancre[0], by - ancre[1]) > portee - 2.0:
                api.run_action(api.walk_to, bx, by, timeout=max(timeout, 90.0))
                ancre = (bx, by)
        suivant = tuiles[i + 1] if i + 1 < len(tuiles) else (x1, y1)
        dx, dy = suivant[0] - bx, suivant[1] - by
        if abs(dx) >= abs(dy):
            d = "east" if dx > 0 else "west"
        else:
            d = "south" if dy > 0 else "north"
        # Une belt DÉJÀ là, sur le tracé : on ne la contourne pas, on la RETOURNE. En
        # prolongeant une ligne, la dernière tuile de l'ancien tronçon garde la direction
        # qu'elle avait et envoie le charbon vers une tuile vide : 31 segments parcourus
        # puis plus rien, à une tuile du but, sans qu'aucune pose n'ait échoué.
        #
        # Le test se fait par LECTURE et non via `can_place` : poser une belt sur une
        # belt est un remplacement rapide, que le jeu AUTORISE. `can_place` répond donc
        # `true` et n'a jamais signalé l'occupation — la correction paraissait en place
        # et ne s'exécutait pas.
        deja = next((e for e in _entites_a(api, bx, by, 0.4)
                     if e.get("type") == "transport-belt"), None)
        if deja is not None:
            if deja.get("direction") != d:
                api.run_action(api.rotate_entity_at, bx, by, d, belt, timeout=timeout)
            poses.append((bx, by))
            continue
        if not can_place(api, belt, bx, by, d):
            # Un seul arbre sur le tracé suffit à couper le flux : la belt se pose de
            # part et d'autre, aucune erreur n'est levée, et le charbon s'accumule sur
            # les six premières tuiles sans jamais arriver. On abat donc ce qui gêne,
            # tuile par tuile — c'est ce que fait un joueur, et le seul obstacle qu'on
            # s'autorise à ôter est celui que la nature a mis là.
            if not degager_tuile(api, bx, by, timeout) or not can_place(api, belt, bx, by, d):
                continue                 # infranchissable : la belt aura un trou, on le dira
        r = api.run_action(api.place_entity_at, belt, bx, by, d, None, timeout=timeout)
        if isinstance(r, dict) and r.get("ok"):
            poses.append((bx, by))

    # ON REPASSE SUR LES TROUS. Une belt à laquelle il manque UNE tuile ne transporte
    # rien, et rien ne le signale : les objets s'accumulent au bord du trou pendant que
    # chaque entité, prise séparément, a l'air juste. Mesuré sur une ligne de soixante-
    # quinze tuiles : deux trous seulement, sur de la terre ordinaire — l'un parce que
    # l'avatar s'y tenait au moment de la pose, l'autre sans cause durable. Une seconde
    # passe les comble, et c'est la règle du projet : on ne croit pas une pose, on la
    # confirme.
    manquantes = [t for t in tuiles if t not in poses]
    for bx, by in manquantes:
        i = tuiles.index((bx, by))
        suivant = tuiles[i + 1] if i + 1 < len(tuiles) else (x1, y1)
        dx, dy = suivant[0] - bx, suivant[1] - by
        d = ("east" if dx > 0 else "west") if abs(dx) >= abs(dy) else \
            ("south" if dy > 0 else "north")
        if any(e.get("type") == "transport-belt" for e in _entites_a(api, bx, by, 0.4)):
            poses.append((bx, by))
            continue
        if not can_place(api, belt, bx, by, d):
            degager_tuile(api, bx, by, timeout)
        api.run_action(api.place_entity_at, belt, bx, by, d, None, timeout=timeout)
        if any(e.get("type") == "transport-belt" for e in _entites_a(api, bx, by, 0.4)):
            poses.append((bx, by))
    return poses, len(poses) == len(tuiles)


def _entites_a(api, x: float, y: float, rayon: float = 0.3) -> list[dict]:
    r = api.inspect_at(x, y, rayon)
    return list(r.get("entities", [])) if isinstance(r, dict) else []


# Ce qu'on s'autorise à enlever pour faire passer une ligne : la nature, et rien d'autre.
# Un rasage large détruit ce qu'on vient de bâtir — le projet l'a payé une fois. Ici on
# lit d'abord CE QUI est sur la tuile, et on ne retire que si c'est un arbre ou un rocher.
OBSTACLES_NATURELS = ("tree", "simple-entity")


def degager_tuile(api, x: float, y: float, timeout: float = 20.0) -> bool:
    """Enlève arbres et rochers d'une tuile. Ne touche à aucune construction."""
    retire = False
    for e in _entites_a(api, x, y, 0.4):
        if e.get("type") in OBSTACLES_NATURELS:
            r = api.run_action(api.remove_entity_at, x, y, e.get("name"), timeout=timeout)
            retire = retire or (isinstance(r, dict) and r.get("ok") is True)
    return retire


def poteau_alimente_le_plus_proche(api, x: float, y: float, rayon: float = 80.0
                                   ) -> Optional[tuple[float, float, int]]:
    """Le poteau relié à un réseau QUI PRODUIT, le plus proche de (x, y). Ou None.

    C'est le point de départ d'une ligne de transmission. Sans lui, `relier` ne pouvait
    que poser un poteau contre la machine — ce qui suffit tant que le réseau est à portée
    et ne sert à rien dès qu'il ne l'est plus.

    AVOIR UN `electric_network_id` N'EST PAS ÊTRE ALIMENTÉ, et la première version de
    cette fonction l'a confondu. Tout poteau posé reçoit un identifiant de réseau, même
    isolé au milieu de nulle part. `relier` tirait donc des lignes vers des réseaux morts
    et fabriquait des îlots : mesuré en partie longue, réseau 128 avec 3 générateurs et
    6 consommateurs, réseau 129 avec ZÉRO générateur et 5 consommateurs — cinq machines
    branchées sur du vide, `connected=false`, `bufferEnergy=0` — et un réseau 131 avec
    2 générateurs et aucun consommateur, c'est-à-dire une centrale bâtie pour rien.

    On établit donc d'abord quels réseaux ont un GÉNÉRATEUR, et l'on ne retient que
    ceux-là. Même famille que la leçon d'E3 : un générateur ne produit que ce qui est
    consommé, et un identifiant ne dit rien de ce qui circule.
    """
    try:
        brut = api.rcon.query_lua(
            f"local s = game.surfaces[1] local vivants = {{}} "
            # Un réseau n'est vivant que s'il porte de quoi produire. Les boilers ne
            # comptent pas : ils chauffent, ils ne génèrent pas d'électricité.
            f"for _, g in pairs(s.find_entities_filtered{{type={{'generator', "
            f"'solar-panel', 'reactor'}}}}) do "
            f"  if g.electric_network_id then vivants[g.electric_network_id] = true end end "
            f"local best, bd = nil, 1e18 "
            f"for _, e in pairs(s.find_entities_filtered{{type='electric-pole', "
            f"area={{{{{x - rayon},{y - rayon}}},{{{x + rayon},{y + rayon}}}}}}}) do "
            f"  local id = e.electric_network_id "
            f"  if id and vivants[id] then "
            f"    local d = (e.position.x - {x})^2 + (e.position.y - {y})^2 "
            f"    if d < bd then bd = d best = e end end end "
            f"if best then rcon.print(best.position.x .. ',' .. best.position.y .. ',' "
            f".. best.electric_network_id) else rcon.print('') end")
    except Exception:
        return None
    morceaux = str(brut).strip().split(",")
    if len(morceaux) != 3:
        return None
    try:
        return (float(morceaux[0]), float(morceaux[1]), int(float(morceaux[2])))
    except ValueError:
        return None


def ancres_libres_sur_minerai(api, bbox: dict, autour: tuple[float, float],
                              ecart: int = 4, plafond: int = 8,
                              degagement: float = 2.0,
                              distance_max: Optional[float] = None
                              ) -> list[tuple[float, float]]:
    """Tuiles de minerai NON bâties d'un gisement, les plus proches de `autour` d'abord.

    Pourquoi ce détour, alors que `scan_patch` renvoie déjà un `sample` : mesuré en jeu,
    ce sample vaut 12 tuiles et reste IDENTIQUE quel que soit le rayon demandé — 8, 16,
    32, 64 ou 128 donnent les mêmes douze tuiles, groupées dans un coin, donc deux ancres
    espacées seulement. Le `bbox`, lui, décrivait un gisement de 31 × 35 tuiles. La place
    existait ; l'échantillon ne la montrait pas, et l'agent concluait « aucune place
    libre » au bord d'un gisement presque intact.

    On interroge donc les tuiles de ressource du bbox et l'on écarte celles qui ont déjà
    une construction à `degagement` tuiles : un mining-drill occupe 3 × 3, l'ancrer contre
    l'existant ferait échouer le four en bout de chaîne — ce qui est arrivé, et ce que le
    seul test du drill ne voyait pas.

    Le tri par distance à `autour` (l'usine) est délibéré : une chaîne posée à cent tuiles
    est hors de portée du réseau électrique et de la boucle qui la surveille.
    """
    try:
        x1, y1 = float(bbox["x1"]), float(bbox["y1"])
        x2, y2 = float(bbox["x2"]), float(bbox["y2"])
    except (KeyError, TypeError, ValueError):
        return []
    try:
        brut = api.rcon.query_lua(
            f"local s = game.surfaces[1] local out = {{}} "
            f"for _, e in pairs(s.find_entities_filtered{{type='resource', "
            f"area={{{{{x1},{y1}}},{{{x2},{y2}}}}}}}) do "
            f"  local p = e.position "
            f"  if #s.find_entities_filtered{{force='player', "
            f"area={{{{p.x - {degagement}, p.y - {degagement}}},"
            f"{{p.x + {degagement}, p.y + {degagement}}}}}}} == 0 then "
            f"    out[#out+1] = math.floor(p.x) .. ',' .. math.floor(p.y) end end "
            # Le nombre de tuiles libres d'un gisement neuf se compte en centaines : on
            # tronque côté Lua pour ne pas faire voyager une réponse RCON inutile.
            f"rcon.print(table.concat(out, ';', 1, math.min(#out, 400)))")
    except Exception:
        return []
    libres: list[tuple[float, float]] = []
    for morceau in str(brut).strip().split(";"):
        cx, _, cy = morceau.partition(",")
        try:
            libres.append((float(int(cx)), float(int(cy))))
        except ValueError:
            continue
    libres.sort(key=lambda p: math.hypot(p[0] - autour[0], p[1] - autour[1]))
    if distance_max is not None:
        # Un gisement s'étend bien au-delà de ce qu'une usine peut desservir. Mesuré :
        # faute de plafond, une extension est allée s'ancrer à 66 tuiles — hors du rayon
        # diagnostiqué et hors du réseau électrique. L'Enquêteur l'a constatée absente
        # (« inspect_at(-93,-59) a retourné entities: [] ») alors qu'elle avait bien été
        # posée : elle était simplement là où personne ne regardait. Mieux vaut dire
        # qu'il n'y a plus de place que d'en prendre une inexploitable.
        libres = [p for p in libres
                  if math.hypot(p[0] - autour[0], p[1] - autour[1]) <= distance_max]
    retenus: list[tuple[float, float]] = []
    for p in libres:
        if len(retenus) >= plafond:
            break
        if all(max(abs(p[0] - q[0]), abs(p[1] - q[1])) >= ecart for q in retenus):
            retenus.append(p)
    return retenus


def place_inserter_vers(api, cible: tuple[float, float], source: tuple[float, float],
                        cible_nom: str, nom: str = "inserter",
                        source_types: tuple[str, ...] = ("transport-belt",),
                        essais: int = 24, timeout: float = 20.0,
                        journal: Optional[list] = None,
                        cible_pos: Optional[tuple[float, float]] = None
                        ) -> Optional[tuple[float, float, str]]:
    """Pose un inserter qui prend RÉELLEMENT sur la source et dépose RÉELLEMENT dans la cible.

    Un inserter mal orienté se pose sans erreur, affiche un statut de bras qui attend,
    et ne transporte rien : mesuré en jeu, l'inserter était à 2.5 tuiles de son boiler
    et déposait dans le vide du côté opposé. Rien dans la réponse de pose ne le disait.

    On ne DÉDUIT donc plus le sens d'un inserter d'une convention de direction — la
    mesure contredit d'ailleurs celle qu'on attendait : orienté `north`, il prend en
    y−1 et dépose en y+1.3. On pose, on LIT `pickup`/`drop` réels, et on tourne jusqu'à
    ce que les deux tombent où il faut. Un emplacement qui n'y arrive dans aucune des
    quatre directions est libéré avant d'essayer le suivant.

    Retourne (x, y, direction) ou None si aucun emplacement ne convient.
    """
    cx, cy = cible
    cands: list[tuple[float, float, float]] = []
    for dx in range(-3, 4):
        for dy in range(-3, 4):
            if dx == 0 and dy == 0:
                continue
            x = math.floor(cx + dx) + 0.5
            y = math.floor(cy + dy) + 0.5
            cands.append((math.hypot(x - source[0], y - source[1]), x, y))
    cands.sort()

    for _, ix, iy in cands[:essais]:
        if not can_place(api, nom, ix, iy):
            if journal is not None:
                journal.append(f"({ix},{iy}) occupe")
            continue
        r = api.run_action(api.place_entity_at, nom, ix, iy, "north", None, timeout=timeout)
        if not (isinstance(r, dict) and r.get("ok")):
            if journal is not None:
                journal.append(f"({ix},{iy}) pose refusee")
            continue
        for d in DIR_NOM.values():
            if d != "north":
                api.run_action(api.rotate_entity_at, ix, iy, d, nom, timeout=timeout)
            ins = next((e for e in _entites_a(api, ix, iy, 0.4)
                        if e.get("type") == "inserter"), None)
            if ins is None or ins.get("dropX") is None or ins.get("pickupX") is None:
                break                      # le mod ne rend pas pickup/drop : on renonce
            prend = any(e.get("type") in source_types or e.get("name") in source_types
                        for e in _entites_a(api, ins["pickupX"], ins["pickupY"]))
            depose = any(e.get("name") == cible_nom
                         for e in _entites_a(api, ins["dropX"], ins["dropY"]))
            # LE NOM NE SUFFIT PAS QUAND IL Y A PLUSIEURS CIBLES DU MÊME NOM. Mesuré :
            # le bras de sortie des engrenages déposait sur une `transport-belt` — la
            # bonne réponse à la question posée — mais c'était celle d'un AUTRE flux,
            # celui qui amenait le fer à cette même assembleuse. Les pièces repartaient
            # d'où elles venaient : vingt-trois produites, zéro arrivée, et rien dans la
            # pose qui le signale. Quand l'appelant sait sur QUELLE tuile déposer, il le
            # dit, et l'on exige cette tuile-là.
            #
            # On compare des DISTANCES, pas des arrondis : un bras dépose à environ 1.3
            # tuile de son centre, sur un point qui n'est pas celui d'une tuile. Et le
            # test ne vaut que pour une cible d'UNE tuile (une belt) : sur une machine
            # 3x3 le dépôt tombe sur un bord, à plus d'une tuile du centre, et exiger le
            # centre refuserait toutes les poses — mesuré, 2/6 au lieu de 5/6.
            if depose and cible_pos is not None:
                depose = (abs(ins["dropX"] - cible_pos[0]) < 0.9
                          and abs(ins["dropY"] - cible_pos[1]) < 0.9)
            if journal is not None:
                journal.append(f"({ix},{iy}) {d} prend={prend} depose={depose}")
            if prend and depose:
                return (ix, iy, d)
        api.run_action(api.remove_entity_at, ix, iy, nom, timeout=timeout)
    return None


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