"""Arbitrage LLM — le modèle CHOISIT parmi des options, il n'en génère aucune.

Module distinct de `llm.py` à dessein. `llm.py` fait générer des plans et des étapes au
modèle (mode P1c), protégé par `validate_plan` et une simulation ; c'est le mode risqué,
celui que le benchmark FLE mesure à 7/24 — les agents y produisent librement coordonnées
et séquences, et échouent dès qu'il faut coordonner plus de six machines.

Ici, le contrat est inverse et tient en une phrase : **le déterministe énumère les
options légales, le modèle en désigne une par son indice**. Il ne peut donc pas proposer
l'impossible. Ce n'est pas une bride ajoutée après coup mais la forme même de
l'interface : `Arbitre.__call__(etat, options) -> int`.

Trois propriétés que ce module s'impose :

  - **toute défaillance rend 0**, c'est-à-dire la décision qu'aurait prise le moteur
    seul. Client absent, modèle injoignable, réponse illisible, indice hors bornes : un
    agent ne s'arrête pas parce qu'un service distant est indisponible ;
  - **chaque décision est journalisée** avec son motif. Un arbitrage qu'on ne peut pas
    relire ne se corrige pas ;
  - **le modèle ne voit pas l'état brut du jeu** — des milliers d'entités — mais ce que
    la couche déterministe en a tiré : « le drill est débranché », « 3 machines en
    service », « menace imminente au nord ». C'est ce qui rend le prompt court, donc peu
    coûteux, et le raisonnement possible.

`ArbitreOmbre` permet d'introduire tout cela sans rien risquer : le déterministe garde
la main, le modèle propose en parallèle, et on mesure les divergences avant de décider
si elles valent quelque chose.
"""

from __future__ import annotations

import json
from typing import Optional

SYSTEM_PROMPT = (
    "Tu arbitres les décisions d'un agent autonome qui construit une usine Factorio.\n"
    "On te donne l'état de l'usine et une liste d'actions DÉJÀ VALIDÉES par le moteur "
    "déterministe : elles sont toutes légales et exécutables.\n"
    "Ton seul travail est de choisir laquelle exécuter MAINTENANT, et de dire pourquoi "
    "en une phrase.\n"
    "Tu ne proposes pas d'action nouvelle, tu n'inventes pas de coordonnées, tu ne "
    "calcules pas de ratios : tout cela est déjà fait et validé.\n"
    "Principes utiles : une usine arrêtée ne produit rien ; se défendre coûte du temps "
    "qui n'est pas passé à produire, mais une usine détruite ne produit plus jamais ; "
    "l'ordre proposé est déjà raisonnable, ne t'en écarte que si l'état le justifie.\n"
    "En cas de doute, choisis l'option 0."
)

CHOISIR_TOOL = {
    "type": "function",
    "function": {
        "name": "choisir",
        "description": "Choisit l'option à exécuter parmi celles proposées.",
        "parameters": {
            "type": "object",
            "properties": {
                "indice": {"type": "integer",
                           "description": "Indice de l'option retenue, à partir de 0."},
                "raison": {"type": "string",
                           "description": "Pourquoi celle-ci plutôt qu'une autre, en une phrase."},
            },
            "required": ["indice", "raison"],
        },
    },
}


def resumer_etat(etat) -> str:
    """Résumé court de l'état, destiné au modèle.

    Duck typing volontaire : ce module ne doit pas importer `agents.coordinator`, qui
    importera celui-ci. On lit ce qui est présent, on ignore le reste.
    """
    lignes = [f"machines en service : {getattr(etat, 'machines', 0)}"]
    reseau = getattr(etat, "reseau", None)
    lignes.append("réseau électrique : "
                  + (f"oui (id {reseau})" if reseau is not None else "aucun")
                  + f", production {getattr(etat, 'production_kw', 0.0):.0f} kW")
    diag = getattr(etat, "diagnostic", None)
    if diag is not None:
        lignes.append(f"diagnostic : {diag.resume()}")
    menace = getattr(etat, "menace", None)
    if menace is not None:
        lignes.append(f"menace : {menace}")
    inv = getattr(etat, "inventaire", None) or {}
    utiles = ("coal", "gun-turret", "firearm-magazine", "iron-plate",
              "small-electric-pole", "electric-mining-drill")
    notables = {k: v for k, v in inv.items() if k in utiles and v}
    if notables:
        lignes.append("inventaire : " + ", ".join(f"{k}={v}" for k, v in notables.items()))
    return "\n".join(lignes)


def resumer_options(options) -> str:
    return "\n".join(f"[{i}] {o.action} (priorité {o.priorite}) — {o.raison}"
                     for i, o in enumerate(options))


class LLMArbitre:
    """Choisit une option parmi celles que le déterministe a validées.

    `client` est injectable : les tests n'ont besoin ni de réseau ni de clé.
    """

    def __init__(self, cfg=None, client=None):
        self.journal: list[str] = []
        if cfg is None:
            try:
                from config import load_config
                cfg = load_config()
            except Exception as e:                      # config absente : on le dit
                self.cfg = None
                self._client = client
                if client is None:
                    self.journal.append(f"configuration illisible : {e}")
                return
        self.cfg = cfg
        if client is not None:
            self._client = client
            return
        try:
            import openai
        except ImportError as e:
            self._client = None
            self.journal.append(f"openai indisponible : {e}")
            return
        if getattr(cfg, "openai_base_url", None) and getattr(cfg, "llm_enabled", False):
            self._client = openai.OpenAI(
                api_key=getattr(cfg, "openai_api_key", None) or "ollama",
                base_url=cfg.openai_base_url,
                timeout=getattr(cfg, "llm_timeout", 30),
            )
        else:
            self._client = None
            self.journal.append("LLM désactivé (base_url vide ou LLM_ENABLED=false)")

    def __call__(self, etat, options) -> int:
        if not options:
            return 0
        if self._client is None:
            self.journal.append("repli : aucun client LLM")
            return 0
        try:
            resp = self._client.chat.completions.create(
                model=getattr(self.cfg, "openai_model", "gpt-4o-mini"),
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content":
                        f"État de l'usine :\n{resumer_etat(etat)}\n\n"
                        f"Options possibles :\n{resumer_options(options)}\n\n"
                        f"Choisis l'option à exécuter maintenant."},
                ],
                tools=[CHOISIR_TOOL],
                tool_choice="auto",
                max_tokens=getattr(self.cfg, "llm_max_tokens", 512),
            )
        except Exception as e:
            self.journal.append(f"repli : modèle injoignable ({type(e).__name__})")
            return 0

        indice, raison = self._lire(resp)
        if indice is None:
            return 0
        if not 0 <= indice < len(options):
            self.journal.append(f"repli : indice {indice} hors des {len(options)} options")
            return 0
        self.journal.append(f"choix [{indice}] {options[indice].action} — {raison}")
        return indice

    def _lire(self, resp) -> tuple[Optional[int], str]:
        """(indice, raison) d'une réponse, ou (None, "") si elle est inexploitable."""
        try:
            appels = getattr(resp.choices[0].message, "tool_calls", None)
        except (AttributeError, IndexError, TypeError):
            self.journal.append("repli : réponse sans message exploitable")
            return None, ""
        if not appels:
            self.journal.append("repli : le modèle n'a pas appelé l'outil")
            return None, ""
        try:
            brut = appels[0].function.arguments
            args = json.loads(brut) if isinstance(brut, str) else brut
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            self.journal.append(f"repli : arguments illisibles ({type(e).__name__})")
            return None, ""
        if not isinstance(args, dict):
            self.journal.append("repli : arguments hors format")
            return None, ""
        indice = args.get("indice")
        # bool est un int en Python : True passerait pour l'indice 1.
        if isinstance(indice, bool) or not isinstance(indice, int):
            self.journal.append(f"repli : indice non entier ({indice!r})")
            return None, ""
        return indice, str(args.get("raison", ""))[:160]


class ArbitreOmbre:
    """Fait tourner un arbitre SANS lui laisser la main, et mesure ses divergences.

    La façon la moins risquée d'introduire un modèle : le déterministe continue de
    décider, le modèle propose en parallèle, et on accumule la seule donnée qui permette
    de trancher — quand il diverge, avait-il raison ? L'activer d'emblée reviendrait à
    parier sans mesure.
    """

    def __init__(self, arbitre):
        self.arbitre = arbitre
        self.divergences: list[str] = []
        self.accords = 0

    @property
    def taux_divergence(self) -> float:
        total = self.accords + len(self.divergences)
        return len(self.divergences) / total if total else 0.0

    def __call__(self, etat, options) -> int:
        try:
            propose = self.arbitre(etat, options)
        except Exception as e:
            self.divergences.append(f"arbitre en erreur : {type(e).__name__}")
            return 0
        if isinstance(propose, int) and not isinstance(propose, bool) \
                and 0 < propose < len(options):
            self.divergences.append(
                f"le modèle aurait choisi [{propose}] {options[propose].action} "
                f"au lieu de [0] {options[0].action}")
        else:
            self.accords += 1
        return 0          # le déterministe garde la main, toujours