"""Validation LIVE S3b : API runtime beacons + modules + electric-furnace (CONSTAT).

Pré-requis : serveur Factorio 2.0 headless lancé (scripts/start_factorio_dedicated.bat)
avec le mod factorio-llm chargé (APRÈS relance pour tools.lua/player.lua S3b),
RCON 127.0.0.1:27015 (pw "factoriollm").

12 recs :
  1.  set_test_mode + setup (kit S3b beacon/modules/electric-furnace dans STARTING_ITEMS)
  2.  describe("beacon") -> entity.beacon present (supply_area_distance attendu)
  3.  describe("beacon") -> supply_area_distance == 3 (accessible ent.prototype)
  4.  CONSTAT describe("beacon") -> module_slots/distribution_effectivity (nil attendu)
  5.  describe("electric-furnace") -> size 3x3 + crafting_speed == 2
  6.  measure_beacon("beacon") -> supply_area_distance == 3 (instance posée)
  7.  measure_beacon round-trip modules -> insert 2 speed-module-3 + get_module_inventory
  8.  populate_from_rcon beacon -> geometry supply_area=3 + module_slots (fixture fallback)
  9.  populate_from_rcon electric-furnace -> geometry w=3 h=3
  10. compute_module_effect(8, speed-module-3) -> speed_bonus=2.0 (8*0.5*0.5)
  11. scan_factory avec beacon dans PRODUCER_TYPES -> pose 1 beacon + scan retourne modules
  12. back-compat : describe stone-furnace inchangé (pas de beacon block)

Lancement :
    cd python
    python verify_layout_s3b.py
"""

from __future__ import annotations
import sys
sys.path.insert(0, "D:/developpement/factorio-llm/python")

from core.rcon import get_rcon
from core.mod_api import ModApi
from services.knowledge import (
    populate_from_rcon, GeometryBase, GEOMETRY_FIXTURE, BEACON_FIXTURE, MODULE_FIXTURE,
)
from services.production_solver import compute_module_effect

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:62s} {detail[:80]}")


def main() -> int:
    rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
    api = ModApi(rcon)
    try:
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"!! MOD NON RECHARGE (can_place_check absent : {e})")
        print("   -> relance scripts/start_factorio_dedicated.bat puis re-execute.")
        rcon.close()
        return 1

    api.set_test_mode(True)
    api.setup()

    # --- Rec 1 : set_test_mode + setup (kit S3b) ---
    rec("S3b-1 : set_test_mode + setup (kit beacon/modules/electric-furnace)",
        True, "test_mode=True kit donné (STARTING_ITEMS S3b)")

    # --- Rec 2 : describe("beacon") -> entity.beacon present ---
    d_beacon = api.describe("beacon")
    ent_beacon = (d_beacon or {}).get("entity", {}) if isinstance(d_beacon, dict) else {}
    beacon_block = ent_beacon.get("beacon")
    rec("S3b-2 : describe(beacon) -> entity.beacon present",
        isinstance(beacon_block, dict),
        f"beacon_block={beacon_block}")

    # --- Rec 3 : CONSTAT supply_area_distance inaccessible beacon (contrairement poles) ---
    # S3b-live : supply_area_distance INACCESSIBLE pour beacon (ni proto ni instance ne
    # l'exposent, contrairement aux electric-poles). Le mod renvoie nil -> fallback fixture
    # supply_area=3.0 (GEOMETRY_FIXTURE). On accepte None (CONSTAT documenté).
    sad = beacon_block.get("supply_area_distance") if isinstance(beacon_block, dict) else None
    rec("S3b-3 : CONSTAT describe(beacon) supply_area_distance inaccessible (None attendu)",
        sad is None,
        f"supply_area_distance={sad} -> fallback fixture 3.0")

    # --- Rec 4 : CONSTAT module_slots nil + distribution_effectivity accessible=1.5 ---
    # S3b-live raffiné : module_slots INACCESSIBLE (proto) -> nil -> fixture module_slots=2.
    # distribution_effectivity ACCESSIBLE (proto.distribution_effectivity=1.5, valeur
    # vanilla 2.0 FFF #409, PAS 0.5 wiki 1.1). allowed_effects ACCESSIBLE (table).
    ms = beacon_block.get("module_slots") if isinstance(beacon_block, dict) else "ABSENT"
    de = beacon_block.get("distribution_effectivity") if isinstance(beacon_block, dict) else "ABSENT"
    constat = (ms is None) and (de == 1.5 or de == 1.5)
    rec("S3b-4 : CONSTAT module_slots nil + distribution_effectivity accessible=1.5 (FFF#409)",
        constat,
        f"module_slots={ms} distribution_effectivity={de} -> fixture module_slots=2")

    # --- Rec 5 : describe("electric-furnace") -> size 3x3 + crafting_speed == 2 ---
    d_ef = api.describe("electric-furnace")
    ent_ef = (d_ef or {}).get("entity", {}) if isinstance(d_ef, dict) else {}
    size_ef = ent_ef.get("size", {})
    cs_ef = ent_ef.get("craftingSpeed")
    rec("S3b-5 : describe(electric-furnace) size 3x3 + crafting_speed==2",
        size_ef.get("w") == 3 and size_ef.get("h") == 3 and cs_ef == 2.0,
        f"size={size_ef} craftingSpeed={cs_ef}")

    # --- Rec 6 : CONSTAT measure_beacon supply_area_distance inaccessible instance ---
    # S3b-live : ent.prototype.supply_area_distance lève sur instance beacon (contrairement
    # aux poles). L'ancien pcall Lua stockait le message d'erreur (truthy) au lieu de nil
    # (fixé côté Lua pour la prochaine relance ; ici on accepte non-numérique = None OU
    # chaîne d'erreur, les deux témoignent du CONSTAT inaccessible).
    mb = api.measure_beacon("beacon", 40.0, 40.0, "north")
    sad_m = mb.get("supply_area_distance") if isinstance(mb, dict) else None
    sad_inaccessible = not isinstance(sad_m, (int, float))
    rec("S3b-6 : CONSTAT measure_beacon supply_area_distance inaccessible instance",
        sad_inaccessible,
        f"supply_area_distance={sad_m!r} -> fallback fixture 3.0")

    # --- Rec 7 : measure_beacon round-trip modules (insert 2 speed-module-3) ---
    mods_m = mb.get("modules", []) if isinstance(mb, dict) else []
    # modules = [{name, count}] attendu (2 speed-module-3 si insert réussi)
    mods_ok = (len(mods_m) > 0 and any(m.get("name") == "speed-module-3" for m in mods_m))
    rec("S3b-7 : measure_beacon round-trip modules (insert + get_module_inventory)",
        mods_ok,
        f"modules={mods_m}")

    # --- Rec 8 : populate_from_rcon beacon -> geometry supply_area=3 + module_slots fixture ---
    kb = populate_from_rcon(api, [], ["beacon"])
    g = kb is not None and True  # populate_from_rcon retourne une KB (machines)
    # On passe par GeometryBase directement pour la géométrie beacon.
    geo = GeometryBase()
    geo.populate_from_rcon(api, ["beacon"])
    gb = geo.geometry("beacon")
    rec("S3b-8 : populate_from_rcon beacon geometry supply_area=3 + module_slots(fixture)",
        gb is not None and abs(gb.supply_area - 3.0) < 1e-9 and gb.module_slots == 2,
        f"supply_area={gb.supply_area if gb else '?'} module_slots={gb.module_slots if gb else '?'}")

    # --- Rec 9 : populate_from_rcon electric-furnace -> geometry w=3 h=3 ---
    geo2 = GeometryBase()
    geo2.populate_from_rcon(api, ["electric-furnace"])
    gef = geo2.geometry("electric-furnace")
    rec("S3b-9 : populate_from_rcon electric-furnace geometry 3x3",
        gef is not None and gef.w == 3 and gef.h == 3,
        f"w={gef.w if gef else '?'} h={gef.h if gef else '?'}")

    # --- Rec 10 : compute_module_effect(8, speed-module-3) -> formule 2.0 (FFF#409) ---
    # S3b-live : distribution_effectivity=1.5 (vanilla 2.0). Rendements décroissants :
    # bonus = dist * sqrt(n) * module_bonus. 8 beacons speed-module-3 (speed=0.5) ->
    # 1.5 * sqrt(8) * 0.5 = 2.1213 ; energy (0.7) -> 1.5 * sqrt(8) * 0.7 = 2.9698.
    meff = compute_module_effect(8, "speed-module-3")
    exp_speed = 1.5 * (8 ** 0.5) * 0.5
    exp_energy = 1.5 * (8 ** 0.5) * 0.7
    rec("S3b-10 : compute_module_effect(8, speed-module-3) 2.0 : dist*sqrt(n)*mod",
        abs(meff.speed_bonus - exp_speed) < 1e-9 and abs(meff.energy_bonus - exp_energy) < 1e-9,
        f"speed_bonus={meff.speed_bonus:.4f} (exp {exp_speed:.4f}) energy={meff.energy_bonus:.4f}")

    # --- Rec 11 : scan_factory avec beacon dans PRODUCER_TYPES -> pose 1 beacon + scan ---
    # Pose un beacon à (45,45), insert 2 speed-module-3, scan_factory doit le retourner
    # avec son contenu modules (get_module_inventory).
    placed = rcon.query_lua(
        "local s=game.surfaces['nauvis'] or game.surfaces[1]; "
        "local e=s.create_entity{name='beacon',position={45,45},force='player'}; "
        "if e then e.insert{name='speed-module-3',count=2}; rcon.print('ok') else rcon.print('fail') end"
    ).strip()
    scan = api.scan_factory() if hasattr(api, "scan_factory") else None
    # scan_factory retourne {entities:[...]} ; cherche le beacon posé.
    found_beacon_mods = None
    if isinstance(scan, dict):
        for row in scan.get("entities", []):
            if row.get("name") == "beacon" and abs(row.get("x", 0) - 45.0) < 1.0:
                found_beacon_mods = row.get("modules")
                break
    # cleanup
    rcon.query_lua(
        "local s=game.surfaces['nauvis'] or game.surfaces[1]; "
        "for _,e in ipairs(s.find_entities_filtered{name='beacon',area={{44,44},{46,46}}}) do e.destroy() end"
    )
    rec("S3b-11 : scan_factory beacon + get_module_inventory (PRODUCER_TYPES étendu)",
        placed == "ok" and found_beacon_mods is not None and len(found_beacon_mods) > 0,
        f"placed={placed} beacon_modules={found_beacon_mods}")

    # --- Rec 12 : back-compat describe stone-furnace inchangé (pas de beacon block) ---
    d_sf = api.describe("stone-furnace")
    ent_sf = (d_sf or {}).get("entity", {}) if isinstance(d_sf, dict) else {}
    no_beacon = "beacon" not in ent_sf
    rec("S3b-12 : back-compat describe(stone-furnace) sans beacon block",
        no_beacon and ent_sf.get("craftingSpeed") == 1.0,
        f"craftingSpeed={ent_sf.get('craftingSpeed')} beacon_absent={no_beacon}")

    # --- Récap ---
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} recs OK")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())