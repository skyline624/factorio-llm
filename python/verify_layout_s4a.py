"""Validation LIVE S4a : mod Lua scan_obstacles / scan_tiles_bbox / get_tile.

Pre-requis : serveur Factorio 2.0 headless lance (scripts/start_factorio_dedicated.bat)
APRES modification du mod (tools.lua + control.lua S4a). Le mod doit etre recharge ->
relance requise (game.reload_mods() ne vide PAS le cache require, pattern S2b).
RCON 127.0.0.1:27015 (pw "factoriollm").

Valide que Python dispose maintenant de la visibilite terrain manquante :
  - scan_obstacles : obstacles organiques (rochers/arbres/cliffs) autour de l'avatar,
    bbox floored (x,y,w,h), non destructif.
  - scan_tiles_bbox : toutes les tuiles dans une bbox arbitraire (water/out-of-map precis).
  - get_tile : nom de tuile ponctuel (frontiere headless out-of-map).

8 recs :
  1. set_test_mode + setup + mod recharge (scan_obstacles present, pas d'erreur).
  2. scan_obstacles structure valide (obstacles[] + count + bbox si count>0).
  3. scan_obstacles count >= 1 (starting_area a des rochers/arbres).
  4. scan_obstacles bbox coherente (x1<=x2, y1<=y2) + w/h entiers.
  5. get_tile(0,0) name valide (pas d'erreur).
  6. scan_tiles_bbox(0,0,10,10) count == 100 (10x10 tuiles).
  7. re-scan identique (non destructif, idempotent).
  8. back-compat : can_place_check toujours present (mod recharge OK).

Lancement (apres relance serveur) :
    cd python
    python verify_layout_s4a.py
"""

from __future__ import annotations
import sys

sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.rcon import get_rcon
from core.mod_api import ModApi

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:80]}")


def main() -> int:
    rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
    api = ModApi(rcon)
    try:
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"!! MOD NON RECHARGE (can_place_check absent : {e})")
        print("   -> relance scripts/start_factorio_dedicated.bat puis re-execute.")
        rcon.close()
        return 1

    api.set_test_mode(True)
    api.setup()

    # --- Rec 1 : mod recharge (scan_obstacles present, pas d'erreur) ---
    r = api.scan_obstacles()
    ok1 = isinstance(r, dict) and "error" not in r and "obstacles" in r
    rec("S4a-1 : mod recharge (scan_obstacles present, pas d'erreur)",
        ok1, f"keys={list(r.keys()) if isinstance(r, dict) else r}")

    # --- Rec 2 : scan_obstacles structure valide ---
    ok2 = (isinstance(r, dict) and isinstance(r.get("obstacles"), list)
           and isinstance(r.get("count"), int)
           and (r.get("count") == 0 or isinstance(r.get("bbox"), dict)))
    rec("S4a-2 : scan_obstacles structure valide (obstacles[] + count + bbox)",
        ok2, f"count={r.get('count')} bbox={r.get('bbox') is not None}")

    # --- Rec 3 : scan_obstacles count >= 1 (starting_area a des rochers/arbres) ---
    count = r.get("count", 0) if isinstance(r, dict) else 0
    rec("S4a-3 : scan_obstacles count >= 1 (rochers/arbres starting_area)",
        count >= 1, f"count={count}")

    # --- Rec 4 : scan_obstacles bbox coherente + w/h entiers ---
    obstacles = r.get("obstacles", []) if isinstance(r, dict) else []
    bbox_ok = True
    if obstacles:
        for o in obstacles:
            if not (isinstance(o.get("w"), int) and isinstance(o.get("h"), int)
                    and isinstance(o.get("x"), int) and isinstance(o.get("y"), int)):
                bbox_ok = False
                break
        bb = r.get("bbox")
        if bb and not (bb.get("x1") <= bb.get("x2") and bb.get("y1") <= bb.get("y2")):
            bbox_ok = False
    rec("S4a-4 : scan_obstacles bbox coherente (x1<=x2) + w/h entiers",
        bbox_ok and len(obstacles) > 0, f"n={len(obstacles)} wh_int={bbox_ok}")

    # --- Rec 5 : get_tile(0,0) name valide ---
    gt = api.get_tile(0, 0)
    ok5 = isinstance(gt, dict) and "error" not in gt and isinstance(gt.get("name"), str)
    rec("S4a-5 : get_tile(0,0) name valide",
        ok5, f"name={gt.get('name') if isinstance(gt, dict) else gt}")

    # --- Rec 6 : scan_tiles_bbox(0,0,10,10) count == 100 (10x10) ---
    st = api.scan_tiles_bbox(0, 0, 10, 10)
    ok6 = (isinstance(st, dict) and "error" not in st
           and st.get("count") == len(st.get("tiles", []))
           and st.get("count") == 100)
    rec("S4a-6 : scan_tiles_bbox(0,0,10,10) count == 100 (10x10 tuiles)",
        ok6, f"count={st.get('count') if isinstance(st, dict) else st}")

    # --- Rec 7 : re-scan identique (non destructif, idempotent) ---
    r2 = api.scan_obstacles()
    ok7 = r == r2
    rec("S4a-7 : re-scan identique (non destructif, idempotent)",
        ok7, f"r==r2={ok7} count1={r.get('count')} count2={r2.get('count')}")

    # --- Rec 8 : back-compat can_place_check present (mod recharge OK) ---
    cp = api.can_place_check("transport-belt", 0.5, 0.5, "north")
    ok8 = isinstance(cp, dict) and "can_place" in cp
    rec("S4a-8 : back-compat can_place_check present (mod recharge OK)",
        ok8, f"can_place={cp.get('can_place') if isinstance(cp, dict) else cp}")

    # --- Recap ---
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} recs OK")
    print("=" * 72)
    rcon.close()
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())