"""Perception typée — wrappers au-dessus de ModApi (fl_tools).

DRY : un seul point d'accès aux observations du mod, avec normalisation des
incohérences connues (champ quantité des ingredients : `count` dans get_recipe
vs `amount` dans describe — cf. mod/scripts/tools.lua). Tous les agents lisent
l'état via ces helpers, jamais via des appels RCON directs.

Les fonctions sont pures (pas d'état caché) et acceptent l'api injectée
(dépendance, pas global) — SOLID.
"""

from __future__ import annotations

from typing import Optional

from core.mod_api import ModApi
from core.state import GameState


def snapshot(api: ModApi) -> GameState:
    """Snapshot typé complet de l'avatar IA (un appel RCON get_state)."""
    return GameState.from_dict(api.get_state())


def inventory(api: ModApi) -> dict[str, int]:
    """Inventaire normalisé {item: count} de l'avatar IA."""
    return dict(snapshot(api).inventory)


def position(api: ModApi) -> Optional[tuple[float, float]]:
    """Position (x, y) de l'avatar IA, ou None si absent."""
    return snapshot(api).pos_tuple()


def nearest(api: ModApi, name: str) -> Optional[tuple[float, float, int]]:
    """Entité/tile la plus proche d'un nom.

    Retourne (x, y, distance) ou None si rien trouvé (le mod renvoie {} quand
    il n'y a pas de candidat — cf. tools.lua:160-163).
    """
    r = api.find_nearest(name)
    if not isinstance(r, dict) or "x" not in r:
        return None
    return (float(r["x"]), float(r["y"]), int(r.get("distance", 0)))


def production_cumulee(api: ModApi, item: str) -> int:
    """Combien d'unités de `item` l'usine a produites depuis le début. -1 si illisible.

    La statistique du jeu, et non l'inventaire du personnage. L'inventaire ne mesurait la
    production que par accident : il montait parce que l'agent vidait les machines À LA
    MAIN, et il se fige au moment précis où un ramassage automatique existe — on lirait
    « l'usine ne produit plus » quand elle devient autonome. Le compteur du jeu, lui, ne
    dépend pas de la destination des objets.

    Rendre -1 plutôt que 0 sur une lecture impossible : 0 est une valeur PLAUSIBLE (une
    usine neuve n'a rien produit), et la confondre avec une panne de mesure ferait
    conclure « débit nul » là où l'on ne sait pas.
    """
    try:
        brut = api.rcon.query_lua(
            "local s = game.forces.player.get_item_production_statistics(game.surfaces[1]) "
            f"rcon.print(s.get_input_count('{item}'))")
        return int(str(brut).strip())
    except (AttributeError, ValueError, TypeError):
        return -1


def recipe_of(api: ModApi, item: str) -> Optional[list[tuple[str, int]]]:
    """Recette craftable d'un item -> [(ingredient, quantité), ...] ou None.

    None si la recette n'existe pas / est verrouillée (le mod renvoie
    {"error": ...}) — l'item n'est alors pas produit par un assembler.

    NB : get_recipe utilise le champ `count` pour les quantités d'ingredients
    (alors que describe utilise `amount`). On lit `count` puis `amount` en
    fallback par robustesse.
    """
    r = api.get_recipe(item)
    if not isinstance(r, dict) or "error" in r or "ingredients" not in r:
        return None
    out: list[tuple[str, int]] = []
    for ing in r.get("ingredients", []):
        name = ing.get("name")
        if not name:
            continue
        count = ing.get("count", ing.get("amount", 0))
        out.append((name, int(count)))
    return out or None