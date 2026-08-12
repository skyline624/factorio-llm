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

C'est LE lanceur d'une partie : avant lui, la commande docker se tapait à la main à
chaque manche — trente-six fois, sans trace de ce qui avait été lancé. 
ne fait pas la même chose : il pilote le Coordinator déterministe, pas l'agent.

Usage :
    cd python
    python jouer.py                 # part du prompt de scripts/.prompt_courant.txt
    python jouer.py --minutes 90
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
# UNE PARTIE AUTONOME MESURE DEUX CHOSES À LA FOIS : la qualité des outils et celle de
# l'arbitrage. Quand une chaîne s'arrête, on ne sait pas si l'outil a menti ou si l'agent a
# mal choisi — les parties 39 à 42 ont fait remonter onze défauts d'outils, chacun coûtant
# une demi-heure à isoler au milieu de ses décisions. En mode piloté, l'humain donne les
# étapes une par une : ce qui échoue est un OUTIL. C'est un banc, pas une partie.
PROMPT_PILOTE = os.path.join(RACINE, "scripts", "prompt_pilote.md")
JOURNAL = "hermes_partie.log"

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
    ap.add_argument("--pilote", action="store_true",
                    help="le JOUEUR donne les étapes une par une dans le chat ; "
                         "l'agent n'entreprend rien de lui-même")
    opt = ap.parse_args()

    # Le mode piloté ne change QUE le prompt de départ : attente de l'avatar, pont des
    # messages et reprise de session restent identiques. Un banc qui change deux choses à
    # la fois ne mesure rien — ce qui est précisément le défaut qu'il corrige.
    chemin = PROMPT_PILOTE if opt.pilote else PROMPT
    if not os.path.exists(chemin):
        print(f"prompt introuvable : {chemin}")
        return 1
    mission = open(chemin, encoding="utf-8").read().strip()
    if opt.pilote:
        # Le fichier est un document : on n'envoie que ce qui suit la ligne de séparation,
        # le reste explique POURQUOI ce mode existe et ne s'adresse pas à l'agent.
        if "\n---\n" in mission:
            mission = mission.split("\n---\n", 1)[1].strip()
        print("[pont] mode PILOTÉ — il attend tes consignes dans le chat du jeu")

    api = ModApi(get_rcon())
    open(opt.journal, "w", encoding="utf-8").close()

    # PAS DE MAINS, PAS DE PARTIE. Partie 39 : l'agent part sur une carte neuve, se heurte
    # trois fois à « aucun avatar connecté », le dit au joueur, et rend la main au bout de
    # douze secondes. Le pont applique alors sa règle de fin — qui est juste — sur une
    # prémisse qui ne l'était pas : il n'avait pas fini, il n'avait jamais commencé.
    if not pont_chat.avatar_present(api):
        print("[pont] aucun avatar connecté — j'attends que tu rejoignes la partie...")
        if not pont_chat.attendre_l_avatar(api, dort=time.sleep):
            print("[pont] toujours personne — connecte un client de jeu, puis relance.")
            return 1
    print("[pont] avatar connecté")

    print("[pont] lancement de la partie")
    proc = _lancer(["docker", "compose", "-f", COMPOSE, "run", "--rm",
                    "--entrypoint", "hermes", "hermes", "chat",
                    "-s", "factorio", "-q", mission], opt.journal)

    # PAS DE MINUTERIE QUAND C'EST L'HUMAIN QUI MÈNE. Le budget borne une partie qui joue
    # SEULE ; en mode piloté il s'écoule pendant que le joueur réfléchit entre deux
    # consignes, et c'est lui qui décide quand on s'arrête — par Ctrl-C ou en fermant.
    #
    # Il ne faisait d'ailleurs pas ce qu'il annonçait : partie 40, « budget écoulé — arrêt
    # de l'agent », et le conteneur a continué de jouer vingt minutes. `proc.terminate()`
    # tue `docker compose run`, pas le conteneur qu'il a lancé.
    fin = float("inf") if opt.pilote else time.time() + opt.minutes * 60
    session = None
    attente: list[str] = []

    while time.time() < fin:
        time.sleep(PAUSE_S)

        # ON NE VOLE PAS LE MESSAGE AU BANDEAU. Ce commentaire disait l'inverse — « on vide
        # la file MÊME quand l'agent travaille : le bandeau des `tool_result` la lit de son
        # côté ». Les deux lisent la MÊME file, et `read_messages` la vide : un seul peut
        # l'obtenir. Partie 41, mesuré — le joueur écrit pendant qu'Hermes travaille, le
        # pont capte, le bandeau ne trouve rien, et le stock du pont n'est livré qu'à
        # l'arrêt de l'agent. Le message était perdu pour toute la partie.
        #
        # Tant qu'il travaille, on REGARDE : le bandeau livre, c'est la voie la plus rapide
        # et la seule qui marche en cours de chantier. On ne prend la file qu'au moment de
        # relancer pour de bon.
        if proc.poll() is None:
            if pont_chat.messages_en_attente(api, consommer=False):
                print("[pont] le joueur parle — le bandeau le livre à l'agent")
            continue          # il travaille : on ne double jamais l'agent

        neufs = pont_chat.messages_en_attente(api)
        if neufs:
            attente.extend(neufs)
            print(f"[pont] {len(neufs)} message(s) du joueur en attente")

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
