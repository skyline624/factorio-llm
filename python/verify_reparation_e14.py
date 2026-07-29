"""Test LIVE E14c : l'agent RÉPARE ce qu'il diagnostique.

Diagnostiquer sans réparer laisse l'usine à l'arrêt avec un compte rendu détaillé. Cette
étape ferme la boucle : constater -> nommer -> remettre en marche -> vérifier que ça
marche.

Le protocole reste celui du FactoryDoctor, et le juge est le même qu'ailleurs : **la
mesure qui suit l'action, pas l'action**. Une réparation appliquée sans effet est un
échec, et c'est exactement le travers qui a produit la plupart des défauts du projet —
des poses acceptées sans erreur et sans conséquence.

Trois cassures, dont on connaît la réparation :
  1. un segment RETIRÉ      -> il faut en reposer un ;
  2. un segment RETOURNÉ    -> il faut le retourner, pas en ajouter (rien ne manque) ;
  3. le bras RETIRÉ         -> il faut en reposer un qui atteigne réellement la machine.

Et une garantie : après chaque réparation le flux doit être CONTINU, mesuré de la sortie
du foreur jusqu'à la machine.

Pré-requis : serveur headless et une chaîne bâtie (verify_supply_e13.py). SKIP sinon.
"""

from __future__ import annotations

import sys

from core.mod_api import ModApi
from core.rcon import get_rcon
from services.flux import reparer_flux, suivre_flux

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:50s} {detail[:104]}")


def _chaine(rcon) -> tuple:
    brut = str(rcon.query_lua(
        # Le foreur RETENU est le plus proche du boiler, pas le premier venu : plusieurs
        # chaînes peuvent coexister sur la carte, et suivre le flux d'un foreur vers un
        # boiler qui n'est pas le sien produit un « chemin qui tourne en rond » où rien
        # n'est cassé. Le test accuserait alors le produit d'un défaut qui est le sien.
        "local s = game.surfaces[1] local d, b, i = nil, nil, nil "
        "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do b = e end "
        "if not b then rcon.print('INCOMPLET') return end "
        "local md = 1e9 "
        "for _, e in pairs(s.find_entities_filtered{type='mining-drill'}) do "
        "local q = (e.position.x-b.position.x)^2 + (e.position.y-b.position.y)^2 "
        "if q < md then md = q d = e end end "
        "local mi = 1e9 "
        "for _, e in pairs(s.find_entities_filtered{type='inserter'}) do "
        "local q = (e.position.x-b.position.x)^2 + (e.position.y-b.position.y)^2 "
        "if q < mi then mi = q i = e end end "
        "if not (d and b) then rcon.print('INCOMPLET') return end "
        "rcon.print(string.format('%.1f;%.1f;%.1f;%.1f', "
        "d.drop_position.x, d.drop_position.y, b.position.x, b.position.y))")).strip()
    if ";" not in brut:
        return ()
    v = [float(t) for t in brut.split(";")]
    return ((v[0], v[1]), (v[2], v[3]))


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
    ch = _chaine(rcon)
    if not ch:
        print("[SKIP] aucune chaîne sur la carte : lance d'abord verify_supply_e13.py.")
        rcon.close()
        return 0
    depart, boiler = ch
    print(f"       . flux de {depart} vers boiler@{boiler}")

    if not suivre_flux(api, depart, "boiler", boiler).continu:
        ok_r, det_r = reparer_flux(api, depart, "boiler", boiler)
        print(f"       . chaîne laissée cassée par un run précédent -> "
              f"{'remise en état' if ok_r else 'IRRÉPARABLE'} ({det_r[:70]})")
    sain = suivre_flux(api, depart, "boiler", boiler)
    rec("e14c-0 : la chaîne de départ est saine", sain.continu, str(sain))
    if not sain.continu:
        print("       (rien à prouver en réparant une chaîne déjà cassée)")
        rcon.close()
        return _verdict()

    cassures = [
        ("segment retiré",
         "local s=game.surfaces[1] local b=s.find_entities_filtered{type='transport-belt'} "
         "local e=b[math.floor(#b/2)] local p=e.position e.destroy() "
         "rcon.print(string.format('%.1f,%.1f', p.x, p.y))"),
        ("segment retourné",
         "local s=game.surfaces[1] local b=s.find_entities_filtered{type='transport-belt'} "
         "local e=b[math.floor(#b/3)] e.direction=(e.direction+8)%16 "
         "rcon.print(string.format('%.1f,%.1f', e.position.x, e.position.y))"),
        ("bras retiré",
         "local s=game.surfaces[1] local p=nil "
         "for _,e in pairs(s.find_entities_filtered{type='inserter'}) do "
         "p=e.position e.destroy() end "
         "rcon.print(p and string.format('%.1f,%.1f', p.x, p.y) or 'aucun')"),
    ]

    for nom, casser in cassures:
        ou = str(rcon.query_lua(casser)).strip()
        avant = suivre_flux(api, depart, "boiler", boiler)
        if avant.continu:
            rec(f"e14c : « {nom} »", False,
                f"la cassure en {ou} n'a pas rompu le flux — rien à réparer")
            continue
        ok, detail = reparer_flux(api, depart, "boiler", boiler)
        apres = suivre_flux(api, depart, "boiler", boiler)
        rec(f"e14c : « {nom} » -> réparée et flux rétabli", ok and apres.continu,
            f"cassé en {ou} ({avant.cause}) -> {detail}")

    print(f"\n       état final : {suivre_flux(api, depart, 'boiler', boiler)}")
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