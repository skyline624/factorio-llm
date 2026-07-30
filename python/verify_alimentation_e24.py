"""Vérification en jeu : les ingrédients arrivent SANS qu'on les porte.

`automatiser_la_science` a monté le maillon aval — une assembleuse verse ses flacons
dans le laboratoire. Mais elle tournait sur une provision déposée à la main : elle
s'arrêtait dès qu'elle l'avait consommée, et il fallait revenir la remplir. Tant que
c'est l'agent qui porte, rien ne tourne en son absence.

Ce qui est éprouvé ici est l'amont : l'agent va chercher une machine qui PRODUIT chaque
ingrédient, en bâtit une s'il n'en existe pas, et l'amène par bras + belt + bras.

L'ordre des constats n'est pas indifférent. Les quatre premiers peuvent être verts
pendant que rien ne circule — c'est exactement ce qui a été observé avant de trouver un
bras de chargement sans courant, à soixante-dix tuiles de la centrale : belt déroulée,
bras posés aux deux bouts, et pas un objet transporté. **Seul le constat 5 tranche.**

Une précaution de mise en scène, et elle est assumée : on garantit que la source A DE
QUOI produire (minerai et combustible) avant d'éprouver le convoyage. Le sujet de ce
banc est le TRANSPORT, pas l'endurance d'une chaîne minière ; une source à sec ferait
échouer le test pour une raison étrangère à ce qu'il mesure — et se lirait comme un
défaut du convoyage. C'est la leçon de la journée : un banc doit monter sa scène, et le
dire quand il n'y arrive pas.

Lancement (serveur Factorio + mod requis) :
    cd python
    python verify_alimentation_e24.py
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import deplacement, perception, save_ref

FLACON = "automation-science-pack"
RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:115]}", flush=True)


def _science(rcon) -> dict:
    """L'assembleuse réglée sur le flacon : son entrée, sa sortie, son statut."""
    brut = str(rcon.query_lua(
        f"local s = game.surfaces[1] local out = '' "
        f"for _, e in pairs(s.find_entities_filtered{{name='assembling-machine-1'}}) do "
        f"  local ok, r = pcall(function() return e.get_recipe() end) "
        f"  if ok and r and r.name == '{FLACON}' then "
        f"    local i = e.get_inventory(defines.inventory.assembling_machine_input) "
        f"    local o = e.get_inventory(defines.inventory.assembling_machine_output) "
        f"    out = (i and i.get_item_count() or -1) .. '|' .. "
        f"(o and o.get_item_count() or -1) .. '|' .. e.position.x .. '|' .. e.position.y "
        f"  end end rcon.print(out)")).strip()
    m = brut.split("|")
    if len(m) != 4:
        return {}
    try:
        return {"entree": int(m[0]), "sortie": int(m[1]),
                "x": float(m[2]), "y": float(m[3])}
    except ValueError:
        return {}


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

    # L'énergie d'abord : une assembleuse sans courant a une recette et ne fait rien.
    for _ in range(2):
        coord.tick()
    ok_auto, detail_auto = coord.automatiser_la_science()
    if not ok_auto:
        print(f"[SKIP] la science n'a pas pu être automatisée ({detail_auto[:90]}) — "
              f"ce banc suppose ce maillon acquis, il ne l'éprouve pas.")
        rcon.close()
        return 0

    ok_alim, detail_alim = coord.alimenter_la_science()

    # --- Rec 1 : une source a été BÂTIE pour ce que l'agent ne produisait pas ---
    # Toutes ses chaînes étaient du fer ; le flacon réclame du cuivre. Le critère est la
    # SOURCE — une machine qui produit la plaque —, et non le `mining_target` d'une
    # foreuse : une foreuse dont le minerai vient de s'épuiser rend `nil` et ferait
    # échouer le constat alors que la chaîne a bien été bâtie.
    source_cuivre = coord._source_de("copper-plate")
    rec("1: une source est bâtie pour un ingrédient jamais produit",
        source_cuivre is not None,
        (f"{source_cuivre[0]}@({source_cuivre[1]:.0f},{source_cuivre[2]:.0f}) produit du "
         f"copper-plate — toutes les chaînes précédentes étaient du fer")
        if source_cuivre else "rien ne produit de copper-plate")

    # --- Rec 2 : la belt est posée ET orientée vers l'aval ---
    # Une seule tuile à l'envers arrête le flux sans que rien ne le signale à la pose.
    # On vérifie donc que chaque segment pointe vers le suivant.
    belts = str(rcon.query_lua(
        "local s = game.surfaces[1] local n, casses = 0, 0 "
        "local dirs = {[0]={x=0,y=-1},[4]={x=1,y=0},[8]={x=0,y=1},[12]={x=-1,y=0}} "
        "local ens = s.find_entities_filtered{name='transport-belt', force='player'} "
        "local set = {} "
        "for _, e in pairs(ens) do set[e.position.x .. ':' .. e.position.y] = true end "
        "for _, e in pairs(ens) do n = n + 1 "
        "  local d = dirs[e.direction] "
        "  if d then "
        "    local aval = (e.position.x + d.x) .. ':' .. (e.position.y + d.y) "
        "    local amont = (e.position.x - d.x) .. ':' .. (e.position.y - d.y) "
        "    if not set[aval] and not set[amont] then casses = casses + 1 end end end "
        "rcon.print(n .. '|' .. casses)")).strip()
    total, _, isoles = belts.partition("|")
    rec("2: la belt est posée et chaînée", total not in ("", "0") and isoles == "0",
        f"{total} tuile(s) de belt, {isoles} isolée(s) (ni amont ni aval)")

    # --- Rec 3 : les bras du CONVOYAGE ont du courant ---
    # Un bras électrique posé loin de la centrale se pose sans erreur et ne transporte
    # rien. Mesuré : `courant=false, energie=0` à soixante-dix tuiles.
    #
    # Deux précautions, apprises ici même. On ne compte que les bras qui bordent une
    # belt — ce sont ceux du convoyage, les autres relèvent des chaînes de production et
    # ont leur propre banc. Et l'on mesure APRÈS avoir laissé le jeu prendre acte : lu
    # dans la foulée de la pose, un bras tout juste raccordé se déclare encore sans
    # courant, et le constat accuse un défaut qui n'existe plus une seconde plus tard.
    api.run_action(api.wait, 120, timeout=60.0)
    bras = str(rcon.query_lua(
        "local s = game.surfaces[1] local n, sans = 0, 0 "
        "for _, e in pairs(s.find_entities_filtered{type='inserter', force='player'}) do "
        "  local b = s.find_entities_filtered{name='transport-belt', "
        "position=e.position, radius=1.6} "
        "  if #b > 0 then n = n + 1 "
        "    if not e.is_connected_to_electric_network() then sans = sans + 1 end end end "
        "rcon.print(n .. '|' .. sans)")).strip()
    nb_bras, _, sans_courant = bras.partition("|")
    rec("3: les bras du convoyage sont alimentés",
        nb_bras not in ("", "0") and sans_courant == "0",
        f"{nb_bras} bras au contact d'une belt, {sans_courant} sans courant")

    # --- SCÈNE : la source doit avoir DE QUOI produire ---
    # Le sujet est le transport, pas l'endurance d'une chaîne minière. On amorce donc les
    # fours ; si l'on n'y arrive pas, on le DIT plutôt que d'accuser le convoyage.
    amorce = str(rcon.query_lua(
        "local s = game.surfaces[1] local n = 0 "
        "for _, e in pairs(s.find_entities_filtered{type='furnace', force='player'}) do "
        "  local f = e.get_fuel_inventory() "
        "  local i = e.get_inventory(defines.inventory.furnace_source) "
        "  if f then f.insert{name='coal', count=30} end "
        "  if i then "
        "    local minerai = e.mining_target and '' or 'copper-ore' "
        "    i.insert{name='copper-ore', count=50} "
        "    i.insert{name='iron-ore', count=50} end "
        "  n = n + 1 end rcon.print(n)")).strip()
    print(f"       scène : {amorce} four(s) amorcé(s) en minerai et combustible", flush=True)

    avant = _science(rcon)
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{name='assembling-machine-1'}}) do "
        f"  local ok, r = pcall(function() return e.get_recipe() end) "
        f"  if ok and r and r.name == '{FLACON}' then "
        f"    e.get_inventory(defines.inventory.assembling_machine_input).clear() "
        f"    e.get_inventory(defines.inventory.assembling_machine_output).clear() "
        f"  end end rcon.print('vide')")

    # ON ÉCHANTILLONNE, on ne prend pas une photo. Un instantané de l'entrée sous-estime
    # tout : l'assembleuse consomme au fur et à mesure, si bien qu'on peut la trouver
    # vide alors qu'elle est servie en continu. Et un seul relevé ne distingue pas un
    # FLUX d'un sursaut — les objets déjà sur la belt au moment du montage arrivent une
    # fois, puis plus rien. Ce que ce banc doit prouver est que la boucle ne s'arrête
    # pas ; il faut donc la regarder plusieurs fois.
    FENETRES, TICKS = 6, 1500
    cumul_depart = perception.production_cumulee(api, FLACON)
    entrees, cumuls = [], []
    for _ in range(FENETRES):
        api.run_action(api.wait, TICKS, timeout=300.0)
        etat = _science(rcon)
        entrees.append(int(etat.get("entree", 0) or 0))
        cumuls.append(perception.production_cumulee(api, FLACON))
    apres = _science(rcon)

    servies = sum(1 for e in entrees if e > 0)
    produits = (cumuls[-1] - cumul_depart) if cumul_depart >= 0 and cumuls[-1] >= 0 else -1
    actives = sum(1 for a, b in zip([cumul_depart] + cumuls, cumuls) if b > a)

    # --- Rec 4 : l'agent a monté la chaîne (constat de forme) ---
    rec("4: la chaîne d'alimentation est montée", ok_alim, detail_alim[:115])

    # --- Rec 5 : LE CONSTAT QUI TRANCHE — les ingrédients arrivent, et ILS REVIENNENT ---
    # Les quatre précédents peuvent être verts pendant que rien ne circule. Exiger
    # plusieurs fenêtres servies distingue une belt qui alimente d'un stock résiduel qui
    # se vide une bonne fois.
    rec("5: l'assembleuse est servie de façon RÉPÉTÉE", servies >= 2,
        f"entrée vidée, puis servie sur {servies}/{FENETRES} fenêtre(s) de {TICKS} ticks "
        f"— relevés {entrees}")

    # --- Rec 6 : et cela PRODUIT, sur la durée ---
    # La statistique de la force, et non l'inventaire de sortie : la sortie est vidée par
    # le bras vers le laboratoire, si bien qu'on lirait « rien produit » au moment même
    # où la chaîne devient autonome.
    rec("6: la production de flacons se poursuit", produits >= 3 and actives >= 2,
        f"{produits} flacon(s) produit(s) sur {FENETRES} fenêtre(s), dont {actives} "
        f"avec progression — cumul {cumul_depart} -> {cumuls[-1]}")

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
