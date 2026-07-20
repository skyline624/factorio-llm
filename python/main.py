"""Demo du socle RCON factorio-llm (mode dual).

Valide la boucle complete : connexion RCON -> lecture d'etat -> actions async ->
attente de completion -> relecture d'etat + observations.

Mode dual :
  --test : bascule le mod en mode test headless (character spawné, actions simulees).
           Permet de valider le tuyau RCON SANS connecter de joueur.
  (defaut) : mode production (joueur connecte, physique reelle). Un joueur doit
             avoir rejoint le serveur (il est l'IA).

Usage :
    cd python
    python main.py            # production (joueur connecte requis)
    python main.py --test     # test headless (aucun joueur requis)

Prerequis : Factorio lance avec le mod factorio-llm active et RCON ouvert sur
RCON_HOST:RCON_PORT avec RCON_PASSWORD (voir scripts/start_factorio_dedicated.bat).
"""

from __future__ import annotations

import argparse
import json
import sys
import time

from config import load_config
from core.mod_api import ModApi
from core.rcon import RconError, get_rcon
from core.state import GameState


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Demo factorio-llm (mode dual).")
    p.add_argument("--test", action="store_true",
                   help="mode test headless (aucun joueur requis, actions simulees)")
    return p.parse_args()


def _print(label: str, obj: object) -> None:
    print(f"[demo] {label} = {json.dumps(obj, ensure_ascii=False) if not isinstance(obj, str) else obj}")


def main() -> int:
    args = _parse_args()
    cfg = load_config()
    rcon = get_rcon(cfg.rcon_host, cfg.rcon_port, cfg.rcon_password)
    api = ModApi(rcon)

    print(f"[demo] connexion RCON {cfg.rcon_host}:{cfg.rcon_port} ...")
    try:
        state = api.get_state()
    except RconError as e:
        print(f"[demo] ECHEC connexion RCON : {e}")
        print("[demo] Verifie que Factorio tourne avec --rcon-port/--rcon-password et le mod active.")
        return 1

    # Bascule du mode dual au runtime si demande.
    if args.test:
        print("[demo] bascule en mode test headless (set_test_mode(true))")
        api.set_test_mode(True)
    else:
        api.set_test_mode(False)

    # Initialisation de l'avatar IA (idempotent).
    api.setup()
    state = api.get_state()
    gs = GameState.from_dict(state)
    _print("connecte", {"tick": gs.tick, "ready": gs.ready, "test_mode": gs.test_mode})

    if not gs.ready:
        print("[demo] ECHEC : aucun avatar IA apres setup (connecter un joueur en production).")
        return 1

    p = gs.pos_tuple()
    _print("position initiale", p)
    _print("inventaire (extrait)", dict(list(gs.inventory.items())[:6]))

    # --- Observation : scan + describe ---
    scan = api.scan_area(20)
    n_ents = len(scan.get("entities", [])) if isinstance(scan, dict) else 0
    _print("scan_area(20)", {"entities": n_ents, "resources": list((scan.get("resources", {}) or {}).keys()) if isinstance(scan, dict) else []})

    # --- Action : walk_to_entity ---
    print("[demo] walk_to_entity('iron-ore', 200)")
    api.walk_to_entity("iron-ore", 200)
    res = api.wait_until_idle(timeout=20.0)
    _print("walk termine", res.get("last_result") if isinstance(res, dict) else res)

    state = api.get_state()
    gs = GameState.from_dict(state)
    _print("position apres walk", gs.pos_tuple())

    # --- Action : mine_entity ---
    print("[demo] mine_entity('iron-ore', 5)")
    api.mine_entity("iron-ore", 5)
    res = api.wait_until_idle(timeout=30.0)
    _print("mine termine", res.get("last_result") if isinstance(res, dict) else res)
    state = api.get_state()
    gs = GameState.from_dict(state)
    _print("inventaire iron-ore", gs.inventory.get("iron-ore", 0))

    # --- Action : craft_item ---
    print("[demo] craft_item('iron-gear-wheel', 5)")
    api.craft_item("iron-gear-wheel", 5)
    res = api.wait_until_idle(timeout=30.0)
    _print("craft termine", res.get("last_result") if isinstance(res, dict) else res)
    state = api.get_state()
    gs = GameState.from_dict(state)
    _print("inventaire iron-gear-wheel", gs.inventory.get("iron-gear-wheel", 0))

    # --- Observation : find_nearest + describe + get_recipe + production_stats ---
    fn = api.find_nearest("iron-ore")
    _print("find_nearest(iron-ore)", fn)
    desc = api.describe("burner-mining-drill")
    _print("describe(burner-mining-drill)", {"has_entity": isinstance(desc, dict) and "entity" in desc})
    recipe = api.get_recipe("iron-gear-wheel")
    _print("get_recipe(iron-gear-wheel)", recipe)
    stats = api.production_stats()
    _print("production_stats", {"produced_keys": len((stats.get("produced", {}) or {})) if isinstance(stats, dict) else 0})

    print("[demo] OK : boucle RCON complete validee.")
    rcon.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())