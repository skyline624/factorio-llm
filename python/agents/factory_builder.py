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