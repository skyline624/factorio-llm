"""Tests unitaires des wrappers ModApi S4a (scan_obstacles/scan_tiles_bbox/get_tile) +
S4d (generate_terrain).

Aucun serveur requis : un StubRconClient retourne du JSON canned selon la méthode
`remote.call("fl_tools", <method>, ...)` émise par ModApi._call. Valide le parsing
côté Python (obstacles[].bbox floored, count cohérent, get_tile name, idempotence
non-destructive, generate_terrain retourne generated==total).

Lancement :
    cd python
    python -m tests.test_mod_api
"""

from __future__ import annotations

import re
import sys
import json

sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.mod_api import ModApi

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:100]}")


rec = record


# ===== Stub RCON =====

# Réponses canned par méthode fl_tools.
CANNED = {
    "scan_obstacles": {
        "obstacles": [
            {"x": 5, "y": 5, "w": 1, "h": 1, "name": "rock-big", "type": "simple-entity"},
            {"x": -3, "y": 10, "w": 2, "h": 2, "name": "tree-01", "type": "tree"},
        ],
        "bbox": {"x1": -3, "y1": 5, "x2": 7, "y2": 12},
        "count": 2,
        "origin": {"x": 0.0, "y": 0.0},
    },
    "scan_tiles_bbox": {
        "tiles": [
            {"x": 0, "y": 0, "name": "grass-1"},
            {"x": 1, "y": 0, "name": "grass-1"},
            {"x": 0, "y": 1, "name": "water"},
            {"x": 1, "y": 1, "name": "water"},
        ],
        "bbox": {"x1": 0, "y1": 0, "x2": 2, "y2": 2},
        "count": 4,
    },
    "get_tile": {"x": 0, "y": 0, "name": "grass-1"},
    "generate_terrain": {
        "x": 100, "y": -50, "radius_chunks": 2, "generated": 25, "total": 25,
    },
}


class StubRconClient:
    """RconClient stub : parse `remote.call("iface","method",...)` et retourne
    le JSON canned pour la méthode. Capture le dernier lua émis pour asserts."""

    def __init__(self):
        self.last_lua: str = ""
        self.calls: list[str] = []

    def query_lua(self, lua_code: str, silent: bool = True) -> str:
        self.last_lua = lua_code
        self.calls.append(lua_code)
        # extraire la méthode : remote.call("fl_tools", "method", ...)
        m = re.search(r'remote\.call\(\s*"(?P<iface>[^"]+)"\s*,\s*"(?P<method>[^"]+)"', lua_code)
        if not m:
            return ""
        method = m.group("method")
        if method in CANNED:
            return json.dumps(CANNED[method])
        return ""

    def query(self, cmd: str) -> str:
        return self.query_lua(cmd.replace("/silent-command ", "", 1))

    def close(self) -> None:
        pass


# ===== Tests =====


def test_scan_obstacles_parse() -> None:
    """scan_obstacles parse obstacles[].bbox (x,y,w,h floored) + count + origin."""
    stub = StubRconClient()
    api = ModApi(stub)
    r = api.scan_obstacles()
    ok = (isinstance(r, dict)
          and r.get("count") == 2
          and len(r.get("obstacles", [])) == 2
          and r["obstacles"][0]["w"] == 1 and r["obstacles"][1]["w"] == 2
          and r["bbox"]["x1"] == -3 and r["bbox"]["y2"] == 12
          and r["origin"]["x"] == 0.0)
    rec("S4a-1 : scan_obstacles parse obstacles[].bbox + count + origin",
        ok, f"count={r.get('count')} n={len(r.get('obstacles', []))} bbox={r.get('bbox')}")


def test_scan_obstacles_radius_arg() -> None:
    """scan_obstacles(radius=400.0) passe le rayon au lua (arg présent)."""
    stub = StubRconClient()
    api = ModApi(stub)
    api.scan_obstacles(400.0)
    ok = "scan_obstacles" in stub.last_lua and "400.0" in stub.last_lua
    rec("S4a-2 : scan_obstacles(radius) passe le rayon au lua",
        ok, f"lua={stub.last_lua[:80]}")


def test_get_tile_name() -> None:
    """get_tile(x,y) retourne {x,y,name}."""
    stub = StubRconClient()
    api = ModApi(stub)
    r = api.get_tile(0, 0)
    ok = isinstance(r, dict) and r.get("name") == "grass-1" and r.get("x") == 0 and r.get("y") == 0
    rec("S4a-3 : get_tile retourne {x,y,name}",
        ok, f"name={r.get('name') if isinstance(r, dict) else r}")


def test_scan_tiles_bbox_count_coherent() -> None:
    """scan_tiles_bbox : count == len(tiles), bbox cohérente."""
    stub = StubRconClient()
    api = ModApi(stub)
    r = api.scan_tiles_bbox(0, 0, 2, 2)
    ok = (isinstance(r, dict)
          and r.get("count") == len(r.get("tiles", []))
          and r.get("count") == 4
          and r["bbox"]["x2"] == 2 and r["bbox"]["y2"] == 2)
    rec("S4a-4 : scan_tiles_bbox count == len(tiles) + bbox cohérente",
        ok, f"count={r.get('count')} n_tiles={len(r.get('tiles', []))}")


def test_scan_tiles_bbox_args_passed() -> None:
    """scan_tiles_bbox(x1,y1,x2,y2) passe les 4 coords au lua."""
    stub = StubRconClient()
    api = ModApi(stub)
    api.scan_tiles_bbox(-5, -5, 10, 10)
    ok = "scan_tiles_bbox" in stub.last_lua and "-5" in stub.last_lua and "10" in stub.last_lua
    rec("S4a-5 : scan_tiles_bbox passe les 4 coords au lua",
        ok, f"lua={stub.last_lua[:90]}")


def test_non_destructif_idempotent() -> None:
    """Re-scan identique (non-destructif) : 2 appels scan_obstacles -> mêmes données,
    stub ne reçoit que des remote.call (pas d'effet de bord attendu côté Python)."""
    stub = StubRconClient()
    api = ModApi(stub)
    r1 = api.scan_obstacles()
    r2 = api.scan_obstacles()
    ok = r1 == r2 and len(stub.calls) == 2 and all("scan_obstacles" in c for c in stub.calls)
    rec("S4a-6 : re-scan identique (non-destructif, idempotent)",
        ok, f"r1==r2={r1 == r2} n_calls={len(stub.calls)}")


def test_generate_terrain_parse() -> None:
    """generate_terrain(x, y, radius) retourne {x, y, radius_chunks, generated, total}
    avec generated == total (tous les chunks demandés générés)."""
    stub = StubRconClient()
    api = ModApi(stub)
    r = api.generate_terrain(100, -50, 30.0)
    ok = (isinstance(r, dict)
          and r.get("x") == 100 and r.get("y") == -50
          and r.get("radius_chunks") == 2
          and r.get("generated") == r.get("total") == 25)
    rec("S4d-1 : generate_terrain parse {x,y,radius_chunks,generated,total}",
        ok, f"r={r}")


def test_generate_terrain_args_passed() -> None:
    """generate_terrain(x, y, radius) passe les 3 args au lua (x, y, radius présents)."""
    stub = StubRconClient()
    api = ModApi(stub)
    api.generate_terrain(100, -50, 60.0)
    ok = ("generate_terrain" in stub.last_lua
          and "100" in stub.last_lua and "-50" in stub.last_lua and "60.0" in stub.last_lua)
    rec("S4d-2 : generate_terrain passe (x, y, radius) au lua",
        ok, f"lua={stub.last_lua[:90]}")


def test_generate_terrain_default_radius() -> None:
    """generate_terrain(x, y) sans radius -> defaut 30.0 transmis au lua."""
    stub = StubRconClient()
    api = ModApi(stub)
    api.generate_terrain(200, 300)
    ok = "generate_terrain" in stub.last_lua and "30.0" in stub.last_lua
    rec("S4d-3 : generate_terrain defaut radius=30.0 transmis",
        ok, f"lua={stub.last_lua[:90]}")


def main() -> int:
    tests = [
        test_scan_obstacles_parse,
        test_scan_obstacles_radius_arg,
        test_get_tile_name,
        test_scan_tiles_bbox_count_coherent,
        test_scan_tiles_bbox_args_passed,
        test_non_destructif_idempotent,
        test_generate_terrain_parse,
        test_generate_terrain_args_passed,
        test_generate_terrain_default_radius,
    ]
    for t in tests:
        t()
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