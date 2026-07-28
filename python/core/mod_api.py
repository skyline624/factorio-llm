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
from typing import Any, Optional

from core.rcon import RconClient


class ModApiError(Exception):
    pass


class ModApi:
    def __init__(self, rcon: RconClient):
        self.rcon = rcon

    # ----- bas niveau -----

    def _lua_literal(self, v: Any) -> str:
        if v is None:
            return "nil"          # argument optionnel omis (positionnel en Lua)
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, float)):
            return repr(v)
        if isinstance(v, str):
            escaped = v.replace("\\", "\\\\").replace('"', '\\"')
            return f'"{escaped}"'
        if isinstance(v, dict):
            # Table Lua. Cles TOUJOURS en ["..."] : les noms d'items contiennent des
            # tirets ("speed-module-3"), illegaux en cle nue.
            inner = ", ".join(f"[{self._lua_literal(str(k))}] = {self._lua_literal(val)}"
                              for k, val in v.items())
            return "{" + inner + "}"
        if isinstance(v, (list, tuple)):
            return "{" + ", ".join(self._lua_literal(i) for i in v) + "}"
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

    # ----- fl_tools : validation LayoutPlanner (S0b) -----
    # Commandes synchrones non destructives (sauf measure_entity qui pose puis detruit,
    # reserve au mode test). Valident qu'un blueprint est placable + mesurent les
    # geometries reelles pour confirmer le hardcode Python (cf. constat API 2.0).

    def can_place_check(self, name: str, x: float, y: float, direction: str = "north") -> dict:
        """Test non destructif de placabilite (surface.can_place_entity). Retourne
        {name, x, y, can_place, error?}. direction = 'north'|'east'|'south'|'west'."""
        return self._call("fl_tools", "can_place_check", name, x, y, direction)

    def scan_patch(self, resource: str, radius: float = 400.0) -> dict:
        """Bbox + count + total_amount d'un gisement reel autour de l'avatar
        (chaque tuile = 1 entite resource ; total_amount = somme des initial_amount)."""
        return self._call("fl_tools", "scan_patch", resource, radius)

    def scan_water_edge(self, radius: float = 200.0) -> dict:
        """Tiles d'eau adjacents à terre (bord d'un plan d'eau) pour offshore-pump.
        Retourne {tiles:[{x,y}], bbox, count, origin}. Non destructif."""
        return self._call("fl_tools", "scan_water_edge", radius)

    # ----- fl_tools : observation terrain (S4a) -----
    # Non destructif. Donne au LayoutPlanner la visibilité terrain (obstacles organiques,
    # tuiles sur bbox arbitraire, tuile ponctuelle) pour le replan auto S4b.

    def scan_obstacles(self, radius: float = 400.0) -> dict:
        """Obstacles organiques (rochers/arbres/cliffs) autour de l'avatar.
        Retourne {obstacles:[{x,y,w,h,name,type}], bbox, count, origin}. Non destructif.
        bbox = bounding_box floored (x1,y1,w,h). Sert au LayoutPlanner S4 (contournement)."""
        return self._call("fl_tools", "scan_obstacles", radius)

    def scan_tiles_bbox(self, x1: float, y1: float, x2: float, y2: float) -> dict:
        """Toutes les tuiles dans une bbox arbitraire (pas de filtre name).
        Retourne {tiles:[{x,y,name}], bbox, count}. Cap aire 200x200 côté mod. Non destructif.
        Sert a peupler Terrain.tile_grid (water/out-of-map précis au niveau tuile)."""
        return self._call("fl_tools", "scan_tiles_bbox", x1, y1, x2, y2)

    def get_tile(self, x: float, y: float) -> dict:
        """Nom de la tuile a une position. Retourne {x,y,name}. Non destructif.
        Sert a vérifier ponctuellement water/out-of-map (frontière headless)."""
        return self._call("fl_tools", "get_tile", x, y)

    def generate_terrain(self, x: float, y: float, radius: float = 30.0) -> dict:
        """Genere les chunks autour de (x, y) SYNCHRONE (resout le CONSTAT S1d/S1g
        out-of-map). request_to_generate_chunks + force_generate_chunk_requests (API
        Factorio 2.0). Sans ceci, walk_to (pathfinding) ne peut pas planifier vers du
        out-of-map -> le character ne s'y rend jamais. radius en tuiles (cap 200 cote
        mod, converti en chunks de 32 tuiles). Non destructif (cree du terrain vierge).
        Retourne {x, y, radius_chunks, generated, total} ou {error}."""
        return self._call("fl_tools", "generate_terrain", x, y, radius)

    def measure_entity(self, name: str, x: float, y: float, direction: str = "north") -> dict:
        """Pose + mesure (size, pickup/drop_position, belt_speed, mining_drill_radius,
        wire/supply, fluid_boxes instance, output_fluid instance) + detruit. Mode test
        uniquement. Valide le hardcode Python (cf. constat API 2.0 : proto.fluid_boxes
        inaccessible -> fluid_boxes lu sur l'instance posée)."""
        return self._call("fl_tools", "measure_entity", name, x, y, direction)

    def measure_fluid_boxes(self, name: str, x: float = 0.0, y: float = 0.0,
                            direction: str = "north") -> dict:
        """Wrapper S2b-1 : mesure les fluid_boxes d'une machine fluide sur instance posée
        (source de vérité Factorio 2.0, proto.fluid_boxes étant inaccessible au runtime).
        Retourne {name, fluid_boxes:[{production_type, pipe_connections:[{x,y,direction?}]}],
        output_fluid?}. Mode test uniquement (pose puis détruit). Si fluid_boxes absent
        (type non fluide ou API 2.0 opaque), retourne un dict avec fluid_boxes=[] et le
        LayoutPlanner retombe sur le hardcode GEOMETRY_FIXTURE."""
        r = self.measure_entity(name, x, y, direction)
        return {
            "name": name,
            "fluid_boxes": r.get("fluid_boxes", []) if isinstance(r, dict) else [],
            "output_fluid": r.get("output_fluid") if isinstance(r, dict) else None,
        }

    def measure_beacon(self, name: str = "beacon", x: float = 0.0, y: float = 0.0,
                       direction: str = "north") -> dict:
        """Wrapper S3b : mesure un beacon sur instance posée (supply_area_distance lisible
        via ent.prototype ; module_slots/distribution_effectivity CONSTAT probablement
        inaccessibles -> nil, fallback BEACON_FIXTURE). Round-trip modules : insert 2
        speed-module-3 + lecture get_module_inventory (accessible sur l'instance). Mode
        test uniquement (pose + insert + lit + détruit). Retourne {name, supply_area_distance,
        beacon:{module_slots?, distribution_effectivity?, allowed_effects?}, modules?}."""
        r = self.measure_entity(name, x, y, direction)
        if not isinstance(r, dict):
            return {"name": name, "error": str(r)}
        return {
            "name": name,
            "supply_area_distance": r.get("supply_area_distance"),
            "beacon": r.get("beacon") or {},
            "modules": r.get("modules") or [],
        }

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

    def place_entity_at(self, entity_name: str, x: float, y: float, direction: str = "north",
                        opts: Optional[dict] = None) -> dict:
        """Pose une entite a une position (garde de reach + can_place en prod).

        `opts` (E2) : {recipe, ug_type, priority_in, priority_out, modules, fuel,
        fuel_count} — les champs que le LayoutPlanner calcule deja et qu'on ne savait
        pas poser. Appliques APRES la creation ; l'echec d'une option n'annule pas la
        pose mais est consigne dans le detail (une entite mal reglee se corrige avec
        set_recipe_at / rotate_entity_at, un trou dans la chaine non).
        """
        return self._call("fl_ops", "place_entity_at", entity_name, x, y, direction, opts)

    def remove_entity_at(self, x: float, y: float, entity_name: Optional[str] = None) -> dict:
        """Retire l'entite a la position (rayon 1.5) ; les items reviennent a l'inventaire.

        Sans `entity_name` : entite de notre force la plus proche, a defaut une entite
        minable (arbre / rocher) — degager un emplacement est un usage prevu.
        Contrairement a mine_entity (qui cible par NOM dans un rayon), cette primitive
        vise une position precise : c'est ce qui rend une erreur de pose reparable.
        """
        return self._call("fl_ops", "remove_entity_at", x, y, entity_name)

    def rotate_entity_at(self, x: float, y: float, direction: str,
                         entity_name: Optional[str] = None) -> dict:
        """Oriente une entite deja posee. Direction ABSOLUE, pas un cran de rotation."""
        return self._call("fl_ops", "rotate_entity_at", x, y, direction, entity_name)

    def set_recipe_at(self, x: float, y: float, recipe: Optional[str],
                      entity_name: Optional[str] = None) -> dict:
        """Regle la recette d'une machine posee (None efface la recette).

        Verrou de toute automatisation au-dela des fours : un assembleur sans recette
        ne produit rien.
        """
        return self._call("fl_ops", "set_recipe_at", x, y, recipe, entity_name)

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