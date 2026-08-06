"""Test LIVE du bois (H11) : cinq affirmations du correctif, vérifiées une par une.

Le correctif tient en une ligne de table — `"wood": {"mode": "mine", "cible": "tree"}` —
mais il repose sur une chaîne d'hypothèses côté mod, TOUTES lues dans le Lua et AUCUNE
mesurée. C'est exactement la situation où un correctif « évident » ne marche pas :

  1. `find_nearest("tree")` doit retomber sur le TYPE quand le nom ne donne rien
     (`utils_entity.find_target_entities` : pcall name, puis pcall type) ;
  2. `walk_to_entity("tree")` doit accepter ce même type et amener le personnage à
     portée de minage (`MINING_REACH = 5` en production) ;
  3. le handler MINING ne doit pas récuser un arbre : il passe par `nearest_idle_entity`
     dès que `type ~= "resource"`, et `en_service` n'écarte une cible que si un inserteur
     pointe dessus — jamais le cas d'un arbre, mais c'est une déduction, pas une mesure ;
  4. un arbre miné doit rendre du `wood` dans l'inventaire du joueur, et le compte du
     mod porte sur les ENTITÉS minées, pas sur les items obtenus ;
  5. le débouché doit s'ouvrir : avec du bois, `wooden-chest` devient fabricable — c'est
     la raison d'être du correctif, pas un détail. Sans coffre, `batir_evacuation`
     échouait « missing={'wooden-chest': 1} » avec dix plaques de fer en poche.

PRÉ-REQUIS : serveur lancé ET un joueur connecté. Sans avatar, `get_ai_entity()` est nil,
le minage n'a pas de main : le script SKIP (return 0) comme les autres verify_* de prod.

CE SCRIPT NE RESTAURE PAS LA RÉFÉRENCE. Il a besoin d'un joueur connecté, donc d'une
partie en cours ; restaurer la couperait. Il ne pose ni ne détruit rien non plus — il
récolte du bois, ce qui repousse au pire quelques arbres.

Usage :
    cd python
    python verify_bois.py
"""

from __future__ import annotations

import sys

from core.mod_api import ModApi
from core.rcon import get_rcon
from services import perception
from services.knowledge import ProductionGoal, plan_production

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:110]}")


def main() -> int:
    rcon = get_rcon()
    api = ModApi(rcon)

    etat = api.get_state()
    if not (etat or {}).get("character"):
        print("SKIP : aucun avatar (connecter un joueur avant de lancer ce script)")
        rcon.close()
        return 0
    if (etat or {}).get("test_mode"):
        print("SKIP : test_mode actif — la portée de minage n'y est pas jugée")
        rcon.close()
        return 0

    # --- 1. Le fallback par TYPE ---------------------------------------------------
    trouve = api.find_nearest("tree")
    x, y = (trouve or {}).get("x"), (trouve or {}).get("y")
    rec("find_nearest('tree') trouve un arbre", x is not None and y is not None,
        f"{trouve}")
    if x is None:
        print("\naucun arbre à 400 tuiles : la suite n'a rien à mesurer")
        rcon.close()
        return 1

    # --- 2. Le plan déterministe ---------------------------------------------------
    # Le plan est PUR : il se juge sans le jeu. On le vérifie tout de même ici, car ce
    # qu'on veut savoir c'est si le plan que l'agent EXÉCUTERA vise bien un `tree`.
    steps = plan_production(ProductionGoal("wood", 4), {},
                            lambda i: perception.recipe_of(api, i))
    kinds = [s.kind for s in steps]
    cibles = sorted({str(s.args.get("name")) for s in steps})
    rec("le plan vise l'arbre et non le bois",
        kinds == ["find_nearest", "walk_to_entity", "mine_entity"] and cibles == ["tree"],
        f"{kinds} visant {cibles}")

    # --- 3. La marche accepte le type ----------------------------------------------
    avant_pos = (etat["character"]["position"]["x"], etat["character"]["position"]["y"])
    api.run_action(api.walk_to_entity, "tree", 400, timeout=180.0)
    apres = api.get_state()["character"]["position"]
    dist = max(abs(apres["x"] - x), abs(apres["y"] - y))
    # `MINING_REACH = 5` : au-delà, le handler ne trouve aucune cible et cale.
    rec("walk_to_entity('tree') amène à portée de minage", dist <= 6.0,
        f"({avant_pos[0]:.0f},{avant_pos[1]:.0f}) -> ({apres['x']:.0f},{apres['y']:.0f}) "
        f"soit {dist:.1f} tuile(s) de l'arbre")

    # --- 4. Le minage rend du bois --------------------------------------------------
    bois_avant = perception.inventory(api).get("wood", 0)
    res = api.run_action(api.mine_entity, "tree", 2, timeout=120.0)
    bois_apres = perception.inventory(api).get("wood", 0)
    rec("miner un arbre rend du bois", bois_apres > bois_avant,
        f"wood {bois_avant} -> {bois_apres} ({bois_apres - bois_avant:+d}) — {res}")

    # --- 5. Le débouché s'ouvre -----------------------------------------------------
    # Ce qui compte n'est pas d'avoir du bois, c'est que le coffre redevienne atteignable.
    coffre = perception.recipe_of(api, "wooden-chest")
    besoin = dict(coffre or [])
    inv = perception.inventory(api)
    faisable = bool(coffre) and all(inv.get(n, 0) >= q for n, q in besoin.items())
    rec("wooden-chest redevient fabricable", faisable,
        f"recette {besoin} contre inventaire wood={inv.get('wood', 0)}")

    rcon.close()
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
