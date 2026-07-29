"""Partie longue : la boucle tourne seule, et l'on mesure ce qu'on a décidé de mesurer.

Tout ce que le projet a validé jusqu'ici l'a été sur des tours de quelques minutes, avec
une panne injectée dont on connaissait la réponse. C'est la bonne méthode pour éprouver
un mécanisme ; elle ne dit rien de deux questions qui ne se posent que dans la durée :

  - **la boucle tient-elle ?** Deux heures sans se bloquer, sans épuiser son inventaire,
    sans tourner en rond sur un problème qu'elle ne sait pas résoudre ;
  - **l'arbitre vaut-il quelque chose ?** Il ne diverge que lorsque plusieurs options se
    valent VRAIMENT — menace imminente pendant qu'une machine est en panne et qu'il reste
    à produire. Ces situations n'existent pas sur une carte vierge en cinq minutes.

Deux partis pris, tirés des chantiers précédents :

**Les critères sont fixés AVANT de lancer.** Sans cela on regarde tourner et l'on ajuste
sa conclusion au résultat. Ce qui est mesuré ici : l'usine tient-elle debout, produit-elle
encore, combien d'écarts constatés, combien réparés, combien de causes restées `inconnu`,
et combien de fois la boucle a cessé de progresser.

**Le jeu est accéléré, pas le modèle.** `game.speed` fait défiler Factorio jusqu'à ×16 sur
cette machine, mais une enquête LLM prend le même temps réel quelle que soit la vitesse.
Sur une partie chargée en incidents, c'est le modèle qui décide de la durée, pas le jeu.

Usage :
    python run_partie_longue.py [minutes_de_jeu] [--vitesse N] [--ombre]

Le journal est écrit en continu (JSONL) : il se lit PENDANT que la partie tourne, ce qui
est le seul moyen de surveiller sans interrompre.
"""

from __future__ import annotations

import sys
import time

from agents.coordinator import Coordinator
from core.mod_api import ModApi
from core.rcon import get_rcon
from services import perception
from services.journal import Journal

# Au-delà, le CPU ne suit plus : mesuré, `game.speed=30` ne rend que ×16.6 sur une carte
# quasi vide, et moins encore avec une usine. Annoncer 30 ne ferait qu'un chiffre faux.
VITESSE_DEFAUT = 10.0
MINUTES_DEFAUT = 30.0


def _tick(api) -> int:
    t = api.get_tick()
    return int(t.get("tick", 0)) if isinstance(t, dict) else 0


def _mesures(api, coord) -> dict:
    """La photographie chiffrée d'un tour. C'est la SÉRIE qui informe, pas le point."""
    sf = api.scan_factory() or {}
    lignes = sf.get("entities") or []
    en_marche = sum(1 for e in lignes if e.get("status") in ("working", "normal"))
    inv = perception.inventory(api)
    return {
        "machines": len(lignes),
        "en_marche": en_marche,
        "arretees": len(lignes) - en_marche,
        "ecarts": len(coord.ecarts),
        "constats": len(coord.constats),
        "inconnus": sum(1 for c in coord.constats
                        if getattr(c, "cause", "") == "inconnu"),
        "coal": inv.get("coal", 0),
        "iron_plate": inv.get("iron-plate", 0),
    }


def main(argv: list[str]) -> int:
    minutes = MINUTES_DEFAUT
    vitesse = VITESSE_DEFAUT
    ombre = "--ombre" in argv
    positionnels = [a for a in argv[1:] if not a.startswith("--")]
    if positionnels:
        try:
            minutes = float(positionnels[0])
        except ValueError:
            pass
    if "--vitesse" in argv:
        try:
            vitesse = float(argv[argv.index("--vitesse") + 1])
        except (IndexError, ValueError):
            pass

    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        api = ModApi(rcon)
        api.can_place_check("transport-belt", 0.5, 0.5, "north")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    api.set_test_mode(True)
    api.setup()
    graine = rcon.query_lua("rcon.print(game.surfaces[1].map_gen_settings.seed)")
    horodatage = time.strftime("%Y%m%d-%H%M%S")
    chemin = f"logs/partie-{horodatage}.jsonl"
    jr = Journal(chemin)

    pos = (api.get_state().get("character") or {}).get("position") or {}
    zone = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))
    coord = Coordinator(api, zone=zone, rayon=30.0, ombre=ombre)

    ticks_vises = int(minutes * 60 * 60)
    t0 = _tick(api)
    jr.ecrire("depart", seed=str(graine).strip(), zone=list(zone), vitesse=vitesse,
              minutes_visees=minutes, tick=t0, ombre=ombre,
              arbitre=coord.arbitre is not None, enqueteur=coord.enqueteur is not None)
    print(f"       partie longue : {minutes:.0f} min de jeu à ×{vitesse:.0f} "
          f"(~{minutes / vitesse:.0f} min réelles si le modèle ne ralentit pas)")
    print(f"       seed {str(graine).strip()} | zone {zone} | "
          f"ombre={'oui' if ombre else 'non'} | journal {chemin}")

    rcon.query_lua(f"game.speed = {vitesse} rcon.print('ok')")
    tour, bloques, derniere_action = 0, 0, ""
    try:
        while _tick(api) - t0 < ticks_vises:
            tour += 1
            t = _tick(api)
            try:
                d, agi, _ = coord.tick()
            except Exception as e:
                # Une boucle autonome ne s'arrête pas sur une exception : on la CONSIGNE
                # et on continue. C'est précisément ce qu'une partie longue doit révéler.
                jr.ecrire("exception", tour=tour, tick=t, type=type(e).__name__,
                          message=str(e)[:300])
                bloques += 1
                if bloques >= 5:
                    jr.ecrire("arret", raison="5 exceptions consécutives", tour=tour)
                    break
                continue
            jr.tour(tour, t, d, agi, coord.journal[-1] if coord.journal else "")
            for e in coord.ecarts[-2:]:
                jr.ecart(t, e)
            for c in coord.constats[-2:]:
                jr.constat(t, c)
            jr.mesure(t, tour=tour, **_mesures(api, coord))

            # « Ne progresse plus » : la même action échoue en boucle. On le compte au
            # lieu de s'arrêter — c'est une donnée de la partie, pas une panne du runner.
            if not agi and d.action == derniere_action:
                bloques += 1
            else:
                bloques = 0
            derniere_action = d.action
            if d.action == "rien":
                # L'usine tourne : on laisse le jeu avancer plutôt que de re-décider en
                # boucle. C'est là que la pollution monte et que les vagues arrivent.
                api.run_action(api.wait, 600, timeout=120.0)
    except KeyboardInterrupt:
        jr.ecrire("arret", raison="interruption manuelle", tour=tour)
    finally:
        rcon.query_lua("game.speed = 1 rcon.print('ok')")

    m = _mesures(api, coord)
    jr.ecrire("fin", tour=tour, ticks=_tick(api) - t0, **m)
    if coord.arbitre is not None and hasattr(coord.arbitre, "divergences"):
        a = coord.arbitre
        jr.ecrire("arbitre", accords=a.accords, divergences=len(a.divergences),
                  taux=round(a.taux_divergence, 3))

    print(f"\n       {tour} tour(s), {(_tick(api) - t0) / 3600:.1f} min de jeu")
    print(f"       usine : {m['machines']} machine(s), {m['en_marche']} en marche, "
          f"{m['arretees']} arrêtée(s)")
    print(f"       écarts : {m['ecarts']} constaté(s), {m['constats']} enquête(s) "
          f"dont {m['inconnus']} sans conclusion")
    print(f"       {jr.resume()}")
    print(f"\n       journal : {chemin}")
    rcon.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))