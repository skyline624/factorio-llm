"""Test LIVE E21 : la boucle cesse de vider à la main et bâtit son ramassage.

Le pendant exact d'E13, à l'autre bout de la machine. E13 avait montré qu'un agent qui
se contente de REMPLIR y passe sa vie ; la partie longue du 29/07 a montré le symétrique
en sortie : sur 952 tours partis d'une carte propre, le même four est retombé en
`full_output` aux tours 152, 380, 608 et 831 — vidé à la main à chaque fois, et bouché à
nouveau entre-temps. Le foreur en amont attendait `waiting_for_space` : toute la chaîne
s'arrêtait faute d'un ramassage de quelques secondes qui n'avait jamais été bâti.

Ce qu'on vérifie, dans l'ordre où cela peut mal tourner :
  1. une sortie pleine se répare d'abord par un VIDAGE — on ne construit pas au premier
     incident, et le compteur retient qu'il a eu lieu ;
  2. au-delà du seuil, la décision bascule sur `batir_evacuation` — mesuré sur l'état
     RÉEL observé en jeu, pas sur un état fabriqué à la main ;
  3. le montage est réellement posé : un coffre existe, et un bras dont le `pickup`
     tombe sur la machine et le `drop` dans le coffre. Un inserter mal orienté se pose
     sans erreur et ne transporte rien — c'est ce qui a coûté un run en E5 ;
  4. et surtout : le ramassage TRANSPORTE. On rebouche la sortie et l'on regarde le
     coffre se remplir. C'est la seule preuve qui vaille ;
  5. la mémoire est remise à zéro : un second bouchon serait un incident neuf, sinon
     l'agent empilerait les coffres.

Le décor n'est pas monté d'avance : l'agent bâtit sa propre usine depuis la référence
« carte propre » (`preparer_reference.py`), et c'est SA machine qu'on bouche.

Pré-requis : serveur headless + référence figée. SKIP si l'un manque.

Lancement :
    cd python
    python verify_evacuation_e21.py
"""

from __future__ import annotations

import sys
import time

from agents.coordinator import SEUIL_AUTOMATISATION, Coordinator, enumerer_options
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import save_ref
from services.factory_doctor import Symptome

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:54s} {detail[:105]}")


def _boucher(rcon, nom: str, x: float, y: float, combien: int = 100) -> int:
    """Remplit la sortie d'une machine. Rend ce qui est réellement entré.

    On ne simule pas la panne par un statut forcé : on la PROVOQUE comme le jeu la
    produit, en remplissant la sortie. Un statut écrit à la main ne prouverait rien du
    comportement réel de la machine ni de celui du bras.
    """
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{name='{nom}', "
        f"area={{{{{x - 1.5},{y - 1.5}}},{{{x + 1.5},{y + 1.5}}}}}}}) do "
        f"local inv = e.get_output_inventory() "
        f"if inv then n = n + inv.insert{{name='iron-plate', count={combien}}} end end "
        f"rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return 0


def _contenu_coffres(rcon, x: float, y: float, r: float = 8.0) -> int:
    """Ce que les coffres autour de la machine contiennent — la preuve du transport."""
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{name='wooden-chest', "
        f"area={{{{{x - r},{y - r}}},{{{x + r},{y + r}}}}}}}) do "
        f"local inv = e.get_inventory(defines.inventory.chest) "
        f"if inv then n = n + inv.get_item_count() end end rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return 0


def _bras_verifie(api, x: float, y: float, machine: str) -> tuple[bool, str]:
    """Un bras prend-il RÉELLEMENT sur la machine pour déposer dans un coffre.

    On lit `pickup`/`drop` posés par le jeu, on ne déduit rien de la direction : la
    mesure a déjà contredit la convention attendue.
    """
    r = api.inspect_at(x, y, 6.0)
    lignes = r.get("entities", []) if isinstance(r, dict) else []
    bras = [e for e in lignes if e.get("type") == "inserter"]
    for b in bras:
        if b.get("pickupX") is None or b.get("dropX") is None:
            continue
        prend = api.inspect_at(b["pickupX"], b["pickupY"], 0.4)
        depose = api.inspect_at(b["dropX"], b["dropY"], 0.4)
        pl = prend.get("entities", []) if isinstance(prend, dict) else []
        dl = depose.get("entities", []) if isinstance(depose, dict) else []
        if (any(e.get("name") == machine for e in pl)
                and any(e.get("name") == "wooden-chest" for e in dl)):
            return True, (f"{b.get('name')}@({b.get('x')},{b.get('y')}) prend sur "
                          f"{machine} et dépose dans un coffre")
    return False, f"{len(bras)} bras autour, aucun ne relie {machine} à un coffre"


def main() -> int:
    ok_ref, motif = save_ref.restaurer_reference()
    if not ok_ref:
        print(f"[SKIP] pas d'état de référence ({motif}).")
        print("       le figer d'abord : python preparer_reference.py")
        return 0
    print(f"       {motif}")

    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    api.setup()

    # La reference peut porter des nids (elle sert aussi aux tests de defense). Ici le
    # sujet est autre : laisses en place, ils consomment les premiers tours en `defendre`
    # et l'usine n'est jamais batie -- le test se met alors en SKIP sans rien avoir
    # eprouve. On ecarte donc la menace explicitement, plutot que d'esperer qu'elle se
    # taise.
    otes = rcon.query_lua(
        "local s = game.surfaces[1] local n = 0 "
        "for _, e in pairs(s.find_entities_filtered{force='enemy'}) do "
        "e.destroy() n = n + 1 end rcon.print(n)")
    print(f"       {str(otes).strip()} entite(s) ennemie(s) ecartee(s) : ce test ne "
          f"porte pas sur la defense")
    pos = (api.get_state().get("character") or {}).get("position") or {}
    zone = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))
    coord = Coordinator(api, zone=zone, rayon=25.0)

    # --- l'agent bâtit SON usine : c'est sa machine qu'on bouchera ---
    # On le laisse RETENTER, comme le fait la vraie boucle : mesuré, `batir_production`
    # échoue au premier passage et aboutit au suivant. Exiger la réussite du premier coup
    # ferait échouer le test sur un comportement normal de l'agent, et non sur son sujet.
    four, lignes, tours = None, [], 0
    while four is None and tours < 8:
        tours += 1
        d, agi, _ = coord.tick()
        api.run_action(api.wait, 240, timeout=90.0)
        sa = api.inspect_at(zone[0], zone[1], 25.0)
        lignes = sa.get("entities", []) if isinstance(sa, dict) else []
        four = next((e for e in lignes if "furnace" in str(e.get("name"))), None)
        print(f"       tour {tours} : {d.action} agi={agi} — "
              f"{len(lignes)} entité(s), four {'trouvé' if four else 'pas encore'}")
    if four is None:
        print(f"[SKIP] aucun four bâti en {tours} tours — ce test suppose la "
              f"construction acquise (E8).")
        rcon.close()
        return 0
    api.run_action(api.wait, 300, timeout=120.0)
    fx, fy, fn = float(four["x"]), float(four["y"]), str(four["name"])
    print(f"       usine bâtie : {fn}@({fx},{fy}) parmi {len(lignes)} entité(s)")

    # --- 1 : premier bouchon -> on VIDE, on ne construit pas ---
    # L'agent pose désormais un ramassage EN MÊME TEMPS que sa chaîne (« ce qu'on pose,
    # on l'évacue »). Un four ainsi servi ne se bouche plus : on remplissait sa sortie,
    # le bras la vidait dans la seconde, et le diagnostic avait raison de ne rien
    # signaler — on croyait alors mesurer un défaut du diagnostic. Ce test porte sur une
    # sortie RÉELLEMENT bloquée : on retire donc d'abord ce qui la dessert.
    retires = str(rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{type='inserter', "
        f"area={{{{{fx - 4.5},{fy - 4.5}}},{{{fx + 4.5},{fy + 4.5}}}}}}}) do "
        f"if e.pickup_target and e.pickup_target.name == '{fn}' then "
        f"e.destroy() n = n + 1 end end rcon.print(n)")).strip()
    mis = _boucher(rcon, fn, fx, fy)
    api.run_action(api.wait, 60, timeout=60.0)

    # Le bouchon TIENT-IL ? Sans ce contrôle, un test qui échoue à boucher se lit comme
    # un diagnostic aveugle, et l'on va corriger le mauvais fichier (leçon E6).
    reste = int(str(rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{name='{fn}', "
        f"area={{{{{fx - 1.5},{fy - 1.5}}},{{{fx + 1.5},{fy + 1.5}}}}}}}) do "
        f"local inv = e.get_output_inventory() "
        f"if inv then n = n + inv.get_item_count('iron-plate') end end "
        f"rcon.print(n)")).strip() or 0)
    print(f"       {retires} bras de ramassage retiré(s) ; sortie chargée à "
          f"{reste} plaque(s) après attente")

    etat = coord.observer()
    options = enumerer_options(etat)
    premiere = next((o for o in options if o.cible is not None
                     and o.cible.cause == "sortie_bloquee"), None)
    rec("e21-1 : une sortie pleine se répare d'abord par un vidage",
        bool(reste) and premiere is not None and premiere.action == "evacuer",
        f"{mis} plaque(s) forcée(s), {reste} restée(s) en sortie -> "
        f"{premiere.action if premiere else 'aucune option sortie_bloquee'}"
        if reste else "SCENE NON MONTEE : la sortie s'est vidée, rien à diagnostiquer")

    # --- 2 : au-delà du seuil, la décision bascule ---
    # Le comptage lui-même est éprouvé hors ligne (test_coordinator) ; ce qu'on veut voir
    # ici est la bascule sur un état RÉELLEMENT observé en jeu.
    #
    # On laisse d'abord l'agent remettre la chaîne EN SERVICE. Mesuré : après le premier
    # vidage, l'inserter était débranché, le four ne recevait plus rien et passait en
    # `other` — un four qui ne travaille pas n'est jamais `full_output`, quoi qu'on mette
    # dans sa sortie. Reboucher une machine à l'arrêt n'éprouve rien.
    for _ in range(6):
        if coord._statut_de(api, fn, fx, fy) in ("working", "normal", "full_output"):
            break
        coord.tick()
        api.run_action(api.wait, 300, timeout=120.0)
    print(f"       le four est « {coord._statut_de(api, fn, fx, fy)} » avant le second "
          f"bouchon")
    # L'agent pose desormais un ramassage DES la construction (E22h) : la sortie ne se
    # bouche donc plus toute seule, et c'est exactement l'effet recherche. Pour eprouver
    # la bascule -- qui sert encore aux machines baties avant, ou dont le coffre est plein
    # -- il faut retirer ce ramassage. On enleve le coffre : le bras n'a plus ou deposer.
    otes = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{name='wooden-chest', "
        f"area={{{{{fx - 6},{fy - 6}}},{{{fx + 6},{fy + 6}}}}}}}) do "
        f"e.destroy() n = n + 1 end rcon.print(n)")
    print(f"       {str(otes).strip()} coffre(s) de ramassage retire(s) pour reproduire "
          f"la panne")
    coord._evacuations[(fn, round(fx), round(fy))] = SEUIL_AUTOMATISATION
    _boucher(rcon, fn, fx, fy)
    api.run_action(api.wait, 300, timeout=120.0)
    etat = coord.observer()
    # On cherche l'option qui porte sur LA machine dont on a forcé le compteur, et non la
    # première venue : un four plein bloque aussi le foreur en amont, dont la sortie est
    # alors « bloquée » elle aussi. Prendre la première `sortie_bloquee` faisait lire la
    # décision concernant le foreur — pour lequel aucun vidage n'a été compté — et le
    # test échouait sur un comportement parfaitement correct.
    apres = next((o for o in enumerer_options(etat) if o.cible is not None
                  and o.cible.cause == "sortie_bloquee"
                  and o.cible.name == fn
                  and round(o.cible.x) == round(fx)
                  and round(o.cible.y) == round(fy)), None)
    # Un test qui échoue doit dire ce qu'il a VU, sinon il faut rejouer la partie à la
    # main pour l'apprendre : on affiche donc l'état réel du four et les causes retenues.
    vu = coord._statut_de(api, fn, fx, fy)
    causes = [(s.name, s.cause) for s in (etat.diagnostic.causes if etat.diagnostic else [])]
    rec("e21-2 : au-delà du seuil, il décide de BÂTIR l'évacuation",
        apres is not None and apres.action == "batir_evacuation" and apres.faisable,
        f"{apres.action if apres else 'aucune option sur ce four'} — {fn} est « {vu} » ; "
        f"causes vues : {causes[:4]}")
    if apres is None or apres.action != "batir_evacuation":
        return _verdict(rcon)

    # --- 3 : le montage est posé, et il est JUSTE ---
    cible = Symptome(name=fn, x=fx, y=fy, cause="sortie_bloquee", gravite=1,
                     detail="sortie pleine")
    fait, detail = coord.batir_evacuation(cible)
    rec("e21-3 : le coffre et le bras sont posés", fait, detail)
    relie, dit = _bras_verifie(api, fx, fy, fn)
    rec("e21-3b : le bras prend sur la machine et dépose dans le coffre", relie, dit)

    # --- 4 : LA preuve — le ramassage transporte ---
    avant = _contenu_coffres(rcon, fx, fy)
    _boucher(rcon, fn, fx, fy)
    api.run_action(api.wait, 600, timeout=180.0)
    apres_transport = _contenu_coffres(rcon, fx, fy)
    rec("e21-4 : le ramassage TRANSPORTE — le coffre se remplit",
        apres_transport > avant,
        f"contenu des coffres {avant} -> {apres_transport} après 600 ticks")

    # --- 5 : la machine repart, c'est-à-dire que le bouchon a disparu ---
    statut = coord._statut_de(api, fn, fx, fy)
    rec("e21-5 : la machine n'est plus bloquée en sortie",
        statut not in ("full_output", "waiting_for_space_in_destination"),
        f"statut de {fn} : {statut}")

    # --- 6 : la mémoire est remise à zéro ---
    reste = coord._evacuations.get((fn, round(fx), round(fy)), 0)
    rec("e21-6 : le compteur repart de zéro — pas d'empilement de coffres", reste == 0,
        f"compteur de vidages après construction : {reste}")

    print("\n       --- journal du Coordinator ---")
    for j in coord.journal[-6:]:
        print(f"       . {j[:115]}")
    return _verdict(rcon)


def _verdict(rcon) -> int:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    try:
        rcon.close()
    except Exception:
        pass
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
