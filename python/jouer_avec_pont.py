"""Lance une partie et tient le pont : le chat du jeu devient un tour de conversation.

CE QUE CE SCRIPT REMPLACE. Jusqu'ici la partie se lançait par un `hermes chat -q "<mission>"`
unique : un seul tour utilisateur, puis l'agent bouclait seul jusqu'à produire une réponse
sans appel d'outil — et la session se fermait. Deux conséquences, toutes deux mesurées :

  - il s'ARRÊTAIT en croyant avoir fini (« l'usine est autonome, prochaine étape quand tu
    veux ») alors que personne n'attendait de l'autre côté ;
  - les messages du joueur n'arrivaient que collés dans un `tool_result`, sans le statut
    d'une instruction : 19 réponses sur 22, mais l'ACTION ne suivait que dans 29 % des cas
    quand un chantier tournait.

Le pont règle les deux d'un coup. Quand l'agent rend la main et que le joueur a parlé, on
relance la MÊME session (`--resume`) avec son message comme tour `user`. La conversation
reprend là où elle s'était arrêtée, avec toute sa mémoire de la partie.

CE QU'IL NE FAIT PAS : parler pendant que l'agent travaille. Deux sessions actives, ce sont
deux agents sur un seul avatar — le 09/08, cinquante-cinq minutes de jeu illisibles. Tant
qu'il tourne, les messages attendent et le bandeau des `tool_result` continue de les
livrer ; c'est moins fort, mais ce n'est jamais dangereux.

Usage :
    cd python
    python jouer_avec_pont.py                 # part du prompt de scripts/.prompt_courant.txt
    python jouer_avec_pont.py --minutes 90
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time

from core.mod_api import ModApi
from core.rcon import get_rcon
from services import pont_chat

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COMPOSE = os.path.join(RACINE, "docker-compose.hermes.yml")
PROMPT = os.path.join(RACINE, "scripts", ".prompt_courant.txt")
JOURNAL = os.path.join("hermes_partie_pont.log")

# Entre deux coups d'œil à la file de messages. Assez court pour que le joueur ne se sente
# pas ignoré, assez long pour ne pas marteler le RCON pendant que l'agent joue.
PAUSE_S = 4.0


def _agent_tourne() -> bool:
    """Un conteneur Hermes est-il en vie ? On ne devine pas cet état, on le LIT."""
    try:
        r = subprocess.run(
            ["docker", "ps", "-q", "--filter",
             "ancestor=nousresearch/hermes-agent:latest"],
            capture_output=True, text=True, timeout=20)
        return bool((r.stdout or "").strip())
    except Exception:
        return True          # dans le doute on s'abstient : ne jamais doubler l'agent


def _lancer(args: list[str], journal: str) -> subprocess.Popen:
    f = open(journal, "a", encoding="utf-8", errors="replace")
    return subprocess.Popen(args, stdout=f, stderr=subprocess.STDOUT, cwd=RACINE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=120.0)
    ap.add_argument("--journal", default=JOURNAL)
    opt = ap.parse_args()

    if not os.path.exists(PROMPT):
        print(f"prompt introuvable : {PROMPT}")
        return 1
    mission = open(PROMPT, encoding="utf-8").read().strip()

    api = ModApi(get_rcon())
    open(opt.journal, "w", encoding="utf-8").close()

    print("[pont] lancement de la partie")
    proc = _lancer(["docker", "compose", "-f", COMPOSE, "run", "--rm",
                    "--entrypoint", "hermes", "hermes", "chat",
                    "-s", "factorio", "-q", mission], opt.journal)

    fin = time.time() + opt.minutes * 60
    session = None
    attente: list[str] = []

    while time.time() < fin:
        time.sleep(PAUSE_S)

        # Ce que le joueur a tapé depuis le dernier tour. On vide la file MÊME quand
        # l'agent travaille : le bandeau des `tool_result` la lit de son côté, et laisser
        # les messages s'empiler ferait arriver dix conseils périmés d'un coup.
        neufs = pont_chat.messages_en_attente(api)
        if neufs:
            attente.extend(neufs)
            print(f"[pont] {len(neufs)} message(s) du joueur en attente")

        if proc.poll() is None:
            continue          # il travaille : on ne double jamais l'agent

        # Il a rendu la main. Sans message, la partie est finie — c'est son choix, on ne
        # le relance pas pour le plaisir de le faire tourner.
        try:
            texte = open(opt.journal, encoding="utf-8", errors="replace").read()
        except OSError:
            texte = ""
        session = pont_chat.session_du_journal(texte) or session

        if not pont_chat.doit_relancer(_agent_tourne(), attente):
            if not attente:
                print("[pont] l'agent s'est arrêté et personne ne lui parle — fin")
                break
            continue

        if session is None:
            print("[pont] session introuvable dans le journal — impossible de reprendre")
            break

        message = pont_chat.composer(attente)
        attente.clear()
        print(f"[pont] reprise de {session} avec le message du joueur")
        proc = _lancer(pont_chat.commande_relance(session, message, COMPOSE), opt.journal)

    if proc.poll() is None:
        print("[pont] budget écoulé — arrêt de l'agent")
        proc.terminate()
    print(f"[pont] terminé. Journal : {opt.journal}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
