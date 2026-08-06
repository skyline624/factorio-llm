"""Tests unitaires de l'Executor E1 (services/executor.py).

Aucun serveur, aucun RCON, aucun LLM : un `FakeApi` scriptable remplace `ModApi` et
journalise tous les appels. On vérifie que l'executor respecte les règles apprises en
live plutôt que le détail des positions (calculées et déjà testées par le MicroPlanner).

Vérifications :
  - pré-vol inventaire : suffisant -> pose ; insuffisant -> 0 pose + `missing` renseigné
    (le mod ne vérifie PAS l'inventaire : create_entity puis inv.remove).
  - retry borné sur collision : offset appliqué et consigné ; retries épuisés -> `blocked`
    + arrêt (pas de pose des entités suivantes).
  - ordre de pose = tri topologique du flux (connections), pas l'ordre de la liste.
  - direction int -> nom ("south"), défaut "north" hors 0/2/4/6.
  - combustible sur TOUS les burners, y compris le burner-inserter (il ne s'auto-alimente
    que s'il manipule du charbon ; ici il manipule du minerai).
  - dry_run : aucune pose ni alimentation. skip=True filtré. plan vide -> ok.
  - generate/approach coupés -> ni generate_terrain ni walk_to.

Chaque test fait un `rec()` (runner du projet) ET un `assert` (verdict pytest réel).

Lancement :
    cd python
    python -m tests.test_executor
"""

from __future__ import annotations

import sys

from services.executor import (
    DIR_TO_STR, ExecutionReport, RETRY_OFFSETS, execute_micro, is_burner,
)
from services.layout_planner import LayoutEntity, ResourcePatch
from services.micro_planner import MicroPlan, MicroRequest, plan_micro

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:48s} {detail[:100]}")


rec = record


# ===== FakeApi : ModApi scriptable, sans RCON =====

class FakeApi:
    """Double de `ModApi` : inventaire fixe, `can_place_check` scriptable, journal.

    `refuse` = ensemble de positions (x, y) arrondies refusées par can_place_check ;
    `refuse_all_for` = noms d'entités refusées partout (retries épuisés).
    `fail_place` = noms dont place_entity_at échoue malgré un can_place positif
    (course entre entités du même plan — can_place_check est indépendant par entité).
    """

    def __init__(self, inventory: dict[str, int] | None = None,
                 refuse: set[tuple[float, float]] | None = None,
                 refuse_all_for: set[str] | None = None,
                 fail_place: set[str] | None = None,
                 patch_bbox: dict | None = None,
                 ghost_place: set[str] | None = None):
        self.inv = dict(inventory or {})
        self.refuse = refuse or set()
        self.refuse_all_for = refuse_all_for or set()
        self.fail_place = fail_place or set()
        # Noms dont la pose repond ok=True SANS rien poser (pose fantome constatee en
        # live E1) : l'item ne quitte pas l'inventaire.
        self.ghost_place = ghost_place or set()
        # None = scan_patch ne trouve aucun gisement (cas d'échec de build_micro_layout).
        self.patch_bbox = patch_bbox
        self.calls: list[tuple] = []          # journal ordonné (méthode, *args)

    # --- fl_tools ---
    def get_state(self) -> dict:
        return {"tick": 1, "ready": True, "test_mode": True,
                "character": {"position": {"x": 0.0, "y": 0.0}},
                "inventory": dict(self.inv), "task": {}}

    def can_place_check(self, name: str, x: float, y: float, direction: str = "north") -> dict:
        self.calls.append(("can_place_check", name, x, y, direction))
        if name in self.refuse_all_for or (round(x, 2), round(y, 2)) in self.refuse:
            return {"name": name, "x": x, "y": y, "can_place": False}
        return {"name": name, "x": x, "y": y, "can_place": True}

    def scan_patch(self, resource: str, radius: float = 400.0) -> dict:
        self.calls.append(("scan_patch", resource, radius))
        if self.patch_bbox is None:
            return {"resource": resource, "count": 0}
        # `sample` = tuiles de minerai REELLES (12 max, tools.lua). C'est la seule
        # donnee sur laquelle build_micro_layout a le droit d'ancrer le drill : le
        # centre du bbox ne garantit rien (bbox agrege sur plusieurs gisements).
        bb = self.patch_bbox
        sample = [{"x": x, "y": y}
                  for y in range(int(bb["y1"]), int(bb["y2"]) + 1)
                  for x in range(int(bb["x1"]), int(bb["x2"]) + 1)][:12]
        return {"resource": resource, "count": 42, "bbox": dict(bb), "sample": sample}

    def generate_terrain(self, x: float, y: float, radius: float = 30.0) -> dict:
        self.calls.append(("generate_terrain", x, y, radius))
        return {"x": x, "y": y, "radius_chunks": 2, "generated": 25, "total": 25}

    # --- fl_ops ---
    def walk_to(self, x: float, y: float) -> dict:
        self.calls.append(("walk_to", x, y))
        return {"ok": True, "detail": f"walk {x},{y}"}

    def place_entity_at(self, name: str, x: float, y: float, direction: str = "north",
                        opts: dict | None = None) -> dict:
        self.calls.append(("place_entity_at", name, x, y, direction, opts))
        if name in self.fail_place:
            return {"ok": False, "detail": "cannot place here"}
        # Le mod retire l'item de l'inventaire APRÈS create_entity (task_manager.lua) :
        # le double doit le faire aussi, sinon la vérification anti pose fantôme
        # (verify=True) rejette toutes les poses.
        if name not in self.ghost_place:
            self.inv[name] = self.inv.get(name, 0) - 1
        return {"ok": True, "detail": f"place {name} at ({x}, {y})"}

    def move_items_at(self, item: str, name: str, x: float, y: float,
                      max_count: int = 0, to_entity: bool = True) -> dict:
        self.calls.append(("move_items_at", item, name, x, y, max_count, to_entity))
        return {"ok": True, "detail": f"move {item} -> {name}"}

    # --- helper d'attente (cycle race-free côté réel) ---
    def run_action(self, action, *args, timeout: float = 30.0, poll_interval: float = 0.25):
        return action(*args)

    # --- utilitaires de test ---
    def named(self, method: str) -> list[tuple]:
        return [c for c in self.calls if c[0] == method]


# ===== Fixtures de plans =====

FULL_KIT = {"burner-mining-drill": 4, "burner-inserter": 10,
            "stone-furnace": 4, "coal": 100}


def _micro(facing: int = 4) -> MicroPlan:
    """MicroPlan réel (pas un mock) : drill(0,0) -> inserter -> furnace, facing south."""
    patch = ResourcePatch(resource="iron-ore", tiles=[(0, 0)], bbox=(0, 0, 0, 0))
    return plan_micro(MicroRequest(patch=patch, facing=facing))


# ===== Tests =====

def test_executor_pose_les_3_entites() -> None:
    api = FakeApi(FULL_KIT)
    rep = execute_micro(api, _micro(), generate=False, approach=False)
    names = [p.name for p in rep.placed]
    ok = (rep.ok and len(rep.placed) == 3 and not rep.blocked and not rep.missing
          and names == ["burner-mining-drill", "burner-inserter", "stone-furnace"])
    rec("test_executor_pose_les_3_entites", ok, f"ok={rep.ok} placed={names}")
    assert ok, f"placed={names} blocked={rep.blocked} missing={rep.missing}"


def test_executor_inventaire_insuffisant_ne_pose_rien() -> None:
    # Le kit sans drill : le mod poserait quand même (create_entity puis inv.remove),
    # d'où le pré-vol côté Python.
    api = FakeApi({"burner-inserter": 10, "stone-furnace": 4, "coal": 100})
    rep = execute_micro(api, _micro(), generate=False, approach=False)
    ok = (not rep.ok and rep.missing.get("burner-mining-drill") == 1
          and not rep.placed and not api.named("place_entity_at"))
    rec("test_executor_inventaire_insuffisant_ne_pose_rien", ok,
        f"missing={rep.missing} poses={len(api.named('place_entity_at'))}")
    assert ok, f"missing={rep.missing} calls={api.named('place_entity_at')}"


def test_executor_inventaire_compte_le_combustible() -> None:
    # 3 burners x 5 coal = 15 requis ; on n'en a que 10.
    api = FakeApi({"burner-mining-drill": 4, "burner-inserter": 10,
                   "stone-furnace": 4, "coal": 10})
    rep = execute_micro(api, _micro(), generate=False, approach=False, fuel_count=5)
    ok = not rep.ok and rep.missing.get("coal") == 5 and not rep.placed
    rec("test_executor_inventaire_compte_le_combustible", ok, f"missing={rep.missing}")
    assert ok, f"missing={rep.missing}"


def test_executor_retry_offset_sur_collision() -> None:
    mp = _micro()
    drill = mp.entities[0]
    api = FakeApi(FULL_KIT, refuse={(round(drill.x, 2), round(drill.y, 2))})
    rep = execute_micro(api, mp, generate=False, approach=False)
    p0 = rep.placed[0] if rep.placed else None
    ok = (rep.ok and p0 is not None and p0.offset != (0.0, 0.0)
          and (p0.x, p0.y) != (drill.x, drill.y) and len(rep.placed) == 3)
    rec("test_executor_retry_offset_sur_collision", ok,
        f"plan=({drill.x},{drill.y}) pose=({p0.x if p0 else '-'},{p0.y if p0 else '-'}) "
        f"offset={p0.offset if p0 else '-'}")
    assert ok, f"placed={rep.placed} blocked={rep.blocked}"


def test_executor_decalage_solidaire() -> None:
    """Le plan entier est translaté du MÊME offset : la géométrie de la chaîne survit.

    Régression du run live E1 : drill décalé de +1y, inserter de +1x -> 3 entités
    posées, ok=True, et zéro production (le drop du drill ne tombait plus sur l'inserter).
    """
    mp = _micro()
    drill = mp.entities[0]
    api = FakeApi(FULL_KIT, refuse={(round(drill.x, 2), round(drill.y, 2))})
    rep = execute_micro(api, mp, generate=False, approach=False)
    offsets = {p.offset for p in rep.placed}
    off = next(iter(offsets)) if len(offsets) == 1 else None
    ok = (rep.ok and len(rep.placed) == 3 and off is not None and off != (0.0, 0.0)
          and all((p.x, p.y) == (round(e.x + off[0], 2), round(e.y + off[1], 2))
                  for p, e in zip(rep.placed, [mp.entities[p.idx] for p in rep.placed])))
    rec("test_executor_decalage_solidaire", ok,
        f"offsets={offsets} positions={[(p.name, p.x, p.y) for p in rep.placed]}")
    assert ok, f"offsets={offsets} placed={rep.placed}"


def test_executor_verifie_toutes_les_positions_avant_de_poser() -> None:
    """Aucune pose tant qu'un offset valable pour TOUT le plan n'est pas trouvé.

    Corollaire du solidaire : si le four est bloqué, le drill ne doit pas être posé
    « en attendant » — sinon on laisse un chantier orphelin à démolir.
    """
    mp = _micro()
    api = FakeApi(FULL_KIT, refuse_all_for={"stone-furnace"})
    rep = execute_micro(api, mp, generate=False, approach=False)
    ok = (not rep.ok and not rep.placed and not api.named("place_entity_at")
          and rep.blocked and rep.blocked[0][1] == "stone-furnace")
    rec("test_executor_verifie_toutes_les_positions_avant_de_poser", ok,
        f"placed={len(rep.placed)} poses={len(api.named('place_entity_at'))} blocked={rep.blocked}")
    assert ok, f"placed={rep.placed} blocked={rep.blocked}"


def test_executor_pose_fantome_rejetee() -> None:
    """`ok=True` du mod mais l'item n'a pas quitté l'inventaire -> pose refusée.

    Constaté en live E1 : rapport « drill + inserter posés », un seul sur la carte,
    inventaire de drills intact (4 = kit complet).
    """
    mp = _micro()
    api = FakeApi(FULL_KIT, ghost_place={"burner-mining-drill"})
    rep = execute_micro(api, mp, generate=False, approach=False)
    ok = (not rep.ok and not rep.placed and rep.blocked
          and "inventaire" in rep.blocked[0][4])
    rec("test_executor_pose_fantome_rejetee", ok, f"blocked={rep.blocked}")
    assert ok, f"placed={rep.placed} blocked={rep.blocked}"


def test_executor_verify_desactivable() -> None:
    """verify=False : on croit le mod sur parole (1 aller-retour RCON de moins)."""
    mp = _micro()
    api = FakeApi(FULL_KIT, ghost_place={"burner-mining-drill"})
    rep = execute_micro(api, mp, generate=False, approach=False, verify=False)
    ok = rep.ok and len(rep.placed) == 3
    rec("test_executor_verify_desactivable", ok, f"ok={rep.ok} placed={len(rep.placed)}")
    assert ok, f"ok={rep.ok} blocked={rep.blocked}"


def test_executor_retries_epuises_bloque_et_arrete() -> None:
    api = FakeApi(FULL_KIT, refuse_all_for={"burner-mining-drill"})
    rep = execute_micro(api, _micro(), generate=False, approach=False)
    ok = (not rep.ok and len(rep.blocked) == 1
          and rep.blocked[0][1] == "burner-mining-drill"
          and not rep.placed and not api.named("place_entity_at"))
    rec("test_executor_retries_epuises_bloque_et_arrete", ok,
        f"blocked={rep.blocked} placed={len(rep.placed)}")
    assert ok, f"blocked={rep.blocked} placed={rep.placed}"
    # Le nombre de can_place_check = 1 position d'origine + tous les offsets.
    n_chk = len(api.named("can_place_check"))
    ok_n = n_chk == 1 + len(RETRY_OFFSETS)
    rec("test_executor_retry_borne", ok_n, f"can_place_check appele {n_chk}x")
    assert ok_n, f"n_chk={n_chk} attendu={1 + len(RETRY_OFFSETS)}"


def test_executor_echec_pose_malgre_can_place() -> None:
    # can_place dit oui, create_entity échoue : l'executor essaie les offsets suivants
    # puis déclare bloqué (course entre entités du même plan).
    api = FakeApi(FULL_KIT, fail_place={"stone-furnace"})
    rep = execute_micro(api, _micro(), generate=False, approach=False)
    ok = (not rep.ok and len(rep.placed) == 2
          and rep.blocked and rep.blocked[0][1] == "stone-furnace")
    rec("test_executor_echec_pose_malgre_can_place", ok,
        f"placed={[p.name for p in rep.placed]} blocked={rep.blocked}")
    assert ok, f"placed={rep.placed} blocked={rep.blocked}"


def test_executor_ordre_topologique() -> None:
    """Les connections dictent l'ordre de pose, pas l'ordre de la liste."""
    mp = _micro()
    # Liste inversée (furnace, inserter, drill) + connections réindexées : le flux
    # reste drill -> inserter -> furnace.
    mp.entities = list(reversed(mp.entities))          # idx 0=furnace, 1=inserter, 2=drill
    mp.connections = [(2, 1, "iron-ore"), (1, 0, "iron-ore")]
    api = FakeApi(FULL_KIT)
    rep = execute_micro(api, mp, generate=False, approach=False)
    order = [c[1] for c in api.named("place_entity_at")]
    ok = order == ["burner-mining-drill", "burner-inserter", "stone-furnace"]
    rec("test_executor_ordre_topologique", ok, f"ordre={order}")
    assert ok, f"ordre={order}"


def test_executor_direction_int_vers_nom() -> None:
    mp = _micro(facing=4)                               # drill facing south
    api = FakeApi(FULL_KIT)
    execute_micro(api, mp, generate=False, approach=False)
    dirs = {c[1]: c[4] for c in api.named("place_entity_at")}
    ok = dirs.get("burner-mining-drill") == "south" and DIR_TO_STR[4] == "south"
    rec("test_executor_direction_int_vers_nom", ok, f"dirs={dirs}")
    assert ok, f"dirs={dirs}"

    # Direction hors 0/2/4/6 -> repli "north" (le mod n'accepte que les 4 noms).
    mp2 = _micro()
    mp2.entities[0].direction = 3
    api2 = FakeApi(FULL_KIT)
    execute_micro(api2, mp2, generate=False, approach=False)
    d0 = api2.named("place_entity_at")[0][4]
    ok2 = d0 == "north"
    rec("test_executor_direction_inconnue_defaut_north", ok2, f"direction={d0}")
    assert ok2, f"direction={d0}"


def test_executor_alimente_tous_les_burners() -> None:
    api = FakeApi(FULL_KIT)
    rep = execute_micro(api, _micro(), generate=False, approach=False, fuel_count=5)
    fueled = {c[2] for c in api.named("move_items_at")}
    ok = (fueled == {"burner-mining-drill", "burner-inserter", "stone-furnace"}
          and all(c[1] == "coal" and c[5] == 5 and c[6] is True
                  for c in api.named("move_items_at")))
    rec("test_executor_alimente_tous_les_burners", ok,
        f"fueled={sorted(fueled)} report={rep.fueled}")
    assert ok, f"fueled={fueled} calls={api.named('move_items_at')}"


def test_executor_is_burner_discrimine_electrique() -> None:
    ok = (is_burner("burner-mining-drill") and is_burner("burner-inserter")
          and is_burner("stone-furnace") and is_burner("steel-furnace")
          and not is_burner("electric-mining-drill") and not is_burner("fast-inserter")
          and not is_burner("electric-furnace") and not is_burner("transport-belt"))
    rec("test_executor_is_burner_discrimine_electrique", ok,
        "burner-* + stone/steel-furnace = True ; tiers électriques = False")
    assert ok


def test_executor_fuel_count_zero_desactive() -> None:
    api = FakeApi({"burner-mining-drill": 4, "burner-inserter": 10, "stone-furnace": 4})
    rep = execute_micro(api, _micro(), generate=False, approach=False, fuel_count=0)
    ok = rep.ok and not api.named("move_items_at") and not rep.fueled
    rec("test_executor_fuel_count_zero_desactive", ok,
        f"ok={rep.ok} move_items_at={len(api.named('move_items_at'))} (coal absent du kit)")
    assert ok, f"ok={rep.ok} fueled={rep.fueled}"


def test_executor_dry_run_ne_pose_rien() -> None:
    api = FakeApi(FULL_KIT)
    rep = execute_micro(api, _micro(), generate=False, approach=False, dry_run=True)
    ok = (rep.ok and len(rep.placed) == 3
          and not api.named("place_entity_at") and not api.named("move_items_at")
          and api.named("can_place_check"))
    rec("test_executor_dry_run_ne_pose_rien", ok,
        f"placed={len(rep.placed)} poses={len(api.named('place_entity_at'))} "
        f"checks={len(api.named('can_place_check'))}")
    assert ok, f"calls={api.calls}"


def test_executor_skip_filtre() -> None:
    mp = _micro()
    mp.entities.append(LayoutEntity("transport-belt", 9.0, 9.0, 2, "belt"))
    mp.entities[-1].skip = True
    api = FakeApi(FULL_KIT)
    rep = execute_micro(api, mp, generate=False, approach=False)
    posed = [c[1] for c in api.named("place_entity_at")]
    ok = rep.ok and "transport-belt" not in posed and len(rep.placed) == 3
    rec("test_executor_skip_filtre", ok, f"posed={posed} notes={rep.notes}")
    assert ok, f"posed={posed}"


def test_place_opts_depuis_layout_entity() -> None:
    """E2 : les champs que le LayoutPlanner calcule deviennent des options de pose.

    `ug_type` et `priority` (S1) et `modules` (S3) étaient calculés depuis longtemps
    mais impossibles à poser : `place_entity_at` ne transmettait que name/x/y/direction.
    La liste de modules du planner (avec répétition) devient {nom: nombre}.
    """
    from services.executor import _place_opts
    nu = LayoutEntity("transport-belt", 0.0, 0.0, 0, "belt")
    ug = LayoutEntity("underground-belt", 0.0, 0.0, 0, "under-in")
    ug.ug_type = "input"
    sp = LayoutEntity("splitter", 0.0, 0.0, 0, "splitter")
    sp.priority = "left"
    bc = LayoutEntity("beacon", 0.0, 0.0, 0, "beacon")
    bc.modules = ["speed-module-3", "speed-module-3"]
    ok = (_place_opts(nu) is None                                   # aucune option -> None
          and _place_opts(ug) == {"ug_type": "input"}
          and _place_opts(sp) == {"priority_out": "left"}           # priority = SORTIE
          and _place_opts(bc) == {"modules": {"speed-module-3": 2}})
    rec("test_place_opts_depuis_layout_entity", ok,
        f"nu={_place_opts(nu)} ug={_place_opts(ug)} sp={_place_opts(sp)} bc={_place_opts(bc)}")
    assert ok


def test_executor_transmet_les_options_de_pose() -> None:
    """E2 : l'executor passe bien les options au mod (et None quand il n'y en a pas)."""
    mp = _micro()
    mp.entities[2].modules = ["speed-module-3"]          # le four porte un module
    api = FakeApi(FULL_KIT)
    execute_micro(api, mp, generate=False, approach=False)
    poses = {c[1]: c[5] for c in api.named("place_entity_at")}      # nom -> opts
    ok = (poses.get("stone-furnace") == {"modules": {"speed-module-3": 1}}
          and poses.get("burner-mining-drill") is None)
    rec("test_executor_transmet_les_options_de_pose", ok, f"opts={poses}")
    assert ok


def test_executor_plan_vide_et_infaisable() -> None:
    api = FakeApi(FULL_KIT)
    rep_vide = execute_micro(api, MicroPlan(), generate=False, approach=False)
    ok_vide = rep_vide.ok and not rep_vide.placed and not api.calls
    rec("test_executor_plan_vide", ok_vide, f"ok={rep_vide.ok} calls={len(api.calls)}")
    assert ok_vide, f"rep={rep_vide}"

    api2 = FakeApi(FULL_KIT)
    rep_ko = execute_micro(api2, MicroPlan(feasibility="missing_geometry"),
                           generate=False, approach=False)
    ok_ko = not rep_ko.ok and not api2.calls and rep_ko.notes
    rec("test_executor_plan_infaisable", ok_ko,
        f"ok={rep_ko.ok} notes={rep_ko.notes}")
    assert ok_ko, f"rep={rep_ko}"


def test_executor_approche_generate_et_walk() -> None:
    api = FakeApi(FULL_KIT)
    execute_micro(api, _micro(), generate=True, approach=True)
    # Deux marches : approcher du chantier, puis en SORTIR. La seconde est ce qui
    # débloque les poses — l'avatar qui se tient sur une tuile la rend inconstructible
    # en mode `manual`, sur du terrain pourtant vide.
    ok = len(api.named("generate_terrain")) == 1 and len(api.named("walk_to")) == 2
    rec("test_executor_approche_generate_et_walk", ok,
        f"generate={len(api.named('generate_terrain'))} walk={len(api.named('walk_to'))}")
    assert ok, f"calls={api.calls[:4]}"

    # generate_terrain AVANT walk_to (sans chunks générés le pathfinding ne planifie pas).
    seq = [c[0] for c in api.calls if c[0] in ("generate_terrain", "walk_to")]
    ok_ordre = seq[:2] == ["generate_terrain", "walk_to"]
    rec("test_executor_generate_avant_walk", ok_ordre, f"sequence={seq}")
    assert ok_ordre, f"sequence={seq}"

    # Le dégagement sort RÉELLEMENT de l'emprise du plan, sinon il ne sert à rien.
    sortie = api.named("walk_to")[-1]
    xs = [e.x for e in _micro().entities]
    ys = [e.y for e in _micro().entities]
    dehors = not (min(xs) - 1.5 <= sortie[1] <= max(xs) + 1.5
                  and min(ys) - 1.5 <= sortie[2] <= max(ys) + 1.5)
    rec("test_executor_degagement_sort_de_l_emprise", dehors,
        f"sortie=({sortie[1]},{sortie[2]}) hors de x{[min(xs), max(xs)]} y{[min(ys), max(ys)]}")
    assert dehors, f"sortie={sortie}"

    # `approach=False` supprime l'APPROCHE, pas le DÉGAGEMENT : l'appelant qui gère
    # lui-même ses déplacements ne renonce pas pour autant à pouvoir poser. Le seul
    # `walk_to` restant doit donc s'éloigner du plan, jamais s'en rapprocher.
    api2 = FakeApi(FULL_KIT)
    execute_micro(api2, _micro(), generate=False, approach=False)
    marches = api2.named("walk_to")
    xs2 = [e.x for e in _micro().entities]
    ys2 = [e.y for e in _micro().entities]
    ok2 = not api2.named("generate_terrain") and all(
        not (min(xs2) - 1.5 <= m[1] <= max(xs2) + 1.5
             and min(ys2) - 1.5 <= m[2] <= max(ys2) + 1.5) for m in marches)
    rec("test_executor_approche_desactivable", ok2,
        f"generate=0, {len(marches)} marche(s) toutes sortantes")
    assert ok2, f"calls={api2.calls[:4]}"


def test_executor_report_journalise() -> None:
    api = FakeApi(FULL_KIT)
    rep = execute_micro(api, _micro(), generate=False, approach=False)
    ok = (isinstance(rep, ExecutionReport) and len(rep.steps) >= 4
          and any("pre-vol" in s for s in rep.steps)
          and any(s.startswith("pose ") for s in rep.steps)
          and any(s.startswith("fuel ") for s in rep.steps))
    rec("test_executor_report_journalise", ok, f"{len(rep.steps)} etapes journalisees")
    assert ok, f"steps={rep.steps}"


def test_factory_builder_run_micro_layout() -> None:
    """Boucle fermée agent → jeu : FactoryBuilder calcule le plan PUIS le bâtit."""
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal

    api = FakeApi(FULL_KIT, patch_bbox={"x1": -4, "y1": -4, "x2": 4, "y2": 4})
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 10)))
    plan, rep = fb.run_micro_layout("iron-ore", generate=False, approach=False)
    ok = (plan.feasibility == "ok" and len(plan.entities) == 3
          and rep.ok and len(rep.placed) == 3
          and len(api.named("place_entity_at")) == 3)
    rec("test_factory_builder_run_micro_layout", ok,
        f"feas={plan.feasibility} placed={[p.name for p in rep.placed]} ok={rep.ok}")
    assert ok, f"plan={plan.feasibility} rep.ok={rep.ok} blocked={rep.blocked}"


def test_factory_builder_run_micro_sans_gisement() -> None:
    """scan_patch ne trouve rien -> plan non faisable, aucune pose tentée."""
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal

    api = FakeApi(FULL_KIT, patch_bbox=None)
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 10)))
    plan, rep = fb.run_micro_layout("iron-ore", generate=False, approach=False)
    ok = (plan.feasibility == "patch" and not rep.ok and not rep.placed
          and not api.named("place_entity_at") and rep.notes)
    rec("test_factory_builder_run_micro_sans_gisement", ok,
        f"feas={plan.feasibility} notes={rep.notes}")
    assert ok, f"feas={plan.feasibility} rep={rep}"


def test_build_micro_ancre_sur_une_tuile_de_minerai() -> None:
    """L'ancre du drill sort du `sample` (minerai réel), jamais du centre du bbox.

    Cause racine de l'échec live E1 : le bbox de scan_patch AGRÈGE tous les gisements
    du rayon (tools.lua, un seul find_entities_filtered). Au rayon 400 son centre
    tombait sur de l'herbe à 250 tuiles du minerai -> `build_check_type=manual` refuse
    un mining-drill hors gisement, 26 poses sur 26 en échec.
    """
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal

    # bbox volontairement décentré : centre (0,0) mais minerai en bas à droite.
    api = FakeApi(FULL_KIT, patch_bbox={"x1": 8, "y1": 8, "x2": 12, "y2": 12})
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 10)))
    plan = fb.build_micro_layout("iron-ore", facing=4)
    drill = plan.entities[0]
    sample_xy = {(t["x"], t["y"]) for t in api.scan_patch("iron-ore")["sample"]}
    # L'ancre est une tuile du sample reculée d'une tuile vers l'intérieur (facing sud).
    ok = (plan.feasibility == "ok"
          and (int(drill.x), int(drill.y) + 1) in sample_xy
          and 8 <= drill.x <= 12 and 8 <= drill.y <= 12)
    rec("test_build_micro_ancre_sur_une_tuile_de_minerai", ok,
        f"drill=({drill.x},{drill.y}) sample={sorted(sample_xy)[:4]}...")
    assert ok, f"drill=({drill.x},{drill.y}) feas={plan.feasibility}"


def test_build_micro_ancre_suit_le_facing() -> None:
    """Bord aval : la chaîne se déploie hors du gisement, là où il reste de la place."""
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal

    def drill_for(bbox: dict, facing: int):
        fb = FactoryBuilder(FakeApi(FULL_KIT, patch_bbox=bbox),
                            Contract(goal=ProductionGoal("iron-plate", 10)))
        return fb.build_micro_layout("iron-ore", facing=facing).entities[0]

    # `sample` est borné à 12 tuiles (tools.lua) : gisements en ligne pour que les deux
    # bords soient distincts dans l'échantillon.
    colonne = {"x1": 0, "y1": 0, "x2": 0, "y2": 11}
    ligne = {"x1": 0, "y1": 0, "x2": 11, "y2": 0}
    sud, nord = drill_for(colonne, 4), drill_for(colonne, 0)
    est, ouest = drill_for(ligne, 2), drill_for(ligne, 6)
    ok = sud.y > nord.y and est.x > ouest.x
    rec("test_build_micro_ancre_suit_le_facing", ok,
        f"sud.y={sud.y} nord.y={nord.y} | est.x={est.x} ouest.x={ouest.x}")
    assert ok, f"sud={sud.y} nord={nord.y} est={est.x} ouest={ouest.x}"


def test_build_micro_escalade_de_rayon() -> None:
    """scan_patch est appelé du plus petit rayon au plus grand : on isole le gisement local."""
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal

    api = FakeApi(FULL_KIT, patch_bbox={"x1": -2, "y1": -2, "x2": 2, "y2": 2})
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 10)))
    fb.build_micro_layout("iron-ore")
    radii = [c[2] for c in api.named("scan_patch")]
    ok = radii and radii[0] == FactoryBuilder.PATCH_RADII[0] and len(radii) == 1
    rec("test_build_micro_escalade_de_rayon", ok, f"rayons appelés={radii}")
    assert ok, f"radii={radii}"


def test_build_micro_sans_sample_refuse_de_planifier() -> None:
    """Pas de tuile de minerai exploitable -> pas de plan (plutôt qu'un plan sur l'herbe)."""
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal

    api = FakeApi(FULL_KIT, patch_bbox={"x1": 0, "y1": 0, "x2": 2, "y2": 2})
    api.scan_patch = lambda resource, radius=400.0: {          # type: ignore[assignment]
        "resource": resource, "count": 9, "bbox": {"x1": 0, "y1": 0, "x2": 2, "y2": 2}}
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 10)))
    plan = fb.build_micro_layout("iron-ore")
    ok = plan.feasibility == "patch" and not plan.entities
    rec("test_build_micro_sans_sample_refuse_de_planifier", ok,
        f"feas={plan.feasibility} notes={plan.notes[-1:]}")
    assert ok, f"feas={plan.feasibility}"


def test_executor_se_rapproche_de_chaque_pose_lointaine() -> None:
    """UNE SEULE APPROCHE NE SUFFIT PAS POUR UN PLAN ÉTALÉ.

    L'executor marchait une fois vers le CENTRE du plan, puis posait toutes les entités
    sans bouger. En `test_mode` c'est indifférent — aucune portée n'est vérifiée. En
    PRODUCTION le mod refuse toute pose au-delà de `build_distance`, et une chaîne plus
    large que cette portée devient impossible à bâtir.

    Mesuré à la septième partie d'Hermes, sur carte vierge : 650 secondes
    d'approvisionnement impeccable, `missing={}`, `can_place` répondant True sur le
    gisement — et pourtant :

        blocked=[(0, 'burner-mining-drill', -55.0, -91.0, 'walk closer first')]

    Le joueur était en (-43,-86), la foreuse à poser en (-55,-91) : treize tuiles, au-delà
    de la portée. Sixième défaut de la famille « sain en test, bloquant en jeu ».

    On s'approche donc AVANT CHAQUE POSE qui l'exige — et seulement celles-là : une
    entité déjà à portée ne doit pas coûter un déplacement.
    """
    api = FakeApi(FULL_KIT)
    api.pos = (0.0, 0.0)                      # loin des entités du plan

    def _walk(x, y):
        api.calls.append(("walk_to", x, y))
        api.pos = (float(x), float(y))        # la marche aboutit
        return {"ok": True}

    def _etat():
        return {"tick": 1, "ready": True, "test_mode": False,
                "character": {"position": {"x": api.pos[0], "y": api.pos[1]}},
                "inventory": dict(api.inv), "task": {}}

    api.walk_to, api.get_state = _walk, _etat
    plan = _micro(facing=4)
    # On éloigne la dernière entité au-delà de toute portée raisonnable.
    plan.entities[-1].x, plan.entities[-1].y = 60.0, 60.0

    execute_micro(api, plan, generate=False, approach=True, verify=False)

    marches = api.named("walk_to")
    # Au moins une marche VERS la zone de l'entité lointaine, en plus de l'approche
    # initiale : sans elle, le mod répondrait « walk closer first ».
    vers_loin = [c for c in marches if abs(c[1] - 60.0) < 12 and abs(c[2] - 60.0) < 12]
    ok = len(marches) >= 2 and bool(vers_loin)
    rec("test_executor_se_rapproche_de_chaque_pose_lointaine", ok,
        f"{len(marches)} marche(s) : {[(round(c[1]), round(c[2])) for c in marches]} — "
        f"approche de l'entité lointaine : {bool(vers_loin)}")
    assert ok


def _executor_loin(approach: bool):
    """Un plan dont la dernière entité est hors de portée, l'avatar à l'origine."""
    api = FakeApi(FULL_KIT)
    api.pos = (0.0, 0.0)

    def _walk(x, y):
        api.calls.append(("walk_to", x, y))
        api.pos = (float(x), float(y))
        return {"ok": True}

    def _etat():
        return {"tick": 1, "ready": True, "test_mode": False,
                "character": {"position": {"x": api.pos[0], "y": api.pos[1]}},
                "inventory": dict(api.inv), "task": {}}

    api.walk_to, api.get_state = _walk, _etat
    plan = _micro(facing=4)
    plan.entities[-1].x, plan.entities[-1].y = 60.0, 60.0
    execute_micro(api, plan, generate=False, approach=approach, verify=False)
    return api, plan


def test_executor_s_approche_meme_sans_approche_initiale() -> None:
    """LA PORTÉE N'EST PAS UNE OPTION — `approach=False` ne l'abolit pas.

    Le correctif précédent plaçait l'approche par pose sous le drapeau `approach`. Or
    `Coordinator.batir_chaine` passe `approach=False` DÉLIBÉRÉMENT, et pour une raison
    juste : l'approche initiale menait l'avatar au milieu du chantier, où il refusait
    ensuite sa propre pose (`can_place` en mode manuel exclut la tuile du personnage).
    Le drapeau court-circuitait donc le correctif sur le seul chemin qui en avait besoin.

    Mesuré à la huitième partie, correctif en place : 646 s, `missing={}`, et toujours
    `blocked=[(0,'burner-mining-drill',57,37,'walk closer first')]` avec l'avatar en
    (52,25) — treize tuiles.

    Marcher n'est pas un confort mais une CONDITION PHYSIQUE : au-delà de
    `build_distance` le mod refuse, et la chaîne est infaisable. Ce que `approach`
    gouverne, c'est l'approche initiale vers le centre du plan — rien d'autre.
    """
    api, _ = _executor_loin(approach=False)
    marches = api.named("walk_to")
    vers_loin = [c for c in marches if abs(c[1] - 60.0) < 12 and abs(c[2] - 60.0) < 12]
    ok = bool(vers_loin)
    rec("test_executor_s_approche_meme_sans_approche_initiale", ok,
        f"{len(marches)} marche(s) : {[(round(c[1]), round(c[2])) for c in marches]}")
    assert ok


def test_executor_ne_marche_pas_sur_la_tuile_a_batir() -> None:
    """S'APPROCHER SANS SE METTRE EN TRAVERS.

    L'autre moitié du dilemme, et la raison pour laquelle `batir_chaine` avait renoncé
    à s'approcher : `can_place_check` en mode manuel REFUSE la tuile où se tient le
    personnage. Vérifié en jeu — une foreuse refusée sur un emplacement parfaitement
    valide, minerai sur les quatre tuiles, `can_place=True` une fois l'avatar ailleurs.
    Et une seule entité refusée fait abandonner le plan entier.

    On s'arrête donc À CÔTÉ : assez près pour bâtir, assez loin pour ne pas occuper
    l'emplacement.
    """
    api, _ = _executor_loin(approach=False)
    vers_loin = [c for c in api.named("walk_to")
                 if abs(c[1] - 60.0) < 12 and abs(c[2] - 60.0) < 12]
    assert vers_loin, "aucune approche : le test precedent couvre ce cas"
    x, y = vers_loin[-1][1], vers_loin[-1][2]
    ecart = max(abs(x - 60.0), abs(y - 60.0))
    # Ni sur la tuile (>= 2), ni hors de portee de construction (<= 8).
    ok = 2.0 <= ecart <= 8.0
    rec("test_executor_ne_marche_pas_sur_la_tuile_a_batir", ok,
        f"arret en ({x:.1f},{y:.1f}) soit {ecart:.1f} tuile(s) de l'entite a poser")
    assert ok


def main() -> int:
    tests = [
        test_executor_pose_les_3_entites,
        test_executor_inventaire_insuffisant_ne_pose_rien,
        test_executor_inventaire_compte_le_combustible,
        test_executor_retry_offset_sur_collision,
        test_executor_decalage_solidaire,
        test_executor_verifie_toutes_les_positions_avant_de_poser,
        test_executor_pose_fantome_rejetee,
        test_executor_se_rapproche_de_chaque_pose_lointaine,
        test_executor_s_approche_meme_sans_approche_initiale,
        test_executor_ne_marche_pas_sur_la_tuile_a_batir,
        test_executor_verify_desactivable,
        test_executor_retries_epuises_bloque_et_arrete,
        test_executor_echec_pose_malgre_can_place,
        test_executor_ordre_topologique,
        test_executor_direction_int_vers_nom,
        test_executor_alimente_tous_les_burners,
        test_executor_is_burner_discrimine_electrique,
        test_executor_fuel_count_zero_desactive,
        test_executor_dry_run_ne_pose_rien,
        test_executor_skip_filtre,
        test_place_opts_depuis_layout_entity,
        test_executor_transmet_les_options_de_pose,
        test_executor_plan_vide_et_infaisable,
        test_executor_approche_generate_et_walk,
        test_executor_report_journalise,
        test_factory_builder_run_micro_layout,
        test_factory_builder_run_micro_sans_gisement,
        test_build_micro_ancre_sur_une_tuile_de_minerai,
        test_build_micro_ancre_suit_le_facing,
        test_build_micro_escalade_de_rayon,
        test_build_micro_sans_sample_refuse_de_planifier,
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