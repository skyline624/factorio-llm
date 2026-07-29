"""Tests unitaires de la référence de save — sans serveur.

Restaurer, c'est ÉCRASER : on remplace la partie en cours par une archive, après avoir
tué le processus qui la tenait. Un protocole de mesure qui rate cette manœuvre ne rend
pas un mauvais chiffre, il détruit l'état sur lequel on comptait mesurer.

Ce qui est éprouvé ici est donc ce qui peut mal tourner, et non le cas nominal :

  - un échec ne doit RIEN écraser — ni la save courante quand la référence manque, ni la
    référence quand le jeu n'a pas écrit sa partie ;
  - l'ordre arrêt → copie → relance n'est pas un détail de mise en œuvre : copier pendant
    que Factorio tourne, c'est se faire réécrire par-dessus au premier autosave ;
  - le serveur revient MÊME quand la copie échoue. Un serveur laissé éteint est le seul
    dénouement dont la boucle ne se relève pas toute seule ;
  - le singleton RCON est oublié au redémarrage. Le garder, c'est conserver une socket
    morte et payer deux reconnexions par appel — mesuré, une restauration passait ainsi
    de trente secondes à plus de dix minutes.

Lancement :
    cd python
    python -m tests.test_save_ref
"""

from __future__ import annotations

import os
import sys
import tempfile
import time

from services import save_ref

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:100]}")


def _bac() -> tuple[str, str, str]:
    """Un dossier jetable, une save « courante » et une référence au contenu distinct."""
    d = tempfile.mkdtemp(prefix="fl-saveref-")
    save = os.path.join(d, "fl-dev.zip")
    ref = os.path.join(d, "fl-reference.zip")
    with open(save, "w", encoding="utf-8") as f:
        f.write("etat-courant")
    with open(ref, "w", encoding="utf-8") as f:
        f.write("etat-de-reference")
    return d, save, ref


def _lire(p: str) -> str:
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except OSError:
        return "<absent>"


class _Serveur:
    """Un serveur de papier : il retient ce qu'on lui demande et dans quel ordre."""

    def __init__(self, arret: bool = True, demarrage: bool = True) -> None:
        self.arret, self.demarrage = arret, demarrage
        self.sequence: list[str] = []

    def arreter(self, delai: float = 15.0) -> bool:
        self.sequence.append("arret")
        return self.arret

    def demarrer(self, delai: float = 180.0) -> bool:
        self.sequence.append("demarrage")
        return self.demarrage


def _brancher(srv: _Serveur, copie=None):
    """Substitue les appels au système, et rend de quoi tout remettre en place."""
    anciens = (save_ref.arreter_serveur, save_ref.demarrer_serveur, save_ref.shutil.copy2)
    save_ref.arreter_serveur = srv.arreter
    save_ref.demarrer_serveur = srv.demarrer
    if copie is not None:
        save_ref.shutil.copy2 = copie
    return anciens


def _debrancher(anciens) -> None:
    (save_ref.arreter_serveur, save_ref.demarrer_serveur,
     save_ref.shutil.copy2) = anciens


# --------------------------------------------------------------------------- restaurer

def test_sans_reference_rien_n_est_touche() -> None:
    """Le pire des dénouements : détruire l'état courant en croyant le remplacer."""
    d, save, ref = _bac()
    absent = os.path.join(d, "jamais-figee.zip")
    srv = _Serveur()
    anciens = _brancher(srv)
    try:
        ok, motif = save_ref.restaurer_reference(chemin=absent, save=save)
    finally:
        _debrancher(anciens)
    intact = _lire(save) == "etat-courant"
    ok_test = (not ok) and intact and srv.sequence == []
    rec("test_sans_reference_rien_n_est_touche", ok_test,
        f"{motif[:60]} | save={_lire(save)} | serveur={srv.sequence or 'jamais touché'}")
    assert ok_test


def test_serveur_non_arrete_aucune_copie() -> None:
    """Copier sous un Factorio vivant, c'est se faire réécrire au premier autosave."""
    d, save, ref = _bac()
    srv = _Serveur(arret=False)
    anciens = _brancher(srv)
    try:
        ok, motif = save_ref.restaurer_reference(chemin=ref, save=save)
    finally:
        _debrancher(anciens)
    ok_test = (not ok) and _lire(save) == "etat-courant" and "demarrage" not in srv.sequence
    rec("test_serveur_non_arrete_aucune_copie", ok_test,
        f"{motif[:50]} | save={_lire(save)} | séquence={srv.sequence}")
    assert ok_test


def test_ordre_arret_copie_relance() -> None:
    """L'ordre EST le mécanisme : arrêter, puis copier, puis relancer."""
    d, save, ref = _bac()
    srv = _Serveur()
    vraie_copie = save_ref.shutil.copy2

    def copie(src, dst, *a, **k):
        # Le même journal que le serveur : l'ordre est ainsi OBSERVÉ, pas reconstitué.
        srv.sequence.append("copie")
        return vraie_copie(src, dst, *a, **k)

    anciens = _brancher(srv, copie=copie)
    try:
        ok, motif = save_ref.restaurer_reference(chemin=ref, save=save)
    finally:
        _debrancher(anciens)
    seq = srv.sequence
    ok_test = ok and seq == ["arret", "copie", "demarrage"] and _lire(save) == "etat-de-reference"
    rec("test_ordre_arret_copie_relance", ok_test,
        f"{' -> '.join(seq)} | save={_lire(save)}")
    assert ok_test


def test_le_serveur_revient_meme_si_la_copie_echoue() -> None:
    """Un serveur laissé éteint est le seul état dont on ne se relève pas tout seul."""
    d, save, ref = _bac()
    impossible = os.path.join(d, "dossier-absent", "fl-dev.zip")
    srv = _Serveur()
    anciens = _brancher(srv)
    try:
        ok, motif = save_ref.restaurer_reference(chemin=ref, save=impossible)
    finally:
        _debrancher(anciens)
    ok_test = (not ok) and "demarrage" in srv.sequence
    rec("test_le_serveur_revient_meme_si_la_copie_echoue", ok_test,
        f"{motif[:55]} | séquence={srv.sequence}")
    assert ok_test


def test_le_motif_dit_pourquoi_le_serveur_n_est_pas_revenu() -> None:
    """« Il n'est pas revenu » n'oriente vers rien ; le log serveur, si."""
    d, save, ref = _bac()
    srv = _Serveur(demarrage=False)
    anciens = _brancher(srv)
    ancien_log = save_ref._fin_du_log
    save_ref._fin_du_log = lambda n=12: "Error Version.cpp:100: map version 1.1 differs"
    try:
        ok, motif = save_ref.restaurer_reference(chemin=ref, save=save)
    finally:
        save_ref._fin_du_log = ancien_log
        _debrancher(anciens)
    ok_test = (not ok) and "map version" in motif and _lire(save) == "etat-de-reference"
    rec("test_le_motif_dit_pourquoi_le_serveur_n_est_pas_revenu", ok_test, motif[:95])
    assert ok_test


# ----------------------------------------------------------------------------- sauver

class _Rcon:
    """Un RCON de papier. `ecrit_apres` : au bout de combien d'appels la save apparaît."""

    def __init__(self, save: str, ecrit: bool = True, leve: bool = False) -> None:
        self.save, self.ecrit, self.leve = save, ecrit, leve
        self.appels = 0

    def query_lua(self, code: str) -> str:
        self.appels += 1
        if self.leve:
            raise RuntimeError("socket fermée")
        if self.ecrit:
            with open(self.save, "w", encoding="utf-8") as f:
                f.write("etat-fraichement-ecrit")
        return "sauve"


def test_sauver_fige_ce_que_le_jeu_vient_d_ecrire() -> None:
    """Copier sans faire écrire figerait le dernier autosave, pas l'état qu'on tient."""
    d, save, ref = _bac()
    os.utime(save, (time.time() - 60, time.time() - 60))
    r = _Rcon(save)
    ok, motif = save_ref.sauver_reference(rcon=r, chemin=ref, save=save,
                                          delai_ecriture=5.0)
    ok_test = ok and _lire(ref) == "etat-fraichement-ecrit" and r.appels == 1
    rec("test_sauver_fige_ce_que_le_jeu_vient_d_ecrire", ok_test,
        f"{motif[:55]} | référence={_lire(ref)}")
    assert ok_test


def test_sauver_sans_ecriture_preserve_l_ancienne_reference() -> None:
    """Une référence tronquée est pire que pas de référence : on restaurerait du vide."""
    d, save, ref = _bac()
    os.utime(save, (time.time() - 60, time.time() - 60))
    r = _Rcon(save, ecrit=False)
    ok, motif = save_ref.sauver_reference(rcon=r, chemin=ref, save=save,
                                          delai_ecriture=1.5)
    ok_test = (not ok) and _lire(ref) == "etat-de-reference"
    rec("test_sauver_sans_ecriture_preserve_l_ancienne_reference", ok_test,
        f"{motif[:55]} | référence={_lire(ref)}")
    assert ok_test


def test_sauver_signale_un_rcon_mort_au_lieu_de_figer_l_ancien_etat() -> None:
    """Avaler l'exception rendrait un succès sur un fichier vieux d'un autosave."""
    d, save, ref = _bac()
    r = _Rcon(save, leve=True)
    ok, motif = save_ref.sauver_reference(rcon=r, chemin=ref, save=save,
                                          delai_ecriture=1.5)
    ok_test = (not ok) and "écrire" in motif and _lire(ref) == "etat-de-reference"
    rec("test_sauver_signale_un_rcon_mort_au_lieu_de_figer_l_ancien_etat", ok_test,
        motif[:95])
    assert ok_test


def test_sauver_sans_save_ne_cree_pas_de_reference() -> None:
    """Sans serveur (rcon=None), on copie ce qui est là — encore faut-il qu'il y soit."""
    d, save, ref = _bac()
    os.remove(save)
    os.remove(ref)
    ok, motif = save_ref.sauver_reference(rcon=None, chemin=ref, save=save)
    ok_test = (not ok) and not os.path.exists(ref)
    rec("test_sauver_sans_save_ne_cree_pas_de_reference", ok_test,
        f"{motif[:60]} | référence créée : {'oui' if os.path.exists(ref) else 'non'}")
    assert ok_test


# -------------------------------------------------------------------------- relance

class _Popen:
    """Un lancement de papier : on retient COMMENT le serveur a été lancé."""

    def __init__(self) -> None:
        self.kwargs: dict = {}
        self.cmd = ""

    def __call__(self, cmd, **kwargs):
        self.cmd, self.kwargs = cmd, kwargs
        return self


def _relance(popen: _Popen, repond=lambda **k: True):
    anciens = (save_ref.subprocess.Popen, save_ref._rcon_repond, save_ref.time.sleep)
    save_ref.subprocess.Popen = popen
    save_ref._rcon_repond = repond
    save_ref.time.sleep = lambda s: None  # la cadence de sonde n'est pas l'objet du test
    return anciens


def _fin_relance(anciens) -> None:
    (save_ref.subprocess.Popen, save_ref._rcon_repond, save_ref.time.sleep) = anciens


def test_le_serveur_relance_ne_tient_pas_la_sortie_de_l_appelant() -> None:
    """Trouvé en live : `verify_save_ref.py | tail` ne rendait jamais la main.

    La restauration s'était pourtant déroulée entière. Le serveur héritait de la sortie
    de l'appelant et tenait le tuyau ouvert tant qu'il vivait ; `tail` attendait donc un
    serveur qui n'a aucune raison de s'arrêter. Tout appelant qui capture sa sortie —
    journal, runner, CI — tombe dans le même piège.
    """
    p = _Popen()
    anciens = _relance(p)
    try:
        ok = save_ref.demarrer_serveur()
    finally:
        _fin_relance(anciens)
    nul = save_ref.subprocess.DEVNULL
    detaches = (p.kwargs.get("stdout") is nul and p.kwargs.get("stderr") is nul
                and p.kwargs.get("stdin") is nul)
    ok_test = ok and detaches
    rec("test_le_serveur_relance_ne_tient_pas_la_sortie_de_l_appelant", ok_test,
        f"stdout/stderr/stdin détachés : {detaches}")
    assert ok_test


def test_relancer_oublie_le_singleton_rcon() -> None:
    """C'est là que l'oubli doit avoir lieu : l'appelant ne peut pas le savoir."""
    from core import rcon as rcon_mod

    ancien = rcon_mod._rcon_singleton
    rcon_mod._rcon_singleton = type("_Mort", (), {"close": lambda self: None})()
    p = _Popen()
    anciens = _relance(p)
    try:
        ok = save_ref.demarrer_serveur()
        oublie = rcon_mod._rcon_singleton is None
    finally:
        _fin_relance(anciens)
        rcon_mod._rcon_singleton = ancien
    ok_test = ok and oublie
    rec("test_relancer_oublie_le_singleton_rcon", ok_test,
        f"singleton oublié dès le retour de demarrer_serveur : {oublie}")
    assert ok_test


def test_relancer_attend_le_mod_pas_seulement_le_port() -> None:
    """Le port écoute avant que le jeu ne réponde : rendre la main là serait mentir."""
    sondes = {"n": 0}

    def repond(**k):
        sondes["n"] += 1
        return sondes["n"] >= 3

    p = _Popen()
    anciens = _relance(p, repond=repond)
    try:
        ok = save_ref.demarrer_serveur()
    finally:
        _fin_relance(anciens)
    ok_test = ok and sondes["n"] == 3
    rec("test_relancer_attend_le_mod_pas_seulement_le_port", ok_test,
        f"{sondes['n']} sonde(s) avant de rendre la main")
    assert ok_test


# ---------------------------------------------------------------------------- outillage

def test_empreinte_ne_leve_jamais() -> None:
    """Elle sert à VÉRIFIER une restauration : lever ici masquerait le résultat mesuré."""
    class _Mort:
        def query_lua(self, code: str) -> str:
            raise RuntimeError("serveur parti")

    e = save_ref.empreinte(_Mort())
    ok_test = e is None
    rec("test_empreinte_ne_leve_jamais", ok_test, f"empreinte={e!r} au lieu d'une levée")
    assert ok_test


def test_reset_rcon_oublie_le_singleton() -> None:
    """Le singleton survit au serveur : gardé, il fait payer deux reconnexions par appel."""
    from core import rcon as rcon_mod

    class _Faux:
        def __init__(self, *a, **k) -> None:
            self.ferme = False

        def close(self) -> None:
            self.ferme = True

    ancien_client, ancien_singleton = rcon_mod.RconClient, rcon_mod._rcon_singleton
    rcon_mod.RconClient = _Faux
    rcon_mod._rcon_singleton = None
    try:
        a = rcon_mod.get_rcon()
        memoire = rcon_mod.get_rcon() is a
        rcon_mod.reset_rcon()
        b = rcon_mod.get_rcon()
        ok_test = memoire and b is not a and a.ferme
    finally:
        rcon_mod.RconClient = ancien_client
        rcon_mod._rcon_singleton = ancien_singleton
    rec("test_reset_rcon_oublie_le_singleton", ok_test,
        f"mémoire={memoire}, client neuf après reset, ancien fermé={a.ferme}")
    assert ok_test


def test_reset_rcon_oublie_meme_un_close_qui_leve() -> None:
    """Une socket morte refuse parfois de se fermer. L'oubli ne doit pas en dépendre."""
    from core import rcon as rcon_mod

    class _Recalcitrant:
        def __init__(self, *a, **k) -> None:
            pass

        def close(self) -> None:
            raise OSError("socket déjà partie")

    ancien_client, ancien_singleton = rcon_mod.RconClient, rcon_mod._rcon_singleton
    rcon_mod.RconClient = _Recalcitrant
    rcon_mod._rcon_singleton = None
    try:
        a = rcon_mod.get_rcon()
        rcon_mod.reset_rcon()
        ok_test = rcon_mod._rcon_singleton is None and rcon_mod.get_rcon() is not a
    finally:
        rcon_mod.RconClient = ancien_client
        rcon_mod._rcon_singleton = ancien_singleton
    rec("test_reset_rcon_oublie_meme_un_close_qui_leve", ok_test,
        "le singleton est oublié malgré l'échec de la fermeture")
    assert ok_test


def main() -> int:
    for t in (test_sans_reference_rien_n_est_touche,
              test_serveur_non_arrete_aucune_copie,
              test_ordre_arret_copie_relance,
              test_le_serveur_revient_meme_si_la_copie_echoue,
              test_le_motif_dit_pourquoi_le_serveur_n_est_pas_revenu,
              test_sauver_fige_ce_que_le_jeu_vient_d_ecrire,
              test_sauver_sans_ecriture_preserve_l_ancienne_reference,
              test_sauver_signale_un_rcon_mort_au_lieu_de_figer_l_ancien_etat,
              test_sauver_sans_save_ne_cree_pas_de_reference,
              test_le_serveur_relance_ne_tient_pas_la_sortie_de_l_appelant,
              test_relancer_oublie_le_singleton_rcon,
              test_relancer_attend_le_mod_pas_seulement_le_port,
              test_empreinte_ne_leve_jamais,
              test_reset_rcon_oublie_le_singleton,
              test_reset_rcon_oublie_meme_un_close_qui_leve):
        t()
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
