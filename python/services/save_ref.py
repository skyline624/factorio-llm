"""Sauvegarder et restaurer l'état du serveur — le préalable à toute comparaison.

Deux parties ne se comparent que si elles partent du même endroit. Or chaque test laisse
la carte dans l'état où il l'a mise : mesuré en partie longue, l'usine héritée du test
précédent faisait varier le résultat d'un run à l'autre sans qu'aucun code n'ait changé.
On mesurait du bruit et on l'aurait pris pour un effet du modèle.

La carte ne suffit d'ailleurs pas : il faut le même inventaire, la même position, le même
bâti. C'est donc la SAVE entière qu'on met de côté et qu'on remet en place.

Restaurer impose d'arrêter le serveur — Factorio garde la partie en mémoire et la
réécrirait par-dessus. Le cycle est donc : arrêt, copie, relance, attente du RCON. C'est
lent (une trentaine de secondes) et c'est le prix d'une mesure honnête.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
from typing import Optional

RACINE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SAVE = os.path.join(RACINE, "saves", "fl-dev.zip")
REFERENCE = os.path.join(RACINE, "saves", "fl-reference.zip")
LANCEUR = os.path.join(RACINE, "scripts", "start_factorio_dedicated.bat")


def _port_ouvert(hote: str = "127.0.0.1", port: int = 27015,
                 delai: float = 1.0) -> bool:
    """Le port écoute-t-il — en une seconde, sans retenter.

    `get_rcon` réessaie en interne, ce qui est bon pour un appel métier et désastreux
    pour une sonde : une attente de démarrage qui interroge toutes les trois secondes
    devenait plus lente que le démarrage lui-même. On sépare donc « le port est là » de
    « le mod répond ».
    """
    import socket
    try:
        with socket.create_connection((hote, port), timeout=delai):
            return True
    except OSError:
        return False


def _rcon_repond(hote: str = "127.0.0.1", port: int = 27015,
                 mdp: str = "factoriollm") -> bool:
    """Le mod répond-il vraiment. Le port peut écouter avant que le jeu ne soit prêt."""
    if not _port_ouvert(hote, port):
        return False
    # Un client NEUF a delai court, jamais le singleton : celui-ci garde la connexion
    # d'un serveur qui n'existe plus et fait payer sa reconnexion a chaque sonde.
    try:
        from core.rcon import RconClient
        c = RconClient(hote, port, mdp, timeout=2.0)
        c.query_lua("rcon.print('ok')")
        c.close()
        return True
    except Exception:
        return False


def _fin_du_log(n: int = 12) -> str:
    """Les dernières lignes du log serveur.

    C'est le seul endroit qui dise POURQUOI le serveur n'est pas revenu — save écrite par
    une autre version, mod absent, port encore occupé. Sans cela, l'échec se résume à
    « il n'est pas revenu », ce qui n'oriente vers rien.
    """
    try:
        with open(os.path.join(RACINE, "logs", "server.log"), "r",
                  encoding="utf-8", errors="replace") as f:
            lignes = f.read().splitlines()
    except OSError:
        return ""
    return " | ".join(x.strip() for x in lignes[-n:] if x.strip())[:400]


def _attendre_ecriture(save: str, mtime0: Optional[float],
                       delai: float = 30.0) -> bool:
    """Attend que la save soit RÉELLEMENT réécrite, au lieu de dormir un temps choisi.

    `game.server_save()` rend la main avant que le fichier ne soit posé. Une pause fixe
    est un pari des deux côtés : trop courte, on fige l'état d'AVANT et l'on croit tenir
    celui d'après ; trop longue, on paie l'attente à chaque appel. On observe donc le
    fichier : sa date change, puis sa taille cesse de bouger — une copie faite pendant
    l'écriture donnerait une archive tronquée.
    """
    fin = time.time() + delai
    taille = -1
    while time.time() < fin:
        try:
            st = os.stat(save)
        except OSError:
            time.sleep(0.5)
            continue
        if mtime0 is None or st.st_mtime > mtime0:
            if st.st_size == taille and st.st_size > 0:
                return True
            taille = st.st_size
        time.sleep(0.5)
    return False


def arreter_serveur(delai: float = 15.0) -> bool:
    """Arrête Factorio. Sans cela la save en mémoire écraserait celle qu'on restaure.

    NB : tue TOUT `factorio.exe`, donc aussi un client d'observation lancé à côté. C'est
    volontaire — un client garde la save verrouillée et la restauration échouerait sans
    dire pourquoi.
    """
    try:
        subprocess.run(["taskkill", "/IM", "factorio.exe", "/F"],
                       capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return False
    fin = time.time() + delai
    while time.time() < fin:
        if not _port_ouvert():
            return True
        time.sleep(1.0)
    return not _port_ouvert()


def demarrer_serveur(delai: float = 180.0) -> bool:
    """Relance le serveur et attend que RCON réponde — pas seulement que le processus existe."""
    # `start` réclame un TITRE en premier argument dès que le reste est entre guillemets.
    # Passé en liste, `start factorio-srv /MIN cmd /c ...` fait prendre « factorio-srv »
    # pour le programme à lancer : Windows répond « ne trouve pas 'factorio-srv' » et
    # rien ne démarre. Le titre vide `""` est la forme qui marche.
    #
    # Les trois flux vont au NÉANT, et ce n'est pas de la propreté : le serveur hérite
    # sinon de la sortie de l'appelant et la tient ouverte tant qu'il vit. Mesuré --
    # `python verify_save_ref.py | tail` ne rendait jamais la main, alors que la
    # restauration s'était déroulée entière ; c'est `tail` qui attendait la fermeture
    # d'un tuyau tenu par Factorio. Tout appelant qui capture sa sortie (journal, CI,
    # runner) tomberait dans le même piège.
    try:
        subprocess.Popen(f'start "" /MIN cmd /c "{LANCEUR}"', cwd=RACINE, shell=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL,
                         # Groupe de processus à part : un Ctrl+C dans le protocole de
                         # mesure ne doit pas emporter le serveur qu'il vient de relancer.
                         creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
    except (OSError, subprocess.SubprocessError):
        return False
    fin = time.time() + delai
    while time.time() < fin:
        if _rcon_repond():
            # Le singleton pointe encore sur l'ancien serveur : le laisser en place
            # ferait echouer le premier appel de tout appelant.
            from core.rcon import reset_rcon
            reset_rcon()
            return True
        time.sleep(3.0)
    return False


def sauver_reference(rcon=None, chemin: str = REFERENCE, save: str = SAVE,
                     delai_ecriture: float = 30.0) -> tuple[bool, str]:
    """Fige l'état courant comme référence des comparaisons à venir.

    On demande d'abord au jeu d'ÉCRIRE sa partie : le fichier sur disque date du dernier
    autosave, et copier sans cela figerait un état antérieur à celui qu'on croit tenir.

    Une référence n'est remplacée qu'en cas de SUCCÈS. Écraser la précédente par une
    archive absente ou tronquée coûterait plus cher que de ne rien figer : on restaurerait
    ensuite un état qui n'a jamais existé, sans que rien ne le signale.
    """
    if rcon is not None:
        try:
            mtime0 = os.stat(save).st_mtime
        except OSError:
            mtime0 = None
        try:
            rcon.query_lua("game.server_save() rcon.print('sauve')")
        except Exception as e:
            return False, f"le jeu n'a pas pu écrire sa partie : {e}"
        if not _attendre_ecriture(save, mtime0, delai_ecriture):
            return False, "la save n'a pas été réécrite — référence non figée"
    if not os.path.exists(save):
        return False, f"aucune save à {save}"
    try:
        shutil.copy2(save, chemin)
    except OSError as e:
        return False, f"copie impossible : {e}"
    taille = os.path.getsize(chemin) / 1_000_000.0
    return True, f"référence figée ({taille:.1f} Mo) dans {os.path.basename(chemin)}"


def restaurer_reference(chemin: str = REFERENCE,
                        save: str = SAVE) -> tuple[bool, str]:
    """Remet le serveur dans l'état de la référence. Arrête, copie, relance, attend RCON.

    Rend (False, motif) plutôt que de lever : un protocole de mesure qui plante au
    milieu laisse le serveur dans un état pire que celui qu'il voulait corriger. Pour la
    même raison, le serveur est relancé même quand la copie échoue — l'abandonner éteint
    serait le seul dénouement dont on ne se relève pas tout seul.
    """
    if not os.path.exists(chemin):
        return False, f"aucune référence à {chemin} — appeler sauver_reference d'abord"
    if not arreter_serveur():
        return False, "le serveur n'a pas pu être arrêté"
    try:
        shutil.copy2(chemin, save)
    except OSError as e:
        demarrer_serveur()
        return False, f"restauration impossible : {e}"
    if not demarrer_serveur():
        return False, f"save restaurée mais le serveur n'est pas revenu — {_fin_du_log()}"
    return True, "état restauré depuis la référence"


def empreinte(rcon) -> Optional[str]:
    """Une signature courte de l'état, pour VÉRIFIER qu'une restauration a bien eu lieu.

    Le tick seul ne suffit pas — il repart de la valeur sauvegardée, ce qui est justement
    ce qu'on veut constater, mais deux états différents peuvent partager un tick. On y
    joint donc le bâti et l'inventaire.

    Il n'y a PAS toujours de `game.players[1]` : en mode test headless, l'avatar est un
    `character` sans joueur. Ne lire que le joueur rendait `items=0` sur un inventaire de
    21 lots — l'empreinte affirmait alors qu'un inventaire restauré était identique sans
    jamais l'avoir regardé. On retombe donc sur le character.
    """
    try:
        return str(rcon.query_lua(
            "local s = game.surfaces[1] "
            "local n = #s.find_entities_filtered{force='player'} "
            "local p = game.players[1] local i = 0 "
            "if p and p.character then i = p.get_main_inventory().get_item_count() "
            "else for _, c in pairs(s.find_entities_filtered{name='character'}) do "
            "  local inv = c.get_inventory(defines.inventory.character_main) "
            "  if inv then i = i + inv.get_item_count() end end end "
            "rcon.print(string.format('tick=%d entites=%d items=%d', game.tick, n, i))"
        )).strip()
    except Exception:
        return None


def _cli(argv: list[str]) -> int:
    """Figer et restaurer se font AUSSI à la main, avant de lancer une série de mesures.

    Sans cela, chaque protocole devrait réécrire les trois lignes de câblage RCON, et la
    référence finirait figée depuis un script d'essai plutôt que depuis l'état voulu.

    Usage :
        python -m services.save_ref figer|restaurer|empreinte
    """
    action = argv[1] if len(argv) > 1 else "empreinte"
    if action == "restaurer":
        t0 = time.time()
        ok, motif = restaurer_reference()
        print(f"[{'OK  ' if ok else 'FAIL'}] {motif} ({time.time() - t0:.0f}s)")
        return 0 if ok else 1

    try:
        from core.rcon import get_rcon
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0
    if action == "figer":
        ok, motif = sauver_reference(rcon=rcon)
        print(f"[{'OK  ' if ok else 'FAIL'}] {motif}")
        print(f"       état figé : {empreinte(rcon)}")
        return 0 if ok else 1
    print(f"       {empreinte(rcon)}")
    print(f"       référence : "
          f"{'présente' if os.path.exists(REFERENCE) else 'ABSENTE'} ({REFERENCE})")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(_cli(sys.argv))