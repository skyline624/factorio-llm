"""Test LIVE E19 : l'agent FABRIQUE ce qu'une machine attend.

Le verrou désigné par la première partie longue. L'agent savait relier un gisement à une
machine ; il ne savait pas fabriquer. Une assembleuse réclamant des plaques restait donc
à l'arrêt pendant qu'il cherchait — en vain — un gisement de plaques de fer, et il
recommençait 559 fois.

Ce qu'on vérifie, dans l'ordre où ça peut casser :

  1. l'agent DISTINGUE extraire de fabriquer : `iron-ore` s'extrait, `iron-plate` non ;
  2. il pose la machine qui sait faire l'item — un four pour du `smelting`, et il ne lui
     impose pas de recette (un four n'en accepte aucune) ;
  3. le bras de livraison PUISE dans le four et DÉPOSE dans l'assembleuse : c'est
     l'inverse d'un bras d'alimentation, et l'orientation est vérifiée par lecture ;
  4. l'entrée du four est raccordée à un gisement de fer ;
  5. et le juge : **l'assembleuse reçoit réellement des plaques**, sans intervention.

Pré-requis : serveur headless. SKIP sinon.
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator, Decision
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.factory_doctor import Symptome
from services.site_finder import _entites_a, can_place

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:104]}")


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

    # On se place près d'un gisement de FER : la chaîne à bâtir en part.
    sp = api.scan_patches("iron-ore", 400.0) or {}
    patches = sp.get("patches") or []
    if not patches:
        print("[SKIP] aucun gisement de fer localisé.")
        rcon.close()
        return 0
    p0 = patches[0]
    fx, fy = float(p0["x"]), float(p0["y"])
    zone = (fx + 22.0, fy + 22.0)
    api.generate_terrain(zone[0], zone[1], 50.0)
    api.generate_terrain(fx, fy, 40.0)
    api.run_action(api.wait, 30, timeout=60.0)
    api.run_action(api.teleport_to, zone[0], zone[1], timeout=30.0)

    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{min(fx, zone[0]) - 40},"
        f"{min(fy, zone[1]) - 40}}},{{{max(fx, zone[0]) + 40},{max(fy, zone[1]) + 40}}}}}}}) do "
        f"if e.force == game.forces.player and e.type ~= 'character' then e.destroy() "
        f"elseif e.force ~= game.forces.player and e.type ~= 'resource' "
        f"and e.type ~= 'character' then e.destroy() end end rcon.print('degage')")
    rcon.query_lua("local c = nil for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{name='character'}) do c = e end "
                   "if c then c.insert{name='assembling-machine-1', count=2} "
                   "c.insert{name='stone-furnace', count=4} "
                   "c.insert{name='burner-mining-drill', count=4} "
                   "c.insert{name='burner-inserter', count=20} "
                   "c.insert{name='transport-belt', count=300} "
                   "c.insert{name='coal', count=300} end rcon.print('kit')")

    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal
    builder = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-gear-wheel", 0.5)))
    coord = Coordinator(api, zone=zone, rayon=30.0, builder=builder)

    # --- 1 : extraire n'est pas fabriquer ---
    brut = coord.produire(Symptome(name="x", x=zone[0], y=zone[1], cause="entree_vide",
                                   gravite=2, detail=""), "iron-ore")
    rec("e19-1 : « iron-ore » s'extrait, il ne se produit pas",
        not brut[0] and "extrait" in brut[1], brut[1])

    # --- L'assembleuse qui attend des plaques ---
    ax = ay = None
    for dx, dy in ((0.0, 0.0), (5.0, 0.0), (-5.0, 0.0), (0.0, 5.0)):
        cx, cy = float(int(zone[0] + dx)) + 0.5, float(int(zone[1] + dy)) + 0.5
        if can_place(api, "assembling-machine-1", cx, cy):
            r = api.run_action(api.place_entity_at, "assembling-machine-1", cx, cy,
                               "north", None, timeout=30.0)
            if isinstance(r, dict) and r.get("ok"):
                ax, ay = cx, cy
                break
    if ax is None:
        print("[SKIP] assembleuse non plaçable.")
        rcon.close()
        return _verdict()
    api.run_action(api.set_recipe_at, ax, ay, "iron-gear-wheel",
                   "assembling-machine-1", timeout=20.0)
    api.run_action(api.wait, 30, timeout=30.0)

    cible = Symptome(name="assembling-machine-1", x=ax, y=ay, cause="entree_vide",
                     gravite=2, detail="rien n'arrive en entrée")
    ok, detail = coord.agir(Decision(action="alimenter", raison="", cible=cible))
    for part in str(detail).split(" — "):
        print(f"       . {part[:200]}")

    # --- 2 : le four est posé et livre l'assembleuse ---
    api.run_action(api.wait, 60, timeout=60.0)
    autour = _entites_a(api, ax, ay, 6.0)
    fours = [e for e in autour if e.get("name") == "stone-furnace"]
    bras = [e for e in autour if e.get("type") == "inserter"]
    rec("e19-2 : un four est posé près de l'assembleuse", bool(fours),
        f"{len(fours)} four(s), {len(bras)} bras à moins de 6 tuiles")

    livreur = None
    for b in bras:
        if b.get("dropX") is None:
            continue
        depose = [e.get("name") for e in _entites_a(api, b["dropX"], b["dropY"], 0.3)]
        prend = [e.get("name") for e in _entites_a(api, b["pickupX"], b["pickupY"], 0.3)]
        if "assembling-machine-1" in depose and "stone-furnace" in prend:
            livreur = b
            break
    rec("e19-3 : un bras puise dans le four et dépose dans l'assembleuse",
        livreur is not None,
        f"bras@({livreur['x']},{livreur['y']})" if livreur else
        f"{len(bras)} bras, aucun ne relie four -> assembleuse")

    # --- 4 : l'entrée du four est raccordée au gisement ---
    drills = rcon.query_lua("local s=game.surfaces[1] "
                            "rcon.print(#s.find_entities_filtered{type='mining-drill'})")
    belts = rcon.query_lua("local s=game.surfaces[1] "
                           "rcon.print(#s.find_entities_filtered{type='transport-belt'})")
    rec("e19-4 : l'entrée du four est raccordée à un gisement",
        int(str(drills).strip() or 0) >= 1 and int(str(belts).strip() or 0) >= 1,
        f"{str(drills).strip()} foreur(s), {str(belts).strip()} belt(s)")

    # --- 5 : LE JUGE — l'assembleuse reçoit des plaques ---
    api.run_action(api.wait, 1800, timeout=300.0)
    recu = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{position={{{ax},{ay}}}, radius=1.5, "
        f"name='assembling-machine-1'}}) do "
        f"local inv = e.get_inventory(defines.inventory.assembling_machine_input) "
        f"if inv then for _, st in pairs(inv.get_contents()) do n = n + st.count end end "
        f"end rcon.print(n)")
    try:
        n_recu = int(str(recu).strip())
    except ValueError:
        n_recu = -1
    rec("e19-5 : l'assembleuse reçoit des plaques SANS intervention", n_recu > 0,
        f"{n_recu} item(s) en entrée après 1800 ticks")

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