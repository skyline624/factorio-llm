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


def test_aucun_module_utilise_sans_etre_importe() -> None:
    """UN NOM DE MODULE NON IMPORTÉ NE SE VOIT QU'EN JEU, ET TROP TARD.

    Deux fois dans la même soirée : `knowledge.fond_en(...)` écrit dans `batir()` sans
    que `knowledge` y soit importé, et `poser_machine` / `move_items` appelés avec des
    signatures inexistantes. Les 353 tests hors ligne restaient VERTS — ces chemins ne
    sont exercés qu'avec un serveur — et la batterie est tombée de 11/12 à 2/12 sur un
    `NameError` que rien n'annonçait.

    Le piège est structurel : ce dépôt importe ses services DANS les fonctions (imports
    tardifs, pour éviter les cycles). Un module importé dans dix méthodes et oublié dans
    la onzième produit exactement ce défaut, et la relecture ne l'attrape pas puisque le
    nom paraît familier.

    On vérifie donc, fonction par fonction : tout nom de module du dépôt utilisé en
    `module.attribut` doit être importé soit au niveau du fichier, soit dans cette
    fonction précise. Analyse statique pure — aucun serveur, aucune exécution.
    """
    import ast
    import os

    racine = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    modules = {os.path.splitext(f)[0] for f in os.listdir(os.path.join(racine, "services"))
               if f.endswith(".py") and not f.startswith("_")}
    fautes: list[str] = []

    for dossier in ("agents", "services"):
        for fichier in sorted(os.listdir(os.path.join(racine, dossier))):
            if not fichier.endswith(".py") or fichier.startswith("_"):
                continue
            chemin = os.path.join(racine, dossier, fichier)
            with open(chemin, encoding="utf-8") as f:
                arbre = ast.parse(f.read(), filename=chemin)

            def _importes(noeud) -> set:
                """Les noms de modules rendus disponibles par ce nœud (non récursif)."""
                noms = set()
                for n in ast.walk(noeud):
                    if isinstance(n, ast.Import):
                        for a in n.names:
                            noms.add(a.asname or a.name.split(".")[0])
                    elif isinstance(n, ast.ImportFrom):
                        for a in n.names:
                            noms.add(a.asname or a.name)
                return noms

            def _lies(noeud) -> set:
                """Les noms LIÉS localement : paramètres et affectations.

                `services/flux.py` a une variable `journal` (une liste) et un module du
                même nom existe : sans cette distinction, chaque `journal.append` serait
                dénoncé à tort. Un nom lié localement n'est jamais le module.
                """
                noms = set()
                args = getattr(noeud, "args", None)
                if args is not None:
                    for a in (list(args.args) + list(args.kwonlyargs)
                              + list(getattr(args, "posonlyargs", []))):
                        noms.add(a.arg)
                    for a in (args.vararg, args.kwarg):
                        if a is not None:
                            noms.add(a.arg)
                for n in ast.walk(noeud):
                    if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
                        noms.add(n.id)
                    elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n is not noeud:
                        noms.add(n.name)
                return noms

            # Chaîne des parents : une fonction imbriquée hérite des imports de celle qui
            # la contient — `_pose_confirmee()` définie dans `relier()` voit son import.
            parent = {}
            for n in ast.walk(arbre):
                for enfant in ast.iter_child_nodes(n):
                    parent[id(enfant)] = n

            fonctions = [n for n in ast.walk(arbre)
                         if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
            dans_fonctions = set()
            for fn in fonctions:
                dans_fonctions |= {id(n) for n in ast.walk(fn)} - {id(fn)}
            globaux = set()
            for n in ast.walk(arbre):
                if isinstance(n, (ast.Import, ast.ImportFrom)) and id(n) not in dans_fonctions:
                    globaux |= _importes(n)

            for fn in fonctions:
                dispo, courant = set(globaux), fn
                while courant is not None:
                    if isinstance(courant, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        dispo |= _importes(courant) | _lies(courant)
                    courant = parent.get(id(courant))
                for n in ast.walk(fn):
                    if (isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
                            and n.value.id in modules and n.value.id not in dispo):
                        fautes.append(f"{dossier}/{fichier}:{n.lineno} "
                                      f"{fn.name}() utilise « {n.value.id}.{n.attr} » "
                                      f"sans importer {n.value.id}")

    assert rec("aucun module utilisé sans être importé", not fautes,
               f"{len(fautes)} faute(s)" + (f" — {fautes[0]}" if fautes else ""))


TESTS = [test_aucun_test_muet_supplementaire, test_le_seuil_reste_honnete,
         test_aucun_module_utilise_sans_etre_importe]


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
