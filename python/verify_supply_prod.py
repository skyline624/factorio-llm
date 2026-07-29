"""Test LIVE E13 en mode PRODUCTION : la chaîne d'approvisionnement, joueur connecté.

`verify_supply_e13.py` valide la même chose en `test_mode` : character headless,
déplacement par téléport, et surtout **aucune contrainte de portée à la pose**. Tout ce
que la chaîne bâtit y était donc posé à distance quelconque.

Ici on coupe `test_mode`. Le mod pilote le character du JOUEUR CONNECTÉ et
`state_placing_at` refuse toute pose au-delà de `build_distance + 2`. Une chaîne
d'approvisionnement est justement ce qui met cette contrainte à l'épreuve : la belt
court sur trente à cinquante tuiles, et rien dans `place_belt_line` ne fait avancer le
personnage. Si le mode test masquait ce défaut, il apparaîtra ici.

Ce qu'on vérifie :
  1. mode production actif et joueur présent pour l'incarner ;
  2. la boucle ravitaille au premier manque, comme en test ;
  3. passé le seuil, elle décide d'APPROVISIONNER ;
  4. la chaîne est réellement posée — foreur, belt continue, bras ;
  5. le boiler se remplit ensuite SANS intervention.

PRÉ-REQUIS : serveur headless + un joueur connecté (scripts/start_factorio_client.bat).
Sans joueur, `test_mode` est restauré et le script SKIP, comme les autres verify_*.

MISE EN CONDITION (par RCON, hors du mod — elle prépare le TERRAIN, rien de plus) :
`reset_character` est refusé en production par design, et le flag `kit_given` du mod est
inaccessible depuis RCON : l'inventaire du joueur est donc complété par `player.insert`.
Le bâti des runs précédents est effacé, sans quoi la pose échouerait sur ses propres
entités.
"""

from __future__ import annotations

import math
import sys

from agents.coordinator import SEUIL_AUTOMATISATION, Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.site_finder import can_place

RESULTS: list[tuple[str, bool, str]] = []

# La marche est réelle : plusieurs centaines de tuiles, pathfinding et contournements.
WALK_TIMEOUT = 300.0
PAS_MARCHE = 60.0        # on génère puis on marche par étapes (sans chunks, pas de chemin)

KIT = (("burner-mining-drill", 4), ("burner-inserter", 10), ("transport-belt", 200),
       ("small-electric-pole", 60), ("boiler", 2), ("coal", 200))


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:50s} {detail[:110]}")


def _pos(api: ModApi) -> tuple[float, float]:
    p = (api.get_state().get("character") or {}).get("position") or {}
    return float(p.get("x", 0.0)), float(p.get("y", 0.0))


def _marcher(api: ModApi, x: float, y: float) -> tuple[float, float]:
    """Génère puis marche, par étapes. Sans chunks générés le pathfinding ne planifie pas."""
    for _ in range(40):
        cx, cy = _pos(api)
        reste = math.hypot(x - cx, y - cy)
        if reste <= 8.0:
            return (cx, cy)
        t = min(1.0, PAS_MARCHE / reste)
        ex, ey = cx + (x - cx) * t, cy + (y - cy) * t
        api.generate_terrain(ex, ey, 60.0)
        api.run_action(api.walk_to, ex, ey, timeout=WALK_TIMEOUT)
        if math.hypot(*(a - b for a, b in zip(_pos(api), (cx, cy)))) < 1.0:
            return _pos(api)          # bloqué : eau, falaise — on rend la position réelle
    return _pos(api)


def main() -> int:
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(False)
    api.setup()
    st = api.get_state()
    if not st.get("ready"):
        api.set_test_mode(True)
        print("[SKIP] aucun joueur connecté : lance scripts/start_factorio_client.bat, "
              "rejoins 127.0.0.1:34197, puis relance ce script (test_mode restauré).")
        rcon.close()
        return 0
    rec("prod-1 : mode production + joueur connecté",
        st.get("test_mode") is False and st.get("ready") is True,
        f"test_mode={st.get('test_mode')} position={(st.get('character') or {}).get('position')}")

    # Table rase du bâti et complément d'inventaire : mise en condition, pas mesure.
    efface = rcon.query_lua(
        "local n = 0 for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered{force='player'}) do "
        "if e.type ~= 'character' then e.destroy() n = n + 1 end end "
        "game.surfaces[1].clear_pollution() rcon.print(n)")
    ins = "".join(f"p.insert{{name='{n}', count={c}}} " for n, c in KIT)
    rcon.query_lua(f"local p = game.players[1] if p and p.character then {ins} end "
                   f"rcon.print('ok')")

    sp = api.scan_patch("coal", 400.0)
    ech = sp.get("sample") or []
    if not ech:
        api.set_test_mode(True)
        print("[SKIP] aucun gisement de charbon localisé.")
        rcon.close()
        return 0
    cx, cy = float(ech[0]["x"]), float(ech[0]["y"])
    depart = _pos(api)
    print(f"       . {str(efface).strip()} entité(s) effacée(s) | charbon@({cx:.0f},{cy:.0f}) "
          f"à {math.hypot(cx - depart[0], cy - depart[1]):.0f} tuiles du joueur")

    # On marche VRAIMENT jusqu'au gisement, puis on pose le boiler à portée de main.
    arrivee = _marcher(api, cx + 16.0, cy + 16.0)
    dist = math.hypot(cx - arrivee[0], cy - arrivee[1])
    rec("prod-2 : le joueur a marché jusqu'au gisement", dist <= 40.0,
        f"joueur@({arrivee[0]:.0f},{arrivee[1]:.0f}), charbon à {dist:.0f} tuiles")
    if dist > 40.0:
        api.set_test_mode(True)
        print("[SKIP] le joueur n'a pas pu rejoindre le gisement (eau ou falaise).")
        rcon.close()
        return _verdict()

    pose = None
    for dx, dy in ((3.0, 0.0), (-3.0, 0.0), (0.0, 3.0), (0.0, -3.0), (5.0, 5.0)):
        bx = math.floor(arrivee[0] + dx) + 0.5
        by = float(round(arrivee[1] + dy))
        if can_place(api, "boiler", bx, by):
            r = api.run_action(api.place_entity_at, "boiler", bx, by, "north", None,
                               timeout=30.0)
            if isinstance(r, dict) and r.get("ok"):
                pose = (bx, by)
                break
    if pose is None:
        api.set_test_mode(True)
        print("[SKIP] boiler non plaçable à portée du joueur.")
        rcon.close()
        return _verdict()
    api.run_action(api.wait, 60, timeout=60.0)

    zone = pose
    coord = Coordinator(api, zone=zone, rayon=25.0, ombre=True)
    print(f"       . boiler@{pose}, charbon à {math.hypot(cx - pose[0], cy - pose[1]):.0f} "
          f"tuiles | mode ombre {'ACTIF' if coord.arbitre is not None else 'inactif'}")

    d1, agi1, _ = coord.tick()
    rec("prod-3 : premier manque -> il ravitaille", d1.action == "ravitailler" and agi1,
        f"{d1}")

    for _ in range(SEUIL_AUTOMATISATION):
        rcon.query_lua(
            f"local s = game.surfaces[1] "
            f"for _, e in pairs(s.find_entities_filtered{{name='boiler', "
            f"position={{{pose[0]},{pose[1]}}}, radius=2}}) do "
            f"local i = e.get_fuel_inventory() if i then i.clear() end end rcon.print('vide')")
        api.run_action(api.wait, 60, timeout=60.0)
        d, agi, detail = coord.tick()
    rec("prod-4 : après le seuil -> il décide d'APPROVISIONNER",
        d.action == "approvisionner", f"{d.action} | {str(detail)[:80]}")

    # La chaîne, telle qu'elle existe RÉELLEMENT sur la carte.
    api.run_action(api.wait, 120, timeout=60.0)
    etat = rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"local d = #s.find_entities_filtered{{type='mining-drill'}} "
        f"local b = #s.find_entities_filtered{{type='transport-belt'}} "
        f"local i = #s.find_entities_filtered{{type='inserter'}} "
        f"rcon.print(d .. ';' .. b .. ';' .. i)")
    try:
        nd, nb, ni = (int(v) for v in str(etat).strip().split(";"))
    except ValueError:
        nd = nb = ni = -1
    rec("prod-5 : la chaîne est posée malgré la contrainte de portée",
        nd >= 1 and nb >= 1 and ni >= 1,
        f"{nd} foreur(s), {nb} belt(s), {ni} bras — {str(detail)[:70]}")

    # PREUVE : le boiler se remplit sans qu'on y touche.
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{name='boiler', "
        f"position={{{pose[0]},{pose[1]}}}, radius=2}}) do "
        f"local i = e.get_fuel_inventory() if i then i.clear() end end rcon.print('vide')")
    api.run_action(api.wait, 900, timeout=300.0)
    reste = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{name='boiler', "
        f"position={{{pose[0]},{pose[1]}}}, radius=2}}) do "
        f"local i = e.get_fuel_inventory() "
        f"if i then for _, st2 in pairs(i.get_contents()) do n = n + st2.count end end end "
        f"rcon.print(n)")
    try:
        charbon = int(str(reste).strip())
    except ValueError:
        charbon = -1
    rec("prod-6 : le boiler se remplit SANS intervention", charbon > 0,
        f"réservoir vidé puis laissé seul -> {charbon} charbon(s) apporté(s)")

    print("\n       --- journal du Coordinator ---")
    for j in coord.journal:
        print(f"       . {j[:150]}")

    api.set_test_mode(True)      # on ne laisse pas le serveur inutilisable pour les autres
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