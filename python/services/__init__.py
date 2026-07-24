"""Services partagés (déterministes) consommés par les agents.

Couche 0 de l'architecture multi-agent (cf. docs/agents-roadmap.md). Aucune
décision LLM ici : ce sont des wrappers typés et des planificateurs déterministes
réutilisés par tous les agents (DRY — un seul point d'accès à chaque capacité).
"""