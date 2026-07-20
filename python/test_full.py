"""Test complet de TOUTES les actions/observations en mode PRODUCTION (physique reelle).

Scénario intégré sur map vierge :
  setup -> observations (7) -> walk vers fer -> mine -> walk (position) ->
  place furnace -> move_items (joueur->four, sens 1) -> smelting (wait) ->
  move_items (four->joueur, sens 2) -> move_items_at (position) ->
  craft iron-gear-wheel -> research automation -> cancel.

Chaque étape est loggée ; récap ok/échec à la fin.

Prerequis : serveur dédié lancé + joueur connecté (il est l'IA). Map fraîche recommandée.
"""
from __future__ import annotations

import json
import time

from core.rcon import get_rcon
from core.mod_api import ModApi

# Récapitulatif des résultats : (étape, ok, détail)
RESULTS: list[tuple[str, bool, str]] = []


def _inv(api: ModApi) -> dict:
    st = api.get_state()
    return dict(st.get("inventory", {}))


def _pos(api: ModApi) -> tuple[float, float]:
    st = api.get_state()
    p = st["character"]["position"]
    return (p["x"], p["y"])


def obs(name: str, fn, *args, **kwargs) -> object:
    """Observation synchrone (retour direct)."""
    try:
        r = fn(*args, **kwargs)
        ok = isinstance(r, dict) and "error" not in r
        RESULTS.append((name, ok, json.dumps(r, ensure_ascii=False)[:160]))
        return r
    except Exception as e:
        RESULTS.append((name, False, f"EXC: {e!r}"))
        return None


def act(name: str, fn, *args, timeout: float = 45.0, **kwargs) -> dict:
    """Action asynchrone race-free : run_action (capture seq0 -> enfile -> attend seq avance)."""
    try:
        lr = api.run_action(fn, *args, timeout=timeout, **kwargs)
        ok = isinstance(lr, dict) and lr.get("ok") is True
        RESULTS.append((name, ok, json.dumps(lr, ensure_ascii=False)[:160]))
        return lr if isinstance(lr, dict) else {}
    except Exception as e:
        RESULTS.append((name, False, f"EXC: {e!r}"))
        return {}


def recap() -> None:
    print("\n" + "=" * 70)
    print("RECAPITULATIF DU TEST COMPLET (production, physique reelle)")
    print("=" * 70)
    nok = 0
    for name, ok, detail in RESULTS:
        flag = "OK  " if ok else "FAIL"
        if ok:
            nok += 1
        print(f"[{flag}] {name:38s} {detail}")
    print("-" * 70)
    print(f"{nok}/{len(RESULTS)} etapes reussies.")
    print("=" * 70)


rcon = get_rcon()
api = ModApi(rcon)

print("[test] === setup + mode production ===")
api.set_test_mode(False)
ack = api.setup()
RESULTS.append(("setup", isinstance(ack, dict) and ack.get("ok"), json.dumps(ack, ensure_ascii=False)))
print("[test] setup =", ack)
state = api.get_state()
print("[test] position initiale =", _pos(api), "test_mode =", state.get("test_mode"))

# ---- 1. Observations (fl_tools) ----
print("\n[test] === observations (fl_tools) ===")
obs("get_state", api.get_state)
obs("get_tick", api.get_tick)
obs("scan_area(30)", api.scan_area, 30)
obs("scan_factory", api.scan_factory)
fn = obs("find_nearest(iron-ore)", api.find_nearest, "iron-ore")
obs("describe(burner-mining-drill)", api.describe, "burner-mining-drill")
obs("get_recipe(iron-gear-wheel)", api.get_recipe, "iron-gear-wheel")
obs("production_stats", api.production_stats)

# ---- 2. walk_to_entity (marche reelle vers le fer) ----
print("\n[test] === walk_to_entity(iron-ore, 300) : marche reelle ===")
inv_before = _inv(api)
act("walk_to_entity(iron-ore,300)", api.walk_to_entity, "iron-ore", 300, timeout=60.0)
print("[test] position apres walk =", _pos(api))

# ---- 3. mine_entity (minage anime, completion via on_player_mined_entity) ----
print("\n[test] === mine_entity(iron-ore, 10) : minage anime ===")
act("mine_entity(iron-ore,10)", api.mine_entity, "iron-ore", 10, timeout=60.0)
inv_after = _inv(api)
d_iron = inv_after.get("iron-ore", 0) - inv_before.get("iron-ore", 0)
print(f"[test] iron-ore : {inv_before.get('iron-ore',0)} -> {inv_after.get('iron-ore',0)} (delta={d_iron})")
RESULTS.append(("verif mine +10 iron-ore", d_iron == 10, f"delta={d_iron}"))

# ---- 4. walk_to (position exacte) : 3 tiles a l'est pour degager le minerai ----
print("\n[test] === walk_to(pos+3 est) : position exacte ===")
px, py = _pos(api)
act("walk_to(pos+3E)", api.walk_to, px + 3, py, timeout=30.0)

# ---- 5. place_entity_at (garde reach + can_place + create_entity) ----
print("\n[test] === place_entity_at(stone-furnace) ===")
px, py = _pos(api)
furnace_pos = None
for (ox, oy) in [(2, 0), (0, 2), (-2, 0), (0, -2), (3, 1), (-1, 3)]:
    lr = act(f"place_entity_at({px+ox:.0f},{py+oy:.0f})", api.place_entity_at,
             "stone-furnace", px + ox, py + oy, "north", timeout=10.0)
    if isinstance(lr, dict) and lr.get("ok"):
        furnace_pos = (px + ox, py + oy)
        print(f"[test] furnace posee a {furnace_pos}")
        break
RESULTS.append(("place furnace reussie", furnace_pos is not None, str(furnace_pos)))

if furnace_pos is None:
    print("[test] ECHEC place furnace -> skip move_items/craft dependants")
else:
    fx, fy = furnace_pos

    # ---- 6. move_items joueur -> four (carburant) ----
    print("\n[test] === move_items(coal -> furnace, 5) [joueur->entite] ===")
    act("move_items(coal->furnace,5,true)", api.move_items, "coal", "stone-furnace", 5, True, timeout=15.0)

    # ---- 7. move_items joueur -> four (input smelting) ----
    print("\n[test] === move_items(iron-ore -> furnace, 5) [input] ===")
    act("move_items(iron-ore->furnace,5,true)", api.move_items, "iron-ore", "stone-furnace", 5, True, timeout=15.0)

    # ---- 8. wait (smelting : ~10s pour qq iron-plate) ----
    print("\n[test] === wait(600) [10s smelting] ===")
    act("wait(600)", api.wait, 600, timeout=20.0)

    # ---- 9. move_items four -> joueur (recuperer iron-plate produit) ----
    print("\n[test] === move_items(iron-plate <- furnace) [entite->joueur] ===")
    act("move_items(iron-plate->player,false)", api.move_items, "iron-plate", "stone-furnace", 10, False, timeout=15.0)

    # ---- 10. move_items_at (position exacte) ----
    print(f"\n[test] === move_items_at(coal -> furnace @ {fx},{fy}, 3) ===")
    act("move_items_at(coal@pos,3,true)", api.move_items_at, "coal", "stone-furnace", fx, fy, 3, True, timeout=15.0)

# ---- 11. craft_item (begin_crafting, completion via on_player_crafted_item) ----
print("\n[test] === craft_item(iron-gear-wheel, 5) ===")
inv_before = _inv(api)
act("craft_item(iron-gear-wheel,5)", api.craft_item, "iron-gear-wheel", 5, timeout=40.0)
inv_after = _inv(api)
d_gear = inv_after.get("iron-gear-wheel", 0) - inv_before.get("iron-gear-wheel", 0)
d_plate = inv_after.get("iron-plate", 0) - inv_before.get("iron-plate", 0)
print(f"[test] iron-gear-wheel delta={d_gear}, iron-plate delta={d_plate}")
RESULTS.append(("verif craft +5 gear / -10 plate", d_gear == 5 and d_plate == -10,
                f"gear={d_gear} plate={d_plate}"))

# ---- 12. research_technology : valider le DEMARRAGE (sans labs, current_research
# reste nil en 2.0 apres add_research -> on valide que la tache demarre, ie add_research appele) ----
print("\n[test] === research_technology(automation) [demarrage] ===")
ack = api.research_technology("automation")
RESULTS.append(("research_technology ack", isinstance(ack, dict) and ack.get("ok"),
                json.dumps(ack, ensure_ascii=False)))
started = False
t0 = time.time()
while time.time() - t0 < 3:
    s = api.status()
    if s.get("state") == "busy" and s.get("task", {}).get("type") == "researching":
        started = True
        break
    time.sleep(0.1)
print(f"[test] research demarree (task=researching) = {started}")
RESULTS.append(("research demarree (task=researching)", started,
                "state_researching a tourne / add_research appele (completion hors-scope sans labs)"))

# ---- 13. cancel (annule la research en cours) ----
print("\n[test] === cancel ===")
ack = api.cancel()
RESULTS.append(("cancel", isinstance(ack, dict) and ack.get("ok"), json.dumps(ack, ensure_ascii=False)))
print("[test] cancel =", ack)

rcon.close()
recap()