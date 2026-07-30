"""Vérification en jeu : l'agent débloque ce qu'il ne savait pas encore faire.

Jusqu'ici, une recette fermée arrêtait tout. Le planificateur disait « la recette est
VERROUILLÉE et il faut d'abord la rechercher » — vrai, et sans issue : l'agent nommait
son mur sans jamais tenter de le franchir. Tout un pan du jeu lui était inaccessible,
et le palier tout-burner passait pour une limite de conception.

Ce qui est éprouvé ici, dans l'ordre où l'agent le fait :

  1. l'arbre est LISIBLE — ce qui est acquis, ce qui est ouvert, à quel prix ;
  2. une recette fermée se relie à la technologie qui l'ouvre ;
  3. les premières marches se paient en GESTES et non en flacons : `electronics`
     s'obtient en fabriquant dix plaques de cuivre, `automation-science-pack` en
     fabriquant un laboratoire ;
  4. puis le régime change : il faut un laboratoire ALIMENTÉ et des flacons, que
     l'agent fabrique depuis le minerai ;
  5. la recherche aboutit, et les recettes qu'elle ouvre deviennent fabricables.

Rien de tout cela ne demande un joueur connecté : le mode test déclare désormais les
flux de ses crafts simulés, sans quoi un déclencheur `craft-item` ne pouvait pas tomber
en headless.

Lancement (serveur Factorio + mod requis) :
    cd python
    python verify_recherche_e24.py
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import deplacement, recherche, save_ref

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:54s} {detail[:110]}", flush=True)


def _recette_ouverte(rcon, nom: str) -> bool:
    return str(rcon.query_lua(
        f"rcon.print(tostring(game.forces.player.recipes['{nom}'].enabled))")).strip() == "true"


def main() -> int:
    ok, motif = save_ref.restaurer_reference()
    if not ok:
        print(f"[SKIP] pas d'état de référence ({motif}).")
        return 0
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    rcon.query_lua("game.speed = 30 rcon.print(1)")
    coord = Coordinator(api, zone=deplacement.position(api), rayon=25.0)

    # --- Rec 1 : l'arbre est lisible ---
    arbre = recherche.lire(api)
    rec("1: l'arbre des technologies est lisible", bool(arbre.acquises) and bool(arbre.marches),
        f"{len(arbre.acquises)} acquise(s), {len(arbre.marches)} marche(s) à portée : "
        f"{', '.join(m.nom for m in arbre.marches)}")

    # --- Rec 2 : le prix d'une marche est dit, geste ou flacons ---
    # Les deux régimes doivent être distingués : s'acharner sur la file de recherche
    # pour une technologie qui attend un craft, ou attendre un craft pour une
    # technologie qui réclame vingt flacons, sont deux façons opposées de bloquer.
    gestes = [m for m in arbre.marches if m.declencheur is not None]
    rec("2: le prix de chaque marche est dit (geste ou flacons)",
        all((m.declencheur is not None) != (bool(m.cout)) for m in arbre.marches),
        f"{len(gestes)} par geste, {len(arbre.marches) - len(gestes)} en flacons : "
        + " | ".join(str(m)[:60] for m in arbre.marches[:2]))

    # --- L'énergie d'abord : un laboratoire sans courant ne cherche rien ---
    for _ in range(2):
        coord.tick()

    # --- Rec 3 : une marche par GESTE est franchie ---
    avant = set(recherche.lire(api).acquises)
    ok3, detail3 = coord.chercher("automation-science-pack")
    apres = set(recherche.lire(api).acquises)
    rec("3: une marche par geste est franchie sans flacon", ok3 and "automation-science-pack" in apres,
        f"acquises {len(avant)} -> {len(apres)} | {detail3[:70]}")

    # --- Rec 4 : la recette ouverte par cette marche est fabricable ---
    rec("4: la recette qu'elle ouvre devient fabricable",
        _recette_ouverte(rcon, "automation-science-pack"),
        "automation-science-pack passe à enabled=true")

    # --- Rec 5 : une marche qui SE PAIE est menée à bout ---
    # C'est l'autre régime : fabriquer vingt flacons depuis le minerai, poser un
    # laboratoire, le brancher, le charger, et attendre que la recherche aboutisse.
    ok5, detail5 = coord.chercher("logistics")
    rec("5: une marche qui se paie est menée à bout", ok5,
        detail5[:110])

    # --- Rec 6 : le laboratoire a été posé ET alimenté ---
    labo = str(rcon.query_lua(
        "local s = game.surfaces[1] local out = 'aucun' "
        "for _, e in pairs(s.find_entities_filtered{name='lab'}) do "
        "out = string.format('(%d,%d) courant=%s energie=%.0f', e.position.x, "
        "e.position.y, tostring(e.is_connected_to_electric_network()), e.energy) end "
        "rcon.print(out)")).strip()
    rec("6: le laboratoire est posé et alimenté", "courant=true" in labo,
        f"laboratoire {labo}")

    # --- Rec 7 : les recettes de la technologie payée sont ouvertes ---
    ouvertes = [n for n in ("underground-belt", "splitter") if _recette_ouverte(rcon, n)]
    rec("7: les recettes de la technologie payée sont ouvertes", len(ouvertes) == 2,
        f"{', '.join(ouvertes) or 'aucune'} sur underground-belt, splitter")

    rcon.query_lua("game.speed = 1 rcon.print(1)")
    rcon.close()

    print("\n" + "=" * 74)
    nok = sum(1 for _, o, _ in RESULTS if o)
    for name, o, detail in RESULTS:
        if not o:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 74)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
