"""Carte NEUVE : on archive, on supprime, le lanceur recrée.

Le serveur charge `saves/fl-dev.zip` ; si le fichier manque,
`start_factorio_dedicated.bat` en crée une avec `--create` puis démarre dessus. Le reset
se résume donc à supprimer la save — mais pas avant de l'avoir mise de côté.

`fl-reference.zip` est la référence figée dont dépend TOUTE la batterie de vérification :
on n'y touche pas. Conséquence à connaître : le premier `restaurer_reference()` venu
écrasera la carte neuve par l'ancienne. Tant qu'une partie tourne, aucun `verify_*` ne
doit être lancé.

CE FICHIER A VÉCU DANS UN RÉPERTOIRE TEMPORAIRE et a disparu avec lui, après une
vingtaine de parties. Un outil qu'on utilise à chaque manche appartient au dépôt : c'est
la même raison qui fait vivre `hermes/` ici plutôt que dans un conteneur.

Usage :
    cd python
    python reset_map.py
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time

from services.save_ref import (LANCEUR, RACINE, REFERENCE, SAVE, arreter_serveur,
                              _rcon_repond)

ARCHIVES = os.path.join(RACINE, "saves", "archives")


def main() -> int:
    horodate = time.strftime("%Y%m%d-%H%M%S")
    os.makedirs(ARCHIVES, exist_ok=True)

    # ARRÊTER D'ABORD, SUPPRIMER ENSUITE. Sans cela le serveur tourne toujours : il
    # répond au RCON dans la seconde, on croit la carte neuve prête, et l'on mesure en
    # réalité l'ANCIENNE partie — tick de trois millions et save de zéro octet, faute
    # d'avoir été réécrite. L'oubli s'est vu à la première reconstitution du script.
    print("  arret du serveur en cours...")
    arreter_serveur()

    print("AVANT :")
    for p in (SAVE, REFERENCE):
        print(f"  {os.path.basename(p):<22} "
              + (f"{os.path.getsize(p)} octets" if os.path.exists(p) else "ABSENT"))
        if os.path.exists(p):
            dest = os.path.join(
                ARCHIVES, f"{os.path.splitext(os.path.basename(p))[0]}-{horodate}.zip")
            shutil.copy2(p, dest)
            print(f"  archive -> {os.path.relpath(dest, RACINE)}")

    if os.path.exists(SAVE):
        os.remove(SAVE)
        print(f"\n  supprime : {os.path.relpath(SAVE, RACINE)} — le lanceur va recreer")

    print("\n  creation de la carte + demarrage (peut prendre 2-3 min)...")
    # DÉTACHÉ, et sans tenir le stdout de l'appelant : le lanceur reste au premier plan
    # tant que le serveur tourne. Hérité de `save_ref`, où l'oublier bloquait le script
    # appelant jusqu'à la fin de la partie.
    subprocess.Popen([LANCEUR], cwd=os.path.join(RACINE, "scripts"),
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     creationflags=getattr(subprocess, "DETACHED_PROCESS", 0))

    debut = time.time()
    while time.time() - debut < 240:
        if _rcon_repond():
            print(f"  RCON repond apres {time.time() - debut:.0f}s")
            break
        time.sleep(3)
    else:
        print("  RCON ne repond pas apres 240s — verifier le lanceur")
        return 1

    from core.mod_api import ModApi
    from core.rcon import get_rcon
    api = ModApi(get_rcon())
    etat = api.get_state() or {}
    from services import perception
    print(f"\nCARTE NEUVE : tick={etat.get('tick')} "
          f"machines={len(perception.parc(api))} "
          f"joueurs={'oui' if etat.get('character') else 'non'}")
    print(f"  taille save : {os.path.getsize(SAVE) if os.path.exists(SAVE) else 0} octets")
    print(f"  reference PRESERVEE : {os.path.basename(REFERENCE)}")
    print("\n  ATTENTION : tout verify_* restaurera l'ANCIENNE carte par-dessus.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
