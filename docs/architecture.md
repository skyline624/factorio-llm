# Architecture de factorio-llm

Pilote Factorio autonome **multi-agent**. Un orchestrateur Python (une serie
d'agents specialises) pilote le jeu via un mod Lua expose en RCON. Access RCON
**commun et verrouille** (singleton thread-safe).

## Philosophie

Reprise des deux references etudiees :
- **ai-player-v3** (Python + Lua) : LLM garde "stupide" (il choisit un skill +
  params), tout le determinisme mecanique (pathfinding, placement, geometrie)
  cote Lua. Perception 100% symbolique (pas de capture d'ecran). Boucle fermee
  par `last_action_results`.
- **airi-factorio** (TS + Lua) : separation nette `autorio_operations` (action
  **asynchrone** via task_manager) / `autorio_tools` (observation **synchrone**),
  communication RCON natif + signalement de fin sur stdout.

Notre projet combine les deux, en **multi-agent** :
- Mod Lua avec 2 interfaces RCON : `fl_tools` (observation sync) et `fl_ops`
  (action async via task_manager).
- Orchestrateur Python multi-agent, agents specialises par domaine, chaque
  agent etant un module avec acces RCON commun verrouille.
- Mode **pull** (les agents interrogent l'etat et envoient les actions via
  RCON), pas **push** (le mod n'ecrit pas de fichiers de requete).
- LLM OpenAI-compatible uniquement.

## Layout

```
factorio_llm/
  mod/                      mod Factorio (Lua)
    info.json, settings.lua, control.lua
    scripts/
      json.lua               serializeur JSON maison
      perception.lua         fl_tools : get_state, get_tick
      operations.lua         fl_ops : walk_to, teleport_to, status, cancel, spawn
      task_manager.lua       file de taches executee sur on_tick
      character.lua          cycle de vie du personnage IA headless
  python/                   orchestrateur multi-agent
    config.py, .env.example, requirements.txt, main.py
    core/
      rcon.py               client RCON natif thread-safe (singleton + verrou)
      mod_api.py            wrapper typé des interfaces remote
      state.py              GameState dataclass (snapshot partage)
    agents/                 (a venir) base.py, coordinator.py, explorer, ...
  scripts/                  lancement Factorio
    start_factorio_dedicated.bat   serveur headless + RCON (principal)
    start_factorio_client.bat       client Steam solo + RCON (alternative)
    server-settings.json            auto_pause:false (requis pour headless)
  docs/                     architecture.md, protocol.md
  README.md
```

## Boucle de perception/decision/action (mode pull)

```
Agent (Python)
  |  1. perceive : api.get_state()  -> RCON /silent-command remote.call("fl_tools","get_state")
  |     le mod assemble l'etat JSON et rcon.print -> le client RCON lit la string
  |  2. decide   : LLM (ou skill deterministe) -> choisit une action
  |  3. act      : api.walk_to(x,y) -> RCON /silent-command remote.call("fl_ops","walk_to",x,y)
  |     le mod enfile une tache (task_manager) ; retour immediat {ok,detail}
  |  4. observe  : api.wait_until_idle() -> sonde fl_ops.status jusqu'a state=idle
  v
factorio mod (on_tick) : task_manager.tick() execute la tache courante
```

## Agents prevus (modules, a construire un par un)

- `base.py` : BaseAgent — boucle perceive/decide/act, RCON partage, contrat
  (objectif, zone, contraintes) fourni par le coordinateur.
- `coordinator.py` : coordinateur leger — fixe des objectifs/contrats par
  agent (zone, item cible), arbitre les conflits (positions, ressources
  partagees), diffuse un snapshot d'etat commun. N'execute pas d'actions.
- `explorer` : cartographie, scan ressources, expansion.
- `logistician` : ceintures, coffres, inserters, flux d'items.
- `producer` : mines, fours, assemblages, chaines de production.
- `researcher` : labs, sciences, technologies.
- `electrician` : energie steam + reseau electrique.
- `defender` : murs, tourelles (phase 2).

## Coordination

Agents autonomes + coordinateur leger. Chaque agent decide seul a son rythme
via le RCON commun (verrou dans `core/rcon.py`). Le coordinateur :
- alloue des **zones** / **items cibles** pour eviter que deux agents ne
  construisent au meme endroit ou ne se disputent le meme patch ;
- diffuse un **snapshot d'etat** unique (un `get_state` partagé) pour limiter
  les appels RCON redondants ;
- arbitre les conflits remontes par les agents (ressource deja prise, etc.).

## Etat courant (socle)

Le socle valide la boucle RCON complete avec le minimum :
- mod : 1 tool (`get_state`), 2 operations (`walk_to`, `teleport_to`) + status.
- python : `RconClient` natif thread-safe, `ModApi` typé, `GameState`, demo
  `main.py` (connect -> get_state -> walk -> wait_until_idle -> get_state).

Prochaine etape : `agents/base.py` + `coordinator.py` + 1 agent concret (ex.
explorer) pour valider la boucle LLM+action sur un domaine reel.