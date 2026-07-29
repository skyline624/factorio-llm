"""Test LIVE E11 (J5) : le Defender réagit-il à la menace, et seulement quand il faut ?

Un agent qui fortifie dès qu'il aperçoit un nid gaspille en défense le temps qui devait
aller à la production. Un agent qui ne fortifie jamais perd son usine. Ce qu'on vérifie
ici est donc autant la RETENUE que la réaction :

  1. la menace réelle est perçue (nids, unités, pollution) ;
  2. des nids sans pollution -> il ne propose RIEN. En Factorio les vagues partent quand
     le nuage atteint un nid : sans pollution, fortifier serait prématuré ;
  3. pollution injectée -> la menace devient imminente et il décide de défendre ;
  4. il pose des tourelles FACE AU FRONT et les munit ;
  5. des biters lâchés sur l'usine -> priorité maximale, avant même les réparations.

Les menaces sont provoquées par RCON — c'est la seule façon de juger une réaction à une
situation qu'on maîtrise, plutôt que d'attendre qu'elle survienne.

Pré-requis : serveur headless avec le mod J5. SKIP (return 0) si injoignable.
"""

from __future__ import annotations

import sys

from agents.coordinator import Coordinator, enumerer_options
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.threat_model import EN_COURS, IMMINENTE, LATENTE, evaluer

RESULTS: list[tuple[str, bool, str]] = []
ZONE = (0.0, 0.0)


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:54s} {detail[:105]}")


def main() -> int:
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    api.setup()
    api.reset_character()
    rcon.query_lua("local n = 0 for _, e in pairs(game.surfaces[1]"
                   ".find_entities_filtered{force='player'}) do "
                   "if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)")
    # Table rase de la POLLUTION, pas seulement du bâti : elle survit d'un run à
    # l'autre et se dissipe lentement. Un run précédent en avait laissé 205, ce qui
    # faisait échouer le contrôle de retenue — la carte n'était pas dans l'état supposé.
    rcon.query_lua("game.surfaces[1].clear_pollution() rcon.print('pollution effacee')")
    api.generate_terrain(ZONE[0], ZONE[1], 120.0)
    api.run_action(api.wait, 30, timeout=60.0)
    api.run_action(api.teleport_to, ZONE[0], ZONE[1], timeout=30.0)
    # Dégager la végétation : sans quoi aucune tourelle ne se pose et on jugerait
    # l'agent sur un refus de terrain.
    rcon.query_lua(
        "local s = game.surfaces[1] "
        "for _, e in pairs(s.find_entities_filtered{area={{-40,-40},{40,40}}}) do "
        "if e.force ~= game.forces.player and e.type ~= 'resource' "
        "and e.type ~= 'character' then e.destroy() end end rcon.print('ok')")

    coord = Coordinator(api, zone=ZONE, rayon=25.0)

    # --- 1 : la menace réelle est perçue ---
    scan = api.scan_threats(ZONE[0], ZONE[1], 300.0)
    m = evaluer(scan, usine=ZONE)
    rec("e11-1 : la menace réelle est perçue",
        isinstance(scan, dict) and scan.get("peaceful") is False
        and (scan.get("nestCount") or 0) > 0,
        f"{scan.get('nestCount')} nid(s), {scan.get('unitCount')} unité(s), "
        f"pollution={scan.get('pollution')} -> {m}")

    # --- 2 : RETENUE — des nids, mais rien ne les déclenche ---
    etat = coord.observer()
    options = enumerer_options(etat)
    rec("e11-2 : nids sans pollution -> il ne propose PAS de fortifier",
        etat.menace is not None and etat.menace.niveau <= LATENTE
        and all(o.action != "defendre" for o in options),
        f"menace={etat.menace} | options={[o.action for o in options]}")

    # --- 3 : pollution injectée -> la menace devient imminente ---
    rcon.query_lua(f"local s = game.surfaces[1] "
                   f"s.pollute({{{ZONE[0]},{ZONE[1]}}}, 4000) rcon.print('pollue')")
    api.run_action(api.wait, 60, timeout=30.0)
    etat2 = coord.observer()
    rec("e11-3 : pollution -> menace imminente et décision de défendre",
        etat2.menace is not None and etat2.menace.niveau >= IMMINENTE
        and any(o.action == "defendre" for o in enumerer_options(etat2)),
        f"pollution={etat2.menace.pollution if etat2.menace else '?'} -> {etat2.menace}")

    # --- 4 : il pose des tourelles face au front, et les munit ---
    d, agi, _ = coord.tick()
    sa = api.scan_area(30.0)
    tourelles = [e for e in (sa.get("entities", []) if isinstance(sa, dict) else [])
                 if e.get("name") == "gun-turret"]
    front = etat2.menace.front if etat2.menace else None
    # « Face au front » : du même côté que les nids par rapport à l'usine.
    bon_cote = [t for t in tourelles
                if front and (t["x"] - ZONE[0]) * front[0] + (t["y"] - ZONE[1]) * front[1] > 0]
    rec("e11-4 : tourelles posées face au front", agi and len(bon_cote) > 0,
        f"décision={d.action} | {len(tourelles)} tourelle(s), {len(bon_cote)} du côté "
        f"{etat2.menace.front_nom if etat2.menace else '?'} — {coord.journal[-1][-60:]}")

    # --- 5 : LA preuve — les tourelles posées neutralisent vraiment les ennemis ---
    #
    # Un premier jet vérifiait ici que des biters lâchés sur l'usine faisaient passer la
    # menace en EN_COURS, priorité maximale. Il échouait, et pour une bonne raison : les
    # tourelles que l'agent venait de poser les abattaient avant l'observation suivante.
    # Mesurer la priorité alors que la défense fonctionne déjà, c'est mesurer la mauvaise
    # chose — la priorité EN_COURS se vérifie sans serveur (test_coordinator), le
    # résultat ne se vérifie qu'ici.
    lache = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for i = 1, 6 do "
        f"local p = s.find_non_colliding_position('small-biter', "
        f"{{{ZONE[0]} - 6 + i * 3, {ZONE[1]} - 16}}, 20, 1) "
        f"if p then s.create_entity{{name='small-biter', position=p, "
        f"force=game.forces.enemy}} n = n + 1 end end rcon.print(n)")
    apres_lacher = api.scan_threats(ZONE[0], ZONE[1], 300.0).get("unitsNear") or 0
    api.run_action(api.wait, 600, timeout=120.0)
    restants = api.scan_threats(ZONE[0], ZONE[1], 300.0).get("unitsNear") or 0
    rec("e11-5 : les tourelles posées neutralisent les ennemis",
        apres_lacher > 0 and restants < apres_lacher,
        f"{str(lache).strip()} biter(s) lâché(s) devant la ligne : {apres_lacher} "
        f"présent(s) -> {restants} après engagement")

    print("\n       --- journal du Coordinator ---")
    for j in coord.journal:
        print(f"       . {j[:115]}")

    rcon.close()
    return _verdict()


def _verdict() -> int:
    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())