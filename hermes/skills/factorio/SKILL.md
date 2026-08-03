---
name: factorio
description: "Jouer à Factorio : bâtir une usine autonome depuis rien, via les outils du serveur MCP `factorio`. Les pièges du moteur, mesurés en jeu."
version: 1.0.0
author: factorio-llm
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [factorio, jeu, usine, automatisation, production]
---

# Jouer à Factorio

## Ce que tu vises

Une usine **autonome** : elle produit sans que tu la nourrisses à la main. Tant que tu
mines et fabriques toi-même, tu n'as pas d'usine — tu as une corvée.

L'ordre qui marche, mesuré sur des dizaines de parties :

1. **amorcer à la main** — miner de quoi fabriquer tes premières machines ;
2. **une chaîne qui tourne** — extraction → fonte, alimentée en combustible ;
3. **le combustible en chaîne** — c'est ce qui fait tenir tout le reste ;
4. **la première recherche** — elle ouvre les machines électriques ;
5. **le courant**, puis les chaînes électriques, plus rapides et sans combustible.

## La règle qui prime sur toutes

**Tu ne calcules jamais une position de pose.** `batir_une_chaine("iron-plate")` fait
l'extraction, la fonte, le transport et le raccordement. Si tu te surprends à réfléchir à
des coordonnées pour construire, tu fais le travail d'un code qui le fait mieux.

Les coordonnées servent à `regarder`, `diagnostiquer` et `se_deplacer`. Rien d'autre.

## Les pièges du moteur — mesurés, non déduits

Ces faits ont coûté des mois. Ils ne se devinent pas, et les ignorer produit des usines
qui ont l'air correctes et ne produisent rien.

**Le charbon ne se fond pas.** Aucune recette de fonte ne l'accepte. Une foreuse à charbon
suivie d'un four bouche toute la chaîne : le four se remplit, le bras se bloque, la
foreuse s'arrête. Vérifie avec `ce_qu_il_faut_pour` — il dit en quoi un minerai se fond,
ou qu'il ne se fond pas.

**Une machine à charbon ne se câble pas.** Foreuse burner, four en pierre, bras burner :
ils mangent du combustible, pas des volts. Leur poser des poteaux ne les alimente pas.

**Le combustible est une ressource comme les autres.** Chaque machine burner en brûle.
Tant que tu le mines à la main, ton usine s'éteint dès que ta poche se vide. Une foreuse
sur un gisement de charbon vaut mieux que dix ravitaillements.

**Vider ou remplir exige d'être là.** Le jeu refuse toute interaction au-delà d'une
dizaine de tuiles. Les outils s'approchent d'eux-mêmes, mais une machine hors de portée
reste hors de portée.

**Un générateur ne produit que ce qui est consommé.** Zéro kW sur une centrale neuve ne
veut pas dire qu'elle est en panne : branche la charge avant de juger.

**La première recherche ne peut pas s'automatiser.** L'assembleuse exige `automation`,
qui exige dix flacons rouges, qui exigeraient une assembleuse. On sort de la boucle en
fabriquant les flacons À LA MAIN — `chercher_une_technologie` le fait pour toi.

## Comment lire une panne

`diagnostiquer` nomme la CAUSE, pas le symptôme. Ce qui compte :

- `sans_combustible` — la machine est à sec ; `reparer("ravitailler", …)` ;
- `sortie_bloquee` — sa sortie est pleine et personne ne ramasse ;
  `reparer("evacuer", …)` dépanne, `reparer("batir_evacuation", …)` règle la cause ;
- `entree_vide` — rien n'arrive ; c'est presque toujours la CONSÉQUENCE d'une panne en
  amont. Remonte la ligne avec `suivre_une_ligne` plutôt que de traiter cette machine ;
- `sans_courant` / `debranchee` — deux choses différentes : pas de réseau, ou pas relié.

Un bras ou un convoyeur n'est jamais une cause racine : ce sont des organes de transit.

## `se_procurer` débloque, il ne produit pas

`se_procurer` mine, fond et fabrique **à ta place, à la main**. C'est ce qu'il faut pour
sortir du néant — obtenir le premier inserteur, les premières plaques, de quoi poser une
machine. Ce n'est **jamais** une façon de produire.

Mesuré à la première partie : 60 plaques de fer obtenues ainsi, et pas une seule machine
au sol. Soixante plaques faites à la main ne valent pas une foreuse qui tourne : la
première fois qu'un produit t'est demandé en quantité, la réponse est
`batir_une_chaine`, pas `se_procurer`.

La règle : `se_procurer` pour ce qui **manque une fois** ; `batir_une_chaine` pour ce qui
doit **continuer d'arriver**.

## Bâtir prend du temps — ce n'est pas une panne

`batir_une_chaine` marche jusqu'au gisement (parfois cent tuiles), pose, amorce, raccorde
et met en route. Plusieurs minutes de temps réel peuvent s'écouler sans réponse.

Mesuré : un appel coupé trop tôt s'est lu comme « le serveur est tombé » alors qu'il
construisait. Si un appel de construction est long, il travaille — attends-le. Ne conclus
à une panne que si un appel de LECTURE (`etat_du_jeu`, `regarder`) échoue lui aussi.

## Ce qui trompe

**Une action « réussie » n'a pas forcément servi.** Poser un coffre rend `OK` même si la
machine reste pleine. Relis l'état après — c'est le statut qui tranche.

**Répéter n'est pas insister utilement.** Trois échecs de la même action sur la même cible
signifient que la cause est ailleurs.

**La production brute ment.** Une partie longue produit plus qu'une partie courte sans
mieux jouer. Si tu compares, compare à durée égale.

## Outils, par usage

| Quand | Outil |
|---|---|
| avant tout, et après chaque action | `etat_du_jeu` |
| quelque chose ne tourne pas | `diagnostiquer` |
| trouver de quoi extraire | `ou_sont_les_ressources` |
| savoir ce qu'un produit exige | `ce_qu_il_faut_pour` |
| produire quelque chose en continu | `batir_une_chaine` |
| obtenir un objet ici et maintenant | `se_procurer` |
| du courant | `batir_une_centrale` |
| ouvrir de nouvelles recettes | `etat_de_la_recherche`, `chercher_une_technologie` |
| remettre une machine en marche | `reparer` |
| voir de près, aller quelque part | `regarder`, `se_deplacer` |

## Quand tu apprends quelque chose

Un fait durable sur ce jeu mérite d'être écrit dans une skill — un piège rencontré, un
enchaînement qui marche, une limite. Écris ce que tu as **mesuré**, pas ce que tu supposes,
et dis comment tu l'as constaté. La partie suivante commencera là où celle-ci s'arrête.
