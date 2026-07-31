"""FactoryBuilder (P1/P1b) — agent-pivot de production.

Transforme un objectif de production (Contract.goal) en un plan exécutable, en
analysant la recette + l'inventaire, puis en l'exécutant via BaseAgent.act.

**P1** = DÉTERMINISTE (services/knowledge.plan_production).
**P1b** = décision LLM (services/llm.LLMPlanner) avec fallback déterministe si
le plan LLM est invalide ou le LLM injoignable. Chaîne couverte : iron-ore ->
iron-plate -> iron-gear-wheel (+ copper idem). Arbitrage selon l'inventaire :
déjà assez -> crafter direct ; pas assez -> produire l'ingrédient manquant.

La décision est déléguée à un `Planner` injecté (DIP). FactoryBuilder construit
un `DeterministicPlanner` par défaut si aucun planner n'est fourni (lié à l'API
pour résoudre les recettes via perception.recipe_of — rétro-compatibilité P1).
FactoryBuilder n'importe jamais openai directement.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Optional

from core.state import GameState
from services import knowledge, perception
from services.llm import DeterministicPlanner, PlanContext, Planner

from .base import BaseAgent


# Kit de départ du mod (player.lua STARTING_ITEMS) — contexte fourni au LLM.
KIT: dict[str, int] = {
    "wood": 50, "coal": 100, "stone": 50, "iron-plate": 50, "copper-plate": 30,
    "burner-mining-drill": 4, "stone-furnace": 4, "burner-inserter": 10,
    "transport-belt": 50, "small-electric-pole": 20, "pipe": 20, "iron-chest": 4,
}

# Contraintes physiques communiquées au LLM (cf. knowledge._smelt_ticks, ratio coal).
_CONSTRAINTS: dict = {
    "smelt_ticks_per_plate": 220,
    "smelt_min_ticks": 600,
    "coal_per_plate_prompt": 1,   # conservatif (incite à mettre assez de coal)
}


class FactoryBuilder(BaseAgent):
    """Agent-pivot : analyse -> plan -> exécution d'une usine de production."""

    def __init__(self, api, contract, planner: Planner | None = None):
        super().__init__(api, contract, planner)
        # Planner par défaut (rétro-compatibilité P1) : déterministe, résout les
        # recettes via perception.recipe_of lié à l'API.
        if self.planner is None:
            self.planner = DeterministicPlanner(
                lambda item: perception.recipe_of(self.api, item))

    def decide(self, gs: GameState) -> list[knowledge.Step]:
        ctx = self._build_context(gs)
        res = self.planner.plan(self.contract.goal, dict(gs.inventory), ctx)
        self.last_plan = res
        print(f"[agent] plan source={res.source} reason={res.reason} "
              f"({len(res.steps)} etapes)")
        return res.steps

    def _build_context(self, gs: GameState) -> PlanContext:
        """Assemble le contexte figé passé au planner (position + recette + kit)."""
        return PlanContext(
            position=gs.pos_tuple(),
            goal_recipe=perception.recipe_of(self.api, self.contract.goal.item),
            kit=dict(KIT),
            constraints=dict(_CONSTRAINTS),
        )

    def run_loop(self, stepplanner=None, max_iters: int = 30) -> list[dict]:
        """Boucle d'actions LLM pas-à-pas (P1c).

        Construit un `LLMStepPlanner` (lié à l'API pour résoudre les recettes)
        si aucun n'est fourni, puis délègue à `BaseAgent.run_loop`. Le contexte
        est assemblé via `_build_context` (réutilisé P1b).
        """
        if stepplanner is None:
            from config import load_config
            from services.llm import LLMStepPlanner
            cfg = load_config()
            stepplanner = LLMStepPlanner(
                cfg, recipe_lookup=lambda it: perception.recipe_of(self.api, it))
            model = cfg.openai_model
        else:
            model = getattr(stepplanner, "cfg", None)
            model = getattr(model, "openai_model", "?") if model is not None else "?"
        ctx = self._build_context(self.perceive())
        print(f"[agent] === boucle P1c pour {self.contract.goal.item} "
              f"x{self.contract.goal.count} model={model} ===")
        return super().run_loop(stepplanner, ctx, max_iters=max_iters)

    def _fallback_recipe_lookup(self):
        """Résolution de recette via l'API pour le fallback déterministe final."""
        return lambda it: perception.recipe_of(self.api, it)

    # ===== S4c : arbitrage replan lourd autour du LayoutPlanner =====
    #
    # Frontière LLM/déterministe (spec §7) : le LayoutPlanner est déterministe pur (replan
    # auto S4b = shift cascade_offset_v / pivot facing, règles fixes). FactoryBuilder ARBITRE
    # le replan LOURD (changer de gisement cible, monter tier belt) — décision stratégique qui
    # peut être LLM (optionnel) ; ici déterministe par défaut. Si le planner épuise son budget
    # (obstacle_blocking), FactoryBuilder essaie un autre gisement (scan_patch rayon croissant)
    # puis un tier belt supérieur (cascade plus compacte). Si tout échoue -> retourne le best
    # layout (abandon/handoff coordinator).

    # Tiers belt essayés (yellow -> red -> blue) pour compacter la cascade si terrain bloqué.
    _BELT_TIERS = ("transport-belt", "fast-transport-belt", "express-transport-belt")
    # Rayons de scan croissants pour trouver un gisement alternatif (replan lourd).
    _SCAN_RADII = (400, 800, 1200)

    def build_layout(self, splan, geometry, resource: Optional[str] = None,
                     facing: int = 2, anchor: Optional[tuple] = None) -> object:
        """S4c : arbitre le replan lourd (gisement/tier) autour du LayoutPlanner.

        1. Ressource = item du noeud mine du splan (ou override). Si pas de mine -> terrain
           vide + plan direct (pas de gisement à scanner).
        2. Pour chaque gisement (scan_patch, rayon croissant si replan lourd) :
           a. Construit Terrain (scan_obstacles/scan_water_edge/scan_tiles_bbox autour anchor).
           b. Pour chaque tier belt : appelle plan() (replan auto S4b via contract.replan_budget).
              - feasibility=ok -> retourne le LayoutPlan.
              - obstacle_blocking -> essaye tier suivant puis gisement suivant (best retenu).
              - autre (missing_geometry/patch) -> retourne tel quel (pas terrain).
        3. Si tout échoue -> retourne le best LayoutPlan (obstacle_blocking, handoff/abandon).

        Déterministe par défaut (LLM optionnel pour la décision stratégique gisement/tier).
        Limitation : scan_patch est centré avatar (pas point arbitraire) -> multi-gisement
        limité au rayon croissant (workaround : find_nearest itéré)."""
        from services.layout_planner import (
            LayoutRequest, LayoutConstraints, Terrain, ResourcePatch, plan,
        )

        # TOUTES les mines du plan, pas seulement la première. `next(...)` retenait le
        # premier nœud `mine` et le terrain ne portait donc qu'un gisement — ce qui suffit
        # tant qu'on fabrique des plaques de fer, et cesse de suffire dès le premier
        # produit à deux branches. Mesuré : le premier flacon de science venu réclame du
        # fer ET du cuivre ; le LayoutPlanner sait poser les deux (test_multi_ingredient),
        # c'est ici que la seconde ressource se perdait.
        ressources = self._ressources_du_plan(splan, resource)
        resource = ressources[0] if ressources else None
        if resource is None:
            # Pas de mine : pas de gisement. Terrain vide + plan direct (back-compat).
            c = self._merge_constraints("transport-belt", LayoutConstraints())
            req = LayoutRequest(plan=splan, terrain=Terrain(), anchor=anchor or (0.0, 0.0),
                                facing=facing, constraints=c)
            return plan(req, geometry)

        best: Optional[object] = None
        deja_tentees: set = set()
        # LE GISEMENT LOCAL D'ABORD, l'agrégat seulement en dernier recours. `scan_patch`
        # rend un bbox AGRÉGÉ : à rayon 400 il enveloppe tout le minerai du secteur d'une
        # seule emprise. Mesuré : un gisement de cuivre réellement large de 25×27 tuiles
        # était rendu 88×578, et le LayoutPlanner — qui n'a aucune raison de douter du
        # terrain qu'on lui donne — étalait l'usine sur 577 tuiles et 1126 belts pour une
        # chaîne de dix étages. `_scan_patch_local` existe précisément pour cela (« le plus
        # petit rayon qui trouve du minerai, pas l'agrégat ») ; elle n'était pas appelée
        # ici. Les rayons croissants gardent leur rôle : chercher AILLEURS quand le
        # gisement local ne permet pas d'implanter.
        for radius in (None,) + self._SCAN_RADII:
            sp = (self._scan_patch_local(resource) if radius is None
                  else self.api.scan_patch(resource, radius))
            if not sp or not sp.get("bbox"):
                continue
            bbox = sp["bbox"]
            anc = anchor or (float(bbox["x1"]), (bbox["y1"] + bbox["y2"]) / 2.0)
            terrain = self._build_terrain(ressources, bbox, anc, eviter=deja_tentees)
            # Ce coin-là est tenté : s'il ne donne rien, le tour suivant cherchera ailleurs.
            for p in terrain.patches:
                for tx in range(int(p.bbox[0]), int(p.bbox[2]) + 1):
                    for ty in range(int(p.bbox[1]), int(p.bbox[3]) + 1):
                        deja_tentees.add((tx, ty))
            for bt in self._BELT_TIERS:
                # Tier non supporté par la géométrie -> skip (ne court-circuite pas le replan lourd
                # en retournant missing_geometry, qui ne distingue pas "tier absent" d'un vrai défaut).
                if geometry.geometry(bt) is None:
                    continue
                c = self._merge_constraints(bt, LayoutConstraints())
                req = LayoutRequest(plan=splan, terrain=terrain, anchor=anc,
                                    facing=facing, constraints=c)
                lp = plan(req, geometry)
                if lp.feasibility == "ok":
                    return lp
                if lp.feasibility == "obstacle_blocking":
                    best = lp if (best is None or len(lp.entities) > len(best.entities)) else best
                    continue
                # missing_geometry/patch/etc. -> pas terrain, retourne tel quel.
                return lp
        return best

    # Rayon retenu autour de la tuile la plus proche pour délimiter « le » gisement. Assez
    # large pour une dizaine de foreuses, assez étroit pour ne pas enjamber le vide qui
    # sépare deux gisements voisins.
    # Resserré sur le CŒUR du gisement. Large, l'emprise englobe les trous du bord :
    # le LayoutPlanner y répartit ses foreuses en croyant le rectangle plein, et une
    # foreuse 2x2 qui déborde sur l'herbe est refusée à la pose — une seule suffit à faire
    # capoter le plan entier (`can_place=False`, zéro entité posée). Près du centre, les
    # tuiles échantillonnées sont denses.
    EMPRISE_GISEMENT = 8
    # Rayon sur lequel on juge qu'une tuile d'ancrage est « dégagée ». Une foreuse fait
    # trois tuiles de côté et sa belt de collecte longe la colonne : huit tuiles autour
    # suffisent à distinguer un coin de gisement libre d'un coin collé à l'usine.
    DEGAGEMENT_ANCRE = 8

    @classmethod
    def emprise_du_gisement(cls, sp: dict, occupes=None) -> Optional[tuple]:
        """L'emprise de MINERAI GARANTI, déduite des tuiles échantillonnées.

        La `bbox` de `scan_patch` enveloppe tous les gisements du rayon, trous compris —
        le mod le dit lui-même : « on ancre sur une tuile du sample, jamais sur le centre
        de la bbox ». Le LayoutPlanner, lui, ne connaît que le bbox du `ResourcePatch`
        qu'on lui donne : il y répartit ses foreuses en le croyant plein, et l'executor
        refuse la pose sur l'herbe (`can_place=False`, mesuré à 112 tuiles).

        On reconstruit donc l'emprise depuis les tuiles réellement vues, bornée autour de
        la plus proche : deux gisements voisins ont des tuiles dans le même échantillon,
        et leur enveloppe commune contiendrait précisément le vide qui les sépare.
        """
        ech = [t for t in ((sp or {}).get("sample") or []) if isinstance(t, dict)]
        if not ech:
            b = (sp or {}).get("bbox")
            return (b["x1"], b["y1"], b["x2"], b["y2"]) if b else None
        # ON N'IMPLANTE PAS SUR CE QUI EST DÉJÀ BÂTI. Les foreuses suivent le gisement et
        # aucun décalage du plan ne les en sort : si l'emprise retenue recouvre la chaîne
        # déjà debout — le cas normal, puisque l'agent a commencé à miner là — le plan est
        # refusé (`obstacle_blocking`) et aucun replan n'y peut rien. On écarte donc les
        # tuiles occupées AVANT de délimiter, pour viser la part encore libre du gisement.
        # Si tout est pris, on garde l'échantillon complet : le refus vaut mieux qu'une
        # emprise inventée.
        if occupes:
            libres = [t for t in ech if (int(t["x"]), int(t["y"])) not in occupes]
            # UNE TUILE LIBRE NE SUFFIT PAS : c'est son ENTOURAGE qui doit l'être. La
            # tuile voisine d'une usine est libre et pourtant inutilisable — les foreuses
            # occupent trois tuiles de côté et la cascade s'étend derrière. On classe donc
            # les candidates par encombrement du voisinage et l'on ancre sur la plus
            # dégagée, à proximité égale. Sans cela, l'emprise recouvrait la chaîne déjà
            # debout et le plan était refusé sans qu'aucun replan puisse y remédier : les
            # foreuses suivent le gisement, elles ne suivent pas la cascade.
            if libres:
                d = cls.DEGAGEMENT_ANCRE
                libres.sort(key=lambda t: sum(
                    1 for dx in range(-d, d + 1) for dy in range(-d, d + 1)
                    if (int(t["x"]) + dx, int(t["y"]) + dy) in occupes))
            ech = libres or ech
        x0, y0 = int(ech[0]["x"]), int(ech[0]["y"])       # la plus proche de l'observateur
        proches = [(int(t["x"]), int(t["y"])) for t in ech
                   if abs(int(t["x"]) - x0) <= cls.EMPRISE_GISEMENT
                   and abs(int(t["y"]) - y0) <= cls.EMPRISE_GISEMENT]
        if not proches:
            proches = [(x0, y0)]
        # UN RECTANGLE PLEIN, PAS UNE ENVELOPPE. L'enveloppe des tuiles vues contient les
        # trous du gisement, et le LayoutPlanner y répartit ses foreuses en la croyant
        # pleine : mesuré, une foreuse plantée sur `sand-3` — zéro minerai, zéro entité —
        # refusée à la pose, et le plan entier abandonné pour elle seule. On fait donc
        # croître un rectangle depuis la tuile la plus proche tant que chaque bande
        # ajoutée est ENTIÈREMENT minérale : ce qu'on transmet est alors constructible par
        # construction, et aucun repère intermédiaire ne peut le trahir.
        dispo = set(proches)
        x1 = x2 = x0
        y1 = y2 = y0
        for _ in range(cls.EMPRISE_GISEMENT * 4):
            grandi = False
            for (nx1, ny1, nx2, ny2) in ((x1 - 1, y1, x2, y2), (x1, y1, x2 + 1, y2),
                                         (x1, y1 - 1, x2, y2), (x1, y1, x2, y2 + 1)):
                if all((tx, ty) in dispo
                       for tx in range(nx1, nx2 + 1) for ty in range(ny1, ny2 + 1)):
                    x1, y1, x2, y2 = nx1, ny1, nx2, ny2
                    grandi = True
            if not grandi:
                break
        return (x1, y1, x2 + 1, y2 + 1)

    @staticmethod
    def _ressources_du_plan(splan, resource: Optional[str] = None) -> list:
        """Les ressources à extraire pour ce plan, dans l'ordre des nœuds `mine`.

        Un override explicite l'emporte (back-compat : un appelant qui nomme sa ressource
        sait ce qu'il veut). Sinon on prend TOUTES les mines, dédoublonnées : la première
        sert d'ancrage, les autres deviennent des gisements du terrain.
        """
        if resource is not None:
            return [resource]
        vues, out = set(), []
        for n in getattr(splan, "nodes", []) or []:
            if getattr(n, "role", "") == "mine" and n.item not in vues:
                vues.add(n.item)
                out.append(n.item)
        return out

    # Fenêtre de relevé du bâti autour de l'ancre. Elle recouvre l'emprise que le
    # LayoutPlanner peut occuper ; au-delà, une entité ne gêne plus.
    FENETRE_BATI = 200

    def _bati_existant(self, anchor: tuple) -> list:
        """Les emprises de ce qui est DÉJÀ construit, en bbox (x1, y1, x2, y2).

        Non destructif : on regarde, on ne touche à rien. Le personnage est écarté — il
        se déplace, et l'executor sait déjà se dégager quand il gêne sa propre pose.
        """
        x1, y1 = int(anchor[0]) - self.FENETRE_BATI, int(anchor[1]) - self.FENETRE_BATI
        x2, y2 = int(anchor[0]) + self.FENETRE_BATI, int(anchor[1]) + self.FENETRE_BATI
        try:
            brut = self.api.rcon.query_lua(
                "local s = game.surfaces[1] local out = {} "
                f"for _, e in pairs(s.find_entities_filtered{{force='player', area="
                f"{{{{{x1},{y1}}},{{{x2},{y2}}}}}}}) do "
                "  if e.type ~= 'character' then local b = e.bounding_box "
                "    out[#out+1] = math.floor(b.left_top.x) .. ',' .. "
                "      math.floor(b.left_top.y) .. ',' .. math.ceil(b.right_bottom.x) "
                "      .. ',' .. math.ceil(b.right_bottom.y) end end "
                "rcon.print(table.concat(out, ';'))")
        except Exception:
            return []
        out = []
        for morceau in str(brut).strip().split(";"):
            bits = morceau.split(",")
            if len(bits) == 4:
                try:
                    out.append(tuple(int(float(v)) for v in bits))
                except ValueError:
                    continue
        return out

    def _build_terrain(self, resource, bbox: dict, anchor: tuple, eviter=None) -> object:
        """Construit Terrain peuplé depuis le RCON (scan_obstacles/water_edge/tiles_bbox).
        Non destructif. tile_grid autour de l'anchor pour précision tuile water/out-of-map.

        `resource` accepte un nom OU une liste de noms (back-compat : un str donne un seul
        patch, comme avant). Le premier est celui dont on a déjà le `bbox` — il a servi à
        choisir l'ancrage ; les suivants sont prospectés ici. Un gisement introuvable n'est
        pas ajouté : le planner rendra `missing_patch:<ressource>`, ce qui NOMME ce qui
        manque, là où un patch silencieusement absent produisait une usine amputée d'une
        branche entière sans que rien ne le signale.
        """
        from services.layout_planner import Terrain, ResourcePatch
        noms = [resource] if isinstance(resource, str) else [n for n in (resource or [])]
        obstacles = []
        r_obs = self.api.scan_obstacles(400)
        if isinstance(r_obs, dict):
            for o in r_obs.get("obstacles", []):
                obstacles.append((o["x"], o["y"], o["x"] + o["w"], o["y"] + o["h"]))
        # LE BÂTI ORIENTE LE CHOIX DU GISEMENT, MAIS N'INTERDIT PAS LE PLAN. Le déclarer
        # comme obstacle de terrain a été essayé : le planner refuse alors d'implanter
        # (`obstacle_blocking`) et, comme les foreuses suivent le gisement — celui-là même
        # où l'agent a commencé à miner — aucun décalage de cascade ne l'en sort. Mesuré :
        # seize replans, un pas porté de 3 à 12 tuiles, un plafond de 64, et toujours zéro
        # entité posée là où l'on en posait trois cent soixante-dix-huit.
        #
        # Il sert donc là où il est utile — écarter les tuiles bâties du choix d'emprise
        # (`emprise_du_gisement`) — et pas là où il stérilise. Les collisions résiduelles
        # sont traitées à la source depuis que le plan est aligné sur la grille du jeu.
        bati = self._bati_existant(anchor)
        water = []
        r_we = self.api.scan_water_edge(200)
        if isinstance(r_we, dict) and r_we.get("bbox"):
            wb = r_we["bbox"]
            water.append((wb["x1"], wb["y1"], wb["x2"], wb["y2"]))
        tile_grid = {}
        sx1, sy1 = int(anchor[0]) - 30, int(anchor[1]) - 60
        sx2, sy2 = int(anchor[0]) + 120, int(anchor[1]) + 120
        r_st = self.api.scan_tiles_bbox(sx1, sy1, sx2, sy2)
        if isinstance(r_st, dict) and "error" not in r_st:
            for t in r_st.get("tiles", []):
                tile_grid[(t["x"], t["y"])] = t["name"]
        # CHAQUE gisement est délimité par ses tuiles, jamais par l'enveloppe du scan —
        # y compris le premier, dont on a pourtant le bbox sous la main : s'y fier faisait
        # planter des foreuses hors minerai. Le bbox reçu ne sert plus que de repli.
        # Les tuiles que le bâti occupe déjà : les gisements les éviteront. `eviter` y
        # ajoute les emprises DÉJÀ TENTÉES : sans cela, « chercher un autre gisement »
        # rendait invariablement le même — `scan_patch` est centré sur l'avatar, donc le
        # coin le plus proche gagne à tous les coups, et les rayons croissants ne
        # servaient à rien. En les écartant, l'agent explore réellement le gisement.
        occupes = set(eviter or ())
        for (ox1, oy1, ox2, oy2) in bati:
            if (ox2 - ox1) * (oy2 - oy1) <= 400:      # une emprise démesurée n'est pas du bâti
                for tx in range(int(ox1), int(ox2) + 1):
                    for ty in range(int(oy1), int(oy2) + 1):
                        occupes.add((tx, ty))
        patches = []
        for rang, nom in enumerate(noms):
            sp = self._scan_patch_local(nom)
            emprise = self.emprise_du_gisement(sp, occupes)
            if emprise is None and rang == 0 and bbox:
                emprise = (bbox["x1"], bbox["y1"], bbox["x2"], bbox["y2"])
            if emprise is not None:
                # LES TUILES, PAS SEULEMENT LA BOÎTE. Un gisement est irrégulier : son
                # rectangle englobant contient de l'herbe, et une foreuse qui déborde
                # dessus est refusée à la pose — une seule suffit à faire capoter le plan.
                # On transmet donc les tuiles réellement vues ; le planner s'en sert pour
                # n'implanter que sur du minerai.
                tuiles = [(int(t["x"]), int(t["y"]))
                          for t in ((sp or {}).get("sample") or []) if isinstance(t, dict)
                          and emprise[0] <= int(t["x"]) <= emprise[2]
                          and emprise[1] <= int(t["y"]) <= emprise[3]]
                patches.append(ResourcePatch(nom, tiles=tuiles, bbox=emprise))
        return Terrain(
            patches=patches,
            obstacles=obstacles, water=water, tile_grid=tile_grid,
        )

    def _merge_constraints(self, belt_tier: str, default_cls) -> object:
        """Merge contract.layout_constraints (si injecté) avec défauts S4b (terrain_check=True,
        replan_budget=contract.replan_budget) + belt_tier forcé (arbitrage tier).

        Si `layout_constraints` est injecté, on RESPECTE son `terrain_check` (back-compat S3d :
        un appelant peut désactiver la détection per-entité en injectant
        `LayoutConstraints(terrain_check=False)`). `replan_budget` vient toujours du Contract
        (l'autorité pour le budget replan). Si None (= défaut S4b), `terrain_check=True`."""
        from services.layout_planner import LayoutConstraints
        base = self.contract.layout_constraints
        if base is None:
            c = LayoutConstraints(terrain_check=True, replan_budget=self.contract.replan_budget)
        else:
            # Respecte base.terrain_check (back-compat S3d explicite) ; replan_budget du Contract.
            c = replace(base, replan_budget=self.contract.replan_budget)
        # constructible_zone depuis contract.zone (point/rayon -> bbox grossier ; None si absent).
        if c.constructible_zone is None and self.contract.zone is not None:
            zx, zy = self.contract.zone
            c = replace(c, constructible_zone=(int(zx) - 60, int(zy) - 60,
                                               int(zx) + 60, int(zy) + 60))
        return replace(c, belt_tier=belt_tier)

    # ===== MicroPlanner : bootstrap compact (drill + inserter + furnace) =====
    #
    # Complément de build_layout pour le cas « production minimale bootstrap » : le
    # LayoutPlanner émet une usine main-bus scalable (40 entités pour 0.3 iron-plate/s) ;
    # build_micro_layout émet une micro-chaîne de 3 entités (1 drill → 1 inserter → 1
    # furnace, tout-burner, sans belt/pole/bus). À utiliser après le flux manuel de
    # bootstrap (les iron-plates du flux manuel servent à crafter l'inserter).
    #
    # Déterministe pur (pas d'LLM) ; pas de terrain check (l'executor fait can_place_check
    # + retry de position, dont la validation que le drill est sur une tuile ore — règle
    # mémoire feedback-production-bootstrap-p2-llm §1, find_nearest non fiable iron-ore).

    # Rayons d'escalade de scan_patch. Le bbox de scan_patch AGRÈGE tous les gisements du
    # carré de rayon r (tools.lua : un seul find_entities_filtered, pas de clustering) :
    # au rayon 400 le bbox observé en live faisait (-259,-91)-(400,46) et son centre
    # tombait sur de l'herbe, à 250 tuiles du minerai le plus proche. On part donc du
    # rayon le plus court qui trouve quelque chose : il isole le gisement local.
    PATCH_RADII: tuple[float, ...] = (50.0, 100.0, 200.0, 400.0)

    def _scan_patch_local(self, resource: str) -> dict:
        """scan_patch au plus petit rayon qui trouve du minerai (gisement local, pas l'agrégat)."""
        last: dict = {}
        for r in self.PATCH_RADII:
            sp = self.api.scan_patch(resource, r)
            last = sp if isinstance(sp, dict) else {}
            if last.get("bbox"):
                last["_radius"] = r
                return last
        return last

    # Écart minimal entre deux ancres proposées. Une micro-chaîne occupe drill (3) +
    # inserter (1) + four (3) : deux ancres voisines d'une tuile décriraient le même
    # emplacement et l'on paierait douze tentatives pour un seul essai utile.
    ECART_ANCRES = 4

    @staticmethod
    def ancres_sur_minerai(sp: dict, facing: int, ecart: int = 0) -> list[tuple]:
        """Toutes les ancres exploitables du gisement, de la plus avancée à la moins.

        `_anchor_on_ore` n'en rendait qu'une — la plus avancée — ce qui suffit pour bâtir
        la PREMIÈRE chaîne et condamne toutes les suivantes : mesuré, une extension
        proposait invariablement (-26.5,-62.5), c'est-à-dire l'emplacement de la chaîne
        déjà posée, et échouait sur `can_place=False` trois fois de suite avant d'être
        abandonnée définitivement. L'inventaire était plein ; seule la place manquait, et
        seulement là.

        Les candidats sont espacés d'au moins `ecart` tuiles : sans cela, douze tuiles de
        `sample` donnent douze ancres qui décrivent le même endroit, et chaque tentative
        coûte un plan et un pré-vol.
        """
        from services.layout_planner import FACING_UNIT

        sample = [(int(t["x"]), int(t["y"])) for t in sp.get("sample", [])
                  if isinstance(t, dict) and "x" in t and "y" in t]
        if not sample:
            return []
        ux, uy = FACING_UNIT.get(facing, (0, 1))
        # Du plus avancé au moins avancé dans le sens de `facing` : la chaîne se déploie
        # de ce côté et sort donc du gisement, où il reste de la place.
        ordonne = sorted(sample, key=lambda t: -(t[0] * ux + t[1] * uy))
        retenus: list[tuple] = []
        for tx, ty in ordonne:
            # Recul d'une tuile vers l'intérieur pour que l'emprise du drill morde le
            # minerai même au bord. Position ENTIÈRE : mesuré live, `create_entity` snappe
            # le drill et le four sur la grille entière ((-17.5,-60.5) -> (-17,-60)).
            a = (float(tx - ux), float(ty - uy))
            if all(max(abs(a[0] - b[0]), abs(a[1] - b[1])) >= ecart for b in retenus):
                retenus.append(a)
        return retenus

    @staticmethod
    def _anchor_on_ore(sp: dict, facing: int) -> Optional[tuple]:
        """La meilleure ancre, c'est-à-dire la première de `ancres_sur_minerai`.

        Conservée telle quelle : c'est le contrat qu'utilisent `build_micro_layout` et
        quatre scripts de vérification, et la première chaîne doit continuer d'être posée
        exactement au même endroit qu'avant.
        """
        candidats = FactoryBuilder.ancres_sur_minerai(sp, facing)
        return candidats[0] if candidats else None

    def build_micro_layout(self, resource: str, geometry: object = None,
                           facing: int = 4, anchor: Optional[tuple] = None) -> object:
        """Bootstrap : micro-chaîne drill→inserter→furnace (3 entités, tout-burner).

        1. scan_patch au plus petit rayon utile -> gisement LOCAL (pas l'agrégat r=400).
        2. anchor = anchor fourni, sinon une tuile réelle du `sample` au bord aval
           (jamais le centre du bbox : un mining-drill hors minerai est refusé à la pose
           par `build_check_type=manual`, mesuré 26/26 en live).
        3. plan_micro(MicroRequest) -> MicroPlan (3 entités, feasibility='ok').

        Retourne un MicroPlan (feasibility='patch' si aucun gisement, ou si scan_patch
        ne renvoie pas de `sample` exploitable — mieux vaut pas de plan qu'un plan posé
        sur de l'herbe).
        Limitation : scan_patch est centré avatar (pas point arbitraire) — cf. build_layout.
        """
        from services.micro_planner import MicroRequest, plan_micro, MicroPlan
        from services.layout_planner import ResourcePatch

        sp = self._scan_patch_local(resource)
        if not sp or not sp.get("bbox"):
            return MicroPlan(
                feasibility="patch",
                notes=[f"scan_patch({resource}): aucun gisement trouvé "
                       f"(rayons {list(self.PATCH_RADII)})"],
            )
        bb = sp["bbox"]
        patch = ResourcePatch(resource,
                              bbox=(int(bb["x1"]), int(bb["y1"]), int(bb["x2"]), int(bb["y2"])))
        note = (f"scan_patch({resource}) r={sp.get('_radius')} count={sp.get('count')} "
                f"bbox=({bb['x1']},{bb['y1']})-({bb['x2']},{bb['y2']})")
        if anchor is None:
            anchor = self._anchor_on_ore(sp, facing)
            if anchor is None:
                return MicroPlan(
                    feasibility="patch",
                    notes=[note, "sample vide : impossible d'ancrer le drill sur du minerai"],
                )
        plan = plan_micro(MicroRequest(patch=patch, facing=facing, anchor=anchor), geometry)
        plan.notes.insert(0, note)
        return plan

    def run_micro_layout(self, resource: str, geometry: object = None,
                         facing: int = 4, anchor: Optional[tuple] = None,
                         **exec_kwargs) -> tuple:
        """Bootstrap de bout en bout : calcule la micro-chaîne PUIS la bâtit en jeu.

        Enchaîne `build_micro_layout` (plan déterministe) et
        `services.executor.execute_micro` (pose + alimentation). Ferme la boucle
        agent → jeu : c'est le premier chemin où FactoryBuilder produit des entités
        réelles à partir d'un objectif, sans script de pose ad hoc.

        `exec_kwargs` est passé tel quel à `execute_micro` (fuel, fuel_count, dry_run,
        generate, approach, retry_offsets, timeout).

        Retourne
        --------
        (MicroPlan, ExecutionReport)
            Le rapport porte l'arbitrage à rendre : `missing` = approvisionnement à
            faire (craft/collecte), `blocked` = replan lourd (autre gisement/tier,
            cf. build_layout S4c). L'executor n'arbitre ni l'un ni l'autre.
        """
        from services.executor import ExecutionReport, execute_micro

        plan = self.build_micro_layout(resource, geometry, facing, anchor)
        if getattr(plan, "feasibility", "ok") != "ok":
            return plan, ExecutionReport(
                notes=[f"plan non exécuté: feasibility={plan.feasibility}"])
        return plan, execute_micro(self.api, plan, **exec_kwargs)