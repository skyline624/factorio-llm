# factorio-llm

Pilote Factorio autonome **multi-agent**. Un orchestrateur Python (une serie
d'agents specialises) pilote le jeu via un mod Lua expose en RCON. Acces RCON
commun verrouille. Perception 100 % symbolique (etat de jeu via RCON, pas de
capture d'ecran). LLM OpenAI-compatible.

Inspire de `ai-player-v3` (Python+Lua, 3 tiers, LLM stupide) et `airi-factorio`
(TS+Lua, operations async / tools sync).

## Etat du projet

Socle RCON valide en conception :
- **mod/** : mod Lua minimal — 2 interfaces RCON `fl_tools` (observation) et
  `fl_ops` (action async via task_manager), personnage IA headless.
- **python/** : client RCON natif thread-safe, `ModApi` typé, `GameState`,
  demo `main.py`.
- Prochaine etape : `agents/base.py` + `coordinator.py` + 1 agent metier.

Voir `docs/architecture.md` et `docs/protocol.md`.

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