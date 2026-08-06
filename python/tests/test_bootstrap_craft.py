"""Tests unitaires du bootstrap : fabriquer ce qu'on pose, en partant de rien.

Jusqu'ici l'agent consommait une dotation qu'un humain lui avait mise dans les poches —
21 lots posés par `preparer_reference.py`. Tant que ce stock existe, « autonome » est un
mot creux : il s'arrête quand la dotation s'épuise, et ne sait pas produire une foreuse
de plus.

Le planificateur savait pourtant enchaîner miner → fondre → crafter, mais sur une TABLE
d'items écrite à la main (`ITEM_PROD`), qui ne connaissait ni `stone-furnace`, ni
`burner-mining-drill`, ni `burner-inserter` — c'est-à-dire aucune des trois machines de
la première chaîne. Il rendait « item non couvert par le planificateur P1 ».

Ce qui est éprouvé ici, sans serveur, avec les recettes RÉELLEMENT ouvertes sur la carte
(mesurées en jeu, cf. `verify_bootstrap_craft`) :

  - un item qui a une recette se fabrique, qu'il figure ou non dans la table ;
  - la table garde ce qui ne se déduit PAS : ce qui se mine (une ressource n'a pas de
    recette) et ce qui se fond (un four n'est pas un craft à la main) ;
  - l'inventaire arbitre : ce qu'on possède déjà ne se refabrique pas ;
  - un item sans recette accessible échoue en le DISANT — la recette peut être
    verrouillée, et c'est alors la recherche qui manque, pas le planificateur.

Lancement :
    cd python
    python -m tests.test_bootstrap_craft
"""

from __future__ import annotations

import sys

from services.knowledge import ProductionGoal, plan_production

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:100]}")


# Les recettes OUVERTES au départ, relevées en jeu le 30/07/2026. Les électriques
# (small-electric-pole, inserter, electric-mining-drill) sont `enabled=false` : elles
# demandent une recherche, et n'ont donc rien à faire dans un bootstrap.
RECETTES = {
    "stone-furnace": [("stone", 5)],
    "burner-mining-drill": [("iron-plate", 3), ("iron-gear-wheel", 3), ("stone-furnace", 1)],
    "burner-inserter": [("iron-plate", 1), ("iron-gear-wheel", 1)],
    "iron-gear-wheel": [("iron-plate", 2)],
    "transport-belt": [("iron-plate", 1), ("iron-gear-wheel", 1)],
}


def _lookup(item: str):
    return RECETTES.get(item)


def _kinds(steps) -> list[str]:
    return [s.kind for s in steps]


def test_une_machine_hors_table_se_planifie_quand_meme() -> None:
    """`stone-furnace` n'est pas dans ITEM_PROD, et c'est la première chose à bâtir.

    Avant, le planificateur rendait « item non couvert par le planificateur P1 » : la
    toute première machine d'une partie était hors de sa portée.
    """
    steps = plan_production(ProductionGoal("stone-furnace", 1), {}, _lookup)
    k = _kinds(steps)
    ok = "mine_entity" in k and k[-1] == "craft_item"
    rec("test_une_machine_hors_table_se_planifie_quand_meme", ok, f"{k}")
    assert ok


def test_le_bootstrap_complet_part_de_rien() -> None:
    """Une foreuse burner depuis un inventaire VIDE : miner, fondre, crafter.

    C'est le début d'une partie de Factorio, et la seule preuve qui vaille qu'un agent
    n'a besoin de personne pour commencer.
    """
    steps = plan_production(ProductionGoal("burner-mining-drill", 1), {}, _lookup)
    k = _kinds(steps)
    # Deux crafts suffisent, et c'est un GAIN : le four de fusion repris sert de
    # composant à la foreuse burner, qui en réclame justement un. Cinq pierres
    # économisées, et un four de moins abandonné à sec sur le terrain.
    ok = ("mine_entity" in k and "place_furnace" in k and "wait" in k
          and k.count("craft_item") >= 2 and k[-1] == "craft_item"
          and any(s.kind == "mine_entity" and s.args.get("name") == "stone-furnace"
                  for s in steps))
    rec("test_le_bootstrap_complet_part_de_rien", ok,
        f"{len(steps)} étapes, {k.count('craft_item')} craft(s), {k.count('mine_entity')} minage(s)")
    assert ok


def test_ce_qu_on_possede_ne_se_refabrique_pas() -> None:
    """L'inventaire arbitre : sinon l'agent refait ce qu'il a déjà, indéfiniment."""
    plein = plan_production(ProductionGoal("stone-furnace", 1), {"stone-furnace": 5}, _lookup)
    partiel = plan_production(ProductionGoal("iron-gear-wheel", 3),
                              {"iron-gear-wheel": 1, "iron-plate": 10}, _lookup)
    ok = plein == [] and _kinds(partiel) == ["craft_item"]
    rec("test_ce_qu_on_possede_ne_se_refabrique_pas", ok,
        f"déjà 5 fours -> {len(plein)} étape(s) ; 1 gear sur 3 avec des plaques -> "
        f"{_kinds(partiel)}")
    assert ok


def test_une_recette_inaccessible_le_dit() -> None:
    """Une recette VERROUILLÉE n'est pas un trou du planificateur : il faut chercher.

    `small-electric-pole`, `inserter` et `electric-mining-drill` sont `enabled=false` sur
    une carte neuve — mesuré en jeu. Confondre « je ne sais pas faire » et « ce n'est pas
    encore débloqué » enverrait le prochain lecteur corriger le mauvais fichier.
    """
    try:
        plan_production(ProductionGoal("small-electric-pole", 1), {}, _lookup)
        motif, leve = "", False
    except ValueError as e:
        motif, leve = str(e), True
    ok = leve and "VERROUILL" in motif.upper()
    rec("test_une_recette_inaccessible_le_dit", ok, motif[:95])
    assert ok


def test_le_plan_va_chercher_le_combustible_du_four() -> None:
    """Un four de pierre BRÛLE : sans charbon, il ne fond rien.

    Mesuré les mains vides : le plan posait le four et faisait `move_items coal` en
    supposant qu'on en avait. Résultat en jeu — deux fours à `fuel=0`, trois minerais en
    attente dans l'un d'eux, et le craft suivant échouant sur « manque iron-plate: 0/6 ».
    Un plan qui consomme quelque chose doit dire d'où il vient.
    """
    steps = plan_production(ProductionGoal("iron-plate", 2), {}, _lookup)
    k = _kinds(steps)
    mines = [s.args.get("name") for s in steps if s.kind == "mine_entity"]
    ok = "coal" in mines and k.index("mine_entity") < k.index("place_furnace")
    rec("test_le_plan_va_chercher_le_combustible_du_four", ok,
        f"mine {mines} avant de poser le four")
    assert ok


def test_le_charbon_deja_en_poche_ne_se_remine_pas() -> None:
    """L'inventaire arbitre aussi pour le combustible — sinon on mine pour rien."""
    steps = plan_production(ProductionGoal("iron-plate", 2),
                            {"coal": 50, "iron-ore": 10}, _lookup)
    # Les RESSOURCES seulement : la reprise du four de fusion est aussi un `mine_entity`,
    # et c'est un rangement, pas un approvisionnement.
    mines = [s.args.get("name") for s in steps if s.kind == "mine_entity"
             and s.args.get("name") in ("iron-ore", "coal", "stone", "copper-ore")]
    ok = mines == []
    rec("test_le_charbon_deja_en_poche_ne_se_remine_pas", ok,
        f"avec 50 charbons et 10 minerais : {len(mines)} minage(s)")
    assert ok


def test_les_ressources_restent_minees_et_les_plaques_fondues() -> None:
    """La table garde ce qui ne se déduit PAS d'une recette.

    Une ressource n'a pas de recette, et une plaque ne se craft pas à la main : elle se
    fond. Si la déduction avalait ces deux cas, l'agent tenterait de « crafter » du
    minerai et n'irait jamais miner.
    """
    minerai = plan_production(ProductionGoal("iron-ore", 5), {}, _lookup)
    plaque = plan_production(ProductionGoal("iron-plate", 2), {}, _lookup)
    ok = (_kinds(minerai) == ["find_nearest", "walk_to_entity", "mine_entity"]
          and "place_furnace" in _kinds(plaque) and "wait" in _kinds(plaque))
    rec("test_les_ressources_restent_minees_et_les_plaques_fondues", ok,
        f"minerai={_kinds(minerai)} | plaque={_kinds(plaque)[:5]}")
    assert ok


def test_la_decision_de_fabriquer_porte_la_quantite() -> None:
    """Fabriquer « du charbon » ne veut rien dire : il faut dire COMBIEN.

    Mesuré en jeu, les mains vides : la chaîne burner réclame vingt charbons, l'agent en
    avait huit, et la décision ne transportait que le NOM. `fabriquer` s'en remettait
    donc à sa valeur par défaut — une unité — et le plan répondait « rien à faire,
    l'inventaire en contient déjà assez ». Neuf tours de suite, puis l'abandon définitif
    de l'action par le garde-fou d'acharnement : la chaîne n'a jamais été posée, et le
    journal accusait le minage alors que rien n'avait même été tenté.
    """
    from agents.coordinator import EtatUsine, enumerer_options
    from services.factory_doctor import diagnose
    etat = EtatUsine(machines=0, diagnostic=diagnose([]), reseau=7, production_kw=900.0,
                     electrique=False, inventaire={"coal": 8},
                     besoins_production=(("burner-mining-drill", 1), ("coal", 20)))
    f = next((o for o in enumerer_options(etat) if o.action == "fabriquer"), None)
    # Le premier manque est la foreuse ; c'est LUI qui doit être proposé, avec sa
    # quantité — et non un item choisi au hasard dans la liste.
    ok = (f is not None and f.item == "burner-mining-drill" and f.quantite == 1)
    rec("test_la_decision_de_fabriquer_porte_la_quantite", ok,
        f"item={getattr(f, 'item', None)!r} quantite={getattr(f, 'quantite', None)}")
    assert ok

    etat.inventaire = {"burner-mining-drill": 1, "coal": 8}
    f = next((o for o in enumerer_options(etat) if o.action == "fabriquer"), None)
    ok = f is not None and f.item == "coal" and f.quantite == 20
    rec("test_la_quantite_est_celle_du_besoin_pas_une_unite", ok,
        f"8 charbons en poche sur 20 requis -> fabriquer {getattr(f, 'item', None)!r} "
        f"x{getattr(f, 'quantite', None)}")
    assert ok


def test_le_bois_se_recolte_sur_les_arbres() -> None:
    """Le bois n'a AUCUNE recette : il se mine, mais sur une cible qui ne porte pas son nom.

    Blocage mesuré : `small-electric-pole` coûte 1 wood + 2 copper-cable, et
    `wooden-chest` 2 wood. Les mains vides, l'agent rendait « ni ressource, ni recette
    accessible pour 'wood' » — donc pas de coffre pour évacuer, pas de poteau pour
    l'électricité, et tout le palier électrique fermé pour une bûche.

    Le piège est dans le NOM. Le mode « mine » cherche `goal.item` en jeu ; or aucune
    entité ne s'appelle « wood » : les arbres portent `tree-01`, `dry-hairy-tree`, et
    vingt-six autres noms. Ce qu'ils ont en commun est leur TYPE, `tree`, et
    `find_target_entities` (mod) retombe justement sur le type quand le nom ne donne
    rien. La cible du minage doit donc être dissociée de l'item récolté.
    """
    steps = plan_production(ProductionGoal("wood", 4), {}, _lookup)
    kinds = _kinds(steps)
    ok_forme = kinds == ["find_nearest", "walk_to_entity", "mine_entity"]
    cibles = sorted({str(s.args.get("name")) for s in steps})
    rec("le bois se recolte sur les arbres", ok_forme and cibles == ["tree"],
        f"{kinds} visant {cibles}")
    assert ok_forme and cibles == ["tree"]


def test_le_bois_deja_en_poche_ne_se_recolte_pas() -> None:
    """Un seul rondin suffit à un poteau : la table ne doit pas rendre le bois inépuisable."""
    steps = plan_production(ProductionGoal("wood", 1), {"wood": 1}, _lookup)
    rec("le bois deja en poche ne se recolte pas", steps == [], f"{_kinds(steps)}")
    assert steps == []


def main() -> int:
    for t in (test_une_machine_hors_table_se_planifie_quand_meme,
              test_le_bootstrap_complet_part_de_rien,
              test_ce_qu_on_possede_ne_se_refabrique_pas,
              test_une_recette_inaccessible_le_dit,
              test_le_plan_va_chercher_le_combustible_du_four,
              test_le_charbon_deja_en_poche_ne_se_remine_pas,
              test_les_ressources_restent_minees_et_les_plaques_fondues,
              test_la_decision_de_fabriquer_porte_la_quantite,
              test_le_bois_se_recolte_sur_les_arbres,
              test_le_bois_deja_en_poche_ne_se_recolte_pas):
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
