"""Tests de l'exécution pas-à-pas de `BaseAgent` — sans jeu.

Un plan de fusion s'exécute étape par étape : poser un four, y verser du charbon, y
verser le minerai, attendre, reprendre les plaques. Chaque étape rend un dict `ok`, et
c'est là qu'un piège ancien du projet ressurgit : `ok=True` ne prouve rien. Le mod peut
répondre « d'accord » à un transfert qui n'a rien transféré, faute d'avoir l'objet.

Lancement :
    cd python
    python -m pytest tests/test_base_agent.py -q
"""

from __future__ import annotations

import sys

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:52s} {detail[:100]}")


def test_un_transfert_qui_ne_transfere_rien_est_un_echec() -> None:
    """SIX ÉTAPES VERTES, ZÉRO PLAQUE, ET AUCUNE CAUSE — partie 40, mesuré.

        essai 1   9 étapes [find_nearest, walk, mine, place_furnace, move…]
                  copper-plate : 10 -> 10 (+0)      aucune étape en erreur
        essai 2   6 étapes [place_furnace, move, move, wait, move, mine]
                  copper-plate : 10 -> 15 (+5)

    Seule différence entre les deux : vingt charbons en poche. Le four du premier essai
    n'avait pas de combustible et ne pouvait rien fondre.

    Le plan PRÉVOIT pourtant le charbon — mais sur un stock SIMULÉ : `_plan` n'ajoute
    aucune étape s'il croit qu'on a déjà les cinq unités. Or elles venaient d'être versées
    dans une foreuse par un ravitaillement (« il t'en reste 5 », puis plus rien). Le plan a
    donc versé un charbon qui n'existait plus, et `move_items` a répondu `ok=True` en ne
    déplaçant rien.

    C'est le piège fondateur du projet, sous une forme nouvelle : ne jamais croire `ok`,
    c'est l'INVENTAIRE qui tranche — la leçon de l'executor E1, où vingt-six poses
    fantômes avaient été annoncées réussies. Elle valait pour les poses ; elle vaut
    identiquement pour les transferts.

    Le rapport devient alors le pire des messages : toutes les cases vertes et rien
    d'arrivé. L'agent a mis deux minutes et deux chantiers à deviner ce qu'une phrase
    aurait dit.
    """
    from agents.base import BaseAgent
    from services.knowledge import Step

    class _ApiMenteur:
        """Le mod répond OK, et l'inventaire ne bouge pas — le cas réel."""
        def __init__(self, stock: dict) -> None:
            self.stock = dict(stock)
        def get_state(self):
            return {"inventory": dict(self.stock), "tick": 1, "ready": True}
        def move_items(self, *a, **kw):
            return {"ok": True, "detail": "move_items"}
        def run_action(self, fn, *a, **kw):
            return fn(*a, **kw)

    a = BaseAgent.__new__(BaseAgent)

    a.api = _ApiMenteur({"coal": 0})
    sans_charbon = a._execute(Step("move_items", {"item": "coal", "to_entity": True,
                                                  "count": 5}))

    a.api = _ApiMenteur({"coal": 20})
    class _ApiHonnete(_ApiMenteur):
        def move_items(self, item, *a, **kw):
            self.stock[item] = self.stock.get(item, 0) - 5
            return {"ok": True, "detail": "move_items"}
    a.api = _ApiHonnete({"coal": 20})
    avec_charbon = a._execute(Step("move_items", {"item": "coal", "to_entity": True,
                                                  "count": 5}))

    refuse = sans_charbon.get("ok") is False
    dit_quoi = "coal" in str(sans_charbon.get("detail", ""))
    laisse_passer = avec_charbon.get("ok") is True

    ok = refuse and dit_quoi and laisse_passer
    rec("test_un_transfert_qui_ne_transfere_rien_est_un_echec", ok,
        f"sans stock -> {sans_charbon} ; avec stock -> ok={avec_charbon.get('ok')}")
    assert ok


def main() -> int:
    for t in (test_un_transfert_qui_ne_transfere_rien_est_un_echec,):
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
