"""Test FactoryBuilder P1 / P1b.

Niveaux de validation :

1. **test_planner()** — UNITAIRE, pur Python, aucun serveur requis. Valide
   l'arbitrage du planificateur déterministe (services/knowledge).
2. **test_llm_schema()** — UNITAIRE, pur Python, aucun serveur ni LLM. Valide
   `services/llm.validate_plan` : accepte les plans valides, rejette les plans
   invalides (kind inconnu, item hors whitelist, count=0, mine sans walk, smelt
   sans four/wait, plan ne satisfaisant pas l'objectif, >40 steps, double four).
3. **test_planner_injection()** — UNITAIRE. Valide l'injection DIP : FakePlanner
   utilisé tel quel par FactoryBuilder ; LLMPlanner désactivé -> fallback det.
4. **test_integration(headless, llm)** — INTÉGRATION sur serveur RCON. Exécute
   réellement la chaîne. Cas 1 (craft direct +5 gears) + Cas 2 (mine+smelt+craft
   +25 gears). En mode `--llm`, la décision vient du LLM (avec fallback det si
   LLM down ou plan invalide) ; les asserts portent sur l'aboutissement (delta
   gears), pas sur le plan exact (non-déterminisme LLM).

Prérequis serveur :
  - Mode physique (headless=False) : serveur dédié + joueur connecté.
  - Mode headless (headless=True) : serveur (re)démarré ; reset_character
    réarme le kit (0 gears, 50 plaques) pour la reproductibilité.
  - Mode `--llm` : Ollama (localhost:11434) avec OPENAI_MODEL (défaut glm-5.2:cloud).
    Si LLM injoignable -> fallback det + test SKIP (pas FAIL).

Lancement :
    cd python
    python -m tests.test_factory_builder --unit               # P1 + schema + injection
    python -m tests.test_factory_builder --headless           # P1 déterministe headless
    python -m tests.test_factory_builder                     # P1 déterministe physique
    python -m tests.test_factory_builder --headless --llm     # P1b LLM headless
    python -m tests.test_factory_builder --llm                # P1b LLM physique
    python -m tests.test_factory_builder --headless --llm-loop # P1c boucle LLM headless
    python -m tests.test_factory_builder --llm-loop           # P1c boucle LLM physique
"""

from __future__ import annotations

import argparse
import json

from core.rcon import get_rcon
from core.mod_api import ModApi
from services import knowledge
from services.knowledge import ProductionGoal, has_mining, plan_summary
from services.llm import PlanContext, PlanResult, validate_plan, LLMPlanner
from agents.base import Contract
from agents.factory_builder import FactoryBuilder

RESULTS: list[tuple[str, bool, str]] = []


def record(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:42s} {detail[:120]}")


def recap() -> None:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        print(f"[{'OK  ' if ok else 'FAIL'}] {name:42s} {detail[:120]}")
    print("-" * 72)
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)


# ===== 1. Test unitaire du planificateur (aucun serveur) =====

def _fake_recipe_lookup(item: str):
    """Recette en dur pour le test unitaire (évite un appel RCON)."""
    if item == "iron-gear-wheel":
        return [("iron-plate", 2)]
    return None


def test_planner() -> None:
    print("\n[test] === UNITAIRE : planificateur (arbitrage inventaire) ===")

    # Cas "stock suffisant" : 50 plaques >= 10 requises -> craft direct, pas de mine.
    plan1 = knowledge.plan_production(ProductionGoal("iron-gear-wheel", 5),
                                      {"iron-plate": 50}, _fake_recipe_lookup)
    kinds1 = [s.kind for s in plan1]
    record("Cas1 plan = craft direct",
           plan1 == [knowledge.Step("craft_item", {"item": "iron-gear-wheel", "count": 5})],
           f"plan={plan_summary(plan1)} kinds={kinds1}")
    record("Cas1 AUCUN minage", not has_mining(plan1), f"has_mining={has_mining(plan1)}")

    # Cas "stock insuffisant" : 40 plaques < 60 requises (30 gears) -> produire 20 plaques.
    plan2 = knowledge.plan_production(ProductionGoal("iron-gear-wheel", 30),
                                      {"iron-plate": 40}, _fake_recipe_lookup)
    mine_steps = [s for s in plan2 if s.kind == "mine_entity"]
    craft_steps = [s for s in plan2 if s.kind == "craft_item"]
    record("Cas2 plan avec minage", has_mining(plan2), f"has_mining={has_mining(plan2)} plan={plan_summary(plan2)}")
    record("Cas2 mine 20 iron-ore",
           len(mine_steps) == 1 and mine_steps[0].args.get("name") == "iron-ore"
           and mine_steps[0].args.get("count") == 20,
           f"mine_steps={mine_steps}")
    record("Cas2 craft 30 gears",
           len(craft_steps) == 1 and craft_steps[0].args == {"item": "iron-gear-wheel", "count": 30},
           f"craft_steps={craft_steps}")

    # Cas limite : objectif déjà satisfait -> plan vide.
    plan0 = knowledge.plan_production(ProductionGoal("iron-gear-wheel", 5),
                                       {"iron-gear-wheel": 10}, _fake_recipe_lookup)
    record("Cas0 deja satisfait -> plan vide", plan0 == [], f"plan={plan0}")


# ===== 1b. Test unitaire validate_plan (services/llm) — aucun serveur ni LLM =====

def _ctx() -> PlanContext:
    return PlanContext(goal_recipe=[("iron-plate", 2)],
                       kit={"iron-plate": 50, "coal": 100})


def _craft_direct(n: int = 5) -> list[dict]:
    return [{"kind": "craft_item", "args": {"item": "iron-gear-wheel", "count": n}}]


def _full_chain(ore: int, gears: int) -> list[dict]:
    return [
        {"kind": "find_nearest", "args": {"name": "iron-ore"}},
        {"kind": "walk_to_entity", "args": {"name": "iron-ore"}},
        {"kind": "mine_entity", "args": {"name": "iron-ore", "count": ore}},
        {"kind": "place_furnace", "args": {}},
        {"kind": "move_items", "args": {"item": "coal", "count": 5, "to_entity": True}},
        {"kind": "move_items", "args": {"item": "iron-ore", "count": ore, "to_entity": True}},
        {"kind": "wait", "args": {"ticks": 2200}},
        {"kind": "move_items", "args": {"item": "iron-plate", "count": ore, "to_entity": False}},
        {"kind": "craft_item", "args": {"item": "iron-gear-wheel", "count": gears}},
    ]


def test_llm_schema() -> None:
    print("\n[test] === UNITAIRE : validate_plan (schema + simulation) ===")
    ctx = _ctx()

    # --- plans valides ---
    ok, _, m = validate_plan(_craft_direct(5), ctx, ProductionGoal("iron-gear-wheel", 5),
                             {"iron-plate": 50, "coal": 100})
    record("VP craft direct valide", ok, m)

    ok, _, m = validate_plan(_full_chain(ore=10, gears=25), ctx,
                             ProductionGoal("iron-gear-wheel", 25),
                             {"iron-plate": 40, "coal": 100})
    record("VP mine+smelt+craft valide", ok, m)

    # plan vide si objectif déjà satisfait
    ok, _, m = validate_plan([], ctx, ProductionGoal("iron-gear-wheel", 5),
                             {"iron-gear-wheel": 10, "coal": 100})
    record("VP plan vide si deja satisfait", ok, m)

    # --- plans invalides ---
    ok, _, m = validate_plan([{"kind": "teleport", "args": {}}], ctx,
                             ProductionGoal("iron-gear-wheel", 5),
                             {"iron-plate": 50, "coal": 100})
    record("VI kind inconnu rejete", not ok, m)

    ok, _, m = validate_plan([{"kind": "craft_item", "args": {"item": "titanium", "count": 5}}],
                             ctx, ProductionGoal("iron-gear-wheel", 5),
                             {"iron-plate": 50, "coal": 100})
    record("VI item hors whitelist rejete", not ok, m)

    ok, _, m = validate_plan([{"kind": "mine_entity", "args": {"name": "iron-ore", "count": 0}}],
                             ctx, ProductionGoal("iron-gear-wheel", 5),
                             {"iron-plate": 50, "coal": 100})
    record("VI count=0 rejete", not ok, m)

    # mine sans walk préalable
    bad = [{"kind": "find_nearest", "args": {"name": "iron-ore"}},
           {"kind": "mine_entity", "args": {"name": "iron-ore", "count": 10}}]
    ok, _, m = validate_plan(bad, ctx, ProductionGoal("iron-gear-wheel", 5),
                             {"iron-plate": 50, "coal": 100})
    record("VI mine sans walk rejete", not ok, m)

    # smelt sans wait (récupère plaque non smeltée)
    bad = [{"kind": "find_nearest", "args": {"name": "iron-ore"}},
           {"kind": "walk_to_entity", "args": {"name": "iron-ore"}},
           {"kind": "mine_entity", "args": {"name": "iron-ore", "count": 10}},
           {"kind": "place_furnace", "args": {}},
           {"kind": "move_items", "args": {"item": "iron-ore", "count": 10, "to_entity": True}},
           {"kind": "move_items", "args": {"item": "iron-plate", "count": 10, "to_entity": False}},
           {"kind": "craft_item", "args": {"item": "iron-gear-wheel", "count": 25}}]
    ok, _, m = validate_plan(bad, ctx, ProductionGoal("iron-gear-wheel", 25),
                             {"iron-plate": 40, "coal": 100})
    record("VI smelt sans wait rejete", not ok, m)

    # plan qui ne satisfait pas l'objectif (craft trop peu)
    ok, _, m = validate_plan(_craft_direct(5), ctx, ProductionGoal("iron-gear-wheel", 30),
                             {"iron-plate": 50, "coal": 100})
    record("VI objectif non atteint rejete", not ok, m)

    # > 40 steps
    big = _craft_direct(5) * 41
    ok, _, m = validate_plan(big, ctx, ProductionGoal("iron-gear-wheel", 205),
                             {"iron-plate": 500, "coal": 100})
    record("VI > 40 steps rejete", not ok, m)

    # double place_furnace
    bad = _full_chain(ore=10, gears=25)
    bad.insert(4, {"kind": "place_furnace", "args": {}})
    ok, _, m = validate_plan(bad, ctx, ProductionGoal("iron-gear-wheel", 25),
                             {"iron-plate": 40, "coal": 100})
    record("VI double place_furnace rejete", not ok, m)


# ===== 1c. Test unitaire injection planner (DIP) — aucun serveur ni réseau =====

class _FakePlanner:
    """Planner factice (mock) qui retourne un plan prédéfini — valide l'injection DIP."""
    def __init__(self, steps: list, source: str = "fake"):
        self._steps = steps
        self._source = source

    def plan(self, goal, inv, ctx):
        return PlanResult(list(self._steps), source=self._source)


def test_planner_injection() -> None:
    print("\n[test] === UNITAIRE : injection planner (DIP) ===")
    # FakePlanner valide -> FactoryBuilder l'utilise tel quel (pas le déterministe).
    fake_steps = [knowledge.Step("craft_item", {"item": "iron-gear-wheel", "count": 7})]
    fake = _FakePlanner(fake_steps, source="fake")
    # FactoryBuilder sans API réelle : on n'appelle pas decide() (qui ferait RCON
    # via _build_context). On vérifie juste que le planner injecté est conservé.
    fb = FactoryBuilder(api=None, contract=Contract(ProductionGoal("iron-gear-wheel", 7)),
                        planner=fake)
    record("FakePlanner injecte conserve", fb.planner is fake,
           f"planner={fb.planner.__class__.__name__}")

    # LLMPlanner désactivé (base_url vide) -> fallback déterministe, pas de réseau.
    from config import Config
    cfg = Config(openai_base_url="", openai_model="glm-5.2:cloud", llm_enabled=True,
                 openai_api_key="ollama")
    pl = LLMPlanner(cfg, recipe_lookup=_fake_recipe_lookup)
    res = pl.plan(ProductionGoal("iron-gear-wheel", 5), {"iron-plate": 50}, _ctx())
    record("LLM disabled -> fallback det", res.source == "det",
           f"source={res.source} reason={res.reason!r} steps={len(res.steps)}")

    # LLMPlanner enabled=False -> fallback.
    cfg2 = Config(openai_base_url="http://localhost:11434/v1", openai_model="glm-5.2:cloud",
                  llm_enabled=False, openai_api_key="ollama")
    pl2 = LLMPlanner(cfg2, recipe_lookup=_fake_recipe_lookup)
    res2 = pl2.plan(ProductionGoal("iron-gear-wheel", 5), {"iron-plate": 50}, _ctx())
    record("LLM_ENABLED=false -> fallback det", res2.source == "det",
           f"source={res2.source} reason={res2.reason!r}")


# ===== 1c. Test unitaire build_layout (S4c) — mock ModApi, aucun serveur =====

class _FakeModApi:
    """Mock ModApi pour build_layout (S4c). scan_patch/obstacles/water_edge/tiles_bbox canned.

    `patches` : liste ordonnée de bbox dict (ou None) retournés par scan_patch successifs
    (rayon croissant 400/800/1200). `obstacles_by_patch` : dict index->list[(x1,y1,x2,y2)] ;
    l'index actif = dernier scan_patch réussi (scan_obstacles dépend du patch traité).
    `water` : bbox dict ou None. `tiles` : nom de tuile pour scan_tiles_bbox (default "land").
    """

    def __init__(self, patches, obstacles_by_patch, water=None, tiles="land"):
        self._patches = patches
        self._obstacles_by_patch = obstacles_by_patch
        self._water = water
        self._tiles = tiles
        self._pcall = 0
        self._active = 0

    def scan_patch(self, resource, radius=400.0):
        i = self._pcall
        self._pcall += 1
        if i < len(self._patches) and self._patches[i] is not None:
            self._active = i
            return {"bbox": self._patches[i], "resource": resource, "count": 100}
        return {}

    def scan_obstacles(self, radius=400.0):
        obs = self._obstacles_by_patch.get(self._active, [])
        return {
            "obstacles": [
                {"x": o[0], "y": o[1], "w": o[2] - o[0], "h": o[3] - o[1],
                 "name": "rock", "type": "simple-entity"} for o in obs
            ],
            "count": len(obs),
        }

    def scan_water_edge(self, radius=200):
        return {"bbox": self._water} if self._water else {}

    def scan_tiles_bbox(self, x1, y1, x2, y2):
        tiles = [{"x": x, "y": y, "name": self._tiles}
                 for x in range(int(x1), int(x2)) for y in range(int(y1), int(y2))]
        return {"tiles": tiles, "count": len(tiles)}


def _splan_gears():
    """splan fixture iron-gear-wheel@5/s (chaîne ore->plate->gear) via solveur + kb fixture."""
    from tests.test_layout_solver import sample_kb, sample_geometry
    from services.production_solver import ProductionRequest, solve
    kb = sample_kb()
    splan = solve(ProductionRequest("iron-gear-wheel", 5.0), kb)
    return splan, sample_geometry()


def test_build_layout() -> None:
    print("\n[test] === UNITAIRE : build_layout (S4c arbitre replan lourd) ===")
    from agents.base import Contract
    from services.knowledge import ProductionGoal
    splan, geometry = _splan_gears()

    # --- Cas 1 : terrain libre -> feasibility=ok (gisement 1, tier yellow) ---
    api1 = _FakeModApi(
        patches=[{"x1": 0, "y1": 0, "x2": 20, "y2": 20}, None, None],
        obstacles_by_patch={0: []},
    )
    fb1 = FactoryBuilder(
        api1, Contract(ProductionGoal("iron-gear-wheel", 5), zone=(0, 0), replan_budget=4))
    lp1 = fb1.build_layout(splan, geometry)
    record("Cas1 terrain libre -> ok",
           lp1 is not None and lp1.feasibility == "ok",
           f"feas={getattr(lp1, 'feasibility', None)}")
    record("Cas1 tier yellow (1er tier)",
           lp1 is not None and lp1.request.constraints.belt_tier == "transport-belt",
           f"belt_tier={getattr(lp1.request.constraints, 'belt_tier', None)}")

    # --- Cas 2 : terrain bloqué partout (obstacle géant) -> obstacle_blocking, best non None ---
    api2 = _FakeModApi(
        patches=[{"x1": 0, "y1": 0, "x2": 20, "y2": 20}, None, None],
        obstacles_by_patch={0: [(-50, -50, 500, 500)]},  # couvre tout, insurmontable par replan léger
    )
    fb2 = FactoryBuilder(
        api2, Contract(ProductionGoal("iron-gear-wheel", 5), zone=(0, 0), replan_budget=4))
    lp2 = fb2.build_layout(splan, geometry)
    record("Cas2 terrain bloqué -> obstacle_blocking (best retenu)",
           lp2 is not None and lp2.feasibility == "obstacle_blocking",
           f"feas={getattr(lp2, 'feasibility', None)} entities={len(getattr(lp2,'entities',[]))}")

    # --- Cas 3 : 2 gisements, patch1 bloqué + patch2 libre -> ok sur gisement 2 ---
    api3 = _FakeModApi(
        patches=[{"x1": 0, "y1": 0, "x2": 20, "y2": 20},
                 {"x1": 200, "y1": 0, "x2": 220, "y2": 20}, None],
        obstacles_by_patch={0: [(-50, -50, 300, 300)], 1: []},
    )
    fb3 = FactoryBuilder(
        api3, Contract(ProductionGoal("iron-gear-wheel", 5), zone=(0, 0), replan_budget=4))
    lp3 = fb3.build_layout(splan, geometry)
    record("Cas3 2 gisements (patch1 bloqué, patch2 libre) -> ok",
           lp3 is not None and lp3.feasibility == "ok",
           f"feas={getattr(lp3, 'feasibility', None)}")

    # --- Cas 4 : aucun patch trouvé (scan_patch {} tous radius) -> retourne None ---
    api4 = _FakeModApi(patches=[None, None, None], obstacles_by_patch={})
    fb4 = FactoryBuilder(
        api4, Contract(ProductionGoal("iron-gear-wheel", 5), zone=(0, 0), replan_budget=4))
    lp4 = fb4.build_layout(splan, geometry)
    record("Cas4 aucun patch -> retourne None (handoff abandon)",
           lp4 is None, f"lp={lp4}")

    # --- Cas 5 : pas de mine (splan sans node mine) -> chemin défensif (terrain vide + plan
    # direct), retourne un LayoutPlan non None (pas de crash ; feasibility dépend du splan). ---
    from types import SimpleNamespace as _NS
    node_craft = _NS(role="craft", item="iron-gear-wheel", machine="assembling-machine-1",
                     ingredients=[("iron-plate", 2)], machine_count=1, rate_effective=5.0,
                     source_item=None, transport="belt")
    splan_no_mine = _NS(nodes=[node_craft], leaves=[node_craft], feasibility="ok")
    api5 = _FakeModApi(patches=[None, None, None], obstacles_by_patch={})
    fb5 = FactoryBuilder(
        api5, Contract(ProductionGoal("iron-gear-wheel", 5), zone=(0, 0), replan_budget=4))
    lp5 = fb5.build_layout(splan_no_mine, geometry)
    record("Cas5 pas de mine -> chemin défensif (LayoutPlan non None, pas crash)",
           lp5 is not None,
           f"feas={getattr(lp5, 'feasibility', None)} entities={len(getattr(lp5,'entities',[]))}")

    # --- Cas 6 : replan_budget propagé depuis Contract vers le LayoutPlan ---
    record("Cas6 replan_budget propagé (Contract -> LayoutPlan)",
           lp1 is not None and lp1.request.constraints.replan_budget == 4,
           f"replan_budget={getattr(lp1.request.constraints, 'replan_budget', None)}")

    # --- Cas 7 : constructible_zone dérivé depuis Contract.zone (point -> bbox ±60) ---
    record("Cas7 constructible_zone dérivé de Contract.zone (±60)",
           lp1 is not None and lp1.request.constraints.constructible_zone == (-60, -60, 60, 60),
           f"zone={getattr(lp1.request.constraints, 'constructible_zone', None)}")

    # --- Cas 8 : terrain_check=True injecté par _merge_constraints (défaut S4b) ---
    record("Cas8 terrain_check=True injecté (défaut S4b)",
           lp1 is not None and lp1.request.constraints.terrain_check is True,
           f"terrain_check={getattr(lp1.request.constraints, 'terrain_check', None)}")


# ===== 2. Test d'intégration sur serveur =====

def _inv(api: ModApi) -> dict[str, int]:
    from services import perception
    return perception.inventory(api)


def _run_case(api: ModApi, goal_count: int, expect_mining: bool,
              label: str, expect_gear_delta: int, mode: str = "det",
              planner=None, stepplanner=None) -> None:
    """Lance FactoryBuilder sur un objectif, vérifie l'arbitrage + l'exécution.

    mode :
      - "det"      : déterministe (P1). Assert strict sur has_mining.
      - "llm"      : plan complet LLM (P1b). has_mining = diagnostic, on vérifie
                     la source (llm OK / det unreachable SKIP / det rattrapage).
      - "llm-loop" : boucle pas-à-pas LLM (P1c). On appelle `run_loop` ; asserts
                     sur l'aboutissement (delta gears + dernier résultat OK),
                     pas sur le plan (boucle non-déterministe). Signale si le
                     fallback déterministe final a déclenché (info, pas FAIL si
                     l'objectif est atteint).

    L'assert principal reste l'aboutissement (delta gears).
    """
    inv_before = _inv(api)
    agent = FactoryBuilder(api, Contract(ProductionGoal("iron-gear-wheel", goal_count)),
                           planner=planner)

    if mode == "llm-loop":
        # Boucle P1c : decide + execute passent dans run_loop (1 action/tour).
        print(f"[test] --- boucle {label} ---")
        results = agent.run_loop(stepplanner)
        last = results[-1] if results else {}
        inv_after = _inv(api)
        d_gear = inv_after.get("iron-gear-wheel", 0) - inv_before.get("iron-gear-wheel", 0)
        fb = " (fallback final)" if getattr(agent.last_plan, "source", None) == "det" else ""
        record(f"{label} loop execution OK",
               isinstance(last, dict) and last.get("ok") is True,
               f"iters={len(results)} last={json.dumps(last, ensure_ascii=False)[:80]}{fb}")
        record(f"{label} +{expect_gear_delta} gears",
               d_gear == expect_gear_delta,
               f"gears {inv_before.get('iron-gear-wheel', 0)} -> "
               f"{inv_after.get('iron-gear-wheel', 0)} (delta={d_gear}){fb}")
        return

    # Inspection du plan AVANT exécution.
    gs = agent.perceive()
    plan = agent.decide(gs)
    mining = has_mining(plan)

    if mode == "llm":
        # has_mining = diagnostic informatif (le LLM peut réordonner), pas d'assert.
        record(f"{label} diag minage={mining}", True,
               f"obtenu={mining} ({plan_summary(plan)})")
        src = getattr(agent.last_plan, "source", "?")
        reason = getattr(agent.last_plan, "reason", "")
        if src == "llm":
            record(f"{label} source=llm", True, f"plan LLM utilise ({len(plan)} etapes)")
        elif reason.startswith("llm unreachable") or reason.startswith("openai indisponible"):
            record(f"{label} source=det (LLM down)", True,
                   f"SKIP: {reason[:80]} (fallback det)")
        else:
            record(f"{label} source=det (rattrapage)", True,
                   f"reason={reason[:80]} (fallback det)")
    else:
        record(f"{label} arbitrage minage={expect_mining}",
               mining == expect_mining,
               f"attendu minage={expect_mining} / obtenu={mining} ({plan_summary(plan)})")

    # Exécution réelle (chaîne complète via run_action race-free).
    print(f"[test] --- execution {label} ---")
    results = agent.act(plan)
    last = results[-1] if results else {}
    inv_after = _inv(api)
    d_gear = inv_after.get("iron-gear-wheel", 0) - inv_before.get("iron-gear-wheel", 0)
    record(f"{label} execution OK (derniere etape)",
           isinstance(last, dict) and last.get("ok") is True,
           f"last={json.dumps(last, ensure_ascii=False)[:80]}")
    record(f"{label} +{expect_gear_delta} gears",
           d_gear == expect_gear_delta,
           f"gears {inv_before.get('iron-gear-wheel', 0)} -> {inv_after.get('iron-gear-wheel', 0)} (delta={d_gear})")


def test_integration(headless: bool, llm: bool = False, loop: bool = False) -> None:
    mode = ("llm-loop " if loop else "llm " if llm else "") + \
          ("headless" if headless else "physique")
    print(f"\n[test] === INTEGRATION {mode} ===")
    rcon = get_rcon()
    api = ModApi(rcon)

    # Planner LLM si mode --llm/--llm-loop, None sinon (défaut déterministe).
    planner = None
    stepplanner = None
    if llm or loop:
        from config import load_config
        from services import perception
        cfg = load_config()
        record(f"{'LLMStepPlanner' if loop else 'LLMPlanner'} construit", True,
               f"model={cfg.openai_model} base_url={cfg.openai_base_url} enabled={cfg.llm_enabled}")
        rl = lambda it: perception.recipe_of(api, it)
        if loop:
            from services.llm import LLMStepPlanner
            stepplanner = LLMStepPlanner(cfg, recipe_lookup=rl)
        else:
            planner = LLMPlanner(cfg, recipe_lookup=rl)

    # Bascule mode dual + setup (kit de depart). En headless, on reset le
    # character (kit rearme) pour repartir d'un etat propre : 0 gears, 50 plaques,
    # independamment de l'etat residuel d'un run precedent sur le meme serveur.
    api.set_test_mode(headless)
    if headless:
        ack = api.reset_character()
        record("reset_character", isinstance(ack, dict) and ack.get("ok"),
               json.dumps(ack, ensure_ascii=False))
    else:
        ack = api.setup()
    if not (isinstance(ack, dict) and ack.get("ok")):
        print("[test] setup/reset echoue -> abandon integration")
        rcon.close()
        return
    if not headless:
        record("setup", True, "ok")

    st = api.get_state()
    print(f"[test] position initiale = {st.get('character', {}).get('position')} "
          f"test_mode={st.get('test_mode')} inv={dict(st.get('inventory', {}))}")

    case_mode = "llm-loop" if loop else "llm" if llm else "det"

    # Cas 1 : 5 gears. Kit present (50 iron-plate) -> craft direct, pas de mine.
    _run_case(api, goal_count=5, expect_mining=False,
              label="Cas1 x5", expect_gear_delta=5, mode=case_mode,
              planner=planner, stepplanner=stepplanner)

    # Cas 2 : 30 gears. Apres Cas 1, 40 plaques < 60 requises -> produire, mine.
    _run_case(api, goal_count=30, expect_mining=True,
              label="Cas2 x30", expect_gear_delta=25, mode=case_mode,
              planner=planner, stepplanner=stepplanner)

    rcon.close()


# ===== main =====

def main() -> None:
    p = argparse.ArgumentParser(description="Test FactoryBuilder P1/P1b/P1c")
    p.add_argument("--headless", action="store_true", help="integration en mode test headless")
    p.add_argument("--unit", action="store_true", help="unitaire seulement (pas de serveur, pas de LLM)")
    p.add_argument("--llm", action="store_true", help="integration avec decision LLM plan complet (P1b)")
    p.add_argument("--llm-loop", action="store_true", help="integration avec boucle LLM pas-a-pas (P1c)")
    args = p.parse_args()

    if args.unit:
        test_planner()
        test_llm_schema()
        test_planner_injection()
        test_build_layout()
        recap()
        return

    test_planner()
    test_llm_schema()
    test_planner_injection()
    test_build_layout()
    try:
        test_integration(headless=args.headless, llm=args.llm, loop=args.llm_loop)
    except Exception as e:
        record("integration", False, f"EXC: {e!r}")
    recap()


if __name__ == "__main__":
    main()