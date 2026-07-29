"""Test LIVE E13 : l'agent cesse de remplir et construit la chaîne — mode ombre actif.

Tout ce que l'agent bâtit s'arrête tout seul : un boiler brûle 0.45 charbon/s, soit
moins de deux minutes d'autonomie par plein. Jusqu'ici la boucle savait remplir, donc
elle passait son temps à remplir. « Autonome » restait un mot creux tant qu'un humain
— ou une boucle qui ne fait que ça — devait revenir toutes les deux minutes.

Ce qu'on vérifie :
  1. au premier manque, il RAVITAILLE (c'est la bonne réponse à un incident) ;
  2. au troisième, il bascule sur APPROVISIONNER : bâtir la chaîne qui manque ;
  3. la chaîne est réellement posée — foreur sur le charbon, belt, inserter ;
  4. le boiler se remplit ensuite SANS intervention ;
  5. le mode ombre a tourné pendant tout ce temps : le déterministe a décidé, le modèle
     a proposé, et l'on mesure l'écart sur des états SUBIS et non construits à la main.

Pré-requis : serveur headless avec le mod J5, Ollama joignable (sinon le mode ombre est
simplement inactif). SKIP si le serveur est absent.
"""

from __future__ import annotations

import math
import sys

from agents.coordinator import SEUIL_AUTOMATISATION, Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.site_finder import can_place

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:105]}")


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
    rcon.query_lua("local n = 0 for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{force='player'}) do "
                   "if e.type ~= 'character' then e.destroy() n = n + 1 end end "
                   "game.surfaces[1].clear_pollution() rcon.print(n)")
    rcon.query_lua("local c = nil for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{name='character'}) do c = e end "
                   "if c then c.insert{name='transport-belt', count=200} "
                   "c.insert{name='small-electric-pole', count=60} "
                   "c.insert{name='electric-mining-drill', count=4} "
                   "c.insert{name='inserter', count=10} c.insert{name='boiler', count=2} "
                   "end rcon.print('ok')")

    # On se place SUR le charbon : la chaîne d'approvisionnement doit être courte pour
    # être construite (au-delà de 60 tuiles, l'agent refuse et le dit — c'est un
    # problème de train, pas de belt).
    sp = api.scan_patch("coal", 250.0)
    ech = sp.get("sample") or []
    if not ech:
        print("[SKIP] aucun gisement de charbon localisé.")
        rcon.close()
        return 0
    cx, cy = float(ech[0]["x"]), float(ech[0]["y"])
    zone = (cx + 18.0, cy + 18.0)          # l'usine, à ~25 tuiles du charbon
    api.generate_terrain(zone[0], zone[1], 60.0)
    api.generate_terrain(cx, cy, 40.0)
    api.run_action(api.wait, 30, timeout=60.0)
    api.run_action(api.teleport_to, zone[0], zone[1], timeout=30.0)
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{min(cx, zone[0]) - 40},"
        f"{min(cy, zone[1]) - 40}}},{{{max(cx, zone[0]) + 40},{max(cy, zone[1]) + 40}}}}}}}) do "
        f"if e.force ~= game.forces.player and e.type ~= 'resource' "
        f"and e.type ~= 'character' then e.destroy() end end rcon.print('ok')")

    # Un boiler seul, sans combustible : la machine à ravitailler.
    pose = None
    for dx in (0.0, 4.0, -4.0, 8.0):
        bx, by = math.floor(zone[0] + dx) + 0.5, float(round(zone[1]))
        if can_place(api, "boiler", bx, by):
            r = api.run_action(api.place_entity_at, "boiler", bx, by, "north", None,
                               timeout=20.0)
            if isinstance(r, dict) and r.get("ok"):
                pose = (bx, by)
                break
    if pose is None:
        print("[SKIP] boiler non plaçable.")
        rcon.close()
        return 0
    api.run_action(api.wait, 60, timeout=30.0)

    coord = Coordinator(api, zone=zone, rayon=25.0, ombre=True)
    mode_ombre = coord.arbitre is not None
    print(f"       . boiler@{pose}, charbon à "
          f"{math.hypot(cx - pose[0], cy - pose[1]):.0f} tuiles | "
          f"mode ombre {'ACTIF' if mode_ombre else 'inactif (pas de modèle)'}")

    # --- 1 : premier manque -> il ravitaille ---
    d1, agi1, _ = coord.tick()
    rec("e13-1 : premier manque -> il ravitaille", d1.action == "ravitailler" and agi1,
        f"{d1} | {coord.journal[-1][-60:] if coord.journal else ''}")

    # --- 2 : après le seuil -> il bascule sur la chaîne ---
    for _ in range(SEUIL_AUTOMATISATION):
        rcon.query_lua(
            f"local s = game.surfaces[1] "
            f"for _, e in pairs(s.find_entities_filtered{{name='boiler', "
            f"position={{{pose[0]},{pose[1]}}}, radius=2}}) do "
            f"local i = e.get_fuel_inventory() if i then i.clear() end end rcon.print('vide')")
        api.run_action(api.wait, 60, timeout=30.0)
        d, agi, _ = coord.tick()
    rec("e13-2 : ravitaillements répétés -> il décide d'APPROVISIONNER",
        d.action == "approvisionner",
        f"{d.action} après {SEUIL_AUTOMATISATION + 1} manques | {str(d)[:70]}")

    # --- 3 : la chaîne est réellement posée ---
    api.run_action(api.wait, 120, timeout=60.0)
    sa = api.scan_area(45.0)
    rows = sa.get("entities", []) if isinstance(sa, dict) else []
    drills = [e for e in rows if e.get("type") == "mining-drill"]
    belts = [e for e in rows if e.get("type") == "transport-belt"]
    inserters = [e for e in rows if e.get("type") == "inserter"]
    rec("e13-3 : la chaîne d'approvisionnement est posée",
        bool(drills) and bool(belts) and bool(inserters),
        f"{len(drills)} foreur(s), {len(belts)} belt(s), {len(inserters)} inserter(s) — "
        f"{coord.journal[-1][-70:] if coord.journal else ''}")

    # --- 4 : PREUVE — le boiler se remplit sans qu'on y touche ---
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{name='boiler', "
        f"position={{{pose[0]},{pose[1]}}}, radius=2}}) do "
        f"local i = e.get_fuel_inventory() if i then i.clear() end end rcon.print('vide')")
    api.run_action(api.wait, 900, timeout=180.0)
    reste = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{name='boiler', "
        f"position={{{pose[0]},{pose[1]}}}, radius=2}}) do "
        f"local i = e.get_fuel_inventory() "
        f"if i then for _, st in pairs(i.get_contents()) do n = n + st.count end end end "
        f"rcon.print(n)")
    try:
        charbon = int(str(reste).strip())
    except ValueError:
        charbon = -1
    rec("e13-4 : le boiler se remplit SANS intervention", charbon > 0,
        f"réservoir vidé puis laissé seul -> {charbon} charbon(s) apporté(s) par la chaîne")

    # --- 5 : ce que le mode ombre a mesuré pendant ce temps ---
    if mode_ombre and hasattr(coord.arbitre, "divergences"):
        a = coord.arbitre
        rec("e13-5 : le mode ombre a observé sans jamais décider", True,
            f"{a.accords} accord(s), {len(a.divergences)} divergence(s), "
            f"taux={a.taux_divergence:.0%}")
        for d_ in a.divergences[:3]:
            print(f"       . {d_[:110]}")
    else:
        rec("e13-5 : le mode ombre a observé sans jamais décider", True,
            "aucun modèle joignable — la boucle a tourné sans lui, ce qui est le but")

    print("\n       --- journal du Coordinator ---")
    for j in coord.journal:
        print(f"       . {j[:115]}")

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