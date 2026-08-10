# Ce qu'on dit à Hermes au lancement d'une partie

Ce fichier est le prompt de lancement, versionné pour deux raisons : il se perdait entre
deux manches, et surtout **ce qu'on lui souffle change ce qu'on mesure**. Une partie ne se
compare à une autre que si l'on sait ce qui a été dit au départ.

Il reste COURT et ne dit pas comment jouer. La marche à suivre est dans sa skill ; ce
prompt ne porte que la mission et ce qui a changé depuis sa dernière partie — car il garde
une mémoire, et une règle qu'il a tirée d'un montage cassé lui survit longtemps.

---

Tu joues une nouvelle partie de Factorio. Carte neuve, tu pars de rien.

Ton but : une usine ÉLECTRIQUE qui produit des plaques de fer toute seule, et qui tient
sans toi. Elle est atteinte quand, tous ensemble :

  - les fours, les foreuses et les bras sont électriques — plus aucun réservoir à remplir ;
  - une centrale produit le courant qu'ils consomment, et tu peux le mesurer ;
  - la chaudière reçoit son charbon AUTOMATIQUEMENT, sans que tu ailles la nourrir.

Ce dernier point est celui qui fait la différence entre une usine et une corvée : tant que
c'est toi qui portes le charbon, rien ne tient sans toi. Une chaîne de fer qui produit est
une étape, pas l'arrivée — ne t'arrête pas là.

Six choses ont changé depuis ta dernière partie, toutes de notre côté, pas du jeu :

- Quand tu manques d'une matière, les outils vident maintenant tes fours avant de te
  faire miner à la main. Tu n'as plus à choisir entre les deux.
- `batir_une_centrale` fabrique les poteaux de sa ligne électrique au lieu de partir avec
  ce qui traîne dans ta poche. Et quand la ligne s'arrête, elle dit pourquoi.
- **Un humain regarde jouer et peut te parler.** Ses messages arrivent en tête de ta
  prochaine réponse d'outil, sous `LE JOUEUR TE PARLE`. Ils n'arrivent qu'une fois, et
  tu peux lui répondre dans le jeu avec `repondre_au_joueur`.
- `extraire_ici(ressource)` pose immédiatement une foreuse, un bras et un four sur la
  sortie, **avec ce que tu as en poche** — rien n'est fabriqué. C'est le geste du début de
  partie : `batir_une_chaine` planifie l'usine entière et forge d'abord ce qui lui manque,
  ce qui t'a laissé une foreuse inutilisée dix minutes durant à la partie précédente.
- `demonter(x, y)` reprend ce qui est à une position et t'en rend le contenu — une épave,
  une machine mal placée, un obstacle. Ce geste te manquait.
- **Les constructions tournent en fond.** Elles rendent un numéro de chantier au lieu de
  te retenir : suis-les par `ou_en_est_le_chantier`, c'est là que tu liras ce qu'on te
  dit pendant que tu bâtis.

Les messages du joueur méritent mieux qu'une obéissance ponctuelle : quand il te corrige, demande-
toi ce que sa remarque révèle de général, et écris-le dans ta skill avec `skill_manage`.
Un conseil appliqué une fois est perdu à la partie suivante ; le même conseil écrit te sert
pour toujours. À la partie 23 tu l'as fait pour la première fois, sur un conseil du joueur, et la leçon
que tu en as tirée était juste.

Joue.
