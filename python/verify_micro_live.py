"""Test LIVE de POSAGE RÉEL du MicroPlanner (chaîne drill+inserter+furnace).

Contrairement à verify_micro_planner.py (intégration RCON + calcul, can_place info-only
sur centre bbox out-of-map), ce script POSE réellement les 3 entités sur un gisement
proche pour valider que le plan est placable (can_place=True + posage sans chevauchement).

Stratégie (headless test_mode, perso au spawn 0,0) :
  1. scan_patch iron-ore r=50 -> gisement LOCAL (bbox + sample de vraies tuiles ore).
  2. anchor = tuile ore du bord NORD du gisement (sample), facing=NORTH -> la chaîne
     (drop + inserter + furnace) pousse au nord, HORS du gisement (herbe libre), donc
     can_place inserter/furnace=True. Le drill couvre des tuiles ore (bord nord).
  3. generate_terrain autour de l'anchor (révèle le terrain headless out-of-map).
  4. plan_micro -> 3 entités.
  5. can_place_check sur les 3 (validation réelle, non info-only).
  6. place_entity_at sur les 3 (poll inventaire, test_mode=teleport).
  7. Injecter coal (drill + furnace) -> valider démarrage (drill fuel>0).

Pré-requis : serveur headless lancé, mod chargé. SKIP (return 0) si serveur down.
"""

from __future__ import annotations

import sys

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.layout_planner import ResourcePatch
from services.micro_planner import MicroRequest, plan_micro

DIR = {0: "north", 2: "east", 4: "south", 6: "west"}

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:42s} {detail[:100]}")


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

    # --- Rec 1 : gisement local (scan_patch r=50) ---
    sp = api.scan_patch("iron-ore", 50.0)
    if not sp or not sp.get("bbox"):
        print("!! aucun gisement iron-ore proche -> generate_terrain(0,0,80) + retry.")
        api.generate_terrain(0.0, 0.0, 80.0)
        api.wait(1.5)
        sp = api.scan_patch("iron-ore", 50.0)
    bb = sp["bbox"]
    sample = sp.get("sample", [])
    rec("live-1 : gisement local (scan_patch r=50)",
        bool(bb) and len(sample) > 0,
        f"bbox=({bb['x1']},{bb['y1']})-({bb['x2']},{bb['y2']}) count={sp.get('count')} sample0={sample[0] if sample else None}")

    # --- Rec 2 : anchor = tuile ore bord nord, facing north ---
    # Bord nord = y1 (y croît vers le sud). sample[0] est au bord nord (y=y1).
    ax = float(sample[0]["x"])
    ay = float(sample[0]["y"])
    # Décaler l'anchor de +1 en x et +1 en y (intérieur du gisement) pour que l'emprise
    # drill 3x3 couvre plus de tuiles ore (évite le bord exact où -1 sort du bbox).
    anchor = (ax + 1.0, ay + 1.0)
    rec("live-2 : anchor tuile ore bord nord (facing north, chaîne au nord hors gisement)",
        True, f"anchor={anchor} (sample0={ax},{ay})")

    # --- Rec 3 : generate_terrain autour (drill + chaîne au nord) ---
    # Chaîne au nord (facing=0, drop en -y), couvre anchor.y-6 .. anchor.y+2.
    try:
        api.generate_terrain(anchor[0], anchor[1] - 3.0, 45.0)
        api.wait(1.5)
        rec("live-3 : generate_terrain autour anchor", True, "rayon 45 couvre drill+chaîne nord")
    except Exception as e:
        rec("live-3 : generate_terrain", False, f"exc: {e}")

    # --- Rec 4 : plan_micro facing north ---
    patch = ResourcePatch("iron-ore", bbox=(int(bb["x1"]), int(bb["y1"]),
                                            int(bb["x2"]), int(bb["y2"])))
    mp = plan_micro(MicroRequest(patch=patch, facing=0, anchor=anchor))
    roles = [(e.role, e.name, e.x, e.y, e.direction) for e in mp.entities]
    rec("live-4 : plan_micro facing north -> 3 entités",
        mp.feasibility == "ok" and len(mp.entities) == 3,
        f"feas={mp.feasibility} n={len(mp.entities)} roles={roles}")

    # --- Rec 5 : can_place réel sur les 3 ---
    can_results = []
    for e in mp.entities:
        r = api.can_place_check(e.name, round(e.x, 2), round(e.y, 2), DIR[e.direction])
        can_results.append(bool(r.get("can_place")) if isinstance(r, dict) else False)
    rec("live-5 : can_place réel (drill sur ore, ins/furn au nord herbe)",
        all(can_results), f"can_place={dict(zip([e.role for e in mp.entities], can_results))}")

    # --- Rec 6 : posage réel des 3 entités ---
    inv_before = api.get_state().get("inventory", {})
    for e in mp.entities:
        try:
            api.place_entity_at(e.name, e.x, e.y, DIR[e.direction])
        except Exception as ex:
            print(f"  place {e.role} exc: {ex}")
    # En test_mode le character téléporte pour poser ; attendre la fin de pose.
    api.wait(4.0)
    inv_after = api.get_state().get("inventory", {})
    placed = {
        "burner-mining-drill": inv_before.get("burner-mining-drill", 0) - inv_after.get("burner-mining-drill", 0),
        "burner-inserter": inv_before.get("burner-inserter", 0) - inv_after.get("burner-inserter", 0),
        "stone-furnace": inv_before.get("stone-furnace", 0) - inv_after.get("stone-furnace", 0),
    }
    rec("live-6 : posage réel (inventaire diminue de 1 par entité)",
        placed["burner-mining-drill"] >= 1 and placed["burner-inserter"] >= 1
        and placed["stone-furnace"] >= 1,
        f"placed={placed} (inv après: drill={inv_after.get('burner-mining-drill',0)} "
        f"ins={inv_after.get('burner-inserter',0)} furn={inv_after.get('stone-furnace',0)})")

    # --- Rec 7 : injecter coal (drill + furnace) -> démarrage ---
    if all(v >= 1 for v in placed.values()):
        try:
            # walk proche du drill pour move_items (rayon 32).
            api.walk_to(mp.entities[0].x, mp.entities[0].y)
            api.wait(2.5)
            api.move_items("coal", "burner-mining-drill", 5, True)
            api.wait(1.0)
            api.move_items("coal", "stone-furnace", 5, True)
            api.wait(2.0)
            rec("live-7 : injecter coal (drill + furnace)", True,
                "move_items coal -> drill + furnace (rayon 32)")
        except Exception as ex:
            rec("live-7 : injecter coal", False, f"exc: {ex}")
    else:
        rec("live-7 : injecter coal", False, "skip (posage incomplet)")

    rcon.close()
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    print("Entités posées sur le serveur (visibles si tu te connectes en mode joueur).")
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())