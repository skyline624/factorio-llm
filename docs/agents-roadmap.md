# Roadmap des agents — factorio-llm

Fondée sur les **étapes réelles de gameplay Factorio 2.0 + Space Age** (recherche
web 2026-07-20) et sur la vision de l'utilisateur : **un agent-pivot capable de
concevoir et bâtir une usine de production complète** en analysant recettes,
ratios de production, besoins en ressources et placements. Le minage n'est pas
un agent — c'est une compétence d'exécution au service de l'usine.

Développement **agent par agent**, chacun testé en **headless ET physique**, en
modules interconnectés (communication par messagerie, zéro duplication),
orchestrateur à **plein accès**.

## 1. Vision : FactoryBuilder comme agent-pivot

Le bon découpage n'est PAS « 1 agent par métier » (mine/four/assembler/bande)
— ça fragmente l'unité de décision et duplique la logique de planification. Le
vrai décideur est l'agent qui transforme un **objectif de production** en
**usine** :

```
objectif ("20 red science/min")
  → analyse recette (dépendances, sous-recettes)
  → calcul des ratios (assemblers/fours/mines pour le débit cible)
  → estimation des besoins ressources (fer, cuivre, coal, eau)
  → choix des placements (layout, bandes transporteuses pour relier)
  → exécution (poser machines, bandes transporteuses, inserters, alimenter)
```

- **`FactoryBuilder`** : agent-pivot (LLM, arbitrage). Détient l'analyse et la
  planification. C'est là que le LLM apporte de la valeur (choix de patch,
  ratios, layout, allocation de ressources — non mécanisable).
- **Services d'exécution** (déterministes, partagés, **pas des agents**) :
  `mining`, `smelting`, `assembly`, `logistics` (bandes/inserters), `power`.
  FactoryBuilder les invoque dans l'ordre de son plan. Aucun n'est dupliqué.
- **Agents satellites** : uniquement pour les domaines à **arbitrage
  indépendant** (non réductible au plan d'usine) : `Refiner` (cracking/balance
  fluide), `Defender` (périmètre), `Space` (interplanétaire).

Règle : si une tâche est un algorithme → service partagé, pas d'agent LLM.
Un agent n'existe que s'il y a un **arbitrage**.

## 2. Étapes de gameplay (fondement)

Progression Nauvis (base game) par science packs, puis Space Age :

| Étape | Pack | Besoin de gameplay dominant | Heures |
|---|---|---|---|
| **Bootstrap** | — | Burner miners + stone furnaces + **steam power** | 0–1 |
| **Red** | Automation | Assemblers (gears/inserters) + labs + red science | 1–3 |
| **Green** | Logistics | Circuits + splitters/underground + **main bus** + Steel | 3–10 |
| **Military** | Gray | Murs/tourelles/munitions (si biters) | 5–10 |
| **Blue** | Chemical | **Oil** (pump/refinery/chemical plant/cracking) + blue circuits | 10–20 |
| **Trains/Robots** | — | Rails + signals + construction robots + **mall** | 20–30 |
| **Purple** | Production | Productivity modules + beacons + electric furnaces | 30–40 |
| **Yellow** | Utility | Nuclear + logistics bots + processing units | 30–40 |
| **White** | Space | Rocket silo + satellite | 40–60 |
| **Space Age** | Metallurgic/Electromagnetic/Agricultural/Cryogenic/Promethium | Vulcanus→Fulgora→Gleba→Aquilo (interplanétaire) | 60+ |

Sources : [Factorio Wiki – Planets](https://wiki.factorio.com/Planets),
[Factorio Wiki – Science pack](https://wiki.factorio.com/Science_pack),
[Factorio Guide Hub – First 60 min](https://factorio-wiki.pages.dev/en/guide/how-to-start),
[Game Foundry – First 10h](https://gamefoundry.games/blog/factorio-early-game-guide-first-10-hours),
[Supercraft – Planet order](https://supercraft.host/article/factorio-space-age-planet-order-2026/),
[Factorio Guides – Science packs](https://factorioguides.com/science-packs/).

## 3. Architecture modulaire (anti-duplication)

Trois couches. **Aucune capacité n'est dupliquée** : ce qui est mécanique vit
dans les services partagés (couche 0) ; FactoryBuilder ne fait que des
*décisions*, en appelant les services via le coordinator.

### Couche 0 — Services partagés (déterministes, utilisés par TOUS les agents)

- **`services/perception.py`** — snapshot d'état partagé. Wrap `fl_tools`,
  mis en cache par tick. Un seul appel RCON `get_state`/`scan_*` par tick.
- **`services/knowledge.py`** — connaissances du jeu : ratios smelting
  (48 stone furnaces / yellow belt), ratios science (5:6:5:12:7:7), prérequis
  tech, recettes + dépendances, coût en fer/cuivre/coal/eau. Données pures.
- **`services/geometer.py`** — géométrie : position libre pour un bâtiment
  (can_place + dégagement), aligner une ligne de bandes transporteuses, reach, zone
  réservée. Complète `utils_entity.lua`.
- **`services/messenger.py`** — **bus de messages typés** entre agents.
  Blackboard pub/sub. Canal d'interconnexion : aucun agent n'en appelle un
  autre directement.
- **`services/exec/`** — compétences d'exécution déterministes que FactoryBuilder
  invoque (et que les agents satellites peuvent aussi utiliser) :
  `mining.py`, `smelting.py`, `assembly.py`, `logistics.py` (belts/inserters),
  `power.py`. **Ces modules ne décident pas** — ils exécutent une instruction
  ("pose N fours alignés à partir de X,Y", "alimente ce four en coal").

### Couche 1 — BaseAgent + Coordinator (orchestrateur, plein accès)

- **`agents/base.py` — BaseAgent** : boucle perceive→decide→act. Reçoit un
  **contrat** (objectif, zone allouée, contraintes) du coordinator. N'a accès
  qu'au snapshot + services + messenger + son contrat. Interface minimale
  focalisée (SOLID).
- **`agents/coordinator.py` — Coordinator** : **plein accès**. Détient la
  référence RCON + instancie services + agents. Décide la **phase de gameplay
  courante**. Alloue objectifs/zones/ressources. Arbitre les conflits du
  messenger. Diffuse le snapshot. Peut **piloter directement** n'importe quel
  agent (override) en cas de blocage.

### Couche 2 — Agents

- **`agents/factory_builder.py` — FactoryBuilder** (pivot) : analyse → plan →
  exécution. Reçoit un objectif de production du coordinator, décompose en
  sous-besoins, calcule ratios/placements, invoque les services d'exécution
  pour bâtir l'usine. Décision LLM.
- **Agents satellites** (arbitrage indépendant) : `refiner.py`, `defender.py`,
  `space.py`. Voir §5.

## 4. Communication inter-agents (messagerie)

Pas d'appels directs agent→agent (couplage = duplication). Pattern **blackboard** :

```
FactoryBuilder --[pub: "besoin iron-ore 50/min en zone nord"]--> Messenger
Coordinator     --[sub]--> alloue zone nord à FactoryBuilder
FactoryBuilder --[pub: "usine red science #1 operationnelle"]--> Messenger
Refiner         --[sub: "besoin petroleum"]--> ... Coordinator arbitre
```

Types de messages : `Supply(item, rate)`, `Demand(item, count)`, `ZoneClaim`,
`ZoneRelease`, `Blockage(reason)`, `PhaseChange(phase)`, `FactoryReady(id)`.

## 5. Liste des agents

| # | Agent | Rôle (arbitrage) | Décision LLM | Dépend de |
|---|---|---|---|---|
| 0 | **Coordinator** | Orchestration + phase + arbitrage messagerie | V1 dur, puis LLM | — |
| 1 | **FactoryBuilder** (pivot) | Concevoir + bâtir une usine de production complète | analyse ratios/besoins/placements, enchaîne les services d'exécution | Coordinator |
| 2 | **Refiner** (satellite) | Oil/refinery/cracking + balance fluide + blue circuits | cracking/balance (arbitrage non réductible au plan d'usine) | FactoryBuilder + Power |
| 3 | **Defender** (satellite) | Périmètre défensif + ammo supply | périmètre selon pollution/nids | FactoryBuilder |
| 4 | **Space** (satellite) | Rocket + space platform + interplanétaire (Vulcanus/Fulgora/Gleba/Aquilo) | stratégie endgame | tous |

FactoryBuilder couvre lui-même, via ses services d'exécution, les étapes
bootstrap → red → green → purple/yellow (mining, smelting, assembly, logistics,
power). Les satellites n'apparaissent qu'aux étapes à arbitrage indépendant
(oil, défense, space).

## 6. Paliers de construction de FactoryBuilder (chacun testé headless + physique)

FactoryBuilder se construit par paliers de complexité croissante. Chaque palier
est un test intégrable (headless + physique).

| Palier | Objectif FactoryBuilder | Compétences d'exécution mobilisées | Extension mod (fl_ops/fl_tools) |
|---|---|---|---|
| **P1** | fer brut → plaques → gears (chaîne pilotée par analyse de recette) | mining + smelting + craft | **rien** (socle 25/25) |
| **P2** | automatiser le flux foreuse→four→assembler (belts+inserters) | logistics | **oui** : place belt lines + inserter config |
| **P3** | usine red science autonome (gears+inserters+science→labs) | assembly + logistics | **oui** : set_recipe sur assemblers + lab feed |
| **P4** | main bus + multi-recettes (green science) | logistics (splitters/underground) | (suite logistics) |
| **P5** | usine blue science | refinery/cracking → délègue à `Refiner` | **oui** : fluid handling complet |
| **P6** | power dédié (steam→solaire→nuclear) + trains + modules | power + transport | **oui** : pipes, rails, module slots |
| **P7+** | rocket + space + interplanétaire | délègue à `Space` | **oui** : surfaces + cargo |

### Détail P1 (réalisable dès maintenant, sur socle 25/25)

Objectif : « produire 5 iron-gear-wheel ». FactoryBuilder :

1. **Analyse** (`knowledge.py`) : iron-gear-wheel = 2 iron-plate → 1 gear ⇒
   besoin 10 iron-plate = 10 iron-ore (furnace 1:1) ⇒ besoin d'un patch fer +
   coal pour le four.
2. **Perception** : `find_nearest("iron-ore")` + `find_nearest("coal")`.
3. **Plan** : marcher → miner 10 ore → poser 1 stone-furnace → coal+ore dedans
   → attendre → récupérer plaques → crafter 5 gears.
4. **Exécution** : `walk_to_entity` → `mine_entity` → `place_entity_at` →
   `move_items` (coal, ore) → `wait` → `move_items` (plaques) → `craft_item`.

Toutes ces opérations sont validées 25/25. P1 prouve qu'un agent peut
**autonomement passer d'une recette à une usine** (ici minimale, manuelle) en
décidant l'enchaînement — sans script codé en dur.

## 7. Itération 0 (avant FactoryBuilder) — socle de coordination

Itération 0 = valider la coordination + messagerie **avant** le premier vrai
agent. Livrables :
- `services/perception.py` (snapshot partagé, cache par tick).
- `services/messenger.py` (blackboard pub/sub, messages typés).
- `services/knowledge.py` (recettes/ratios — données pures, testables seules).
- `agents/base.py` (BaseAgent).
- `agents/coordinator.py` (objectifs en dur, agent factice).
- Tests : 2 agents factices qui publient/souscrivent via le messenger, le
  coordinator alloue une zone. Headless (aucun joueur requis) + physique.

## 8. Capacités du socle déjà disponibles (réutilisées, pas réécrites)

`fl_tools` : get_state, get_tick, scan_area, scan_factory, find_nearest,
describe, get_recipe, production_stats.
`fl_ops` : walk_to, walk_to_entity, mine_entity, place_entity_at, move_items,
move_items_at, wait, craft_item, research_technology, set_test_mode, setup,
status, cancel. + `completion_seq` (attente race-free).

Validé en production 25/25 (joueur connecté, physique réelle).

## 9. Ce qui reste déterministe (PAS un agent LLM)

- **Exploration / cartographie** : `scan_grid()` dans `services/perception.py`
  (spirale autour du spawn).
- **Géométrie de placement** : `services/geometer.py` + `utils_entity.lua`.
- **Ratios / recettes / prérequis tech** : `services/knowledge.py` (données).
- **Pose mécanique de machines/bandes** : `services/exec/*` (exécutent une
  instruction, ne décident pas).
- **Routage de bandes transporteuses** : skill géométrique (logistics.py l'utilise ; le
  routage pur est déterministe).