"""Vérification RCON : ce que fl_tools expose réellement (describe/get_recipe).

Lance (serveur headless déjà démarré), puis inspecte recettes + machines pour
valider les champs disponibles : craft_time/energy, crafting_categories,
craftingSpeed, miningSpeed, products, category. Sortie JSON brute indexable.

Usage :
    cd python
    python verify_rcon_data.py
"""
from __future__ import annotations

import json

from core.rcon import get_rcon
from core.mod_api import ModApi

# Items = recettes (P1 fer/cuivre + P3 circuits pour tester la couverture).
ITEMS = [
    "iron-plate", "copper-plate", "stone-brick",
    "iron-gear-wheel", "copper-wire", "electronic-circuit",
]
# Machines = entities placeables (les producteurs du graphe).
MACHINES = [
    "stone-furnace", "steel-furnace",
    "assembling-machine-1", "assembling-machine-2", "assembling-machine-3",
    "burner-mining-drill", "electric-mining-drill",
]


def main() -> None:
    rcon = get_rcon()
    api = ModApi(rcon)

    print("=== setup headless ===")
    print("set_test_mode:", json.dumps(api.set_test_mode(True), ensure_ascii=False))
    print("setup:", json.dumps(api.setup(), ensure_ascii=False))

    print("\n=== get_recipe (items) ===")
    for it in ITEMS:
        print(f"--- get_recipe({it!r}) ---")
        print(json.dumps(api.get_recipe(it), ensure_ascii=False, indent=2))

    print("\n=== describe (items = recettes) ===")
    for it in ITEMS:
        print(f"--- describe({it!r}) ---")
        print(json.dumps(api.describe(it), ensure_ascii=False, indent=2))

    print("\n=== describe (machines) ===")
    for m in MACHINES:
        print(f"--- describe({m!r}) ---")
        print(json.dumps(api.describe(m), ensure_ascii=False, indent=2))

    rcon.close()
    print("\n=== DONE ===")


if __name__ == "__main__":
    main()