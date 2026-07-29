"""Tests unitaires du suivi de flux — les quatre ruptures, sans serveur.

Chacun de ces cas a été RENCONTRÉ en jeu pendant le chantier E13, et aucun n'était
visible sur un statut de machine : la chaîne était posée, aucune pose n'avait échoué, et
le boiler au bout affichait simplement « réservoir vide ».

Le faux monde rend des belts et des bras avec les mêmes champs que `entity_row` du mod
(`direction`, `pickupX/pickupY`, `dropX/dropY`), ce qui permet de scripter une chaîne
saine puis de la casser d'une manière connue — le protocole du FactoryDoctor.

Lancement :
    cd python
    python -m tests.test_flux
"""

from __future__ import annotations

import sys

from services.flux import (BRAS_ABSENT, BRAS_DEPOSE_VIDE, BRAS_MAL_ORIENTE,
                           INTERROMPUE, MAL_ORIENTEE, OK, suivre_flux)

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:44s} {detail[:100]}")


class FakeMonde:
    """Une carte scriptée : des belts orientées, des bras, une machine cible."""

    def __init__(self, belts, bras=(), cible=None):
        # belts : {(x, y): "east"} ; bras : [(x, y, pickup, drop)] ; cible : (x, y, nom)
        self.belts = dict(belts)
        self.bras = list(bras)
        self.cible = cible

    def inspect_at(self, x, y, radius=0.5):
        rows = []
        for (bx, by), d in self.belts.items():
            if abs(bx - x) <= radius and abs(by - y) <= radius:
                rows.append({"name": "transport-belt", "type": "transport-belt",
                             "x": bx, "y": by, "direction": d})
        for ix, iy, pk, dp in self.bras:
            if abs(ix - x) <= radius and abs(iy - y) <= radius:
                rows.append({"name": "burner-inserter", "type": "inserter",
                             "x": ix, "y": iy, "pickupX": pk[0], "pickupY": pk[1],
                             "dropX": dp[0], "dropY": dp[1]})
        if self.cible is not None:
            cx, cy, nom = self.cible
            # La cible est une machine large : on la considère présente dès que l'aire
            # interrogée touche son emprise (1.5 x 1), comme le fait `area` côté mod.
            if abs(cx - x) <= radius + 1.0 and abs(cy - y) <= radius + 0.5:
                rows.append({"name": nom, "type": "boiler", "x": cx, "y": cy})
        return {"entities": rows}


def _ligne(x0, y0, n, direction="east"):
    dx, dy = {"east": (1, 0), "west": (-1, 0), "south": (0, 1), "north": (0, -1)}[direction]
    return {(x0 + dx * i, y0 + dy * i): direction for i in range(n)}


def _monde_sain():
    """Foreur -> 6 belts vers l'est -> bras -> boiler. Le cas nominal."""
    belts = _ligne(0.5, 0.5, 6)
    bras = [(6.5, 0.5, (5.5, 0.5), (7.6, 0.5))]
    return FakeMonde(belts, bras, cible=(8.5, 0.5, "boiler"))


def test_chaine_saine_est_continue() -> None:
    r = suivre_flux(_monde_sain(), (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = r.continu and r.cause == OK and r.tuiles == 6
    rec("test_chaine_saine_est_continue", ok, str(r))
    assert ok, str(r)


def test_belt_interrompue() -> None:
    """Un segment retiré au milieu : le flux s'arrête au trou, et on le situe."""
    m = _monde_sain()
    del m.belts[(3.5, 0.5)]
    r = suivre_flux(m, (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = not r.continu and r.cause == INTERROMPUE and r.rupture == (3.5, 0.5)
    rec("test_belt_interrompue", ok, str(r))
    assert ok, str(r)


def test_raccord_retourne_vers_le_vide() -> None:
    """Le défaut d'E13 : un segment garde son ancienne direction et déverse à côté.

    Rien ne manque, rien n'est en erreur — le chemin part simplement ailleurs. C'est
    l'écart entre « la belt est complète » et « la belt mène quelque part ».
    """
    m = _monde_sain()
    m.belts[(3.5, 0.5)] = "south"          # envoie hors de la ligne
    r = suivre_flux(m, (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = not r.continu and r.cause == INTERROMPUE and r.rupture == (3.5, 1.5)
    rec("test_raccord_retourne_vers_le_vide", ok, str(r))
    assert ok, str(r)


def test_deux_segments_se_renvoient() -> None:
    """Deux belts face à face : le chemin boucle au lieu de s'interrompre."""
    m = _monde_sain()
    m.belts[(3.5, 0.5)] = "west"           # renvoie vers la précédente
    r = suivre_flux(m, (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = not r.continu and r.cause == MAL_ORIENTEE
    rec("test_deux_segments_se_renvoient", ok, str(r))
    assert ok, str(r)


def test_bras_depose_dans_le_vide() -> None:
    """Le bras prend bien sur la belt, mais son dépôt ne touche pas la machine.

    Mesuré en jeu : bras à 2.5 tuiles de son boiler, `drop` du côté opposé, statut d'un
    bras qui attend. Il n'a jamais transporté un seul charbon.
    """
    m = _monde_sain()
    m.bras = [(6.5, 0.5, (5.5, 0.5), (6.5, -1.7))]     # dépose au nord, dans le vide
    r = suivre_flux(m, (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = not r.continu and r.cause == BRAS_DEPOSE_VIDE
    rec("test_bras_depose_dans_le_vide", ok, str(r))
    assert ok, str(r)


def test_bras_absent_au_pied_de_la_cible() -> None:
    """Belt complète jusqu'à la machine, mais personne pour décharger.

    À distinguer d'une belt trouée : ici il ne manque pas un segment, il manque un bras.
    """
    m = _monde_sain()
    m.bras = []
    m.belts = _ligne(0.5, 0.5, 7)          # arrive au pied du boiler (8.5)
    r = suivre_flux(m, (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = not r.continu and r.cause == BRAS_ABSENT
    rec("test_bras_absent_au_pied_de_la_cible", ok, str(r))
    assert ok, str(r)


def test_bras_mal_oriente_nest_pas_un_bras_absent() -> None:
    """Un bras tourné d'un quart de tour cesse de puiser sur la belt.

    Le flux ne le rencontre alors jamais, exactement comme s'il n'existait pas. Conclure
    « il manque un bras » ferait en poser un second à côté du premier ; la réparation est
    de RETOURNER celui qui est là. Distinction mesurée en jeu (E14).
    """
    m = _monde_sain()
    m.belts = _ligne(0.5, 0.5, 7)
    m.bras = [(7.5, 1.5, (7.5, 2.5), (7.5, -0.7))]   # à côté, puise ailleurs
    r = suivre_flux(m, (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = not r.continu and r.cause == BRAS_MAL_ORIENTE
    rec("test_bras_mal_oriente_nest_pas_un_bras_absent", ok, str(r))
    assert ok, str(r)


def test_depart_sans_belt() -> None:
    """Rien au départ : le foreur ne déverse sur aucune belt."""
    r = suivre_flux(FakeMonde({}), (0.5, 0.5), "boiler", (8.5, 0.5))
    ok = not r.continu and r.tuiles == 0 and r.cause == INTERROMPUE
    rec("test_depart_sans_belt", ok, str(r))
    assert ok, str(r)


def main() -> int:
    for t in (test_chaine_saine_est_continue, test_belt_interrompue,
              test_raccord_retourne_vers_le_vide, test_deux_segments_se_renvoient,
              test_bras_depose_dans_le_vide, test_bras_absent_au_pied_de_la_cible,
              test_bras_mal_oriente_nest_pas_un_bras_absent, test_depart_sans_belt):
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