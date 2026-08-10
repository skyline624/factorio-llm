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
        with _VERROU_JEU:
            return fn(*a, **kw)

    @functools.wraps(fn)
    async def enveloppe(*a, **kw):
        args = kw or dict(enumerate(a))
        _debut(fn.__name__, args)
        t0 = time.time()
        try:
            r = await anyio.to_thread.run_sync(functools.partial(_travail, *a, **kw))
        except Exception as e:
            _fin(fn.__name__, f"ERREUR {type(e).__name__}: {e}", time.time() - t0)
            raise
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
    """
    ok, detail = _coord().batir_chaine(
        item, debit,
        alimentations_max=(int(alimentations_max) if alimentations_max else None))
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@outil
def se_procurer(item: str, combien: int = 1) -> str:
    """Obtient `item` par tous les moyens : miner, fondre, fabriquer — dans cet ordre.

    Fonctionne les mains vides. Une recette verrouillée est signalée comme telle : c'est
    une recherche qui manque, pas une impossibilité.
    """
    ok, detail = _coord().fabriquer(item, combien)
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@outil
def batir_une_centrale() -> str:
    """Pose une centrale à vapeur au bord de l'eau et la relie à la zone de travail.

    Nécessaire avant toute machine électrique. Un générateur ne produit que ce qui est
    consommé : ne juge pas son succès sur les kW à vide.
    """
    from agents.coordinator import Decision
    ok, detail = _coord().batir(Decision(action="batir_energie", raison="demandé"))
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@outil
def chercher_une_technologie(nom: str) -> str:
    """Lance une recherche, en payant ses flacons — d'une chaîne ou à la main.

    La première recherche ne peut pas s'automatiser (l'assembleuse exige `automation`) :
    dans ce cas les flacons sont fabriqués et portés au laboratoire.
    """
    ok, detail = _coord().chercher(nom)
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


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
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
