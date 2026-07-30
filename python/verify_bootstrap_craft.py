"""Vérification en jeu : LES MAINS VIDES, l'agent fabrique ce qu'il pose.

Toutes les vérifications précédentes partaient d'un inventaire garni — vingt et un
lots posés par `preparer_reference.py`, plus le kit de `reset_character` qui contient
jusqu'à une raffinerie. Tant que ce stock existe, « autonome » est un mot creux :
l'agent consomme ce qu'un humain lui a donné, et s'arrête quand c'est épuisé.

Ici l'inventaire est VIDE. Pour poser sa première foreuse, l'agent doit miner la
pierre, fondre le fer, forger les engrenages et assembler la machine — puis aller
chercher son combustible, qui n'est pas sous ses pieds.

Ce qui est éprouvé, dans l'ordre où cela se produit :

  1. le départ est réellement vide (empreinte de la référence, `items=0`) ;
  2. l'agent fabrique ses trois machines burner sans rien recevoir ;
  3. il va chercher le charbon LOIN — 215 tuiles mesurées sur cette carte, hors de
     l'horizon généré, ce qui exige de générer devant soi et de marcher par bonds ;
  4. il pose sa chaîne et produit.

Les recettes ÉLECTRIQUES (poteau, inserter, foreuse électrique) sont `enabled=false`
sur une carte neuve : elles demandent une recherche. Le palier atteignable sans
laboratoire est donc le tout-burner, et c'est celui qu'on mesure. Exiger ici une
centrale ou un poteau serait mesurer l'absence d'un chantier qui n'a pas commencé.

Lancement (serveur Factorio + mod requis) :
    cd python
    python verify_bootstrap_craft.py
"""

from __future__ import annotations

import subprocess
import sys

from agents.coordinator import Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon, reset_rcon
from services import deplacement, save_ref

# Les trois machines de la première chaîne. Aucune n'est électrique : sur une carte
# neuve, c'est tout ce que les recettes ouvertes permettent d'assembler.
CHAINE = ("burner-mining-drill", "stone-furnace", "burner-inserter")
TOURS = 12

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:110]}", flush=True)


def _inv(api: ModApi) -> dict[str, int]:
    return dict(api.get_state().get("inventory") or {})


def main() -> int:
    # --- La référence SANS DOTATION est le juge de paix : on la refait ici plutôt
    # que de supposer qu'elle traîne, sinon un run hérite d'une carte garnie et
    # « réussit » sans rien fabriquer.
    print("[prep] fabrication de la référence sans dotation...", flush=True)
    p = subprocess.run([sys.executable, "preparer_reference.py", "--sans-dotation"],
                       capture_output=True, text=True, timeout=600)
    if "[OK  ]" not in p.stdout:
        print("[SKIP] référence non fabriquée (serveur injoignable ?)")
        print(p.stdout[-400:])
        return 0
    reset_rcon()

    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    rcon.query_lua("game.speed = 20 rcon.print(1)")

    # --- Rec 1 : le départ est VRAIMENT vide ---
    depart = _inv(api)
    total = sum(depart.values())
    rec("1: l'inventaire de départ est vide", total == 0,
        f"{total} objet(s) en poche | empreinte {save_ref.empreinte(rcon)}")

    zone = deplacement.position(api)
    coord = Coordinator(api, zone=zone, rayon=25.0)

    # --- Rec 2 : les trois machines sortent de rien ---
    fabriquees, journal, loin = [], [], 0.0
    for tour in range(TOURS):
        d, agi, etat = coord.tick()      # l'état est RELU après l'action
        inv = _inv(api)
        journal.append((tour + 1, d.action, agi, dict(inv)))
        print(f"  t{tour + 1:<2} {d.action:20s} agi={agi} "
              f"foreuses={inv.get('burner-mining-drill', 0)} "
              f"charbon={inv.get('coal', 0)}", flush=True)
        # Le MOTIF, pas seulement le verdict : un `agi=False` sans motif envoie
        # chercher la panne au mauvais endroit — c'est ce qui est arrivé ici, où
        # l'action échouée n'était pas celle qu'on croyait.
        if coord.journal:
            print(f"        {coord.journal[-1][:150]}", flush=True)
        # Les machines POSÉES sortent de l'inventaire : on retient donc ce qu'on a
        # tenu en main à un moment, sinon poser la chaîne effacerait la preuve
        # qu'on l'avait fabriquée.
        for m in CHAINE:
            if inv.get(m, 0) > 0 and m not in fabriquees:
                fabriquees.append(m)
        # Le POINT LE PLUS LOIN atteint, et non la position finale : l'agent revient
        # bâtir sur son gisement, si bien que mesurer à la fin ferait disparaître le
        # voyage de 215 tuiles qui lui a valu son combustible.
        loin = max(loin, deplacement.distance(api, zone[0], zone[1]))

    inv = _inv(api)
    obtenues = [m for m in CHAINE if inv.get(m, 0) > 0 or m in fabriquees]
    rec("2: les 3 machines de la chaîne sont fabriquées", len(obtenues) == len(CHAINE),
        f"{len(obtenues)}/3 : {', '.join(obtenues) or 'aucune'}")

    # --- Rec 3 : le combustible a été cherché LOIN ---
    # Sur cette carte, zéro tuile de charbon à moins de 60 tuiles du fer, et la plus
    # proche à 215. `walk_to_entity` seul ne l'atteint pas : le pathfinding ne planifie
    # pas à travers des chunks non générés, le personnage s'arrête bien avant, et le
    # minage échoue sur « cible hors portée ». Il faut générer devant soi et marcher
    # par bonds (services.deplacement).
    rec("3: le combustible a été cherché hors de l'horizon", loin > 100.0,
        f"l'agent s'est éloigné jusqu'à {loin:.0f} tuiles de son gisement pour son charbon")

    # --- Rec 3b : la chaîne est POSÉE sur le terrain ---
    posees = int(str(rcon.query_lua(
        "local s = game.surfaces[1] local n = 0 "
        "for _, e in pairs(s.find_entities_filtered{name='burner-mining-drill', "
        "force='player'}) do n = n + 1 end rcon.print(n)")).strip() or 0)
    rec("3b: la chaîne fabriquée est posée sur le terrain", posees > 0,
        f"{posees} foreuse(s) burner en service, bâtie(s) à partir de rien")

    # --- Rec 4 : rien ne vient d'une dotation ---
    # Tout ce qui est en poche a été miné, fondu ou assemblé : l'inventaire de départ
    # était vide, donc l'égalité est structurelle — on la vérifie tout de même, car
    # un `reset_character` glissé dans un chemin d'erreur la casserait en silence.
    rec("4: rien n'a été reçu, tout a été produit", total == 0 and sum(inv.values()) > 0,
        f"départ {total} objet(s) -> arrivée {sum(inv.values())} objet(s)")

    # --- Rec 5 : la boucle a bien AGI, et pas tourné à vide ---
    agis = [j for j in journal if j[2]]
    rec("5: la boucle agit au lieu de tourner à vide", len(agis) >= 3,
        f"{len(agis)}/{len(journal)} tour(s) avec action réussie : "
        f"{', '.join(sorted({j[1] for j in agis}))}")

    # --- Rec 6 : le cycle complet s'enchaîne tout seul ---
    # Fabriquer PUIS bâtir, dans cet ordre et sans intervention : c'est ce qui sépare
    # un agent qui consomme une dotation d'un agent qui part de rien.
    batis = [j for j in journal if j[1] == "batir_production" and j[2]]
    rec("6: fabriquer puis bâtir s'enchaînent sans dotation", len(batis) >= 1,
        f"{len(batis)} chaîne(s) bâtie(s) au(x) tour(s) {[j[0] for j in batis]} "
        f"après {len([j for j in journal if j[1] == 'fabriquer' and j[2]])} fabrication(s)")

    rcon.query_lua("game.speed = 1 rcon.print(1)")
    rcon.close()

    # On REND la référence garnie. Ce script est le seul à figer une carte sans dotation ;
    # la laisser en place ferait échouer tout ce qui tourne après lui pour une raison
    # invisible — des poches vides là où l'on croyait avoir un stock. Un script qui change
    # l'état commun le remet comme il l'a trouvé.
    print("[prep] restitution de la référence garnie...", flush=True)
    subprocess.run([sys.executable, "preparer_reference.py"],
                   capture_output=True, text=True, timeout=600)
    reset_rcon()

    print("\n" + "=" * 74)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 74)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
