# factorio-llm

Pilote Factorio autonome **multi-agent**. Un orchestrateur Python (une serie
d'agents specialises) pilote le jeu via un mod Lua expose en RCON. Acces RCON
commun verrouille. Perception 100 % symbolique (etat de jeu via RCON, pas de
capture d'ecran). LLM OpenAI-compatible.

Inspire de `ai-player-v3` (Python+Lua, 3 tiers, LLM stupide) et `airi-factorio`
(TS+Lua, operations async / tools sync).

## Etat du projet

L'agent part d'une carte vierge, batit sa centrale et sa chaine de production,
repare ce qui tombe en panne, etend son usine sous objectif de debit, et TIENT cet
objectif : mesure sur 30 minutes de jeu, 35 relevés de debit sur 61 au-dessus de
2.0 iron-plate/s (pointe 3.12). 286 tests unitaires, une trentaine de scripts de
verification en jeu.

- **mod/** : mod Lua — interfaces RCON `fl_tools` (observation) et `fl_ops`
  (action async via task_manager), personnage IA headless.
- **python/core** : client RCON thread-safe, `ModApi` typé, `GameState`.
- **python/services** : planners deterministes (production_solver, layout_planner,
  micro_planner, power_planner), `executor`, `factory_doctor` (diagnostic),
  `site_finder`, `flux`, `gisements`, `arbitre` (LLM), `journal`, `save_ref`.
- **python/agents** : `coordinator` (observe / diagnostique / decide / agit /
  verifie), `factory_builder`, `enqueteur` (LLM outille).

**Le LLM ne genere rien** : le determinisme enumere les options legales, le modele
en DESIGNE une par son indice (`services/arbitre.py`), et l'Enqueteur choisit quoi
MESURER dans une liste blanche d'outils de lecture. Toute defaillance du modele
retombe sur la decision deterministe.

### Mesurer une partie

```
cd python
python preparer_reference.py [--rayon N] [--nids N]   # fige un etat de depart
python run_partie_longue.py 30 --vitesse 10 --ombre --depuis-reference --objectif 2.0
```

`--depuis-reference` restaure la save figee : sans etat de depart identique, deux
parties ne se comparent pas. Le monde est fige pendant que le modele reflechit,
faute de quoi on compare un agent lent a un agent rapide et non deux strategies.

Voir `docs/architecture.md`, `docs/agents-architecture.md` et `docs/protocol.md`.

## Demarrage rapide (socle)

1. **Lancer Factorio avec RCON** (serveur dedie headless, recommande) :
   ```
   scripts\start_factorio_dedicated.bat
   ```
   Cree une junction `mods/factorio-llm -> mod`, genere une save fresh, demarre
   le serveur sur RCON `127.0.0.1:27015` (mdp `factoriollm`).
   `auto_pause:false` est impose via `scripts/server-settings.json` (indispensable
   pour que le monde tourne sans joueur connecte).

   Alternative observable en solo : `scripts\start_factorio_client.bat` (client
   Steam + RCON, mod installe par junction dans `%APPDATA%\Factorio\mods`).
   Pense a activer le mod dans Mods avant de charger une save.

2. **Installer le socle Python** :
   ```
   cd python
   pip install -r requirements.txt
   copy .env.example .env   (adapter RCON_HOST/PORT/PASSWORD si besoin)
   ```

3. **Lancer la demo** (valide la boucle RCON complete : get_state -> walk ->
   wait_until_idle -> get_state -> teleport) :
   ```
   python main.py
   ```

## Configuration

`python/.env` (voir `.env.example`) :
- `RCON_HOST` / `RCON_PORT` / `RCON_PASSWORD` (doivent correspondre au lancement
  Factorio).
- `OPENAI_API_KEY` / `OPENAI_BASE_URL` / `OPENAI_MODEL` (LLM, utilise plus tard par
  les agents ; ex Ollama : `http://localhost:11434/v1`, modele `qwen2.5`).

## Structure

```
mod/         mod Factorio (Lua) : fl_tools, fl_ops, task_manager, character
python/      orchestrateur : core/rcon.py, core/mod_api.py, core/state.py, main.py
scripts/     lancement Factorio (dedie / client) + server-settings.json
docs/        architecture.md, protocol.md
```

## Principe

Mode **pull** : les agents Python interrogent l'etat via RCON (`fl_tools`) et
envoient les actions via RCON (`fl_ops`). Le mod garde l'execution asynchrone
(task_manager sur `on_tick`) pour les actions longues (marche, minage). Le LLM
reste "stupide" : il choisit des operations/params ; tout le determinisme
mecanique reste cote Lua.