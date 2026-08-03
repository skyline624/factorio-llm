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


# ------------------------------------------------------------------------- OBSERVER

@mcp.tool()
def etat_du_jeu() -> str:
    """L'état de l'usine : machines, énergie, diagnostic, inventaire, recherche visée.

    À appeler EN PREMIER, et après chaque action : c'est l'état relu qui dit si l'action
    a servi, jamais le fait qu'elle ait été prise.
    """
    from services.arbitre import resumer_etat
    return resumer_etat(_coord().observer())


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def etat_de_la_recherche() -> str:
    """Technologies acquises, et les marches à portée avec leur coût en flacons."""
    from services import recherche
    arbre = recherche.lire(_api())
    return _rendu({"acquises": getattr(arbre, "acquises", None),
                   "marches": [str(m) for m in (arbre.marches or [])[:12]]})


@mcp.tool()
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


@mcp.tool()
def regarder(x: float, y: float, rayon: float = 4.0) -> str:
    """Ce qui est POSÉ à un endroit : nom, type, statut, orientation.

    La primitive de lecture la plus utile — elle dit ce qu'une machine contient et
    pourquoi elle est arrêtée. Lecture seule.
    """
    return _rendu(_api().inspect_at(x, y, rayon))


@mcp.tool()
def ce_que_l_usine_a_produit(item: str) -> str:
    """Combien de `item` l'usine a réellement produit depuis le début.

    Distingue une chaîne qui tourne d'une chaîne qui en a l'air.
    """
    from services import perception
    return f"{item} produit(s) : {perception.production_cumulee(_api(), item)}"


# ---------------------------------------------------------------------------- AGIR

@mcp.tool()
def batir_une_chaine(item: str, debit: float = 0.5) -> str:
    """Bâtit de quoi produire `item` : extraction, fonte, transport, raccordement.

    LA capacité principale. Le placement, l'orientation et les raccords sont calculés —
    ne demande jamais de position. Rend ce qui a été posé, ou ce qui a manqué.
    """
    ok, detail = _coord().batir_chaine(item, debit)
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@mcp.tool()
def se_procurer(item: str, combien: int = 1) -> str:
    """Obtient `item` par tous les moyens : miner, fondre, fabriquer — dans cet ordre.

    Fonctionne les mains vides. Une recette verrouillée est signalée comme telle : c'est
    une recherche qui manque, pas une impossibilité.
    """
    ok, detail = _coord().fabriquer(item, combien)
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@mcp.tool()
def batir_une_centrale() -> str:
    """Pose une centrale à vapeur au bord de l'eau et la relie à la zone de travail.

    Nécessaire avant toute machine électrique. Un générateur ne produit que ce qui est
    consommé : ne juge pas son succès sur les kW à vide.
    """
    from agents.coordinator import Decision
    ok, detail = _coord().batir(Decision(action="batir_energie", raison="demandé"))
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@mcp.tool()
def chercher_une_technologie(nom: str) -> str:
    """Lance une recherche, en payant ses flacons — d'une chaîne ou à la main.

    La première recherche ne peut pas s'automatiser (l'assembleuse exige `automation`) :
    dans ce cas les flacons sont fabriqués et portés au laboratoire.
    """
    ok, detail = _coord().chercher(nom)
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@mcp.tool()
def reparer(quoi: str, x: float, y: float, nom_machine: str = "") -> str:
    """Répare une machine nommée par le diagnostic.

    `quoi` : « ravitailler » (combustible), « evacuer » (sortie pleine), « relier »
    (courant), « approvisionner » (bâtir sa desserte), « batir_evacuation » (ramassage
    permanent). L'agent s'approche avant d'agir — le jeu refuse au-delà de dix tuiles.
    """
    from agents.coordinator import Decision
    from services.factory_doctor import Symptome
    cible = Symptome(name=nom_machine or "machine", x=x, y=y,
                     cause="demandé", gravite=1, detail="")
    ok, detail = _coord().agir(Decision(action=quoi, raison="demandé", cible=cible))
    return f"{'OK' if ok else 'ÉCHEC'} — {detail}"


@mcp.tool()
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
