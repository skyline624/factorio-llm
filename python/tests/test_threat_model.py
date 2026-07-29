"""Tests unitaires du ThreatModel — quand se défendre, et de quel côté.

`evaluer` est pure : un scan entre, une menace sort. Aucun serveur.

Ce qui est vérifié est la RÈGLE de décision, pas un barème :

  - la pollution commande, pas la distance. Des nids proches sans pollution ne
    déclenchent rien ; c'est ce qui évite de fortifier trop tôt, au prix de la
    production ;
  - des unités déjà sur l'usine passent avant tout le reste : le déclencheur n'a plus
    d'importance quand elles sont là ;
  - le mode pacifique annule tout investissement défensif ;
  - le front pointe vers les nids, et les tourelles se posent en arc de ce côté — pas
    en ceinture, qui coûterait plusieurs fois plus pour la même protection.

Lancement :
    cd python
    python -m tests.test_threat_model
"""

from __future__ import annotations

import math
import sys

from services.threat_model import (
    AUCUNE, EN_COURS, IMMINENTE, LATENTE, evaluer, positions_defense,
)

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:50s} {detail[:100]}")


def _scan(nids=(), unites=0, pollution=0.0, peaceful=False, nearest_dist=None,
          proches=0) -> dict:
    nests = [{"name": "biter-spawner", "x": x, "y": y,
              "dist": math.hypot(x, y)} for x, y in nids]
    s = {"nests": nests, "nestCount": len(nests), "unitCount": unites,
         "unitsNear": proches, "pollution": pollution, "peaceful": peaceful}
    if nearest_dist is not None:
        s["nearest"] = {"name": "small-biter", "x": 0, "y": nearest_dist,
                        "dist": nearest_dist}
    return s


def test_pollution_commande_pas_la_distance() -> None:
    """Même nid, même distance : c'est la pollution qui fait basculer la décision."""
    sans = evaluer(_scan(nids=[(0, -100)], pollution=0.0))
    avec = evaluer(_scan(nids=[(0, -100)], pollution=250.0))
    ok = (sans.niveau == LATENTE and not sans.agir
          and avec.niveau == IMMINENTE and avec.agir)
    rec("test_pollution_commande_pas_la_distance", ok,
        f"sans pollution -> {sans.niveau} (agir={sans.agir}) ; "
        f"avec -> {avec.niveau} (agir={avec.agir})")
    assert ok


def test_nids_proches_sans_pollution_ne_declenchent_rien() -> None:
    """Un nid à 40 tuiles sans pollution reste LATENT : fortifier serait prématuré.

    C'est le cas qui coûte cher si on se trompe : réagir à la peur plutôt qu'au risque
    consomme le temps qui devait aller à la production.
    """
    m = evaluer(_scan(nids=[(0, -40), (30, -30)], pollution=2.0))
    ok = m.niveau == LATENTE and not m.agir and "rien ne les" in m.raison
    rec("test_nids_proches_sans_pollution_ne_declenchent_rien", ok, str(m))
    assert ok


def test_unites_sur_lusine_priment() -> None:
    """Des biters déjà là : on n'attend pas que la pollution justifie quoi que ce soit."""
    m = evaluer(_scan(nids=[(0, -300)], unites=6, proches=6, pollution=0.0,
                      nearest_dist=25.0))
    ok = m.niveau == EN_COURS and m.agir
    rec("test_unites_sur_lusine_priment", ok, str(m))
    assert ok


def test_unites_lointaines_ne_declenchent_pas() -> None:
    """Des unités au loin ne sont pas « sur l'usine » : pas d'urgence immédiate.

    Mesuré en jeu : une carte normale compte des dizaines d'unités dans un rayon de
    300 tuiles et aucune à 60. Les confondre ferait fortifier en permanence.
    """
    m = evaluer(_scan(nids=[(0, -200)], unites=39, proches=0, pollution=0.0,
                      nearest_dist=230.0))
    ok = m.niveau == LATENTE
    rec("test_unites_lointaines_ne_declenchent_pas", ok, str(m))
    assert ok


def test_mode_pacifique_annule_tout() -> None:
    """En paix, tout investissement défensif est du temps perdu — et on le dit."""
    m = evaluer(_scan(nids=[(0, -20)], unites=30, pollution=999.0, peaceful=True))
    ok = m.niveau == AUCUNE and not m.agir and "pacifique" in m.raison
    rec("test_mode_pacifique_annule_tout", ok, str(m))
    assert ok


def test_aucun_nid_aucune_menace() -> None:
    m = evaluer(_scan(nids=[], pollution=500.0))
    ok = m.niveau == AUCUNE and not m.agir
    rec("test_aucun_nid_aucune_menace", ok, str(m))
    assert ok


def test_scan_illisible_ne_produit_pas_de_fausse_certitude() -> None:
    """Face à un scan cassé, dire qu'on ne sait pas — pas « aucune menace »."""
    m = evaluer({"error": "aucune surface"})
    ok = m.niveau == AUCUNE and "non évaluable" in m.raison
    rec("test_scan_illisible_ne_produit_pas_de_fausse_certitude", ok, str(m))
    assert ok


def test_front_pointe_vers_le_nid_le_plus_proche() -> None:
    """Le front désigne d'où viendront les vagues, pas la moyenne des nids."""
    bad = []
    for (x, y), attendu in (((0, -100), "nord"), ((100, 0), "est"),
                            ((0, 100), "sud"), ((-100, 0), "ouest")):
        m = evaluer(_scan(nids=[(x, y), (300, 300)], pollution=50.0))
        if m.front_nom != attendu:
            bad.append(f"nid en ({x},{y}) -> {m.front_nom} (attendu {attendu})")
    rec("test_front_pointe_vers_le_nid_le_plus_proche", not bad, f"{bad or 'aucun écart'}")
    assert not bad, bad


def test_tourelles_en_arc_face_au_front() -> None:
    """Les tourelles se posent DEVANT, du côté des nids, et espacées entre elles.

    Ceinturer l'usine coûterait plusieurs fois plus pour la même protection : les
    vagues arrivent d'un côté.
    """
    m = evaluer(_scan(nids=[(0, -100)], pollution=50.0))
    pos = positions_defense((0.0, 0.0), m, nombre=3, distance=12.0, ecart=4.0)
    # Toutes du côté du nid (y négatif), à ~12 tuiles, et distinctes.
    ok = (len(pos) == 3
          and all(p[1] < 0 for p in pos)
          and all(abs(math.hypot(p[0], p[1]) - 12.0) < 5.0 for p in pos)
          and len(set(pos)) == 3)
    rec("test_tourelles_en_arc_face_au_front", ok, f"{pos}")
    assert ok


def test_pas_de_tourelles_sans_front() -> None:
    """Sans menace localisée, aucune position : on ne pose pas au hasard."""
    m = evaluer(_scan(nids=[], pollution=0.0))
    ok = positions_defense((0.0, 0.0), m) == []
    rec("test_pas_de_tourelles_sans_front", ok, "aucune position proposée")
    assert ok


def main() -> int:
    tests = [
        test_pollution_commande_pas_la_distance,
        test_nids_proches_sans_pollution_ne_declenchent_rien,
        test_unites_sur_lusine_priment,
        test_unites_lointaines_ne_declenchent_pas,
        test_mode_pacifique_annule_tout,
        test_aucun_nid_aucune_menace,
        test_scan_illisible_ne_produit_pas_de_fausse_certitude,
        test_front_pointe_vers_le_nid_le_plus_proche,
        test_tourelles_en_arc_face_au_front,
        test_pas_de_tourelles_sans_front,
    ]
    for t in tests:
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