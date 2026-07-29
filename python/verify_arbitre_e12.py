"""Test LIVE E12 (étape 5) : que vaut l'arbitre LLM sur les vrais carrefours ?

Les étapes 1 à 4 ont posé le contrat (le modèle choisit, il ne génère pas), les
garde-fous et le mode ombre. Restait la question qu'aucun test unitaire ne peut
trancher : **le modèle décide-t-il mieux, ou seulement autrement ?**

On lui soumet quatre situations construites à la main, dont on connaît la réponse
défendable, et on regarde son choix ET sa justification :

  1. le carrefour du Defender — menace imminente contre première machine à bâtir. Les
     deux options sont légitimes ; c'est le cas qui a motivé tout ce travail ;
  2. deux pannes simultanées — laquelle réparer d'abord ;
  3. une seule option — il ne doit PAS être consulté (économie) ;
  4. usine saine — ne rien faire est la bonne réponse, et un modèle bavard aura
     tendance à vouloir agir.

Aucun serveur Factorio requis : les états sont construits directement. Ce qui est
mesuré ici est le jugement, pas l'exécution.

Pré-requis : Ollama joignable avec le modèle configuré. SKIP si absent.
"""

from __future__ import annotations

import sys
import time

from agents.coordinator import Decision, EtatUsine, decide, enumerer_options
from services.arbitre import ArbitreOmbre, LLMArbitre
from services.factory_doctor import diagnose
from services.threat_model import IMMINENTE, Menace

RESULTS: list[tuple[str, bool, str]] = []


def rec(name: str, ok: bool, detail: str) -> None:
    RESULTS.append((name, ok, detail))
    print(f"[{'OK  ' if ok else 'FAIL'}] {name:50s} {detail[:110]}")


def _m(name, x, y, status, type_="machine"):
    return {"name": name, "x": x, "y": y, "status": status, "type": type_}


def _etat(rows=None, reseau=7, machines=None, menace=None) -> EtatUsine:
    diag = diagnose(rows or [])
    e = EtatUsine(machines=machines if machines is not None else diag.machines,
                  diagnostic=diag, reseau=reseau, production_kw=900.0,
                  inventaire={"coal": 120, "gun-turret": 8, "firearm-magazine": 200,
                              "electric-mining-drill": 4})
    e.menace = menace
    return e


def main() -> int:
    from config import load_config
    cfg = load_config()
    arbitre = LLMArbitre(cfg)
    if arbitre._client is None:
        print(f"[SKIP] aucun client LLM ({arbitre.journal}).")
        return 0
    print(f"       modèle : {cfg.openai_model} via {cfg.openai_base_url}\n")

    # --- 1 : LE carrefour — se défendre ou produire ---
    menace = Menace(niveau=IMMINENTE, raison="pollution 3689 et 14 nids, le plus proche "
                                             "à 241 : les vagues vont partir",
                    nids=14, pollution=3689.0, distance_nid=241.0,
                    front=(0.0, -1.0), front_nom="nord")
    etat = _etat(machines=0, menace=menace)
    options = enumerer_options(etat)
    t0 = time.time()
    choix = arbitre(etat, options)
    dt = time.time() - t0
    motif = arbitre.journal[-1] if arbitre.journal else ""
    rec("e12-1 : le carrefour défendre/produire est arbitré",
        0 <= choix < len(options) and "repli" not in motif,
        f"{[o.action for o in options]} -> [{choix}] en {dt:.1f}s | {motif[:70]}")

    # --- 2 : deux pannes, laquelle d'abord ---
    etat2 = _etat([_m("electric-furnace", 0, 0, "no_fuel"),
                   _m("assembling-machine-1", 0, 6, "no_recipe")])
    options2 = enumerer_options(etat2)
    choix2 = arbitre(etat2, options2)
    motif2 = arbitre.journal[-1] if arbitre.journal else ""
    rec("e12-2 : deux pannes -> il en désigne une et l'explique",
        0 <= choix2 < len(options2) and "repli" not in motif2,
        f"{[o.action for o in options2]} -> [{choix2}] | {motif2[:70]}")

    # --- 3 : une seule option -> pas d'appel (le coût compte) ---
    avant = len(arbitre.journal)
    etat3 = _etat([_m("electric-furnace", 0, 0, "no_fuel")])
    d3 = decide(etat3, arbitre)
    rec("e12-3 : une seule option -> le modèle n'est pas consulté",
        len(arbitre.journal) == avant and d3.action == "ravitailler",
        f"décision={d3.action}, {len(arbitre.journal) - avant} appel(s) supplémentaire(s)")

    # --- 4 : usine saine -> ne rien faire, malgré la tentation d'agir ---
    etat4 = _etat([_m("electric-furnace", 0, 0, "working"),
                   _m("electric-mining-drill", 0, 6, "working")])
    d4 = decide(etat4, arbitre)
    rec("e12-4 : usine saine -> il ne s'agite pas", d4.action == "rien", f"{d4}")

    # --- 5 : mode ombre — le déterministe garde la main, on mesure l'écart ---
    ombre = ArbitreOmbre(LLMArbitre(cfg))
    situations = [
        _etat(machines=0, menace=menace),
        _etat([_m("electric-furnace", 0, 0, "no_fuel"),
               _m("assembling-machine-1", 0, 6, "no_recipe")]),
        _etat([_m("electric-mining-drill", 0, 0, "no_fuel"),
               _m("electric-furnace", 0, 6, "full_output")]),
    ]
    pris = []
    for e in situations:
        d = decide(e, ombre)
        pris.append(d.action)
    rec("e12-5 : mode ombre — le déterministe décide, l'écart est mesuré",
        all(a == enumerer_options(e)[0].action for a, e in zip(pris, situations)),
        f"décisions={pris} | {len(ombre.divergences)} divergence(s), "
        f"{ombre.accords} accord(s), taux={ombre.taux_divergence:.0%}")
    for d in ombre.divergences:
        print(f"       . {d[:110]}")

    return _verdict()


def _verdict() -> int:
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