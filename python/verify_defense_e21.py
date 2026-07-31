"""Test LIVE E21c : une défense qui ne mène à rien finit par céder la place.

Mesuré en partie longue : 936 tours sur 952 passés à redécider `defendre`, dont 933 sans
rien faire — tous sur le même motif, « aucune position de tourelle libre au nord ». Sept
tourelles étaient posées, les nids à 290 tuiles, et il n'y avait plus rien à tenter.

Le mécanisme d'abandon existait pourtant, et c'est ce qui rend la panne intéressante :
`tick` COMPTAIT les échecs de `defendre` — le journal affichait « ABANDON de defendre
après 3 échecs » — mais `enumerer_options` ne LISAIT ce compteur que pour les réparations
et les constructions. La mémoire était écrite et jamais relue ; l'option repartait
faisable au tour suivant, indéfiniment.

Ce qu'on vérifie, sur une menace RÉELLE et non simulée :
  1. des nids à portée produisent bien une menace, et `defendre` prend la tête ;
  2. après trois échecs, l'option devient infaisable et cesse d'être choisie ;
  3. l'agent DIT alors qu'il n'y a rien à faire, au lieu de retenter la même chose ;
  4. l'abandon se LÈVE quand le compteur retombe — un échec passager ne doit pas devenir
     un renoncement définitif.

Les trois échecs sont inscrits dans le compteur plutôt que provoqués par le terrain :
saturer les emplacements de tourelles demanderait de reconstruire la configuration exacte
d'une partie de 952 tours. Ce qui est éprouvé ici est la BOUCLE — que l'agent lâche prise
et passe à autre chose — le comptage lui-même l'étant hors ligne (test_coordinator).

Pré-requis : serveur headless + référence figée. SKIP si l'un manque.

Lancement :
    cd python
    python verify_defense_e21.py
"""

from __future__ import annotations

import sys

from agents.coordinator import SEUIL_ABANDON, Coordinator, decide, enumerer_options
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import save_ref

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:54s} {detail[:105]}")


def _semer_des_nids(api, rcon, x: float, y: float, combien: int = 4) -> int:
    """Replante des nids au nord de l'usine, à portée de détection.

    Les préparations de carte rasent tout ce qui n'appartient pas au joueur — nids
    compris — dans leur rayon de dégagement. Une partie mesurée après un dégagement large
    n'a donc AUCUNE menace, et l'on prendrait l'absence de `defendre` pour une réussite
    du garde-fou. Il faut donc remettre la menace avant de prétendre l'éprouver.
    """
    api.generate_terrain(x, y, 60.0)
    api.run_action(api.wait, 60, timeout=60.0)
    out = rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for i = 1, {combien} do "
        f"  local p = s.find_non_colliding_position('biter-spawner', "
        f"{{{x} + i * 6, {y}}}, 40, 1) "
        f"  if p and s.create_entity{{name='biter-spawner', position=p, "
        f"force=game.forces.enemy}} then n = n + 1 end end rcon.print(n)")
    try:
        return int(str(out).strip())
    except ValueError:
        return 0


def main() -> int:
    ok_ref, motif = save_ref.restaurer_reference()
    if not ok_ref:
        print(f"[SKIP] pas d'état de référence ({motif}).")
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
    # x1 hérité de la référence : voir la même note dans verify_gisement_e21. Ce banc
    # était le plus exposé — 22 s à l'origine, 761 s une fois que l'agent s'est mis à
    # fondre du minerai entre deux décisions.
    rcon.query_lua("game.speed = 30 rcon.print(1)")
    pos = (api.get_state().get("character") or {}).get("position") or {}
    zone = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))

    # La menace doit exister AVANT qu'on prétende l'éprouver.
    semes = _semer_des_nids(api, rcon, zone[0], zone[1] - 280.0)

    # Des nids ne suffisent pas : ce qui les réveille est la POLLUTION, et une carte
    # fraîchement restaurée en compte à peine. Mesuré, le test partait en SKIP sur
    # « 4 nids semés, mais pollution 7 < 10 : rien ne les déclenche encore » — une
    # menace bien présente mais endormie, donc rien à éprouver. On pollue donc la zone
    # comme le ferait une usine qui tourne, ce qui est exactement la cause que le jeu
    # attend. Sans cela, on mesurerait un garde-fou sur une carte sans danger et le
    # chiffre serait flatteur.
    rcon.query_lua(f"game.surfaces[1].pollute({{{zone[0]},{zone[1]}}}, 400) "
                   f"rcon.print('ok')")
    api.run_action(api.wait, 120, timeout=60.0)

    coord = Coordinator(api, zone=zone, rayon=25.0)
    etat = coord.observer()
    menace = etat.menace
    if semes == 0 or menace is None or not menace.agir:
        print(f"[SKIP] aucune menace obtenue ({semes} nid(s) semé(s), menace={menace}).")
        rcon.query_lua("game.speed = 1 rcon.print(1)")
        rcon.close()
        return 0
    print(f"       {semes} nid(s) semé(s) à 280 tuiles au nord — {menace}")

    # --- 1 : la défense prend la tête ---
    options = enumerer_options(etat)
    d = decide(etat)
    defense = next((o for o in options if o.action == "defendre"), None)
    rec("e21c-1 : une menace réelle met la défense en tête",
        d.action == "defendre" and defense is not None and defense.faisable,
        f"{d.action} parmi {[(o.action, o.priorite) for o in options]}")

    # --- 2 : après trois échecs, elle cesse d'être choisie ---
    # Équivaut à trois poses ratées d'affilée : c'est ce que `tick` inscrit dans cette
    # même mémoire quand l'action n'aboutit pas.
    coord._echecs[("defendre", "", 0, 0)] = SEUIL_ABANDON
    etat2 = coord.observer()
    options2 = enumerer_options(etat2)
    defense2 = next((o for o in options2 if o.action == "defendre"), None)
    rec("e21c-2 : après trois échecs, la défense est déclassée",
        defense2 is not None and not defense2.faisable and defense2.priorite == 0,
        f"faisable={defense2.faisable if defense2 else None}, "
        f"priorite={defense2.priorite if defense2 else None}")

    # --- 3 : et l'agent DIT qu'il n'y a rien à faire ---
    d2 = decide(etat2)
    rec("e21c-3 : il cesse de s'acharner et le dit", d2.action != "defendre",
        f"{d2.action} — {d2.raison[:70]}")

    # --- 4 : l'abandon se lève ---
    coord._echecs.pop(("defendre", "", 0, 0), None)
    etat3 = coord.observer()
    d3 = decide(etat3)
    rec("e21c-4 : l'abandon se lève dès que le compteur retombe",
        d3.action == "defendre",
        f"{d3.action} — un succès rend la défense à nouveau prioritaire")

    # --- 5 : la boucle réelle ne s'acharne pas non plus ---
    # Le test précédent porte sur `decide` (pur) ; celui-ci sur `tick`, qui observe,
    # décide, agit et RÉÉCRIT la mémoire. C'est là que l'ancien code bouclait.
    coord._echecs[("defendre", "", 0, 0)] = SEUIL_ABANDON
    actions = []
    for _ in range(4):
        dd, agi, _ = coord.tick()
        actions.append(dd.action)
    rec("e21c-5 : quatre tours de boucle réelle sans retomber sur la défense",
        actions.count("defendre") == 0, f"actions : {actions}")

    print("\n       --- journal du Coordinator ---")
    for j in coord.journal[-5:]:
        print(f"       . {j[:115]}")

    print("\n" + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    rcon.query_lua("game.speed = 1 rcon.print(1)")
    rcon.close()
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
