"""FactoryBuilder (P1/P1b) — agent-pivot de production.

Transforme un objectif de production (Contract.goal) en un plan exécutable, en
analysant la recette + l'inventaire, puis en l'exécutant via BaseAgent.act.

**P1** = DÉTERMINISTE (services/knowledge.plan_production).
**P1b** = décision LLM (services/llm.LLMPlanner) avec fallback déterministe si
le plan LLM est invalide ou le LLM injoignable. Chaîne couverte : iron-ore ->
iron-plate -> iron-gear-wheel (+ copper idem). Arbitrage selon l'inventaire :
déjà assez -> crafter direct ; pas assez -> produire l'ingrédient manquant.

La décision est déléguée à un `Planner` injecté (DIP). FactoryBuilder construit
un `DeterministicPlanner` par défaut si aucun planner n'est fourni (lié à l'API
pour résoudre les recettes via perception.recipe_of — rétro-compatibilité P1).
FactoryBuilder n'importe jamais openai directement.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from core.state import GameState
from services import knowledge, perception
from services.llm import DeterministicPlanner, PlanContext, Planner

from .base import BaseAgent


# Kit de départ du mod (player.lua STARTING_ITEMS) — contexte fourni au LLM.
KIT: dict[str, int] = {
    "wood": 50, "coal": 100, "stone": 50, "iron-plate": 50, "copper-plate": 30,
    "burner-mining-drill": 4, "stone-furnace": 4, "burner-inserter": 10,
    "transport-belt": 50, "small-electric-pole": 20, "pipe": 20, "iron-chest": 4,
}

# Contraintes physiques communiquées au LLM (cf. knowledge._smelt_ticks, ratio coal).
_CONSTRAINTS: dict = {
    "smelt_ticks_per_plate": 220,
    "smelt_min_ticks": 600,
    "coal_per_plate_prompt": 1,   # conservatif (incite à mettre assez de coal)
}


class FactoryBuilder(BaseAgent):
    """Agent-pivot : analyse -> plan -> exécution d'une usine de production."""

    def __init__(self, api, contract, planner: Planner | None = None):
        super().__init__(api, contract, planner)
        # Planner par défaut (rétro-compatibilité P1) : déterministe, résout les
        # recettes via perception.recipe_of lié à l'API.
        if self.planner is None:
            self.planner = DeterministicPlanner(
                lambda item: perception.recipe_of(self.api, item))

    def decide(self, gs: GameState) -> list[knowledge.Step]:
        ctx = self._build_context(gs)
        res = self.planner.plan(self.contract.goal, dict(gs.inventory), ctx)
        self.last_plan = res
        print(f"[agent] plan source={res.source} reason={res.reason} "
              f"({len(res.steps)} etapes)")
        return res.steps

    def _build_context(self, gs: GameState) -> PlanContext:
        """Assemble le contexte figé passé au planner (position + recette + kit)."""
        return PlanContext(
            position=gs.pos_tuple(),
            goal_recipe=perception.recipe_of(self.api, self.contract.goal.item),
            kit=dict(KIT),
            constraints=dict(_CONSTRAINTS),
        )

    def run_loop(self, stepplanner=None, max_iters: int = 30) -> list[dict]:
        """Boucle d'actions LLM pas-à-pas (P1c).

        Construit un `LLMStepPlanner` (lié à l'API pour résoudre les recettes)
        si aucun n'est fourni, puis délègue à `BaseAgent.run_loop`. Le contexte
        est assemblé via `_build_context` (réutilisé P1b).
        """
        if stepplanner is None:
            from config import load_config
            from services.llm import LLMStepPlanner
            cfg = load_config()
            stepplanner = LLMStepPlanner(
                cfg, recipe_lookup=lambda it: perception.recipe_of(self.api, it))
            model = cfg.openai_model
        else:
            model = getattr(stepplanner, "cfg", None)
            model = getattr(model, "openai_model", "?") if model is not None else "?"
        ctx = self._build_context(self.perceive())
        print(f"[agent] === boucle P1c pour {self.contract.goal.item} "
              f"x{self.contract.goal.count} model={model} ===")
        return super().run_loop(stepplanner, ctx, max_iters=max_iters)

    def _fallback_recipe_lookup(self):
        """Résolution de recette via l'API pour le fallback déterministe final."""
        return lambda it: perception.recipe_of(self.api, it)

    # ===== S4c : arbitrage replan lourd autour du LayoutPlanner =====
    #
    # Frontière LLM/déterministe (spec §7) : le LayoutPlanner est déterministe pur (replan
    # auto S4b = shift cascade_offset_v / pivot facing, règles fixes). FactoryBuilder ARBITRE
    # le replan LOURD (changer de gisement cible, monter tier belt) — décision stratégique qui
    # peut être LLM (optionnel) ; ici déterministe par défaut. Si le planner épuise son budget
    # (obstacle_blocking), FactoryBuilder essaie un autre gisement (scan_patch rayon croissant)
    # puis un tier belt supérieur (cascade plus compacte). Si tout échoue -> retourne le best
    # layout (abandon/handoff coordinator).

    # Tiers belt essayés (yellow -> red -> blue) pour compacter la cascade si terrain bloqué.
    _BELT_TIERS = ("transport-belt", "fast-transport-belt", "express-transport-belt")
    # Rayons de scan croissants pour trouver un gisement alternatif (replan lourd).
    _SCAN_RADII = (400, 800, 1200)

    def build_layout(self, splan, geometry, resource: Optional[str] = None,
                     facing: int = 2, anchor: Optional[tuple] = None) -> object:
        """S4c : arbitre le replan lourd (gisement/tier) autour du LayoutPlanner.

        1. Ressource = item du noeud mine du splan (ou override). Si pas de mine -> terrain
           vide + plan direct (pas de gisement à scanner).
        2. Pour chaque gisement (scan_patch, rayon croissant si replan lourd) :
           a. Construit Terrain (scan_obstacles/scan_water_edge/scan_tiles_bbox autour anchor).
           b. Pour chaque tier belt : appelle plan() (replan auto S4b via contract.replan_budget).
              - feasibility=ok -> retourne le LayoutPlan.
              - obstacle_blocking -> essaye tier suivant puis gisement suivant (best retenu).
              - autre (missing_geometry/patch) -> retourne tel quel (pas terrain).
        3. Si tout échoue -> retourne le best LayoutPlan (obstacle_blocking, handoff/abandon).

        Déterministe par défaut (LLM optionnel pour la décision stratégique gisement/tier).
        Limitation : scan_patch est centré avatar (pas point arbitraire) -> multi-gisement
        limité au rayon croissant (workaround : find_nearest itéré)."""
        from services.layout_planner import (
            LayoutRequest, LayoutConstraints, Terrain, ResourcePatch, plan,
        )

        if resource is None:
            mine = next((n for n in splan.nodes if n.role == "mine"), None)
            resource = mine.item if mine is not None else None
        if resource is None:
            # Pas de mine : pas de gisement. Terrain vide + plan direct (back-compat).
            c = self._merge_constraints("transport-belt", LayoutConstraints())
            req = LayoutRequest(plan=splan, terrain=Terrain(), anchor=anchor or (0.0, 0.0),
                                facing=facing, constraints=c)
            return plan(req, geometry)

        best: Optional[object] = None
        for radius in self._SCAN_RADII:
            sp = self.api.scan_patch(resource, radius)
            if not sp or not sp.get("bbox"):
                continue
            bbox = sp["bbox"]
            anc = anchor or (float(bbox["x1"]), (bbox["y1"] + bbox["y2"]) / 2.0)
            terrain = self._build_terrain(resource, bbox, anc)
            for bt in self._BELT_TIERS:
                # Tier non supporté par la géométrie -> skip (ne court-circuite pas le replan lourd
                # en retournant missing_geometry, qui ne distingue pas "tier absent" d'un vrai défaut).
                if geometry.geometry(bt) is None:
                    continue
                c = self._merge_constraints(bt, LayoutConstraints())
                req = LayoutRequest(plan=splan, terrain=terrain, anchor=anc,
                                    facing=facing, constraints=c)
                lp = plan(req, geometry)
                if lp.feasibility == "ok":
                    return lp
                if lp.feasibility == "obstacle_blocking":
                    best = lp if (best is None or len(lp.entities) > len(best.entities)) else best
                    continue
                # missing_geometry/patch/etc. -> pas terrain, retourne tel quel.
                return lp
        return best

    def _build_terrain(self, resource: str, bbox: dict, anchor: tuple) -> object:
        """Construit Terrain peuplé depuis le RCON (scan_obstacles/water_edge/tiles_bbox).
        Non destructif. tile_grid autour de l'anchor pour précision tuile water/out-of-map."""
        from services.layout_planner import Terrain, ResourcePatch
        obstacles = []
        r_obs = self.api.scan_obstacles(400)
        if isinstance(r_obs, dict):
            for o in r_obs.get("obstacles", []):
                obstacles.append((o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]))
        water = []
        r_we = self.api.scan_water_edge(200)
        if isinstance(r_we, dict) and r_we.get("bbox"):
            wb = r_we["bbox"]
            water.append((wb["x1"], wb["y1"], wb["x2"], wb["y2"]))
        tile_grid = {}
        sx1, sy1 = int(anchor[0]) - 30, int(anchor[1]) - 60
        sx2, sy2 = int(anchor[0]) + 120, int(anchor[1]) + 120
        r_st = self.api.scan_tiles_bbox(sx1, sy1, sx2, sy2)
        if isinstance(r_st, dict) and "error" not in r_st:
            for t in r_st.get("tiles", []):
                tile_grid[(t["x"], t["y"])] = t["name"]
        return Terrain(
            patches=[ResourcePatch(resource,
                                    bbox=(bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"]))],
            obstacles=obstacles, water=water, tile_grid=tile_grid,
        )

    def _merge_constraints(self, belt_tier: str, default_cls) -> object:
        """Merge contract.layout_constraints (si injecté) avec défauts S4b (terrain_check=True,
        replan_budget=contract.replan_budget) + belt_tier forcé (arbitrage tier).

        Si `layout_constraints` est injecté, on RESPECTE son `terrain_check` (back-compat S3d :
        un appelant peut désactiver la détection per-entité en injectant
        `LayoutConstraints(terrain_check=False)`). `replan_budget` vient toujours du Contract
        (l'autorité pour le budget replan). Si None (= défaut S4b), `terrain_check=True`."""
        from services.layout_planner import LayoutConstraints
        base = self.contract.layout_constraints
        if base is None:
            c = LayoutConstraints(terrain_check=True, replan_budget=self.contract.replan_budget)
        else:
            # Respecte base.terrain_check (back-compat S3d explicite) ; replan_budget du Contract.
            c = replace(base, replan_budget=self.contract.replan_budget)
        # constructible_zone depuis contract.zone (point/rayon -> bbox grossier ; None si absent).
        if c.constructible_zone is None and self.contract.zone is not None:
            zx, zy = self.contract.zone
            c = replace(c, constructible_zone=(int(zx) - 60, int(zy) - 60,
                                               int(zx) + 60, int(zy) + 60))
        return replace(c, belt_tier=belt_tier)