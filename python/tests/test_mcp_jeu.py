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


def test_le_serveur_repond_quand_l_agent_ne_peut_pas() -> None:
    """PENDANT QU'IL ATTEND UN OUTIL, L'AGENT N'EXISTE PAS.

    Aucun tour de modèle ne tourne entre l'appel et son retour : il ne peut ni lire ni
    écrire. Mesuré ce jour — `batir_une_chaine` lancé à 14:56:05, trois messages envoyés
    entre 14:54 et 14:58, aucune réaction avant la fin de la construction. Le joueur en
    conclut que ses messages se perdent, alors qu'ils sont seulement en file.

    On ne peut pas faire parler l'agent pendant ce temps. On peut faire parler le SERVEUR,
    qui lui sait deux choses que le joueur ignore : que le message est bien arrivé, et ce
    que l'agent est en train de faire depuis combien de temps. C'est ce qui manque —
    « occupé » ne s'invente pas depuis l'extérieur, « silencieux » et « perdu » se
    ressemblent.

    Le veilleur ne consomme PAS la file : il regarde sans vider, sans quoi il détruirait
    le message avant que l'agent le voie. Deux lecteurs, deux besoins.
    """
    import mcp_jeu

    dits, lu_destructif = [], []

    class _ApiVeille:
        def peek_messages(self):
            return {"messages": [{"joueur": "pier", "texte": "tu as une foreuse ?"}]}
        def read_messages(self):
            lu_destructif.append(1)
            return {"messages": []}
        def say(self, texte):
            dits.append(texte)
            return {"ok": True}

    vrai = mcp_jeu._api
    mcp_jeu._api = lambda: _ApiVeille()
    try:
        mcp_jeu._accuser_reception("batir_une_chaine", 187.0)
    finally:
        mcp_jeu._api = vrai

    a_parle = len(dits) == 1
    dit_quoi = a_parle and "batir_une_chaine" in dits[0]
    dit_depuis = a_parle and ("3" in dits[0] or "187" in dits[0])
    na_pas_vole = not lu_destructif

    ok = a_parle and dit_quoi and dit_depuis and na_pas_vole
    rec("test_le_serveur_repond_quand_l_agent_ne_peut_pas", ok,
        f"dit={dits} file_consommee={bool(lu_destructif)}")
    assert ok


def test_le_veilleur_n_accuse_qu_une_fois_le_meme_message() -> None:
    """UN ACCUSÉ RÉPÉTÉ EST UNE NUISANCE — il s'affiche à l'écran du joueur.

    Le veilleur regarde la file toutes les quelques secondes ; le message y reste jusqu'à
    ce que l'agent le lise, ce qui peut durer les dix minutes d'un `batir_une_chaine`.
    Sans garde, le joueur verrait « bien reçu » deux cents fois par-dessus son jeu.
    """
    import mcp_jeu

    dits = []

    class _ApiVeille:
        def peek_messages(self):
            return {"messages": [{"joueur": "pier", "texte": "meme message"}]}
        def say(self, texte):
            dits.append(texte)
            return {"ok": True}

    vrai = mcp_jeu._api
    mcp_jeu._api = lambda: _ApiVeille()
    mcp_jeu._DERNIER_ACCUSE = None
    try:
        for _ in range(5):
            mcp_jeu._accuser_reception("batir_une_chaine", 60.0)
    finally:
        mcp_jeu._api = vrai

    ok = len(dits) == 1
    rec("test_le_veilleur_n_accuse_qu_une_fois_le_meme_message", ok,
        f"{len(dits)} accusé(s) pour 5 passages")
    assert ok


def test_un_chantier_rend_la_main_tout_de_suite() -> None:
    """QUATORZE MINUTES SANS LA MAIN, C'EST QUATORZE MINUTES DE SURDITÉ.

    Partie 23 : `batir_une_chaine` part à 14:56:05 et rend à 15:10:09. Le joueur écrit
    trois fois pendant ce temps et ne voit rien venir. Ce n'est pas qu'un agent occupé
    ignore les messages — entre l'appel d'un outil et son retour, aucun tour de modèle ne
    tourne. Il n'est pas sourd, il n'est pas là.

    Le premier réflexe fut de faire parler le serveur à sa place, le second d'interrompre
    le chantier dès qu'un message arrive. Les deux décident À SA PLACE. Ce que le joueur a
    demandé est plus juste : que le travail CONTINUE, que l'agent garde la main, et que ce
    soit LUI qui juge, son message lu, s'il coupe ou s'il laisse finir.

    L'outil rend donc immédiatement un numéro de chantier. L'agent suit par `ou_en_est`
    quand il veut — et c'est là qu'il reçoit ce qu'on lui dit, quelques secondes après,
    au lieu de quatorze minutes.
    """
    import mcp_jeu, time

    def _long():
        time.sleep(0.4)
        return "chaîne bâtie : 29 entités"

    t0 = time.time()
    reponse = mcp_jeu._lancer_chantier("batir_une_chaine", _long)
    rendu_en = time.time() - t0

    tout_de_suite = rendu_en < 0.2
    donne_un_numero = "1" in reponse
    dit_quoi_faire = "ou_en_est" in reponse

    ok = tout_de_suite and donne_un_numero and dit_quoi_faire
    rec("test_un_chantier_rend_la_main_tout_de_suite", ok,
        f"rendu en {rendu_en:.3f}s — {reponse!r}")
    assert ok


def test_le_chantier_dit_ou_il_en_est_puis_son_resultat() -> None:
    """SUIVRE N'EST PAS ATTENDRE — et le résultat ne doit pas se perdre en route.

    Un chantier qu'on lance sans pouvoir en lire l'issue ne vaut rien : l'agent saurait
    qu'il a demandé une chaîne, jamais si elle est là. `ou_en_est` répond donc « en cours »
    tant qu'il tourne, puis rend le résultat COMPLET une fois fini — le même texte que
    l'outil synchrone rendait avant.
    """
    import mcp_jeu, time

    mcp_jeu._lancer_chantier("batir_une_chaine", lambda: (time.sleep(0.3), "29 entités")[1])
    pendant = mcp_jeu.ou_en_est_le_chantier.fn() if hasattr(
        mcp_jeu.ou_en_est_le_chantier, "fn") else mcp_jeu._etat_chantier()
    time.sleep(0.6)
    apres = mcp_jeu._etat_chantier()

    en_cours = "en cours" in pendant.lower()
    rendu = "29 entités" in apres

    ok = en_cours and rendu
    rec("test_le_chantier_dit_ou_il_en_est_puis_son_resultat", ok,
        f"pendant={pendant!r} apres={apres!r}")
    assert ok


def test_deux_chantiers_ne_pilotent_pas_le_meme_avatar() -> None:
    """UN SEUL AVATAR, DONC UN SEUL CHANTIER.

    Deux constructions simultanées se disputeraient le personnage : chacune le fait
    marcher ailleurs, et les poses tombent où il n'est pas. Le cas s'est produit le 09/08
    par accident — deux conteneurs lancés sur la même partie, cinquante-cinq minutes de
    jeu illisibles.

    Le second appel n'attend donc pas en silence : il dit ce qui occupe la place et
    comment reprendre la main. Un refus qui n'explique pas se retente en boucle.
    """
    import mcp_jeu, time

    mcp_jeu._lancer_chantier("batir_une_chaine", lambda: (time.sleep(0.5), "fini")[1])
    refus = mcp_jeu._lancer_chantier("batir_une_centrale", lambda: "jamais")

    refuse = "jamais" not in refus and "en cours" in refus.lower()
    dit_quoi = "batir_une_chaine" in refus
    dit_comment = "arreter" in refus.lower() or "arrête" in refus.lower()

    ok = refuse and dit_quoi and dit_comment
    rec("test_deux_chantiers_ne_pilotent_pas_le_meme_avatar", ok, repr(refus))
    assert ok


def test_arreter_le_chantier_atteint_vraiment_la_pose() -> None:
    """UN BOUTON D'ARRÊT QUI N'ARRÊTE RIEN EST PIRE QUE PAS DE BOUTON.

    `arreter_le_chantier` lève un drapeau dans le serveur MCP ; la pose, elle, se déroule
    dans l'`executor`, trois couches plus bas. Entre les deux il faut que quelqu'un porte
    le message — sinon l'agent lit « arrêt demandé », attend, et la construction va au bout
    comme si de rien n'était.

    C'est le défaut typique du dépôt : H10 posé sous un drapeau que l'appelant met à False,
    H23 posé dans une méthode que l'appelant ne traverse pas, H27 posé dans une branche
    jamais prise. Trois correctifs inertes, chacun cru bon pendant une partie entière. On
    vérifie donc le CHEMIN, pas l'intention.
    """
    import mcp_jeu

    coord = mcp_jeu._coord.__wrapped__ if hasattr(mcp_jeu._coord, "__wrapped__") else None
    # On ne construit pas de Coordinator ici (il lui faut le jeu) : on vérifie que le
    # serveur lui attache bien de quoi savoir qu'on veut l'arrêter.
    class _Faux:
        pass
    faux = _Faux()
    mcp_jeu._ETAT["coord"] = faux
    mcp_jeu._brancher_l_arret(faux)

    porte = getattr(faux, "interrompu_par", None)
    mcp_jeu._CHANTIER["arret"] = False
    avant = porte() if callable(porte) else None
    mcp_jeu._CHANTIER["arret"] = True
    apres = porte() if callable(porte) else None
    mcp_jeu._CHANTIER["arret"] = False
    mcp_jeu._ETAT["coord"] = None

    ok = callable(porte) and avant is False and apres is True
    rec("test_arreter_le_chantier_atteint_vraiment_la_pose", ok,
        f"porté={callable(porte)} avant={avant} après={apres}")
    assert ok


def main() -> int:
    for t in (test_une_lecture_ne_patiente_pas_derriere_une_construction,
              test_reparer_lit_le_nom_reel_de_la_machine,
              test_reparer_prefere_une_machine_a_une_belt,
              test_le_joueur_peut_couper_la_parole_a_l_agent,
              test_un_chat_muet_ne_coute_rien_a_l_agent,
              test_ce_qu_on_souffle_a_l_agent_laisse_une_trace,
              test_le_serveur_repond_quand_l_agent_ne_peut_pas,
              test_le_veilleur_n_accuse_qu_une_fois_le_meme_message,
              test_un_chantier_rend_la_main_tout_de_suite,
              test_le_chantier_dit_ou_il_en_est_puis_son_resultat,
              test_deux_chantiers_ne_pilotent_pas_le_meme_avatar,
              test_arreter_le_chantier_atteint_vraiment_la_pose):
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
