"""Tests unitaires du PowerPlanner — dimensionnement d'une centrale vapeur.

Aucun serveur : le dimensionnement est du calcul pur. Ce qui est vérifié ici, ce ne
sont pas des nombres recopiés du code (ce serait tautologique) mais les PROPRIÉTÉS
que le dimensionnement doit tenir :

  - la capacité installée couvre TOUJOURS la demande (jamais de sous-dimensionnement) ;
  - les ratios se dérivent des deux mesures faites en jeu (30 vapeur/s par engine,
    1200 eau/s par pompe) et non de constantes posées à la main ;
  - la vapeur produite par les boilers couvre celle que consomment les engines ;
  - l'eau pompée couvre celle que consomment les boilers ;
  - la consommation de combustible est cohérente avec la puissance installée
    (conservation de l'énergie : kW installés = charbon/s x MJ/unité, au rendement près).

Chaque test porte un `assert` : `rec()` seul n'échoue pas sous pytest (faux verts).

Lancement :
    cd python
    python -m tests.test_power_planner
"""

from __future__ import annotations

import sys

from services.power_planner import (
    BOILERS_PER_PUMP, BOILER_POWER_KW, BOILER_WATER_PER_S, ENGINES_PER_BOILER,
    ENGINE_POWER_KW, ENGINE_STEAM_PER_S, FUEL_MJ, PUMP_WATER_PER_S,
    PowerRequest, describe_sizing, fuel_autonomy_s, size_power,
)

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:46s} {detail[:100]}")


def test_ratios_derives_des_mesures() -> None:
    """Les ratios tombent des deux valeurs lues en jeu, ils ne sont pas saisis."""
    ok = (ENGINE_POWER_KW == ENGINE_STEAM_PER_S * 30.0          # 30 vapeur/s x 30 kJ
          and ENGINES_PER_BOILER == BOILER_POWER_KW / 30.0 / ENGINE_STEAM_PER_S
          and BOILERS_PER_PUMP == PUMP_WATER_PER_S / BOILER_WATER_PER_S)
    rec("test_ratios_derives_des_mesures", ok,
        f"engine={ENGINE_POWER_KW:.0f}kW engines/boiler={ENGINES_PER_BOILER:.0f} "
        f"boilers/pompe={BOILERS_PER_PUMP:.0f}")
    assert ok


def test_capacite_couvre_toujours_la_demande() -> None:
    """Propriété centrale : jamais de sous-dimensionnement, quelle que soit la demande."""
    mauvais = []
    for kw in (1, 10, 899, 900, 901, 1799, 1800, 5400, 18000, 100000):
        s = size_power(PowerRequest(demand_kw=kw))
        if not s.ok or s.capacity_kw < kw:
            mauvais.append((kw, s.capacity_kw))
    rec("test_capacite_couvre_toujours_la_demande", not mauvais,
        f"defauts={mauvais or 'aucun'}")
    assert not mauvais, mauvais


def test_chaine_coherente_vapeur_et_eau() -> None:
    """Les boilers produisent assez de vapeur, les pompes assez d'eau — à tout format."""
    mauvais = []
    for kw in (900, 1000, 4500, 20000, 54000):
        s = size_power(PowerRequest(demand_kw=kw))
        vapeur_dispo = s.boilers * BOILER_POWER_KW / 30.0
        eau_dispo = s.pumps * PUMP_WATER_PER_S
        if vapeur_dispo < s.steam_per_s or eau_dispo < s.water_per_s:
            mauvais.append((kw, vapeur_dispo, s.steam_per_s, eau_dispo, s.water_per_s))
    rec("test_chaine_coherente_vapeur_et_eau", not mauvais,
        f"defauts={mauvais or 'aucun'}")
    assert not mauvais, mauvais


def test_conservation_energie_combustible() -> None:
    """kW installes ~= charbon/s x MJ/unite : le combustible colle a la puissance.

    Tolerance : la capacite est un multiple de 900 kW alors que le combustible suit
    les boilers (1800 kW), donc un demi-boiler d'ecart est normal.
    """
    mauvais = []
    for kw in (900, 1800, 3600, 9000):
        s = size_power(PowerRequest(demand_kw=kw, fuel="coal"))
        puissance_thermique = s.fuel_per_s * FUEL_MJ["coal"] * 1000.0   # kW
        if not (s.capacity_kw <= puissance_thermique + 1
                and puissance_thermique <= s.capacity_kw + BOILER_POWER_KW):
            mauvais.append((kw, s.capacity_kw, puissance_thermique))
    rec("test_conservation_energie_combustible", not mauvais,
        f"defauts={mauvais or 'aucun'}")
    assert not mauvais, mauvais


def test_petite_demande_une_machine_de_chaque() -> None:
    """1 kW demande -> la centrale minimale : 1 pompe, 1 boiler, 1 engine."""
    s = size_power(PowerRequest(demand_kw=1.0))
    ok = s.ok and (s.pumps, s.boilers, s.engines) == (1, 1, 1) and s.capacity_kw == 900.0
    rec("test_petite_demande_une_machine_de_chaque", ok,
        f"{s.pumps}p/{s.boilers}b/{s.engines}e capacite={s.capacity_kw}")
    assert ok


def test_marge_augmente_le_dimensionnement() -> None:
    """La marge sert a ne pas decrocher au moindre pic : elle doit ajouter du materiel."""
    sans = size_power(PowerRequest(demand_kw=1800.0))
    avec = size_power(PowerRequest(demand_kw=1800.0, margin=0.5))
    ok = avec.capacity_kw > sans.capacity_kw and avec.engines > sans.engines
    rec("test_marge_augmente_le_dimensionnement", ok,
        f"sans={sans.capacity_kw:.0f}kW ({sans.engines}e) avec 50%={avec.capacity_kw:.0f}kW "
        f"({avec.engines}e)")
    assert ok


def test_demande_nulle_et_combustible_inconnu() -> None:
    """Refuser proprement plutot que de rendre une centrale vide ou incoherente."""
    zero = size_power(PowerRequest(demand_kw=0.0))
    neg = size_power(PowerRequest(demand_kw=-5.0))
    bad = size_power(PowerRequest(demand_kw=900.0, fuel="uranium-235"))
    ok = (zero.feasibility == "no_demand" and neg.feasibility == "no_demand"
          and bad.feasibility == "unknown_fuel"
          and zero.engines == 0 and bad.engines == 0)
    rec("test_demande_nulle_et_combustible_inconnu", ok,
        f"zero={zero.feasibility} neg={neg.feasibility} fuel={bad.feasibility}")
    assert ok


def test_combustibles_alternatifs() -> None:
    """Un combustible plus energetique se consomme moins vite, a puissance egale."""
    charbon = size_power(PowerRequest(demand_kw=1800.0, fuel="coal"))
    solide = size_power(PowerRequest(demand_kw=1800.0, fuel="solid-fuel"))
    ok = (charbon.capacity_kw == solide.capacity_kw
          and solide.fuel_per_s < charbon.fuel_per_s)
    rec("test_combustibles_alternatifs", ok,
        f"coal={charbon.fuel_per_s}/s solid-fuel={solide.fuel_per_s}/s "
        f"(meme capacite {charbon.capacity_kw:.0f} kW)")
    assert ok


def test_autonomie_combustible() -> None:
    """L'autonomie est la grandeur qui juge une alimentation, pas le statut instantane.

    Le bootstrap burner mettait 5 charbons par machine : ~2 minutes. Le contrôle
    disait « working » et la chaîne mourait juste apres. On veut pouvoir le calculer.
    """
    s = size_power(PowerRequest(demand_kw=900.0))         # 1 boiler
    court = fuel_autonomy_s(s, 5)
    long = fuel_autonomy_s(s, 500)
    ok = court < 20.0 and long > 1000.0 and long > court
    rec("test_autonomie_combustible", ok,
        f"5 charbons={court}s, 500 charbons={long}s (1 boiler a {s.fuel_per_s}/s)")
    assert ok


def test_describe_lisible() -> None:
    s = size_power(PowerRequest(demand_kw=2700.0))
    txt = describe_sizing(s)
    ko = describe_sizing(size_power(PowerRequest(demand_kw=0)))
    ok = "kW installes" in txt and "coal/s" in txt and "non dimensionnable" in ko
    rec("test_describe_lisible", ok, f"{txt} | {ko}")
    assert ok


def test_implantation_chainage_geometrique() -> None:
    """Le chaînage réel : ports d'eau des boilers jointifs, moteurs sur la vapeur.

    On rejoue les positions de ports MESURÉES en jeu (E3a), pas les formules du
    planner — sinon le test ne vérifie que sa propre arithmétique. C'est la leçon du
    MicroPlanner : des positions « correctes » ne prouvent rien, seul le chaînage compte.
    """
    from services.power_planner import plan_power
    p = plan_power(PowerRequest(demand_kw=3600.0), origin=(10.0, 0.0), pump_pos=(0.0, 20.0))
    boilers = [e for e in p.entities if e.role == "boiler"]
    engines = [e for e in p.entities if e.role == "steam-engine"]
    pipes = {(e.x, e.y) for e in p.entities if e.role == "pipe"}
    bad = []
    # 1. Eau : le voisin du port DROIT du boiler i et celui du port GAUCHE du boiler
    #    i+1 tombent sur la même tuile, et un tuyau doit s'y trouver pour les relier.
    for a, b in zip(boilers, boilers[1:]):
        voisin_droit = (a.x + 2.0, a.y + 0.5)
        voisin_gauche = (b.x - 2.0, b.y + 0.5)
        if voisin_droit != voisin_gauche:
            bad.append(f"ports d'eau non face a face: {voisin_droit} vs {voisin_gauche}")
        elif voisin_droit not in pipes:
            bad.append(f"aucun tuyau entre les boilers en {voisin_droit}")
    # 2. Vapeur : voisin de la sortie du boiler (0,-1.5) == port sud du moteur (0,+2).
    for b in boilers:
        col = sorted([e for e in engines if e.x == b.x], key=lambda e: -e.y)
        if not col:
            bad.append(f"boiler@{b.x} sans moteur")
            continue
        if (b.x, b.y - 1.5) != (col[0].x, col[0].y + 2.0):
            bad.append(f"moteur non raccorde a la vapeur: {col[0].y} vs {b.y - 1.5}")
        # 3. Moteurs en enfilade : sortie nord (0,-3) == port sud du suivant (0,+2).
        for m, n in zip(col, col[1:]):
            if (m.x, m.y - 3.0) != (n.x, n.y + 2.0):
                bad.append(f"moteurs non enfiles: {m.y} -> {n.y}")
    rec("test_implantation_chainage_geometrique", not bad, f"anomalies={bad or 'aucune'}")
    assert not bad, bad


def test_implantation_grille_et_absence_de_chevauchement() -> None:
    """Positions sur la grille légale, et aucune entité n'en recouvre une autre."""
    from services.power_planner import plan_power
    p = plan_power(PowerRequest(demand_kw=2700.0), origin=(7.3, -4.8), pump_pos=(0.0, 10.0))
    bad = []
    for e in p.entities:
        if e.role == "boiler" and not (abs(e.x % 1) == 0.5 and e.y % 1 == 0):
            bad.append(f"boiler hors grille ({e.x},{e.y})")   # 3x2 -> x en .5, y entier
        if e.role in ("pipe", "pole") and not (abs(e.x % 1) == 0.5 and abs(e.y % 1) == 0.5):
            bad.append(f"{e.role} hors centre de tuile ({e.x},{e.y})")
    # Emprises (w, h) par rôle, d'après les bbox MESURÉS en jeu (E3a). L'offshore-pump
    # compte pour 1x1 : sa taille nominale est 2x2 mais son bbox réel vaut 1.2 x 1.3,
    # et la tuile juste au-dessus de lui est précisément celle où son tuyau de sortie
    # doit se brancher (port (0,0) -> voisin (0,+1)).
    taille = {"boiler": (3, 2), "steam-engine": (3, 5), "pipe": (1, 1),
              "pole": (1, 1), "offshore-pump": (1, 1)}
    for i, a in enumerate(p.entities):
        for b in p.entities[i + 1:]:
            aw, ah = taille[a.role]
            bw, bh = taille[b.role]
            if (abs(a.x - b.x) < (aw + bw) / 2 - 1e-6
                    and abs(a.y - b.y) < (ah + bh) / 2 - 1e-6):
                bad.append(f"{a.role}@({a.x},{a.y}) recouvre {b.role}@({b.x},{b.y})")
    rec("test_implantation_grille_et_absence_de_chevauchement", not bad,
        f"{len(p.entities)} entites, anomalies={bad[:3] or 'aucune'}")
    assert not bad, bad


def test_implantation_sans_eau_refuse() -> None:
    """Pas de bord d'eau -> pas de plan complet : une centrale sans eau ne démarre jamais."""
    from services.power_planner import plan_power
    sec = plan_power(PowerRequest(demand_kw=900.0), origin=(0.0, 0.0))
    mouille = plan_power(PowerRequest(demand_kw=900.0), origin=(0.0, 0.0), pump_pos=(0.0, 12.0))
    ok = (sec.feasibility == "no_water"
          and not [e for e in sec.entities if e.role == "offshore-pump"]
          and mouille.ok
          and len([e for e in mouille.entities if e.role == "offshore-pump"]) == 1
          and len([e for e in mouille.entities if e.role == "pipe"]) > 0)
    rec("test_implantation_sans_eau_refuse", ok,
        f"sans eau={sec.feasibility} | avec eau={mouille.feasibility} "
        f"pipes={len([e for e in mouille.entities if e.role == 'pipe'])}")
    assert ok


def test_implantation_totaux_coherents_avec_dimensionnement() -> None:
    """Le nombre d'entités posées correspond exactement au dimensionnement."""
    from services.power_planner import plan_power
    r = PowerRequest(demand_kw=4500.0)
    s = size_power(r)
    p = plan_power(r, origin=(0.0, 0.0), pump_pos=(-10.0, 6.0))
    ok = (p.totals.get("boiler") == s.boilers
          and p.totals.get("steam-engine") == s.engines
          and p.totals.get("offshore-pump") == 1
          and p.sizing.capacity_kw == s.capacity_kw)
    rec("test_implantation_totaux_coherents_avec_dimensionnement", ok,
        f"dimensionne {s.boilers}b/{s.engines}e -> pose "
        f"{p.totals.get('boiler')}b/{p.totals.get('steam-engine')}e, totals={p.totals}")
    assert ok


def main() -> int:
    tests = [
        test_ratios_derives_des_mesures,
        test_capacite_couvre_toujours_la_demande,
        test_chaine_coherente_vapeur_et_eau,
        test_conservation_energie_combustible,
        test_petite_demande_une_machine_de_chaque,
        test_marge_augmente_le_dimensionnement,
        test_demande_nulle_et_combustible_inconnu,
        test_combustibles_alternatifs,
        test_autonomie_combustible,
        test_describe_lisible,
        test_implantation_chainage_geometrique,
        test_implantation_grille_et_absence_de_chevauchement,
        test_implantation_sans_eau_refuse,
        test_implantation_totaux_coherents_avec_dimensionnement,
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