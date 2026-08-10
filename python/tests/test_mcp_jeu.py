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


def test_le_joueur_peut_couper_la_parole_a_l_agent() -> None:
    """LE CHAT DU JEU EST LE SEUL CANAL VERS UN AGENT QUI JOUE DÉJÀ.

    Sans lui, corriger le tir coûte une manche entière : arrêter la partie, réécrire la
    skill, remonter carte + client + serveur MCP. Partie 22 le montre — 534 plaques
    produites par l'usine, zéro en poche, et l'agent repart miner cent cinquante plaques
    à la pioche. Une phrase aurait suffi : « vide tes fours ».

    Le point dur n'est pas de lire les messages, c'est de les LIVRER. Un outil dédié
    `lire_les_messages` ne sert à rien : l'agent ne l'appellerait que s'il pense à
    demander, or c'est précisément quand il s'enlise qu'il cesse de demander. Le message
    part donc en tête de la PROCHAINE réponse d'outil, quelle qu'elle soit — il coupe la
    parole plutôt que d'attendre son tour.

    Et il ne se répète pas. Un conseil relivré à chaque appel deviendrait un bruit de
    fond que l'agent apprendrait à sauter ; la file se vide à la lecture.
    """
    import mcp_jeu

    file = [{"joueur": "pier", "texte": "vide tes fours au lieu de miner"}]

    class _ApiChat:
        def read_messages(self):
            lu, file[:] = list(file), []
            return {"messages": lu}

    vrai = mcp_jeu._api
    mcp_jeu._api = lambda: _ApiChat()
    try:
        premier = mcp_jeu._bandeau_du_joueur("machines en service : 12")
        second = mcp_jeu._bandeau_du_joueur("machines en service : 12")
    finally:
        mcp_jeu._api = vrai

    livre = "vide tes fours" in premier
    garde = "machines en service : 12" in premier
    en_tete = premier.index("vide tes fours") < premier.index("machines en service")
    silence = "vide tes fours" not in second

    ok = livre and garde and en_tete and silence
    rec("test_le_joueur_peut_couper_la_parole_a_l_agent", ok,
        f"livré={livre} résultat_conservé={garde} en_tête={en_tete} pas_de_répétition={silence}")
    assert ok


def test_un_chat_muet_ne_coute_rien_a_l_agent() -> None:
    """LE CANAL EST OUVERT EN PERMANENCE — il doit donc être invisible quand il est vide.

    Le bandeau se greffe sur CHAQUE réponse d'outil. S'il ajoutait ne serait-ce qu'une
    ligne à vide, l'agent la lirait des centaines de fois par partie pour rien, et une
    panne du canal ferait tomber tous les outils avec elle. Quand personne ne parle, la
    réponse doit sortir à l'octet près comme si le chat n'existait pas ; et quand la
    lecture échoue, l'outil répond quand même.
    """
    import mcp_jeu

    class _ApiMuette:
        def read_messages(self):
            return {"messages": []}

    class _ApiCassee:
        def read_messages(self):
            raise RuntimeError("RCON coupé")

    vrai = mcp_jeu._api
    try:
        mcp_jeu._api = lambda: _ApiMuette()
        muet = mcp_jeu._bandeau_du_joueur("état : rien à signaler")
        mcp_jeu._api = lambda: _ApiCassee()
        casse = mcp_jeu._bandeau_du_joueur("état : rien à signaler")
    finally:
        mcp_jeu._api = vrai

    ok = muet == "état : rien à signaler" and casse == "état : rien à signaler"
    rec("test_un_chat_muet_ne_coute_rien_a_l_agent", ok,
        f"muet={muet!r} canal_casse={casse!r}")
    assert ok


def test_ce_qu_on_souffle_a_l_agent_laisse_une_trace() -> None:
    """UNE PARTIE COACHÉE NE SE COMPARE PAS À UNE PARTIE AUTONOME — encore faut-il savoir.

    Le bandeau se greffe APRÈS que le journal a enregistré le résultat : ce que le joueur
    a soufflé n'apparaît donc nulle part dans `mcp_appels.log`. Conséquence vécue le
    10/08 — trois messages envoyés, aucune réaction visible, et faute de trace j'ai
    cherché pendant dix minutes une panne du canal qui n'existait pas. Le canal marchait ;
    l'agent était simplement au milieu d'un `batir_une_chaine` de plusieurs minutes, et
    aucun outil n'avait encore rendu.

    Deux raisons de tracer, donc. La première est le diagnostic : sans trace, un message
    livré et un message perdu se ressemblent. La seconde pèse plus lourd — les résultats
    d'une partie où on a soufflé ne valent pas ceux d'une partie autonome, et on ne peut
    pas s'en souvenir après coup.
    """
    import mcp_jeu

    trace = []
    vrai_fin, vrai_api = mcp_jeu._fin, mcp_jeu._api
    file = [{"joueur": "pier", "texte": "vide tes fours"}]

    class _ApiChat:
        def read_messages(self):
            lu, file[:] = list(file), []
            return {"messages": lu}

    mcp_jeu._fin = lambda nom, res, duree=0.0: trace.append((nom, str(res)))
    mcp_jeu._api = lambda: _ApiChat()
    try:
        mcp_jeu._bandeau_du_joueur("machines en service : 12")
    finally:
        mcp_jeu._fin, mcp_jeu._api = vrai_fin, vrai_api

    ok = any("vide tes fours" in r for _, r in trace)
    rec("test_ce_qu_on_souffle_a_l_agent_laisse_une_trace", ok,
        f"{len(trace)} entrée(s) journalisée(s) : {trace}")
    assert ok


def main() -> int:
    for t in (test_une_lecture_ne_patiente_pas_derriere_une_construction,
              test_reparer_lit_le_nom_reel_de_la_machine,
              test_reparer_prefere_une_machine_a_une_belt,
              test_le_joueur_peut_couper_la_parole_a_l_agent,
              test_un_chat_muet_ne_coute_rien_a_l_agent,
              test_ce_qu_on_souffle_a_l_agent_laisse_une_trace):
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
