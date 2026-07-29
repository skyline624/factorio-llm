"""Construction du client LLM, et lecture DÉFENSIVE de ses réponses.

Extrait de `services/arbitre.py` sans rien y changer : deux composants ont maintenant
besoin d'un modèle (l'arbitre qui choisit, l'enquêteur qui cherche), et dupliquer la
construction garantirait que la prochaine correction n'atteigne qu'une copie sur deux.

Le principe est celui de tout le projet : **une défaillance du modèle n'arrête pas
l'agent**. Client absent, service injoignable, réponse illisible, arguments hors format —
chacun de ces cas rend une valeur de repli et laisse une trace dans le journal. Un
agent autonome qui s'arrête parce qu'un service distant est indisponible n'est pas
autonome.
"""

from __future__ import annotations

import json
from typing import Optional


def construire_client(cfg=None, client=None, journal: Optional[list] = None,
                      timeout: Optional[float] = None):
    """(client, cfg) — `client` vaut None si aucun modèle n'est joignable.

    `client` est injectable : les tests n'ont besoin ni de réseau ni de clé.

    `timeout` permet à un appelant d'imposer le sien. Un arbitrage tient en un aller-
    retour et supporte un délai court ; une enquête enchaîne plusieurs tours de
    raisonnement sur un contexte qui grossit, et 30 secondes l'interrompent en plein
    milieu — l'échec ressemble alors à un modèle incapable de conclure.
    """
    if journal is None:
        journal = []
    if cfg is None:
        try:
            from config import load_config
            cfg = load_config()
        except Exception as e:                      # config absente : on le dit
            if client is None:
                journal.append(f"configuration illisible : {e}")
            return client, None
    if client is not None:
        return client, cfg
    try:
        import openai
    except ImportError as e:
        journal.append(f"openai indisponible : {e}")
        return None, cfg
    if getattr(cfg, "openai_base_url", None) and getattr(cfg, "llm_enabled", False):
        return openai.OpenAI(
            api_key=getattr(cfg, "openai_api_key", None) or "ollama",
            base_url=cfg.openai_base_url,
            timeout=timeout if timeout is not None else getattr(cfg, "llm_timeout", 30),
        ), cfg
    journal.append("LLM désactivé (base_url vide ou LLM_ENABLED=false)")
    return None, cfg


def lire_appels(message, journal: Optional[list] = None) -> list[tuple[str, str, dict]]:
    """[(identifiant, nom d'outil, arguments)] d'un message, ou [] s'il est inexploitable.

    L'**identifiant est rendu avec le reste**, et ce n'est pas un détail : la réponse
    d'un outil doit citer l'`id` que le modèle a lui-même émis. En fabriquer de nouveaux
    (`c0`, `c1`...) casse l'appariement, et le tour suivant revient entièrement vide —
    ni outil, ni texte. Le symptôme fait accuser le modèle de ne pas savoir conclure
    alors qu'il ne reçoit tout simplement plus une conversation cohérente.

    Rien n'est supposé de la forme de la réponse : un modèle local peut rendre des
    arguments déjà décodés, une chaîne JSON, ou du texte qui n'en est pas. Chaque cas
    donne une liste vide plutôt qu'une exception au milieu d'une boucle d'agent.
    """
    if journal is None:
        journal = []
    try:
        appels = getattr(message, "tool_calls", None)
    except (AttributeError, TypeError):
        journal.append("réponse sans message exploitable")
        return []
    if not appels:
        return []
    sortie: list[tuple[str, str, dict]] = []
    for i, appel in enumerate(appels):
        try:
            nom = appel.function.name
            brut = appel.function.arguments
            args = json.loads(brut) if isinstance(brut, str) else brut
        except (json.JSONDecodeError, AttributeError, TypeError) as e:
            journal.append(f"arguments illisibles ({type(e).__name__})")
            continue
        if not isinstance(args, dict):
            journal.append(f"arguments hors format pour {nom!r}")
            continue
        identifiant = str(getattr(appel, "id", "") or f"appel_{i}")
        sortie.append((identifiant, str(nom), args))
    return sortie