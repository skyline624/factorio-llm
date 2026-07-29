"""Test LIVE : l'état du serveur se fige et se remet en place à l'identique.

E20 a buté sur ceci : les parties longues héritent du terrain laissé par le test
précédent, donc deux exécutions du même code ne mesurent pas la même chose. Tant que le
point de départ varie, on mesure du bruit et l'on peut le prendre pour un effet du
modèle. Une restauration d'état est le préalable à toute comparaison avec/sans arbitre.

Ce qu'on vérifie, dans l'ordre où cela peut mal tourner :
  1. l'état courant se fige — le jeu ÉCRIT sa partie, on copie ce qu'il vient d'écrire ;
  2. l'état modifié se distingue de la référence (sans quoi l'étape 4 ne prouverait rien) ;
  3. la restauration aboutit, et en un temps qui reste utilisable dans un protocole ;
  4. l'état revient à l'identique : mêmes entités, mêmes items, et le tick a RECULÉ ;
  5. le premier appel qui suit passe TOUT DE SUITE — le singleton RCON, s'il survivait au
     redémarrage, garderait une socket morte et ferait payer deux reconnexions par appel ;
  6. une référence absente ne casse rien : le serveur reste debout et l'on est prévenu.

La référence de ce test est un fichier À PART : écraser `fl-reference.zip` détruirait
l'état que l'on a peut-être figé exprès pour une série de mesures.

Pré-requis : serveur headless en marche. SKIP s'il est absent.

Lancement :
    cd python
    python verify_save_ref.py
"""

from __future__ import annotations

import os
import sys
import time

from core.rcon import get_rcon
from services import save_ref

RESULTS: list[tuple[str, bool, str]] = []
REF_TEST = os.path.join(save_ref.RACINE, "saves", "fl-reference-test.zip")


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:105]}")


def _chiffres(empreinte: str | None) -> dict:
    """`tick=123 entites=45 items=6` -> dict. Comparer les PARTIES, pas la chaîne.

    Le tick de la référence n'est pas celui qu'on lit avant de la figer : le jeu avance
    entre les deux. Exiger l'égalité de la chaîne entière ferait échouer une restauration
    pourtant parfaite.
    """
    out: dict = {}
    for morceau in (empreinte or "").split():
        if "=" in morceau:
            cle, _, val = morceau.partition("=")
            try:
                out[cle] = int(val)
            except ValueError:
                pass
    return out


def _tick(rcon) -> int:
    try:
        return int(str(rcon.query_lua("rcon.print(game.tick)")).strip())
    except (ValueError, TypeError, AttributeError):
        return 0


def _avancer_le_temps(rcon, depart: int, marge: int = 20_000,
                      delai: float = 60.0) -> int:
    """Fait défiler le jeu d'au moins `marge` ticks, puis remet la vitesse à ×1.

    On accélère plutôt que d'attendre : 20 000 ticks font cinq minutes de jeu, soit cinq
    secondes réelles à ×64. La vitesse est remise à ×1 dans tous les cas — un serveur
    laissé emballé fausserait la mesure suivante sans rien annoncer.
    """
    try:
        rcon.query_lua("game.speed = 64 rcon.print('ok')")
        fin = time.time() + delai
        t = _tick(rcon)
        while t - depart < marge and time.time() < fin:
            time.sleep(1.0)
            t = _tick(rcon)
        return t - depart
    finally:
        try:
            rcon.query_lua("game.speed = 1 rcon.print('ok')")
        except Exception:
            pass


def main() -> int:
    try:
        rcon = get_rcon("127.0.0.1", 27015, "factoriollm")
        rcon.query_lua("rcon.print('ok')")
    except Exception as e:
        print(f"[SKIP] serveur injoignable ({e}).")
        return 0

    avant = _chiffres(save_ref.empreinte(rcon))
    if not avant:
        print("[SKIP] empreinte illisible — le mod ne répond pas comme attendu.")
        return 0
    print(f"       état de départ : {avant}")

    # Une empreinte qui ne voit pas l'inventaire dirait « identique » sans l'avoir
    # regardé. Mesuré : `game.players[1]` est absent en headless, et 21 lots d'items
    # étaient comptés pour zéro. La garde vient donc AVANT toute comparaison.
    rcon.query_lua(
        "for _, c in pairs(game.surfaces[1].find_entities_filtered{name='character'}) do "
        "c.insert{name='iron-plate', count=5} end rcon.print('ok')")
    vu = _chiffres(save_ref.empreinte(rcon)).get("items", 0)
    rec("saveref-0 : l'empreinte VOIT l'inventaire de l'avatar", vu > 0,
        f"items lus : {avant.get('items')} -> {vu} après cinq plaques ajoutées")

    # --- 1. figer -----------------------------------------------------------------
    t0 = time.time()
    ok, motif = save_ref.sauver_reference(rcon=rcon, chemin=REF_TEST)
    pose = os.path.exists(REF_TEST) and os.path.getsize(REF_TEST) > 100_000
    rec("saveref-1 : l'état courant se fige dans une référence", ok and pose,
        f"{motif} en {time.time() - t0:.0f}s")
    if not (ok and pose):
        return _verdict()
    # L'empreinte de la RÉFÉRENCE est celle d'après l'écriture : c'est cet état-là qui
    # est dans l'archive, à quelques ticks près.
    reference = _chiffres(save_ref.empreinte(rcon))

    # --- 2. s'écarter -------------------------------------------------------------
    # Sans écart mesurable, l'étape 4 passerait même si la restauration n'avait rien fait.
    rcon.query_lua(
        "local s = game.surfaces[1] local n = 0 "
        "for i = 1, 12 do "
        "  local p = s.find_non_colliding_position('wooden-chest', {40 + i * 3, 40}, 30, 1) "
        "  if p and s.create_entity{name='wooden-chest', position=p, force='player'} then "
        "    n = n + 1 end end "
        "local pl = game.players[1] "
        "if pl and pl.character then pl.insert{name='iron-plate', count=77} end "
        "rcon.print(n)"
    )
    # Le temps de jeu doit AVANCER franchement, sinon le recul du tick n'est pas
    # observable : le redémarrage consomme à lui seul quelques centaines de ticks, et un
    # état modifié deux ticks après la référence se retrouverait « restauré » à un tick
    # plus GRAND qu'avant. Mesuré : 1050159 -> 1050195 sur une restauration parfaite.
    # `game.speed` est enregistré DANS la save : la référence a été figée à ×1, elle
    # reviendra donc à ×1 toute seule.
    avance = _avancer_le_temps(rcon, reference.get("tick", 0), marge=20_000)
    apres = _chiffres(save_ref.empreinte(rcon))
    ecart = (apres.get("entites", 0) != reference.get("entites", 0)
             or apres.get("items", 0) != reference.get("items", 0))
    rec("saveref-2 : l'état modifié se distingue de la référence", ecart,
        f"référence={reference} -> modifié={apres} (+{avance} ticks de jeu)")

    # --- 3. restaurer -------------------------------------------------------------
    t0 = time.time()
    ok, motif = save_ref.restaurer_reference(chemin=REF_TEST)
    duree = time.time() - t0
    # 120 s est le seuil d'utilisabilité, pas une performance : au-delà, une série de
    # mesures coûte plus en restaurations qu'en parties.
    rec("saveref-3 : la restauration aboutit en un temps utilisable", ok and duree < 120.0,
        f"{motif} en {duree:.0f}s")
    if not ok:
        return _verdict()

    # --- 4. l'état est revenu -----------------------------------------------------
    # Le serveur a redémarré : le client précédent pointe sur un processus mort. C'est
    # `demarrer_serveur` qui a oublié le singleton ; il suffit de le redemander.
    t0 = time.time()
    rcon2 = get_rcon("127.0.0.1", 27015, "factoriollm")
    revenu = _chiffres(save_ref.empreinte(rcon2))
    premier_appel = time.time() - t0

    memes_entites = revenu.get("entites") == reference.get("entites")
    memes_items = revenu.get("items") == reference.get("items")
    recul = revenu.get("tick", 0) < apres.get("tick", 0)
    rec("saveref-4 : l'état est revenu à l'identique", memes_entites and memes_items,
        f"entités {apres.get('entites')} -> {revenu.get('entites')} "
        f"(référence {reference.get('entites')}), items {apres.get('items')} -> "
        f"{revenu.get('items')} (référence {reference.get('items')})")
    rec("saveref-4b : le tick a reculé — c'est bien un retour en arrière", recul,
        f"tick {apres.get('tick')} -> {revenu.get('tick')}, soit "
        f"{revenu.get('tick', 0) - reference.get('tick', 0):+d} par rapport à la référence")

    # --- 5. le premier appel qui suit ne paie pas de reconnexion ------------------
    # Un singleton conservé coûte deux tentatives à dix secondes AVANT d'échouer ; une
    # boucle qui sonde toutes les trois secondes devient alors plus lente que le
    # démarrage qu'elle surveille. Mesuré : la restauration passait à plus de dix minutes.
    rec("saveref-5 : le premier appel après restauration passe tout de suite",
        revenu != {} and premier_appel < 15.0,
        f"{premier_appel:.1f}s pour reconnecter et lire l'empreinte")

    # --- 6. garde-fou : référence absente ----------------------------------------
    absent = os.path.join(save_ref.RACINE, "saves", "fl-reference-jamais-figee.zip")
    ok6, motif6 = save_ref.restaurer_reference(chemin=absent)
    debout = save_ref._rcon_repond()
    rec("saveref-6 : une référence absente ne touche pas au serveur",
        (not ok6) and debout, f"{motif6[:70]} | serveur debout : {'oui' if debout else 'NON'}")

    # La référence du test a servi ; la garder encombrerait `saves/`. L'état courant EST
    # déjà celui-là, donc rien n'est perdu en la supprimant.
    try:
        os.remove(REF_TEST)
        print(f"       référence de test retirée ({os.path.basename(REF_TEST)})")
    except OSError:
        pass
    rcon2.close()
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
