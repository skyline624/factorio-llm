"""Vérification en jeu : les ingrédients arrivent SANS qu'on les porte.

`automatiser_la_science` a monté le maillon aval — une assembleuse verse ses flacons
dans le laboratoire. Mais elle tournait sur une provision déposée à la main : elle
s'arrêtait dès qu'elle l'avait consommée. Tant que c'est l'agent qui porte, rien ne
tourne en son absence. Ce qui est éprouvé ici est l'amont : `alimenter_la_science` va
chercher une machine qui PRODUIT chaque ingrédient et l'amène par bras + belt + bras.

**CE BANC POSE SA PROPRE SCÈNE**, et c'est le fruit d'une leçon coûteuse. Une première
version demandait à l'agent de tout construire — centrale, chaîne de fer, chaîne de
cuivre à soixante-treize tuiles, laboratoire, deux assembleuses, quatre-vingts tuiles de
belt — puis mesurait le convoyage au bout de cette accumulation. Résultat : un verdict
qui oscillait entre 2/6 et 5/6 sur le MÊME code, où l'on ne pouvait plus distinguer
l'effet d'un correctif du bruit du système. Il ne mesurait pas le convoyage, il mesurait
la somme de toutes les fragilités du projet.

Les autres bancs du projet ne font pas cela : `verify_doctor_e6` monte sa panne avant de
juger le diagnostic, il ne demande pas d'abord de bâtir une usine. On fait pareil — deux
sources pérennes posées À COURTE DISTANCE, alimentées par un coffre et un bras, et l'on
n'éprouve que ce qu'on prétend éprouver.

Ce qui reste éprouvé, sans rien relâcher : les deux flux ne se rejoignent pas, la belt
est chaînée, les bras ont du courant, la chaîne se monte, l'assembleuse est servie de
façon répétée, et la production se poursuit sur six fenêtres. La CONSTRUCTION des chaînes
minières, elle, a ses propres bancs (`verify_bootstrap_craft`, `verify_objectif_e22`).

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
    if not ok and len(detail) > 115:
        for suite in range(115, len(detail), 115):
            print(f"       {detail[suite:suite + 115]}", flush=True)


def _science(rcon) -> dict:
    """L'assembleuse réglée sur le flacon : entrée, sortie, position, contenu."""
    brut = str(rcon.query_lua(
        f"local s = game.surfaces[1] local out = '' "
        f"for _, e in pairs(s.find_entities_filtered{{name='assembling-machine-1'}}) do "
        f"  local ok, r = pcall(function() return e.get_recipe() end) "
        f"  if ok and r and r.name == '{FLACON}' then "
        f"    local i = e.get_inventory(defines.inventory.assembling_machine_input) "
        f"    local o = e.get_inventory(defines.inventory.assembling_machine_output) "
        f"    local d = {{}} "
        f"    if i then for _, st in pairs(i.get_contents()) do "
        f"      d[#d+1] = st.name .. 'x' .. st.count end end "
        f"    out = (i and i.get_item_count() or -1) .. '|' .. "
        f"(o and o.get_item_count() or -1) .. '|' .. e.position.x .. '|' .. e.position.y "
        f".. '|' .. table.concat(d, ',') "
        f"  end end rcon.print(out)")).strip()
    m = brut.split("|")
    if len(m) < 4:
        return {}
    try:
        return {"entree": int(m[0]), "sortie": int(m[1]),
                "x": float(m[2]), "y": float(m[3]),
                "contenu": m[4] if len(m) > 4 else ""}
    except ValueError:
        return {}


def _poser_source(rcon, item: str, minerai: str, x: float, y: float) -> str:
    """Une source PÉRENNE, posée par le banc : coffre -> bras -> four.

    Pérenne au sens de l'agent : une machine qu'un bras remplit tout seul. Un four qu'on
    garnit à la main ferait illusion le temps d'une fournée, puis tarirait — et le banc
    mesurerait alors l'endurance d'une chaîne minière au lieu du convoyage.
    """
    return str(rcon.query_lua(
        f"local s = game.surfaces[1] local f = game.forces.player "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{x - 4},{y - 4}}},"
        f"{{{x + 4},{y + 4}}}}}, force='player'}}) do e.destroy() end "
        f"local four = s.create_entity{{name='electric-furnace', position={{{x},{y}}}, "
        f"force=f}} "
        f"local coffre = s.create_entity{{name='wooden-chest', position={{{x + 3},{y}}}, "
        f"force=f}} "
        f"local bras = s.create_entity{{name='inserter', position={{{x + 1.5},{y}}}, "
        f"force=f, direction=defines.direction.west}} "
        f"local pole = s.create_entity{{name='small-electric-pole', "
        f"position={{{x - 2.5},{y + 2.5}}}, force=f}} "
        f"if not (four and coffre and bras) then rcon.print('echec') return end "
        f"coffre.get_inventory(defines.inventory.chest).insert{{name='{minerai}', "
        f"count=2000}} "
        f"rcon.print(string.format('%s@(%.0f,%.0f) servi par un bras depuis un coffre', "
        f"'{item}', four.position.x, four.position.y))")).strip()


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
    zone = deplacement.position(api)
    coord = Coordinator(api, zone=zone, rayon=25.0)

    # --- SCÈNE : l'énergie, puis DEUX SOURCES pérennes à courte distance ---
    for _ in range(2):
        coord.tick()
    cuivre = _poser_source(rcon, "copper-plate", "copper-ore", zone[0] - 14, zone[1] - 6)
    fer = _poser_source(rcon, "iron-plate", "iron-ore", zone[0] + 14, zone[1] - 6)
    # Les fours sont électriques : ils doivent être reliés comme le reste.
    for dx in (-14, 14):
        coord.brancher("electric-furnace", zone[0] + dx, zone[1] - 6)
        coord.brancher("inserter", zone[0] + dx + 1.5, zone[1] - 6)
    # Les chaudières sont regarnies : un boiler brûle son charbon en moins de deux
    # minutes, et sur les neuf mille ticks de la mesure tout s'arrêterait.
    rcon.query_lua(
        "local s = game.surfaces[1] "
        "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
        "  local f = e.get_fuel_inventory() if f then f.insert{name='coal', count=200} end end "
        "rcon.print('ok')")
    api.run_action(api.wait, 600, timeout=180.0)
    print(f"       scène : {cuivre} | {fer}", flush=True)

    reseau = api.get_power_state(zone[0], zone[1], 6.0) or {}
    if float(reseau.get("productionKW") or 0) <= 0:
        print("[SKIP] SCENE NON MONTEE : le réseau ne produit rien — ce banc éprouve le "
              "transport, pas la tenue d'une centrale.")
        rcon.close()
        return 0

    ok_auto, detail_auto = coord.automatiser_la_science()
    if not ok_auto:
        print(f"[SKIP] la science n'a pas pu être automatisée ({detail_auto[:90]}) — "
              f"ce banc suppose ce maillon acquis, il ne l'éprouve pas.")
        rcon.close()
        return 0

    ok_alim, detail_alim = coord.alimenter_la_science()

    # --- Rec 1 : les deux sources sont RECONNUES ---
    # L'agent doit voir ce qui produit, et ne retenir que ce qui est réellement alimenté :
    # un four garni à la main ferait illusion une fournée puis tarirait.
    src_cu = coord._source_de("copper-plate")
    src_fe = coord._source_de("iron-plate")
    rec("1: les deux sources pérennes sont reconnues",
        src_cu is not None and src_fe is not None,
        f"cuivre -> {src_cu[0] if src_cu else 'AUCUNE'} ; fer -> {src_fe[0] if src_fe else 'AUCUNE'}")

    # --- Rec 2 : la belt est posée ET chaînée ---
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

    # --- Rec 3 : les bras du convoyage ont VRAIMENT du courant ---
    # Être raccordé n'est pas être alimenté : on juge sur le STATUT.
    bras = str(rcon.query_lua(
        "local s = game.surfaces[1] local n, sans = 0, 0 "
        "for _, e in pairs(s.find_entities_filtered{type='inserter', force='player'}) do "
        "  local b = s.find_entities_filtered{name='transport-belt', "
        "position=e.position, radius=1.6} "
        "  if #b > 0 then n = n + 1 "
        "    if e.status == defines.entity_status.no_power "
        "       or e.status == defines.entity_status.low_power "
        "    then sans = sans + 1 end end end "
        "rcon.print(n .. '|' .. sans)")).strip()
    nb_bras, _, sans_courant = bras.partition("|")
    rec("3: les bras du convoyage ont vraiment du courant",
        nb_bras not in ("", "0") and sans_courant == "0",
        f"{nb_bras} bras au contact d'une belt, {sans_courant} en no_power/low_power")

    # --- Rec 4 : la chaîne est montée ---
    rec("4: la chaîne d'alimentation est montée", ok_alim, detail_alim)

    # --- Mesure du FLUX sur six fenêtres ---
    FENETRES, TICKS = 6, 1500

    def _consommes() -> dict:
        brut = str(rcon.query_lua(
            "local f = game.forces.player local s = game.surfaces[1] "
            "local st = f.get_item_production_statistics(s) "
            "rcon.print(st.get_output_count('copper-plate') .. '|' "
            ".. st.get_output_count('iron-gear-wheel'))")).strip()
        m = brut.split("|")
        try:
            return {"copper-plate": int(m[0]), "iron-gear-wheel": int(m[1])}
        except (ValueError, IndexError):
            return {"copper-plate": -1, "iron-gear-wheel": -1}

    cumul_depart = perception.production_cumulee(api, FLACON)
    conso_depart = _consommes()
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{name='assembling-machine-1'}}) do "
        f"  local ok, r = pcall(function() return e.get_recipe() end) "
        f"  if ok and r and r.name == '{FLACON}' then "
        f"    e.get_inventory(defines.inventory.assembling_machine_input).clear() "
        f"    e.get_inventory(defines.inventory.assembling_machine_output).clear() "
        f"  end end rcon.print('vide')")

    entrees, cumuls, consos = [], [], []
    for _ in range(FENETRES):
        api.run_action(api.wait, TICKS, timeout=300.0)
        etat = _science(rcon)
        entrees.append(int(etat.get("entree", 0) or 0))
        cumuls.append(perception.production_cumulee(api, FLACON))
        consos.append(_consommes())
    apres = _science(rcon)

    # Une fenêtre est SERVIE si un ingrédient y a été consommé ou s'y trouve en attente :
    # les deux signes disent que la belt a livré. Le seul stock ne suffit pas — une
    # assembleuse qui consomme aussitôt paraîtrait vide alors qu'elle est bien servie.
    servies, precedent = 0, conso_depart
    for i in range(FENETRES):
        bouge = any(consos[i].get(k, 0) > precedent.get(k, 0)
                    for k in ("copper-plate", "iron-gear-wheel"))
        if bouge or entrees[i] > 0:
            servies += 1
        precedent = consos[i]

    produits = (cumuls[-1] - cumul_depart) if cumul_depart >= 0 and cumuls[-1] >= 0 else -1
    actives = sum(1 for a, b in zip([cumul_depart] + cumuls, cumuls) if b > a)

    rec("5: l'assembleuse est servie de façon RÉPÉTÉE", servies >= 2,
        f"servie sur {servies}/{FENETRES} fenêtre(s) de {TICKS} ticks — relevés {entrees}, "
        f"consommé {consos[-1]['copper-plate'] - conso_depart['copper-plate']} cuivre et "
        f"{consos[-1]['iron-gear-wheel'] - conso_depart['iron-gear-wheel']} engrenage(s)"
        f" | contenu final : {apres.get('contenu') or 'vide'}")

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
