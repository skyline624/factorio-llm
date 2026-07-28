# Architecture des agents — du bootstrap à l'autonomie

Analyse de fond : quelles **étapes** séparent l'état actuel d'un agent autonome, quels
**agents** il faut pour les franchir, et de quels **outils RCON** chacun a besoin.

Ce document **complète** `agents-roadmap.md` (qui pose la vision : FactoryBuilder comme
agent-pivot, un agent seulement s'il y a arbitrage). Il ne la remplace pas. Il ajoute
trois choses que la roadmap n'a pas :

1. la confrontation à l'**état de l'art mesuré** (benchmark FLE) — qui dit précisément
   où les LLM échouent en Factorio, donc ce qu'il ne faut *pas* leur confier ;
2. le **chemin critique côté mod** — la liste ordonnée des primitives RCON manquantes,
   avec la fonction d'API Factorio 2.x correspondante ;
3. une **fiche par agent** : objectif, arbitrage justifiant un LLM, outils RCON,
   services appelés, et surtout **critère de vérification** — comment on sait qu'il a
   réussi, mesuré dans le jeu et non déclaré par le modèle.

## 0. Écart entre la roadmap et le code réel (à savoir avant de lire)

`agents-roadmap.md` a été écrit avant l'implémentation. Trois écarts :

| Roadmap | Réalité du dépôt |
|---|---|
| `services/geometer.py`, `services/messenger.py` | **N'existent pas.** La géométrie a été absorbée par `layout_planner.py` ; aucun bus de messages n'a été écrit. |
| « Itération 0 : messenger + coordinator avant le premier agent » | **Sautée.** On est allé directement aux services de planification (ProductionSolver, LayoutPlanner) puis à FactoryBuilder. Il n'y a **aucun Coordinator**. |
| « socle validé 25/25 » | Daté. Le socle a beaucoup grossi : 14 outils d'observation, 15 actions, 7 services, executor validé headless **et** production. |

Ce document part du code réel.

## 1. Ce que dit l'état de l'art, et ce que ça impose

Le **Factorio Learning Environment** (Hopkins, Bakler, Khan — arXiv 2503.09617) est un
benchmark d'agents LLM sur Factorio, d'architecture **identique à la nôtre** : client
Python + serveur Lua communiquant en RCON. Leurs résultats sont la donnée la plus utile
qu'on puisse avoir pour dimensionner notre architecture.

**Ce qu'ils mesurent** (frontier models, jusqu'à 5000 pas, 8 runs) :

- le meilleur modèle complète **7 tâches sur 24** en lab-play ;
- les agents **échouent à coordonner plus de ~6 machines** dès que l'objet produit a
  **plus de 3 ingrédients**, *même après 128 interactions avec l'environnement* ;
- en open-play, ils trouvent l'automatisation simple (foreuses électriques) mais
  **échouent à automatiser l'electronic-circuit** ;
- coût observé : ~500 USD et 154 M tokens pour un seul run d'un modèle.

**Les trois modes d'échec identifiés** — à lire comme un cahier des charges inversé :

| Mode d'échec | Conséquence pour nous |
|---|---|
| **Raisonnement spatial** (placement de tuyaux/machines inefficace) | Ne JAMAIS demander à un LLM de choisir des coordonnées. → `LayoutPlanner`, `MicroPlanner` (déterministes). Le décalage d'une tuile qu'on a corrigé aujourd'hui dans le MicroPlanner illustre le niveau de précision requis : une demi-tuile d'erreur latérale et la chaîne ne produit rien. |
| **Débogage systémique** (les agents regardent une machine, pas la topologie) | Le diagnostic ne doit pas être prompté. → service **`FactoryDoctor`** déterministe qui remonte la chaîne de production et localise le goulet. |
| **Amélioration itérative faible** (rarement de raffinement après la 1ʳᵉ implémentation) | Il faut une **boucle de contrôle explicite** avec un critère mesuré (débit atteint / non atteint) qui **relance** tant que la cible n'est pas tenue, plutôt que d'espérer que le modèle y repense. |

De **Voyager** (agent Minecraft, arXiv 2305.16291) on retient trois mécanismes
transposables, dont deux nous manquent :

- **curriculum automatique** (proposer la tâche suivante) → notre `Coordinator` ;
- **skill library** (les compétences validées sont stockées et rejouées) → nous
  recalculons tout à chaque fois : à créer (`SkillLibrary`) ;
- **self-verification** (vérifier l'exécution avant de capitaliser) → **déjà notre
  culture** : l'executor ne croit pas `ok=True` et confirme la pose par l'inventaire,
  les tests live prouvent la production et non la pose.

**Conclusion structurante** : notre pari (calcul déterministe + LLM réservé à
l'arbitrage) est exactement la parade aux trois modes d'échec mesurés. Il faut le tenir
strictement : *chaque fois qu'on est tenté de faire décider un LLM, se demander si un
algorithme le ferait — si oui, c'est un service.*

## 2. Principe directeur : la frontière calcul / arbitrage

```
Est-ce que deux experts humains compétents, avec les mêmes données,
donneraient la même réponse ?
   OUI  -> c'est un CALCUL      -> service déterministe, testable sans serveur
   NON  -> c'est un ARBITRAGE   -> agent LLM, avec critère de succès mesurable
```

Conséquence assumée : **peu d'agents, beaucoup de services**. Chaque agent LLM est un
point de non-déterminisme, un coût par token et une source de dérive. Sur Nauvis
entier, 3 à 4 agents suffisent ; tout le reste est calculable.

Contre-exemple utile : « quelle technologie rechercher ensuite ? » ressemble à un
arbitrage, mais dès qu'un objectif est fixé (« red science »), les prérequis se
**déduisent** de l'arbre technologique. C'est un service (`TechResolver`). Seul le choix
de l'objectif est un arbitrage.

## 3. La boucle de contrôle

L'autonomie n'est pas « un agent qui décide », c'est une boucle qui se referme sur une
mesure prise dans le jeu.

```
   OBSERVE      perception : état, usines, débits, recherche, alertes, menaces
      |
   DIAGNOSE     FactoryDoctor (déterministe) : écart débit réel / débit attendu,
      |         remontée de chaîne jusqu'au goulet
      |
   DECIDE       Coordinator : quel objectif maintenant (progresser / scaler /
      |         sécuriser / réparer)
      |
   PLAN         FactoryBuilder + solveurs : BOM -> ratios -> layout -> plan posable
      |
   EXECUTE      Executor : pré-vol, pose ordonnée, alimentation
      |
   VERIFY       mesure dans le jeu (statuts + production_stats sur fenêtre)
      |         -- échec -> retour DIAGNOSE, pas retour DECIDE
      |
   CAPITALISE   SkillLibrary : le plan qui a tenu la cible est mémorisé
```

Deux règles :

- **VERIFY renvoie à DIAGNOSE, pas à DECIDE.** C'est ce qui manque aux agents FLE :
  face à l'échec ils re-décident au lieu de comprendre.
- **La vérification est une mesure de débit sur une fenêtre de temps**, pas un statut
  instantané. `status=working` à l'instant t ne prouve rien : notre chaîne de ce jour
  était `working` puis `no_fuel` deux minutes plus tard. FLE valide ses tâches sur
  **60 secondes de débit soutenu** — c'est le bon critère, à reprendre.

## 4. Les jalons, et ce que chacun exige

`agents-roadmap.md` §2 donne la progression de gameplay. Voici la même progression vue
sous l'angle **« qu'est-ce qui bloque, concrètement »**.

| # | Jalon | État | Ce qu'il exige de nouveau |
|---|---|---|---|
| **J0** | Bootstrap manuel (miner, fondre à la main) | **fait** | — |
| **J1** | Micro-chaîne burner (drill→inserter→four) | **fait** (8/8 headless, 10/10 production) | — |
| **J1.5** | **Correction d'erreur** : retirer/tourner une entité mal posée | **bloqué** | `remove_entity_at`, `rotate_entity_at` |
| **J2** | **Électricité** (offshore-pump → boiler → steam-engine → poles) | **bloqué** | `PowerPlanner`, lecture du réseau électrique |
| **J3** | **Automatisation réelle** : assembleurs + red science | **bloqué** | **`set_recipe`** — sans lui aucun assembleur ne fonctionne |
| **J4** | Main bus + green science (circuits, splitters, underground) | planner **prêt** (S1), pose bloquée | `place_entity_at` étendu : `ug_type`, `priority` |
| **J5** | Défense (murs, tourelles, munitions) | absent | `scan_enemies`, `get_pollution`, `get_alerts` |
| **J6** | Pétrole / fluides / cracking | planner **prêt** (S2) | `get_fluid_contents`, agent `Refiner` |
| **J7** | Trains (gisements distants) | absent | rails, signaux, gares, horaires |
| **J8** | Robots de construction | absent | **ghosts** + roboports → change le mode d'exécution |
| **J9** | Modules / beacons / électricité avancée | planner **prêt** (S3) | `modules` à la pose |
| **J10** | Fusée | absent | — (dérive de J4–J9) |
| **J11** | Space Age (4 planètes) | absent | surfaces multiples, plateformes |

**Lecture de ce tableau** : le projet a une avance considérable côté *planification*
(S1 à S3 du LayoutPlanner savent déjà calculer bus, fluides et beacons) et un retard
côté *actionneurs*. Trois primitives Lua manquantes bloquent J1.5, J3 et J4 — soit
l'essentiel du jeu. **Le chemin critique est dans le mod, pas dans les agents.**

## 5. Catalogue des agents

### A0 — `Coordinator` (stratège) — *à créer*

- **Objectif** : décider **quoi faire maintenant** et tenir un cap sur des heures de jeu.
- **Arbitrage justifiant un LLM** : trancher entre quatre pressions concurrentes —
  *progresser* (jalon suivant), *scaler* (plus de débit), *sécuriser* (défense),
  *réparer* (panne). Aucune formule ne donne la réponse : elle dépend de l'état global,
  du risque et du temps. C'est le curriculum automatique de Voyager.
- **Entrées** : snapshot de perception, diagnostic du `FactoryDoctor`, état de la
  recherche, pollution/menaces, inventaire, liste des usines et de leurs débits.
- **Sorties** : un objectif typé — `ProductionRequest(item, débit, zone)`,
  `ResearchRequest(techno)`, `DefenseRequest(secteur)`, `RepairRequest(usine, symptôme)`.
- **Outils RCON** : `get_state`, `get_tick`, `scan_factory`, `production_stats`,
  *(à créer)* `get_research_state`, `get_alerts`, `get_pollution`.
- **Services** : `TechResolver`, `FactoryDoctor`, `SkillLibrary`.
- **Vérification** : le jalon visé est atteint (milestone mesurable : premier objet d'un
  type produit, technologie acquise).
- **Recommandation forte** : **V1 déterministe.** Le curriculum J0→J4 est connu
  d'avance — une machine à états suffit et sera plus fiable qu'un LLM. Le LLM ne devient
  utile qu'au moment où plusieurs chemins se valent réellement (à partir de J5–J6, quand
  défense, pétrole et scaling entrent en concurrence). Écrire d'abord la boucle, puis y
  brancher le modèle.

### A1 — `FactoryBuilder` (agent-pivot) — *existe, à étendre*

- **Objectif** : transformer un objectif de production en **usine qui produit**.
- **Arbitrage** : choix du gisement (distance / taille / concurrence), du **tier** de
  machines selon les technologies acquises et l'inventaire, du site d'implantation, et
  la décision **replan lourd vs adaptation locale** quand le terrain refuse le plan.
- **Entrées** : `ProductionRequest`, terrain, inventaire, technologies.
- **Sorties** : `LayoutPlan` / `MicroPlan` + `ExecutionReport`.
- **Outils RCON** : `scan_patch`, `scan_water_edge`, `scan_obstacles`,
  `scan_tiles_bbox`, `get_tile`, `generate_terrain`, `describe`, `get_recipe`,
  `can_place_check`, puis via l'executor `walk_to`, `place_entity_at`, `move_items_at`.
- **Services** : `ProductionSolver`, `LayoutPlanner`, `MicroPlanner`, `Executor`,
  *(à créer)* `PowerPlanner`, `CraftPlanner`, `SkillLibrary`.
- **Vérification** : **débit cible soutenu sur 60 s** (`production_stats`), pas « les
  entités sont posées ».
- **Parade FLE intégrée** : aucune coordonnée ne sort du LLM ; le spatial est calculé.

### A2 — `Defender` (satellite) — *à créer, plus tôt que prévu*

- **Objectif** : empêcher la destruction de l'usine.
- **Arbitrage** : *quand* investir dans la défense (chaque minute passée à se défendre
  n'est pas passée à produire), *où* (le nuage de pollution désigne les fronts), *quel
  niveau* (murs seuls / tourelles / laser).
- **Entrées** : carte de pollution, nids et unités ennemies, alertes d'attaque, entités
  détruites, périmètre de l'usine.
- **Sorties** : `DefenseRequest` — segments de mur, positions de tourelles, débit de
  munitions à assurer.
- **Outils RCON** *(tous à créer)* : `scan_enemies(radius)` (nids + unités),
  `get_pollution(x, y)`, `get_alerts()`, `get_entity_health(x, y)`.
- **Services** : `ThreatModel` (déterministe : pollution × distance aux nids →
  probabilité d'attaque par secteur), `LayoutPlanner` (lignes de mur, alimentation des
  tourelles en munitions = un problème de logistique déjà résolu).
- **Vérification** : aucune entité perdue sur N minutes, tourelles approvisionnées.
- **Déclenchement** : dès que le nuage de pollution atteint un nid — c'est-à-dire dès
  **J2–J3**, bien plus tôt que la roadmap ne le laisse penser. Sur cette carte le
  personnage a déjà été tué par des biters lors des tests P2.

### A3 — `Refiner` (satellite fluides) — *à créer, mince*

- **Objectif** : produire et **équilibrer** les produits pétroliers.
- **Arbitrage** : le cracking. Une raffinerie produit trois fluides liés ; si une sortie
  sature, **toute** la raffinerie se bloque. L'équilibre dépend de la demande
  instantanée en plastique / soufre / lubrifiant / carburant.
- **Outils RCON** : `production_stats`, *(à créer)* `get_fluid_contents(x, y)`, puis les
  **circuits** pour automatiser le cracking sur seuil.
- **Services** : `ProductionSolver` (gère déjà les fluides), `LayoutPlanner` S2
  (pipe-bus, séparation validée).
- **Note honnête** : l'essentiel est calculable. Commencer comme **service** ; ne le
  promouvoir en agent que si l'arbitrage de priorité entre débouchés se révèle
  réellement non mécanisable.

### A4 — `Logistician` (trains & robots) — *à créer, J7+*

- **Objectif** : relier des sites distants quand la belt ne suffit plus.
- **Arbitrage** : belt vs train vs robots ; quel gisement desservir en priorité ;
  dimensionnement du parc et des gares.
- **Outils RCON** *(à créer)* : pose de rails et signaux, création de gares, horaires
  (`LuaTrain.schedule`), roboports et **ghosts**.
- **Services** : `RoutePlanner` (pathfinding rail — déterministe), `BlueprintDeployer`.
- **Effet de bord majeur** : avec les robots, on **cesse de marcher**. Poser un ghost
  supprime la contrainte `build_distance` que le mode production nous impose
  aujourd'hui. C'est un changement de nature de l'exécution, pas une optimisation.

### A5 — `Space` (satellite endgame) — *J11*

Arbitrage : quelle planète, quelle cargaison, quand. Trop loin pour être spécifié
utilement ; mentionné pour mémoire.

## 6. Catalogue des services (le gros du travail)

**Existants** : `perception`, `knowledge`, `production_solver`, `layout_planner`,
`micro_planner`, `executor`, `llm`.

**À créer**, par ordre d'utilité :

| Service | Rôle | Pourquoi déterministe |
|---|---|---|
| **`PowerPlanner`** (J2) | Dimensionner et implanter la production électrique : `offshore-pump` → `boiler` (1,8 MW) → `steam-engine` (900 kW) → poles ; vérifier la **couverture** des consommateurs. | Arithmétique pure + géométrie. Le seul « choix » est le site, qui se déduit de `scan_water_edge`. |
| **`FactoryDoctor`** (J3) | Comparer débit attendu / réel, **remonter la chaîne** jusqu'au goulet, produire un symptôme typé (`no_fuel`, `no_power`, `input_starved`, `output_blocked`, `ratio_insuffisant`). | C'est un parcours de graphe. **C'est la parade au mode d'échec n°1 de FLE.** |
| **`CraftPlanner`** (J1–J3) | Que crafter à la main, dans quel ordre, en résolvant les *chicken-and-egg* (l'inserter exige des plaques qui exigent un four…). | Tri topologique sur les recettes + inventaire. |
| **`TechResolver`** (J3) | Prérequis d'une technologie, coût en science packs, ordre de recherche. | Parcours de l'arbre technologique. |
| **`SkillLibrary`** (J3+) | Mémoriser les plans **vérifiés** (objectif, terrain, débit obtenu) et les rejouer au lieu de recalculer. | Stockage + similarité. Réduit le coût LLM et la variance — cf. les 500 USD par run FLE. |
| **`ThreatModel`** (J5) | Pollution × nids → risque par secteur. | Calcul sur la carte. |
| **`RoutePlanner`** (J7) | Tracé rail, signalisation. | Pathfinding. |

## 7. Chemin critique : les primitives RCON manquantes

Classées par ordre de déblocage. La colonne API donne la fonction Factorio 2.x
correspondante — toutes vérifiées dans la doc runtime.

### P0 — bloquent le jeu entier (à faire en premier)

| Primitive proposée | API Factorio 2.x | Ce qu'elle débloque |
|---|---|---|
| `set_recipe_at(x, y, recipe)` | `LuaEntity.set_recipe(recipe, quality?)` | **Tout.** Sans recette, un assembleur ne fait rien. Aucune automatisation au-delà des fours n'est possible aujourd'hui. **Blocage n°1.** |
| `remove_entity_at(x, y)` | `LuaEntity.destroy{...}` (ou `order_deconstruction`) | La **correction d'erreur**. Aujourd'hui `mine_entity(name, count)` cible par *nom* dans un rayon : impossible de retirer une entité précise. Sans ça, une erreur de pose est définitive — or c'est le mode d'échec n°1 des LLM. |
| `place_entity_at` **étendu** : `recipe`, `ug_type`, `priority`, `modules`, `fuel` | `create_entity{... type=, input_priority=, output_priority=}` + `set_recipe` + `get_module_inventory().insert` | Le `LayoutPlan` calcule **déjà** ces champs (S1–S3) ; l'executor ne peut pas les poser. Débloque bus (J4) et beacons (J9). |
| `rotate_entity_at(x, y, direction)` | `LuaEntity.rotate{}` ou `direction =` | Corriger une orientation sans détruire/reposer. |

### P1 — électricité et rétroaction (J2, J3)

| Primitive | API | Usage |
|---|---|---|
| `get_power_state(x, y)` | `LuaEntity.electric_network_id` + statistiques du réseau | Savoir si une machine est **alimentée**, et si le réseau est en déficit. Sans ça, impossible de vérifier une usine électrique. |
| `get_alerts()` | alertes de la force | La rétroaction la moins chère : machine sans énergie, entité détruite, attaque. Alimente `Coordinator` et `FactoryDoctor`. |
| `get_research_state()` | `LuaForce.technologies`, `current_research`, `research_progress`, `research_queue` | Aujourd'hui on lance `research_technology` **à l'aveugle** : on ne sait ni ce qui est acquis, ni où en est la recherche. |

### P2 — fiabilité et montée en charge

`get_fluid_contents(x, y)` (niveaux tanks/pipes, J6) · `scan_enemies(radius)` +
`get_pollution(x, y)` (J5) · `set_inserter_positions(x, y, pickup, drop)`
(`pickup_position` / `drop_position` sont **RW** — corriger une géométrie sans
repose) · `chart(area)` (`LuaForce.chart` — révéler la carte **sans marcher**,
complément de `generate_terrain`).

### P3 — changement d'échelle (J7+)

Rails, signaux, gares, `LuaTrain.schedule` · **ghosts** (`create_entity{name="entity-ghost",
inner_name=...}`) + roboports — supprime la marche et la contrainte de portée ·
circuits (`get_wire_connector`, `get_control_behavior`) · blueprints (import/export
string) — un accélérateur considérable : un blueprint valide remplace des dizaines de
poses unitaires.

## 8. Ordre de chantier recommandé

1. **E2 — mod : les 4 primitives P0.** Petit chantier Lua, effet de levier maximal :
   débloque J1.5, J3, J4 et J9 d'un coup. Rien d'autre ne devrait passer devant.
2. **E3 — `PowerPlanner` + J2 (électricité).** Premier jalon où l'usine **cesse de
   dépendre d'un humain** qui verse du charbon. C'est le vrai sens du mot autonomie ici.
3. **E4 — J3 : première usine automatisée (red science)**, avec `set_recipe` et
   `CraftPlanner`. Critère : débit soutenu 60 s.
4. **E5 — `FactoryDoctor` + `Coordinator` V1 déterministe.** La boucle se referme :
   observer, diagnostiquer, relancer. C'est le passage de « script piloté » à « agent ».
5. **E6 — J4 : main bus + green science** (le LayoutPlanner S1 est prêt et attend).
6. **E7 — `Defender`**, dès que la pollution atteint les nids.
7. Ensuite : J6 fluides (S2 prêt), J7 trains, J8 robots — chacun changeant l'échelle.

Le LLM n'entre vraiment en scène qu'à l'étape 4, et d'abord comme *option* : la boucle
doit tourner sans lui.

## 9. Risques et angles morts

- **Le temps de jeu.** Un run FLE, c'est 5000 pas et des heures. Notre exécution passe
  par la marche du personnage (94 tuiles mesurées pour un chantier) : c'est le principal
  coût caché. Les **robots (J8)** et les **ghosts** sont la sortie structurelle.
- **Le coût LLM.** ~500 USD par run chez FLE. Le `SkillLibrary` et le `Coordinator`
  déterministe ne sont pas des raffinements : ce sont des conditions de viabilité.
- **La vérification est le vrai produit.** Tout ce qui a été gagné aujourd'hui l'a été
  parce que les tests mesurent la *production*, pas la *pose*. Chaque nouvel agent doit
  arriver avec son critère mesuré dans le jeu.
- **Le mode production est le juge.** `test_mode` masque la contrainte de portée et
  téléporte le personnage : un agent validé uniquement en headless n'est pas validé.
- **Angle mort actuel : l'irréversibilité.** Sans `remove_entity_at`, toute erreur de
  construction est permanente. Un agent autonome qui ne peut pas défaire ce qu'il fait
  accumule les dégâts au lieu de converger.

## Sources

- Hopkins, Bakler, Khan — *Factorio Learning Environment*, arXiv:2503.09617 —
  <https://arxiv.org/abs/2503.09617>
- Leaderboard et versions FLE — <https://jackhopkins.github.io/factorio-learning-environment/>
- Wang et al. — *Voyager: An Open-Ended Embodied Agent with LLMs*, arXiv:2305.16291 —
  <https://arxiv.org/abs/2305.16291>
- Factorio Runtime API 2.x — `LuaEntity`, `LuaForce` —
  <https://lua-api.factorio.com/latest/classes/LuaEntity.html>