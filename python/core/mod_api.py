"""Wrapper typé des interfaces RCON du mod factorio-llm.

Le mod expose :
  fl_tools : observation synchrone -> get_state, get_tick, scan_area, scan_factory,
             find_nearest, describe, get_recipe, production_stats
  fl_ops   : action asynchrone      -> walk_to, walk_to_entity, teleport_to,
             mine_entity, place_entity_at, move_items, move_items_at, wait,
             craft_item, research_technology, set_test_mode, setup, status, cancel

Mode dual : production (joueur connecte, physique reelle) / test headless (character
spawné, actions simulees). `set_test_mode(True)` bascule au runtime.

Chaque appel est traduit en `remote.call("iface","method", args...)` envoye via
/silent-command. Les fonctions du mod repondent par rcon.print(json), on parse
donc le retour en JSON.
"""

from __future__ import annotations

import json
from typing import Any

from core.rcon import RconClient


class ModApiError(Exception):
    pass


class ModApi:
    def __init__(self, rcon: RconClient):
        self.rcon = rcon

    # ----- bas niveau -----

    def _lua_literal(self, v: Any) -> str:
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        raise ModApiError(f"type non serialisable en literal Lua: {type(v)}")

    def _call(self, interface: str, method: str, *args: Any) -> Any:
        parts = [f'"{interface}"', f'"{method}"'] + [self._lua_literal(a) for a in args]
        lua = f"remote.call({', '.join(parts)})"
        raw = self.rcon.query_lua(lua)
        raw = raw.strip()
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as e:
            raise ModApiError(f"reponse non-JSON de {interface}.{method}: {raw!r} ({e})")

    # ----- fl_tools (observation) -----

    def get_state(self) -> dict:
        """Etat complet : tick, character (position, sante), inventaire, tache en cours."""
        return self._call("fl_tools", "get_state")

    def get_tick(self) -> dict:
        return self._call("fl_tools", "get_tick")

    def scan_area(self, radius: float = 32.0) -> dict:
        """Entites + ressources dans un rayon autour de l'avatar IA (borne 128/200)."""
        return self._call("fl_tools", "scan_area", radius)

    def scan_factory(self) -> dict:
        """Recense les machines productrices de la force (surface entiere, borne 100)."""
        return self._call("fl_tools", "scan_factory")

    def find_nearest(self, name: str) -> dict:
        """Entite/tile la plus proche du nom donne (rayon 400), filtre les tiles sous foreuse."""
        return self._call("fl_tools", "find_nearest", name)

    def describe(self, name: str) -> dict:
        """Lookup autoritaire : recette + mecaniques d'entite placeable (JSON)."""
        return self._call("fl_tools", "describe", name)

    def get_recipe(self, item: str) -> dict:
        """Ingredients + enabled d'une recette (JSON homogene)."""
        return self._call("fl_tools", "get_recipe", item)

    def production_stats(self) -> dict:
        """Compteurs cumules de production/consommation de la force."""
        return self._call("fl_tools", "production_stats")

    # ----- fl_ops (action) -----

    def walk_to(self, x: float, y: float) -> dict:
        """Enfile une marche (pathfinding request_path) vers (x, y). Retour immediat {ok, detail}."""
        return self._call("fl_ops", "walk_to", x, y)

    def walk_to_entity(self, entity_name: str, search_radius: float = 100.0) -> dict:
        """Marche vers l'entite la plus proche du nom/type donne (reachability-aware,
        essaye plusieurs candidats). Retour immediat {ok, detail}."""
        return self._call("fl_ops", "walk_to_entity", entity_name, search_radius)

    def teleport_to(self, x: float, y: float) -> dict:
        """Deplacement instantane (reserve au mode test ; refuse en production)."""
        return self._call("fl_ops", "teleport_to", x, y)

    def mine_entity(self, entity_name: str, count: int = 1) -> dict:
        """Mine `count` entites du nom/type donne (mining_state anime en prod)."""
        return self._call("fl_ops", "mine_entity", entity_name, count)

    def place_entity_at(self, entity_name: str, x: float, y: float, direction: str = "north") -> dict:
        """Pose une entite a une position (garde de reach + can_place en prod)."""
        return self._call("fl_ops", "place_entity_at", entity_name, x, y, direction)

    def move_items(self, item_name: str, entity_name: str, max_count: int = 0,
                   to_entity: bool = True) -> dict:
        """Deplace des items entre l'inventaire IA et les entites du nom (rayon 32).
        to_entity=True -> joueur->entite ; False -> entite->joueur. max_count=0 = infini."""
        return self._call("fl_ops", "move_items", item_name, entity_name, max_count, to_entity)

    def move_items_at(self, item_name: str, entity_name: str, x: float, y: float,
                      max_count: int = 0, to_entity: bool = True) -> dict:
        """Idem move_items mais entite unique a la position (rayon 1.5)."""
        return self._call("fl_ops", "move_items_at", item_name, entity_name, x, y, max_count, to_entity)

    def wait(self, ticks: int) -> dict:
        """Attend `ticks` ticks (reels)."""
        return self._call("fl_ops", "wait", ticks)

    def craft_item(self, item_name: str, count: int = 1) -> dict:
        """Lance le craft de `count` objets (file de craft reelle en prod)."""
        return self._call("fl_ops", "craft_item", item_name, count)

    def research_technology(self, technology_name: str) -> dict:
        """Lance la recherche d'une technologie (force-level, pole tech.researched)."""
        return self._call("fl_ops", "research_technology", technology_name)

    def set_test_mode(self, enabled: bool) -> dict:
        """Bascule le mode dual au runtime. True -> test headless, False -> production."""
        return self._call("fl_ops", "set_test_mode", enabled)

    def setup(self) -> dict:
        """Initialise l'avatar IA (idempotent) : cree le character headless (test) ou
        donne le kit de depart au joueur connecte (prod)."""
        return self._call("fl_ops", "setup")

    def reset_character(self) -> dict:
        """Detruit et recree le character headless avec le kit integral (mode test
        uniquement). Rend les tests d'integration reproductibles sans relancer le
        serveur. Refuse en production (ne touche pas le character d'un joueur)."""
        return self._call("fl_ops", "reset_character")

    def status(self) -> dict:
        """Etat de la tache courante : {state: busy|idle, ...}."""
        return self._call("fl_ops", "status")

    def cancel(self) -> dict:
        return self._call("fl_ops", "cancel")

    # ----- helper d'attente / dispatch -----

    def wait_until_idle(self, timeout: float = 30.0, poll_interval: float = 0.25,
                        prev_seq: int | None = None) -> dict:
        """Sonde status() jusqu'a ce que le mod soit idle (ou timeout).

        Si ``prev_seq`` est fourni (mode race-free), on attend que la sequence de
        completion du mod avance au-dela de prev_seq : cela garantit qu'on capture
        bien la completion de la NOUVELLE tache et non le ``last_result`` laisse par
        la precedente (fenetre idle entre enqueue et dispatch). Sans prev_seq, on
        retombe sur le comportement historique (attente de state != busy).
        """
        import time
        deadline = time.monotonic() + timeout
        last = self.status()
        if prev_seq is not None:
            while last.get("seq", 0) <= prev_seq and time.monotonic() < deadline:
                time.sleep(poll_interval)
                last = self.status()
            return last
        while last.get("state") == "busy" and time.monotonic() < deadline:
            time.sleep(poll_interval)
            last = self.status()
        return last

    def run_action(self, action, *args, timeout: float = 30.0,
                   poll_interval: float = 0.25) -> dict:
        """Cycle race-free enfile+attente pour une action fl_ops.

        1. lit le ``seq`` courant (celui du dernier resultat connu),
        2. enfile l'action (``action`` = callable, ex. ``self.walk_to_entity``),
        3. attend que la completion_seq avance (completion de la nouvelle tache).

        Retourne le ``last_result`` de la tache (ou l'ack si l'enfile lui-meme echoue).
        """
        seq0 = self.status().get("seq", 0)
        ack = action(*args)
        if isinstance(ack, dict) and ack.get("ok") is False:
            return ack
        res = self.wait_until_idle(timeout=timeout, poll_interval=poll_interval, prev_seq=seq0)
        lr = res.get("last_result") if isinstance(res, dict) else None
        return lr if isinstance(lr, dict) else ack