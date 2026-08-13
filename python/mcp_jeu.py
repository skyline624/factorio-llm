"""Les mains du joueur — les services du dépôt exposés en outils MCP.

Hermes Agent est le JOUEUR ; ce module est ce qu'il manipule. Il tourne sur l'HÔTE, pas
dans le conteneur : il lui faut le Python du projet, le client RCON et un accès au serveur
Factorio. L'embarquer dans l'image rendrait celle-ci non jetable, ce qu'on veut
précisément éviter — le dépôt porte tout, le conteneur n'est qu'un moteur.

LA FRONTIÈRE, ET C'EST TOUT L'ENJEU. Un modèle de langage est mauvais en placement
spatial ; c'est la raison d'être des services déterministes de ce dépôt. On expose donc
leur CALCUL, jamais la pose entité par entité :

    exposé      « bâtis une chaîne de fer ici », « diagnostique cette zone »
    JAMAIS      place_entity_at, remove_entity_at, rotate_entity_at

Ce que ces services portent et qu'aucun modèle ne redécouvrira seul — chaque ligne a coûté
des heures de mesure en jeu :

  - le drop d'une foreuse est décalé d'une demi-tuile HORS de son axe ;
  - foreuse et four font 2×2, pas 3×3 ;
  - un inserteur PREND du côté où il pointe et dépose à l'opposé ;
  - deux belts parallèles adjacentes ne transmettent rien ;
  - `place_entity_at` est asynchrone : croire son `ok=True` a produit 26 poses fantômes ;
  - le charbon ne se fond pas ; une machine burner ne se câble pas ;
  - vider ou remplir une machine exige d'être à moins de dix tuiles.

Lancement (sur l'hôte, serveur Factorio démarré) :
    cd python
    python mcp_jeu.py                 # http://127.0.0.1:8765/mcp
"""

from __future__ import annotations

import os
import threading
from typing import Any, Optional

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from services.outils_llm import tronquer

# `0.0.0.0` et non `127.0.0.1` : le conteneur Hermes atteint l'hôte par
# `host.docker.internal`, ce qui n'est PAS la loopback. Écouter seulement sur 127.0.0.1
# rend le serveur invisible depuis le conteneur — il répond parfaitement aux essais
# locaux et Hermes ne voit aucun outil.
#
# Conséquence assumée : le port est ouvert sur le réseau local. Ce serveur pilote un jeu,
# sans secret ni accès disque arbitraire, et la machine de développement n'est pas
# exposée — mais si elle l'était, il faudrait le refermer par `FL_MCP_HOST=127.0.0.1` et
# passer par un réseau Docker dédié.
HOTE = os.environ.get("FL_MCP_HOST", "0.0.0.0")
PORT = int(os.environ.get("FL_MCP_PORT", "8765"))
RCON_HOTE = os.environ.get("FL_RCON_HOST", "127.0.0.1")
RCON_PORT = int(os.environ.get("FL_RCON_PORT", "27015"))
RCON_MDP = os.environ.get("FL_RCON_PASSWORD", "factoriollm")

# LE CONTENEUR N'ARRIVE PAS PAR LA LOOPBACK. Le SDK refuse par défaut tout en-tête `Host`
# inconnu — une protection contre le DNS rebinding, saine dans un navigateur. Depuis
# Docker, l'appel arrive avec `Host: host.docker.internal` et se voit répondre
# « HTTP 421 Misdirected Request » : la connexion aboutit, le serveur la rejette, et
# Hermes ne voit simplement aucun outil sans qu'aucune erreur ne l'explique.
#
# On autorise donc explicitement les hôtes par lesquels on sait qu'on sera appelé, plutôt
# que de désactiver la protection.
_SECURITE = TransportSecuritySettings(
    allowed_hosts=[f"host.docker.internal:{PORT}", f"127.0.0.1:{PORT}",
                   f"localhost:{PORT}", f"host.docker.internal", "127.0.0.1",
                   "localhost"],
    allowed_origins=["*"],
)

mcp = FastMCP(
    "factorio",
    transport_security=_SECURITE,
    instructions=(
        "Les mains d'un joueur de Factorio. Tu demandes des RÉSULTATS — « bâtis une "
        "chaîne de fer », « diagnostique cette zone » — jamais des positions : le "
        "placement est calculé par des services déterministes qui connaissent la "
        "géométrie exacte du jeu. Observe avant d'agir, et relis l'état après : une "
        "action qui réussit sans rien changer est un échec."),
)

# --------------------------------------------------------------------- socle partagé

_ETAT: dict[str, Any] = {"api": None, "coord": None}


def _api():
    """Le lien vers le jeu, ouvert à la première demande et gardé ensuite.

    LE LIEN SURVIT MAL AUX REDÉMARRAGES. Restaurer une carte relance le serveur Factorio,
    et le singleton RCON garde alors une connexion morte : tous les outils répondent
    « connexion refusée » jusqu'à ce qu'on relance le serveur MCP à la main. Comme une
    partie commence justement par une carte neuve, le cas est la règle, pas l'exception.
    On sonde donc le lien et on le rouvre s'il est tombé — une seule fois, sans boucler.
    """
    from core.mod_api import ModApi
    from core.rcon import get_rcon

    def _neuf():
        return ModApi(get_rcon(RCON_HOTE, RCON_PORT, RCON_MDP))

    if _ETAT["api"] is None:
        _ETAT["api"] = _neuf()
        return _ETAT["api"]
    try:
        _ETAT["api"].rcon.query_lua("rcon.print(1)")
    except Exception:
        # Le serveur a redémarré sous nos pieds : on repart d'un lien neuf, et le
        # Coordinator qui tenait l'ancien doit être reconstruit avec lui.
        _ETAT["api"], _ETAT["coord"] = _neuf(), None
    return _ETAT["api"]


def _coord():
    """Le Coordinator comme BOÎTE À OUTILS, non comme pilote.

    Ses méthodes portent la géométrie et les enchaînements éprouvés ; c'est Hermes qui
    décide quand les appeler. Sa zone suit le joueur au moment où on le construit.
    """
    from agents.coordinator import Coordinator
    from services import deplacement
    if _ETAT["coord"] is None:
        api = _api()
        _ETAT["coord"] = Coordinator(api, zone=deplacement.position(api), rayon=25.0)
        _brancher_l_arret(_ETAT["coord"])
    return _ETAT["coord"]


def _rendu(valeur: Any) -> str:
    """Réponse réduite à ce qui se raisonne — `inspect_at` peut rendre 200 entités."""
    return tronquer(valeur, entites_max=20, caracteres=2000)


# CE QUE L'AGENT APPELLE, ET DANS QUEL ORDRE. Sans ce journal on observe à l'aveugle :
# le serveur ne trace que des `POST /mcp` anonymes, et il a fallu deviner, deux parties
# durant, si Hermes demandait vraiment de bâtir ou s'il minait encore à la main. Une
# partie ne se lit pas dans l'état final — elle se lit dans la suite des gestes.
JOURNAL = os.environ.get("FL_MCP_JOURNAL", "mcp_appels.log")


def _tracer(ligne: str) -> None:
    """Écrit une ligne de journal, sur la sortie et sur le disque."""
    # `errors="replace"` : la console Windows ecrit en cp1252 et un seul caractere
    # hors table ferait echouer l'outil lui-meme. Un journal ne doit jamais etre plus
    # fragile que ce qu'il observe.
    try:
        print(f"[appel] {ligne}", flush=True)
    except UnicodeEncodeError:
        print("[appel] " + ligne.encode("ascii", "replace").decode("ascii"), flush=True)
    try:
        with open(JOURNAL, "a", encoding="utf-8") as f:
            f.write(ligne + "\n")
    except OSError:
        pass          # un journal qui échoue ne doit jamais arrêter une partie


# Ce que l'agent fait EN CE MOMENT : (nom de l'outil, instant de départ). Le veilleur
# s'en sert pour dire au joueur pourquoi personne ne lui répond.
_EN_COURS = {}
_DERNIER_ACCUSE = None
# Numéro du dernier chantier dont la fin a déjà été annoncée (cf. _bandeau_du_joueur).
_FIN_ANNONCEE = None


def _accuser_reception(outil_en_cours, depuis_s):
    """Dit au joueur, DANS LE JEU, que son message est arrivé et pourquoi ça ne répond pas.

    PENDANT QU'IL ATTEND UN OUTIL, L'AGENT N'EXISTE PAS : aucun tour de modèle ne tourne
    entre l'appel et son retour, il ne peut ni lire ni écrire. Mesuré le 10/08 —
    `batir_une_chaine` lancé à 14:56:05, trois messages envoyés entre 14:54 et 14:58,
    aucune réaction avant la fin de la construction. Le joueur en conclut que ses messages
    se perdent alors qu'ils ne font qu'attendre.

    On ne peut pas faire parler l'agent pendant ce temps. On peut faire parler le SERVEUR,
    qui sait deux choses que le joueur ignore : que le message est bien arrivé, et ce que
    l'agent fait depuis combien de temps. « Silencieux » et « perdu » se ressemblent trop.

    IL NE CONSOMME PAS LA FILE. `read_messages` vide par conception — juste pour l'agent,
    fatal ici : le veilleur détruirait le message avant que l'agent le voie.
    """
    global _DERNIER_ACCUSE
    try:
        api = _api()
        msgs = (api.peek_messages() or {}).get("messages") or []
        if not msgs:
            _DERNIER_ACCUSE = None
            return
        # Un accusé répété s'affiche PAR-DESSUS le jeu du joueur. Le message reste en file
        # jusqu'à ce que l'agent le lise — plusieurs minutes — donc sans cette garde il
        # verrait « bien reçu » deux cents fois.
        cle = tuple((m.get("joueur"), m.get("texte")) for m in msgs)
        if cle == _DERNIER_ACCUSE:
            return
        _DERNIER_ACCUSE = cle

        # PENDANT UN CHANTIER, LE JOUEUR PARLE DANS LE VIDE. Partie 37, mesuré : « arrête
        # de miner, ta foreuse n'a plus rien » à 13:44:41, puis ONZE
        # `ou_en_est_le_chantier` d'affilée sur deux minutes — pas une réponse, pas une
        # action. Sa foreuse était bien épuisée (`no_minable_resources`, zéro minerai
        # dessous) et il ne pouvait rien y faire : l'avatar était pris.
        #
        # RETOURNEMENT ASSUMÉ. En montant les chantiers (H42) j'avais écarté l'arrêt
        # automatique, au motif qu'il déciderait à la place de l'agent. Deux faits l'ont
        # renversé, et aucun n'était connu alors : le message n'a AUCUN poids autrement
        # (29 % d'action mesurés contre 80 % quand l'avatar est libre), et arrêter ne
        # détruit rien — « ce qui est posé reste posé, relance pour reprendre ».
        #
        # L'arbitrage de l'agent reste entier : il relance s'il juge que le joueur se
        # trompait. Ce qu'il perd, c'est seulement l'impossibilité de répondre.
        if _chantier_tourne():
            _demander_l_arret()
            try:
                api.say("j'arrête le chantier pour t'écouter — il reprendra où il en "
                        "était si besoin")
            except Exception:
                pass

        # ON N'ACCUSE RÉCEPTION QUE SI L'ATTENTE EST RÉELLE. Ce mot servait quand un
        # message mettait des minutes à parvenir : il évitait au joueur de parler dans le
        # vide. Depuis H80 (le pont ne vole plus le message au bandeau) et H87
        # (`attendre_le_joueur`), il arrive en une seconde — et l'accusé tombe alors EN
        # MÊME TEMPS que la vraie réponse.
        #
        # Le 12/08 le joueur l'a signalé : « pourquoi il me dit Bien reçu je le lis au
        # prochain geste ? ». Il croyait lire Hermes ; c'était notre serveur, annonçant une
        # attente qui n'existait plus. Un accusé qui double la réponse n'informe pas, il
        # brouille — et il vient de NOUS, ce qui le rend plus trompeur encore.
        #
        # On le garde donc pour le seul cas où il dit quelque chose de vrai : un outil
        # occupe l'avatar depuis assez longtemps pour que le silence soit inquiétant.
        if outil_en_cours and depuis_s >= ACCUSE_SI_OCCUPE_S:
            api.say(f"bien reçu — occupé par {outil_en_cours} depuis "
                    f"{int(depuis_s // 60)} min {int(depuis_s % 60)} s, "
                    f"je le lis dès que j'ai la main")
    except Exception:
        pass


def _veiller():
    """Boucle du veilleur : regarde la file, accuse réception, ne vole rien."""
    import time as _t
    while True:
        _t.sleep(3.0)
        try:
            if _EN_COURS:
                nom, t0 = next(iter(_EN_COURS.items()))
                _accuser_reception(nom, _t.time() - t0)
            else:
                _accuser_reception(None, 0.0)
        except Exception:
            pass


# ----------------------------------------------------------------- chantiers de fond
#
# QUATORZE MINUTES SANS LA MAIN, C'EST QUATORZE MINUTES DE SURDITÉ. Partie 23 :
# `batir_une_chaine` part à 14:56:05 et rend à 15:10:09 ; le joueur écrit trois fois et ne
# voit rien venir. Ce n'est pas qu'un agent occupé ignore les messages — entre l'appel d'un
# outil et son retour, aucun tour de modèle ne tourne. Il n'est pas sourd, il n'est pas là.
#
# Deux remèdes ont été essayés et écartés, pour la même raison : ils décidaient à sa place.
# Faire parler le serveur pendant qu'il travaille, c'est avouer qu'il ne reprendra pas la
# main. Couper le chantier dès qu'un message arrive, c'est arrêter un travail que le
# message ne demandait peut-être pas d'arrêter.
#
# Ce que le joueur a demandé est plus juste : que le travail CONTINUE, que l'agent garde la
# main, et que ce soit LUI qui juge — son message lu — s'il coupe ou s'il laisse finir.
# C'est la même règle que pour les plafonds de fabrication : le choix lui revient.

# Combien de temps `ou_en_est_le_chantier` patiente quand rien ne bouge. Assez long pour
# qu'attendre coûte un tour au lieu de dix, assez court pour ne pas donner l'impression
# d'un outil bloqué. L'attente se coupe de toute façon dès qu'il se passe quelque chose.
ATTENTE_SUIVI_S = 8.0

# Au-delà de quoi le silence devient inquiétant, et un accusé de réception dit quelque
# chose de vrai. En deçà, le message arrive de toute façon avant qu'on ait fini de le lire
# — le bandeau le livre au prochain retour d'outil, une seconde plus tard.
ACCUSE_SI_OCCUPE_S = 15.0

_CHANTIER = {"n": 0, "nom": "", "debut": 0.0, "fil": None,
             "resultat": None, "arret": False}
_VERROU_CHANTIER = threading.Lock()


def _chantier_tourne() -> bool:
    fil = _CHANTIER.get("fil")
    return bool(fil is not None and fil.is_alive())


# (instant de la dernière lecture, motif ou None) — cf. _avatar_absent.
_AVATAR_VU = [0.0, None]


def _avatar_absent():
    """Rend le motif si aucun avatar n'est connecté, `None` si tout va bien.

    UN OUTIL DÉFAILLANT PRODUIT UN APPRENTISSAGE FAUX — et la règle fausse survit. Partie
    23, le client du joueur se déconnecte ; trois outils réagissent de trois façons, dont
    deux trompeuses :

        se_deplacer(80,-80)   -> « arrivé en (0,0) »        ment : il n'a pas bougé
        se_procurer(coal)     -> « nearest coal = None »    masque : ce n'est pas le charbon
        reparer(evacuer)      -> « aucun avatar IA »        seul honnête des trois

    L'agent a fini par conclure juste, mais en croisant trois symptômes dissemblables sur
    vingt minutes — et il en a tiré une règle écrite dans sa skill comme une loi du jeu,
    alors que c'était un incident de montage. Même mécanisme que « deux timeouts crashent
    le jeu » ou « le bois est verrouillé donc pas d'électricité » : une observation juste,
    une règle fausse, et elle lui survit des parties entières.

    On ne répare donc pas trois outils, on supprime la devinette. Un agent qui lit
    « aucun joueur connecté » ne peut en tirer aucune loi sur le jeu.

    DANS LE DOUTE, ON LAISSE PASSER. Ce contrôle tourne avant chaque action, des centaines
    de fois par partie : s'il bloquait quand la lecture d'état échoue, il ferait plus de
    mal que le défaut qu'il corrige. Un garde-fou ne doit jamais être plus fragile que ce
    qu'il protège.
    """
    # UN CONTRÔLE QUI COÛTE UN ALLER-RETOUR RCON EST PIRE QUE LE DÉFAUT. Mesuré au banc :
    # 4,07 s pour lancer un chantier, contre 0,00 s sans lui — sur des centaines d'actions
    # par partie. On garde donc la réponse quelques secondes : un avatar ne se déconnecte
    # pas dix fois par seconde, et le pire cas est de refuser une action de trop juste
    # après une reconnexion.
    import time as _t
    quand, valeur = _AVATAR_VU
    if _t.time() - quand < 5.0:
        return valeur
    try:
        etat = _api().get_state() or {}
    except Exception:
        _AVATAR_VU[:] = [_t.time(), None]
        return None
    if etat.get("character"):
        _AVATAR_VU[:] = [_t.time(), None]
        return None
    motif = ("aucun avatar connecté au serveur de jeu — rien ne peut marcher, miner, "
            "poser ni vider tant qu'un joueur n'est pas là. Les outils de LECTURE "
            "continuent de répondre. C'est un problème de MONTAGE, pas du jeu : dis-le "
            "à l'humain qui te regarde, il peut reconnecter le client.")
    _AVATAR_VU[:] = [_t.time(), motif]
    return motif


def _lancer_chantier(nom: str, travail) -> str:
    """Démarre `travail` en fond et rend la main TOUT DE SUITE.

    UN SEUL AVATAR, DONC UN SEUL CHANTIER. Deux constructions simultanées se disputeraient
    le personnage : chacune le fait marcher ailleurs et les poses tombent où il n'est pas.
    Le cas s'est produit le 09/08 — deux conteneurs sur la même partie, cinquante-cinq
    minutes de jeu illisibles. Le refus dit donc ce qui occupe la place et comment
    reprendre la main : un refus qui n'explique pas se retente en boucle.
    """
    import time as _t
    souci = _avatar_absent()
    if souci:
        return f"refusé : {souci}"
    with _VERROU_CHANTIER:
        if _chantier_tourne():
            depuis = _t.time() - _CHANTIER["debut"]
            return (f"refusé : chantier n°{_CHANTIER['n']} « {_CHANTIER['nom']} » en cours "
                    f"depuis {int(depuis // 60)} min {int(depuis % 60)} s — un seul à la "
                    f"fois (un seul avatar). Suis-le par `ou_en_est_le_chantier`, ou "
                    f"`arreter_le_chantier` si tu veux la place.")
        _CHANTIER.update(n=_CHANTIER["n"] + 1, nom=nom, debut=_t.time(),
                         resultat=None, arret=False)
        n = _CHANTIER["n"]

        def _porter():
            try:
                r = travail()
            except ChantierTue:
                r = ("ARRÊTÉ à la demande — ce qui était posé reste posé, relance pour "
                     "reprendre là où tu t'es arrêté")
            except Exception as e:
                r = f"ERREUR {type(e).__name__}: {e}"
            _CHANTIER["resultat"] = r
            _tracer(f"{_t.strftime('%H:%M:%S')} == chantier n°{n} « {nom} » fini")

        fil = threading.Thread(target=_porter, daemon=True, name=f"chantier-{n}")
        _CHANTIER["fil"] = fil
        fil.start()
    return (f"chantier n°{n} « {nom} » lancé — tu gardes la main. Appelle "
            f"`ou_en_est_le_chantier` pour suivre : c'est là que tu liras ce qu'on te dit.")


def _brancher_l_arret(coord) -> None:
    """Donne au Coordinator de quoi savoir qu'on veut l'arrêter.

    UN BOUTON D'ARRÊT QUI N'ARRÊTE RIEN EST PIRE QUE PAS DE BOUTON. Le drapeau vit ici, la
    pose se déroule trois couches plus bas dans l'`executor` ; sans ce porteur l'agent lit
    « arrêt demandé », attend, et la construction va au bout comme si de rien n'était.

    C'est le défaut typique de ce dépôt : H10 posé sous un drapeau que l'appelant met à
    False, H23 posé dans une méthode que l'appelant ne traverse pas, H27 dans une branche
    jamais prise. Trois correctifs inertes, chacun cru bon pendant une partie entière.
    """
    coord.interrompu_par = _doit_s_arreter
    # ET SUR LE BUILDER, QUI EXÉCUTE LES ÉTAPES. `batir_chaine` se fournit par
    # `builder.act(steps)` : miner, marcher, fondre. Poser le drapeau sur le seul
    # Coordinator laissait donc l'arrêt muet pendant tout l'approvisionnement — sept
    # minutes de minage après un arrêt demandé, partie 29, sous les yeux du joueur.
    # C'est la septième fois qu'un correctif juste ne rencontre pas l'exécution.
    builder = getattr(coord, "builder", None)
    if builder is not None:
        builder.interrompu_par = _doit_s_arreter


class ChantierTue(BaseException):
    """Levée DANS le thread d'un chantier pour le tuer.

    `BaseException` et non `Exception` : les services attrapent volontiers `Exception`
    pour survivre à un aléa du jeu, et avaleraient l'ordre d'arrêt sans le savoir.
    """


def _tuer_le_fil(fil) -> bool:
    """Lève `ChantierTue` dans le thread visé, où qu'il en soit de son travail.

    UN ARRÊT COOPÉRATIF N'EST PAS UN ARRÊT. Sept points de sortie ont été ajoutés
    aujourd'hui — pose, forge, marche, étapes d'approvisionnement — et sept fois le
    chantier a continué, parce que le temps se passait ailleurs. Partie 29 : arrêt demandé
    à 17:45:27, chantier fini à 17:52:47, sept minutes vingt plus tard, pour zéro entité
    posée. On ne cherche donc plus à couvrir tous les chemins : on tue.

    `PyThreadState_SetAsyncExc` est l'outil prévu par CPython pour cela. Il agit à la
    prochaine instruction de bytecode du thread — donc immédiatement dans une boucle
    Python, mais PAS pendant un appel bloquant en C (un `socket.recv` sur le lien RCON).
    D'où le second étage dans `_demander_l_arret`.
    """
    import ctypes
    ident = getattr(fil, "ident", None)
    if ident is None:
        return False
    n = ctypes.pythonapi.PyThreadState_SetAsyncExc(
        ctypes.c_ulong(ident), ctypes.py_object(ChantierTue))
    if n > 1:                      # visé plus d'un thread : on annule, c'est anormal
        ctypes.pythonapi.PyThreadState_SetAsyncExc(ctypes.c_ulong(ident), None)
        return False
    return n == 1


def _debloquer_le_lien() -> None:
    """Ferme le lien RCON pour sortir un chantier bloqué dans un appel réseau.

    SECOND ÉTAGE, et il est indispensable : un chantier passe l'essentiel de son temps à
    attendre le jeu — miner cinquante minerais, marcher cent tuiles. Il est alors dans un
    `recv` bloquant, où aucune exception asynchrone ne l'atteint. Fermer la socket fait
    lever l'appel, et `ChantierTue`, déjà armée, prend le relais à l'instruction suivante.

    Le lien se rouvre tout seul à la demande d'après (`_api` sonde et reconstruit), donc
    l'agent ne perd que l'appel en cours — celui qu'on voulait précisément interrompre.
    """
    try:
        api = _ETAT.get("api")
        if api is not None and getattr(api, "rcon", None) is not None:
            api.rcon.close()
    except Exception:
        pass
    _ETAT["api"], _ETAT["coord"] = None, None


def _demander_l_arret() -> bool:
    """Arrête le chantier POUR DE BON, où qu'il en soit.

    Trois étages, du plus doux au plus ferme. Le drapeau d'abord : si le chantier passe
    par un point de sortie dans la seconde, il s'arrête proprement et l'on n'a rien cassé.
    L'exception ensuite, pour les boucles Python qui ne consultent rien. La fermeture du
    lien enfin, seule capable de sortir d'un appel réseau bloquant.

    Ce qui est posé RESTE posé : on interrompt un travail, on ne défait rien.
    """
    import threading
    import time as _t

    _CHANTIER["arret"] = True
    # ON ARRÊTAIT LE DONNEUR D'ORDRES, PAS L'OUVRIER. Les trois étages ci-dessous visent
    # tous le côté PYTHON — or le travail réel se fait dans une tâche du mod :
    # `task_manager` fait marcher puis miner l'avatar sur `on_tick`, et rien de ce qu'on
    # tue côté Python ne l'atteint.
    #
    # Partie 42, vu à l'écran par le joueur pendant que je lisais le contraire dans le
    # journal : « chantier ARRÊTÉ à la demande » à 11:15:29, et le personnage continuait
    # de miner. Un journal qui annonce un arrêt qui n'a pas lieu est pire que pas de
    # journal — il m'avait fait valider H70 sur une preuve fausse.
    #
    # Le mod expose `fl_ops.cancel` depuis toujours (`operations.cancel()` ->
    # `task_manager.clear()` : marche stoppée, file vidée, tâche courante oubliée). Il
    # n'était simplement jamais appelé. On le fait EN PREMIER, avant de tuer quoi que ce
    # soit : c'est le seul étage qui touche le jeu, et il doit partir même si le fil est
    # déjà mort — une tâche de minage peut lui survivre.
    try:
        _api().cancel()
    except Exception:
        pass

    fil = _CHANTIER.get("fil")
    if fil is None or not fil.is_alive():
        return False

    def _insister():
        # Une seconde de grâce : le chantier a peut-être un point de sortie tout proche,
        # et s'arrêter proprement vaut mieux que d'être tué.
        _t.sleep(1.0)
        if not (fil.is_alive()):
            return
        _tuer_le_fil(fil)
        _t.sleep(2.0)
        if fil.is_alive():
            _debloquer_le_lien()      # il attend le jeu : on lui coupe l'attente
            _tuer_le_fil(fil)

    threading.Thread(target=_insister, daemon=True, name="tueur-chantier").start()
    return True


def _doit_s_arreter() -> bool:
    return bool(_CHANTIER.get("arret"))


def _etat_chantier() -> str:
    """Où en est le chantier, ou son résultat complet s'il est fini.

    SUIVRE N'EST PAS ATTENDRE — mais un chantier dont on ne peut pas lire l'issue ne vaut
    rien : l'agent saurait qu'il a demandé une chaîne, jamais si elle est là. On rend donc
    le résultat ENTIER une fois fini, le même texte que l'outil synchrone rendait avant.
    """
    import time as _t
    if not _CHANTIER["n"]:
        return "aucun chantier lancé"
    n, nom = _CHANTIER["n"], _CHANTIER["nom"]

    # RENDRE LA MAIN NE SUFFIT PAS : ENCORE FAUT-IL AVOIR QUELQUE CHOSE À EN FAIRE.
    # Partie 24, dix secondes après le premier chantier — 16:18:35, 16:18:37, 16:18:38 :
    # une interrogation par seconde. L'agent attend ACTIVEMENT, et son budget de cinq cents
    # tours s'épuise en un quart d'heure sans qu'il ait rien fait d'autre. Le défaut est le
    # nôtre : « appelle régulièrement » ne dit pas à quel rythme, et un agent qui n'a rien
    # d'autre à faire appelle aussi vite qu'il peut.
    #
    # On ne corrige pas cela par une consigne, qui dépend du modèle, mais par l'outil :
    # quand rien n'a changé, il ATTEND avant de répondre. Redemander coûte alors un tour au
    # lieu de dix. L'attente se coupe dès qu'il se passe quelque chose — chantier fini, ou
    # joueur qui parle — donc elle ne retarde jamais rien.
    fin = _t.time() + ATTENTE_SUIVI_S
    while _chantier_tourne() and _t.time() < fin:
        try:
            if (_api().peek_messages() or {}).get("messages"):
                break                      # le joueur parle : on rend la main aussitôt
        except Exception:
            pass
        _t.sleep(0.2)

    if _chantier_tourne():
        depuis = _t.time() - _CHANTIER["debut"]
        arret = " — arrêt demandé, il finit sa pose en cours" if _CHANTIER["arret"] else ""
        return (f"chantier n°{n} « {nom} » EN COURS depuis "
                f"{int(depuis // 60)} min {int(depuis % 60)} s{arret}")
    r = _CHANTIER["resultat"]
    return f"chantier n°{n} « {nom} » terminé — {r}" if r is not None else            f"chantier n°{n} « {nom} » terminé sans résultat"



def _debut(nom: str, args: dict) -> None:
    """UN APPEL EN COURS DOIT SE VOIR. Ne tracer qu'à la sortie rend invisible tout ce
    qui dure — c'est-à-dire précisément ce qu'on veut observer : bâtir une chaîne occupe
    plusieurs minutes, et pendant ce temps le journal restait muet.

    Mesuré sur quatre parties : on jugeait le comportement de l'agent sur l'état du jeu
    faute de voir ses gestes, et « il mine à la main » a été conclu trois fois sans
    pouvoir distinguer un `se_procurer` d'un `batir_une_chaine` qui s'approvisionne. Ce
    ne sont pas les mêmes conduites : la seconde est celle qu'on lui demande.
    """
    import datetime
    _tracer(f"{datetime.datetime.now():%H:%M:%S} >> {nom:26s} {str(args)[:80]}")


def _fin(nom: str, reponse: str, duree: float) -> None:
    import datetime
    _tracer(f"{datetime.datetime.now():%H:%M:%S} << {nom:26s} {duree:6.1f}s "
            # LA RAISON D'UN ECHEC EST TOUJOURS EN FIN DE RAPPORT. Tronquer a 400 la
            # coupait systematiquement : « 0 sortie(s) evacuee(s) » restait sans motif
            # trois heures durant, alors que le rapport le nommait.
            f"{str(reponse)[:1200]}")


# UN SEUL OUTIL À LA FOIS. Le lien RCON est un singleton et n'est pas réentrant : deux
# outils en parallèle mélangeraient leurs réponses. Ce verrou sérialise le TRAVAIL sans
# bloquer le TRANSPORT — c'est toute la différence avec l'exécution sur la boucle.
_VERROU_JEU = threading.Lock()



def _bandeau_du_joueur(resultat):
    """Fait précéder `resultat` de ce que le joueur a tapé dans le chat, s'il a parlé.

    LE POINT DUR N'EST PAS DE LIRE LES MESSAGES, C'EST DE LES LIVRER. Un outil dédié que
    l'agent appellerait « quand il y pense » ne sert à rien : c'est précisément quand il
    s'enlise qu'il cesse de regarder autour de lui. Partie 22 en donne la mesure — 534
    plaques produites par son usine, zéro en poche, et il repart en miner cent cinquante
    à la pioche sans jamais rien demander à personne. Le message part donc en tête de la
    PROCHAINE réponse d'outil, quelle qu'elle soit : il coupe la parole.

    Il ne se répète pas — la file se vide à la lecture. Un conseil relivré à chaque appel
    deviendrait un bruit de fond que l'agent apprendrait à sauter, et on aurait dépensé
    le seul canal dont on dispose pour le rendre inaudible.

    ET IL S'EFFACE QUAND IL N'A RIEN À DIRE. Ce bandeau se greffe sur CHAQUE outil : une
    ligne ajoutée à vide serait lue des centaines de fois par partie pour rien, et une
    panne du canal ferait tomber tous les outils avec elle. D'où le retour à l'identique
    et le `except` large — le chat est un confort, jamais une dépendance.
    """
    if not isinstance(resultat, str):
        return resultat

    # LA FIN D'UN CHANTIER SE DIT AUSSI SANS QU'ON LA DEMANDE. Partie 24 : le chantier
    # finit à 16:30:41, plus rien pendant 2 min 23, puis l'agent redemande l'arrêt de ce
    # qui est déjà terminé. Il attendait un signal qui ne vient jamais, pendant que le
    # résultat de sa construction dormait dans le serveur.
    #
    # Le bandeau sert déjà à livrer ce qui compte sans qu'on le demande — c'est ainsi que
    # les messages du joueur parviennent. Une fin de chantier relève du même besoin, et
    # se dit une seule fois. Elle reste SÉPARÉE de la parole du joueur : lui n'a pas dit
    # cela, et confondre les deux ferait attribuer à un humain ce que la machine annonce.
    global _FIN_ANNONCEE
    fini = (not _chantier_tourne() and _CHANTIER["n"]
            and _CHANTIER["resultat"] is not None
            and _CHANTIER["n"] != _FIN_ANNONCEE)
    if fini:
        _FIN_ANNONCEE = _CHANTIER["n"]
        resultat = (f"CHANTIER TERMINÉ — n°{_CHANTIER['n']} « {_CHANTIER['nom']} » : "
                    f"{_CHANTIER['resultat']}\n\n{resultat}")

    try:
        msgs = (_api().read_messages() or {}).get("messages") or []
    except Exception:
        return resultat
    if not msgs:
        return resultat
    dits = "\n".join(f"  [{m.get('joueur', '?')}] {m.get('texte', '')}" for m in msgs)
    # ON TRACE CE QU'ON SOUFFLE. Sans cela un message livré et un message perdu se
    # ressemblent : le 10/08, trois messages envoyés sans réaction visible m'ont fait
    # chercher dix minutes une panne du canal qui n'existait pas — l'agent était
    # simplement au milieu d'un `batir_une_chaine` de plusieurs minutes, et aucun outil
    # n'avait encore rendu. Et surtout, les résultats d'une partie où l'on a soufflé ne
    # valent pas ceux d'une partie autonome ; on ne peut pas s'en souvenir après coup.
    try:
        _fin("MESSAGE DU JOUEUR", dits, 0.0)
    except Exception:
        pass
    # « LE JOUEUR TE PARLE » NE DISAIT NI QUI IL EST, NI CE QU'IL VOIT. L'agent recevait
    # une phrase sans provenance ni portée, au milieu d'un `tool_result` — au même rang
    # que « 12 machines ». Mesuré sur six parties : 19 réponses sur 22, mais l'action ne
    # suit que dans 29 % des cas quand un chantier tourne.
    #
    # On lui donne les deux faits qui manquent, les mêmes que le pont met dans le tour
    # `user` : l'humain REGARDE L'ÉCRAN, et arrêter NE DÉTRUIT RIEN. Le second change son
    # calcul — il refusait d'interrompre comme si couper faisait tout perdre.
    # C'EST CELUI QUI LIT QUI DOIT LIBÉRER L'AVATAR. Deux lecteurs se disputaient la file :
    # le bandeau avec `read_messages`, qui la VIDE par conception, et le veilleur avec
    # `peek_messages`, qui regardait ensuite et ne trouvait plus rien. Le veilleur
    # n'arrêtait donc jamais rien — mesuré partie 38 : l'agent répond en quatre secondes,
    # et le chantier continue comme si de rien n'était.
    #
    # Le veilleur garde son rôle — accuser réception DANS LE JEU quand personne ne répond —
    # mais couper appartient à celui qui a le message en main.
    if _chantier_tourne():
        _demander_l_arret()
        try:
            _api().say("j'arrête le chantier pour t'écouter — il reprendra où il en "
                       "était si besoin")
        except Exception:
            pass

    return ("LE JOUEUR TE PARLE — il regarde l'écran, toi tu lis des compteurs :\n"
            f"{dits}\n"
            "Ce qu'il décrit, il le voit — un four vide, une machine épuisée, du minerai "
            "par terre — et cela se vérifie en secondes avec `regarder`. Il peut se "
            "tromper ou parler d'un état déjà changé : dis-le et mesure. "
            "`arreter_le_chantier` NE DÉTRUIT RIEN : ce qui est posé reste posé, et "
            "relancer reprend où l'on s'était arrêté.\n\n"
            f"{resultat}")


def outil(fn=None, *, ecrit: bool = True):
    """Déclare un outil MCP et journalise chaque appel, HORS de la boucle d'événements.

    Enveloppe `mcp.tool()` plutôt que de le remplacer : la signature et la docstring —
    donc ce que l'agent LIT de l'outil — restent celles de la fonction.

    UN OUTIL SYNCHRONE GÈLE TOUT LE SERVEUR. Nos outils parlent RCON pendant des dizaines
    de secondes (`chercher_une_technologie` : 72 s, `batir_une_chaine` : 647 s). Exécutés
    sur la boucle asyncio, ils suspendent le transport entier : plus de ping, plus de
    session gérée, plus rien. Le client en conclut que le lien est mort, ferme la session
    et en rouvre une — et la réponse, produite ensuite, part sur une session sans
    destinataire.

    Mesuré des deux côtés. En jeu (9e partie) : serveur fini en 71,9 s, client en
    « TimeoutError after 900.0s » — quinze minutes perdues, et Hermes conclut à une panne
    du serveur pour un appel qui avait RÉUSSI. Hors du jeu (banc `sonde_mcp_bloque`) : un
    outil qui dort 20 s empêche un second appel de seulement s'INITIALISER avant sa fin.

    D'où `to_thread` : le travail part sur un thread, la boucle reste libre de répondre.
    """
    if fn is None:                      # usage `@outil(ecrit=False)`
        import functools as _ft
        return _ft.partial(outil, ecrit=ecrit)

    import functools
    import time

    import anyio.to_thread

    def _travail(*a, **kw):
        # LE VERROU N'EST PAS POUR TOUT LE MONDE. Il protège contre deux ÉCRITURES
        # simultanées — deux constructions pilotant le même avatar produiraient
        # n'importe quoi. Les LECTURES n'engagent pas le personnage, et le lien RCON
        # gère déjà sa propre concurrence : `RconClient.query` prend un verrou à chaque
        # échange, donc deux appels se sérialisent au niveau de la REQUÊTE.
        #
        # Mesuré partie 17 : `batir_une_chaine` dure 2261 s et `etat_du_jeu` lancé
        # pendant ce temps répond en 457 s. L'agent ne pouvait pas regarder son usine
        # pendant qu'il la construisait — aveugle sur sa propre action.
        if not ecrit:
            return fn(*a, **kw)
        # UNE GARDE NON BRANCHÉE EST UNE GARDE INEXISTANTE. Trois correctifs justes se
        # sont révélés inertes cette semaine (H10, H23, H27), chacun cru bon pendant une
        # partie entière. On la pose donc là où TOUTE action passe, plutôt que dans chaque
        # outil où il suffit d'en oublier un.
        souci = _avatar_absent()
        if souci:
            return f"refusé : {souci}"
        # UN SEUL AVATAR — ET LE GARDE-FOU NE COUVRAIT QUE LA MOITIÉ DU DANGER.
        # `_lancer_chantier` refuse bien un second CHANTIER, mais `extraire_ici`,
        # `demonter`, `reparer` et `se_deplacer` sont des actions directes : elles ne
        # passent pas par lui. Or le chantier travaille dans un thread qui ne tient aucun
        # verrou — il l'a relâché en rendant la main, c'est tout l'intérêt du modèle. Rien
        # n'empêchait donc de poser une extraction pendant qu'une chaîne se bâtissait,
        # avec deux constructions faisant marcher le même personnage en sens contraire.
        #
        # Le 09/08, deux conteneurs sur la même partie ont donné cinquante-cinq minutes de
        # jeu illisibles. Le code se protégeait de ce cas-là et laissait l'autre porte
        # ouverte. Les LECTURES restent libres : c'est ce qui permet de regarder son usine
        # pendant qu'on la bâtit.
        if _chantier_tourne() and fn.__name__ != _CHANTIER["nom"].split("(")[0]:
            import time as _t
            depuis = _t.time() - _CHANTIER["debut"]
            return (f"refusé : chantier n°{_CHANTIER['n']} « {_CHANTIER['nom']} » en cours "
                    f"depuis {int(depuis // 60)} min {int(depuis % 60)} s, et il n'y a "
                    f"qu'un avatar. Attends-le (`ou_en_est_le_chantier`) ou libère la "
                    f"place (`arreter_le_chantier`).")
        with _VERROU_JEU:
            return fn(*a, **kw)

    @functools.wraps(fn)
    async def enveloppe(*a, **kw):
        args = kw or dict(enumerate(a))
        _debut(fn.__name__, args)
        t0 = time.time()
        _EN_COURS[fn.__name__] = t0
        try:
            r = await anyio.to_thread.run_sync(functools.partial(_travail, *a, **kw))
        except Exception as e:
            _EN_COURS.pop(fn.__name__, None)
            _fin(fn.__name__, f"ERREUR {type(e).__name__}: {e}", time.time() - t0)
            raise
        _EN_COURS.pop(fn.__name__, None)
        # ON JOURNALISE CE QUI EST LIVRÉ, PAS CE QU'ON A CALCULÉ. Le bandeau se greffait
        # APRÈS l'écriture du journal : ni la parole du joueur ni le « CHANTIER TERMINÉ »
        # n'y figuraient, alors qu'ils partaient bel et bien dans la réponse.
        #
        # Le 11/08, cela m'a fait diagnostiquer à tort que l'agent n'avait jamais lu le
        # compte rendu de neuf minutes de construction — il l'avait reçu, en tête de
        # l'appel suivant. J'écrivais le correctif d'un défaut qui n'existait pas.
        # Un journal qui montre autre chose que la réalité est pire que pas de journal :
        # il fabrique des coupables, exactement comme un double de test qui rend la
        # fiction qu'on s'est faite du mod plutôt que sa vraie réponse.
        livre = _bandeau_du_joueur(r)
        _fin(fn.__name__, livre, time.time() - t0)
        return livre

    enveloppe.__fl_ecrit__ = ecrit
    return mcp.tool()(enveloppe)


# ------------------------------------------------------------------------- OBSERVER

@outil(ecrit=False)
def etat_du_jeu() -> str:
    """L'état de l'usine : machines, énergie, diagnostic, inventaire, recherche visée.

    À appeler EN PREMIER, et après chaque action : c'est l'état relu qui dit si l'action
    a servi, jamais le fait qu'elle ait été prise.
    """
    from services.arbitre import resumer_etat
    return resumer_etat(_coord().observer())


@outil(ecrit=False)
def repondre_au_joueur(texte: str) -> str:
    """Écrit dans le chat du jeu — c'est ainsi que l'humain qui te regarde te lit.

    Tes réponses habituelles vont dans un journal qu'il n'a pas sous les yeux. Quand il te
    dit quelque chose, un mot ici lui montre que tu l'as reçu et ce que tu comptes en
    faire. Utile aussi pour annoncer ce que tu vas entreprendre avant une longue action :
    pendant qu'un outil travaille, tu ne peux plus rien dire.
    """
    _api().say(texte)
    return f"dit dans le jeu : « {texte} »"


@outil
def demonter(x: float, y: float, combien: int = 1) -> str:
    """Démonte ce qui se trouve à cette position et en RÉCUPÈRE le contenu.

    Même geste que dans le jeu : miner une épave, un four que tu as posé ou un rocher, ce
    n'est qu'une seule et même action. Sert à récupérer les ressources d'un
    `crash-site-spaceship`, à reprendre une machine mal placée, ou à dégager un obstacle.

    Tu désignes par POSITION, jamais par nom : le jeu sait ce qui s'y trouve. Il faut être
    à portée — une dizaine de tuiles — sans quoi le jeu refuse.
    """
    api = _api()
    quoi = _machine_a(api, x, y) or _nom_a(api, x, y)
    if not quoi:
        return f"rien à démonter en ({x:.0f},{y:.0f})"
    from services import deplacement
    # QUATRE TUILES, PAS HUIT. Le mod cherche la cible d'un minage dans un rayon de cinq ;
    # la tolérance par défaut nous arrêtait à huit, et le geste échouait toujours en
    # « cible hors portée / introuvable » — un motif qui accuse la distance alors qu'on ne
    # s'était pas assez approché. Mesuré partie 37 : cinq refus d'affilée à 5,5 tuiles.
    deplacement.marcher_vers(api, x, y, tolerance=4.0)
    r = api.run_action(api.mine_entity, quoi, int(combien))
    ok = isinstance(r, dict) and r.get("ok") is not False
    return (f"{'OK' if ok else 'ÉCHEC'} — démonté {quoi} en ({x:.0f},{y:.0f}) ; "
            f"ce qu'il en reste va dans ton inventaire" if ok else
            f"ÉCHEC — {quoi} en ({x:.0f},{y:.0f}) non démonté : {r}")


def _nom_a(api, x: float, y: float) -> str:
    """Le nom de CE QUI EST LÀ, même quand ce n'est pas une machine.

    `_machine_a` ne retient que ce qui peut tomber en panne — c'est ce qu'il faut pour
    réparer, pas pour démonter. Une épave, un rocher ou une belt se minent tout autant.
    """
    try:
        vues = (api.inspect_at(x, y, 1.0) or {}).get("entities") or []
    except Exception:
        return ""
    for e in vues:
        nom = str(e.get("name") or "")
        if nom and nom != "character":
            return nom
    return ""


def _foreuse_posee_pres_de(api, ressource: str):
    """La foreuse DÉJÀ en terre sur ce gisement, s'il y en a une. Sinon None.

    Une extraction à moitié posée est le cas normal, pas l'exception : une pose peut
    échouer, un chantier être arrêté, le joueur intervenir. La refuser oblige à tout
    démonter — ce que le jeu interdit justement quand la foreuse couvre du minerai.
    """
    # ON LIT LE PARC, PAS UN RAYON AUTOUR D'UN POINT INCERTAIN. La recherche partait de
    # `find_nearest(ressource)` — un point relatif au JOUEUR — puis inspectait six tuiles
    # autour. Le joueur se déplace ; ce point désigne alors un autre bout du gisement, et
    # la foreuse posée n'y est pas.
    #
    # Partie 37 : l'outil venait de poser une foreuse en (-65,9) et le disait
    # (« INCOMPLET … MANQUE wooden-chest »). Relancé pour compléter, il réclamait une
    # foreuse « manquante » — celle qui était en terre, à l'endroit qu'il avait lui-même
    # nommé. Trois refus de suite. Même défaut que le rayon de vingt-cinq tuiles de la
    # récolte (H63) : une recherche bornée autour d'un repère mouvant.
    #
    # MAIS UNE FOREUSE N'EST PAS L'AUTRE. `ressource` était reçu ici et lu NULLE PART : on
    # rendait la première foreuse du parc. Partie 38 : le joueur demande une foreuse sur le
    # charbon, l'agent obéit dans la minute, et `extraire_ici('coal')` ancre sur sa foreuse
    # de FER en (38,46) — « cannot place here », trois fois, la tuile étant occupée par
    # elle. Le filtre s'est perdu en gagnant la portée, au correctif ci-dessus.
    #
    # On regarde donc ce qu'il y a SOUS la foreuse, et l'on ne conclut rien de ce qu'on ne
    # voit pas : sans ressource lisible, on rend None et le chemin normal (`find_nearest`)
    # reprend la main. Le raccourci, lui, ancrerait sur une tuile non vérifiée.
    # `inspect_at` NE REND JAMAIS DE RESSOURCE — et c'est voulu. Le mod l'exclut
    # explicitement (`tools.lua:837`, `e.type ~= "resource"`), sans quoi une seule
    # inspection rendrait des milliers de tuiles de minerai. J'avais écrit ce filtre
    # dessus : il ne trouvait donc RIEN, rendait toujours None, et supprimait la reprise
    # d'une foreuse existante que H63 venait justement d'obtenir.
    #
    # Le test passait au vert parce que mon double retournait des ressources — la fiction
    # que je m'étais faite du mod, pas sa réponse. C'est le piège de la clé `found`
    # inventée pour `extraire_ici`, refait à l'identique et le même jour. Un double doit
    # refléter le CONTRAT MESURÉ : ici `inspect_at` -> {entities: [...]} sans ressources,
    # et `scan_patches` -> {patches: [{x1,y1,x2,y2,...}]}.
    #
    # Limite assumée : `scan_patches` part du PERSONNAGE et la boîte englobante d'un
    # gisement peut couvrir des trous. On répond donc « cette foreuse est sur ce
    # gisement-là » avec la précision d'un rectangle, ce qui suffit à distinguer le fer du
    # charbon — la question posée.
    from services import perception
    try:
        tout = perception.parc(api) or []
    except Exception:
        tout = []
    foreuses = [(float(e.get("x", 0.0)), float(e.get("y", 0.0))) for e in tout
                if str(e.get("name", "")).endswith("mining-drill")]
    if not foreuses:
        return None
    try:
        boites = [(float(p["x1"]), float(p["y1"]), float(p["x2"]), float(p["y2"]))
                  for p in ((api.scan_patches(ressource, 500.0, 20) or {}).get("patches")
                            or []) if p.get("x1") is not None]
    except Exception:
        return None
    for x, y in foreuses:
        # Une foreuse fait 2×2 : sa position est un coin de son emprise, d'où la marge.
        if any(x1 - 1.5 <= x <= x2 + 1.5 and y1 - 1.5 <= y <= y2 + 1.5
               for x1, y1, x2, y2 in boites):
            return (x, y)
    return None


def _ancres_du_gisement(api, ressource: str, depuis) -> list:
    """Les cases d'un gisement où tenter la pose, de la plus proche à la plus lointaine.

    TROIS ARBRES ONT FAIT ÉCHOUER UNE POSE SUR SIX CENT SOIXANTE-ONZE TUILES LIBRES.
    Mesuré en jeu le 12/08 :

        en (6,98)  : 2 tree-08-red, 1 dead-tree-desert   can_place = False
        patch fer  : de (-9,98) à (22,128) — 671 tuiles
        11:27:24 ÉCHEC … 11:31:23 ÉCHEC … 11:31:42 ÉCHEC — toujours la même case

    L'outil demandait UN point à `find_nearest` et abandonnait s'il était pris. Le
    troisième essai n'a réussi que parce que le personnage avait bougé, si bien que
    `find_nearest` a rendu une autre case : de la chance, pas de la robustesse.

    On garde ce point en tête de liste — c'est le plus proche, et marcher n'est pas
    gratuit (leçon `ancres_par_proximite`) — puis on prend les TUILES DE MINERAI, pas la
    boîte englobante.

    LA BBOX MENT, ET LE MOD LE DIT DEPUIS LONGTEMPS. Première version de cette fonction :
    je balayais la boîte de `scan_patches`, et `can_place` acceptait volontiers une case
    d'herbe — rien n'y gêne. Mesuré en jeu le 13/08, quelques minutes après l'avoir
    écrite : foreuse posée en (-38,-72), minerai réel à 1,6 tuile, `no_minable_resources`.
    Elle tournait dans le vide.

    `tools.lua` porte l'avertissement noir sur blanc : « la bbox enveloppe tous les patches
    du rayon, trous compris — le LayoutPlanner s'y fiait et posait un foreur sur de
    l'herbe ». J'avais refait son erreur sans lire son commentaire.

    Contrat MESURÉ de `scan_patch` (singulier) : {resource, count, bbox, sample:[{x,y}],
    origin, total_amount}. Le `sample` porte jusqu'à quatre cents tuiles de minerai
    GARANTI, déjà triées par distance à l'observateur — exactement ce qu'il faut.
    """
    import math

    tete = None
    try:
        r = api.find_nearest(ressource) or {}
        if r.get("x") is not None:
            tete = (float(r["x"]), float(r["y"]))
    except Exception:
        tete = None

    # ON GÉNÈRE AVANT DE SCANNER. Sur une carte neuve, les chunks autour du gisement
    # n'existent pas encore pour le jeu : `scan_patch` ne voit rien, et l'on retombe sur le
    # secours `find_nearest` — qui pointe alors une case vide.
    #
    # Banc piloté du 13/08, premier geste sur carte fraîche :
    #   06:56:48  extraire_ici(iron-ore)
    #   06:58:19  ÉCHEC — rien posé en (0,1) : pose non confirmée   [91 s]
    #   06:58:33  OK — extraction posée en (3,12)                   [1,5 s]
    # (0,1) n'était pas du minerai. C'est la marche du premier essai qui a généré le
    # terrain, d'où la réussite immédiate du second.
    #
    # `find_nearest` sert à savoir OÙ générer : c'est son seul rôle utile depuis que le
    # `sample` fournit les tuiles (H92).
    if tete is not None:
        try:
            api.generate_terrain(tete[0], tete[1], 64.0)
        except Exception:
            pass

    try:
        sample = (api.scan_patch(ressource, 400.0) or {}).get("sample") or []
    except Exception:
        sample = []

    vues, proches = set(), []
    for t in sample:
        if t.get("x") is None:
            continue
        c = (float(t["x"]), float(t["y"]))
        if c not in vues:
            vues.add(c)
            proches.append(c)

    # ÊTRE SUR DU MINERAI NE SUFFIT PAS : IL FAUT EN AVOIR AUTOUR. La tuile la plus proche
    # est souvent en BORDURE du gisement — une foreuse burner couvre 2×2, elle n'a alors
    # qu'une ou deux tuiles sous elle et les épuise en trois minutes. Mesuré au banc du
    # 13/08, signalé par le joueur depuis l'écran : « la foreuse de fer n'a plus de minerai
    # sur son emplacement, il faut la déplacer » — statut `no_minable_resources`, trois
    # minutes après une pose que j'avais validée.
    #
    # Le `sample` porte toutes les tuiles : on compte donc les voisines de chacune. Un
    # cœur de gisement à quinze tuiles vaut mieux qu'un bord à trois — on n'y marche
    # qu'une fois, on y mine des heures.
    ici = (float(depuis[0]), float(depuis[1])) if depuis else (0.0, 0.0)

    def _voisines(c):
        return sum(1 for dx in (-1, 0, 1) for dy in (-1, 0, 1)
                   if (c[0] + dx, c[1] + dy) in vues)

    # Densité d'abord (par paliers, pour ne pas traverser la carte à la tuile près), puis
    # distance. Une case pleinement entourée vaut 9 ; on regroupe 9-8, 7-5, puis le reste.
    def _rang(c):
        n = _voisines(c)
        palier = 0 if n >= 8 else (1 if n >= 5 else 2)
        return (palier, math.dist(ici, c))

    proches.sort(key=_rang)

    # `find_nearest` ne garde PLUS la tête. Elle l'avait tant que le reste venait d'une
    # bbox douteuse — c'était la seule tuile de minerai garanti. Depuis que le `sample`
    # n'en contient que des vraies, son seul privilège est d'être la plus PROCHE, et c'est
    # précisément ce qui la place en bordure. Elle ne sert plus que de secours quand le
    # `sample` est vide (gisement hors du rayon scanné).
    if not proches and tete is not None:
        proches = [tete]
    # ASSEZ POUR CONTOURNER, PAS ASSEZ POUR BOUCLER. Un bosquet fait quelques tuiles ; si
    # quarante essais échouent, ce n'est plus un obstacle local et il faut le DIRE plutôt
    # que d'arpenter le gisement pendant dix minutes.
    return proches[:40]


def _extraction_deja_complete(api, ancre, receveur: str) -> str:
    """Ce qui est DÉJÀ posé à cet endroit, si l'extraction y est entière. Sinon "".

    CENT SOIXANTE-QUATRE SECONDES DE MARCHE POUR REPOSER CE QUI EST DÉJÀ LÀ. Banc piloté
    du 13/08 :

        06:45:12  extraire_ici(iron-ore)
        06:47:56  ÉCHEC — rien posé en (-32,-9) : coal (il t'en manque 4)   [164 s]
        06:49:43  ÉCHEC — rien posé en (-32,-9) : cannot place here
                  — burner-mining-drill, stone-furnace

    L'obstacle que H88 nomme dit tout : ce qui gêne, c'est SA PROPRE foreuse et SON four.
    L'extraction était entière, la chaîne tournait, et l'outil repartait marcher jusqu'au
    gisement pour tenter de reposer une foreuse sur elle-même.

    Le chemin de reprise (H63, H82) reste juste quand il MANQUE quelque chose — foreuse
    posée sans son four, mesuré partie 31, l'agent enfermé faute de pouvoir compléter. Il
    n'a aucun sens quand les deux sont là : on constate, on le dit, on rend la main.
    """
    try:
        ents = (api.inspect_at(float(ancre[0]), float(ancre[1]), 3.0) or {}).get("entities") or []
    except Exception:
        return ""
    noms = {str(e.get("name")) for e in ents}
    if "burner-mining-drill" in noms and receveur in noms:
        return f"burner-mining-drill et {receveur}"
    return ""


def _pourquoi_refus(api, x: float, y: float) -> str:
    """Ce qui occupe la case, en clair. Chaîne vide si l'on ne voit rien.

    « CANNOT PLACE HERE » — UN ARBRE S'ABAT, UN LAC NON. Le message ne disait pas ce qui
    gênait, et la conduite à tenir en dépend entièrement : devant un arbre on déboise,
    devant une machine on démonte ou l'on se décale, devant de l'eau on change de gisement.
    L'agent a réessayé trois fois à l'identique, faute du mot.

    `inspect_at` exclut les ressources (`tools.lua:837`, cf. H82) mais rend tout le reste —
    arbres, rochers, machines. Quand il ne rend RIEN et que la pose est pourtant refusée,
    c'est le terrain lui-même (eau, bord de carte) : on le dit comme une hypothèse, pas
    comme un fait, puisqu'on ne l'a pas lu.
    """
    from collections import Counter
    try:
        ents = (api.inspect_at(float(x), float(y), 1.5) or {}).get("entities") or []
    except Exception:
        return ""
    if not ents:
        return "rien de visible — c'est sans doute le terrain (eau, bord de carte)"
    arbres = [e for e in ents if str(e.get("type")) == "tree"
              or "tree" in str(e.get("name", ""))]
    autres = [e for e in ents if e not in arbres]
    bouts = []
    if arbres:
        bouts.append(f"{len(arbres)} arbre(s)")
    if autres:
        c = Counter(str(e.get("name")) for e in autres)
        bouts.append(", ".join(f"{n}×{k}" if n > 1 else k for k, n in c.most_common(3)))
    return " et ".join(bouts)


@outil
def extraire_ici(ressource: str = "iron-ore") -> str:
    """Pose TOUT DE SUITE une extraction minimale : une foreuse, un four sur sa sortie.

    DEUX entités, avec ce que tu as en poche — le four posé sur la tuile où la foreuse
    dépose reçoit le minerai directement, aucun bras n'est nécessaire. Il te faut une
    foreuse ; le four, il le forge au besoin.

    C'est le geste du début de partie, quand tu tiens déjà une foreuse : il produit en
    quelques secondes là où `batir_une_chaine` planifie l'usine entière, part chercher son
    gisement au débit et fabrique ce qui lui manque avant de poser quoi que ce soit. Cette
    dernière reste le bon outil dès que tu veux du volume ; celui-ci sert à ne pas rester
    les mains vides pendant qu'elle travaille.
    """
    from services import deplacement, perception
    from services.executor import execute_micro
    from services.layout_planner import ResourcePatch
    from services.micro_planner import MicroRequest, plan_micro

    api = _api()
    inv = perception.inventory(api)
    # ON POSE AVEC CE QU'ON A. Le geste minimal perdrait tout son sens s'il déclenchait la
    # même attente que ce qu'il remplace : c'est justement d'avoir fabriqué avant de poser
    # qui a laissé la foreuse en poche quatre minutes durant (partie 24).
    # SANS FOREUSE, PAS DE GESTE MINIMAL. En forger une suppose de miner et de fondre :
    # on retombe dans la longue attente que cet outil existe justement pour éviter, et le
    # dire honnêtement vaut mieux que la simuler.
    # DÉJÀ BÂTI ? ON REPREND. Une foreuse en terre sur le bon gisement est un ACQUIS, pas
    # un obstacle. Partie 31 : la foreuse est posée, le four est resté en poche, et
    # relancer l'outil répondait « il te manque burner-mining-drill » — puisqu'elle n'est
    # plus dans la poche, justement parce qu'elle est en terre. L'agent s'est retrouvé
    # enfermé : il ne pouvait ni compléter, ni démonter (« minerai sous foreuse »), pendant
    # que sa foreuse minait dans le vide. Le joueur, lui, voyait le four manquant.
    #
    # Le Coordinator connaît ce cas depuis la partie 16 (`_foreuse_existante`) ; l'outil
    # l'ignorait. On regarde donc le terrain avant de réclamer quoi que ce soit.
    deja = _foreuse_posee_pres_de(api, ressource)
    if inv.get("burner-mining-drill", 0) < 1 and deja is None:
        return ("rien posé — il te manque burner-mining-drill, et le forger suppose de "
                "miner puis fondre. Passe par `batir_une_chaine`, qui fait tout cela, ou "
                "`se_procurer` si tu veux garder la main entre-temps.")

    # PLUS DE BRAS DU TOUT — le four posé SUR la tuile de drop reçoit directement.
    #
    # Le MicroPlanner affirmait « drop-direct drill→furnace IMPOSSIBLE en Factorio 2.0 » ;
    # sa propre note disait pourtant « four 1 TUILE TROP LOIN + drop hors axe ». Une
    # impossibilité conclue d'un mauvais placement, que personne n'avait remesurée. Le
    # joueur l'a corrigée en regardant l'écran : « tu n'as pas besoin du bras, pose une
    # foreuse et un four devant ». La géométrie lui donne raison sur les quatre
    # orientations (cf. test_micro_planner).
    #
    # Il ne reste donc que le four à forger au besoin — et le kit de départ en contient un.
    # UN FOUR DERRIÈRE UNE FOREUSE À CHARBON BOUCHE TOUTE LA CHAÎNE. Mesuré et documenté
    # dans `knowledge.fond_en` : sur un gisement de charbon, la foreuse finit
    # `waiting_for_space_in_destination` avec 33 charbons en sortie, le four `full_output`,
    # trois machines arrêtées en cascade. Le `MicroPlanner` a appris la leçon ; cet outil
    # imposait `stone-furnace` en dur, quelle que soit la ressource.
    #
    # On demande donc au JEU ce qui se fond, plutôt que de le supposer. Le geste ne change
    # pas — une entité qui reçoit sur la tuile de drop — seule sa nature change.
    from services import knowledge
    try:
        se_fond = knowledge.fond_en(api, ressource) is not None
    except Exception:
        se_fond = True                      # dans le doute, le comportement d'avant
    receveur = "stone-furnace" if se_fond else "wooden-chest"
    petit = {receveur: 1}
    forge = []
    for nom, combien in petit.items():
        if inv.get(nom, 0) >= combien:
            continue
        ok, detail = _coord().fabriquer(nom, combien)
        if not ok:
            return (f"rien posé — {nom} manque et n'a pas pu être fabriqué : {detail}")
        forge.append(nom)
    if forge:
        inv = perception.inventory(api)

    # LE CONTRAT EST CELUI DU MOD, PAS CELUI QU'ON IMAGINE. `find_nearest` rend
    # {"name","x","y","distance"} — ou {} s'il n'a rien vu. J'avais inventé une clé
    # `found` : elle valait toujours None, l'outil refusait donc TOUJOURS, et l'agent se
    # rabattait sur `batir_une_chaine` en croyant qu'il n'y avait pas de gisement.
    # Mesuré partie 29, sur un charbon qui était à douze tuiles.
    #
    # Le banc ne pouvait pas le voir : ses doubles rendaient `{"found": True, ...}`,
    # c'est-à-dire ma fiction. Un double doit refléter la RÉPONSE DU MOD, jamais l'idée
    # qu'on s'en fait — sans quoi il confirme l'erreur au lieu de la révéler.
    # UN GISEMENT OFFRE DES CENTAINES DE CASES, ON N'EN ESSAYAIT QU'UNE. Trois arbres
    # suffisaient à faire échouer la pose sur six cent soixante-onze tuiles libres —
    # mesuré le 12/08, trois refus de suite au même point, et le succès n'est venu que
    # parce que le personnage avait bougé entre-temps.
    if deja is not None:
        candidats = [(float(deja[0]), float(deja[1]))]
    else:
        candidats = _ancres_du_gisement(api, ressource, perception.position(api))
    if not candidats:
        return f"aucun gisement de {ressource} en vue — essaie `ou_sont_les_ressources`"

    # RIEN À COMPLÉTER : ON NE MARCHE MÊME PAS. Le chemin de reprise vaut quand il MANQUE
    # une pièce ; quand foreuse ET receveur sont là, repartir au gisement pour reposer une
    # foreuse sur elle-même coûte cent soixante-quatre secondes et deux échecs (mesuré au
    # banc du 13/08). On constate avant d'agir.
    if deja is not None:
        entier = _extraction_deja_complete(api, deja, receveur)
        if entier:
            return (f"rien à faire — l'extraction de {ressource} est déjà complète en "
                    f"({deja[0]:.0f},{deja[1]:.0f}) : {entier}. Vérifie qu'elle tourne "
                    f"(`regarder`) ; si elle est à l'arrêt c'est le combustible ou "
                    f"l'évacuation, pas la pose.")

    ancre = candidats[0]
    api.generate_terrain(ancre[0], ancre[1], 25.0)
    deplacement.marcher_vers(api, ancre[0], ancre[1])

    # On ne marche qu'une fois : les autres candidats sont à quelques tuiles, dans la
    # portée de construction. C'est `can_place` qui tranche — lui seul connaît le terrain,
    # les arbres et le bâti.
    from services import site_finder
    refus = {}
    for c in candidats[:12]:
        try:
            if site_finder.can_place(api, "burner-mining-drill", c[0], c[1]):
                ancre = c
                break
        except Exception:
            ancre = c
            break
        if len(refus) < 3:
            motif = _pourquoi_refus(api, c[0], c[1])
            if motif:
                refus[c] = motif
    else:
        # Aucune case libre parmi celles essayées : on DIT ce qui gênait, en clair. « cannot
        # place here » n'apprend rien — un arbre s'abat, un lac non.
        dit = " ; ".join(f"({x:.0f},{y:.0f}) : {m}" for (x, y), m in refus.items())
        return (f"rien posé sur {ressource} — les {min(len(candidats), 12)} emplacements "
                f"essayés sont pris. {dit or 'cause non lisible'}. Déboise, démonte ce qui "
                f"gêne, ou vise un autre gisement (`ou_sont_les_ressources`).")

    plan = plan_micro(MicroRequest(
        patch=ResourcePatch(resource=ressource, tiles=[], bbox=(0, 0, 0, 0)),
        facing=4, anchor=ancre, drill_tier="burner-mining-drill",
        furnace_tier=receveur, drill_size=2, furnace_size=1 if not se_fond else 2,
        sans_bras=True))
    # POSER ET ALIMENTER SONT DEUX GESTES. Réclamer dix charbons pour amorcer le four
    # faisait refuser la POSE entière — or le kit de départ n'en contient aucun
    # (burner-mining-drill, stone-furnace, wood). Partie 30 : le joueur demande « pose une
    # foreuse sur du fer et un four devant sa sortie », l'agent obéit dans la seconde,
    # marche jusqu'au gisement, et l'outil rend « rien posé ». Une foreuse posée sans
    # charbon reste une foreuse posée, qu'on ravitaille ensuite ; poser est le geste rare,
    # alimenter se répare à tout moment.
    charbon = min(10, int(perception.inventory(api).get("coal", 0)))
    rap = execute_micro(api, plan, fuel="coal", fuel_count=charbon, generate=False,
                        approach=True, timeout=30.0)
    poses = ", ".join(sorted({p.name for p in (rap.placed or [])}))
    if not rap.placed:
        # `blocked` était vide et la cause dormait dans `missing` : l'agent lisait un
        # échec sans motif, n'en tirait rien, et réessayait à l'identique — trois fois en
        # quatre-vingt-dix secondes, mesuré.
        souci = (", ".join(f"{n} (il t'en manque {c})"
                           for n, c in (rap.missing or {}).items())
                 or (str(rap.blocked[0]) if rap.blocked else "cause inconnue"))
        # ET SI C'EST LA CASE QUI REFUSE, ON DIT QUOI. « cannot place here » ne distingue
        # pas un arbre d'un lac, et la conduite à tenir en dépend entièrement.
        if "place" in souci:
            gene = _pourquoi_refus(api, ancre[0], ancre[1])
            if gene:
                souci = f"{souci} — {gene}"
        return f"ÉCHEC — rien posé en ({ancre[0]:.0f},{ancre[1]:.0f}) : {souci}"
    # ON RELIT LE MONDE, ON NE CROIT PAS LE RAPPORT. Partie 31 : l'outil annonce « posée :
    # burner-mining-drill, stone-furnace » et le jeu ne contient AUCUN four — celui-ci
    # était resté en poche. La foreuse minait donc dans le vide, saturée, son minerai par
    # terre ; l'agent a ravitaillé puis tenté d'évacuer, raisonnant juste sur des prémisses
    # fausses. C'est le joueur qui a vu le manque : « il manque le four a la sorti ».
    #
    # C'est la règle d'E1 — ne jamais croire un `ok=True` — appliquée un cran plus haut :
    # le rapport de l'executor est lui aussi une affirmation, et une extraction sans son
    # four n'extrait rien. On vérifie donc sur place, quelle que soit la cause du décalage.
    attendus = {"burner-mining-drill", receveur}
    # UNE VÉRIFICATION QUI SE RABAT SUR LE RAPPORT NE VÉRIFIE RIEN. Le `except` retombait
    # sur `rap.placed`, c'est-à-dire précisément la source qu'on refuse de croire : dès que
    # la lecture du jeu échouait, le contrôle s'annulait en silence et validait n'importe
    # quoi. Partie 35 : « posée : burner-mining-drill, wooden-chest » alors que le coffre
    # n'existe pas, du charbon par terre à côté de la foreuse, et l'agent l'annonce au
    # joueur de bonne foi — « foreuse de charbon posée AVEC COFFRE, elle tourne toute
    # seule ». Un rapport qui ment se propage jusqu'à l'humain.
    #
    # Deux lectures espacées : la pose est asynchrone côté mod, et une entité peut n'être
    # visible qu'au relevé suivant. Si le jeu reste muet, on le DIT — ne pas savoir n'est
    # pas la même chose que réussir.
    import time as _t
    vues, lu = set(), False
    for essai in range(2):
        try:
            vues |= {e.get("name") for e in
                     (api.inspect_at(ancre[0], ancre[1], 4.0) or {}).get("entities") or []}
            lu = True
            if not (attendus - vues):
                break
        except Exception:
            pass
        if essai == 0:
            _t.sleep(1.0)
    if not lu:
        return (f"posé en ({ancre[0]:.0f},{ancre[1]:.0f}) d'après le rapport, mais LE JEU "
                f"N'A PAS RÉPONDU quand j'ai voulu vérifier — regarde toi-même avec "
                f"`regarder` avant de compter dessus.")
    absents = sorted(attendus - vues)
    if absents:
        return (f"INCOMPLET en ({ancre[0]:.0f},{ancre[1]:.0f}) — posé : "
                f"{', '.join(sorted(vues & attendus)) or 'rien'} ; MANQUE "
                f"{', '.join(absents)}. Sans receveur sur sa sortie, la foreuse crache son "
                f"minerai par terre et se bloque. Relance `extraire_ici` pour compléter.")

    dit_forge = f" (forgé au passage : {', '.join(forge)})" if forge else ""
    manque_feu = (" — ELLES N'ONT PAS DE COMBUSTIBLE : sans charbon dans la foreuse et le "
                  "four, rien ne tourne. `se_procurer('coal')` puis `reparer('ravitailler')`."
                  if charbon <= 0 else "")
    return (f"OK — extraction posée en ({ancre[0]:.0f},{ancre[1]:.0f}) : {poses}{dit_forge}"
            f"{manque_feu}. Vérifie qu'elle tourne (`regarder`), puis évacue ou étends.")


def _attendre_le_joueur(api, dort, limite_s: float, pas_s: float = 0.5) -> bool:
    """Patiente jusqu'à ce que le joueur parle. Rend True s'il a parlé.

    CINQUANTE-HUIT SECONDES POUR LIVRER UNE PHRASE. Mesuré le 12/08 en mode piloté :

        11:31:50  il répond, puis rend la main       (fin de session)
        11:32:27  le pont relance un conteneur
        11:32:48  première action de la session suivante

    Quand le bandeau livre pendant qu'il travaille, c'est HUIT secondes. Quand il a rendu
    la main, c'est une minute : `hermes chat -q` est une commande one-shot, et le mode
    piloté lui demande justement de s'arrêter après chaque étape — chaque consigne payait
    donc un démarrage de conteneur.

    Rester vivant coûte quelques tours de modèle ; relancer coûte une minute à chaque
    phrase du joueur. `ou_en_est_le_chantier` ne peut pas jouer ce rôle : sans chantier en
    cours il rend « aucun chantier lancé » dans la milliseconde, et l'agent boucle à vide.

    L'attente est BORNÉE : un agent qui ne reprend jamais la main ne pourrait plus ni
    signaler une panne, ni être arrêté.
    """
    reste = float(limite_s)
    while reste > 0:
        try:
            if (api.peek_messages() or {}).get("messages"):
                return True
        except Exception:
            pass
        dort(pas_s)
        reste -= pas_s
    return False


@outil(ecrit=False)
def attendre_le_joueur(secondes: float = 20.0) -> str:
    """Patiente jusqu'à ce que le joueur t'écrive — sans consommer de tour pour rien.

    À appeler quand tu as fini ce qu'il t'a demandé et que tu attends la suite. L'appel se
    coupe DÈS qu'il parle, et son message arrive en tête de la réponse ; s'il ne dit rien,
    il rend la main au bout du délai et tu peux rappeler.

    Cela vaut mieux que de t'arrêter : reprendre une session coûte près d'une minute au
    joueur, alors qu'ici il est servi en quelques secondes.
    """
    import time as _t
    limite = max(1.0, min(float(secondes), 60.0))
    a_parle = _attendre_le_joueur(_api(), _t.sleep, limite)
    if a_parle:
        return "le joueur vient d'écrire — sa consigne est en tête de ce message."
    return (f"rien de neuf après {int(limite)} s. Rappelle `attendre_le_joueur` si tu n'as "
            f"rien d'autre à faire, ou vérifie ton usine (`etat_du_jeu`, `diagnostiquer`).")


@outil(ecrit=False)
def ou_en_est_le_chantier() -> str:
    """Où en est le travail lancé en fond — et ce qu'on te dit pendant ce temps.

    Les constructions durent des minutes. Elles tournent désormais en fond : tu gardes la
    main et tu suis ici. Appelle-le régulièrement pendant qu'un chantier tourne — c'est là
    que tu recevras ce que le joueur t'écrit, et tu jugeras alors s'il faut arrêter le
    chantier ou le laisser finir.

    Une fois terminé, il rend le résultat complet de la construction.
    """
    return _etat_chantier()


@outil(ecrit=False)
def arreter_le_chantier() -> str:
    """Arrête le travail en cours, à n'importe quel moment — il est TUÉ s'il résiste.

    Le chantier s'arrête d'abord proprement s'il passe par un point de sortie ; sinon il
    est interrompu de force, en trois secondes au plus. Ce qui est posé RESTE posé, et
    relancer la même construction reprend où elle s'était arrêtée.

    À utiliser quand ce que tu viens d'apprendre rend le chantier inutile ou nuisible —
    pas par principe : un chantier qui va au bout coûte moins que trois relances.
    """
    tournait = _demander_l_arret()
    if not tournait:
        return "aucun chantier en cours — rien à arrêter"
    return ("arrêt en cours — le chantier s'arrête proprement s'il le peut, et il est TUÉ "
            "sinon, en trois secondes au plus. Ce qui est posé reste posé, et relancer la "
            "même construction reprend où elle s'était arrêtée. Vérifie par "
            "`ou_en_est_le_chantier`.")


@outil(ecrit=False)
def diagnostiquer(x: float = 0.0, y: float = 0.0, rayon: float = 30.0) -> str:
    """Pourquoi les machines d'une zone ne tournent pas — la CAUSE, pas le symptôme.

    Distingue une machine débranchée d'une machine sans courant, et ne prend jamais un
    organe de transit (bras, belt) pour une cause racine. `rayon` est plafonné à 64 par
    le mod ; au-delà, préférer plusieurs appels centrés sur les machines.
    """
    from services.factory_doctor import diagnose_zone
    from services import perception
    api = _api()
    diag = diagnose_zone(api, x, y, rayon,
                         rows_sup=perception.centrales(api) + perception.parc(api))
    return f"{diag.resume()}\n" + "\n".join(
        f"  {s.name}@({s.x:.0f},{s.y:.0f}) : {s.cause} — {s.detail}"
        for s in (diag.symptomes or [])[:20])


@outil(ecrit=False)
def ou_sont_les_ressources(ressource: str, portee_max: float = 200.0) -> str:
    """Où trouver une ressource : distance depuis le joueur, et nids alentour.

    `ressource` : « iron-ore », « copper-ore », « coal », « stone »… Sert à savoir si une
    extraction vaut le déplacement — et si elle se paiera d'une attaque.
    """
    from services import deplacement, gisements
    api = _api()
    trouves = gisements.enumerer(api, ressource, deplacement.position(api),
                                 portee_max=portee_max)
    if not trouves:
        return f"aucun gisement de {ressource} à moins de {portee_max:.0f} tuiles"
    # `sur` est un BOOLÉEN — « aucun nid assez près pour menacer ce qu'on y bâtira » —
    # et non une position. Le lire comme un couple donnait « bool object is not
    # subscriptable » : les vrais champs sont x, y, reserve, distance.
    return "\n".join(
        f"  ({g.x:.0f},{g.y:.0f}) à {g.distance:.0f} tuiles — réserve {g.reserve}, "
        f"{g.tuiles} tuile(s)"
        + (" — SÛR" if g.sur else
           f" — {g.nids} nid(s)" + (f", le plus proche à {g.nid_proche:.0f}"
                                    if g.nid_proche is not None else ""))
        for g in trouves[:8])


@outil(ecrit=False)
def ce_qu_il_faut_pour(item: str) -> str:
    """La chaîne complète d'un produit : tous les intermédiaires et les minerais à extraire.

    Le produit est un PARAMÈTRE — la chaîne est découverte dans le jeu, pas écrite
    d'avance. Attention : le nom d'une recette n'est pas celui de son produit.
    """
    from services import knowledge
    items, gisements_requis = knowledge.decouvrir_chaine(_api(), item)
    fondu = knowledge.fond_en(_api(), item)
    return (f"{item} : {len(items)} étage(s) — {', '.join(items)}\n"
            f"à extraire : {', '.join(gisements_requis) or 'aucun'}\n"
            f"se fond en : {fondu or 'RIEN (ce minerai ne se fond pas)'}")


@outil(ecrit=False)
def etat_de_la_recherche() -> str:
    """Technologies acquises, et les marches à portée avec leur coût en flacons."""
    from services import recherche
    arbre = recherche.lire(_api())
    return _rendu({"acquises": getattr(arbre, "acquises", None),
                   "marches": [str(m) for m in (arbre.marches or [])[:12]]})


@outil(ecrit=False)
def suivre_une_ligne(depart_x: float, depart_y: float,
                     cible_nom: str = "", cible_x: float = 0.0,
                     cible_y: float = 0.0) -> str:
    """Remonte une ligne de belts depuis un point jusqu'à sa rupture.

    Répond à « pourquoi ce four ne reçoit rien » quand le diagnostic dit seulement
    « entrée vide ».
    """
    from services.flux import suivre_flux
    cible = (cible_x, cible_y) if (cible_x or cible_y) else None
    return _rendu(suivre_flux(_api(), (depart_x, depart_y), cible_nom, cible))


@outil(ecrit=False)
def regarder(x: float, y: float, rayon: float = 4.0) -> str:
    """Ce qui est POSÉ à un endroit : nom, type, statut, orientation.

    La primitive de lecture la plus utile — elle dit ce qu'une machine contient et
    pourquoi elle est arrêtée. Lecture seule.
    """
    return _rendu(_api().inspect_at(x, y, rayon))


@outil(ecrit=False)
def ce_que_l_usine_a_produit(item: str) -> str:
    """Combien de `item` l'usine a réellement produit depuis le début.

    Distingue une chaîne qui tourne d'une chaîne qui en a l'air.
    """
    from services import perception
    return f"{item} produit(s) : {perception.production_cumulee(_api(), item)}"


# ---------------------------------------------------------------------------- AGIR

@outil
def batir_une_chaine(item: str, debit: float = 0.5,
                     alimentations_max: int = 0, budget_belts: int = 0,
                     repartir_de_zero: bool = False) -> str:
    """Bâtit une usine complète pour produire `item` : extraction, fonte, transport.

    Pour du VOLUME. Elle choisit son gisement au débit visé, marche jusqu'à lui, et forge
    ce qui lui manque avant de poser — plusieurs minutes, parfois plus de dix. Si tu tiens
    déjà une foreuse et que tu veux produire tout de suite, `extraire_ici` pose trois
    entités avec ce que tu as, en quelques secondes ; les deux se complètent.

    `repartir_de_zero` : ce que tu veux faire de ce qui est DÉJÀ en terre.

      - `False` (défaut) — on ÉTEND : rien n'est touché, la nouvelle chaîne se pose à
        côté. Ce qui existe et souffre te sera nommé dans le rapport, à toi d'en faire
        ce que tu veux.
      - `True` — on RASE : les foreuses, fours et assembleuses en place sont démontés et
        leurs pièces reviennent dans ta poche, AVANT que le plan ne soit calculé — elles
        serviront donc à le bâtir. C'est ce qu'il faut quand tu changes de palier :
        des machines à charbon sur un gisement que tu veux passer à l'électrique ne sont
        pas seulement inutiles, elles occupent la place et réclament du combustible.

    Le défaut est l'extension, parce que démonter ce qui produit ne se rattrape pas.

    `budget_belts` : combien de tuiles de convoyeur tu acceptes de faire FORGER pour
    amener le charbon jusqu'aux machines. Une tuile coûte 3 plaques de fer. Sans lui, la
    chaîne ne tire que ce que ton fer permet déjà — et si le gisement est plus loin, elle
    te le dit avec la distance, à toi de juger si la ligne vaut son prix.

    Le placement, l'orientation et les raccords sont calculés — ne demande jamais de
    position. Rend ce qui a été posé, ou ce qui a manqué.

    ELLE TOURNE EN FOND et te rend la main tout de suite : bâtir prend des minutes, et
    pendant qu'un outil travaille tu n'existes pas — c'est ainsi qu'on t'a laissé sourd
    quatorze minutes durant. Suis-la par `ou_en_est_le_chantier`, qui te donnera aussi ce
    qu'on te dit pendant ce temps, et coupe-la par `arreter_le_chantier` si tu juges que
    cela n'a plus de sens.
    """
    def _travail():
        # `budget_belts` DOIT EXISTER ICI. Le refus d'alimentation naît dans ce chantier
        # et conseille « rappelle avec `budget_belts` » — or le paramètre n'existait que
        # sur `reparer`. L'agent lisait un remède inapplicable, et l'usine restait sans
        # charbon automatique : mesuré partie 34, deux chantiers, zéro foreuse à charbon.
        coord = _coord()
        coord.budget_belts = int(budget_belts) if budget_belts else None
        ok, detail = coord.batir_chaine(
            item, debit,
            alimentations_max=(int(alimentations_max) if alimentations_max else None),
            repartir_de_zero=bool(repartir_de_zero))
        return f"{'OK' if ok else 'ÉCHEC'} — {detail}"
    return _lancer_chantier(f"batir_une_chaine({item})", _travail)


def _cible_apres(api, item: str, combien: int) -> int:
    """Le stock à ATTEINDRE pour que `combien` de plus soient obtenus.

    DEUX SENS POUR UN MÊME NOMBRE, ET L'UN DES DEUX BOUCLE. `Coordinator.fabriquer(item,
    n)` veut dire « fais que j'en aie n » — juste pour lui, qui approvisionne un plan
    connaissant ses totaux. L'outil exposé, lui, s'appelle `se_procurer`, et « procure-moi
    quinze charbons » n'a qu'un sens : quinze DE PLUS.

    Partie 38 : la centrale échoue sur « il t'en faut 50 en tout, tu en as 35 » ; l'agent
    pose la soustraction, demande quinze, et s'entend répondre « tu en as déjà assez en
    poche (35), rien à fabriquer ». Il relance la centrale, qui réclame les mêmes quinze.
    Rien ne signale l'anomalie : l'outil répond OK.

    C'est le mode d'échec qui tranche, pas la préférence. Lu en TOTAL, l'outil ne fait rien
    en annonçant une réussite — l'agent ne peut qu'y retourner. Lu en DELTA, le pire est
    d'en forger en trop. On traduit donc ICI, à la frontière ; le Coordinator ne bouge pas.
    """
    from services import perception
    try:
        deja = int(perception.inventory(api).get(item, 0) or 0)
    except Exception:
        deja = 0
    return deja + max(1, int(combien))


@outil
def se_procurer(item: str, combien: int = 1) -> str:
    """Obtient `combien` unités DE PLUS de `item` : miner, fondre, fabriquer — dans l'ordre.

    `combien` s'ajoute à ce que tu as : en demander 15 quand tu en as 35 t'en fait 50.

    Fonctionne les mains vides. Une recette verrouillée est signalée comme telle : c'est
    une recherche qui manque, pas une impossibilité.

    Miner et fondre prennent parfois des minutes — elle tourne donc EN FOND comme les
    autres constructions. Suis-la par `ou_en_est_le_chantier`.
    """
    def _travail():
        ok, detail = _coord().fabriquer(item, _cible_apres(_api(), item, combien))
        return f"{'OK' if ok else 'ÉCHEC'} — {detail}"
    return _lancer_chantier(f"se_procurer({item}x{combien})", _travail)


@outil
def batir_une_centrale() -> str:
    """Pose une centrale à vapeur au bord de l'eau et la relie à la zone de travail.

    Nécessaire avant toute machine électrique. Un générateur ne produit que ce qui est
    consommé : ne juge pas son succès sur les kW à vide.

    Elle tourne EN FOND — suis-la par `ou_en_est_le_chantier`.
    """
    from agents.coordinator import Decision
    def _travail():
        ok, detail = _coord().batir(Decision(action="batir_energie", raison="demandé"))
        return f"{'OK' if ok else 'ÉCHEC'} — {detail}"
    return _lancer_chantier("batir_une_centrale", _travail)


@outil
def chercher_une_technologie(nom: str) -> str:
    """Lance une recherche, en payant ses flacons — d'une chaîne ou à la main.

    La première recherche ne peut pas s'automatiser (l'assembleuse exige `automation`) :
    dans ce cas les flacons sont fabriqués et portés au laboratoire.

    Elle tourne EN FOND — suis-la par `ou_en_est_le_chantier`.
    """
    def _travail():
        ok, detail = _coord().chercher(nom)
        return f"{'OK' if ok else 'ÉCHEC'} — {detail}"
    return _lancer_chantier(f"chercher_une_technologie({nom})", _travail)


# Ce qui peut tomber en panne, par opposition aux organes de TRANSIT qui les longent.
# Même règle que `factory_doctor`, qui n'accuse jamais une belt ni un inserter d'être
# une cause : quand plusieurs entités se touchent, on répare la machine.
_TYPES_REPARABLES = ("mining-drill", "furnace", "assembling-machine", "boiler",
                     "generator", "lab", "pumpjack", "offshore-pump")


def _machine_a(api, x: float, y: float) -> str:
    """Le nom RÉEL de la machine à cette position, ou "" si l'on ne sait pas.

    L'agent désigne ce qu'il voit par sa POSITION ; il n'a pas à connaître le nom du
    prototype. Le jeu, lui, le sait — il suffit de le lui demander.
    """
    try:
        proches = ((api.inspect_at(x, y, 1.5) or {}).get("entities") or [])
    except Exception:
        return ""

    # LA PLUS PROCHE, PAS LA PREMIÈRE. `inspect_at` cherche par AIRE — le mod teste
    # l'intersection des bounding box, ce qui est la bonne question (« qu'y a-t-il À cet
    # endroit ? ») mais ramène aussi les voisines. Deux machines 2×2 distantes de deux
    # tuiles se retrouvent donc toutes deux dans le rayon, et l'ordre du jeu n'est pas
    # garanti.
    #
    # Partie 32, boucle de plusieurs minutes : `reparer(x=-6, y=-86)` répondait
    # « ravitaillement de stone-furnace@(-6.0,-86.0) (n°0) » alors qu'à cet endroit se
    # trouve la FOREUSE — le four est deux tuiles plus loin. `move_items_at` cherchait donc
    # un four là où il n'y en a pas : zéro item versé, indéfiniment, avec 78 charbons en
    # poche et la machine à moins de trois tuiles.
    #
    # Désigner par position n'a de sens que si l'on répond CE QUI EST à cette position.
    def _loin(e):
        try:
            return abs(float(e.get("x", 0.0)) - x) + abs(float(e.get("y", 0.0)) - y)
        except Exception:
            return 1e9

    reparables = [e for e in proches if str(e.get("type", "")) in _TYPES_REPARABLES]
    if reparables:
        return str(min(reparables, key=_loin).get("name") or "")
    return str(min(proches, key=_loin).get("name") or "") if proches else ""


@outil
def reparer(quoi: str, x: float, y: float, nom_machine: str = "",
            budget_belts: int = 0) -> str:
    """Répare une machine nommée par le diagnostic.

    `quoi` : « ravitailler » (combustible), « evacuer » (sortie pleine), « relier »
    (courant), « approvisionner » (bâtir sa desserte), « batir_evacuation » (ramassage
    permanent). L'agent s'approche avant d'agir — le jeu refuse au-delà de dix tuiles.

    `budget_belts` : combien de convoyeurs tu acceptes de faire FORGER pour une
    « approvisionner ». Une belt coûte trois plaques de fer la tuile. Sans budget, on
    reste prudent et l'outil te dit la distance manquante ; c'est à TOI de juger si
    relier un gisement lointain vaut son prix — l'écart entre gisements dépend de la
    carte, et quand elle les éloigne, une longue ligne est la seule option.

    `nom_machine` est FACULTATIF : sans lui, on lit sur place ce qui s'y trouve. Le
    remplacer par le mot « machine » — ce que faisait cet outil — envoyait
    `move_items_at` chercher une entité de ce nom, qui n'existe dans aucun prototype :
    le versement échouait toujours, avec du combustible en poche et la machine à portée
    (partie 11, foreuse en `no_fuel` sur 495 minerais).
    """
    from agents.coordinator import Decision
    from services.factory_doctor import Symptome
    cible = Symptome(name=nom_machine or _machine_a(_api(), x, y) or "machine",
                     x=x, y=y, cause="demandé", gravite=1, detail="")
    coord = _coord()
    if quoi == "approvisionner" and budget_belts > 0:
        ok, detail = coord.approvisionner(cible, coord.combustible,
                                          budget_belts=int(budget_belts))
    else:
        ok, detail = coord.agir(Decision(action=quoi, raison="demandé", cible=cible))
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@outil
def se_deplacer(x: float, y: float) -> str:
    """Marche jusqu'à un point, en générant le terrain et en contournant les obstacles.

    Utile pour aller voir, ou pour se mettre à portée. Les actions qui en ont besoin
    s'approchent déjà d'elles-mêmes.
    """
    from services import deplacement
    ax, ay = deplacement.marcher_vers(_api(), x, y)
    return f"arrivé en ({ax:.0f},{ay:.0f}) — visé ({x:.0f},{y:.0f})"


def main() -> None:
    mcp.settings.host, mcp.settings.port = HOTE, PORT
    print(f"[mcp_jeu] les mains du joueur sur http://{HOTE}:{PORT}/mcp", flush=True)
    print(f"[mcp_jeu] RCON {RCON_HOTE}:{RCON_PORT}", flush=True)
    # LE VEILLEUR TOURNE À CÔTÉ. Il ne sert que si quelqu'un regarde jouer : quand un
    # message attend et que l'agent est au milieu d'une action longue, il dit dans le jeu
    # que c'est bien arrivé et ce qui occupe l'agent. Daemon — il ne doit jamais retenir
    # l'arrêt du serveur.
    threading.Thread(target=_veiller, daemon=True, name="veilleur-chat").start()
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
