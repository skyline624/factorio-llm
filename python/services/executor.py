"""Executor E1 — pose en jeu une micro-chaîne planifiée (MicroPlan).

Maillon manquant entre les planners (déterministes, qui CALCULENT un plan) et le jeu
(qui doit le BÂTIR). Jusqu'ici chaque script `verify_*.py` réimplémentait sa propre
boucle de pose ad hoc (`place_entity_at` + `wait(4.0)` arbitraire, `DIR_TO_STR` recopié
dans dix fichiers). `execute_micro` centralise cette mécanique, avec les règles apprises
en live.

Chaîne complète :

    scan_patch -> plan_micro (MicroPlanner)  ->  execute_micro (ici)  ->  entités en jeu
                  [calcul déterministe]           [pose + fuel]

Périmètre E1 : `MicroPlan` (3 entités, tout-burner), intégralement couvert par le mod
actuel — aucune extension Lua requise. Le `LayoutPlan` complet viendra ensuite : il
exige d'étendre `place_entity_at` (cf. « Limites connues » plus bas).

Frontière (§7 `docs/layout-planner.md`) — l'executor **adapte à la marge** (offset ±1
tuile, ordre de pose, alimentation). Il ne replanifie PAS, ne crafte PAS, ne choisit PAS
de gisement : cela reste à FactoryBuilder (replan lourd S4c). Un plan infaisable revient
donc en `blocked`, pas en plan modifié.

Règles apprises en live et intégrées ici
----------------------------------------
1. **Pré-vol inventaire obligatoire.** Le mod ne vérifie PAS l'inventaire avant de poser :
   `task_manager.lua` fait `create_entity` PUIS `inv.remove`. Sans contrôle côté Python
   on bâtit des usines gratuites (triche) et les tests deviennent faux.
2. **`run_action` et non `place_entity_at` + `wait`.** La pose est asynchrone
   (task_manager sur `on_tick`) ; `run_action` attend la complétion réelle via
   `completion_seq` (race-free), là où un `wait(4.0)` est un pari.
3. **`generate_terrain` avant d'approcher** (S4d) : sans chunks générés, le pathfinding
   ne planifie pas et le character n'arrive jamais.
4. **`move_items_at` et non `move_items`** pour le combustible : ciblage par POSITION
   (rayon 1.5) au lieu du nom (rayon 32) — sans ambiguïté quand plusieurs fours existent.
5. **Tout burner reçoit du combustible, y compris le `burner-inserter`.** Un burner
   inserter ne s'auto-alimente que s'il manipule du charbon ; ici il manipule du minerai,
   donc sans injection il reste immobile et toute la chaîne est morte.
6. **Décalage SOLIDAIRE, jamais par entité** (run live E1). Les positions d'un `MicroPlan`
   sont relatives entre elles à la tuile près (drop du drill = pickup de l'inserter).
   Décaler le drill de +1y et l'inserter de +1x a donné 3 entités posées, `ok=True`, et
   zéro production. On translate le plan entier ou on rend la main.
7. **Ne jamais croire le `ok=True` du mod sur parole** (`verify=True`). Run live E1 : le
   rapport annonçait drill + inserter posés, la carte n'en portait qu'un et l'inventaire
   de drills était intact. La pose n'est confirmée que si l'item quitte l'inventaire.

Limites connues (levées au chantier suivant)
--------------------------------------------
- `place_entity_at` (Lua) fait `create_entity{name, position, direction, force}` : il
  ignore `LayoutEntity.ug_type` (underground-belt input/output), `priority` (splitter)
  et `modules` (beacon). L'executor `LayoutPlan` complet exigera une extension du mod.
- `scan_factory` filtre sur `PRODUCER_TYPES` (`tools.lua:21`) qui n'inclut PAS
  `inserter` : l'inserter ne se vérifie pas directement, seulement par son effet (le
  furnace ne passe `working` que s'il est alimenté en minerai).
- `can_place_check` reste aveugle aux entités du même plan non encore posées : le
  décalage solidaire vérifie donc tout AVANT la première pose (carte identique pour
  toutes les entités), et la pose reste vérifiée a posteriori par l'inventaire.
- `create_entity` SNAPPE la position sur la grille de l'entité : mesuré live, une entité
  2×2 (drill, four) atterrit sur la position entière, une 1×1 (inserter) sur le centre
  de tuile (demandé (71,-20) -> réel (71.5,-19.5)). Le MicroPlanner raisonne en coins de
  tuile, ce qui tombe juste après snap, mais `can_place_check` teste la position DEMANDÉE :
  les deux peuvent diverger d'une demi-tuile. À rendre explicite dans le planner.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Optional

from services import deplacement, perception


# Directions Factorio : 0=N(-Y), 2=E(+X), 4=S(+Y), 6=W(-X). `place_entity_at` et
# `can_place_check` acceptent le nom (le Lua convertit via direction_from_name), alors
# que les planners produisent l'entier. Table unique du projet (remplace les copies
# locales des scripts verify_*).
DIR_TO_STR: dict[int, str] = {0: "north", 2: "east", 4: "south", 6: "west"}

# Portee de CONSTRUCTION du personnage (`build_distance + 2` cote mod). Au-dela, la pose
# est refusee : « walk closer first ». On garde une marge -- arriver pile a la limite
# laisserait la moindre derive de position hors de portee.
PORTEE_POSE = 8.0

# OU L'ON S'ARRETE en s'approchant d'une pose. Jamais sur la tuile visee : `can_place`
# en mode manuel refuse l'emplacement ou se tient le personnage, et une seule entite
# refusee fait abandonner le plan entier. Trois tuiles laissent l'entite libre (les plus
# grandes du bootstrap font 2x2) tout en restant largement dans `PORTEE_POSE`.
RECUL_POSE = 3.0

# Candidats de repli quand `can_place_check` refuse la position calculée. Bornés et
# ordonnés du plus proche au plus éloigné : l'executor absorbe l'imprécision du bbox de
# `scan_patch` (qui ne garantit pas que le centre soit une tuile de minerai), pas une
# erreur de plan. Même esprit que `_FURNACE_OFFSETS` (agents/base.py).
RETRY_OFFSETS: tuple[tuple[float, float], ...] = (
    (1.0, 0.0), (-1.0, 0.0), (0.0, 1.0), (0.0, -1.0),
    (1.0, 1.0), (-1.0, -1.0), (1.0, -1.0), (-1.0, 1.0),
)

# Entités à combustible dont le nom ne commence pas par "burner-". Le `boiler` en fait
# partie : il brûle du charbon pour produire la vapeur d'une centrale. Sans lui dans
# cette liste, l'executor pose une centrale complète et ne l'allume jamais.
_BURNER_NAMES = frozenset({"stone-furnace", "steel-furnace", "boiler"})


def _place_opts(e) -> Optional[dict]:
    """Options de pose portées par une LayoutEntity (E2) — None si elle n'en a aucune.

    Le LayoutPlanner CALCULE ces champs depuis S1 (`ug_type`, `priority`) et S3
    (`modules`) ; jusqu'à E2 le mod ne savait pas les poser, donc un `LayoutPlan`
    complet n'était pas exécutable. Lecture par `getattr` : le `MicroPlan` partage la
    dataclass mais n'utilise aucun de ces champs, et `recipe` n'existe pas encore sur
    `LayoutEntity` (il arrivera avec l'étape « usine automatisée »).
    """
    opts: dict = {}
    ug = getattr(e, "ug_type", "")
    if ug:
        opts["ug_type"] = ug
    # `LayoutEntity.priority` est la priorité de SORTIE du splitter (cf. sa définition).
    prio = getattr(e, "priority", "")
    if prio:
        opts["priority_out"] = prio
    mods = getattr(e, "modules", None)
    if mods:
        # Le planner liste les modules avec répétition ; le mod attend {nom: nombre}.
        counts: dict[str, int] = {}
        for m in mods:
            counts[m] = counts.get(m, 0) + 1
        opts["modules"] = counts
    recipe = getattr(e, "recipe", "")
    if recipe:
        opts["recipe"] = recipe
    return opts or None


def is_burner(name: str) -> bool:
    """L'entité consomme-t-elle du combustible solide (donc à alimenter à la pose) ?

    True pour `burner-mining-drill`, `burner-inserter`, `stone-furnace`, `steel-furnace`.
    False pour les tiers électriques (`electric-mining-drill`, `fast-inserter`,
    `electric-furnace`) qui tirent leur énergie du réseau.
    """
    return name.startswith("burner-") or name in _BURNER_NAMES


@dataclass
class PlacedEntity:
    """Une entité effectivement posée en jeu, avec son écart éventuel au plan."""
    idx: int                                    # index dans plan.entities
    name: str
    x: float
    y: float
    direction: int                              # 0/2/4/6 (tel que planifié)
    role: str
    offset: tuple[float, float] = (0.0, 0.0)    # décalage appliqué vs plan (retry)


@dataclass
class ExecutionReport:
    """Compte rendu d'exécution — tout ce dont FactoryBuilder a besoin pour arbitrer.

    `ok` est vrai seulement si toutes les entités utiles du plan sont posées, sans
    manque d'inventaire ni blocage. `blocked` non vide = handoff vers le replan lourd
    (S4c) : changer de gisement ou de tier, décision qui n'appartient pas à l'executor.
    """
    ok: bool = False
    placed: list[PlacedEntity] = field(default_factory=list)
    # (idx, name, x, y, raison) — position(s) refusée(s), retries épuisés.
    blocked: list[tuple[int, str, float, float, str]] = field(default_factory=list)
    missing: dict[str, int] = field(default_factory=dict)   # {item: quantité manquante}
    fueled: dict[str, int] = field(default_factory=dict)    # {entity_name: unités injectées}
    steps: list[str] = field(default_factory=list)          # journal ordonné
    notes: list[str] = field(default_factory=list)


def _dir_str(direction: int) -> str:
    """Direction entière -> nom attendu par le mod. Défaut "north" si hors 0/2/4/6."""
    return DIR_TO_STR.get(direction, "north")


def _useful_entities(plan) -> list[tuple[int, object]]:
    """[(idx, entity)] des entités à poser, `skip=True` filtré.

    `LayoutEntity.skip` marque une lane RETIRÉE du plan (place libérée pour un splitter
    de tap/feed ou un underground) : la spec S1f dit explicitement que le consommateur
    filtre `not skip`. `MicroPlan` n'en produit pas, mais l'executor doit être correct
    pour les deux formes de plan.
    """
    return [(i, e) for i, e in enumerate(plan.entities) if not getattr(e, "skip", False)]


def _topological_order(plan, useful: list[tuple[int, object]]) -> list[tuple[int, object]]:
    """Ordonne les entités selon le graphe de flux `plan.connections`.

    Tri de Kahn sur les arêtes (from_idx -> to_idx) : une entité est posée après ses
    producteurs. Sur un `MicroPlan` cela donne drill -> inserter -> furnace (déjà l'ordre
    de la liste) ; l'intérêt est la généricité pour un `LayoutPlan`, dont l'ordre de liste
    ne suit pas le flux. Repli sur l'ordre de la liste si le graphe est vide ou cyclique
    (jamais le cas des planners, mais on ne bloque pas dessus).
    """
    keep = {i for i, _ in useful}
    edges = [(a, b) for a, b, *_ in plan.connections if a in keep and b in keep]
    if not edges:
        return useful

    indeg = {i: 0 for i in keep}
    succ: dict[int, list[int]] = {i: [] for i in keep}
    for a, b in edges:
        succ[a].append(b)
        indeg[b] += 1

    # File initiale dans l'ordre de la liste (déterminisme : deux plans identiques
    # produisent la même séquence de pose).
    order: list[int] = []
    queue = [i for i, _ in useful if indeg[i] == 0]
    while queue:
        cur = queue.pop(0)
        order.append(cur)
        for nxt in succ[cur]:
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                queue.append(nxt)

    if len(order) != len(keep):        # cycle -> ordre de la liste (fail-safe)
        return useful

    by_idx = dict(useful)
    return [(i, by_idx[i]) for i in order]


def _required_items(plan, useful: list[tuple[int, object]],
                    fuel: str, fuel_count: int) -> dict[str, int]:
    """Items nécessaires : une unité par entité à poser + le combustible des burners.

    Calculé sur `useful` (et non sur `plan.totals`) pour rester juste quand des entités
    sont filtrées par `skip`.
    """
    need: dict[str, int] = {}
    for _, e in useful:
        need[e.name] = need.get(e.name, 0) + 1
        if fuel_count > 0 and is_burner(e.name):
            need[fuel] = need.get(fuel, 0) + fuel_count
    return need


def _ok(res) -> bool:
    """Le mod a-t-il confirmé l'action ? (`run_action` retourne le last_result)."""
    return isinstance(res, dict) and res.get("ok") is True


def _can_place(api, name: str, x: float, y: float, d: str) -> tuple[bool, str]:
    """(placable, raison). Isole la lecture défensive de la réponse du mod."""
    chk = api.can_place_check(name, x, y, d)
    if isinstance(chk, dict) and chk.get("can_place") is True:
        return True, ""
    if isinstance(chk, dict):
        # LE MOTIF D'ABORD. Le mod dit désormais POURQUOI il refuse — « aucun minerai sous
        # la foreuse », « occupé par … (l'avatar) », « tuile water ». Sans lui, `blocked`
        # ne portait qu'un « can_place=False » muet, et il fallait relancer une sonde par
        # hypothèse pour apprendre ce que le jeu savait déjà.
        return False, str(chk.get("motif") or chk.get("error") or "can_place=False")
    return False, "can_place_check illisible"


# De combien s'écarter de l'emprise d'un plan. Assez pour libérer les tuiles, assez peu
# pour rester dans la portée de construction d'un joueur connecté (~10 tuiles).
MARGE_DEGAGEMENT = 5.0


def _degager_le_personnage(api, ordered: list[tuple[int, object]], report,
                           timeout: float) -> None:
    """Écarte l'avatar de l'emprise du plan — il bloque ses propres poses.

    Mesuré : `can_place_entity` en mode `manual` rend `false` sur une tuile vide, du
    seul fait que le personnage se tient dessus ; le même appel en mode par défaut rend
    `true`. Comme on approche AU CENTRE du plan avant de bâtir, l'avatar se met
    systématiquement en travers, et les décalages de repli ne servent à rien puisqu'il
    reste au milieu. Le symptôme est un `can_place=False` sur du sable nu, sans aucune
    entité alentour — introuvable tant qu'on ne pense pas à se regarder soi-même.

    On ne s'éloigne que de `MARGE_DEGAGEMENT` : au-delà, un joueur connecté sortirait de
    sa portée de construction et toutes les poses échoueraient pour la raison inverse.
    """
    pos = perception.position(api)
    if pos is None or not ordered:
        return
    xs = [e.x for _, e in ordered]
    ys = [e.y for _, e in ordered]
    x1, x2, y1, y2 = min(xs) - 1.5, max(xs) + 1.5, min(ys) - 1.5, max(ys) + 1.5
    if not (x1 <= pos[0] <= x2 and y1 <= pos[1] <= y2):
        return
    # On sort par le bord le plus proche : le trajet est court et reste à portée.
    sorties = ((x1 - MARGE_DEGAGEMENT, pos[1]), (x2 + MARGE_DEGAGEMENT, pos[1]),
               (pos[0], y1 - MARGE_DEGAGEMENT), (pos[0], y2 + MARGE_DEGAGEMENT))
    cible = min(sorties, key=lambda p: (p[0] - pos[0]) ** 2 + (p[1] - pos[1]) ** 2)
    res = api.run_action(api.walk_to, cible[0], cible[1], timeout=max(timeout, 60.0))
    report.steps.append(f"degagement du personnage {pos} -> "
                        f"({cible[0]:.1f},{cible[1]:.1f}) ok={_ok(res)}")


def _rigid_offset(api, ordered: list[tuple[int, object]],
                  retry_offsets) -> tuple[Optional[tuple[float, float]], tuple]:
    """Cherche un décalage SOLIDAIRE valable pour toutes les entités du plan.

    Retry par entité = chaîne cassée : mesuré en live, décaler le drill de +1y et
    l'inserter de +1x place l'inserter hors de la drop tile du drill — les 3 entités
    sont posées, `ok=True`, et rien ne produit. Les positions d'un `MicroPlan` sont
    relatives entre elles (drop/pickup à la tuile près) : on translate le plan ENTIER
    ou pas du tout.

    Bonus de correction : toutes les vérifications ont lieu AVANT la première pose,
    donc `can_place_check` voit la même carte pour toutes les entités (il est aveugle
    aux entités du plan non encore posées — limite connue de la vérification).

    Retourne (offset retenu ou None, (idx, name, x, y, raison) du blocage à l'offset 0).
    """
    first_fail: tuple = ()
    for ox, oy in ((0.0, 0.0),) + tuple(retry_offsets):
        for idx, e in ordered:
            okp, why = _can_place(api, e.name, round(e.x + ox, 2), round(e.y + oy, 2),
                                  _dir_str(e.direction))
            if not okp:
                if not first_fail:
                    first_fail = (idx, e.name, e.x, e.y, why)
                break
        else:
            return (ox, oy), first_fail
    return None, first_fail


def execute_micro(api, plan, *, fuel: str = "coal", fuel_count: int = 5,
                  retry_offsets: tuple[tuple[float, float], ...] = RETRY_OFFSETS,
                  generate: bool = True, approach: bool = True,
                  dry_run: bool = False, verify: bool = True,
                  timeout: float = 20.0) -> ExecutionReport:
    """Pose en jeu les entités d'un `MicroPlan` et les alimente en combustible.

    Déroulé : pré-vol inventaire -> approche (génération de terrain + marche) -> choix
    d'un décalage solidaire (borné) -> pose ordonnée vérifiée -> alimentation des burners
    -> rapport.

    Paramètres
    ----------
    api : ModApi
        Accès RCON injecté (dépendance, pas global — SOLID).
    plan : MicroPlan | LayoutPlan
        Plan calculé. Seuls `entities`, `connections` et `feasibility` sont lus.
    fuel, fuel_count : str, int
        Combustible injecté dans chaque entité burner. `fuel_count=0` désactive
        l'alimentation (et la sort du pré-vol inventaire).
    retry_offsets : tuple
        Décalages essayés quand `can_place_check` refuse. Vide = fail-fast strict.
    generate, approach : bool
        Génération de chunks (S4d) et marche vers le chantier. À couper en test_mode
        headless quand le character est déjà sur place.
    dry_run : bool
        Vérifie tout (inventaire, positions, retry) sans rien poser ni alimenter.
    verify : bool
        Après chaque pose confirmée par le mod, relit l'inventaire pour s'assurer que
        l'item en est bien sorti (anti pose fantôme). Un aller-retour RCON par entité.
    timeout : float
        Timeout par action `run_action`.

    Retourne
    --------
    ExecutionReport
        `ok=True` si toutes les entités utiles sont posées. Sinon `missing` (inventaire)
        ou `blocked` (positions) indique à FactoryBuilder quoi arbitrer.
    """
    report = ExecutionReport()

    feasibility = getattr(plan, "feasibility", "ok")
    if feasibility != "ok":
        report.notes.append(f"plan non faisable: feasibility={feasibility}")
        return report

    useful = _useful_entities(plan)
    skipped = len(plan.entities) - len(useful)
    if skipped:
        report.notes.append(f"{skipped} entité(s) skip=True filtrée(s)")

    if not useful:
        report.ok = True
        report.notes.append("plan vide : rien à poser")
        return report

    # --- 1. Pré-vol inventaire (le mod ne le fait pas : create_entity puis inv.remove) ---
    need = _required_items(plan, useful, fuel, fuel_count)
    inv = perception.inventory(api)
    missing = {item: n - inv.get(item, 0) for item, n in need.items() if inv.get(item, 0) < n}
    if missing:
        report.missing = missing
        report.steps.append(f"pre-vol inventaire: manque {missing}")
        report.notes.append("aucune entité posée (inventaire insuffisant)")
        return report
    report.steps.append(f"pre-vol inventaire OK ({len(need)} item(s) requis)")

    # --- 2. Approche : générer les chunks puis marcher (règle S4d) ---
    x1, y1, x2, y2 = getattr(plan, "bbox", (0.0, 0.0, 0.0, 0.0))
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    if generate and not dry_run:
        # Sans chunks générés, le pathfinding ne planifie pas vers la cible (CONSTAT
        # S1d/S1g) : générer AVANT de marcher, jamais l'inverse.
        r = api.generate_terrain(cx, cy, 60.0)
        gen = r.get("generated") if isinstance(r, dict) else None
        report.steps.append(f"generate_terrain({cx:.1f},{cy:.1f},60) -> generated={gen}")
    if approach and not dry_run:
        res = api.run_action(api.walk_to, cx, cy, timeout=max(timeout, 60.0))
        report.steps.append(f"walk_to({cx:.1f},{cy:.1f}) -> ok={_ok(res)}")

    # --- 3. Décalage solidaire du plan entier, puis pose dans l'ordre du flux ---
    ordered = _topological_order(plan, useful)
    if not dry_run:
        _degager_le_personnage(api, ordered, report, timeout)
    offset, fail = _rigid_offset(api, ordered, retry_offsets)
    if offset is None:
        idx, name, ex, ey, why = fail or (-1, "?", 0.0, 0.0, "aucun candidat")
        report.blocked.append((idx, name, ex, ey, why))
        report.steps.append(f"BLOQUE {name}@({ex},{ey}) : {why} "
                            f"({1 + len(tuple(retry_offsets))} décalage(s) essayé(s))")
        return report
    ox, oy = offset
    if offset != (0.0, 0.0):
        report.steps.append(f"decalage solidaire du plan : {offset}")

    for idx, e in ordered:
        d = _dir_str(e.direction)
        px, py = round(e.x + ox, 2), round(e.y + oy, 2)

        if dry_run:
            report.placed.append(PlacedEntity(idx, e.name, px, py, e.direction,
                                              getattr(e, "role", ""), offset))
            report.steps.append(f"pose {e.name}@({px},{py}) dir={d} [dry_run]")
            continue

        # S'APPROCHER DE CHAQUE POSE QUI L'EXIGE. Une seule marche vers le centre du plan
        # ne suffit pas : le mod refuse toute pose au-delà de `build_distance`, et une
        # chaîne plus large que cette portée devient impossible à bâtir. En `test_mode`
        # rien ne le montre — aucune portée n'y est vérifiée.
        #
        # Mesuré en jeu (7e partie Hermes) : 650 s d'approvisionnement, `missing={}`,
        # `can_place` à True sur le gisement, et pourtant
        # « blocked=[(0,'burner-mining-drill',-55,-91,'walk closer first')] » — le joueur
        # était à treize tuiles. On ne marche que si c'est nécessaire : une entité déjà à
        # portée ne doit pas coûter un déplacement.
        #
        # INDÉPENDANT DE `approach`, et c'est le cœur du correctif. `batir_chaine` passe
        # `approach=False` pour une raison juste — l'approche initiale menait l'avatar au
        # milieu du chantier, où il refusait ensuite sa propre pose. Mais le drapeau
        # court-circuitait alors le seul chemin qui en avait besoin : 8e partie, correctif
        # en place, toujours « walk closer first » à treize tuiles. La portée n'est pas
        # une option de confort, c'est une condition physique ; `approach` ne gouverne
        # que l'approche INITIALE vers le centre du plan.
        #
        # ET L'ON S'ARRÊTE À CÔTÉ, jamais sur la tuile visée : `can_place` en mode manuel
        # exclut l'emplacement du personnage. C'est l'autre moitié du dilemme, et la
        # raison pour laquelle `batir_chaine` avait renoncé à s'approcher.
        if not dry_run:
            try:
                cx0, cy0 = deplacement.position(api)
                loin = math.hypot(px - cx0, py - cy0)
                if loin > PORTEE_POSE:
                    part = max(0.0, (loin - RECUL_POSE) / loin)
                    ax, ay = deplacement.marcher_vers(api, cx0 + (px - cx0) * part,
                                                      cy0 + (py - cy0) * part)
                    report.steps.append(f"approche {e.name}@({px},{py}) -> "
                                        f"({ax:.0f},{ay:.0f})")
            except Exception as exc:
                report.steps.append(f"approche impossible : {type(exc).__name__}")

        res = api.run_action(api.place_entity_at, e.name, px, py, d, _place_opts(e),
                             timeout=timeout)
        reason = ""
        if not _ok(res):
            reason = str(res.get("detail", "place_entity_at ko")) \
                if isinstance(res, dict) else "place_entity_at illisible"
        elif verify:
            # Le mod a dit oui : vérifier que l'item a QUITTÉ l'inventaire. Sans ce
            # contrôle le rapport peut annoncer une pose fantôme (constaté en live E1 :
            # 3 entités « posées », une seule sur la carte, inventaire de drills intact).
            after = perception.inventory(api)
            if after.get(e.name, 0) >= inv.get(e.name, 0):
                reason = "pose non confirmée (inventaire inchangé)"
            inv = after

        if reason:
            report.blocked.append((idx, e.name, px, py, reason))
            report.steps.append(f"BLOQUE {e.name}@({px},{py}) : {reason}")
            # Arrêt : la suite du plan dépend de cette entité (chaîne de flux). Le
            # replan lourd appartient à FactoryBuilder (S4c).
            return report

        report.placed.append(PlacedEntity(idx, e.name, px, py, e.direction,
                                          getattr(e, "role", ""), offset))
        report.steps.append(f"pose {e.name}@({px},{py}) dir={d}")

    # --- 4. Alimentation des burners (drill, inserter burner, furnace) ---
    if fuel_count > 0 and not dry_run:
        for p in report.placed:
            if not is_burner(p.name):
                continue
            res = api.run_action(api.move_items_at, fuel, p.name, p.x, p.y,
                                 fuel_count, True, timeout=timeout)
            if _ok(res):
                report.fueled[p.name] = report.fueled.get(p.name, 0) + fuel_count
                report.steps.append(f"fuel {fuel} x{fuel_count} -> {p.name}@({p.x},{p.y})")
            else:
                report.notes.append(f"alimentation {p.name}@({p.x},{p.y}) echouee")

    report.ok = len(report.placed) == len(useful) and not report.blocked
    if dry_run:
        report.notes.append("dry_run : aucune entité posée ni alimentée")
    return report