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

import math
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
    # S4c : arbitrage replan lourd FactoryBuilder. replan_budget = budget replan auto du
    # LayoutPlanner (S4b, shift offset/facing). layout_constraints = injection de contraintes
    # (tiers, beacons, terrain_check) ; None = défauts S4b (terrain_check=True). `zone` (ci-
    # dessus) est utilisé comme constructible_zone (conversion point/rayon -> bbox = S4c).
    replan_budget: int = 4
    layout_constraints: Optional[object] = None


# Offsets essayés pour poser un four près du character (cf. test_full.py:120).
_FURNACE_OFFSETS = [(2, 0), (0, 2), (-2, 0), (0, -2), (3, 1), (-1, 3), (2, 2), (-2, -2)]

# Combien de fois relire l'inventaire après un craft, et à quel rythme. Le jeu met la
# recette en FILE : l'objet arrive un instant plus tard, et conclure trop tôt annonce un
# échec sans cause pour une fabrication qui allait réussir (partie 42, mesuré : verdict à
# 11:16:26, objet en poche à 11:16:40). Trois relevés d'une seconde couvrent les crafts
# simples ; au-delà c'est que rien ne vient.
CRAFT_RELEVES = 3
CRAFT_PAUSE_S = 1.0


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
            # SEPTIÈME ENDROIT OÙ L'ARRÊT NE MORDAIT PAS. Partie 29, mesuré pendant que le
            # joueur regardait : arrêt demandé, et 2 min 19 plus tard le chantier tournait
            # toujours — immobile en (66,42), +3 minerais en six secondes, zéro machine
            # posée. Il ne posait pas (H42), ne forgeait pas entre deux pièces (H50), ne
            # marchait pas (H52) : il MINAIT. Se fournir exécute un plan d'étapes, et
            # cette boucle-ci ne croisait aucun point de sortie.
            #
            # Le joueur l'a vu avant nous : « pourtant il continue de miner a la main ».
            #
            # On sort ENTRE deux étapes. Celle qui a commencé va au bout — un `mine_entity`
            # coupé au milieu laisserait un compte partiel qu'aucun état ne décrit — mais
            # la suivante ne démarre pas, et l'appelant sait où l'on s'est arrêté.
            interrupteur = getattr(self, "interrompu_par", None)
            if interrupteur is not None:
                try:
                    doit = bool(interrupteur())
                except Exception:
                    doit = False
                if doit:
                    results.append({"ok": False, "detail": (
                        f"interrompu à la demande après {i} étape(s) sur {len(steps)} ; "
                        f"relance pour finir")})
                    print(f"[agent]   INTERROMPU avant {step.kind}")
                    break
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
            # Au-delà de l'horizon généré, `walk_to_entity` ne trouve pas de chemin :
            # le personnage s'arrête bien avant, et le minage suivant échoue sur
            # « cible hors portée ». Mesuré au bootstrap — charbon à 215 tuiles du fer,
            # l'agent bloqué faute de combustible. On génère alors devant soi et on
            # marche par bonds (services.deplacement).
            cible = perception.nearest(api, a["name"])
            if cible is not None:
                from services import deplacement
                if deplacement.distance(api, cible[0], cible[1]) > deplacement.PAS:
                    ax, ay = deplacement.marcher_vers(api, cible[0], cible[1])
                    reste = math.hypot(cible[0] - ax, cible[1] - ay)
                    # Les bonds franchissent la distance ; l'APPROCHE finale reste le
                    # métier de `walk_to_entity`, qui vise l'entité et non un point.
                    # Juger l'arrivée sur la seule distance au but faisait échouer une
                    # marche de 215 tuiles pour les quinze dernières — et le garde-fou
                    # d'acharnement abandonnait alors le charbon DÉFINITIVEMENT.
                    res = api.run_action(api.walk_to_entity, a["name"],
                                         a.get("radius", 300), timeout=120.0)
                    if isinstance(res, dict):
                        res["detail"] = (f"{reste:.0f} tuiles après les bonds "
                                         f"({ax:.0f},{ay:.0f}) puis approche : "
                                         f"{res.get('detail', '')}")
                    return res
            return api.run_action(api.walk_to_entity, a["name"], a.get("radius", 300),
                                  timeout=60.0)

        if k == "mine_entity":
            return api.run_action(api.mine_entity, a["name"], a["count"], timeout=90.0)

        if k == "place_furnace":
            return self._place_furnace_near()

        if k == "move_items":
            # C'EST L'INVENTAIRE QUI TRANCHE, PAS `ok`. Partie 40, mesuré :
            #
            #   essai 1   9 étapes toutes vertes   copper-plate : 10 -> 10 (+0)
            #   essai 2   6 étapes                 copper-plate : 10 -> 15 (+5)
            #
            # Seule différence : vingt charbons en poche. Le four du premier essai n'avait
            # pas de combustible. Le plan PRÉVOIT pourtant le charbon — mais sur un stock
            # SIMULÉ, et les cinq unités qu'il croyait disponibles venaient d'être versées
            # dans une foreuse. `move_items` a répondu `ok=True` en ne déplaçant rien, et
            # le rapport a rendu le pire des messages : tout vert, rien produit, aucune
            # cause. L'agent a mis deux minutes et deux chantiers à le deviner.
            #
            # Même leçon que les vingt-six poses fantômes de l'executor E1, à l'endroit
            # qu'elle n'avait pas couvert : elle valait pour ce qu'on pose, elle vaut
            # identiquement pour ce qu'on transfère.
            avant = perception.inventory(api).get(a["item"], 0)
            r = api.run_action(api.move_items, a["item"], "stone-furnace",
                               a["count"], a["to_entity"], timeout=20.0)
            if not a["to_entity"]:
                return r
            apres = perception.inventory(api).get(a["item"], 0)
            if apres >= avant:
                return {"ok": False,
                        "detail": (f"{a['item']} non versé dans le four : tu en avais "
                                   f"{avant} en poche, il en fallait {a['count']}")}
            return r

        if k == "wait":
            ticks = a["ticks"]
            return api.run_action(api.wait, ticks, timeout=max(20.0, ticks / 60.0 + 5))

        if k == "craft_item":
            # UN CRAFT SE MET EN FILE, IL NE S'EXÉCUTE PAS. Partie 42, mesuré :
            #
            #   11:16:26  ÉCHEC — burner-mining-drill : 0 -> 0 (+0) en 2 étape(s)
            #   11:16:40  inventaire : burner-mining-drill = 1
            #
            # `craft_item` rend `ok=True` dès la mise en file, et l'objet n'arrive qu'un
            # instant plus tard. On relisait trop tôt : le rapport annonçait un échec sans
            # cause pour une fabrication qui allait réussir, et l'agent repartait fabriquer
            # ce qu'il avait déjà.
            #
            # C'est la leçon d'E1 avec sa conséquence inverse : il ne suffit pas de RELIRE
            # l'inventaire, il faut lui laisser le temps d'arriver. Un verdict prématuré
            # est aussi faux qu'un `ok=True` cru sur parole.
            item, combien = a["item"], int(a["count"])
            avant = perception.inventory(api).get(item, 0)
            r = api.run_action(api.craft_item, item, combien, timeout=90.0)
            dort = getattr(self, "_dort", None) or __import__("time").sleep
            for _ in range(CRAFT_RELEVES):
                if perception.inventory(api).get(item, 0) > avant:
                    return r
                dort(CRAFT_PAUSE_S)
            apres = perception.inventory(api).get(item, 0)
            if apres > avant:
                return r
            return {"ok": False,
                    "detail": (f"{item} toujours à {apres} après {combien} craft(s) "
                               f"demandé(s) — la file n'a rien rendu : ingrédient manquant "
                               f"ou recette verrouillée")}

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