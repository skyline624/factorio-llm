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


def test_un_craft_qui_ne_produit_rien_le_dit() -> None:
    """DEUX ÉTAPES VERTES, ZÉRO FOREUSE, ET AUCUNE CAUSE — puis elle apparaît toute seule.

    Partie 42, mesuré :

        11:16:26  ÉCHEC — burner-mining-drill : 0 -> 0 (+0) en 2 étape(s)
                  [1:craft_item, 2:craft_item]      aucune etape en erreur
        11:16:40  inventaire : burner-mining-drill = 1

    Le craft est ASYNCHRONE côté jeu : `craft_item` met la recette en file et rend
    `ok=True` aussitôt, alors que l'objet n'arrive dans l'inventaire qu'un instant plus
    tard. On relisait trop tôt, et le rapport annonçait un échec sans cause pour une
    fabrication qui allait réussir.

    C'est la leçon d'E1 — c'est l'inventaire qui tranche — mais avec sa conséquence
    inverse : ici il ne faut pas seulement RELIRE, il faut ATTENDRE avant de relire. Un
    verdict prématuré est aussi faux qu'un `ok=True` cru sur parole.
    """
    from agents.base import BaseAgent
    from services.knowledge import Step

    class _ApiLent:
        """Le jeu met la recette en file : l'objet arrive au 3e relevé."""
        def __init__(self) -> None:
            self.lectures = 0
        def get_state(self):
            self.lectures += 1
            n = 1 if self.lectures >= 3 else 0
            return {"inventory": {"burner-mining-drill": n}, "tick": 1, "ready": True}
        def craft_item(self, item, count, **kw):
            return {"ok": True, "detail": "mis en file"}
        def run_action(self, fn, *a, **kw):
            kw.pop('timeout', None)
            return fn(*a, **kw)

    class _ApiJamais:
        def get_state(self):
            return {"inventory": {}, "tick": 1, "ready": True}
        def craft_item(self, item, count, **kw):
            return {"ok": True, "detail": "mis en file"}
        def run_action(self, fn, *a, **kw):
            kw.pop('timeout', None)
            return fn(*a, **kw)

    a = BaseAgent.__new__(BaseAgent)
    dodos: list[float] = []

    a.api = _ApiLent()
    a._dort = dodos.append
    lent = a._execute(Step("craft_item", {"item": "burner-mining-drill", "count": 1}))

    a.api = _ApiJamais()
    a._dort = lambda s: None
    jamais = a._execute(Step("craft_item", {"item": "burner-mining-drill", "count": 1}))

    patiente = lent.get("ok") is True and dodos          # il a attendu, puis constaté
    dit_le_vide = (jamais.get("ok") is False
                   and "burner-mining-drill" in str(jamais.get("detail", "")))

    ok = patiente and dit_le_vide
    rec("test_un_craft_qui_ne_produit_rien_le_dit", ok,
        f"file lente -> {lent} ({len(dodos)} attentes) | jamais -> {jamais}")
    assert ok


def main() -> int:
    for t in (test_un_transfert_qui_ne_transfere_rien_est_un_echec,
              test_un_craft_qui_ne_produit_rien_le_dit):
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
