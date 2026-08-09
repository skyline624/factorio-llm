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

## La marche à suivre, dans cet ordre

Ne t'en écarte que si une observation te le commande. Mesuré sur trois parties : sans cet
ordre, on mine à la main pendant vingt minutes et on ne pose jamais rien.

1. `etat_du_jeu` — voir où l'on est.
2. `ou_sont_les_ressources("iron-ore")` — savoir où est le fer.
3. **`batir_une_chaine("iron-plate")`** — la poser. C'est l'étape qui compte, et elle
   vient TÔT. Elle fabrique elle-même ce qui lui manque : n'attends pas d'avoir « assez
   de matériel » pour l'appeler, c'est son travail.
4. `etat_du_jeu` — vérifier ce qui est en terre.
5. `diagnostiquer` puis `reparer` — mettre en marche ce qui ne tourne pas.

Puis — et c'est là que la partie se gagne ou stagne :

6. **`chercher_une_technologie("electronics")`** — dix plaques de cuivre, rien de plus.
   Elle ouvre `small-electric-pole`, `inserter`, `electronic-circuit` et `lab`.
7. **`batir_une_centrale`** — `steam-power` s'acquiert toute seule dès que tu as fabriqué
   cinquante plaques de fer, donc tu l'as déjà quand ta chaîne tourne. Sans les poteaux
   de l'étape 6, en revanche, le courant ne va nulle part : fais les deux dans cet ordre.
8. **Passe tes machines à l'électrique.** C'est l'étape qui change tout, et la raison
   d'être des deux précédentes.

**POURQUOI L'ÉLECTRIQUE CHANGE TOUT.** Une machine burner a un réservoir : il faut le
remplir, il se vide, et tu recommences. Mesuré sur six parties : c'est ce qui consomme
l'essentiel de ton temps — la même foreuse rechargée quatre fois en trois minutes, des
ravitaillements en boucle, une ligne de charbon à tirer jusqu'à chaque brûleur. Une
machine électrique n'a pas de réservoir. Une centrale alimente tout le réseau d'un coup,
et le problème du combustible disparaît au lieu d'être géré.

Tant que tu restes en burner, tu es le convoyeur de ton usine. L'électricité est ce qui
te rend inutile — c'est le but.

**Tu n'appelles `se_procurer` que si `batir_une_chaine` te dit explicitement ce qui lui
manque.** Jamais « au cas où », jamais pour accumuler.

Le signe que tu t'égares : ton inventaire grossit et le sol reste vide. Quarante-cinq
minerais en poche et zéro machine posée, c'est une partie perdue — pas un début prometteur.

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

## Joue par petits pas, pas en une seule fois

Un appel qui dure vingt minutes te prive de tout : tu ne peux ni observer, ni corriger, ni
changer d'avis. Mesuré à la deuxième partie : une seule construction lancée d'un bloc a
occupé la session entière sans rien poser.

Alterne : **une action, puis un regard**. `batir_une_chaine`, puis `etat_du_jeu` pour voir
ce qui en est sorti. Si une construction échoue, tu l'apprends tout de suite et tu peux
faire autrement — plutôt que de découvrir à la fin que rien n'a abouti.

## Bâtir prend du temps — ce n'est pas une panne

`batir_une_chaine` marche jusqu'au gisement (parfois cent tuiles), pose, amorce, raccorde
et met en route. Plusieurs minutes de temps réel peuvent s'écouler sans réponse.

Mesuré : un appel coupé trop tôt s'est lu comme « le serveur est tombé » alors qu'il
construisait. Si un appel de construction est long, il travaille — attends-le. Ne conclus
à une panne que si un appel de LECTURE (`etat_du_jeu`, `regarder`) échoue lui aussi.

## Les timeouts à 900 s venaient du serveur, pas du jeu — CORRIGÉ le 06/08

Tu as observé, parties 4 à 9, une famille de symptômes que tu as attribués à un
« état dégradé » du jeu : des appels qui expirent à 900 s, `find_nearest` qui rend
None pour toutes les ressources, des recherches qui échouent sans rien fabriquer.
Tes observations étaient exactes. **Ta conclusion sur leur cause était fausse**, et
elle t'a fait éviter les outils dont tu avais besoin.

La cause réelle, mesurée des deux côtés : les outils du serveur MCP étaient
synchrones et s'exécutaient sur sa boucle d'événements. Un appel de 72 s gelait donc
le transport entier — plus de ping, plus de session gérée. Ton client concluait que
le lien était mort, ouvrait une session neuve, et la réponse partait sans
destinataire. Tu attendais alors ton timeout complet de 900 s **pour un appel qui
avait réussi**.

Preuve, 9e partie : `chercher_une_technologie("electronics")` a rendu OK côté serveur
en 71,9 s ; tu as reçu « TimeoutError after 900.0s » exactement 900 s après l'appel.
La technologie était bien débloquée — tu l'as constaté toi-même en vérifiant l'état
juste après.

Le travail part maintenant sur un thread et la boucle reste libre. Un appel long
**rend sa réponse**, quelle que soit sa durée.

**Ce qui change pour toi :**

- **Appelle `batir_une_chaine` quand tu en as besoin.** La consigne « ne jamais
  l'appeler avant d'avoir steel-processing » était une déduction de ces faux
  timeouts. Elle est annulée.
- **Un appel long travaille.** Une chaîne, c'est marcher jusqu'au gisement, miner,
  fondre, fabriquer, poser, amorcer. Plusieurs minutes sont normales. Attends-le.
- **N'interprète plus un timeout comme une corruption du jeu.** Si l'un survient
  encore, dis-le mais continue : vérifie l'état, et si l'action a abouti, poursuis.
- **Ne relance pas le serveur Factorio de ta propre initiative.** Aucune des
  situations décrites plus haut ne l'exigeait.

Le défaut voisin `'list' object has no attribute 'get'` (partie 5) est lui aussi
corrigé : `batir_une_chaine` échouait en une seconde sur une lecture de recette mal
formée. Il ne se reproduira pas.

## L'arbre de recherche a changé — automation exige un lab alimenté — mesuré, partie 5

L'ordre documenté (electronics → automation-science-pack → automation →
steel-processing) est **obsolète**. L'arbre réel mesuré le 06/08 :

1. `electronics` (fabriquer 10 copper-plate) — OK à la main, pas de lab.
2. `steam-power` (fabriquer 50 iron-plate) — OK à la main, pas de lab.
3. `automation-science-pack` (fabriquer 1 lab) — OK à la main.
4. `automation` (10 automation-science-pack) — **ÉCHEC** : « aucune place
   libre pour un laboratoire près du réseau ». Exige un lab POSÉ sur un
   RÉSEAU ÉLECTRIQUE. Contrairement à electronics/steam-power (coût = objets
   fabriqués à la main), automation coûte des flacons de science, qui
   exigent un lab alimenté.

**Conséquence** : la centrale est nécessaire avant `automation`, qui coûte des flacons
— donc un lab POSÉ et ALIMENTÉ. Mais cela ne change RIEN à l'ordre de la marche à
suivre : `batir_une_chaine` reste l'étape 3, et elle vient tôt. C'est elle qui produit
les plaques dont tout le reste se paie ; attendre d'avoir fini l'arbre de recherche pour
bâtir, c'est attendre sans rien produire.

L'enchaînement complet, en une ligne :

  chaîne de fer (tôt) → electronics → centrale → machines électriques → automation

**LE BOIS N'EST PLUS UN VERROU — corrigé le 06/08.** Tu avais écrit que
`se_procurer("wood")` échouait et que rien n'en trouvait, donc pas de poteau, pas de
réseau, pas de lab, pas de recherche. C'était exact, et c'était un défaut de nos outils,
pas du jeu : le mode « miner » cherchait en jeu une entité nommée « wood », qui n'existe
pas — les arbres portent `tree-01`, `dry-hairy-tree` et vingt-six autres noms.

Vérifié en jeu depuis : `se_procurer("wood")` rend quatre bûches par arbre, et
`wooden-chest` (2 bois) redevient fabricable. Le chemin vers l'électricité est ouvert.

## `se_deplacer` marche — ce que tu avais mesuré venait d'une partie sans avatar

Tu avais noté « `se_deplacer` retourne toujours arrivé en (0,0) quelle que soit la
cible ». C'était exact ce jour-là, et trompeur : sans joueur connecté au serveur, le mod
n'a personne à déplacer et rend la position par défaut. Rien dans la réponse ne le dit.

Mesuré depuis, avec un avatar : « arrivé en (10,0) — visé (12,0) » en partant de (0,0).
L'outil marche, il contourne les obstacles et génère le terrain devant lui.

**Ce qu'il faut en retenir, et qui vaut au-delà de cet outil** : un résultat obtenu dans
un environnement incomplet ne dit rien de l'outil, seulement de l'environnement. Quand
une lecture te paraît absurde — une position qui ne bouge pas, un inventaire vide, une
ressource introuvable — vérifie d'abord que la partie est en état, par `etat_du_jeu`.

## `chercher_une_technologie` n'a pas besoin de courant ni de lab posé — mesuré

`chercher_une_technologie("electronics")` a réussi à fabriquer 10 copper-plate à la
main, sans lab posé ni réseau électrique. `chercher_une_technologie("automation-science-pack")`
a réussi à fabriquer un lab à la main. L'outil simule la recherche en fabriquant les
prérequis directement — il ne pose pas de lab ni ne l'alimente.

**Conséquence** : la séquence documentée (étape 4 : batir_une_centrale avant
automation) est **fausse** pour ce serveur. La centrale n'est pas nécessaire pour
les recherches manuelles. L'ordre correct est :

1. `chercher_une_technologie("electronics")` — 10 copper-plate à la main ;
2. `chercher_une_technologie("automation-science-pack")` — lab à la main ;
3. `chercher_une_technologie("automation")` — 10 flacons à la main ;
4. `chercher_une_technologie("steel-processing")` — 50 flacons à la main ;
5. `batir_une_chaine("iron-plate")` — coffre débloqué, chaîne possible.

La centrale vient **après** steel-processing, pas avant. Le cercle vicieux
small-electric-pole n'existe plus car on n'a pas besoin de la centrale pour
rechercher.

ATTENTION : les étapes 3-4 exigent `find_nearest(iron-ore)` fonctionnel. Si le
serveur est dans l'état dégradé (find_nearest = None), ces étapes échouent.

## `se_procurer` : le paramètre est `combien`, pas `quantite`

Le schema de `se_procurer` utilise `combien` (int, défaut 1). Passer `quantite` est
ignoré silencieusement — l'outil prend la valeur par défaut 1. Mesuré : 17 charbons
fabriqués un par un au lieu de 17 d'un coup.

## `batir_une_centrale` consomme plus de prérequis que annoncé

La centrale affiche `missing={pipe: 4, coal: 16}` même quand l'inventaire contient
4 pipes et 50 charbon. Il faut fabriquer **plus** que le compte affiché : 8 pipes et
51 charbon dans la pratique. La centrale consomme des pipes pendant la pose et en
demande encore. Méthode : fabriquer le double des prérequis affichés avant de
lancer `batir_une_centrale`.

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

## `batir_une_chaine` peut poser sur un gisement épuisé — mesuré, partie 10

`batir_une_chaine("iron-plate")` a posé une foreuse sur un gisement de fer qui
n'avait plus de minerai sous l'emprise (`gisement_epuise` au diagnostic suivant).
L'outil trouve le gisement le plus proche par `find_nearest` mais ne vérifie pas
la quantité restante avant de poser. Conséquence : la foreuse tourne à vide.

**Signe révélateur** : `diagnostiquer` montre `gisement_epuise` sur une foreuse
qui vient d'être posée. Remède : relancer `batir_une_chaine` — l'outil va
relocaliser sur un autre gisement. Mais si l'inventaire ne permet pas de
refabriquer les machines, il faut `se_procurer` les prérequis manquants
(burner-mining-drill exige iron-gear-wheel + stone-furnace ; stone-furnace
exige stone). `se_procurer("stone", combien=20)` fonctionne par minage direct.

## Les foreuses de charbon ont besoin d'un bras de retour — TU L'AS TROUVÉ, C'EST CORRIGÉ

Tu avais raison, et personne d'autre ne l'avait vu : une foreuse burner sur un gisement
de charbon devrait s'auto-alimenter, mais la chaîne faisait tomber le charbon sur un
convoyeur qui s'en allait, sans rien qui revienne vers son réservoir. Elle brûlait son
amorce et s'arrêtait sur son propre gisement, une belt pleine à côté.

**`approvisionner` pose maintenant ce bras de retour** (correctif du 06/08). Tu n'as
donc plus à recharger ces foreuses à la main — c'est ce qui te faisait perdre le plus de
temps : quatre rechargements de la même foreuse en trois minutes, pendant lesquelles tu
ne construisais pas.

**Signe qui reste utile** : `regarder` montre une foreuse `no_fuel` avec `oreUnder > 0`
— du minerai sous elle, pas de combustible. Si cela persiste APRÈS une chaîne bâtie,
c'est que le bras de retour n'a pas pu se poser : dis-le dans ton compte rendu plutôt
que de colmater indéfiniment.

## Le serveur Factorio peut tomber en cours de partie — mesuré, partie 10

Après ~30 actions (bâtir, ravitailler, évacuer, diagnostiquer), la connexion
RCON a été refusée (`[WinError 10061]`). Le serveur MCP factorio lui-même
restait debout — c'est le processus de jeu Factorio sur Windows qui est tombé.
`etat_du_jeu` et `regarder` échouent tous les deux avec le même WinError 10061.

La skill dit « ne relance pas le serveur de ta propre initiative » — mais
ici le serveur de JEU (pas le MCP) est tombé, pas seulement un appel long.
Il faut le redémarrer côté Windows pour reprendre. Depuis WSL, on ne peut ni
voir ni relancer le processus Windows.

## Ce que le bois verrouille — vérifié contre le jeu le 06/08

`wooden-chest` coûte **2 bois**, `small-electric-pole` **1 bois + 2 câbles**. Les deux
recettes sont OUVERTES dès le départ : rien n'est verrouillé par une technologie.

Mais le bois n'a **aucune recette** — il se récolte en abattant un arbre, et `se_procurer`
ne sait pas encore le faire. C'est donc le bois, et lui seul, qui limite le coffre et le
poteau. Une chaîne se bâtit sans coffre (`batir_une_chaine` choisit le réceptacle qu'elle
peut faire, ou s'en passe).

CORRECTION D'UNE LEÇON PRÉCÉDENTE : il avait été écrit ici que `wooden-chest` exigeait
`steel-plate` et que `steel-processing` était le seul chemin vers toute production. C'était
faux, et la faute venait de l'outil : `ce_qu_il_faut_pour` remontait des recettes de
RECYCLAGE — recycler un fusil à pompe rend du bois — et inventait neuf étages pour un objet
qui en compte deux. L'outil est réparé. Se méfier d'une chaîne anormalement longue pour un
objet simple : c'est le signe d'un outil qui ment, pas d'un jeu compliqué.
