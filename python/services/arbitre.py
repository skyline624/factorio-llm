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

# Combien d'items de l'inventaire tiennent dans le résumé. Une borne de TAILLE, jamais
# de contenu : un prompt a ses limites, mais c'est au plus abondant de rester, pas à une
# liste de noms décidée d'avance de choisir ce que le modèle a le droit de savoir.
MAX_ITEMS_RESUMES = 25

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
    # CE QUE L'ÉTAT PORTE DÉJÀ ET QU'ON JETAIT. Ces champs sont collectés à chaque
    # observation ; les taire revient à demander un arbitrage en cachant l'enjeu.
    marche = getattr(etat, "marche", None)
    if marche:
        cout = getattr(etat, "marche_cout", "")
        lignes.append(f"recherche visée : {marche}" + (f" (coût : {cout})" if cout else ""))
    manque = getattr(etat, "a_fournir", ()) or ()
    if manque:
        lignes.append("rien ne produit encore : " + ", ".join(str(m) for m in manque))
    debit, objectif = getattr(etat, "debit", None), getattr(etat, "objectif", None)
    if debit is not None and objectif is not None:
        lignes.append(f"débit {debit:.2f}/s pour {objectif:.2f} demandés")
    besoins = getattr(etat, "besoins_production", ()) or ()
    if besoins:
        lignes.append("pour bâtir une chaîne il faut : "
                      + ", ".join(f"{n}×{q}" for n, q in besoins))

    # L'INVENTAIRE ENTIER, sans liste de noms choisie d'avance. Mesuré en jeu : l'option
    # proposée était « chercher automation — coût 10 × automation-science-pack », l'agent
    # en avait EXACTEMENT dix en poche, et le résumé s'arrêtait à `coal=37` parce que les
    # flacons ne figuraient pas dans une liste de six mots écrite des semaines plus tôt.
    # Le modèle ne pouvait pas savoir qu'il tenait déjà de quoi payer.
    #
    # Ce qu'une option met en jeu doit figurer dans l'état. On tronque donc par QUANTITÉ
    # — un prompt a ses limites — jamais par nom : le plus abondant d'abord, et le compte
    # de ce qui déborde plutôt qu'un silence.
    inv = {k: v for k, v in (getattr(etat, "inventaire", None) or {}).items() if v}
    if inv:
        tries = sorted(inv.items(), key=lambda kv: (-kv[1], kv[0]))
        montres = tries[:MAX_ITEMS_RESUMES]
        reste = len(tries) - len(montres)
        lignes.append("inventaire : " + ", ".join(f"{k}={v}" for k, v in montres)
                      + (f" (+{reste} autre(s))" if reste > 0 else ""))
    return "\n".join(lignes)


def resumer_options(options) -> str:
    return "\n".join(f"[{i}] {o.action} (priorité {o.priorite}) — {o.raison}"
                     for i, o in enumerate(options))


class LLMArbitre:
    """Choisit une option parmi celles que le déterministe a validées.

    `client` est injectable : les tests n'ont besoin ni de réseau ni de clé.
    """

    def __init__(self, cfg=None, client=None):
        # La construction est partagée avec l'enquêteur : deux composants ont besoin d'un
        # modèle, et deux copies du même code garantiraient que la prochaine correction
        # n'en atteigne qu'une.
        from services.llm_client import construire_client
        self.journal: list[str] = []
        # Combien de fois le modèle N'A PAS pu se prononcer. Rendre 0 est la bonne
        # conduite — c'est la décision du moteur seul — mais 0 « par défaut » et 0
        # « après réflexion » sont deux faits opposés qu'un seul entier confondait. Sans
        # ce compteur, un modèle injoignable se lit « le modèle est d'accord », c'est-à-dire
        # la conclusion la plus flatteuse pour ce qu'on essaie de mesurer.
        self.replis = 0
        self._client, self.cfg = construire_client(cfg, client, self.journal)

    def _repli(self, motif: str) -> int:
        self.replis += 1
        self.journal.append(f"repli : {motif}")
        return 0

    def __call__(self, etat, options) -> int:
        if not options:
            return 0
        if self._client is None:
            return self._repli("aucun client LLM")
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
            return self._repli(f"modèle injoignable ({type(e).__name__})")

        indice, raison = self._lire(resp)
        if indice is None:
            return 0            # `_lire` a déjà compté le repli et dit lequel
        if not 0 <= indice < len(options):
            return self._repli(f"indice {indice} hors des {len(options)} options")
        self.journal.append(f"choix [{indice}] {options[indice].action} — {raison}")
        return indice

    def _lire(self, resp) -> tuple[Optional[int], str]:
        """(indice, raison) d'une réponse, ou (None, "") si elle est inexploitable."""
        try:
            appels = getattr(resp.choices[0].message, "tool_calls", None)
        except (AttributeError, IndexError, TypeError):
            self._repli("réponse sans message exploitable")
            return None, ""
        if not appels:
            self._repli("le modèle n'a pas appelé l'outil")
            return None, ""
        try:
            brut = appels[0].function.arguments
            args = json.loads(brut) if isinstance(brut, str) else brut
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            self._repli(f"arguments illisibles ({type(e).__name__})")
            return None, ""
        if not isinstance(args, dict):
            self._repli("arguments hors format")
            return None, ""
        indice = args.get("indice")
        # bool est un int en Python : True passerait pour l'indice 1.
        if isinstance(indice, bool) or not isinstance(indice, int):
            self._repli(f"indice non entier ({indice!r})")
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
        # Les tours où le modèle n'a PAS pu se prononcer. Ils étaient comptés comme des
        # accords, ce qui est la lecture la plus flatteuse de tout ce qui rate : modèle
        # injoignable, réponse illisible, outil non appelé — autant de « le modèle est
        # d'accord ». Mesuré à l'inverse en jeu : sur 14 appels réels, zéro repli et
        # zéro divergence, donc des accords authentiques. C'est ce constat qui n'était
        # pas démontrable avant de séparer les deux.
        self.replis = 0
        # Les incidents d'arbitrage, séparés des divergences : une exception n'est pas un
        # désaccord, et la ranger parmi les désaccords gonflerait le seul chiffre qui doit
        # rester propre.
        self.incidents: list[str] = []

    @property
    def taux_divergence(self) -> float:
        """Rapporté aux seuls tours où le modèle s'est PRONONCÉ.

        Diviser par l'ensemble des appels diluerait le taux avec des tours où il n'a rien
        dit, et un modèle absent afficherait 0 % de divergence — le chiffre d'un modèle
        parfaitement d'accord.
        """
        total = self.accords + len(self.divergences)
        return len(self.divergences) / total if total else 0.0

    def __call__(self, etat, options) -> int:
        avant = getattr(self.arbitre, "replis", None)
        try:
            propose = self.arbitre(etat, options)
        except Exception as e:
            # Une exception n'est pas une divergence : le modèle n'a rien proposé.
            self.replis += 1
            self.incidents.append(f"arbitre en erreur : {type(e).__name__}")
            return 0
        # Le compteur de replis de l'arbitre distingue « il a répondu 0 » de « on a mis 0
        # faute de réponse ». Duck typing : un arbitre qui ne l'expose pas est traité
        # comme avant, ses 0 comptant pour des accords.
        apres = getattr(self.arbitre, "replis", None)
        if avant is not None and apres is not None and apres > avant:
            self.replis += 1
            return 0
        if isinstance(propose, int) and not isinstance(propose, bool) \
                and 0 < propose < len(options):
            self.divergences.append(
                f"le modèle aurait choisi [{propose}] {options[propose].action} "
                f"au lieu de [0] {options[0].action}")
        else:
            self.accords += 1
        return 0          # le déterministe garde la main, toujours