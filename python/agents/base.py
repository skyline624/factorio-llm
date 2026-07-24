"""BaseAgent — boucle perceive/decide/act + exécuteur de plan.

Contrat (objectif + zone) injecté (P1 : en dur par le test ; P2 : par le
coordinator). RCON partagé via ModApi injecté (dépendance, pas global — SOLID).
L'exécuteur déroule les Step du plan via ModApi.run_action (race-free via
completion_seq) ; fail-fast si une étape échoue (Enterprise-Grade : on ne
continue pas un plan dont une étape a échoué — l'agent peut alors redécider).

La décision est déléguée à un `Planner` injecté (services/llm) : `LLMPlanner`
pour la décision LLM (P1b), `DeterministicPlanner` pour le fallback / défaut
(P1 rétro-compatible). BaseAgent reste générique et ne dépend pas d'openai.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from core.mod_api import ModApi
from core.state import GameState
from services import perception
from services.knowledge import ProductionGoal, Step
from services.llm import NextAction, StepPlanner


@dataclass
class Contract:
    """Objectif alloué à un agent. P1 : passé en dur par le test.
    P2 : injecté par le coordinator (zone allouée, contraintes).
    """
    goal: ProductionGoal
    zone: Optional[tuple[float, float]] = None


# Offsets essayés pour poser un four près du character (cf. test_full.py:120).
_FURNACE_OFFSETS = [(2, 0), (0, 2), (-2, 0), (0, -2), (3, 1), (-1, 3), (2, 2), (-2, -2)]


class BaseAgent:
    """Boucle perceive -> decide -> act. `decide` est à surcharger par métier.

    `planner` (optionnel) délègue la décision ; la sous-classe métier (ex.
    FactoryBuilder) construit un planner par défaut si None (lié à l'API pour
    résoudre les recettes). `last_plan` expose le PlanResult pour logs/tests.
    """

    def __init__(self, api: ModApi, contract: Contract, planner=None):
        self.api = api
        self.contract = contract
        self.planner = planner
        # Dernier résultat de planification (source/reason) — diagnostic tests/logs.
        self.last_plan = None
        # Mémoire d'exécution courte (positions posées) pour les étapes suivantes.
        self._furnace_pos: Optional[tuple[float, float]] = None

    # ----- boucle -----

    def perceive(self) -> GameState:
        return perception.snapshot(self.api)

    def decide(self, gs: GameState) -> list[Step]:
        raise NotImplementedError

    def act(self, steps: list[Step]) -> list[dict]:
        """Exécute les étapes dans l'ordre. Fail-fast à la 1re étape échouée."""
        results: list[dict] = []
        for i, step in enumerate(steps):
            print(f"[agent]   {i + 1}/{len(steps)} {step.kind} {step.args}")
            res = self._execute(step)
            results.append(res)
            ok = isinstance(res, dict) and res.get("ok") is True
            if not ok:
                print(f"[agent]   ECHEC etape {step.kind}: {res}")
                break
        return results

    def run(self) -> list[dict]:
        """Cycle complet perceive -> decide -> act, avec logs structurés."""
        gs = self.perceive()
        steps = self.decide(gs)
        from services.knowledge import plan_summary
        src = getattr(self.last_plan, "source", "?")
        reason = getattr(self.last_plan, "reason", "")
        print(f"[agent] === plan source={src} ({len(steps)} etapes) "
              f"[{plan_summary(steps)}] pour {self.contract.goal.item} "
              f"x{self.contract.goal.count} reason={reason} ===")
        return self.act(steps)

    def run_loop(self, stepplanner: StepPlanner, ctx, max_iters: int = 30,
                 max_consec_fails: int = 5) -> list[dict]:
        """Boucle d'actions LLM pas-à-pas (P1c).

        A chaque itération : perceive -> si objectif atteint, stop ; sinon le
        StepPlanner choisit UNE action, on l'exécute via `_execute` (mécanique
        déterministe Lua), on mémorise le résultat pour le tour suivant.

        Replanification : sur échec d'action, on NE stoppe pas — le LLM voit le
        résultat + le nouvel état au tour suivant et choisit une autre action.
        Anti-boucle : `max_iters` (budget total) + `max_consec_fails` (échecs
        consécutifs). Fallback déterministe final via `act()` sur le plan
        `knowledge.plan_production` si l'objectif n'est pas atteint (garantie de
        bout en bout — l'agent ne manque jamais l'objectif si physiquement
        possible).
        """
        goal = self.contract.goal
        results: list[dict] = []
        consec_fails = 0
        last_result: dict = {"ok": None, "detail": "premier tour"}
        # Anti-oscillation : dernier ore vers lequel on a marché (ok) sans
        # mine_entity(ok) ensuite. Si le LLM réémet find/walk sur ce même ore,
        # on l'avertit (injection dans last_result) sans exécuter l'action —
        # garde-fou mécanique contre la boucle "LLM stupide" (cf. arch-choices).
        walked_for: Optional[str] = None

        for i in range(max_iters):
            gs = self.perceive()
            if gs.inventory.get(goal.item, 0) >= goal.count:
                print(f"[agent] objectif atteint en {i} iterations")
                break
            decision = stepplanner.next_action(goal, dict(gs.inventory),
                                               gs.pos_tuple(), ctx, last_result)
            status = decision.status

            if status == "stop":
                print(f"[agent] LLM stop apres {i} iterations")
                break
            if status == "fallback":
                print(f"[agent] LLM injoignable -> fallback deterministe "
                      f"({decision.motif})")
                break
            if status == "invalid":
                consec_fails += 1
                last_result = {"ok": False,
                               "detail": f"action invalide: {decision.motif}"}
                print(f"[agent] iteration {i + 1} action invalide: "
                      f"{decision.motif} (replanification)")
                if consec_fails >= max_consec_fails:
                    print(f"[agent] {max_consec_fails} echecs consecutifs "
                          f"-> fallback")
                    break
                continue

            step = decision.step
            # Détection d'oscillation find/walk répétés sur un ore déjà ciblé.
            name = step.args.get("name") if isinstance(step.args, dict) else None
            if (step.kind in ("find_nearest", "walk_to_entity")
                    and name is not None and name == walked_for):
                last_result = {
                    "ok": False,
                    "detail": (f"DEJA a cote de {walked_for} (walk_to_entity "
                               f"reussi avant). NE repete PAS find/walk. Action "
                               f"suivante: mine_entity(name={walked_for}, "
                               f"count=<N>)."),
                }
                consec_fails += 1
                print(f"[agent] iteration {i + 1} oscillation {step.kind} "
                      f"sur {walked_for} -> avertissement (replanification)")
                if consec_fails >= max_consec_fails:
                    print(f"[agent] {max_consec_fails} echecs consecutifs "
                          f"-> fallback")
                    break
                continue  # on n'exécute pas l'action répétée

            print(f"[agent] iteration {i + 1}/{max_iters} {step.kind} {step.args}")
            res = self._execute(step)
            results.append(res)
            last_result = res if isinstance(res, dict) else {"ok": False, "detail": str(res)}
            if last_result.get("ok") is True:
                consec_fails = 0
                if step.kind == "walk_to_entity" and name is not None:
                    walked_for = name
                elif step.kind == "mine_entity":
                    walked_for = None
            else:
                consec_fails += 1
                print(f"[agent] iteration {i + 1} echec: {res} (replanification)")
                if consec_fails >= max_consec_fails:
                    print(f"[agent] {max_consec_fails} echecs consecutifs "
                          f"-> fallback")
                    break

        # Verification objectif + fallback deterministe final (garantie).
        gs = self.perceive()
        if gs.inventory.get(goal.item, 0) < goal.count:
            print("[agent] objectif non atteint -> fallback deterministe final")
            from services.llm import DeterministicPlanner
            det = DeterministicPlanner(self._fallback_recipe_lookup())
            det_res = det.plan(goal, dict(gs.inventory), ctx)
            self.last_plan = det_res
            print(f"[agent] fallback plan source={det_res.source} "
                  f"({len(det_res.steps)} etapes)")
            results += self.act(det_res.steps)
        return results

    def _fallback_recipe_lookup(self):
        """Résolution de recette pour le fallback déterministe final.
        Par défaut None (recettes hardcoded de knowledge) ; surchargé par les
        sous-classes liées à l'API (ex. FactoryBuilder) pour perception réelle.
        """
        return None

    # ----- exécuteur (mapping Step.kind -> ModApi) -----

    def _execute(self, step: Step) -> dict:
        api = self.api
        k = step.kind
        a = step.args

        if k == "find_nearest":
            # Observation synchrone (pas async via task_manager) : on mémorise.
            pos = perception.nearest(api, a["name"])
            return {"ok": pos is not None, "detail": f"nearest {a['name']} = {pos}"}

        if k == "walk_to_entity":
            return api.run_action(api.walk_to_entity, a["name"], a.get("radius", 300),
                                  timeout=60.0)

        if k == "mine_entity":
            return api.run_action(api.mine_entity, a["name"], a["count"], timeout=90.0)

        if k == "place_furnace":
            return self._place_furnace_near()

        if k == "move_items":
            return api.run_action(api.move_items, a["item"], "stone-furnace",
                                  a["count"], a["to_entity"], timeout=20.0)

        if k == "wait":
            ticks = a["ticks"]
            return api.run_action(api.wait, ticks, timeout=max(20.0, ticks / 60.0 + 5))

        if k == "craft_item":
            return api.run_action(api.craft_item, a["item"], a["count"], timeout=90.0)

        return {"ok": False, "detail": f"kind inconnu: {k}"}

    def _place_furnace_near(self) -> dict:
        """Pose un stone-furnace du kit près du character (essaie des offsets)."""
        px, py = perception.position(self.api) or (None, None)
        if px is None:
            return {"ok": False, "detail": "aucun avatar pour poser le four"}
        for ox, oy in _FURNACE_OFFSETS:
            res = self.api.run_action(self.api.place_entity_at,
                                      "stone-furnace", px + ox, py + oy, "north",
                                      timeout=10.0)
            if isinstance(res, dict) and res.get("ok"):
                self._furnace_pos = (px + ox, py + oy)
                return res
        return {"ok": False, "detail": "aucune position libre pour le four"}