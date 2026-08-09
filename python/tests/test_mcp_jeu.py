"""Tests des outils MCP — la façade qu'Hermes manipule.

Ces outils ne calculent rien : ils traduisent une demande de l'agent en appel de service
déterministe. Leur seul travail propre est donc de ne pas DÉFORMER la demande, et c'est
précisément là qu'un défaut est passé.

Lancement :
    cd python
    python -m pytest tests/test_mcp_jeu.py -q
"""

from __future__ import annotations

import sys

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:100]}")


class _ApiCarte:
    """Le jeu répond ce qu'il y a réellement à une position."""

    def __init__(self, entites):
        self.entites = entites

    def inspect_at(self, x, y, radius=0.5):
        return {"entities": [e for e in self.entites
                             if abs(e["x"] - x) <= radius and abs(e["y"] - y) <= radius]}


def test_reparer_lit_le_nom_reel_de_la_machine() -> None:
    """« MACHINE » N'EST LE NOM D'AUCUNE ENTITÉ DU JEU.

    Partie 11 d'Hermes, en direct. Il diagnostique, repère une foreuse en `no_fuel` avec
    495 minerais sous elle, a cinquante charbons en poche, et appelle :

        reparer(quoi='ravitailler', x=1.0, y=5.0, nom_machine='')

    Réponse : « ÉCHEC — ravitaillement de machine@(1.0,5.0) (n°0) ». L'outil remplaçait
    le nom manquant par le mot « machine », et `move_items_at` cherchait donc une entité
    de ce nom à cette position. Il n'en existe aucune : le versement échouait toujours,
    alors que le combustible était là et la machine à portée.

    L'agent ne DOIT pas avoir à connaître le nom du prototype pour désigner ce qu'il
    voit — il donne une position, le jeu sait ce qui s'y trouve. On le lui demande.
    """
    from mcp_jeu import _machine_a

    api = _ApiCarte([{"name": "burner-mining-drill", "type": "mining-drill",
                      "x": 1.0, "y": 5.0},
                     {"name": "transport-belt", "type": "transport-belt",
                      "x": 2.5, "y": 5.5}])

    lu = _machine_a(api, 1.0, 5.0)
    # Une position vide reste sans nom inventé : on ne devine pas davantage qu'avant.
    vide = _machine_a(api, 50.0, 50.0)

    ok = lu == "burner-mining-drill" and vide == ""
    rec("test_reparer_lit_le_nom_reel_de_la_machine", ok,
        f"(1,5) -> « {lu} » ; (50,50) -> « {vide} »")
    assert ok


def test_reparer_prefere_une_machine_a_une_belt() -> None:
    """Un raccord de belt passe souvent sous le curseur ; ce n'est pas ce qu'on répare.

    Le diagnostic désigne une MACHINE en panne. Si plusieurs entités se touchent à la
    position donnée, on retient celle qui peut tomber en panne — foreuse, four,
    assembleuse — et non l'organe de transit qui la longe. C'est la même règle que
    `factory_doctor`, qui n'accuse jamais un inserter ou une belt d'être une cause.
    """
    from mcp_jeu import _machine_a

    api = _ApiCarte([{"name": "transport-belt", "type": "transport-belt",
                      "x": 6.0, "y": 6.0},
                     {"name": "stone-furnace", "type": "furnace", "x": 6.0, "y": 6.0}])
    lu = _machine_a(api, 6.0, 6.0)
    ok = lu == "stone-furnace"
    rec("test_reparer_prefere_une_machine_a_une_belt", ok, f"(6,6) -> « {lu} »")
    assert ok


def test_une_lecture_ne_patiente_pas_derriere_une_construction() -> None:
    """REGARDER PENDANT QU'ON BÂTIT — le verrou de H13 était trop large.

    Partie 17, mesuré : `batir_une_chaine` dure 2261 s, et `etat_du_jeu` lancé pendant
    ce temps répond en **457 s**. L'agent ne peut pas observer son usine pendant qu'il
    la construit — il attend, aveugle, la fin d'une action qu'il a lui-même lancée.

    H13 sérialisait TOUS les outils derrière un verrou unique, au motif que le lien RCON
    n'est pas réentrant. C'est vrai, mais le client le gère déjà : `RconClient.query`
    prend son propre `threading.Lock` à chaque échange, donc deux appels concurrents se
    sérialisent au niveau de la REQUÊTE, pas de l'outil entier.

    Le verrou reste indispensable entre deux ÉCRITURES — deux constructions pilotant le
    même avatar produiraient n'importe quoi. Les lectures, elles, n'ont aucune raison
    d'attendre : elles n'engagent pas le personnage.
    """
    import mcp_jeu

    lecture = [t for t in ("etat_du_jeu", "regarder", "diagnostiquer",
                           "ou_sont_les_ressources", "ce_que_l_usine_a_produit")]
    ecriture = [t for t in ("batir_une_chaine", "se_procurer", "reparer",
                            "se_deplacer", "batir_une_centrale")]

    manquants = [n for n in lecture + ecriture if not hasattr(mcp_jeu, n)]
    sans_verrou = [n for n in lecture if getattr(mcp_jeu, n, None) is not None
                   and getattr(getattr(mcp_jeu, n), "__fl_ecrit__", True) is False]
    avec_verrou = [n for n in ecriture if getattr(mcp_jeu, n, None) is not None
                   and getattr(getattr(mcp_jeu, n), "__fl_ecrit__", False) is True]

    ok = (not manquants and sorted(sans_verrou) == sorted(lecture)
          and sorted(avec_verrou) == sorted(ecriture))
    rec("test_une_lecture_ne_patiente_pas_derriere_une_construction", ok,
        f"lectures libres : {len(sans_verrou)}/{len(lecture)} — "
        f"écritures verrouillées : {len(avec_verrou)}/{len(ecriture)}"
        + (f" — absents : {manquants}" if manquants else ""))
    assert ok


def main() -> int:
    for t in (test_une_lecture_ne_patiente_pas_derriere_une_construction,
              test_reparer_lit_le_nom_reel_de_la_machine,
              test_reparer_prefere_une_machine_a_une_belt):
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
