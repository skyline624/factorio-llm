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


def centrales(api: ModApi,
              types: tuple[str, ...] = ("boiler", "generator", "offshore-pump")
              ) -> list[dict]:
    """Les organes de production électrique de l'agent, OÙ QU'ILS SOIENT.

    Le diagnostic passe par `scan_area`, centré sur le personnage, et ne voit donc que
    l'usine. Or les centrales se posent au bord de l'EAU : mesuré, l'usine était en
    (-27,-60) et ses quatre centrales entre (15,-2) et (46,36), soit soixante à cent
    vingt tuiles plus loin — hors de portée du diagnostic. Deux de leurs boilers étaient
    à sec (`fuel=0`), les moteurs à l'arrêt, la production nulle, et rien ne le signalait :
    l'agent ne peut pas réparer ce qu'il ne regarde pas.

    On lit leur emprise en un appel, puis leurs lignes complètes par `inspect_at` — qui
    est centré sur un POINT, lui. Passer par le mod plutôt que par du Lua brut évite de
    réimplémenter le mapping des statuts (`status_name`), dont l'entier ne se lit pas.
    """
    noms = ",".join(f"'{t}'" for t in types)
    try:
        brut = api.rcon.query_lua(
            f"local s = game.surfaces[1] "
            f"local x1, y1, x2, y2 = 1e9, 1e9, -1e9, -1e9 local n = 0 "
            f"for _, e in pairs(s.find_entities_filtered{{type={{{noms}}}}}) do "
            f"  local p = e.position n = n + 1 "
            f"  if p.x < x1 then x1 = p.x end if p.x > x2 then x2 = p.x end "
            f"  if p.y < y1 then y1 = p.y end if p.y > y2 then y2 = p.y end end "
            f"if n == 0 then rcon.print('') else "
            f"rcon.print(x1 .. ',' .. y1 .. ',' .. x2 .. ',' .. y2) end")
    except Exception:
        return []
    morceaux = str(brut).strip().split(",")
    if len(morceaux) != 4:
        return []
    try:
        x1, y1, x2, y2 = (float(v) for v in morceaux)
    except ValueError:
        return []
    cx, cy = (x1 + x2) / 2.0, (y1 + y2) / 2.0
    # +8 : une emprise réduite à un point donnerait un rayon nul, et un boiler fait 3×2.
    rayon = max(abs(x2 - x1), abs(y2 - y1)) / 2.0 + 8.0
    r = api.inspect_at(cx, cy, rayon)
    lignes = r.get("entities", []) if isinstance(r, dict) else []
    return [e for e in lignes if e.get("type") in types]


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