"""Vérification en jeu : l'agent bâtit une chaîne pour un item qu'on lui NOMME, et elle débite.

C'est ce qui sépare un agent d'un script. Jusqu'ici chaque marchandise nouvelle était un
chantier : `automatiser_la_science`, `poser_le_laboratoire`, `_batir_la_source_de` — autant
de méthodes portant le nom de ce qu'elles fabriquaient. Le produit est désormais un
PARAMÈTRE, de bout en bout : on découvre sa chaîne, on la dimensionne, on l'implante, on la
pose, on la raccorde, on l'évacue. Aucune ligne du dépôt ne nomme l'item visé.

Ce qui est éprouvé ici, dans cet ordre :

  1. la chaîne est DÉCOUVERTE seule — personne ne souffle ses ingrédients ni ses gisements ;
  2. l'agent bâtit, et ce qu'il pose tient debout (`ok`, entités en terre) ;
  3. tous les gisements requis sont prospectés, pas seulement le premier ;
  4. la chaîne PRODUIT, l'agent ne touchant plus à rien ;
  5. elle produit ENCORE trois fenêtres plus tard — elle ne s'étouffe pas ;
  6. le MÊME chemin de code, sans une ligne nouvelle, calcule une autre chaîne.

Le sixième constat est celui qui compte : une chaîne qui marche prouve qu'on sait la
bâtir ; deux chaînes différentes par le même chemin prouvent qu'on ne l'a pas codée.

Lancement (serveur Factorio + mod requis) :
    cd python
    python verify_produire_generique.py
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import deplacement, knowledge, perception, save_ref
from services.production_solver import ProductionRequest, solve

# Une chaîne courte à un seul gisement : le sujet est la GÉNÉRICITÉ du chemin, pas
# l'endurance d'une usine de cent machines. Le second item n'est jamais bâti — il sert à
# montrer que le même code le calcule sans qu'on y touche.
CIBLE = "iron-gear-wheel"
TEMOIN = "electronic-circuit"
DEBIT = 0.5
FENETRES = 3
TICKS_PAR_FENETRE = 1800

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:110]}", flush=True)
    if not ok and len(detail) > 110:
        for suite in range(110, len(detail), 110):
            print(f"       {detail[suite:suite + 110]}", flush=True)


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

    def produits() -> int:
        return int(str(rcon.query_lua(
            f"local f = game.forces.player rcon.print(math.floor("
            f"f.get_item_production_statistics(game.surfaces[1])"
            f".get_input_count('{CIBLE}')))")).strip() or 0)

    # --- Rec 1 : la chaîne se découvre seule ---
    items, gisements = knowledge.decouvrir_chaine(api, CIBLE)
    rec("1: la chaîne est découverte sans qu'on la souffle",
        len(items) >= 2 and bool(gisements),
        f"{len(items)} item(s) : {', '.join(items)} — à extraire : {', '.join(gisements)}")

    # --- Rec 2 : l'agent bâtit ---
    bati, detail = coord.batir_chaine(CIBLE, DEBIT)
    rec("2: l'agent bâtit la chaîne", bati, detail)
    if not bati:
        print("[SKIP] rien n'est bâti — les constats suivants n'auraient rien à mesurer.")
        rcon.query_lua("game.speed = 1 rcon.print(1)")
        rcon.close()
        return 1

    # --- Rec 3 : TOUS les gisements sont prospectés ---
    # Une chaîne à deux branches dont un seul minerai est prospecté naît amputée, et rien
    # ne le signale : les machines de la branche absente restent simplement vides.
    poses = str(rcon.query_lua(
        "local s = game.surfaces[1] local out = {} "
        "for _, e in pairs(s.find_entities_filtered{type='mining-drill'}) do "
        "  local r = s.find_entities_filtered{position=e.position, radius=2, type='resource'} "
        "  if r[1] then out[#out+1] = r[1].name end end "
        "local u = {} for _, n in pairs(out) do u[n] = true end "
        "local l = {} for n in pairs(u) do l[#l+1] = n end "
        "table.sort(l) rcon.print(table.concat(l, ','))")).strip()
    exploites = {n for n in poses.split(",") if n}
    rec("3: chaque gisement requis est exploité",
        exploites >= set(gisements),
        f"requis {sorted(gisements)} — foreuses posées sur {sorted(exploites) or 'aucun'}")

    # --- L'agent donne le courant, puis on le laisse tranquille ---
    for _ in range(8):
        coord.tick()

    # --- Rec 4 : la chaîne PRODUIT ---
    # L'agent ne touche plus à rien : ce qui sort maintenant sort de la CHAÎNE, pas de ses
    # mains. C'est la seule mesure qui distingue une usine d'un tas d'entités bien rangées.
    base = produits()
    api.run_action(api.wait, TICKS_PAR_FENETRE, timeout=200.0)
    apres_une = produits()
    rec("4: la chaîne produit, l'agent n'y touchant plus", apres_une > base,
        f"{base} -> {apres_une} ({apres_une - base:+d}) sur une fenêtre")

    # --- Rec 5 : elle ne s'étouffe pas ---
    # Une chaîne sans sortie produit quelques minutes puis se tait — `full_output` sur ses
    # machines de tête. Mesurer une seule fenêtre ne l'aurait pas vu.
    for _ in range(FENETRES):
        api.run_action(api.wait, TICKS_PAR_FENETRE, timeout=200.0)
    fin = produits()
    rec("5: elle produit ENCORE trois fenêtres plus tard", fin > apres_une,
        f"{apres_une} -> {fin} ({fin - apres_une:+d}) — une chaîne sans sortie stagnerait ici")

    # --- Rec 6 : LA GÉNÉRICITÉ ---
    # Le même chemin, sans une ligne nouvelle, sur un item que rien dans le dépôt ne nomme.
    # Une chaîne qui marche prouve qu'on sait la bâtir ; deux prouvent qu'on ne l'a pas
    # écrite à la main.
    inv = perception.inventory(api)
    dispo = [m for m in knowledge.entites_par_type(api)
             if inv.get(m, 0) > 0 or perception.recipe_of(api, m) is not None]
    items_t, gis_t = knowledge.decouvrir_chaine(api, TEMOIN)
    kb_t, _ = knowledge.populate_pour(api, TEMOIN, dispo)
    foreuses = [(s.mining_speed, n) for n, s in kb_t.machines.items()
                if getattr(s, "type", "") == "mining-drill" and s.mining_speed > 0
                and getattr(s, "mining_kind", "solid") == "solid"]
    tiers = {"mine": max(foreuses)[1]} if foreuses else {}
    plan_t = solve(ProductionRequest(item=TEMOIN, rate_per_sec=DEBIT,
                                     machine_tiers=tiers), kb_t)
    rec("6: le même chemin calcule une AUTRE chaîne",
        getattr(plan_t, "feasibility", "") == "ok" and len(items_t) > len(items),
        f"{TEMOIN} : {len(items_t)} items, {len(getattr(plan_t, 'nodes', []) or [])} étages, "
        f"gisements {', '.join(gis_t)} — {getattr(plan_t, 'feasibility', '?')}")

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
