"""Les sondes qu'un modèle peut appeler — déclaration, garde et troncature.

Extrait de `agents/enqueteur.py`, où ce mécanisme a fait ses preuves : cinq pannes
trouvées sur six, aucune fausse piste. Il n'y était pas propre à l'enquête — c'est le
contrat général « un modèle demande une mesure, le code la fournit ou la refuse » — et
l'arbitre en avait besoin.

RELEVÉ QUI A MOTIVÉ CETTE EXTRACTION : l'agent qui CONSTATE disposait de six sondes,
celui qui DÉCIDE d'aucune. On demandait à l'arbitre de trancher entre remplir une
machine, bâtir une chaîne ou aller miner, sans qu'il puisse regarder ce que contenait
cette machine. Quand il choisissait mal, on ne mesurait pas son jugement.

Trois règles, et la première est la seule qui protège vraiment :

  - **liste blanche**. Le modèle propose un nom ; rien ne garantit qu'il s'en tienne à
    ceux qu'on lui a décrits. Un outil inconnu est refusé AVANT exécution, pas constaté
    après. Les sondes d'un arbitre restent en LECTURE — miner ou poser n'en sont pas ;
  - **arguments filtrés** sur ceux qu'on a déclarés, pour qu'un paramètre inventé
    n'atteigne jamais l'API ;
  - **sortie tronquée**. `scan_area` peut rendre deux cents entités : les envoyer
    noierait le signal et coûterait cher pour rien.

Une mesure qui échoue rend un message, jamais une exception : un agent ne s'arrête pas
parce qu'une sonde a raté.
"""

from __future__ import annotations

import json
from typing import Any, Optional


def tronquer(valeur: Any, entites_max: int = 12, caracteres: int = 700) -> str:
    """Réponse d'outil réduite à ce qui se raisonne.

    Le prompt court est ce qui rend le raisonnement possible — principe posé par
    l'arbitre, qui n'a jamais vu l'état brut du jeu.
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


def schema_outils(outils: dict, terminal: Optional[dict] = None) -> list[dict]:
    """Le schéma d'appel de fonctions, depuis `{nom: {description, params, requis}}`.

    `terminal` est l'outil qui clôt l'échange — `conclure` pour une enquête, `choisir`
    pour un arbitrage. Il est déclaré comme les autres mais n'est pas une mesure : c'est
    la réponse attendue.
    """
    schema = [{
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
    } for nom, spec in outils.items()]
    if terminal is not None:
        schema.append(terminal)
    return schema


def mesurer(api, outils: dict, nom: str, args: dict,
            journal: Optional[list] = None) -> str:
    """Exécute une sonde de la liste blanche. Tout le reste est REFUSÉ, pas exécuté.

    L'ordre compte : on vérifie l'appartenance à la liste avant de chercher la méthode,
    sinon une API qui exposerait `mine_entity` la rendrait appelable par le seul fait
    d'exister.
    """
    if nom not in outils:
        if journal is not None:
            journal.append(f"outil refusé (hors liste blanche) : {nom!r}")
        return f"outil inconnu : {nom}"
    fn = getattr(api, nom, None)
    if fn is None:
        return f"outil indisponible : {nom}"
    permis = outils[nom]["params"]
    propres = {k: v for k, v in (args or {}).items() if k in permis}
    try:
        return tronquer(fn(**propres))
    except Exception as e:
        return f"mesure impossible : {type(e).__name__}"
