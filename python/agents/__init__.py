"""Agents métier du pilote factorio-llm.

Couche 2 de l'architecture (cf. docs/agents-roadmap.md). Chaque agent hérite de
BaseAgent (boucle perceive/decide/act) et ne fait que des DÉCISIONS — l'exécution
mécanique est déléguée au mod via ModApi (run_action, race-free) et aux services
déterministes (services/perception, services/knowledge).
"""