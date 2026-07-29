"""Banc d'essai LIVE de l'Enquêteur : six pannes dont on connaît la réponse.

Le protocole est celui du FactoryDoctor, et c'est le seul qui prouve quelque chose :
**casser une chaîne SAINE d'une manière connue, et vérifier que le diagnostic la
nomme**. Les six pannes injectées ici ne sont pas imaginées — ce sont exactement celles
rencontrées pendant le chantier E13, et chacune avait alors coûté une enquête à la main.

Ce qui est mesuré, et annoncé AVANT de lancer :

  - **4 causes correctes sur 6** valide l'approche. En dessous, on le dit et l'on en
    tire les conséquences plutôt que de garder un composant qui produit des conclusions
    plausibles et fausses ;
  - le **coût** : combien de mesures par enquête ;
  - le taux d'**inconnu**, qui n'est pas un échec. Un modèle qui dit « je ne sais pas »
    laisse la boucle informée ; un modèle qui invente une cause la ferait réparer un
    problème inexistant. Les deux sont comptés séparément.

Une réponse est jugée correcte si la cause nommée figure parmi celles ATTENDUES pour
cette panne — plusieurs formulations peuvent être justes (un bras qu'on retourne cesse
de puiser : « bras mal orienté » et « bras absent » décrivent tous deux la réalité).

Pré-requis : serveur headless, une chaîne bâtie (lance verify_supply_e13.py avant), et
Ollama joignable. SKIP sinon.
"""

from __future__ import annotations

import sys

from agents.coordinator import Ecart
from agents.enqueteur import Enqueteur
from core.mod_api import ModApi
from core.rcon import get_rcon
from services.factory_doctor import Symptome

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:46s} {detail[:104]}")


def _chaine(rcon) -> tuple:
    brut = str(rcon.query_lua(
        "local s = game.surfaces[1] local d, b, i = nil, nil, nil "
        "for _, e in pairs(s.find_entities_filtered{type='mining-drill'}) do d = e end "
        "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do b = e end "
        "for _, e in pairs(s.find_entities_filtered{type='inserter'}) do i = e end "
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
        print("[SKIP] aucune chaîne sur la carte : lance d'abord verify_supply_e13.py.")
        rcon.close()
        return 0
    foreur, depart, boiler, bras = ch

    enqueteur = Enqueteur()
    if enqueteur._client is None:
        print(f"[SKIP] aucun modèle joignable ({enqueteur.journal}).")
        rcon.close()
        return 0
    print(f"       . foreur@{foreur} -> boiler@{boiler}, bras@{bras}")
    print(f"       . modèle : {getattr(enqueteur.cfg, 'openai_model', '?')}\n")

    cible = Symptome(name="boiler", x=boiler[0], y=boiler[1], cause="sans_combustible",
                     gravite=2, detail="réservoir vide")

    def _ecart(observe: str) -> Ecart:
        # Le point de départ du flux est ce que la boucle SAIT : le lui cacher
        # obligerait l'enquête à le redécouvrir, et fausserait la mesure du coût.
        return Ecart("approvisionner", "le charbon atteint le boiler par la chaîne",
                     observe, cible, {"depart_du_flux": list(depart)})

    # Chaque panne : (nom, Lua qui casse, Lua qui répare, causes acceptables).
    # Les causes acceptables sont PLURIELLES quand la réalité l'est : retourner un bras
    # le rend à la fois « mal orienté » et, du point de vue du flux, introuvable.
    pannes = [
        ("segment retiré",
         "local s=game.surfaces[1] local b=s.find_entities_filtered{type='transport-belt'} "
         "local e=b[math.floor(#b/2)] local p=e.position "
         "storage.fl_trou={x=p.x, y=p.y, d=e.direction} e.destroy() rcon.print('casse')",
         "local s=game.surfaces[1] local t=storage.fl_trou "
         "if t then s.create_entity{name='transport-belt', position={t.x,t.y}, "
         "direction=t.d, force='player'} end rcon.print('repare')",
         {"belt_interrompue"}),

        ("segment retourné",
         "local s=game.surfaces[1] local b=s.find_entities_filtered{type='transport-belt'} "
         "local e=b[math.floor(#b/3)] storage.fl_vire={x=e.position.x, y=e.position.y, "
         "d=e.direction} e.direction=(e.direction+8)%16 rcon.print('casse')",
         "local s=game.surfaces[1] local t=storage.fl_vire if t then "
         "for _,e in pairs(s.find_entities_filtered{position={t.x,t.y}, radius=0.4, "
         "type='transport-belt'}) do e.direction=t.d end end rcon.print('repare')",
         {"belt_mal_orientee", "belt_interrompue"}),

        ("bras retourné",
         f"local s=game.surfaces[1] "
         f"for _,e in pairs(s.find_entities_filtered{{position={{{bras[0]},{bras[1]}}}, "
         f"radius=0.4, type='inserter'}}) do storage.fl_bras=e.direction "
         f"e.direction=(e.direction+4)%16 end rcon.print('casse')",
         f"local s=game.surfaces[1] "
         f"for _,e in pairs(s.find_entities_filtered{{position={{{bras[0]},{bras[1]}}}, "
         f"radius=0.4, type='inserter'}}) do if storage.fl_bras then "
         f"e.direction=storage.fl_bras end end rcon.print('repare')",
         {"bras_mal_oriente", "bras_absent", "bras_depose_dans_le_vide"}),

        ("bras retiré",
         f"local s=game.surfaces[1] "
         f"for _,e in pairs(s.find_entities_filtered{{position={{{bras[0]},{bras[1]}}}, "
         f"radius=0.4, type='inserter'}}) do storage.fl_ins={{n=e.name, d=e.direction}} "
         f"e.destroy() end rcon.print('casse')",
         f"local s=game.surfaces[1] local t=storage.fl_ins if t then "
         f"s.create_entity{{name=t.n, position={{{bras[0]},{bras[1]}}}, direction=t.d, "
         f"force='player'}} end rcon.print('repare')",
         {"bras_absent", "bras_mal_oriente"}),

        ("réservoir vidé et foreur à l'arrêt",
         f"local s=game.surfaces[1] "
         f"for _,e in pairs(s.find_entities_filtered{{name='boiler', "
         f"position={{{boiler[0]},{boiler[1]}}}, radius=2}}) do "
         f"local i=e.get_fuel_inventory() if i then i.clear() end end "
         f"for _,d in pairs(s.find_entities_filtered{{type='mining-drill'}}) do "
         f"local i=d.get_fuel_inventory() if i then i.clear() end end rcon.print('casse')",
         "rcon.print('rien a reparer')",
         {"combustible_epuise"}),
        ("minerai retiré sous le foreur",
         f"local s=game.surfaces[1] local n=0 "
         f"for _,e in pairs(s.find_entities_filtered{{area={{{{{foreur[0]-1.5},"
         f"{foreur[1]-1.5}}},{{{foreur[0]+1.5},{foreur[1]+1.5}}}}}, type='resource'}}) do "
         f"storage.fl_ore=storage.fl_ore or {{}} "
         f"table.insert(storage.fl_ore, {{x=e.position.x, y=e.position.y, "
         f"n=e.name, a=e.amount}}) e.destroy() n=n+1 end rcon.print('casse '..n)",
         "local s=game.surfaces[1] local t=storage.fl_ore if t then "
         "for _,o in pairs(t) do s.create_entity{name=o.n, position={o.x,o.y}, "
         "amount=o.a} end storage.fl_ore=nil end rcon.print('repare')",
         {"foreur_hors_gisement", "machine_absente"}),

    ]

    # Après avoir cassé, on VIDE ce qui est déjà en transit. Sans cela le symptôme
    # décrit à l'enquêteur est faux : le boiler contient encore son plein, la belt
    # transporte encore du charbon, et le bras affiche `waiting_for_space_in_destination`
    # parce que la machine est PLEINE. Trois enquêtes sur quatre l'avaient relevé et
    # avaient refusé de conclure — à juste titre. On ne peut rien mesurer d'un modèle en
    # lui décrivant un symptôme qui n'existe pas.
    VIDER = ("local s = game.surfaces[1] "
             "for _, e in pairs(s.find_entities_filtered{name='boiler'}) do "
             "local i = e.get_fuel_inventory() if i then i.clear() end end "
             "for _, b in pairs(s.find_entities_filtered{type='transport-belt'}) do "
             "for k = 1, b.get_max_transport_line_index() do "
             "b.get_transport_line(k).clear() end end "
             "for _, i in pairs(s.find_entities_filtered{type='inserter'}) do "
             "local h = i.held_stack if h and h.valid_for_read then h.clear() end end "
             "rcon.print('vide')")

    # Le banc se CONTRÔLE lui-même avant chaque panne. Sans cela il a menti deux fois :
    # une réparation qui échoue laisse sa panne en place, la suivante s'y ajoute, et le
    # modèle diagnostique alors très correctement une cause qu'on compte comme fausse.
    # Vu en jeu : `status=no_minable` sur un foreur dont on croyait le minerai restauré.
    # Une seule panne à la fois, ou l'on ne mesure rien.
    def _saine() -> bool:
        from services.flux import suivre_flux
        if not suivre_flux(api, depart, "boiler", boiler).continu:
            return False
        # Le flux ne dit rien du GISEMENT : une belt reste continue au-dessus d'un foreur
        # privé de minerai. Il faut le vérifier à part, sans quoi la panne « minerai
        # retiré » survivrait invisible à sa propre réparation.
        etat = api.inspect_at(foreur[0], foreur[1], 1.5)
        for e in (etat.get("entities", []) if isinstance(etat, dict) else []):
            if e.get("type") == "mining-drill":
                return str(e.get("status")) not in ("no_minable_resources", "no_minable")
        return False

    justes, inconnus, evaluees, mesures = 0, 0, 0, 0
    for nom, casser, reparer, acceptables in pannes:
        if not _saine():
            rec(f"panne « {nom} »", False,
                "NON ÉVALUÉE : la chaîne n'était pas saine avant l'injection "
                "(remise en état incomplète)")
            continue
        rcon.query_lua(casser)
        rcon.query_lua(VIDER)
        api.run_action(api.wait, 120, timeout=120.0)
        constat = enqueteur(api, _ecart(f"le boiler reste à sec — panne « {nom} »"))
        evaluees += 1
        mesures += constat.outils_appeles
        if constat.cause == "inconnu":
            inconnus += 1
        elif constat.cause in acceptables:
            justes += 1
        rec(f"panne « {nom} »", constat.cause in acceptables,
            f"{constat.cause} (attendu {'/'.join(sorted(acceptables))}) — "
            f"{constat.preuve[:60]} [{constat.outils_appeles} mesure(s)]")
        rcon.query_lua(reparer)
        api.run_action(api.wait, 30, timeout=60.0)

    n = max(evaluees, 1)
    print(f"\n       {justes}/{evaluees} causes correctes sur les pannes ÉVALUÉES "
          f"({len(pannes) - evaluees} non évaluée(s)), {inconnus} « inconnu », "
          f"{evaluees - justes - inconnus} fausse(s) piste(s)")
    print(f"       {mesures / n:.1f} mesure(s) par enquête en moyenne")
    verdict = justes >= 4
    print(f"\n       VERDICT : {'approche VALIDÉE' if verdict else 'approche NON validée'} "
          f"(critère annoncé d'avance : 4 causes correctes sur 6)")
    if not verdict:
        print("       Une fausse piste coûte plus qu'un « inconnu » : elle ferait "
              "réparer un problème qui n'existe pas.")

    rcon.close()
    print("\n" + "=" * 72)
    print(f"{justes}/{n} reussies.")
    print("=" * 72)
    return 0 if verdict else 1


if __name__ == "__main__":
    sys.exit(main())