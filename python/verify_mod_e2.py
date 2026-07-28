"""Test LIVE des 4 primitives E2 : agir sur une entité DÉJÀ posée.

Jusqu'ici le mod ne savait que **construire**. Il ne savait ni retirer, ni tourner, ni
régler une recette, et `place_entity_at` ignorait les champs que le LayoutPlanner
calcule pourtant depuis S1/S3 (`ug_type`, `priority`, `modules`). Deux conséquences :

  - **aucune correction d'erreur** : `mine_entity` cible par NOM dans un rayon, donc
    impossible de viser l'entité qu'on vient de mal poser. Toute erreur était
    définitive — or c'est le mode d'échec n°1 des agents LLM mesuré par le benchmark
    Factorio Learning Environment (arXiv 2503.09617) ;
  - **aucune automatisation au-delà des fours** : une machine sans recette ne produit
    rien, et un `LayoutPlan` avec bus (underground/splitter) ou beacons n'était pas
    posable.

Ce script valide les 4 primitives contre l'état RÉEL du jeu (relecture via `scan_area`
et `scan_factory`), jamais contre le `ok=True` renvoyé par le mod.

Chaque contrôle cible une POSITION précise : les runs précédents laissent des entités
sur la carte, valider par nom seul validerait les leurs.

Pré-requis : serveur headless lancé, mod E2 chargé. SKIP (return 0) si injoignable.
Tourne en `test_mode` (aucun joueur requis) ; la contrainte de portée est, elle,
couverte par `verify_executor_prod.py`.
"""

from __future__ import annotations

import math
import sys

from core.mod_api import ModApi
from core.rcon import get_rcon

RESULTS: list[tuple[str, bool, str]] = []

# Variantes de grille essayées avant de poser. Une emprise PAIRE se pose sur une
# position entière, une IMPAIRE sur un centre de tuile (x.5) — et `create_entity`
# snappe de toute façon. Le test mesure la variante acceptée au lieu de la supposer.
GRID_VARIANTS: tuple[tuple[float, float], ...] = ((0.0, 0.0), (0.5, 0.5), (0.5, 0.0), (0.0, 0.5))


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:105]}")


def _can(api: ModApi, name: str, x: float, y: float, d: str = "north") -> bool:
    c = api.can_place_check(name, x, y, d)
    return isinstance(c, dict) and c.get("can_place") is True


def _place(api: ModApi, name: str, x: float, y: float, direction: str = "north",
           opts: dict | None = None) -> tuple[float, float] | None:
    """Pose `name` près de (x, y) sur la première variante de grille acceptée.

    Retourne la position réelle, ou None si aucune ne passe.
    """
    for dx, dy in GRID_VARIANTS:
        px, py = round(x + dx, 2), round(y + dy, 2)
        if not _can(api, name, px, py, direction):
            continue
        res = api.run_action(api.place_entity_at, name, px, py, direction, opts, timeout=20.0)
        if isinstance(res, dict) and res.get("ok"):
            return px, py
    return None


def _at(api: ModApi, x: float, y: float, name: str | None = None,
        tol: float = 1.6) -> dict | None:
    """L'entité réellement présente autour de (x, y) — relue via scan_area.

    Rayon large : `scan_area` est centré sur le PERSONNAGE (pas sur (x, y)), et le
    banc d'essai s'étale sur ±8 tuiles autour du centre de zone. Un rayon trop court
    faisait échouer des contrôles alors que le mod avait bien travaillé.
    """
    sa = api.scan_area(40.0)
    for e in sa.get("entities", []) if isinstance(sa, dict) else []:
        if name and e.get("name") != name:
            continue
        if e.get("type") in ("character", "item-entity"):
            continue
        if abs(float(e.get("x", 1e9)) - x) <= tol and abs(float(e.get("y", 1e9)) - y) <= tol:
            return e
    return None


def _clean(rcon, cx: float, cy: float, r: float = 20.0) -> int:
    """Vide une zone (entités des runs précédents + arbres qui bloquent la pose)."""
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{cx - r},{cy - r}}},"
        f"{{{cx + r},{cy + r}}}}}}}) do "
        f"if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return -1


def _find_workspace(api: ModApi, rcon) -> tuple[float, float] | None:
    """Cherche une zone SÈCHE et dégagée pour y bâtir le banc d'essai.

    Le premier jet codait la zone en dur : elle est tombée dans un lac (`water` /
    `deepwater`), et 7 contrôles sur 10 ont échoué sans que le mod y soit pour rien.
    Le terrain n'est pas une constante — on le mesure. Critère : une machine 3×3 doit
    passer sur les 5 emplacements du banc.
    """
    for radius in (0, 30, 60, 90, 130, 180):
        for angle in (range(0, 360, 45) if radius else (0,)):
            cx = float(round(radius * math.cos(math.radians(angle))))
            cy = float(round(radius * math.sin(math.radians(angle))))
            api.generate_terrain(cx, cy, 30.0)
            api.run_action(api.wait, 10, timeout=30.0)
            _clean(rcon, cx, cy)
            spots = ((cx, cy), (cx + 8, cy), (cx - 8, cy), (cx, cy + 8), (cx, cy - 8))
            if all(_can(api, "assembling-machine-1", sx + 0.5, sy + 0.5) for sx, sy in spots):
                return cx, cy
    return None


def main() -> int:
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    api.setup()
    api.reset_character()

    zone = _find_workspace(api, rcon)
    if zone is None:
        print("[SKIP] aucune zone sèche et dégagée trouvée autour du spawn.")
        rcon.close()
        return 0
    BX, BY = zone
    # Le personnage bloque `can_place_entity` en mode manual : on le met hors des
    # emplacements (qui sont à ±8 du centre) mais assez près pour que scan_area,
    # centré sur LUI, couvre tout le banc d'essai.
    api.run_action(api.teleport_to, BX, BY + 14.0, timeout=30.0)
    print(f"       . zone de travail : ({BX},{BY}) — terrain "
          f"{api.get_tile(BX, BY).get('name')}")

    inv = api.get_state().get("inventory", {})
    besoins = ("assembling-machine-1", "underground-belt", "splitter", "beacon",
               "burner-inserter", "speed-module-3")
    manque = [n for n in besoins if inv.get(n, 0) < 1]
    rec("e2-0 : kit suffisant pour tester les 4 primitives", not manque,
        f"manque={manque or 'rien'} (kit rearmé par reset_character)")
    if manque:
        print("       ! le kit du mod a été étendu par E2 : une carte déjà jouée garde "
              "l'ancien (storage.fl.kit_given). Repartir d'une carte neuve.")
        rcon.close()
        return _verdict()

    # --- 1/2. set_recipe_at : le verrou de l'automatisation ---
    pos = _place(api, "assembling-machine-1", BX, BY)
    if pos is None:
        rec("e2-1 : set_recipe_at -> la machine porte la recette", False,
            "assembleur non posable sur la zone de travail")
        rcon.close()
        return _verdict()
    ax, ay = pos
    res = api.run_action(api.set_recipe_at, ax, ay, "iron-gear-wheel", None, timeout=20.0)
    apres = _at(api, ax, ay, "assembling-machine-1")
    rec("e2-1 : set_recipe_at -> la machine porte la recette",
        bool(apres) and apres.get("recipe") == "iron-gear-wheel",
        f"@({ax},{ay}) recipe={apres.get('recipe') if apres else None} "
        f"detail={res.get('detail') if isinstance(res, dict) else res}")

    # Effacer la recette (recipe=None) doit être accepté et relu comme 'none'.
    api.run_action(api.set_recipe_at, ax, ay, None, None, timeout=20.0)
    vide = _at(api, ax, ay, "assembling-machine-1")
    rec("e2-2 : set_recipe_at(None) efface la recette",
        bool(vide) and vide.get("recipe") in ("none", None),
        f"recipe={vide.get('recipe') if vide else None}")

    # --- 3/4. remove_entity_at : rendre une erreur de pose réparable ---
    avant_inv = api.get_state().get("inventory", {}).get("assembling-machine-1", 0)
    res = api.run_action(api.remove_entity_at, ax, ay, None, timeout=20.0)
    apres_inv = api.get_state().get("inventory", {}).get("assembling-machine-1", 0)
    encore = _at(api, ax, ay, "assembling-machine-1")
    rec("e2-3 : remove_entity_at retire l'entité ET rend l'item",
        encore is None and apres_inv > avant_inv,
        f"encore là={encore is not None} inventaire {avant_inv} -> {apres_inv} "
        f"detail={res.get('detail') if isinstance(res, dict) else res}")

    # Cibler une position vide doit ÉCHOUER proprement, pas détruire au hasard.
    res = api.run_action(api.remove_entity_at, BX + 15, BY + 15, None, timeout=20.0)
    ok_vide = isinstance(res, dict) and res.get("ok") is False
    rec("e2-4 : remove_entity_at sur une position vide échoue proprement", ok_vide,
        f"res={res.get('detail') if isinstance(res, dict) else res}")

    # --- 5. rotate_entity_at : direction ABSOLUE ---
    pos = _place(api, "burner-inserter", BX + 4.0, BY + 4.0)
    ins = None
    if pos:
        api.run_action(api.rotate_entity_at, pos[0], pos[1], "east", None, timeout=20.0)
        ins = _at(api, pos[0], pos[1], "burner-inserter")
    rec("e2-5 : rotate_entity_at oriente une entité déjà posée",
        bool(ins) and ins.get("direction") == "east",
        f"posé={pos} direction={ins.get('direction') if ins else None} (attendu east)")

    # --- 6/7/8 : les options de pose (ce que le LayoutPlanner calculait sans pouvoir poser) ---
    pos = _place(api, "underground-belt", BX - 8.0, BY + 8.0, "north", {"ug_type": "output"})
    ug = _at(api, pos[0], pos[1], "underground-belt") if pos else None
    rec("e2-6 : place_entity_at(ug_type) -> sens de l'underground appliqué",
        bool(ug) and ug.get("ugType") == "output",
        f"posé={pos} ugType={ug.get('ugType') if ug else None} "
        f"(non modifiable après pose : belt_to_ground_type est en lecture seule)")

    pos = _place(api, "splitter", BX - 8.0, BY - 8.0, "north", {"priority_out": "left"})
    sp = _at(api, pos[0], pos[1], "splitter") if pos else None
    rec("e2-7 : place_entity_at(priority_out) -> priorité splitter appliquée",
        bool(sp) and sp.get("prioOut") == "left",
        f"posé={pos} prioOut={sp.get('prioOut') if sp else None} "
        f"prioIn={sp.get('prioIn') if sp else None}")

    pos = _place(api, "beacon", BX + 8.0, BY - 8.0, "north", {"modules": {"speed-module-3": 2}})
    bc = _at(api, pos[0], pos[1], "beacon") if pos else None
    mods = bc.get("modules") if bc else None
    total = sum(m.get("count", 0) for m in mods) if isinstance(mods, list) else 0
    rec("e2-8 : place_entity_at(modules) -> modules insérés dans le beacon",
        total == 2, f"posé={pos} modules={mods} total={total} (attendu 2)")

    # --- 9 : recette DÈS la pose (une machine ne doit pas exister sans recette) ---
    pos = _place(api, "assembling-machine-1", BX + 8.0, BY + 8.0, "north",
                 {"recipe": "iron-gear-wheel"})
    am = _at(api, pos[0], pos[1], "assembling-machine-1") if pos else None
    rec("e2-9 : place_entity_at(recipe) -> machine posée AVEC sa recette",
        bool(am) and am.get("recipe") == "iron-gear-wheel",
        f"posé={pos} recipe={am.get('recipe') if am else None}")

    rcon.close()
    return _verdict()


def _verdict() -> int:
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