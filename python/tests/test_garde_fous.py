"""Garde-fous sur la suite elle-même : un test qui ne peut pas échouer ne prouve rien.

Découvert en mesurant : 61 des 326 fonctions `test_*` du projet n'avaient NI `assert` NI
`raise`. Elles impriment `[FAIL]` et passent en vert sous pytest — la suite affichait donc
« tout va bien » sur des constats faux. Le piège est d'autant plus tenace que ces fonctions
sont lisibles et bien écrites : elles journalisent proprement, elles ne concluent
simplement rien.

CE FICHIER EST UN CLIQUET, pas un nettoyage. Les fonctions muettes héritées ne sont pas
corrigées ici — ce serait un chantier à part, et le faire sans les relire une par une
reviendrait à croire des constats qu'on n'a jamais vérifiés. Ce qu'on interdit, c'est d'en
AJOUTER : le compte ne doit plus monter. Chaque test neuf doit pouvoir rougir.

Corollaire pratique : écrire `assert rec(...)` plutôt que `rec(...)`, en faisant rendre à
`rec` son booléen. La trace lisible reste, et pytest voit l'échec.

Lancement :
    cd python
    python -m tests.test_garde_fous
"""

from __future__ import annotations

import ast
import pathlib
import sys

# Relevé le 31/07/2026. Ce nombre ne doit que DESCENDRE : le baisser quand on répare un
# test hérité est un progrès à saluer, le monter est le défaut que ce fichier existe pour
# empêcher.
MUETTES_TOLEREES = 54

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> bool:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:110]}")
    return ok


def _muettes() -> list[str]:
    """Les fonctions `test_*` sans `assert` ni `raise` — donc incapables d'échouer."""
    base = pathlib.Path(__file__).parent
    out = []
    for f in sorted(base.glob("test_*.py")):
        try:
            arbre = ast.parse(f.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in arbre.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not node.name.startswith("test_"):
                continue
            corps = ast.dump(node)
            if "Assert(" not in corps and "Raise(" not in corps:
                out.append(f"{f.name}::{node.name}")
    return out


def test_aucun_test_muet_supplementaire() -> None:
    """Le nombre de tests incapables d'échouer ne doit pas augmenter."""
    muettes = _muettes()
    neuves = len(muettes) - MUETTES_TOLEREES
    assert rec("aucune fonction test_* muette ajoutée", neuves <= 0,
               f"{len(muettes)} muettes pour {MUETTES_TOLEREES} tolérées"
               + (f" — {neuves} de trop : {muettes[-neuves:]}" if neuves > 0 else ""))


def test_le_seuil_reste_honnete() -> None:
    """Si l'on a réparé des tests hérités, le seuil doit suivre — sinon il se périme.

    Un cliquet qui ne descend jamais finit par autoriser une régression silencieuse : on
    répare dix tests, le seuil garde dix places libres, et dix nouveaux muets s'y logent
    sans que rien ne bronche.
    """
    reste = len(_muettes())
    assert rec("le seuil colle au réel (à resserrer si l'on a réparé)",
               reste >= MUETTES_TOLEREES - 5,
               f"{reste} muettes, seuil {MUETTES_TOLEREES} — abaisser MUETTES_TOLEREES")


TESTS = [test_aucun_test_muet_supplementaire, test_le_seuil_reste_honnete]


def main() -> int:
    for t in TESTS:
        try:
            t()
        except AssertionError:
            pass
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    print(f"\n{nok}/{len(RESULTS)} reussies.")
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
