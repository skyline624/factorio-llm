"""Enquêteur — le modèle CHOISIT quoi observer, et nomme une cause.

C'est le pendant de `services/arbitre.py`, et il corrige une sur-application de sa
doctrine. L'arbitre part d'un constat du benchmark FLE : les modèles échouent quand ils
**génèrent** (coordonnées, séquences de poses), pas quand ils choisissent. D'où le
contrat « le déterministe énumère, le modèle désigne un indice ».

Ce contrat a été étendu trop loin. **Lire n'est pas générer.** Les six défauts du
chantier E13 ont tous été trouvés par une enquête — former une hypothèse, choisir quel
outil appeler pour la discriminer, interpréter une mesure qui contredit l'attendu — et
aucune de ces étapes n'existait dans l'agent. Le FLE nomme d'ailleurs ce manque comme le
premier mode d'échec des agents LLM : le débogage systémique.

Ici le modèle reçoit un ÉCART (une action menée à son terme sans produire son effet) et
les outils de LECTURE du mod. Il mène l'investigation en plusieurs tours, puis conclut.

Trois garde-fous, qui séparent un diagnostic d'une hypothèse séduisante :

  - **liste blanche d'outils** — que de l'observation, jamais d'action. Le pire coût
    d'une enquête est du temps ;
  - **vocabulaire fermé** de causes, ancré sur des défauts RÉELLEMENT rencontrés, plus
    `inconnu`. Une cause hors vocabulaire n'est pas exploitable par la boucle ;
  - **preuve obligatoire** — la conclusion doit citer une valeur lue. Sans preuve, elle
    est ramenée à `inconnu`, exactement comme `LLMArbitre` ramène tout échec à 0.

Un `inconnu` honnête vaut mieux qu'une fausse piste : la boucle sait alors qu'elle ne
sait pas, et le constat rejoint la liste de ce qui reste à comprendre.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Optional

# Ce que l'enquêteur a le droit d'appeler. Uniquement des lectures : `inspect_at` et
# consorts ne modifient rien. Aucune primitive de pose, de retrait ou de rotation n'est
# exposée — la réparation reste déterministe et hors de portée du modèle.
OUTILS: dict[str, dict] = {
    "inspect_at": {
        "description": "Ce qui est POSÉ à une position : nom, type, statut, direction, "
                       "et pour les inserters leur pickup/drop réels. L'outil principal.",
        "params": {"x": "number", "y": "number", "radius": "number"},
        "requis": ["x", "y"],
    },
    "get_power_state": {
        "description": "État électrique autour d'une position : networkId (absent = "
                       "personne ne l'a reliée), production et consommation.",
        "params": {"x": "number", "y": "number", "radius": "number"},
        "requis": ["x", "y"],
    },
    "can_place_check": {
        "description": "Peut-on poser cette entité ici ? Utile pour comprendre POURQUOI "
                       "une pose a échoué (occupé, hors gisement, avatar en travers).",
        "params": {"entity_name": "string", "x": "number", "y": "number",
                   "direction": "string"},
        "requis": ["entity_name", "x", "y"],
    },
    "get_tile": {
        "description": "Nature du sol à une position (eau, out-of-map, terre).",
        "params": {"x": "number", "y": "number"},
        "requis": ["x", "y"],
    },
    "scan_patch": {
        "description": "Gisements d'une ressource : count, bbox et un échantillon de "
                       "tuiles TRIÉ du plus proche au plus lointain.",
        "params": {"resource": "string", "radius": "number"},
        "requis": ["resource"],
    },
    # Outil COMPOSITE : il n'est pas sur l'api du mod, il enchaîne des lectures pour
    # rendre un résultat qu'aucune mesure isolée ne donne.
    #
    # Il est là parce que le banc d'essai l'a exigé : les deux seules pannes que le
    # modèle n'arrivait pas à nommer étaient celles de la BELT, et pour cause — établir
    # qu'une ligne de 44 tuiles est rompue demande de la parcourir, ce qui épuisait son
    # budget de mesures. Suivre un chemin est un algorithme, pas un jugement ; le laisser
    # au modèle, c'était lui confier le travail où une machine est meilleure et lui
    # retirer le temps de faire celui où il l'est.
    "suivre_flux": {
        "description": "Remonte une ligne de belts depuis un point de départ jusqu'à la "
                       "machine visée, et dit si le flux est CONTINU ou bien où et "
                       "comment il casse (trou, segment mal tourné, bras mal orienté, "
                       "bras absent, bras qui dépose à côté). À utiliser en premier "
                       "quand une chaîne de transport est en cause.",
        "params": {"depart_x": "number", "depart_y": "number", "cible_nom": "string",
                   "cible_x": "number", "cible_y": "number"},
        "requis": ["depart_x", "depart_y", "cible_nom"],
    },
}

# Vocabulaire FERMÉ. Chaque entrée a été observée en jeu, aucune n'est imaginée : c'est
# ce qui garantit qu'une cause nommée corresponde à une réparation possible.
CAUSES: dict[str, str] = {
    "belt_interrompue": "un segment manque sur le trajet, le flux s'arrête au trou",
    "belt_mal_orientee": "un segment envoie ailleurs qu'on ne croit (souvent un raccord "
                         "qui a gardé son ancienne direction)",
    "bras_mal_oriente": "un inserter est là mais ne puise pas sur la belt",
    "bras_depose_dans_le_vide": "un inserter puise bien mais dépose à côté de la machine",
    "bras_absent": "la belt arrive mais personne ne décharge",
    "machine_debranchee": "la machine n'appartient à aucun réseau électrique",
    "machine_sans_courant": "la machine est reliée à un réseau qui n'a pas de courant",
    "machine_absente": "ce qu'on croyait avoir posé n'est pas là",
    "foreur_hors_gisement": "un foreur sans minerai sous son emprise",
    "combustible_epuise": "la machine a brûlé tout son combustible et rien ne la réalimente",
    "machine_pleine": "la machine ne consomme pas ce qu'on lui apporte, son entrée est "
                      "saturée et le bras reste bloqué (waiting_for_space_in_destination)",
    "entree_fluide_manquante": "il manque un fluide à la machine (eau d'un boiler), elle "
                               "ne travaille donc pas et n'a plus besoin de combustible",
    "inconnu": "les mesures ne permettent pas de conclure — à dire plutôt qu'à deviner",
}
# Les deux causes ci-dessus ont été AJOUTÉES par le banc d'essai, pas imaginées : le
# modèle les a décrites précisément (`waiting_for_space_in_destination` sur le bras,
# `no_input_fluid` sur le boiler) et a refusé de les plaquer sur une entrée existante du
# vocabulaire. Un « inconnu » bien motivé vaut une entrée de plus.

CONCLURE = {
    "type": "function",
    "function": {
        "name": "conclure",
        "description": "Nomme la cause de l'écart, une fois les mesures faites.",
        "parameters": {
            "type": "object",
            "properties": {
                "cause": {"type": "string", "enum": sorted(CAUSES),
                          "description": "La cause, dans le vocabulaire fourni."},
                "preuve": {"type": "string",
                           "description": "La VALEUR LUE qui fonde cette conclusion "
                                          "(position, statut, direction...). Pas un "
                                          "raisonnement : une mesure."},
                "x": {"type": "number", "description": "Où, si la cause a un lieu."},
                "y": {"type": "number", "description": "Où, si la cause a un lieu."},
            },
            "required": ["cause", "preuve"],
        },
    },
}

SYSTEM_PROMPT = (
    "Tu diagnostiques les pannes d'une usine Factorio pilotée par un agent autonome.\n"
    "On te donne un ÉCART : une action que l'agent a menée à son terme, et l'effet "
    "attendu qui ne s'est pas produit.\n"
    "Tu disposes d'outils de LECTURE pour aller mesurer l'état réel du jeu. Sers-t'en : "
    "n'invente aucune valeur, ne suppose rien que tu puisses vérifier.\n"
    "Méthode : forme une hypothèse, appelle l'outil qui la départage, recommence si la "
    "mesure la contredit. Deux à quatre mesures suffisent en général.\n"
    "Rappels utiles, tous vérifiés en jeu :\n"
    "- une machine peut afficher `no_fuel` alors que la vraie cause est en amont "
    "(la chaîne qui devait l'alimenter ne transporte rien) ;\n"
    "- un inserter mal orienté se pose sans erreur et ne transporte jamais rien : "
    "compare son pickup/drop à ce qu'il y a RÉELLEMENT à ces positions ;\n"
    "- `networkId` absent = personne ne l'a reliée ; `no_power` = reliée à un réseau "
    "à sec. Ce sont deux réparations différentes.\n"
    "Quand tu as mesuré, appelle `conclure`. Si les mesures ne permettent pas de "
    "trancher, conclus `inconnu` : c'est une réponse utile, une fausse piste ne l'est pas."
)


@dataclass
class Constat:
    """Ce que l'enquête a établi. `preuve` est ce qui la distingue d'une opinion."""
    cause: str
    preuve: str = ""
    position: Optional[tuple[float, float]] = None
    outils_appeles: int = 0
    journal: list = field(default_factory=list)

    @property
    def concluant(self) -> bool:
        return self.cause != "inconnu"

    def __str__(self) -> str:
        ou = f" en ({self.position[0]},{self.position[1]})" if self.position else ""
        return (f"{self.cause}{ou} — {self.preuve} "
                f"[{self.outils_appeles} mesure(s)]")


def _schema_outils() -> list[dict]:
    outils = []
    for nom, spec in OUTILS.items():
        outils.append({
            "type": "function",
            "function": {
                "name": nom,
                "description": spec["description"],
                "parameters": {
                    "type": "object",
                    "properties": {p: {"type": t} for p, t in spec["params"].items()},
                    "required": spec["requis"],
                },
            },
        })
    outils.append(CONCLURE)
    return outils


def resumer_ecart(ecart) -> str:
    """L'écart tel que le modèle le reçoit — court, et sans état brut du jeu."""
    lignes = [f"action menée : {getattr(ecart, 'action', '?')}",
              f"effet attendu : {getattr(ecart, 'attendu', '?')}",
              f"observé à la place : {getattr(ecart, 'observe', '?')}"]
    c = getattr(ecart, "cible", None)
    if c is not None:
        lignes.append(f"machine concernée : {c.name} en ({c.x}, {c.y})")
    return "\n".join(lignes)


def tronquer(valeur: Any, entites_max: int = 12, caracteres: int = 700) -> str:
    """Réponse d'outil réduite à ce qui se raisonne.

    `scan_area` peut rendre deux cents entités : les envoyer noierait le signal et
    coûterait cher pour rien. Le prompt court est ce qui rend le raisonnement possible —
    principe déjà posé par l'arbitre, qui ne voit jamais l'état brut du jeu.
    """
    if isinstance(valeur, dict) and isinstance(valeur.get("entities"), list):
        lignes = valeur["entities"]
        valeur = dict(valeur)
        valeur["entities"] = lignes[:entites_max]
        if len(lignes) > entites_max:
            valeur["entities_tronquees"] = len(lignes) - entites_max
    if isinstance(valeur, dict) and isinstance(valeur.get("sample"), list):
        valeur = dict(valeur)
        valeur["sample"] = valeur["sample"][:6]
    try:
        return json.dumps(valeur, ensure_ascii=False)[:caracteres]
    except (TypeError, ValueError):
        return str(valeur)[:caracteres]


class Enqueteur:
    """Mène l'enquête sur un écart et rend une cause nommée.

    `client` est injectable : les tests n'ont besoin ni de réseau ni de modèle. Toute
    défaillance — client absent, service injoignable, budget dépassé, cause hors
    vocabulaire — rend `inconnu` plutôt qu'une exception : un agent ne s'arrête pas
    parce qu'un service distant est indisponible.
    """

    BUDGET = 8          # mesures autorisées avant de rendre la main
    # Une enquête enchaîne plusieurs tours sur un contexte qui grossit ; mesuré, 30 s
    # l'interrompaient en plein raisonnement et l'échec passait pour une incapacité du
    # modèle à conclure. Un diagnostic n'est pas pressé — la boucle, elle, attend.
    TIMEOUT = 180.0

    def __init__(self, cfg=None, client=None, budget: Optional[int] = None,
                 timeout: Optional[float] = None):
        from services.llm_client import construire_client
        self.journal: list[str] = []
        self._client, self.cfg = construire_client(
            cfg, client, self.journal, timeout if timeout is not None else self.TIMEOUT)
        self.budget = budget if budget is not None else self.BUDGET

    def __call__(self, api, ecart) -> Constat:
        if self._client is None:
            self.journal.append("repli : aucun client LLM")
            return Constat("inconnu", "aucun modèle joignable", journal=list(self.journal))

        messages = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content":
                f"ÉCART constaté :\n{resumer_ecart(ecart)}\n\n"
                f"Causes possibles :\n"
                + "\n".join(f"- {k} : {v}" for k, v in CAUSES.items())
                + "\n\nMesure ce qu'il faut, puis appelle `conclure`."},
        ]
        outils = _schema_outils()
        appels = 0

        for _ in range(self.budget + 1):
            try:
                resp = self._client.chat.completions.create(
                    model=getattr(self.cfg, "openai_model", "gpt-4o-mini"),
                    messages=messages,
                    tools=outils,
                    tool_choice="auto",
                    max_tokens=getattr(self.cfg, "llm_max_tokens", 2048),
                )
                message = resp.choices[0].message
            except Exception as e:
                self.journal.append(f"repli : modèle injoignable ({type(e).__name__})")
                return Constat("inconnu", f"modèle injoignable ({type(e).__name__})",
                               outils_appeles=appels, journal=list(self.journal))

            from services.llm_client import lire_appels
            demandes = lire_appels(message, self.journal)
            if not demandes:
                # Le modèle a répondu en TEXTE au lieu d'appeler l'outil. Abandonner ici
                # serait imputer au modèle un défaut de harnais : son raisonnement est
                # dans ce texte, il ne lui manque que la forme. On le lui redemande en
                # forçant l'outil — mesuré sur le banc d'essai, c'est ce qui séparait
                # « le modèle ne sait pas » de « je ne lui ai pas laissé dire ».
                texte = str(getattr(message, "content", "") or "")[:400]
                self.journal.append(f"réponse en texte libre : {texte[:120]}")
                messages.append({"role": "assistant", "content": texte})
                return self._forcer_conclusion(messages, outils, appels)

            # Une conclusion termine l'enquête, même si d'autres appels l'accompagnent.
            for _, nom, args in demandes:
                if nom == "conclure":
                    return self._conclure(args, appels)

            # Le message assistant est rejoué avec les identifiants QUE LE MODÈLE A
            # ÉMIS, et chaque réponse d'outil cite le sien. Les réinventer rompt
            # l'appariement et le tour suivant revient vide.
            messages.append({
                "role": "assistant", "content": "",
                "tool_calls": [
                    {"id": ident, "type": "function",
                     "function": {"name": n, "arguments": json.dumps(a)}}
                    for ident, n, a in demandes],
            })
            for ident, nom, args in demandes:
                appels += 1
                resultat = self._mesurer(api, nom, args)
                self.journal.append(f"{nom}({args}) -> {resultat[:90]}")
                messages.append({"role": "tool", "tool_call_id": ident,
                                 "content": resultat})
            if appels >= self.budget:
                self.journal.append(f"budget de {self.budget} mesures épuisé")
                return self._forcer_conclusion(messages, outils, appels)

        self.journal.append("aucune conclusion après le budget")
        return self._forcer_conclusion(messages, outils, appels)

    def _forcer_conclusion(self, messages: list, outils: list, appels: int) -> Constat:
        """Redemande une conclusion en IMPOSANT l'appel de `conclure`.

        Dernier tour, sans outil de mesure : le modèle a déjà tout ce qu'il lui faut, il
        ne lui reste qu'à nommer. Si l'API refuse le `tool_choice` forcé, on retombe sur
        `inconnu` — jamais sur une conclusion fabriquée.
        """
        messages = messages + [{
            "role": "user",
            "content": "Tu as fini de mesurer. Appelle MAINTENANT l'outil `conclure` avec "
                       "la cause et la valeur lue qui la fonde. Si tes mesures ne "
                       "permettent pas de trancher, conclus `inconnu`."}]
        try:
            resp = self._client.chat.completions.create(
                model=getattr(self.cfg, "openai_model", "gpt-4o-mini"),
                messages=messages,
                tools=[CONCLURE],
                tool_choice={"type": "function", "function": {"name": "conclure"}},
                # Large marge : mesuré, le modèle épuisait `max_tokens` en raisonnement
                # avant d'émettre l'appel, et rendait un message VIDE (finish_reason
                # `length`). Le symptôme faisait accuser le modèle de ne pas savoir
                # conclure alors qu'on ne lui laissait pas la place de le dire.
                max_tokens=max(4096, getattr(self.cfg, "llm_max_tokens", 2048)),
            )
            from services.llm_client import lire_appels
            fin = getattr(resp.choices[0], "finish_reason", "?")
            self.journal.append(f"conclusion forcée : finish_reason={fin}")
            demandes = lire_appels(resp.choices[0].message, self.journal)
        except Exception as e:
            self.journal.append(f"conclusion forcée impossible ({type(e).__name__})")
            return Constat("inconnu", f"pas de conclusion ({type(e).__name__})",
                           outils_appeles=appels, journal=list(self.journal))
        for _, nom, args in demandes:
            if nom == "conclure":
                return self._conclure(args, appels)
        self.journal.append("aucune conclusion même en la forçant")
        return Constat("inconnu", "aucune conclusion rendue",
                       outils_appeles=appels, journal=list(self.journal))

    def _mesurer(self, api, nom: str, args: dict) -> str:
        """Exécute un outil de la LISTE BLANCHE. Tout le reste est refusé, pas exécuté."""
        if nom not in OUTILS:
            self.journal.append(f"outil refusé : {nom!r}")
            return f"outil inconnu : {nom}"
        if nom == "suivre_flux":
            propres = {k: v for k, v in args.items() if k in OUTILS[nom]["params"]}
            try:
                return tronquer(self._suivre_flux(api, **propres))
            except Exception as e:
                return f"mesure impossible : {type(e).__name__}"
        fn = getattr(api, nom, None)
        if fn is None:
            return f"outil indisponible : {nom}"
        permis = OUTILS[nom]["params"]
        propres = {k: v for k, v in args.items() if k in permis}
        try:
            return tronquer(fn(**propres))
        except Exception as e:
            return f"mesure impossible : {type(e).__name__}"

    @staticmethod
    def _suivre_flux(api, depart_x=None, depart_y=None, cible_nom="",
                     cible_x=None, cible_y=None) -> dict:
        """Adaptateur du service `flux` en outil : lecture composite, non destructive."""
        from services.flux import suivre_flux
        cible = None
        if isinstance(cible_x, (int, float)) and isinstance(cible_y, (int, float)):
            cible = (float(cible_x), float(cible_y))
        r = suivre_flux(api, (float(depart_x or 0.0), float(depart_y or 0.0)),
                        str(cible_nom), cible)
        return {"continu": r.continu, "tuiles_parcourues": r.tuiles, "cause": r.cause,
                "rupture": list(r.rupture) if r.rupture else None, "detail": r.detail}

    @staticmethod
    def _normaliser(cause: str) -> str:
        """Ramène une cause à la forme du vocabulaire : sans accent, en minuscules.

        Le vocabulaire est fermé et il doit le rester — mais le refuser sur un ACCENT
        n'écarte pas une hypothèse douteuse, il jette une conclusion juste. Mesuré : le
        modèle a rendu « machine_debranchée » après huit outils appelés, et l'enquête
        entière a été ramenée à `inconnu` pour un é. La rigueur porte sur le SENS des
        causes admises, pas sur leur orthographe.
        """
        import unicodedata
        sans_accent = "".join(
            c for c in unicodedata.normalize("NFD", cause.strip().lower())
            if unicodedata.category(c) != "Mn")
        return sans_accent.replace(" ", "_").replace("-", "_")

    def _conclure(self, args: dict, appels: int) -> Constat:
        cause = self._normaliser(str(args.get("cause", "")))
        preuve = str(args.get("preuve", "")).strip()
        if cause not in CAUSES:
            self.journal.append(f"cause hors vocabulaire : {cause!r}")
            return Constat("inconnu", f"cause proposée hors vocabulaire ({cause!r})",
                           outils_appeles=appels, journal=list(self.journal))
        if cause != "inconnu" and not preuve:
            # Une conclusion sans mesure est une opinion. On la refuse plutôt que de la
            # laisser déclencher une réparation.
            self.journal.append(f"conclusion sans preuve : {cause}")
            return Constat("inconnu", f"« {cause} » avancé sans aucune mesure à l'appui",
                           outils_appeles=appels, journal=list(self.journal))
        pos = None
        x, y = args.get("x"), args.get("y")
        if isinstance(x, (int, float)) and isinstance(y, (int, float)) \
                and not isinstance(x, bool) and not isinstance(y, bool):
            pos = (float(x), float(y))
        return Constat(cause, preuve[:200], pos, appels, list(self.journal))