"""Fabrique la référence « carte propre » : l'agent devra tout bâtir depuis zéro.

E20 a buté sur un plafond de mesure : l'usine restait à deux machines, donc la boucle
n'avait presque jamais plusieurs options, donc l'arbitre n'avait presque jamais la parole
et son apport ne se mesurait pas. La cause n'était pas le modèle : chaque partie héritait
du bâti laissé par le test précédent — deux machines en panne dont tout était déjà tenté.

Une carte rase change cela. `verify_coordinator_e8` a montré que le Coordinator sait
enchaîner énergie -> production -> arrêt depuis rien ; c'est cette mise en condition qu'on
reprend ici, à ceci près qu'on la FIGE au lieu de la consommer aussitôt. Toute partie
lancée ensuite avec `--depuis-reference` repart du même point.

Trois partis pris, et ils se discutent :

**L'inventaire est GARNI, et c'est un paramètre de l'expérience.** On ne cherche pas ici
à savoir si l'agent sait miner à la main de quoi se construire — c'est un autre chantier.
On cherche à ce que l'usine se développe assez pour produire des situations à plusieurs
options. La dotation est donc explicite, journalisée, et la même pour toutes les parties.

**Le personnage est posé sur le gisement de fer**, ancré comme le fait `FactoryBuilder` :
le runner prend la position du personnage comme zone quand il n'y a aucune machine, et
une zone tombée dans un lac ne mesure rien (leçon E2).

**La vitesse est remise à ×1 avant de figer** : `game.speed` est enregistré DANS la save.
Une référence figée à ×10 imposerait sa vitesse à toutes les parties suivantes, sans que
rien ne l'annonce.

Usage :
    cd python
    python preparer_reference.py            # fige saves/fl-reference.zip
    python preparer_reference.py --rayon 300
"""

from __future__ import annotations

import sys

from core.mod_api import ModApi
from core.rcon import get_rcon
from services import save_ref

# Ce que le personnage emporte. Générosité VOULUE : le pré-vol de l'executor refuse de
# poser ce qu'on n'a pas, et une boucle qui manque d'un poteau s'arrête sur un détail
# qui n'a rien à voir avec ce qu'on mesure.
DOTATION = [
    ("coal", 600),
    ("iron-plate", 300),
    ("small-electric-pole", 200),
    ("medium-electric-pole", 50),
    ("electric-mining-drill", 8),
    ("burner-mining-drill", 4),
    ("inserter", 40),
    ("transport-belt", 400),
    ("underground-belt", 20),
    ("splitter", 10),
    ("stone-furnace", 10),
    ("electric-furnace", 4),
    ("assembling-machine-1", 4),
    ("boiler", 4),
    ("steam-engine", 6),
    ("offshore-pump", 4),
    ("pipe", 150),
    ("pipe-to-ground", 20),
    ("wooden-chest", 10),
    ("gun-turret", 4),
    ("firearm-magazine", 40),
]


def _rase(rcon) -> int:
    """Efface le bâti du joueur — c'est lui qui fait varier un départ d'une partie à l'autre.

    Le `character` est épargné : le détruire laisserait l'avatar sans corps et toute la
    suite échouerait sur une cause qui n'a rien à voir avec ce qu'on veut observer.
    """
    return int(str(rcon.query_lua(
        "local n = 0 for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered{force='player'}) do "
        "if e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)"
    )).strip() or 0)


def _degager(rcon, r: float) -> int:
    """Arbres, rochers, nids : tout ce qui n'appartient pas au joueur et n'est pas minerai.

    Les arbres refusent les poses ; les nids envoient des vagues dont la date dépend du
    hasard, ce qui rendrait deux parties incomparables pour une raison étrangère au sujet.
    """
    return int(str(rcon.query_lua(
        f"local s = game.surfaces[1] local n = 0 "
        f"for _, e in pairs(s.find_entities_filtered{{area={{{{-{r},-{r}}},{{{r},{r}}}}}}}) do "
        f"if e.force ~= game.forces.player and e.type ~= 'resource' "
        f"and e.type ~= 'character' then e.destroy() n = n + 1 end end rcon.print(n)"
    )).strip() or 0)


def _doter(rcon) -> tuple[int, list[str]]:
    """Insère la dotation item par item.

    Chaque insertion est protégée : un nom inconnu (renommage entre versions de Factorio)
    ferait échouer l'ordre entier et l'on croirait avoir doté l'inventaire. On rend donc
    ce qui est réellement entré, et ce qui a été refusé.
    """
    items = ",".join(f"{{'{n}',{c}}}" for n, c in DOTATION)
    sortie = str(rcon.query_lua(
        "local c = nil for _, e in pairs(game.surfaces[1]"
        ".find_entities_filtered{name='character'}) do c = e end "
        "if not c then rcon.print('0|pas de character') return end "
        f"local liste = {{{items}}} local ok, refus = 0, {{}} "
        "for _, it in pairs(liste) do "
        "  local bon = pcall(function() c.insert{name=it[1], count=it[2]} end) "
        "  if bon then ok = ok + 1 else refus[#refus+1] = it[1] end end "
        "rcon.print(ok .. '|' .. table.concat(refus, ','))"
    )).strip()
    nombre, _, refuses = sortie.partition("|")
    try:
        return int(nombre), [x for x in refuses.split(",") if x]
    except ValueError:
        return 0, [sortie]


def _semer_des_nids(api, rcon, x: float, y: float, combien: int) -> int:
    """Replante des nids au nord, hors du rayon dégagé.

    Le dégagement rase TOUT ce qui n'appartient pas au joueur, nids compris — et une
    fois rasés, ils ne repoussent pas. Après quelques préparations larges, la carte n'a
    plus une seule menace à portée : on mesurerait alors une partie sans défense en
    croyant mesurer une défense maîtrisée. C'est arrivé, et le chiffre était flatteur.

    Semer explicitement rend le régime « menace permanente » reproductible, et le dit
    dans la référence au lieu de dépendre de ce que la carte a bien voulu garder.
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


def main(argv: list[str]) -> int:
    rayon = 260.0
    nids = 0
    if "--rayon" in argv:
        try:
            rayon = float(argv[argv.index("--rayon") + 1])
        except (IndexError, ValueError):
            pass
    if "--nids" in argv:
        try:
            nids = int(argv[argv.index("--nids") + 1])
        except (IndexError, ValueError):
            nids = 4

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
    efface = _rase(rcon)
    # Le terrain doit EXISTER avant d'être dégagé : hors des tuiles générées, il n'y a
    # rien à trouver et rien à poser (leçon S4d).
    api.generate_terrain(0.0, 0.0, min(rayon, 300.0))
    api.run_action(api.wait, 60, timeout=60.0)
    degage = _degager(rcon, rayon)
    doses, refuses = _doter(rcon)
    print(f"       carte rase : {efface} entité(s) du joueur effacée(s), "
          f"{degage} obstacle(s) dégagé(s) sur {rayon:.0f} tuiles")
    print(f"       dotation : {doses}/{len(DOTATION)} lots insérés"
          + (f" | REFUSÉS : {', '.join(refuses)}" if refuses else ""))

    # Le personnage est posé sur le gisement, comme le fait E8 : le runner prend sa
    # position pour zone quand il n'y a aucune machine.
    from agents.base import Contract
    from agents.factory_builder import FactoryBuilder
    from services.knowledge import ProductionGoal
    fb = FactoryBuilder(api, Contract(goal=ProductionGoal("iron-plate", 0.5)))
    sp = fb._scan_patch_local("iron-ore")
    ancre = fb._anchor_on_ore(sp, 4) if sp.get("sample") else None
    if ancre is None:
        print("[FAIL] aucun gisement de fer exploitable — référence NON figée.")
        print("       (sans gisement, la partie n'aurait rien à exploiter)")
        rcon.close()
        return 1
    api.run_action(api.teleport_to, ancre[0], ancre[1] + 3.0, timeout=30.0)
    print(f"       personnage ancré sur le fer en ({ancre[0]:.0f}, {ancre[1]:.0f})")

    # Les nids sont semés APRÈS le dégagement, sinon celui-ci les emporterait — et à
    # 280 tuiles, c'est-à-dire dans le rayon de détection (300) mais hors de portée
    # d'une tourelle. C'est exactement la configuration qui a produit 936 tours de
    # `defendre` stériles, et donc celle qu'il faut pouvoir reproduire à volonté.
    if nids:
        semes = _semer_des_nids(api, rcon, ancre[0], ancre[1] - 280.0, nids)
        print(f"       {semes} nid(s) semé(s) à 280 tuiles au nord "
              f"({'menace reproductible' if semes else 'AUCUN — menace absente'})")

    # ×1 AVANT de figer : la vitesse voyage dans la save.
    rcon.query_lua("game.speed = 1 rcon.print('ok')")
    ok, motif = save_ref.sauver_reference(rcon=rcon)
    print(f"[{'OK  ' if ok else 'FAIL'}] {motif}")
    print(f"       empreinte de la référence : {save_ref.empreinte(rcon)}")
    if ok:
        print("\n       Partir de là : python run_partie_longue.py 30 "
              "--vitesse 10 --ombre --depuis-reference")
    rcon.close()
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
