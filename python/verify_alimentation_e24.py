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


def _poser_source(rcon, api, item: str, minerai: str, x: float, y: float) -> str:
    """Une source PÉRENNE, posée par le banc : coffre -> bras -> four.

    Pérenne au sens de l'agent : une machine qu'un bras remplit tout seul. Un four qu'on
    garnit à la main ferait illusion le temps d'une fournée, puis tarirait — et le banc
    mesurerait alors l'endurance d'une chaîne minière au lieu du convoyage.
    """
    rcon.query_lua(
        f"local s = game.surfaces[1] local f = game.forces.player "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{{x - 4},{y - 4}}},"
        f"{{{x + 4},{y + 4}}}}}, force='player'}}) do e.destroy() end "
        f"s.create_entity{{name='electric-furnace', position={{{x},{y}}}, force=f}} "
        f"s.create_entity{{name='wooden-chest', position={{{x + 3},{y}}}, force=f}} "
        f"s.create_entity{{name='small-electric-pole', "
        f"position={{{x - 2.5},{y + 2.5}}}, force=f}} "
        f"rcon.print('cree')")

    # LE BRAS SE POSE AVEC `place_inserter_vers`, qui LIT son pickup et son drop réels et
    # tourne jusqu'à ce que les deux tombent où il faut. Créé à la main avec une
    # orientation devinée, il ne desservait pas le four : mesuré, `servi_par=0` alors que
    # le four affichait soixante-huit plaques en sortie — la source était donc rejetée
    # comme non pérenne, et l'agent allait en bâtir une à quatre-vingt-douze tuiles au
    # lieu d'utiliser celle-ci. On ne DÉDUIT pas le sens d'un bras : on le mesure.
    from services import site_finder
    site_finder.place_inserter_vers(api, (x, y), (x + 3, y), "electric-furnace",
                                    nom="inserter", source_types=("wooden-chest",))
    # Et le poteau qui couvre le bras : mesuré, ce bras-là sortait `no_power` — le four
    # était alimenté, lui, mais pas ce qui devait le remplir. La source aurait tari dès
    # son amorce consommée, et le banc aurait mesuré l'endurance d'un stock au lieu d'un
    # convoyage. Ce que ce banc pose, il l'alimente : la règle vaut aussi pour lui.
    rcon.query_lua(
        f"local s = game.surfaces[1] local f = game.forces.player "
        f"if s.can_place_entity{{name='small-electric-pole', position={{{x + 1.5},{y + 2}}}}} "
        f"then s.create_entity{{name='small-electric-pole', "
        f"position={{{x + 1.5},{y + 2}}}, force=f}} end rcon.print('ok')")

    # LE GARNISSAGE DANS UNE REQUÊTE SÉPARÉE. Fait dans la foulée de `create_entity`,
    # l'insertion ne prend pas : l'inventaire de l'entité n'est pas encore disponible.
    # Mesuré — les fours affichaient `in=0` alors qu'on venait d'y verser cent minerais,
    # donc aucune recette, donc invisibles comme sources ; et rejouer la même insertion
    # séparément rendait « insere=50, contenu=50 ». Même famille que la pose asynchrone :
    # on ne chaîne pas une écriture sur une entité qui vient de naître.
    return str(rcon.query_lua(
        f"local s = game.surfaces[1] local dit = 'echec' "
        f"for _, e in pairs(s.find_entities_filtered{{name='wooden-chest', "
        f"area={{{{{x + 2},{y - 1}}},{{{x + 4},{y + 1}}}}}}}) do "
        f"  e.get_inventory(defines.inventory.chest).insert{{name='{minerai}', count=2000}} end "
        f"for _, e in pairs(s.find_entities_filtered{{name='electric-furnace', "
        f"area={{{{{x - 1},{y - 1}}},{{{x + 1},{y + 1}}}}}}}) do "
        f"  local i = e.get_inventory(defines.inventory.furnace_source) "
        f"  if i then i.insert{{name='{minerai}', count=100}} "
        f"    dit = string.format('%s@(%.0f,%.0f) amorce de %d, servi par un bras', "
        f"'{item}', e.position.x, e.position.y, i.get_item_count()) end end "
        f"rcon.print(dit)")).strip()


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
    # UN SEUL tour, celui de l'énergie. Le second bâtissait une chaîne de fer dont
    # l'agent se servait ensuite comme source — à quatorze tuiles de la nôtre, hors de
    # la scène que ce banc contrôle, et parfois sans courant. Deux sources concurrentes
    # rendaient le résultat imprévisible : on ne savait plus laquelle était mesurée.
    # ON JOUE DES TOURS JUSQU'À CE QUE L'ÉNERGIE SOIT LÀ, sans aller plus loin. Un seul
    # tour ne bâtit pas toujours la centrale — l'agent décide, et son premier choix n'est
    # pas garanti ; mesuré, un passage sans le moindre boiler sur la carte, donc « SCENE
    # NON MONTEE » alors que le banc n'avait simplement pas laissé le temps de bâtir. On
    # s'arrête dès que le réseau produit, pour ne pas laisser naître une chaîne
    # concurrente qui brouillerait la mesure.
    for _ in range(4):
        etat_r = api.get_power_state(zone[0], zone[1], 8.0) or {}
        if float(etat_r.get("productionKW") or 0) > 0:
            break
        coord.tick()
        rcon.query_lua(
            "local s = game.surfaces[1] "
            "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
            "  local f = e.get_fuel_inventory() if f then f.insert{name='coal', count=200} end end "
            "rcon.print('ok')")
        api.run_action(api.wait, 180, timeout=90.0)

    # LA CENTRALE EST GARNIE TOUT DE SUITE. Un boiler brûle son charbon en moins de deux
    # minutes ; garni plus tard, le réseau est déjà à zéro quand l'agent pose son
    # laboratoire et son assembleuse, et tout ce qui suit se monte sans courant.
    rcon.query_lua(
        "local s = game.surfaces[1] "
        "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
        "  local f = e.get_fuel_inventory() if f then f.insert{name='coal', count=200} end end "
        "rcon.print('ok')")
    # LES SOURCES SONT ALIGNÉES SUR L'ASSEMBLEUSE, et posées APRÈS elle. Placées d'avance
    # « quelque part », leurs trajets devenaient des L dont le coude contournait la tuile
    # d'arrivée et coupait le flux — trois belts isolées, et le cuivre qui s'arrêtait à
    # deux tuiles de son but. Sur un même axe, le trajet est une LIGNE DROITE : il n'y a
    # plus de coude, donc plus de contournement possible. Un banc de convoyage doit
    # éprouver le convoyage, pas la capacité d'un tracé en L à se faufiler.
    ok_auto, detail_auto = coord.automatiser_la_science()
    if not ok_auto:
        print(f"[SKIP] la science n'a pas pu être automatisée ({detail_auto[:90]}) — "
              f"ce banc suppose ce maillon acquis, il ne l'éprouve pas.")
        rcon.close()
        return 0
    ou_asm = _science(rcon)
    axe_y = ou_asm.get("y", zone[1] - 6)
    axe_x = ou_asm.get("x", zone[0])
    cuivre = _poser_source(rcon, api, "copper-plate", "copper-ore", axe_x - 14, axe_y)
    fer = _poser_source(rcon, api, "iron-plate", "iron-ore", axe_x + 14, axe_y)

    # ET ON TIRE LE COURANT JUSQU'À ELLES. Un four électrique sans courant ne fond rien,
    # n'a donc jamais de recette, et reste invisible comme source : mesuré, les deux
    # sources posées par ce banc affichaient `rec=AUCUNE` et `no_power`, et le constat 1
    # concluait « aucune source de fer » sur une scène que le banc venait de monter.
    # Une scène doit être FONCTIONNELLE, pas seulement présente — c'est la même règle que
    # pour l'agent, et le banc n'y échappe pas.
    from services import site_finder
    depart = site_finder.poteau_alimente_le_plus_proche(api, zone[0], zone[1])
    for dx in (-14, 14):
        cible_p = (axe_x + dx - 2.5, axe_y + 2.5)
        if depart is not None:
            site_finder.place_pole_line(api, (depart[0], depart[1]), cible_p)
        coord.brancher("electric-furnace", axe_x + dx, axe_y)
        coord.brancher("inserter", axe_x + dx + 1.5, axe_y)
    # Les chaudières sont regarnies : un boiler brûle son charbon en moins de deux
    # minutes, et sur les neuf mille ticks de la mesure tout s'arrêterait.
    api.run_action(api.wait, 600, timeout=180.0)
    print(f"       scène : {cuivre} | {fer}", flush=True)

    # ON REGARNIT JUSTE AVANT DE JUGER. Deux cents charbons brûlent en quelques minutes
    # de jeu, et le montage de la scène en consomme autant : la centrale, garnie au
    # départ, était déjà retombée à zéro au moment du contrôle — « SCENE NON MONTEE » sur
    # une usine qui venait de tourner. Le combustible se donne au dernier moment.
    rcon.query_lua(
        "local s = game.surfaces[1] "
        "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
        "  local f = e.get_fuel_inventory() if f then f.insert{name='coal', count=500} end end "
        "rcon.print('ok')")
    api.run_action(api.wait, 300, timeout=120.0)
    # À L'ASSEMBLEUSE, pas au point de départ : la scène a pu déblayer ce coin-là.
    reseau = api.get_power_state(axe_x, axe_y, 6.0) or {}
    if float(reseau.get("productionKW") or 0) <= 0:
        print("[SKIP] SCENE NON MONTEE : le réseau ne produit rien — ce banc éprouve le "
              "transport, pas la tenue d'une centrale.")
        rcon.close()
        return 0

    ok_alim, detail_alim = coord.alimenter_la_science()

    # TOUT CE QUI EST POSÉ DOIT AVOIR DU COURANT — y compris ce que la scène a monté et
    # ce que l'agent a ajouté. Mesuré : le bras qui verse dans le laboratoire et ceux qui
    # remplissent les fours sortaient `no_power`, chacun à quelques tuiles d'un réseau
    # vivant. On repasse donc brancher ce qui ne l'est pas, plutôt que de mesurer une
    # usine à moitié morte.
    sans_jus = str(rcon.query_lua(
        "local s = game.surfaces[1] local out = {} "
        "for _, e in pairs(s.find_entities_filtered{type='inserter', force='player'}) do "
        "  if e.status == defines.entity_status.no_power then "
        "    out[#out+1] = e.position.x .. ',' .. e.position.y end end "
        "rcon.print(table.concat(out, ';'))")).strip()
    for coord_txt in [c for c in sans_jus.split(";") if c]:
        try:
            bx, by = (float(v) for v in coord_txt.split(","))
        except ValueError:
            continue
        coord.brancher("inserter", bx, by)
    api.run_action(api.wait, 120, timeout=60.0)

    # --- Rec 1 : les deux sources sont RECONNUES ---
    # L'agent doit voir ce qui produit, et ne retenir que ce qui est réellement alimenté :
    # un four garni à la main ferait illusion une fournée puis tarirait.
    src_cu = coord._source_de("copper-plate")
    src_fe = coord._source_de("iron-plate")
    rec("1: les deux sources pérennes sont reconnues",
        src_cu is not None and src_fe is not None,
        f"cuivre -> {src_cu[0] if src_cu else 'AUCUNE'} ; fer -> {src_fe[0] if src_fe else 'AUCUNE'}")

    # --- Rec 2 : la belt est posée ET chaînée ---
    # UNE BELT PEUT ÊTRE ALIMENTÉE PAR LE CÔTÉ. Le premier critère ne regardait que
    # l'axe de la belt elle-même — son amont et son aval en ligne droite — et comptait
    # donc comme « isolée » une tuile qu'une voisine perpendiculaire alimente très bien,
    # ce que Factorio permet et que les tracés en L produisent à chaque coude. Est
    # réellement isolée une belt qui ne verse dans aucune autre ET vers laquelle aucune
    # ne verse.
    belts = str(rcon.query_lua(
        "local s = game.surfaces[1] local n, casses = 0, 0 "
        "local dirs = {[0]={x=0,y=-1},[4]={x=1,y=0},[8]={x=0,y=1},[12]={x=-1,y=0}} "
        "local ens = s.find_entities_filtered{name='transport-belt', force='player'} "
        "local par = {} "
        "for _, e in pairs(ens) do par[e.position.x .. ':' .. e.position.y] = e end "
        "for _, e in pairs(ens) do n = n + 1 "
        "  local d = dirs[e.direction] "
        "  if d then "
        "    local verse = par[(e.position.x + d.x) .. ':' .. (e.position.y + d.y)] ~= nil "
        "    local recoit = false "
        "    for _, v in pairs(dirs) do "
        "      local q = par[(e.position.x - v.x) .. ':' .. (e.position.y - v.y)] "
        "      if q and dirs[q.direction] and dirs[q.direction].x == v.x "
        "         and dirs[q.direction].y == v.y then recoit = true end end "
        # ET UN BOUT DE LIGNE N'EST PAS UNE ORPHELINE. La dernière tuile d'une ligne
        # d'alimentation ne verse dans aucune belt — c'est là que le BRAS vient prendre,
        # et c'est exactement ce qu'on veut. La compter comme isolée reviendrait à
        # déclarer défectueuse toute ligne correctement terminée.
        "    local sert = #s.find_entities_filtered{type='inserter', "
        "position=e.position, radius=1.6} > 0 "
        "    if not verse and not recoit and not sert then casses = casses + 1 end end end "
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
    # Fenêtres de 1500 ticks : allongées à 3000, elles ne captent pas mieux un flux qui
    # s'épuise après sa première salve — mesuré, le même total de trois cuivres et trois
    # engrenages, concentré sur une seule fenêtre dans les deux cas.
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
        # LA CENTRALE EST RENOURRIE À CHAQUE FENÊTRE. Mesuré : au bout de la mesure, TOUT
        # était `no_power` — bras, fours, assembleuses — alors que les coffres avaient
        # livré treize cents minerais et que les fours étaient pleins. Le flux ne s'était
        # pas « épuisé » : le courant avait disparu. Ce banc éprouve le TRANSPORT, pas
        # l'endurance d'une centrale ; laisser celle-ci tomber à sec revient à mesurer
        # autre chose que son sujet.
        rcon.query_lua(
            "local s = game.surfaces[1] "
            "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
            "  local f = e.get_fuel_inventory() "
            "  if f and f.get_item_count() < 50 then f.insert{name='coal', count=200} end end "
            "rcon.print('ok')")
        api.run_action(api.wait, TICKS, timeout=300.0)
        etat = _science(rcon)
        entrees.append(int(etat.get("entree", 0) or 0))
        cumuls.append(perception.production_cumulee(api, FLACON))
        consos.append(_consommes())
    apres = _science(rcon)

    # Une fenêtre est SERVIE si un ingrédient y a été consommé ou s'y trouve en attente :
    # les deux signes disent que la belt a livré. Le seul stock ne suffit pas — une
    # assembleuse qui consomme aussitôt paraîtrait vide alors qu'elle est bien servie.
    # TROIS SIGNES, un seul fait. Une fenêtre est SERVIE si un ingrédient y a été
    # consommé, s'il en reste en attente dans l'entrée, ou si un flacon en est sorti —
    # car un flacon ne peut être produit que si ses DEUX ingrédients sont arrivés. Ce
    # n'est pas un critère plus lâche, c'est le même fait observé par le canal le plus
    # fiable : les statistiques de consommation se sont révélées bien plus avares que la
    # production, qui, elle, ne peut pas mentir sur ce qui est entré dans la machine.
    servies, precedent, cumul_prec = 0, conso_depart, cumul_depart
    for i in range(FENETRES):
        bouge = any(consos[i].get(k, 0) > precedent.get(k, 0)
                    for k in ("copper-plate", "iron-gear-wheel"))
        produit_ici = cumuls[i] > cumul_prec >= 0
        if bouge or produit_ici or entrees[i] > 0:
            servies += 1
        precedent, cumul_prec = consos[i], cumuls[i]

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
