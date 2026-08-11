"""Le pont : ce que le joueur tape dans le jeu devient un tour de conversation.

POURQUOI CE MODULE EXISTE. Jusqu'ici, un message du chat Factorio arrivait collé en tête
d'un `tool_result` — la valeur de retour d'un outil. Pour le modèle, c'est une observation
au même rang que « 12 machines » ou « no_fuel » : rien ne dit qu'un humain parle, et rien
ne lui donne l'autorité d'une instruction.

Mesuré sur six parties, vingt-deux messages :

    réponse à un message   : 19/22, médiane 7 s
    ACTION qui suit        : 29 % quand un chantier tourne, 80 % quand l'avatar est libre

Il LIT, il répond volontiers, et il poursuit son plan. Ce n'est pas de l'entêtement :
l'information n'a simplement pas le statut d'un ordre. Le prompt de mission, lui, arrive
comme un tour `user` — et il est suivi scrupuleusement depuis trente-six parties.

Le pont donne donc aux messages du jeu le même statut, par `hermes chat --resume <session>
-q "<message>"`. Vérifié en conditions réelles avant d'écrire ces lignes : le contexte
Factorio est conservé (« arrête le chantier » déclenche la recherche de
`arreter_le_chantier`), l'agent AGIT, et deux processus concurrents n'abîment pas la base
— SQLite est en WAL, quatre tours écrits en parallèle, aucun perdu.

CE QUE LE PONT NE FAIT PAS : parler pendant que l'agent travaille. Deux sessions actives,
ce sont deux agents sur un seul avatar — le désastre du 09/08, cinquante-cinq minutes de
jeu illisibles. Tant que l'agent tourne, les messages attendent et le bandeau des
`tool_result` continue de les livrer ; dès qu'il rend la main, ils deviennent un vrai tour.
"""

from __future__ import annotations

from typing import Iterable, Optional


def doit_relancer(agent_tourne: bool, messages: Iterable[str]) -> bool:
    """Faut-il relancer la session pour livrer ces messages ?

    Deux conditions, et la première n'est pas négociable : l'agent doit avoir rendu la
    main. Relancer pendant qu'il travaille ne casse pas la base — c'est mesuré — mais met
    deux agents sur un seul personnage, qui s'enverront des ordres contradictoires.

    On ne devine pas cet état : le conteneur existe ou n'existe pas.
    """
    return bool(not agent_tourne and list(messages))


def composer(messages: Iterable[str]) -> str:
    """Le tour `user` à envoyer, à partir des messages en attente.

    UN ORDRE VENU DE NULLE PART SE DEVINE MAL. L'agent reprend une conversation vieille de
    plusieurs minutes, dont le dernier tour était son propre rapport de fin. « Arrête le
    chantier », seul, l'oblige à reconstituer qui parle et d'où.

    On donne donc la provenance et ce qu'elle implique — l'humain REGARDE L'ÉCRAN, il voit
    ce que les compteurs ne montrent pas — sans rien dire de la conduite à tenir. Trois
    fois cette semaine il a refusé un conseil en argumentant, et deux fois il avait raison :
    ce qu'il fait du message reste son arbitrage.
    """
    dits = [str(m).strip() for m in messages if str(m).strip()]
    if not dits:
        return ""
    corps = "\n".join(f"  « {d} »" for d in dits)
    return ("Message du joueur, tapé dans le chat du jeu pendant que tu jouais :\n"
            f"{corps}\n\n"
            "Il regarde l'écran ; toi tu lis des compteurs. Ce qu'il décrit, il le voit — "
            "un four vide, une machine mal placée, du minerai par terre — et c'est "
            "vérifiable en quelques secondes avec `regarder` ou `etat_du_jeu`. Il peut se "
            "tromper ou parler d'un état déjà changé : dans ce cas dis-le et mesure. "
            "Rappel utile : `arreter_le_chantier` ne détruit rien — ce qui est posé reste "
            "posé, et relancer reprend où l'on s'était arrêté.")


def messages_en_attente(api) -> list[str]:
    """Vide la file du mod et rend ce que le joueur a tapé.

    La lecture CONSOMME, par conception : un conseil relivré à chaque tour deviendrait un
    bruit de fond que l'agent apprendrait à sauter.
    """
    try:
        lus = (api.read_messages() or {}).get("messages") or []
    except Exception:
        return []
    return [f"{m.get('joueur', '?')} : {m.get('texte', '')}" for m in lus
            if str(m.get("texte", "")).strip()]


def avatar_present(api) -> bool:
    """Un personnage est-il connecté au serveur ? On le LIT, on ne le suppose pas.

    Le mod rend `character` dans `get_state` dès qu'un client est là. Une lecture qui
    échoue ne prouve rien — mais elle ne prouve pas non plus qu'il y a quelqu'un, et c'est
    l'absence de preuve qui compte au moment de dépenser une session.
    """
    try:
        return bool((api.get_state() or {}).get("character"))
    except Exception:
        return False


def attendre_l_avatar(api, dort, essais: int = 60, pause_s: float = 5.0) -> bool:
    """Attend qu'un joueur soit là. Rend False si personne ne vient — sans rien lancer.

    DOUZE SECONDES POUR BRÛLER UNE PARTIE. Partie 39 : carte neuve, serveur MCP relancé,
    l'agent part et se heurte trois fois à « aucun avatar connecté », répond au joueur
    qu'il n'a pas de mains, puis rend la main. Le pont lit alors sa règle de fin — un agent
    qui s'arrête sans qu'on lui parle a FINI — et clôt la partie. La règle est juste ; la
    prémisse ne l'était pas. Il n'avait pas fini, il n'avait jamais commencé.

    Le prérequis se vérifie donc AVANT de dépenser la session. Et l'attente est bornée :
    lancer quand même passé le délai refabriquerait le défaut qu'on corrige.
    """
    for _ in range(max(1, int(essais))):
        if avatar_present(api):
            return True
        dort(pause_s)
    return avatar_present(api)


def commande_relance(session: str, message: str, compose_file: str,
                     skill: str = "factorio") -> list[str]:
    """La commande docker qui ajoute ce tour à la session existante.

    `--resume` et non un nouveau `chat` : c'est ce qui préserve la mémoire de la partie.
    Sans lui, l'agent redécouvrirait une usine qu'il a bâtie lui-même.
    """
    return ["docker", "compose", "-f", compose_file, "run", "--rm",
            "--entrypoint", "hermes", "hermes", "chat",
            "-s", skill, "--resume", session, "-q", message]


def session_du_journal(texte: str) -> Optional[str]:
    """L'identifiant de session qu'Hermes imprime en fin d'exécution.

    Il le donne lui-même (« Resume this session with: hermes --resume <id> ») ; le lire
    évite de le reconstruire à partir d'une horloge, qui dériverait.
    """
    import re
    trouves = re.findall(r"--resume\s+(\d{8}_\d{6}_[0-9a-f]+)", texte or "")
    return trouves[-1] if trouves else None
