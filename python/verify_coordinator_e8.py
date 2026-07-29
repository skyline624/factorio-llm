"""Test LIVE E8 : le Coordinator part d'une carte VIDE et bâtit son usine.

E7 montrait un agent capable de réparer ce qu'on avait bâti pour lui. Ici la carte est
rase : pas de centrale, pas de machine, rien. Personne ne lui dit de commencer par
l'énergie, ni où trouver l'eau, ni quel gisement viser.

Ce qu'on vérifie est l'enchaînement du CURRICULUM, dans l'ordre et sans intervention :

    tour 1   rien d'alimenté          -> batir_energie     -> centrale + ligne
    tour 2   du courant, 0 machine    -> batir_production  -> drill + inserter + four
    tour 3   tout tourne              -> rien

Puis on casse, et il répare — pour montrer que bâtir n'a pas fait perdre la capacité
de maintenir. Une boucle qui ne sait que construire empile les usines mortes ; c'est
justement le reproche que le benchmark FLE adresse aux agents LLM.

Le décor n'est pas monté d'avance : c'est la différence avec tous les tests précédents.

Pré-requis : serveur headless, mod E3a. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:54s} {detail[:105]}")


def _degager_large(rcon, r: float = 260.0) -> None:
    """Enlève la végétation autour de l'origine : les arbres refusent les poses.

    Le Coordinator ne rase rien de lui-même — raser sans discernement détruit ce qu'on
    vient de bâtir (leçon E5). Dégager le terrain est donc une mise en condition du
    test, faite une fois, avant qu'il ne commence.
    """
    rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{-{r},-{r}}},{{{r},{r}}}}}}}) do "
        f"if e.force ~= game.forces.player and e.type ~= 'resource' "
        f"and e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")


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
    efface = rcon.query_lua(
        "local n = 0 for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered{force='player'}) do "
        "if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    rcon.query_lua("local c = nil for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{name='character'}) do c = e end "
                   "if c then c.insert{name='small-electric-pole', count=80} c.insert{name='coal', count=300} "
                   "c.insert{name='electric-mining-drill', count=4} "
                   "c.insert{name='inserter', count=10} end rcon.print('ok')")
    api.generate_terrain(0.0, 0.0, 200.0)
    api.run_action(api.wait, 60, timeout=60.0)
    _degager_large(rcon)
    print(f"       . carte rase : {str(efface).strip()} entité(s) effacée(s), "
          f"végétation dégagée")

    # Le Coordinator travaille autour du gisement : il ira chercher l'eau lui-même.
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 0.5)))
    sp = fb._scan_patch_local("iron-ore")
    ancre = fb._anchor_on_ore(sp, 4) if sp.get("sample") else None
    if ancre is None:
        print("[SKIP] aucun gisement de fer exploitable.")
        rcon.close()
        return 0
    api.run_action(api.teleport_to, ancre[0], ancre[1] + 3.0, timeout=30.0)
    coord = Coordinator(api, zone=(ancre[0], ancre[1]), rayon=25.0, builder=fb)

    # --- 1 : carte vide -> il commence par l'énergie ---
    d1, agi1, _ = coord.tick()
    rec("e8-1 : carte vide -> il décide de bâtir l'ÉNERGIE en premier",
        d1.action == "batir_energie" and agi1,
        f"{d1} | {coord.journal[-1][-90:] if coord.journal else ''}")

    # --- 2 : du courant -> il bâtit la production ---
    api.run_action(api.wait, 180, timeout=60.0)
    d2, agi2, _ = coord.tick()
    rec("e8-2 : du courant, aucune machine -> il bâtit la PRODUCTION",
        d2.action == "batir_production" and agi2,
        f"{d2} | {coord.journal[-1][-90:] if coord.journal else ''}")

    # --- 3 : il s'arrête quand tout tourne ---
    api.run_action(api.wait, 300, timeout=90.0)
    d3, agi3, etat3 = coord.tick()
    rec("e8-3 : usine complète -> il s'arrête de lui-même",
        d3.action == "rien" and not agi3,
        f"{d3} | machines={etat3.machines} réseau={etat3.reseau}")

    # --- 4 : preuve dans le jeu — la chaîne produit ---
    api.run_action(api.wait, 600, timeout=120.0)
    sa = api.scan_area(25.0)
    rows = sa.get("entities", []) if isinstance(sa, dict) else []
    drill = next((e for e in rows if e.get("name") == "electric-mining-drill"), None)
    four = next((e for e in rows if e.get("name") == "electric-furnace"), None)
    avant = api.get_state().get("inventory", {}).get("iron-plate", 0)
    if four:
        api.run_action(api.move_items_at, "iron-plate", "electric-furnace",
                       four["x"], four["y"], 0, False, timeout=30.0)
    apres = api.get_state().get("inventory", {}).get("iron-plate", 0)
    rec("e8-4 : l'usine qu'il a bâtie PRODUIT", apres > avant,
        f"drill={drill.get('status') if drill else None} "
        f"four={four.get('status') if four else None} "
        f"iron-plate {avant} -> {apres} (+{apres - avant})")

    # --- 5 : bâtir ne lui a pas fait perdre la capacité de réparer ---
    if drill:
        # Rayon large : la ligne principale passe près des machines et suffit à les
        # couvrir. En retirer trop peu ne casse rien, et on jugerait la réaction de
        # l'agent à une panne qui n'existe pas.
        n_det = rcon.query_lua(
            f"local s = game.surfaces[1] local n = 0 "
            f"for _, e in pairs(s.find_entities_filtered{{type='electric-pole', "
            f"position={{{drill['x']},{drill['y']}}}, radius=9}}) do e.destroy() n = n + 1 end "
            f"rcon.print(n)")
        api.run_action(api.wait, 240, timeout=60.0)
        # On VÉRIFIE que la panne existe avant de tester la réparation.
        avant_ps = api.get_power_state(drill["x"], drill["y"], 2.0)
        casse = avant_ps.get("networkId") is None
        d5, agi5, _ = coord.tick()
        rec("e8-5 : on le débranche -> il répare encore",
            casse and d5.action == "relier" and agi5,
            f"{str(n_det).strip()} poteau(x) retiré(s), machine débranchée={casse} "
            f"-> {d5}")
    else:
        rec("e8-5 : on le débranche -> il répare encore", False, "drill introuvable")

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