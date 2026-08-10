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
        if outil_en_cours:
            api.say(f"bien reçu — occupé par {outil_en_cours} depuis "
                    f"{int(depuis_s // 60)} min {int(depuis_s % 60)} s, "
                    f"je le lis dès que j'ai la main")
        else:
            api.say("bien reçu — je le lis au prochain geste")
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


def _demander_l_arret() -> bool:
    """Pose le drapeau d'arrêt. La pose en cours se termine, la suivante ne commence pas."""
    _CHANTIER["arret"] = True
    return _chantier_tourne()


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
    return ("LE JOUEUR TE PARLE — tiens-en compte avant de poursuivre :\n"
            f"{dits}\n\n{resultat}")


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
        _fin(fn.__name__, r, time.time() - t0)
        return _bandeau_du_joueur(r)

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
    deplacement.marcher_vers(api, x, y)
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
    """Arrête proprement le travail en cours — entre deux entités, jamais au milieu d'une.

    Ce qui est posé RESTE posé, et relancer la même construction reprend où elle s'était
    arrêtée. À utiliser quand ce que tu viens d'apprendre rend le chantier inutile ou
    nuisible — pas par principe : un chantier qui va au bout coûte moins qu'un chantier
    relancé trois fois.
    """
    tournait = _demander_l_arret()
    if not tournait:
        return "aucun chantier en cours — rien à arrêter"
    return ("arrêt demandé : la pose en cours se termine, la suivante ne commencera pas. "
            "Suis la fin par `ou_en_est_le_chantier`.")


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
                     alimentations_max: int = 0) -> str:
    """Bâtit de quoi produire `item` : extraction, fonte, transport, raccordement.

    LA capacité principale. Le placement, l'orientation et les raccords sont calculés —
    ne demande jamais de position. Rend ce qui a été posé, ou ce qui a manqué.

    ELLE TOURNE EN FOND et te rend la main tout de suite : bâtir prend des minutes, et
    pendant qu'un outil travaille tu n'existes pas — c'est ainsi qu'on t'a laissé sourd
    quatorze minutes durant. Suis-la par `ou_en_est_le_chantier`, qui te donnera aussi ce
    qu'on te dit pendant ce temps, et coupe-la par `arreter_le_chantier` si tu juges que
    cela n'a plus de sens.
    """
    def _travail():
        ok, detail = _coord().batir_chaine(
            item, debit,
            alimentations_max=(int(alimentations_max) if alimentations_max else None))
        return f"{'OK' if ok else 'ÉCHEC'} — {detail}"
    return _lancer_chantier(f"batir_une_chaine({item})", _travail)


@outil
def se_procurer(item: str, combien: int = 1) -> str:
    """Obtient `item` par tous les moyens : miner, fondre, fabriquer — dans cet ordre.

    Fonctionne les mains vides. Une recette verrouillée est signalée comme telle : c'est
    une recherche qui manque, pas une impossibilité.

    Miner et fondre prennent parfois des minutes — elle tourne donc EN FOND comme les
    autres constructions. Suis-la par `ou_en_est_le_chantier`.
    """
    def _travail():
        ok, detail = _coord().fabriquer(item, combien)
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
    for e in proches:
        if str(e.get("type", "")) in _TYPES_REPARABLES:
            return str(e.get("name") or "")
    return str(proches[0].get("name") or "") if proches else ""


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
