"""Test LIVE E15 : les causes que le diagnostic savait NOMMER sans savoir traiter.

Le FactoryDoctor distingue huit causes depuis longtemps ; la boucle n'en réparait que
trois. Les cinq autres finissaient en « pas encore automatisé » — un diagnostic juste
suivi d'une usine à l'arrêt.

Le protocole ne change pas : **casser d'une manière connue, et vérifier que la machine
REPART**. Le juge est l'état APRÈS l'action, jamais le fait qu'elle ait été tentée.

  1. `regler_recette` — assembleuse sans recette : elle attend indéfiniment ;
  2. `evacuer`        — sortie saturée : arrête une machine aussi sûrement qu'un
                        réservoir vide, et le produit n'a pas à être connu d'avance ;
  3. `reactiver`      — `active = false` : aucun ingrédient ne manque, rien d'autre
                        n'y changerait quoi que ce soit ;
  4. `alimenter`      — entrée vide : c'est le même problème qu'un manque de
                        combustible, il manque une chaîne. On réutilise donc
                        `approvisionner` avec l'ingrédient attendu.

`renforcer_energie` n'est pas éprouvé ici : il bâtit une seconde centrale, ce que
`verify_coordinator_e8` couvre déjà, et qui demande une rive.

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
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:44s} {detail[:106]}")


def _statut(api, nom, x, y) -> str:
    for e in _entites_a(api, x, y, 1.5):
        if e.get("name") == nom:
            return str(e.get("status", "?"))
    return "absente"


def _cible(nom, x, y, cause) -> Symptome:
    return Symptome(name=nom, x=x, y=y, cause=cause, gravite=2, detail="injecté")


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
    pos = (api.get_state().get("character") or {}).get("position") or {}
    px, py = float(pos.get("x", 0.0)), float(pos.get("y", 0.0))

    # Terrain de jeu : on dégage et on se donne de quoi poser.
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{px - 20},{py - 20}}},"
        f"{{{px + 20},{py + 20}}}}}}}) do "
        f"if e.type ~= 'character' and e.type ~= 'resource' then e.destroy() end end "
        f"rcon.print('degage')")
    rcon.query_lua("local c = nil for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{name='character'}) do c = e end "
                   "if c then c.insert{name='assembling-machine-1', count=3} "
                   "c.insert{name='stone-furnace', count=3} c.insert{name='iron-ore', count=50} "
                   "c.insert{name='coal', count=100} end rcon.print('kit')")

    # L'objectif doit être fabricable PAR CETTE MACHINE : une assembleuse ne fait pas
    # de `smelting`. Donner « iron-plate » à une assembleuse n'éprouverait que le refus.
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal
    builder = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-gear-wheel", 0.5)))
    coord = Coordinator(api, zone=(px, py), rayon=20.0, builder=builder)

    # --- 1 : une assembleuse sans recette ---
    ax = ay = None
    for dx in (6.0, -6.0, 10.0, -10.0):
        cx, cy = float(int(px + dx)) + 0.5, float(int(py)) + 0.5
        if can_place(api, "assembling-machine-1", cx, cy):
            r = api.run_action(api.place_entity_at, "assembling-machine-1", cx, cy,
                               "north", None, timeout=20.0)
            if isinstance(r, dict) and r.get("ok"):
                ax, ay = cx, cy
                break
    if ax is None:
        print("[SKIP] assembleuse non plaçable.")
        rcon.close()
        return 0
    api.run_action(api.wait, 30, timeout=30.0)
    avant = _statut(api, "assembling-machine-1", ax, ay)
    ok, detail = coord.agir(Decision(action="regler_recette", raison="",
                                     cible=_cible("assembling-machine-1", ax, ay,
                                                  "sans_recette")))
    api.run_action(api.wait, 30, timeout=30.0)
    apres = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 'aucune' "
        f"for _, e in pairs(s.find_entities_filtered{{position={{{ax},{ay}}}, radius=1.5, "
        f"name='assembling-machine-1'}}) do local r = e.get_recipe() "
        f"if r then n = r.name end end rcon.print(n)")
    rec("e15-1 : recette réglée sur une machine qui attendait",
        ok and str(apres).strip() not in ("aucune", ""),
        f"{avant} -> recette « {str(apres).strip()} » ({detail[:40]})")

    # --- 2 : une sortie saturée ---
    fx = fy = None
    for dx in (6.0, -6.0, 12.0, -12.0):
        cx, cy = float(int(px + dx)) + 0.5, float(int(py + 6.0)) + 0.5
        if can_place(api, "stone-furnace", cx, cy):
            r = api.run_action(api.place_entity_at, "stone-furnace", cx, cy, "north",
                               None, timeout=20.0)
            if isinstance(r, dict) and r.get("ok"):
                fx, fy = cx, cy
                break
    if fx is None:
        print("[SKIP] four non plaçable.")
        rcon.close()
        return _verdict()
    api.run_action(api.wait, 30, timeout=30.0)
    # On SATURE la sortie : le four ne pourra plus rien produire.
    rempli = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{position={{{fx},{fy}}}, radius=1.5, "
        f"name='stone-furnace'}}) do local o = e.get_output_inventory() "
        f"if o then n = o.insert{{name='iron-plate', count=100}} end end rcon.print(n)")
    ok2, det2 = coord.agir(Decision(action="evacuer", raison="",
                                    cible=_cible("stone-furnace", fx, fy,
                                                 "sortie_bloquee")))
    api.run_action(api.wait, 30, timeout=30.0)
    reste = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{position={{{fx},{fy}}}, radius=1.5, "
        f"name='stone-furnace'}}) do local o = e.get_output_inventory() "
        f"if o then for _, st in pairs(o.get_contents()) do n = n + st.count end end end "
        f"rcon.print(n)")
    try:
        n_reste = int(str(reste).strip())
    except ValueError:
        n_reste = -1
    rec("e15-2 : sortie saturée vidée sans nommer le produit",
        ok2 and 0 <= n_reste < 100,
        f"{str(rempli).strip()} plaque(s) insérée(s) -> reste {n_reste} ({det2[:40]})")

    # --- 3 : une machine désactivée ---
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{position={{{fx},{fy}}}, radius=1.5, "
        f"name='stone-furnace'}}) do e.active = false end rcon.print('desactive')")
    ok3, det3 = coord.agir(Decision(action="reactiver", raison="",
                                    cible=_cible("stone-furnace", fx, fy, "desactivee")))
    api.run_action(api.wait, 30, timeout=30.0)
    actif = rcon.query_lua(
        f"local s = game.surfaces[1] local a = 'introuvable' "
        f"for _, e in pairs(s.find_entities_filtered{{position={{{fx},{fy}}}, radius=1.5, "
        f"name='stone-furnace'}}) do a = tostring(e.active) end rcon.print(a)")
    rec("e15-3 : machine désactivée réactivée",
        ok3 and str(actif).strip() == "true",
        f"active={str(actif).strip()} ({det3[:50]})")

    # --- 4 : l'ingrédient attendu est LU, pas deviné ---
    besoin = coord._ingredient_manquant(_cible("assembling-machine-1", ax, ay,
                                               "entree_vide"))
    rec("e15-4 : l'ingrédient d'une entrée vide est déduit de la recette",
        besoin is not None,
        f"{besoin} attendu par l'assembleuse (recette lue sur la machine)")

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