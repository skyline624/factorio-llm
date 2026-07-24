"""Planificateur LLM — décision FactoryBuilder P1b.

Remplace la décision déterministe (`knowledge.plan_production`) par un plan
produit par un LLM OpenAI-compatible (Ollama /v1), tout en gardant le déterministe
comme **fallback** robustesse (Enterprise-Grade : jamais de crash, jamais de plan
invalide exécuté). Principe mémoire [[factorio-llm-arch-choices]] : « LLM stupide
(choix d'opération/params) + déterminisme mécanique côté Lua » — le LLM ne fait
QUE choisir la séquence ordonnée de Steps ; l'exécution physique reste déterministe
via `BaseAgent.act`/`run_action`.

Architecture (DIP) : FactoryBuilder ne dépend que de l'interface `Planner`
(Protocol). Ce module est le SEUL à importer `openai` ; il fournit :
  - `LLMPlanner` : produit un plan via LLM (function-calling `emit_plan`),
    valide chaque Step, retombe sur le déterministe si LLM injoignable ou plan
    invalide.
  - `DeterministicPlanner` : wrap `knowledge.plan_production` (fallback ET
    planner par défaut pour la rétro-compatibilité P1).
  - `validate_plan` : validation pure Python (10 règles + simulation fidèle du
    four + craft). Testable sans LLM ni serveur.

P1b = chaîne fer uniquement : énumération finie d'items/kinds (anti-hallucination).
Extensible en P2+ (copper, circuits, oil → élargir les whitelists).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Optional, Protocol

from config import Config
from services import knowledge
from services.knowledge import ProductionGoal, Step, _credit


# ===== Whitelists P1 (énumération finie anti-hallucination) =====

MINEABLE: frozenset[str] = frozenset({"iron-ore", "copper-ore", "coal", "stone"})
SMELTABLE: frozenset[str] = frozenset({"iron-plate", "copper-plate", "stone-brick"})
CRAFTABLE: frozenset[str] = frozenset({"iron-gear-wheel"})
# Ores smelt -> plaque (table de smelting : ore -> plate output).
SMELT_FROM_TO: dict[str, str] = {
    "iron-ore": "iron-plate",
    "copper-ore": "copper-plate",
    "stone": "stone-brick",
}
# Items que move_items peut pousser vers le four (combustible + ore).
FURNACE_INPUTS: frozenset[str] = frozenset({"coal", "iron-ore", "copper-ore", "stone"})
# Items que move_items peut récupérer depuis le four (plaques).
FURNACE_OUTPUTS: frozenset[str] = frozenset(SMELTABLE)
# Capacité réelle d'un coal dans un stone-furnace : 1 coal = 4MJ / 90kW / 1.6s ≈ 27
# plaques. Le prompt LLM dit "1 coal ≈ 1 plaque" (conservatif, pousse à en mettre
# assez) ; la simulation de validation utilise le ratio RÉEL pour ne pas rejeter
# un plan valide (ex: coal=5 smelt 10 ore — validé 13/13 en jeu).
SMELT_PLATES_PER_COAL = 27

KINDS: frozenset[str] = frozenset({
    "find_nearest", "walk_to_entity", "mine_entity", "place_furnace",
    "move_items", "wait", "craft_item",
})

# Bornes anti-boucle / anti-plan absurde.
MAX_STEPS = 40
MAX_FIND_PER_NAME = 3
MAX_TICKS = 200_000
MAX_WAIT_TICKS = 200_000


# ===== Données du contexte passé au planner =====

@dataclass
class PlanContext:
    """Contexte figé passé à Planner.plan (state + recette + kit + contraintes)."""
    position: Optional[tuple[float, float]] = None
    goal_recipe: Optional[list[tuple[str, int]]] = None  # recette du goal.item
    kit: dict[str, int] = field(default_factory=dict)
    constraints: dict = field(default_factory=dict)  # smelt_ticks_per_plate, etc.


@dataclass
class PlanResult:
    """Résultat d'un Planner.plan : steps + provenance + motif (logs/tests)."""
    steps: list[Step]
    source: str = "det"      # "llm" | "det"
    reason: str = ""         # motif si fallback / rejet


class Planner(Protocol):
    """Interface consommée par FactoryBuilder (DIP : pas de dépendance à openai)."""
    def plan(self, goal: ProductionGoal, inv: dict, ctx: PlanContext) -> PlanResult: ...


# ===== Schéma de function-calling (force le LLM à émettre un plan structuré) =====

EMIT_PLAN_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "emit_plan",
        "description": (
            "Produit le plan complet ordonné des étapes exécutables pour atteindre "
            "l'objectif de production. Doit être appelé obligatoirement."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["steps"],
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 0,
                    "maxItems": MAX_STEPS,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["kind", "args"],
                        "properties": {
                            "kind": {
                                "type": "string",
                                "enum": sorted(KINDS),
                            },
                            "args": {
                                "type": "object",
                                "additionalProperties": False,
                                "properties": {
                                    "name": {"type": "string"},
                                    "item": {"type": "string"},
                                    "count": {"type": "integer", "minimum": 1},
                                    "ticks": {"type": "integer", "minimum": 1,
                                              "maximum": MAX_WAIT_TICKS},
                                    "to_entity": {"type": "boolean"},
                                    "radius": {"type": "number", "minimum": 1,
                                               "maximum": 400},
                                },
                            },
                        },
                    },
                },
            },
        },
    },
}


# ===== Validation pure Python (testable sans LLM ni serveur) =====

def _is_int(v) -> bool:
    # bool est un sous-type d'int en Python -> on l'exclut explicitement.
    return isinstance(v, int) and not isinstance(v, bool)


def _args_ok(kind: str, a: dict) -> Optional[str]:
    """Vérifie les args attendus par kind. Retourne None si OK, un motif sinon."""
    if not isinstance(a, dict):
        return "args n'est pas un dict"

    if kind == "find_nearest":
        if not isinstance(a.get("name"), str) or not a["name"]:
            return "find_nearest: name str requis"
    elif kind == "walk_to_entity":
        if not isinstance(a.get("name"), str) or not a["name"]:
            return "walk_to_entity: name str requis"
        r = a.get("radius", 300)
        if not isinstance(r, (int, float)) or isinstance(r, bool) or not (1 <= r <= 400):
            return "walk_to_entity: radius invalide"
    elif kind == "mine_entity":
        if not isinstance(a.get("name"), str) or not a["name"]:
            return "mine_entity: name str requis"
        if not _is_int(a.get("count")) or a["count"] < 1:
            return "mine_entity: count int>=1 requis"
    elif kind == "place_furnace":
        pass  # aucun arg requis
    elif kind == "move_items":
        if not isinstance(a.get("item"), str) or not a["item"]:
            return "move_items: item str requis"
        if not _is_int(a.get("count")) or a["count"] < 1:
            return "move_items: count int>=1 requis"
        if not isinstance(a.get("to_entity"), bool):
            return "move_items: to_entity bool requis"
    elif kind == "wait":
        if not _is_int(a.get("ticks")) or not (1 <= a["ticks"] <= MAX_WAIT_TICKS):
            return "wait: ticks int [1, 200000] requis"
    elif kind == "craft_item":
        if not isinstance(a.get("item"), str) or not a["item"]:
            return "craft_item: item str requis"
        if not _is_int(a.get("count")) or a["count"] < 1:
            return "craft_item: count int>=1 requis"
    return None


def validate_plan(raw_steps, ctx: PlanContext, goal: ProductionGoal,
                  inv: dict) -> tuple[bool, list[Step], str]:
    """Valide un plan brut (liste de dicts {kind, args}).

    Retourne (ok, steps_typés, motif). `ok=False` => l'appelant doit retomber sur
    le planificateur déterministe. Aucun effet de bord sur `inv`.
    """
    if not isinstance(raw_steps, list):
        return False, [], "raw_steps n'est pas une liste"
    if len(raw_steps) > MAX_STEPS:
        return False, [], f"plan > {MAX_STEPS} steps"

    steps: list[Step] = []
    seen_find: dict[str, int] = {}
    furnace_active = False
    walked_to: set[str] = set()      # noms vers lesquels on a marché
    found: set[str] = set()          # noms cherchés via find_nearest
    total_ticks = 0

    for i, raw in enumerate(raw_steps):
        if not isinstance(raw, dict) or "kind" not in raw or "args" not in raw:
            return False, [], f"step {i}: manque kind/args"
        kind = raw["kind"]
        a = raw["args"]
        if kind not in KINDS:
            return False, [], f"step {i}: kind inconnu {kind!r}"
        err = _args_ok(kind, a)
        if err:
            return False, [], f"step {i}: {err}"

        # --- cohérence sémantique + séquentielle par kind ---
        if kind == "find_nearest":
            nm = a["name"]
            seen_find[nm] = seen_find.get(nm, 0) + 1
            if seen_find[nm] > MAX_FIND_PER_NAME:
                return False, [], f"trop de find_nearest({nm})"
            found.add(nm)

        elif kind == "walk_to_entity":
            walked_to.add(a["name"])

        elif kind == "mine_entity":
            nm = a["name"]
            if nm not in MINEABLE:
                return False, [], f"mine_entity: {nm!r} non minable"
            if nm not in walked_to:
                return False, [], f"mine_entity({nm}) sans walk_to_entity préalable"
            if nm not in found:
                return False, [], f"mine_entity({nm}) sans find_nearest préalable"

        elif kind == "place_furnace":
            if furnace_active:
                return False, [], "double place_furnace (un seul four requis)"
            furnace_active = True

        elif kind == "move_items":
            item = a["item"]
            to_entity = a["to_entity"]
            if to_entity:
                if item not in FURNACE_INPUTS:
                    return False, [], f"move_items vers four: {item!r} non valide (coal/ore)"
                if not furnace_active:
                    return False, [], "move_items vers four sans place_furnace"
            else:
                if item not in FURNACE_OUTPUTS:
                    return False, [], f"move_items depuis four: {item!r} non valide (plaque)"
                if not furnace_active:
                    return False, [], "move_items depuis four sans place_furnace"

        elif kind == "wait":
            total_ticks += a["ticks"]

        elif kind == "craft_item":
            if a["item"] not in CRAFTABLE:
                return False, [], f"craft_item: {a['item']!r} non craftable P1"

        steps.append(Step(kind, dict(a)))

    # plan vide autorisé seulement si l'objectif est déjà satisfait
    if not steps:
        if inv.get(goal.item, 0) >= goal.count:
            return True, [], ""
        return False, [], "plan vide mais objectif non satisfait"

    if total_ticks > MAX_TICKS:
        return False, [], f"sum(ticks)={total_ticks} > {MAX_TICKS}"

    # --- simulation fidèle (suffisance + faisabilité) ---
    ok, motif = _simulate(steps, ctx, goal, inv)
    if not ok:
        return False, [], motif
    return True, steps, ""


def _simulate(steps: list[Step], ctx: PlanContext, goal: ProductionGoal,
              inv: dict) -> tuple[bool, str]:
    """Simule le plan sur un inventaire symbolique (mine/smelt/craft) pour vérifier
    qu'il aboutit à `goal.count` de `goal.item`. Réutilise `knowledge._credit` (DRY).

    Modélise le four (ore/coal/plate) pour ne pas créditer des plaques non smeltées.
    """
    sim: dict[str, int] = dict(inv)
    furnace: Optional[dict[str, int]] = None  # {coal, <ore>, <plate>}

    # Recettes connues : celle du goal (P1b = seul craft est goal.item).
    recipes: dict[str, list[tuple[str, int]]] = {}
    if ctx.goal_recipe and goal.item in CRAFTABLE:
        recipes[goal.item] = ctx.goal_recipe

    for i, s in enumerate(steps):
        k, a = s.kind, s.args
        if k == "mine_entity":
            _credit(sim, a["name"], a["count"])

        elif k == "place_furnace":
            furnace = {"coal": 0}

        elif k == "move_items":
            item, count, to_entity = a["item"], a["count"], a["to_entity"]
            if to_entity:
                # inv -> four
                take = min(sim.get(item, 0), count)
                sim[item] = sim.get(item, 0) - take
                if furnace is not None:
                    if item == "coal":
                        furnace["coal"] = furnace.get("coal", 0) + take
                    else:
                        furnace[item] = furnace.get(item, 0) + take
            else:
                # four -> inv : limité par ce qui a été smelté
                if furnace is None:
                    return False, f"step {i}: récupère {item} sans four"
                avail = furnace.get(item, 0)
                if avail <= 0:
                    return False, f"step {i}: récupère {item} sans l'avoir smelté"
                take = min(avail, count)
                furnace[item] = avail - take
                _credit(sim, item, take)

        elif k == "wait":
            if furnace is not None:
                # smelt : 1 ore -> 1 plaque ; limité par l'ore et la capacité du
                # coal (1 coal ≈ SMELT_PLATES_PER_COAL plaques en réalité).
                for ore, plate in SMELT_FROM_TO.items():
                    n_ore = furnace.get(ore, 0)
                    if n_ore <= 0:
                        continue
                    coal_cap = furnace.get("coal", 0) * SMELT_PLATES_PER_COAL
                    n = min(n_ore, coal_cap)
                    if n <= 0:
                        continue
                    furnace[ore] = n_ore - n
                    # consomme le coal au ratio réel (arrondi supérieur).
                    furnace["coal"] = max(0, furnace.get("coal", 0) - (n + SMELT_PLATES_PER_COAL - 1) // SMELT_PLATES_PER_COAL)
                    furnace[plate] = furnace.get(plate, 0) + n

        elif k == "craft_item":
            item, count = a["item"], a["count"]
            recipe = recipes.get(item)
            if recipe is None:
                return False, f"step {i}: recette inconnue pour {item!r}"
            for ing_name, ing_amt in recipe:
                need = ing_amt * count
                if sim.get(ing_name, 0) < need:
                    return False, (f"step {i}: craft {item} x{count} manque "
                                   f"{ing_name} ({sim.get(ing_name, 0)}/{need})")
                sim[ing_name] = sim.get(ing_name, 0) - need
            _credit(sim, item, count)

    if sim.get(goal.item, 0) < goal.count:
        return False, (f"objectif non atteint: {sim.get(goal.item, 0)}/{goal.count} "
                       f"{goal.item}")
    return True, ""


# ===== Prompt LLM =====

SYSTEM_PROMPT = """Tu es un planificateur de production Factorio. Tu reçois un objectif de production et l'etat de l'avatar IA. Tu DOIS appeler l'outil `emit_plan` avec la liste ORDONNEE des etapes (steps) a executer sequentiellement. Tu n'inventes JAMAIS d'items ni de kinds.

ITEMS AUTORISES (ne cite AUCUN autre item) :
- mine : iron-ore, copper-ore, coal, stone
- smelt (plaques) : iron-plate, copper-plate, stone-brick
- craft : iron-gear-wheel

KINDS AUTORISES et leurs arguments (utilise EXACTEMENT la bonne cle, ne confonds pas `name` et `item`) :
- find_nearest   : {\"name\": <item a miner>}
- walk_to_entity : {\"name\": <item a miner>}
- mine_entity    : {\"name\": <ore>, \"count\": <int>=1>}
- place_furnace  : {}   (pose un stone-furnace du kit pres du personnage)
- move_items     : {\"item\": <item>, \"count\": <int>=1>, \"to_entity\": <bool>}  (to_entity=true : joueur->four ; false : four->joueur)
- wait           : {\"ticks\": <int>=1>}
- craft_item     : {\"item\": <item craftable>, \"count\": <int>=1}   (UTILISE `item`, PAS `name`)

REGLES PHYSIQUES (le mod execute, tu ne fais QUE choisir la sequence) :
- 1 iron-ore -> 1 iron-plate dans un stone-furnace. Smelt ~220 ticks/plaque, minimum 600 ticks. 1 coal smelt plusieurs plaques (mets count=5 de coal).
- iron-gear-wheel : 2 iron-plate -> 1 gear (1 craft).
- walk + mine sont ANIMES (non instantanes). Un four doit etre POSE avant d'y mettre des items.
- Avant mine_entity(name) : find_nearest(name) puis walk_to_entity(name).
- Pour smelter : place_furnace, move_items(coal, to_entity=true, count=5), move_items(ore, to_entity=true, count=M), wait(ticks=...), move_items(plate, to_entity=false, count=K).

EXEMPLE 1 (stock suffisant, craft direct) — inventaire iron-plate=50, objectif 5 iron-gear-wheel :
  steps=[{\"kind\":\"craft_item\",\"args\":{\"item\":\"iron-gear-wheel\",\"count\":5}}]

EXEMPLE 2 (stock insuffisant, produire) — inventaire iron-plate=40,coal=100, objectif 25 iron-gear-wheel (besoin 50 plaques, manquent 10) :
  steps=[{\"kind\":\"find_nearest\",\"args\":{\"name\":\"iron-ore\"}},{\"kind\":\"walk_to_entity\",\"args\":{\"name\":\"iron-ore\"}},{\"kind\":\"mine_entity\",\"args\":{\"name\":\"iron-ore\",\"count\":10}},{\"kind\":\"place_furnace\",\"args\":{}},{\"kind\":\"move_items\",\"args\":{\"item\":\"coal\",\"count\":5,\"to_entity\":true}},{\"kind\":\"move_items\",\"args\":{\"item\":\"iron-ore\",\"count\":10,\"to_entity\":true}},{\"kind\":\"wait\",\"args\":{\"ticks\":2200}},{\"kind\":\"move_items\",\"args\":{\"item\":\"iron-plate\",\"count\":10,\"to_entity\":false}},{\"kind\":\"craft_item\",\"args\":{\"item\":\"iron-gear-wheel\",\"count\":25}}]

Si l'inventaire contient deja assez d'iron-plate pour le craft final, retourne uniquement le craft_item (pas de minage, pas de smelt). Si l'objectif est deja atteint, retourne steps=[]."""


def _build_user_message(goal: ProductionGoal, inv: dict, ctx: PlanContext) -> str:
    pos = ctx.position if ctx.position is not None else "inconnue"
    recipe = ctx.goal_recipe if ctx.goal_recipe is not None else "inconnue"
    kit = ", ".join(f"{k}={v}" for k, v in sorted(ctx.kit.items())) or "vide"
    inv_s = ", ".join(f"{k}={v}" for k, v in sorted(inv.items())) or "vide"
    return (
        f"OBJECTIF : produire {goal.count} x {goal.item}\n"
        f"POSITION : {pos}\n"
        f"INVENTAIRE : {inv_s}\n"
        f"RECETTE OBJECTIF : {recipe}\n"
        f"CONSTRAINTES : smelt_ticks_per_plate=220, smelt_min=600, coal_per_plate=1\n"
        f"KIT DEPART : {kit}\n"
        f"Retourne le plan via emit_plan."
    )


# ===== LLMPlanner =====

class LLMPlanner:
    """Planner LLM OpenAI-compatible (Ollama /v1) avec fallback déterministe.

    Fail-soft : toute erreur (réseau, modèle, tool_call absent, plan invalide)
    retombe sur `DeterministicPlanner.plan` — l'agent n'échoue jamais à décider.
    """

    def __init__(self, cfg: Config, recipe_lookup=None):
        self.cfg = cfg
        self._recipe_lookup = recipe_lookup  # pour le fallback déterministe
        self._det = DeterministicPlanner(recipe_lookup)
        # Import local (seul point du projet qui importe openai) — DIP.
        try:
            import openai
            self._openai = openai
        except ImportError as e:  # openai non installé -> fallback permanent
            self._openai = None
            self._import_err = str(e)
        else:
            self._import_err = None
            if cfg.openai_base_url and cfg.llm_enabled:
                self._client = openai.OpenAI(
                    api_key=cfg.openai_api_key or "ollama",
                    base_url=cfg.openai_base_url,
                    timeout=cfg.llm_timeout,
                )
            else:
                self._client = None

    def plan(self, goal: ProductionGoal, inv: dict, ctx: PlanContext) -> PlanResult:
        if self._openai is None:
            return self._fallback(goal, inv, ctx, f"openai indisponible: {self._import_err}")
        if self._client is None:
            return self._fallback(goal, inv, ctx, "llm disabled (base_url vide ou LLM_ENABLED=false)")

        try:
            resp = self._client.chat.completions.create(
                model=self.cfg.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": _build_user_message(goal, inv, ctx)},
                ],
                tools=[EMIT_PLAN_TOOL],
                tool_choice={"type": "function", "function": {"name": "emit_plan"}},
                max_tokens=self.cfg.llm_max_tokens,
            )
        except Exception as e:  # APIConnectionError, APITimeoutError, BadRequestError, NotFoundError, ...
            return self._fallback(goal, inv, ctx, f"llm unreachable: {type(e).__name__}: {e}")

        tool_calls = getattr(resp.choices[0].message, "tool_calls", None)
        if not tool_calls:
            return self._fallback(goal, inv, ctx, "llm no tool_call")
        try:
            args_json = tool_calls[0].function.arguments
            raw = json.loads(args_json) if isinstance(args_json, str) else args_json
            raw_steps = raw.get("steps", []) if isinstance(raw, dict) else []
        except (json.JSONDecodeError, AttributeError, IndexError) as e:
            return self._fallback(goal, inv, ctx, f"llm tool_call illisible: {e}")

        ok, steps, motif = validate_plan(raw_steps, ctx, goal, inv)
        if not ok:
            print(f"[fl] plan llm rejete: {motif}")
            return self._fallback(goal, inv, ctx, f"plan llm rejete: {motif}")

        print(f"[fl] plan llm valide: {len(steps)} etapes")
        return PlanResult(steps, source="llm")

    def _fallback(self, goal: ProductionGoal, inv: dict, ctx: PlanContext,
                  reason: str) -> PlanResult:
        print(f"[fl] fallback deterministe: {reason}")
        res = self._det.plan(goal, inv, ctx)
        res.reason = reason
        return res


# ===== DeterministicPlanner (fallback + défaut rétro-compatible P1) =====

class DeterministicPlanner:
    """Wrap `knowledge.plan_production` dans un PlanResult(source='det').

    DRY : un seul planificateur déterministe (services/knowledge). Sert de
    fallback pour LLMPlanner ET de planner par défaut quand aucun planner LLM
    n'est injecté (rétro-compatibilité P1 : tests existants sans `planner`).
    """

    def __init__(self, recipe_lookup=None):
        # recipe_lookup : callable (item) -> list[(name, count)] | None.
        # Par défaut on ne peut pas résoudre de recette (le caller doit injecter
        # perception.recipe_of). Si None, plan_production lèvera sur craft.
        self._recipe_lookup = recipe_lookup

    def plan(self, goal: ProductionGoal, inv: dict, ctx: PlanContext) -> PlanResult:
        steps = knowledge.plan_production(goal, dict(inv), self._recipe_lookup)
        return PlanResult(steps, source="det")


# =====================================================================
# P1c — boucle d'actions LLM pas-à-pas (1 action par tour, replanification)
# =====================================================================

@dataclass
class NextAction:
    """Décision d'un StepPlanner pour un tour de boucle.

    status :
      - "action"   : exécuter `step` (Step valide).
      - "stop"     : le LLM déclare l'objectif atteint -> la boucle s'arrête.
      - "fallback" : LLM injoignable -> la boucle bascule en déterministe final.
      - "invalid"  : action invalide (motif) -> la boucle log + continue
                     (compte comme un échec ; le LLM verra l'erreur au tour
                     suivant via last_result et replanifiera).
    """
    step: Optional[Step] = None
    status: str = "action"
    motif: str = ""


class StepPlanner(Protocol):
    """Grain 1 action (distinct de `Planner` plan complet). Consommé par
    BaseAgent.run_loop. DIP : BaseAgent ne dépend pas d'openai."""
    def next_action(self, goal: ProductionGoal, inv: dict, position,
                    ctx: PlanContext, last_result) -> NextAction: ...


# Schémas de function-calling P1c (2 tools, tool_choice="auto").
EMIT_ACTION_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "emit_action",
        "description": (
            "Émet UNE action à exécuter maintenant (un seul tour de boucle)."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "required": ["action"],
            "properties": {
                "action": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["kind", "args"],
                    "properties": {
                        "kind": {"type": "string", "enum": sorted(KINDS)},
                        "args": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "name": {"type": "string"},
                                "item": {"type": "string"},
                                "count": {"type": "integer", "minimum": 1},
                                "ticks": {"type": "integer", "minimum": 1,
                                          "maximum": MAX_WAIT_TICKS},
                                "to_entity": {"type": "boolean"},
                                "radius": {"type": "number", "minimum": 1,
                                           "maximum": 400},
                            },
                        },
                    },
                },
            },
        },
    },
}

STOP_TOOL: dict = {
    "type": "function",
    "function": {
        "name": "stop",
        "description": (
            "Déclare l'objectif atteint (l'inventaire contient assez de l'item "
            "cible). Arrête la boucle."
        ),
        "parameters": {"type": "object", "additionalProperties": False,
                       "properties": {}},
    },
}


def validate_step(raw, ctx: PlanContext, goal: ProductionGoal,
                  inv: dict) -> tuple[bool, Optional[Step], str]:
    """Valide UNE action (sous-ensemble de validate_plan : règles 1-5).

    Pas de cohérence séquentielle ni de simulation (la boucle construit le plan
    pas à pas et le LLM observe l'état réel). Réutilise `_args_ok` + whitelists.
    """
    if not isinstance(raw, dict) or "kind" not in raw or "args" not in raw:
        return False, None, "action manque kind/args"
    kind = raw["kind"]
    a = raw["args"]
    if kind not in KINDS:
        return False, None, f"kind inconnu {kind!r}"
    err = _args_ok(kind, a)
    if err:
        return False, None, err
    # cohérence sémantique (whitelist items)
    if kind in ("find_nearest", "walk_to_entity"):
        if a["name"] not in MINEABLE:
            return False, None, f"{kind}: {a['name']!r} non minable"
    elif kind == "mine_entity":
        if a["name"] not in MINEABLE:
            return False, None, f"mine_entity: {a['name']!r} non minable"
    elif kind == "move_items":
        item, to_entity = a["item"], a["to_entity"]
        if to_entity:
            if item not in FURNACE_INPUTS:
                return False, None, f"move_items vers four: {item!r} non valide"
        else:
            if item not in FURNACE_OUTPUTS:
                return False, None, f"move_items depuis four: {item!r} non valide"
    elif kind == "craft_item":
        if a["item"] not in CRAFTABLE:
            return False, None, f"craft_item: {a['item']!r} non craftable P1"
    return True, Step(kind, dict(a)), ""


SYSTEM_PROMPT_LOOP = """Tu es un planificateur de production Factorio en boucle. A CHAQUE tour, tu choisis UNE seule action à exécuter maintenant, puis tu verras le résultat et l'état mis à jour au tour suivant. Tu n'inventes JAMAIS d'items ni de kinds.

ITEMS AUTORISES :
- mine : iron-ore, copper-ore, coal, stone
- smelt (plaques) : iron-plate, copper-plate, stone-brick
- craft : iron-gear-wheel

KINDS AUTORISES et leurs arguments (utilise EXACTEMENT la bonne cle, ne confonds pas `name` et `item`) :
- find_nearest   : {\"name\": <item a miner>}
- walk_to_entity : {\"name\": <item a miner>}
- mine_entity    : {\"name\": <ore>, \"count\": <int>=1>}
- place_furnace  : {}
- move_items     : {\"item\": <item>, \"count\": <int>=1>, \"to_entity\": <bool>}  (true : joueur->four ; false : four->joueur)
- wait           : {\"ticks\": <int>=1>}
- craft_item     : {\"item\": <item craftable>, \"count\": <int>=1}   (UTILISE `item`, PAS `name`)

REGLES PHYSIQUES (le mod execute, tu ne fais QUE choisir 1 action) :
- 1 iron-ore -> 1 iron-plate dans un stone-furnace. Smelt ~220 ticks/plaque, minimum 600 ticks. 1 coal smelt plusieurs plaques (mets count=5 de coal).
- iron-gear-wheel : 2 iron-plate -> 1 gear.
- walk + mine sont ANIMES. Un four doit etre POSE avant d'y mettre des items.

PROGRESSION OBLIGATOIRE (AVANCE, ne tourne pas en boucle) :
Pour produire des iron-gear-wheel quand l'inventaire n'a pas assez d'iron-plate, suis CETTE séquence stricte, UNE action par tour, sans JAMAIS revenir en arrière :
  1. find_nearest(name=iron-ore)
  2. walk_to_entity(name=iron-ore)
  3. mine_entity(name=iron-ore, count=<le total d'ore nécessaire, en une seule fois>)
  4. place_furnace
  5. move_items(item=coal, count=5, to_entity=true)
  6. move_items(item=iron-ore, count=<M>, to_entity=true)
  7. wait(ticks=2200)
  8. move_items(item=iron-plate, count=<K>, to_entity=false)
  9. craft_item(item=iron-gear-wheel, count=<N>)
REGLE CRITIQUE : une fois find_nearest ET walk_to_entity faits pour un ore, l'action suivante DOIT etre mine_entity. NE JAMAIS répéter find_nearest ou walk_to_entity sur un ore deja ciblé. Une fois mine_entity réussi (ore en inventaire), passe à place_furnace. Une fois place_furnace réussi, passe aux move_items. Une fois l'inventaire a assez d'iron-plate, appelle craft_item.

STRATEGIE : observe l'INVENTAIRE et le DERNIER RESULTAT fournis. Si l'inventaire contient deja assez d'iron-plate pour le craft final, appelle craft_item directement. Si une action a echoue (DERNIER RESULTAT ok=false), choisis une autre action (replanification). Quand l'inventaire contient >= count x item objectif, appelle `stop`.

Si l'objectif est deja atteint dans l'inventaire, appelle l'outil `stop` (pas emit_action)."""


def _build_loop_user_message(goal: ProductionGoal, inv: dict, position,
                             ctx: PlanContext, last_result) -> str:
    pos = position if position is not None else "inconnue"
    recipe = ctx.goal_recipe if ctx.goal_recipe is not None else "inconnue"
    inv_s = ", ".join(f"{k}={v}" for k, v in sorted(inv.items())) or "vide"
    if last_result is None or last_result.get("ok") is None:
        lr = "premier tour"
    else:
        lr = ("OK" if last_result.get("ok") else "ECHEC") + " : " + str(last_result.get("detail", ""))[:120]
    return (
        f"OBJECTIF : produire {goal.count} x {goal.item}\n"
        f"POSITION : {pos}\n"
        f"INVENTAIRE : {inv_s}\n"
        f"RECETTE OBJECTIF : {recipe}\n"
        f"DERNIER RESULTAT : {lr}\n"
        f"Choisis UNE action via emit_action, ou stop si l'objectif est atteint."
    )


class LLMStepPlanner:
    """StepPlanner LLM (P1c) : 1 action par tour via emit_action/stop.

    Fail-soft : LLM injoignable ou tool_call absent -> NextAction(status="fallback")
    (la boucle bascule en déterministe final). Action invalide ->
    NextAction(status="invalid", motif) (la boucle log + continue, le LLM verra
    l'erreur au tour suivant).
    """

    def __init__(self, cfg: Config, recipe_lookup=None):
        self.cfg = cfg
        self._recipe_lookup = recipe_lookup
        try:
            import openai
            self._openai = openai
        except ImportError as e:
            self._openai = None
            self._import_err = str(e)
            self._client = None
            return
        self._import_err = None
        if cfg.openai_base_url and cfg.llm_enabled:
            self._client = openai.OpenAI(
                api_key=cfg.openai_api_key or "ollama",
                base_url=cfg.openai_base_url,
                timeout=cfg.llm_timeout,
            )
        else:
            self._client = None

    def next_action(self, goal: ProductionGoal, inv: dict, position,
                    ctx: PlanContext, last_result) -> NextAction:
        if self._openai is None:
            return NextAction(status="fallback", motif=f"openai indisponible: {self._import_err}")
        if self._client is None:
            return NextAction(status="fallback", motif="llm disabled")

        try:
            resp = self._client.chat.completions.create(
                model=self.cfg.openai_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT_LOOP},
                    {"role": "user", "content": _build_loop_user_message(
                        goal, inv, position, ctx, last_result)},
                ],
                tools=[EMIT_ACTION_TOOL, STOP_TOOL],
                tool_choice="auto",
                max_tokens=self.cfg.llm_max_tokens,
            )
        except Exception as e:
            return NextAction(status="fallback",
                              motif=f"llm unreachable: {type(e).__name__}: {e}")

        tool_calls = getattr(resp.choices[0].message, "tool_calls", None)
        if not tool_calls:
            return NextAction(status="fallback", motif="llm no tool_call")

        name = tool_calls[0].function.name
        try:
            args_json = tool_calls[0].function.arguments
            raw = json.loads(args_json) if isinstance(args_json, str) else args_json
        except (json.JSONDecodeError, AttributeError) as e:
            return NextAction(status="invalid", motif=f"tool_call illisible: {e}")

        if name == "stop":
            return NextAction(status="stop")
        if name != "emit_action":
            return NextAction(status="invalid", motif=f"tool inconnu: {name!r}")

        raw_action = raw.get("action") if isinstance(raw, dict) else None
        if not isinstance(raw_action, dict):
            return NextAction(status="invalid", motif="emit_action sans action")
        ok, step, motif = validate_step(raw_action, ctx, goal, inv)
        if not ok:
            return NextAction(status="invalid", motif=motif)
        return NextAction(step=step, status="action")