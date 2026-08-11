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


def test_le_pont_ne_VOLE_pas_le_message_au_bandeau() -> None:
    """TROIS LECTEURS POUR UNE FILE, ET C'EST LE MAUVAIS QUI GAGNE.

    Partie 41, mesuré. Le joueur dépose un message pendant qu'Hermes travaille :

        file du mod                         vide
        bandeau « LE JOUEUR TE PARLE »      0 occurrence dans le journal
        message                             capté par le pont, mis de côté

    Le pont relit la file toutes les quatre secondes avec `read_messages`, qui VIDE par
    conception. Le bandeau des `tool_result` ne trouve plus rien. Or le pont ne livre son
    stock qu'au moment où l'agent rend la main — donc jamais tant que la partie tourne.
    Le message était perdu pour toute la durée du jeu.

    Le commentaire de `jouer.py` affirmait exactement le contraire : « on vide la file MÊME
    quand l'agent travaille : le bandeau des tool_result la lit de son côté ». Les deux
    consomment la même file ; un seul peut l'obtenir.

    C'est le motif de H70 — deux lecteurs, un seul message — corrigé alors entre le bandeau
    et le veilleur, sans voir qu'un TROISIÈME lecteur existait dans un autre fichier.

    Tant que l'agent travaille, le pont REGARDE sans consommer : le bandeau livre, et c'est
    la voie la plus rapide. Il ne prend la file que lorsqu'il va vraiment relancer.
    """
    from services.pont_chat import messages_en_attente

    class _Api:
        def __init__(self) -> None:
            self.file = [{"joueur": "skyline624", "texte": "pose une foreuse sur le cuivre"}]
            self.vide = 0
        def peek_messages(self):
            return {"messages": list(self.file)}
        def read_messages(self):
            self.vide += 1
            m, self.file = list(self.file), []
            return {"messages": m}

    # L'agent TRAVAILLE : on regarde, on ne prend pas — le bandeau doit pouvoir livrer.
    a = _Api()
    vus = messages_en_attente(a, consommer=False)
    reste = len(a.file)

    # On va VRAIMENT relancer : là, on prend.
    b = _Api()
    pris = messages_en_attente(b, consommer=True)

    ok = (vus and reste == 1 and a.vide == 0        # rien n'a été volé au bandeau
          and pris and b.file == [] and b.vide == 1)
    rec("test_le_pont_ne_VOLE_pas_le_message_au_bandeau", ok,
        f"regarde={vus} reste={reste} vidages={a.vide} | pris={pris} vidages={b.vide}")
    assert ok


def test_on_ne_lance_pas_un_agent_sans_mains() -> None:
    """DOUZE SECONDES POUR BRÛLER UNE PARTIE — et pas une seule action de jeu.

    Partie 39, mesuré. La carte est neuve, le serveur MCP redémarré, l'agent part :

        14:46:38  batir_une_chaine -> refusé : aucun avatar connecté au serveur
        14:46:42  repondre_au_joueur : « je ne peux pas marcher, miner ni poser »
        14:46:50  batir_une_chaine -> même refus
        14:46:52  [pont] l'agent s'est arrêté et personne ne lui parle — fin

    Le lanceur a bien fait ce qu'on lui demandait : un agent qui rend la main sans qu'on
    lui parle a FINI, et on ne le relance pas pour le plaisir. Mais la prémisse était
    fausse — il n'avait pas fini, il n'avait jamais commencé. Le client de jeu n'était pas
    connecté, et rien dans le montage ne l'avait vu.

    On ne corrige donc pas la règle de fin, qui est juste : on vérifie le PRÉREQUIS avant
    de dépenser une session. Attendre coûte quelques secondes ; lancer à vide coûte une
    partie, et laisse dans le journal la trace d'un agent qui « abandonne » alors qu'on ne
    lui avait rien donné.

    ET ON N'ATTEND PAS INDÉFINIMENT : passé le délai, on le dit et on s'arrête. Lancer
    quand même refabriquerait exactement le défaut qu'on corrige.
    """
    from services.pont_chat import attendre_l_avatar, avatar_present

    class _Api:
        def __init__(self, arrive_au: int) -> None:
            self.vus, self.arrive_au = 0, arrive_au
        def get_state(self):
            self.vus += 1
            return {"character": {"x": 0, "y": 0}} if self.vus >= self.arrive_au else {}

    class _ApiMuet:
        def get_state(self):
            raise RuntimeError("rcon coupé")

    sommeils: list[float] = []
    tardif = _Api(arrive_au=4)
    venu = attendre_l_avatar(tardif, dort=sommeils.append, essais=10)

    jamais = _Api(arrive_au=999)
    absent = attendre_l_avatar(jamais, dort=lambda s: None, essais=3)

    ok = (venu is True and len(sommeils) == 3            # trois attentes, puis il est là
          and absent is False                            # on abandonne, on ne lance pas
          and avatar_present(_Api(arrive_au=1)) is True
          and avatar_present(_ApiMuet()) is False)       # rien vu = pas d'avatar
    rec("test_on_ne_lance_pas_un_agent_sans_mains", ok,
        f"arrive apres {len(sommeils)} attentes={venu} ; jamais la={absent}")
    assert ok


def main() -> int:
    for t in (test_le_pont_attend_que_l_agent_ait_rendu_la_main,
              test_le_message_dit_d_ou_il_vient,
              test_le_pont_ne_VOLE_pas_le_message_au_bandeau,
              test_on_ne_lance_pas_un_agent_sans_mains):
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
