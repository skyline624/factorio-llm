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