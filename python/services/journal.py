"""Journal persistant — ce que l'agent a fait pendant qu'on ne le regardait pas.

Tout ce que la boucle sait d'elle-même vit en mémoire : décisions, écarts, constats,
réparations. Sur un tour de cinq minutes cela suffit ; sur deux heures, un échec à la
quarantième minute devient indéboguable — l'information a existé, personne ne l'a gardée.

Format : une ligne JSON par événement (JSONL). Le choix n'est pas cosmétique — un fichier
qu'on ne peut lire qu'entièrement est inutilisable pendant qu'il s'écrit, et une partie
longue se surveille en cours de route. Chaque ligne est autonome, horodatée en TICKS de
jeu autant qu'en temps réel : à `game.speed = 10`, l'un ne se déduit pas de l'autre.

L'écriture ne lève jamais. Un agent autonome qui s'arrête parce que son disque est plein
n'est pas autonome — on perd la trace, pas la partie.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass
class Journal:
    """Écrit des événements en JSONL, et tient le compte de ce qui s'est passé.

    Les compteurs sont ce qu'on regarde en premier après coup : combien de décisions,
    combien d'écarts, combien réparés. Un journal qu'il faut relire en entier pour savoir
    si la partie s'est bien passée ne répond pas à la question qu'on lui pose.
    """
    chemin: str
    debut_reel: float = field(default_factory=time.time)
    compteurs: dict = field(default_factory=dict)
    lignes: int = 0
    erreurs: int = 0

    def __post_init__(self) -> None:
        dossier = os.path.dirname(os.path.abspath(self.chemin))
        try:
            os.makedirs(dossier, exist_ok=True)
        except OSError:
            self.erreurs += 1

    def ecrire(self, genre: str, **champs: Any) -> None:
        """Ajoute un événement. N'échoue jamais : on perd la trace, pas la partie."""
        self.compteurs[genre] = self.compteurs.get(genre, 0) + 1
        evenement = {"genre": genre, "reel": round(time.time() - self.debut_reel, 1)}
        evenement.update(champs)
        try:
            with open(self.chemin, "a", encoding="utf-8") as f:
                f.write(json.dumps(evenement, ensure_ascii=False, default=str) + "\n")
            self.lignes += 1
        except OSError:
            self.erreurs += 1

    def tour(self, n: int, tick: Optional[int], decision, agi: bool, detail: str) -> None:
        # L'arbitrage est journalisé À CHAQUE TOUR, même quand il n'a pas eu lieu :
        # « combien de fois y avait-il un vrai choix » est la question qui décide si une
        # comparaison avec/sans modèle a le moindre sens.
        a = getattr(decision, "arbitrage", None)
        self.ecrire("tour", n=n, tick=tick, action=getattr(decision, "action", "?"),
                    raison=getattr(decision, "raison", "")[:200], agi=agi,
                    detail=str(detail)[:300],
                    options=getattr(a, "options", 0),
                    faisables=getattr(a, "faisables", 0),
                    arbitrable=bool(getattr(a, "arbitrable", False)),
                    arbitre_appele=bool(getattr(a, "appele", False)),
                    diverge=bool(getattr(a, "diverge", False)))

    def ecart(self, tick: Optional[int], e) -> None:
        self.ecrire("ecart", tick=tick, action=getattr(e, "action", "?"),
                    attendu=getattr(e, "attendu", ""), observe=str(getattr(e, "observe", ""))[:200])

    def constat(self, tick: Optional[int], c) -> None:
        self.ecrire("constat", tick=tick, cause=getattr(c, "cause", "?"),
                    preuve=str(getattr(c, "preuve", ""))[:200],
                    mesures=getattr(c, "outils_appeles", 0))

    def mesure(self, tick: Optional[int], **grandeurs: Any) -> None:
        """Une photographie chiffrée de l'usine. C'est la série qui compte, pas le point."""
        self.ecrire("mesure", tick=tick, **grandeurs)

    def resume(self) -> str:
        parts = [f"{n} {g}" for g, n in sorted(self.compteurs.items())]
        duree = time.time() - self.debut_reel
        return (f"{self.lignes} ligne(s) en {duree / 60:.0f} min réelles"
                + (f" — {', '.join(parts)}" if parts else "")
                + (f" — {self.erreurs} écriture(s) perdue(s)" if self.erreurs else ""))


def relire(chemin: str, genre: Optional[str] = None) -> list[dict]:
    """Relit un journal. Les lignes illisibles sont IGNORÉES, pas fatales.

    Un journal tronqué (partie interrompue, disque plein) doit rester exploitable : la
    dernière ligne est souvent incomplète, et c'est justement celle qui précède l'incident
    qu'on cherche à comprendre.
    """
    sortie: list[dict] = []
    try:
        with open(chemin, "r", encoding="utf-8") as f:
            for ligne in f:
                ligne = ligne.strip()
                if not ligne:
                    continue
                try:
                    e = json.loads(ligne)
                except json.JSONDecodeError:
                    continue
                if genre is None or e.get("genre") == genre:
                    sortie.append(e)
    except OSError:
        return sortie
    return sortie