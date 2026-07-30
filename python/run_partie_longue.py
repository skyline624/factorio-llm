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

**Le point de départ est CONTRÔLÉ ou il n'est pas comparable.** Chaque test laisse la
carte dans l'état où il l'a mise ; deux parties du même code ne mesuraient donc pas la
même chose. `--depuis-reference` remet le serveur dans l'état figé au préalable
(`python -m services.save_ref figer`). Sans cela, on compare des parties qui n'ont pas
commencé au même endroit — et l'écart qu'on lit est du bruit, non un effet du modèle.

**Un agent qui VISE n'est pas un agent qui maintient.** `--objectif N` fixe un débit en
items/s : sous ce débit, l'usine « qui tourne » ne suffit plus et l'agent l'agrandit.
Sans l'option, il maintient l'existant — le comportement d'avant, conservé tel quel.

Usage :
    python run_partie_longue.py [minutes_de_jeu] [--vitesse N] [--ombre]
                                [--depuis-reference] [--objectif ITEMS_PAR_S]

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
from services.save_ref import restaurer_reference

# Au-delà, le CPU ne suit plus : mesuré, `game.speed=30` ne rend que ×16.6 sur une carte
# quasi vide, et moins encore avec une usine. Annoncer 30 ne ferait qu'un chiffre faux.
VITESSE_DEFAUT = 10.0
MINUTES_DEFAUT = 30.0


def _tick(api) -> int:
    t = api.get_tick()
    return int(t.get("tick", 0)) if isinstance(t, dict) else 0


def _produites(rcon, item: str = "iron-plate") -> int:
    """Ce que l'usine a RÉELLEMENT produit depuis le début, où que ce soit parti.

    L'inventaire du personnage ne mesurait la production que par accident : il montait
    parce que l'agent vidait les machines À LA MAIN, et chaque vidage y versait cent
    plaques. Dès qu'un ramassage automatique existe, la production part au coffre et
    l'indicateur se fige — on lirait « l'usine ne produit plus » au moment précis où
    elle devient autonome. La statistique du jeu, elle, ne dépend pas de la destination.
    """
    try:
        return int(str(rcon.query_lua(
            "local s = game.forces.player.get_item_production_statistics(game.surfaces[1]) "
            f"rcon.print(s.get_input_count('{item}'))")).strip())
    except (ValueError, TypeError, AttributeError):
        return -1


def _mesures(api, coord, rcon=None) -> dict:
    """La photographie chiffrée d'un tour. C'est la SÉRIE qui informe, pas le point.

    Les organes PASSIFS sont comptés à part. Mesuré : « 20 machines dont 17 arrêtées »
    étaient 16 poteaux électriques (`status=n/a`, ils n'ont pas d'état de marche) et un
    foreur — le chiffre alarmant ne décrivait rien. Un poteau qui ne « tourne » pas est
    normal ; un four qui ne tourne pas est une panne. Les confondre fait lire une usine
    en ruine là où trois machines sur quatre vont bien, et l'inverse quand elles vont mal.
    """
    sf = api.scan_factory() or {}
    toutes = sf.get("entities") or []
    # `n/a` est ce que rend le mod pour ce qui n'a pas d'état de marche : poteaux, coffres,
    # belts. On mesure les machines, pas le mobilier.
    lignes = [e for e in toutes if e.get("status") not in (None, "n/a")]
    en_marche = sum(1 for e in lignes if e.get("status") in ("working", "normal"))
    inv = perception.inventory(api)
    return {
        "machines": len(lignes),
        "passifs": len(toutes) - len(lignes),
        "en_marche": en_marche,
        "arretees": len(lignes) - en_marche,
        "ecarts": len(coord.ecarts),
        "constats": len(coord.constats),
        "inconnus": sum(1 for c in coord.constats
                        if getattr(c, "cause", "") == "inconnu"),
        "coal": inv.get("coal", 0),
        # Gardé : c'est ce que l'agent a SOUS LA MAIN, donc ce qu'il peut dépenser.
        "iron_plate": inv.get("iron-plate", 0),
        # Le débit que l'agent a lui-même mesuré, et l'objectif qu'il vise. C'est la
        # SÉRIE qui dit si une extension a servi ; le point ne dit rien.
        # `is not None` et non la véracité : un débit de 0.0 est une MESURE — l'usine est
        # à l'arrêt — et le tester comme un booléen le transformait en « non mesuré ».
        # Sixième fois dans ce chantier qu'un zéro se fait passer pour une absence.
        "debit": (round(coord._debit, 3)
                  if getattr(coord, "_debit", None) is not None else None),
        "objectif": getattr(coord, "objectif_par_s", None),
        # Ce que l'usine a produit — la seule des deux qui mesure l'usine.
        "produites": _produites(rcon) if rcon is not None else -1,
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
    objectif = None
    if "--objectif" in argv:
        try:
            objectif = float(argv[argv.index("--objectif") + 1])
        except (IndexError, ValueError):
            objectif = None

    # La restauration vient AVANT toute connexion : elle arrête le serveur, remet la save
    # en place et le relance. Un client obtenu plus tôt pointerait sur un processus mort.
    depuis_reference = "--depuis-reference" in argv
    if depuis_reference:
        print("       restauration de l'état de référence (arrêt, copie, relance)...")
        t0 = time.time()
        ok, motif = restaurer_reference()
        print(f"       {motif} ({time.time() - t0:.0f}s)")
        if not ok:
            # On ABANDONNE au lieu de partir quand même : une partie lancée depuis un
            # état inconnu produit des chiffres qu'on croira comparables et qui ne le
            # sont pas. Mieux vaut pas de mesure qu'une mesure trompeuse.
            print("       -> partie non lancée : l'état de départ n'est pas celui voulu.")
            print("          Figer une référence d'abord : "
                  "python -m services.save_ref figer")
            return 1

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

    # La zone est le BARYCENTRE des machines existantes, pas la position du personnage :
    # celle-ci dépend du dernier test lancé, et l'on mesurait une partie sur un terrain
    # vide pendant que l'usine tournait ailleurs.
    sf0 = api.scan_factory() or {}
    machines0 = sf0.get("entities") or []
    if machines0:
        zone = (sum(float(e["x"]) for e in machines0) / len(machines0),
                sum(float(e["y"]) for e in machines0) / len(machines0))
    else:
        pos = (api.get_state().get("character") or {}).get("position") or {}
        zone = (float(pos.get("x", 0.0)), float(pos.get("y", 0.0)))
    # Le monde est figé pendant que le modèle réfléchit. Sans cela, une partie « avec
    # modèle » se compare à une partie « sans » comme un agent lent à un agent rapide :
    # mesuré, un appel coûte cinq secondes réelles, soit trois mille ticks de jeu à ×10,
    # et douze appels emportent le tiers d'une partie de trente minutes.
    coord = Coordinator(api, zone=zone, rayon=30.0, ombre=ombre,
                        objectif_par_s=objectif, pause_reflexion=True)

    ticks_vises = int(minutes * 60 * 60)
    t0 = _tick(api)
    # `depuis_reference` est consigné : deux journaux ne se comparent que si l'on sait
    # lequel est parti d'un état contrôlé.
    jr.ecrire("depart", seed=str(graine).strip(), zone=list(zone), vitesse=vitesse,
              minutes_visees=minutes, tick=t0, ombre=ombre,
              depuis_reference=depuis_reference,
              arbitre=coord.arbitre is not None, enqueteur=coord.enqueteur is not None)
    print(f"       partie longue : {minutes:.0f} min de jeu à ×{vitesse:.0f} "
          f"(~{minutes / vitesse:.0f} min réelles si le modèle ne ralentit pas)")
    print(f"       seed {str(graine).strip()} | zone {zone} | "
          f"ombre={'oui' if ombre else 'non'} | journal {chemin}")

    rcon.query_lua(f"game.speed = {vitesse} rcon.print('ok')")
    tour, bloques, derniere_action = 0, 0, ""
    mesures_vues: list[dict] = []
    arbitrables, appels, divergences = 0, 0, 0
    vus_arbitrages: list = []
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
            a = getattr(d, "arbitrage", None)
            if a is not None:
                arbitrables += 1 if a.arbitrable else 0
                appels += 1 if a.appele else 0
                divergences += 1 if a.diverge else 0
            # Les arbitrages ANNEXES (choix du gisement) comptent autant : les omettre
            # ferait conclure que le modèle n'a jamais eu la parole.
            for _, n_opt, indice in coord.arbitrages[len(vus_arbitrages):]:
                arbitrables += 1 if n_opt >= 2 else 0
                appels += 1
                divergences += 1 if indice != 0 else 0
            vus_arbitrages = list(coord.arbitrages)
            for e in coord.ecarts[-2:]:
                jr.ecart(t, e)
            for c in coord.constats[-2:]:
                jr.constat(t, c)
            mes = _mesures(api, coord, rcon)
            jr.mesure(t, tour=tour, **mes)
            # Gardées en mémoire pour le bilan : la SÉRIE dit ce qu'un point final ne dit
            # pas, notamment quand le dernier tour n'a pas de débit mesurable.
            mesures_vues.append(dict(mes, tour=tour))

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

    m = _mesures(api, coord, rcon)
    jr.ecrire("fin", tour=tour, ticks=_tick(api) - t0,
              arbitrables=arbitrables, appels=appels, divergences=divergences, **m)
    if coord.arbitre is not None and hasattr(coord.arbitre, "divergences"):
        a = coord.arbitre
        # Les REPLIS sont consignés à part : un modèle qui n'a pas pu se prononcer était
        # compté comme d'accord, donc un modèle injoignable se lisait « le modèle valide
        # le déterministe ». Sans ce chiffre, aucun taux d'accord n'est interprétable.
        jr.ecrire("arbitre", accords=a.accords, divergences=len(a.divergences),
                  replis=getattr(a, "replis", 0),
                  incidents=len(getattr(a, "incidents", []) or []),
                  taux=round(a.taux_divergence, 3))

    print(f"\n       {tour} tour(s), {(_tick(api) - t0) / 3600:.1f} min de jeu")
    print(f"       usine : {m['machines']} machine(s), {m['en_marche']} en marche, "
          f"{m['arretees']} arrêtée(s) — plus {m['passifs']} organe(s) passif(s)")
    print(f"       production : {m['produites']} iron-plate depuis le début "
          f"(dont {m['iron_plate']} en inventaire)")
    if objectif is not None:
        # Le débit du DERNIER tour peut être « non mesuré » : la fenêtre de 600 ticks n'est
        # pas toujours atteinte quand l'agent enchaîne les actions. Prendre cette valeur
        # afficherait « objectif : None » après trente minutes de production. On rend donc
        # le dernier débit CONNU, et l'on dit à quel tour il a été relevé.
        connus = [(d.get("tour"), d.get("debit")) for d in mesures_vues
                  if d.get("debit") is not None]
        tour_debit, dernier = connus[-1] if connus else (None, None)
        atteint = dernier is not None and dernier >= objectif * 0.9
        print(f"       objectif : {dernier} {'≥' if atteint else '<'} {objectif} "
              f"iron-plate/s — {'TENU' if atteint else 'non tenu'}"
              + (f" (dernière mesure au tour {tour_debit})" if tour_debit else ""))
        if connus:
            pointe = max(d for _, d in connus)
            print(f"       débit maximal atteint : {pointe} iron-plate/s")
    print(f"       écarts : {m['ecarts']} constaté(s), {m['constats']} enquête(s) "
          f"dont {m['inconnus']} sans conclusion")
    # LE chiffre qui décide si une comparaison avec/sans modèle a un sens.
    part = (100.0 * arbitrables / tour) if tour else 0.0
    # En mode OMBRE, l'indice rendu est toujours 0 par construction : le déterministe
    # garde la main. Compter les divergences sur cet indice donnait donc invariablement
    # zéro, y compris quand le modèle avait proposé autre chose — mesuré, l'arbitre en
    # comptait une pendant que cette ligne affichait « 0 divergence ». La source de vérité
    # est l'arbitre lui-même dès qu'il en tient le compte.
    if coord.arbitre is not None and hasattr(coord.arbitre, "divergences"):
        divergences = len(coord.arbitre.divergences)
    print(f"       arbitrage : {arbitrables} tour(s) à VRAI choix sur {tour} "
          f"({part:.0f} %), {appels} appel(s) au modèle, {divergences} divergence(s)")
    if coord.arbitre is not None and hasattr(coord.arbitre, "accords"):
        a = coord.arbitre
        replis = getattr(a, "replis", 0)
        prononces = a.accords + len(a.divergences)
        print(f"       le modèle s'est PRONONCÉ {prononces} fois "
              f"({a.accords} d'accord, {len(a.divergences)} en désaccord) et n'a rien "
              f"pu dire {replis} fois")
        if replis and not prononces:
            print("       -> aucun avis exploitable : ne rien conclure de ce taux.")
    if arbitrables == 0:
        print("       -> aucun arbitrage possible : comparer avec et sans modèle ne "
              "mesurerait rien. C'est le nombre d'options qu'il faut traiter d'abord.")
    print(f"       {jr.resume()}")
    print(f"\n       journal : {chemin}")
    rcon.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))