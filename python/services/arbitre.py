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

from services.outils_llm import mesurer, schema_outils

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

# LES SONDES DE L'ARBITRE — en LECTURE SEULE, et c'est la condition pour qu'il reste un
# arbitre : il désigne une option, il ne va pas miner de son propre chef.
#
# Relevé avant de les ajouter : l'agent qui CONSTATE (`Enqueteur`) disposait de six
# sondes et trouvait cinq pannes sur six ; celui qui DÉCIDE n'en avait aucune. On lui
# demandait de trancher entre vider une machine, bâtir une chaîne ou aller miner, sans
# qu'il puisse regarder ce que cette machine contenait — « des plaques dorment dans ce
# four » lui était invisible.
#
# On reste au strict nécessaire : chaque sonde coûte un aller-retour (~3 s). Ce sont les
# questions qu'un joueur se pose avant d'agir — qu'y a-t-il là, y a-t-il du courant, ce
# gisement est-il encore bon, qu'ai-je produit, que sais-je faire.
SONDES: dict[str, dict] = {
    "inspect_at": {
        "description": "Ce qui est POSÉ à une position : nom, type, statut, direction. "
                       "Sert à voir ce qu'une machine contient ou pourquoi elle est "
                       "arrêtée avant de décider quoi faire d'elle.",
        "params": {"x": "number", "y": "number", "radius": "number"},
        "requis": ["x", "y"],
    },
    "get_power_state": {
        "description": "État électrique autour d'une position : networkId (absent = "
                       "personne ne l'a reliée), production et consommation.",
        "params": {"x": "number", "y": "number", "radius": "number"},
        "requis": ["x", "y"],
    },
    "scan_patch": {
        "description": "Gisements d'une ressource : count, bbox et un échantillon de "
                       "tuiles trié du plus proche au plus lointain. Pour savoir si "
                       "aller extraire vaut le déplacement.",
        "params": {"resource": "string", "radius": "number"},
        "requis": ["resource"],
    },
    "production_stats": {
        "description": "Ce que l'usine a réellement produit et consommé. Distingue une "
                       "chaîne qui tourne d'une chaîne qui a l'air de tourner.",
        "params": {"item": "string"},
        "requis": [],
    },
    "get_technologies": {
        "description": "Technologies acquises et recherches disponibles avec leur coût. "
                       "Pour savoir ce qu'une recherche ouvrirait avant de la payer.",
        "params": {},
        "requis": [],
    },
}

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
        lignes.append("pour bâtir une chaîne (usine) il faut : "
                      + ", ".join(f"{n}×{q}" for n, q in besoins))
        # UNE INFORMATION VRAIE PEUT ÊTRE TROMPEUSE SI ELLE EST SEULE. Partie 32 : l'agent
        # lit qu'il lui manque un bras et vingt charbons, et en conclut que rien n'est
        # possible tout de suite. C'est exact pour l'USINE — et faux pour une extraction
        # minimale, qui ne demande ni bras ni combustible depuis que le four se pose sur la
        # tuile de drop. Il tenait de quoi produire et ne pouvait pas le savoir.
        #
        # On donne donc les deux coûts et l'on s'arrête là : ce qui est mesuré s'écrit, ce
        # qu'on en tire appartient à l'agent.
        inv = getattr(etat, "inventaire", {}) or {}
        a_foreuse = any(str(n).endswith("mining-drill") and inv.get(str(n), 0) > 0
                        for n, _ in besoins) or inv.get("burner-mining-drill", 0) > 0
        lignes.append("pour une extraction minimale (`extraire_ici`) : une foreuse suffit, "
                      + ("tu en as une en poche" if a_foreuse else "tu n'en as pas")
                      + " — le four se pose sur sa sortie et se forge au besoin, sans bras "
                        "ni combustible pour la pose")

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

    # Mesures autorisées avant de rendre la main. L'enquêteur s'en accorde huit ; un
    # arbitrage est plus pressé — la boucle attend, et chaque aller-retour coûte trois
    # secondes réelles.
    BUDGET = 4

    def _sonde_demandee(self, resp):
        """(nom, args, id) si le modèle réclame une MESURE, None s'il choisit.

        `choisir` traverse : c'est la réponse attendue, pas une sonde.
        """
        try:
            appels = getattr(resp.choices[0].message, "tool_calls", None) or []
        except (AttributeError, IndexError, TypeError):
            return None
        for a in appels:
            nom = getattr(getattr(a, "function", None), "name", "")
            if not nom or nom == "choisir":
                return None
            brut = getattr(a.function, "arguments", "{}")
            try:
                args = json.loads(brut) if isinstance(brut, str) else (brut or {})
            except (json.JSONDecodeError, TypeError):
                args = {}
            return nom, (args if isinstance(args, dict) else {}), getattr(a, "id", "m")
        return None

    def __init__(self, cfg=None, client=None, budget: Optional[int] = None, api=None):
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
        # Combien de fois il a REGARDÉ avant de trancher. Un arbitre qui ne mesure jamais
        # décide sur ce qu'on lui pousse ; le savoir change la lecture d'une A/B.
        self.mesures = 0
        self.budget = budget if budget is not None else self.BUDGET
        # `decide` est PURE : elle n'a pas d'API à passer. L'arbitre porte donc la sienne,
        # que le Coordinator lui confie à la construction. Sans elle, il décide comme
        # avant — sur ce qu'on lui pousse, sans rien pouvoir regarder.
        self.api = api
        self._client, self.cfg = construire_client(cfg, client, self.journal)

    def _repli(self, motif: str) -> int:
        self.replis += 1
        self.journal.append(f"repli : {motif}")
        return 0

    def __call__(self, etat, options, api=None) -> int:
        """Choisit une option. Avec `api`, le modèle peut MESURER avant de trancher.

        Sans `api` le comportement est celui d'avant — un aller-retour, un choix : les
        appelants qui n'ont rien à sonder ne paient rien de plus.
        """
        if not options:
            return 0
        if self._client is None:
            return self._repli("aucun client LLM")
        api = api if api is not None else self.api

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"État de l'usine :\n{resumer_etat(etat)}\n\n"
                f"Options possibles :\n{resumer_options(options)}\n\n"
                + ("Tu peux d'abord MESURER ce qui te manque, puis choisir.\n"
                   if api is not None else "")
                + "Choisis l'option à exécuter maintenant."},
        ]
        outils = (schema_outils(SONDES, CHOISIR_TOOL) if api is not None
                  else [CHOISIR_TOOL])

        # UNE MESURE, PUIS UNE AUTRE, PUIS LE CHOIX — et un budget, sans quoi un modèle
        # qui mesure sans fin bloquerait la boucle. Le budget épuisé n'est pas une panne :
        # on retombe sur `options[0]`, la décision du moteur seul.
        resp = None
        for _ in range(max(1, self.budget) + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=getattr(self.cfg, "openai_model", "gpt-4o-mini"),
                    messages=messages,
                    tools=outils,
                    tool_choice="auto",
                    max_tokens=getattr(self.cfg, "llm_max_tokens", 512),
                )
            except Exception as e:
                return self._repli(f"modèle injoignable ({type(e).__name__})")

            appel = self._sonde_demandee(resp) if api is not None else None
            if appel is None:
                break
            if self.mesures >= self.budget:
                # Le budget est un plafond de MESURES, pas d'allers-retours : une de plus
                # serait exactement ce que le plafond doit empêcher.
                return self._repli(f"budget de {self.budget} mesure(s) épuisé sans choix")
            nom, args, ident = appel
            resultat = mesurer(api, SONDES, nom, args, self.journal)
            self.mesures += 1
            self.journal.append(f"mesure {nom}({args}) -> {resultat[:70]}")
            messages.append({"role": "assistant", "tool_calls": [
                {"id": ident, "type": "function",
                 "function": {"name": nom, "arguments": json.dumps(args)}}]})
            messages.append({"role": "tool", "tool_call_id": ident, "content": resultat})
        else:
            return self._repli(f"budget de {self.budget} mesure(s) épuisé sans choix")

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