"""Ce qu'il faut faire pour ouvrir une recette verrouillée.

Jusqu'ici le planificateur s'arrêtait sur « ni ressource, ni recette accessible pour
`small-electric-pole` ». C'est vrai et inutilisable : la recette existe, elle est
simplement fermée, et rien ne disait par quoi. L'agent ne pouvait donc ni la contourner,
ni aller la chercher — il constatait.

Ce module traduit un manque en ACTION. Il ne décide rien et ne touche à rien : il lit
l'arbre (`get_technologies`) et répond à deux questions.

  - *Quelle technologie ouvre cette recette, et est-elle à ma portée ?*
  - *Que dois-je faire pour l'obtenir ?*

**Le prix d'une technologie n'est pas toujours en flacons**, et c'est ce qui rend le
début de partie franchissable. Dans Factorio 2.0 les premières marches sont des
DÉCLENCHEURS : `electronics` s'ouvre en fabriquant dix plaques de cuivre,
`automation-science-pack` en fabriquant un laboratoire. Un agent qui sait fondre peut
donc ouvrir tout l'électrique de base sans posséder ni laboratoire ni flacon. Traiter
toutes les technologies comme un coût en science ferait attendre indéfiniment un prix
qui ne vient jamais.

Une nuance mesurée en jeu, et elle coûte cher si on l'ignore : un déclencheur
`craft-item` compte les objets FABRIQUÉS, pas ceux qu'on possède. Demander « fabrique-moi
dix plaques de cuivre » quand on en a déjà trente ne déclenche rien — le plan répond
« l'inventaire en contient déjà assez » et la technologie reste fermée. Il faut en
produire autant EN PLUS.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class Marche:
    """Une technologie à portée, et ce qu'elle réclame pour tomber."""
    nom: str
    debloque: tuple[str, ...] = ()
    # Le geste qui l'ouvre, quand il y en a un : ("craft-item", "copper-plate", 10).
    declencheur: Optional[tuple[str, str, int]] = None
    # Le prix en flacons, quand il y en a un : (("automation-science-pack", 1),).
    cout: tuple[tuple[str, int], ...] = ()
    unites: int = 0

    @property
    def gratuite(self) -> bool:
        """Rien à payer en science : un geste suffit, ou même rien du tout."""
        return not self.cout

    def __str__(self) -> str:
        if self.declencheur is not None:
            t, cible, n = self.declencheur
            geste = {"craft-item": "fabriquer", "mine-entity": "miner",
                     "capture-spawner": "capturer"}.get(t, t)
            comment = f"{geste} {n} {cible}"
        elif self.cout:
            comment = (f"{self.unites} x "
                       + " + ".join(f"{c} {n}" for n, c in self.cout))
        else:
            comment = "rien à faire"
        # ASCII dans une chaîne DESTINÉE AU JOURNAL : une flèche Unicode fait tomber
        # l'affichage dès que la sortie est redirigée vers un fichier (cp1252 sous
        # Windows). Le projet l'a déjà payé une fois avec un « ≥ ».
        return f"{self.nom} ({comment}) -> {', '.join(self.debloque) or 'rien'}"


@dataclass
class Arbre:
    """L'état de la recherche à un instant donné."""
    acquises: frozenset[str] = frozenset()
    marches: tuple[Marche, ...] = ()
    en_cours: Optional[str] = None

    def pour_recette(self, recette: str) -> Optional[Marche]:
        """La marche à portée qui ouvrirait cette recette. None si aucune."""
        for m in self.marches:
            if recette in m.debloque:
                return m
        return None

    @property
    def sans_flacons(self) -> tuple[Marche, ...]:
        """Les marches franchissables sans laboratoire ni science.

        Ce sont les seules que peut viser un agent qui n'a pas encore d'usine de
        science — c'est-à-dire tout agent au début d'une partie.
        """
        return tuple(m for m in self.marches if m.gratuite)


def lire(api, seulement_pretes: bool = True) -> Arbre:
    """Lit l'arbre par RCON. Rend un Arbre vide si la lecture échoue.

    Un arbre vide se lit comme « rien à chercher », ce qui est le comportement d'avant
    ce module : une lecture ratée ne doit pas rendre l'agent plus bête qu'il ne l'était.
    """
    try:
        brut = api.get_technologies(seulement_pretes)
    except Exception:
        return Arbre()
    if not isinstance(brut, dict):
        return Arbre()
    marches = []
    for o in brut.get("ouvertes") or []:
        if not isinstance(o, dict) or not o.get("name"):
            continue
        d = o.get("declencheur")
        decl = None
        if isinstance(d, dict) and d.get("cible"):
            decl = (str(d.get("type") or ""), str(d["cible"]), int(d.get("count") or 1))
        cout = tuple((str(c.get("name")), int(c.get("count") or 1))
                     for c in (o.get("cout") or []) if isinstance(c, dict) and c.get("name"))
        marches.append(Marche(
            nom=str(o["name"]),
            debloque=tuple(str(x) for x in (o.get("debloque") or [])),
            declencheur=decl,
            cout=cout,
            unites=int(o.get("unites") or 0),
        ))
    return Arbre(acquises=frozenset(str(a) for a in (brut.get("acquises") or [])),
                 marches=tuple(marches),
                 en_cours=brut.get("en_cours") or None)


def quantite_a_produire(marche: Marche, inventaire: dict[str, int]) -> tuple[str, int]:
    """Combien demander pour qu'un déclencheur `craft-item` compte VRAIMENT.

    Le déclencheur compte ce qu'on FABRIQUE, jamais ce qu'on possède. Mesuré en jeu :
    avec trente plaques de cuivre en poche, demander « fabrique 10 copper-plate » rend
    « l'inventaire en contient déjà assez », rien n'est produit, et la technologie reste
    fermée. On demande donc ce qu'on a DÉJÀ, plus ce que le déclencheur réclame.

    Rend (item, quantité totale à viser). ("", 0) si la marche n'a pas de déclencheur
    d'objet.
    """
    if marche.declencheur is None:
        return ("", 0)
    type_, cible, combien = marche.declencheur
    if type_ != "craft-item":
        return ("", 0)
    return (cible, int(inventaire.get(cible, 0)) + combien)
