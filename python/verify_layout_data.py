"""Vérification RCON des emprises (size) pour le LayoutPlanner (Factorio 2.0).

Conclusion API 2.0 (doc lua-api.factorio.com) : LuaEntityPrototype (retourné par
prototypes.entity[]) n'expose PAS au runtime les géométries fines :
  - inserter pickup/drop, electric-pole wire/supply, mining-drill mining_area,
    belt_speed -> AUCUN accessible (ni propriété, ni getter via prototypes.entity).
Stratégie : size (emprise) = seule géométrie lisible via RCON -> source de vérité.
Les géométries fines (portées, zones, vitesses) -> HARDCODE Python (valeurs wiki
stables), à valider en S0b par mesure in-game (poser l'entité, lire sa zone réelle).

Lancement (apres restart serveur) : python verify_layout_data.py
"""

from __future__ import annotations

import json
import sys

from core.rcon import get_rcon
from core.mod_api import ModApi

# Entités dont l'emprise (size) est nécessaire au LayoutPlanner.
ENTITIES = [
    "burner-inserter", "inserter", "long-handed-inserter", "fast-inserter",
    "small-electric-pole", "medium-electric-pole", "big-electric-pole", "substation",
    "burner-mining-drill", "electric-mining-drill",
    "transport-belt", "fast-transport-belt", "express-transport-belt",
    "splitter", "underground-belt",  # logistique S1 (emprise utile dès S0)
]

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:40s} {detail[:90]}")


def main() -> int:
    rcon = get_rcon()
    api = ModApi(rcon)
    marker_checked = False
    try:
        print("=== describe (emprises size, géométries fines -> hardcode Python) ===\n")
        for name in ENTITIES:
            d = api.describe(name)
            if not isinstance(d, dict) or "entity" not in d:
                rec(name, False, "describe echoue / pas d'entity")
                continue
            e = d["entity"]
            if not marker_checked:
                marker_checked = True
                mark = e.get("_layoutMark")
                if mark != "tools_v2":
                    print(f"\n!! MOD NON RECHARGE (marker={mark!r}) -> relance le .bat\n")
                    return 1
                print(f"[MARK] mod recharge OK (_layoutMark={mark!r})\n")
            rec(name, "size" in e, f"type={e.get('type')} size={e.get('size')}")
        print("\n--- valeurs hardcode Python (wiki, à valider S0b par mesure) ---")
        print("  belts: 15/30/45 items/s (yellow/red/blue)")
        print("  inserters reach: 1.0 (normal/fast/stack), 2.0 (long-handed)")
        print("  poles wire/supply: small 7.5/2.5, medium 9/3, big 30/2, substation 64/9")
        print("  drills mining area: burner 2x2, electric 5x5")
    finally:
        rcon.close()
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())