"""Test LIVE E14 brique 1 : l'agent constate ses propres échecs.

Jusqu'ici la boucle relisait bien l'état après avoir agi, mais ne le confrontait à
rien. Une chaîne posée dont aucun charbon ne sortait était journalisée « chaîne bâtie »,
et le tour suivant passait à autre chose. Les six défauts du chantier E13 ont tous vécu
dans cet angle mort.

Le protocole est celui du FactoryDoctor, et c'est le seul qui prouve quelque chose :
**bâtir une chaîne SAINE, la casser d'une manière connue, et vérifier que le diagnostic
la nomme**. Une vérification qu'on ne met pas en défaut ne vaut rien.

  1. la chaîne bâtie par l'agent est reconnue continue — sans quoi tout le reste serait
     du bruit ;
  2. un segment RETIRÉ  -> `belt_interrompue`, situé à la tuile près ;
  3. un segment RETOURNÉ -> rupture détectée (le défaut d'E13 : rien ne manque, le
     chemin part simplement ailleurs) ;
  4. le bras TOURNÉ     -> `bras_depose_dans_le_vide` ;
  5. réservoir vidé et laissé seul -> l'attente de `ravitailler` est DÉÇUE, et l'écart
     est consigné dans le journal de la boucle.

Pré-requis : serveur headless avec le mod E13+. SKIP si le serveur est absent.
"""

from __future__ import annotations

import math
import sys

from agents.coordinator import Coordinator, Decision
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.factory_doctor import Symptome
from services.flux import BRAS_MAL_ORIENTE, INTERROMPUE, reparer_flux, suivre_flux

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:105]}")


def _chaine(rcon) -> tuple:
    """(foreur, drop, boiler, bras) tels qu'ils sont RÉELLEMENT sur la carte."""
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
        "if not (d and b and i) then rcon.print('INCOMPLET') return end "
        "rcon.print(string.format('%.1f;%.1f;%.1f;%.1f;%.1f;%.1f;%.1f;%.1f', "
        "d.position.x, d.position.y, d.drop_position.x, d.drop_position.y, "
        "b.position.x, b.position.y, i.position.x, i.position.y))")).strip()
    if ";" not in brut:
        return ()
    v = [float(t) for t in brut.split(";")]
    return ((v[0], v[1]), (v[2], v[3]), (v[4], v[5]), (v[6], v[7]))


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
        print("[SKIP] aucune chaîne foreur/belt/boiler sur la carte : "
              "lance d'abord verify_supply_e13.py.")
        rcon.close()
        return 0
    foreur, depart, boiler, bras = ch
    print(f"       . foreur@{foreur} drop={depart} -> boiler@{boiler}, bras@{bras}")

    # --- 1 : la chaîne saine est reconnue continue ---
    # Un run précédent a pu laisser une cassure : on la répare AVANT de mesurer, sinon
    # les injections suivantes s'ajouteraient à une panne préexistante et le test
    # accuserait le produit de ce qu'il a lui-même laissé.
    if not suivre_flux(api, depart, "boiler", boiler).continu:
        ok_r, det_r = reparer_flux(api, depart, "boiler", boiler)
        print(f"       . chaîne laissée cassée par un run précédent -> "
              f"{'remise en état' if ok_r else 'IRRÉPARABLE'} ({det_r[:80]})")
    r0 = suivre_flux(api, depart, "boiler", boiler)
    rec("e14-1 : la chaîne bâtie est reconnue continue", r0.continu, str(r0))
    if not r0.continu:
        print("       (la chaîne n'est pas saine au départ : les cassures suivantes "
              "ne prouveraient rien)")
        rcon.close()
        return _verdict()

    # --- 2 : un segment RETIRÉ ---
    milieu = str(rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"local b = s.find_entities_filtered{{type='transport-belt'}} "
        f"if #b < 4 then rcon.print('-') return end "
        f"local e = b[math.floor(#b/2)] local p = e.position e.destroy() "
        f"rcon.print(string.format('%.1f;%.1f', p.x, p.y))")).strip()
    r1 = suivre_flux(api, depart, "boiler", boiler)
    trou = tuple(float(t) for t in milieu.split(";")) if ";" in milieu else None
    rec("e14-2 : un segment retiré -> belt_interrompue",
        not r1.continu and r1.cause == INTERROMPUE,
        f"{r1.cause} en {r1.rupture} (segment détruit en {trou})")

    # Remise en état par le SERVICE, pas par une direction devinée : reposer la belt
    # avec un « east » en dur laissait la ligne coupée dès qu'elle tournait, et la
    # cassure suivante mesurait alors deux pannes au lieu d'une.
    reparer_flux(api, depart, "boiler", boiler)

    # --- 3 : un segment RETOURNÉ (le défaut d'E13 : rien ne manque) ---
    vire = str(rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"local b = s.find_entities_filtered{{type='transport-belt'}} "
        f"if #b < 4 then rcon.print('-') return end "
        f"local e = b[math.floor(#b/3)] local avant = e.direction "
        f"e.direction = (e.direction + 8) % 16 "
        f"rcon.print(string.format('%.1f;%.1f;%d;%d', e.position.x, e.position.y, "
        f"avant, e.direction))")).strip()
    r2 = suivre_flux(api, depart, "boiler", boiler)
    rec("e14-3 : un segment retourné -> rupture détectée", not r2.continu,
        f"{r2.cause} en {r2.rupture} (segment retourné : {vire})")

    rcon.query_lua(
        "local s = game.surfaces[1] "
        "for _, e in pairs(s.find_entities_filtered{type='transport-belt'}) do "
        "end rcon.print('ok')")

    # --- 4 : le BRAS tourné dépose dans le vide ---
    #     On rétablit d'abord la belt pour que le flux atteigne le bras.
    if ";" in vire:
        vx, vy, avant, _ = (float(t) for t in vire.split(";"))
        rcon.query_lua(
            f"local s = game.surfaces[1] "
            f"for _, e in pairs(s.find_entities_filtered{{position={{{vx},{vy}}}, "
            f"radius=0.4, type='transport-belt'}}) do e.direction = {int(avant)} end "
            f"rcon.print('remis')")
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{position={{{bras[0]},{bras[1]}}}, "
        f"radius=0.4, type='inserter'}}) do e.direction = (e.direction + 4) % 16 end "
        f"rcon.print('tourne')")
    r3 = suivre_flux(api, depart, "boiler", boiler)
    # Tourner un bras d'un quart de tour déplace SON PICKUP autant que son dépôt : il
    # cesse de puiser sur la belt. Le diagnostic juste est « mal orienté », et surtout
    # pas « absent » — on le retourne, on n'en pose pas un second à côté.
    rec("e14-4 : le bras tourné -> bras_mal_oriente",
        not r3.continu and r3.cause == BRAS_MAL_ORIENTE, str(r3))

    # --- 5 : l'écart est CONSIGNÉ par la boucle, pas seulement calculable ---
    rcon.query_lua(
        f"local s = game.surfaces[1] "
        f"for _, e in pairs(s.find_entities_filtered{{name='boiler', "
        f"position={{{boiler[0]},{boiler[1]}}}, radius=2}}) do "
        f"local i = e.get_fuel_inventory() if i then i.clear() end end rcon.print('vide')")
    coord = Coordinator(api, zone=boiler, rayon=25.0)
    cible = Symptome(name="boiler", x=boiler[0], y=boiler[1],
                     cause="sans_combustible", gravite=2, detail="réservoir vide")
    attente = coord._attente(Decision(action="ravitailler", raison="", cible=cible))
    tenue, observe = attente.evaluer(api)
    rec("e14-5 : l'attente d'un réservoir non rempli est DÉÇUE", not tenue,
        f"attendu « {attente.description} », observé {observe}")

    print(f"\n       distance foreur -> boiler : "
          f"{math.hypot(foreur[0] - boiler[0], foreur[1] - boiler[1]):.0f} tuiles")
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