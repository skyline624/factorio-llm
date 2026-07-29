"""Test LIVE E3a : lire l'état ÉLECTRIQUE, et mesurer la géométrie fluide pour E3b.

Le mod savait poser une centrale mais pas vérifier qu'elle alimente : aucun outil ne
distinguait « machine débranchée » de « machine à court de courant », ni ne disait si
le réseau tenait la charge. `get_power_state` comble ce trou — et c'est le préalable à
tout le jalon électricité : sans mesure, on ne saurait pas qu'une centrale ne marche pas.

Ce que le script établit :
  1. la distinction DÉBRANCHÉ / SANS COURANT (`networkId` absent vs `status`) ;
  2. qu'un poteau relie bien deux entités au MÊME réseau (`networkId` partagé) ;
  3. la lecture de charge du réseau (production / consommation / satisfaction) ;
  4. la géométrie fluide RÉELLE de offshore-pump / boiler / steam-engine, mesurée en
     jeu — c'est l'entrée du PowerPlanner (E3b). La leçon du MicroPlanner s'applique :
     une géométrie supposée coûte deux runs, une géométrie mesurée zéro.

Pré-requis : serveur headless lancé avec le mod E3a. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import math
import sys

from core.mod_api import ModApi
from core.rcon import get_rcon

RESULTS: list[tuple[str, bool, str]] = []
GRID_VARIANTS: tuple[tuple[float, float], ...] = ((0.0, 0.0), (0.5, 0.5), (0.5, 0.0), (0.0, 0.5))


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:50s} {detail[:105]}")


def _can(api: ModApi, name: str, x: float, y: float, d: str = "north") -> bool:
    c = api.can_place_check(name, x, y, d)
    return isinstance(c, dict) and c.get("can_place") is True


def _place(api: ModApi, name: str, x: float, y: float, direction: str = "north",
           opts: dict | None = None) -> tuple[float, float] | None:
    """Pose sur la première variante de grille acceptée. Retourne la position réelle."""
    for dx, dy in GRID_VARIANTS:
        px, py = round(x + dx, 2), round(y + dy, 2)
        if not _can(api, name, px, py, direction):
            continue
        res = api.run_action(api.place_entity_at, name, px, py, direction, opts, timeout=20.0)
        if isinstance(res, dict) and res.get("ok"):
            return px, py
    return None


def _clean(rcon, cx: float, cy: float, r: float = 20.0) -> int:
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{cx - r},{cy - r}}},"
        f"{{{cx + r},{cy + r}}}}}}}) do "
        f"if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return -1


def _find_workspace(api: ModApi, rcon) -> tuple[float, float] | None:
    """Zone sèche et dégagée : le terrain n'est pas une constante (cf. E2, tombé dans un lac)."""
    for radius in (0, 30, 60, 90, 130, 180):
        for angle in (range(0, 360, 45) if radius else (0,)):
            cx = float(round(radius * math.cos(math.radians(angle))))
            cy = float(round(radius * math.sin(math.radians(angle))))
            api.generate_terrain(cx, cy, 30.0)
            api.run_action(api.wait, 10, timeout=30.0)
            _clean(rcon, cx, cy)
            spots = ((cx, cy), (cx + 6, cy), (cx - 6, cy), (cx, cy + 6), (cx, cy - 6))
            if all(_can(api, "electric-furnace", sx + 0.5, sy + 0.5) for sx, sy in spots):
                return cx, cy
    return None


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

    zone = _find_workspace(api, rcon)
    if zone is None:
        print("[SKIP] aucune zone sèche et dégagée trouvée.")
        rcon.close()
        return 0
    BX, BY = zone
    api.run_action(api.teleport_to, BX, BY + 12.0, timeout=30.0)
    print(f"       . zone de travail : ({BX},{BY}) — {api.get_tile(BX, BY).get('name')}")

    # --- 1 : rien à cette position -> found=False (et pas une erreur) ---
    vide = api.get_power_state(BX + 40.0, BY + 40.0, 3.0)
    rec("e3-1 : get_power_state sur une zone vide -> found=False",
        isinstance(vide, dict) and vide.get("found") is False,
        f"res={vide}")

    # --- 2 : consommateur ISOLÉ -> le mod doit dire DÉBRANCHÉ, pas « sans courant » ---
    pos = _place(api, "electric-furnace", BX, BY)
    if pos is None:
        rec("e3-2 : machine isolée détectée comme débranchée", False,
            "electric-furnace non posable")
        rcon.close()
        return _verdict()
    fx, fy = pos
    api.run_action(api.wait, 30, timeout=30.0)
    seul = api.get_power_state(fx, fy, 3.0)
    rec("e3-2 : machine isolée -> détectée DÉBRANCHÉE (networkId absent)",
        isinstance(seul, dict) and seul.get("found") is True
        and not seul.get("connected") and seul.get("networkId") is None,
        f"name={seul.get('name')} networkId={seul.get('networkId')} "
        f"connected={seul.get('connected')} status={seul.get('status')}")

    # --- 3 : un poteau à portée -> même réseau (networkId partagé) ---
    ppos = _place(api, "small-electric-pole", BX + 2.0, BY + 2.0)
    api.run_action(api.wait, 30, timeout=30.0)
    avec = api.get_power_state(fx, fy, 3.0)
    pole = api.get_power_state(ppos[0], ppos[1], 1.0) if ppos else {}
    meme_reseau = (isinstance(avec, dict) and avec.get("networkId") is not None
                   and avec.get("networkId") == pole.get("networkId"))
    rec("e3-3 : poteau à portée -> machine et poteau sur le MÊME réseau", meme_reseau,
        f"poteau={ppos} networkId machine={avec.get('networkId')} "
        f"poteau={pole.get('networkId')}")

    # --- 4 : réseau sans production -> la machine reste sans courant ---
    rec("e3-4 : réseau sans production -> pas de courant, et ça se lit",
        isinstance(avec, dict) and (avec.get("productionKW") or 0) == 0
        and avec.get("status") in ("no_power", "low_power", "other"),
        f"status={avec.get('status')} productionKW={avec.get('productionKW')} "
        f"consumptionKW={avec.get('consumptionKW')} satisfaction={avec.get('satisfaction')}")

    # --- 5 : géométrie fluide RÉELLE de la chaîne vapeur (entrée du PowerPlanner E3b) ---
    #
    # Le contrôle porte sur les POSITIONS des ports, pas sur leur simple existence :
    # savoir qu'un boiler a deux entrées d'eau ne dit pas où poser le tuyau. Un premier
    # jet lisait `pipe_connections[].x`, absent (le prototype expose `positions[]`, non
    # rotées) — il passait au vert en ne mesurant rien. On lit `ports[]`, positions
    # réelles relatives au centre de l'entité posée.
    print("\n       --- géométrie mesurée pour E3b (PowerPlanner) ---")
    geo_ok = True
    for name in ("offshore-pump", "boiler", "steam-engine"):
        m = api.measure_entity(name, BX + 30.0, BY + 30.0, "north")
        size = m.get("size") if isinstance(m, dict) else None
        boxes = m.get("fluid_boxes", []) if isinstance(m, dict) else []
        lus = 0
        for fb in boxes:
            ports = [(p.get("x"), p.get("y"), p.get("tx"), p.get("ty"))
                     for p in fb.get("ports", [])]
            lus += len(ports)
            print(f"       . {name:16s} size={size} [{fb.get('production_type')}] "
                  f"ports(port->voisin)={ports}")
        if not size or lus == 0:
            geo_ok = False
            print(f"       . {name:16s} AUCUN port lisible (size={size})")
    rec("e3-5 : POSITIONS des ports fluides lisibles (pas juste leur nombre)", geo_ok,
        "offshore-pump / boiler / steam-engine : port et tuile voisine, relatifs au centre")

    # --- 6 : l'eau nécessaire à la centrale est localisable ---
    we = api.scan_water_edge(200.0)
    n_water = we.get("count", 0) if isinstance(we, dict) else 0
    rec("e3-6 : bord d'eau localisable pour l'offshore-pump", n_water > 0,
        f"{n_water} tuile(s) de bord d'eau, bbox={we.get('bbox') if isinstance(we, dict) else None}")

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