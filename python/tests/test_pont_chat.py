"""Tests du pont chat du jeu -> tour utilisateur d'Hermes — sans jeu ni conteneur.

Ce que le joueur tape dans Factorio n'arrivait jusqu'ici que collé dans un `tool_result`.
Pour le modèle, c'était une observation parmi d'autres — pas son interlocuteur. Mesuré sur
six parties : 19 réponses sur 22, mais l'ACTION ne suit que dans 29 % des cas quand un
chantier tourne, contre 80 % quand l'avatar est libre. Il lit, il répond poliment, et il
poursuit son plan.

Le pont donne à ces messages leur vrai statut : un tour `user` dans sa conversation, via
`hermes chat --resume <session> -q "<message>"`. Vérifié en conditions réelles avant
d'écrire ce module — le contexte Factorio est conservé, l'agent agit, et deux sessions
concurrentes n'abîment pas la base (WAL).

Lancement :
    cd python
    python -m pytest tests/test_pont_chat.py -q
"""

from __future__ import annotations

import sys

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:56s} {detail[:100]}")


def test_le_pont_attend_que_l_agent_ait_rendu_la_main() -> None:
    """DEUX AGENTS SUR UN SEUL AVATAR, C'EST LE DÉSASTRE DU 09/08.

    Relancer une session pendant qu'une autre tourne ne casse RIEN côté base — vérifié :
    quatre tours `user` écrits par deux processus concurrents, aucun perdu, SQLite est en
    WAL. Le risque est dans le JEU : deux agents raisonnent en parallèle sur un seul
    personnage et s'envoient des ordres contradictoires. Cinquante-cinq minutes de jeu
    illisibles la première fois que c'est arrivé, par accident.

    Le pont ne relance donc QUE si l'agent est arrêté, et cela se vérifie sans deviner :
    le conteneur existe ou n'existe pas.
    """
    from services.pont_chat import doit_relancer

    ok = (doit_relancer(agent_tourne=False, messages=["arrête le chantier"]) is True
          and doit_relancer(agent_tourne=True, messages=["arrête le chantier"]) is False
          and doit_relancer(agent_tourne=False, messages=[]) is False)
    rec("test_le_pont_attend_que_l_agent_ait_rendu_la_main", ok,
        "relance seulement agent arrêté ET message en attente")
    assert ok


def test_le_message_dit_d_ou_il_vient() -> None:
    """UN TOUR `user` SANS CONTEXTE SE LIT COMME UN ORDRE VENU DE NULLE PART.

    L'agent reprend une conversation vieille de plusieurs minutes, dont le dernier tour
    était son propre rapport de fin. Recevoir « arrête le chantier » sans rien d'autre
    l'oblige à deviner qui parle et depuis où. On le dit : c'est l'humain qui REGARDE
    L'ÉCRAN, et il voit ce que les compteurs ne montrent pas.

    On ne lui donne pas d'ordre sur la conduite à tenir — seulement la provenance et ce
    qu'elle implique. Ce qu'il en fait reste son arbitrage.
    """
    from services.pont_chat import composer

    texte = composer(["pose une foreuse sur le charbon", "le four est vide"])

    porte_les_deux = ("pose une foreuse" in texte and "le four est vide" in texte)
    dit_la_source = "chat" in texte.lower() or "écran" in texte.lower()
    ne_commande_pas = "tu dois" not in texte.lower() and "obéis" not in texte.lower()

    ok = porte_les_deux and dit_la_source and ne_commande_pas
    rec("test_le_message_dit_d_ou_il_vient", ok, repr(texte[:90]))
    assert ok


def main() -> int:
    for t in (test_le_pont_attend_que_l_agent_ait_rendu_la_main,
              test_le_message_dit_d_ou_il_vient):
        t()
    print(chr(10) + "=" * 72)
    nok = sum(1 for _, ok, _ in RESULTS if ok)
    for name, ok, detail in RESULTS:
        if not ok:
            print(f"  ECHEC : {name} -> {detail}")
    print(f"{nok}/{len(RESULTS)} reussies.")
    print("=" * 72)
    return 0 if nok == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
