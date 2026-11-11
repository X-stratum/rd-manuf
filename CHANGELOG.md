# Journal des changements

Ce fichier raconte le projet **PR par PR, du plus récent au plus ancien**, en langage clair : ce qui a changé, pourquoi, et les choix à ne pas perdre.
Pour le détail technique (fichiers, tests, preuves), ouvrir la PR indiquée sous chaque entrée.

**Le tenir à jour.** Chaque PR ajoute sa propre entrée en tête de liste, dans la même PR. L'entrée reprend le « En clair » et les « Décisions à retenir » de la description de la PR.
Les messages de commit déjà présents dans `main` ne sont pas réécrits (l'historique d'un dépôt public ne se réécrit pas) : ce fichier en est la version lisible.

## Petit lexique

- **CSV** : un fichier texte où chaque ligne est une ligne de données, les valeurs étant séparées par un symbole (virgule ou point-virgule). C'est ce qu'exporte Excel.
- **Livraison** : un fichier de données envoyé par l'usine. Celui qui l'envoie est l'**émetteur** ; ceux qui utilisent les données sont le **métier** ; ceux qui construisent le pipeline sont l'**équipe data**.
- **Contrat de données** : la fiche qui décrit ce qu'une livraison doit contenir (colonnes, types de valeurs, limites, règles).
- **Rejet** : une ligne qui ne respecte pas le contrat est mise de côté pour être corrigée, au lieu d'être utilisée.
- **Seuil de rejet** : la part maximale de lignes rejetées (2 %) au-delà de laquelle toute la livraison est bloquée.
- **Bronze / Silver** : deux étapes de traitement. Le bronze garde les données telles que reçues ; le silver contient les données contrôlées, prêtes à servir.
- **PR** (*pull request*) : une demande de modification, relue avant d'être intégrée au projet.
- **CI** : la vérification automatique lancée à chaque PR (style du code, puis tests).
- **Sabotage** : une modification volontaire du code pour vérifier que les tests détectent bien l'erreur.

---

## #16 — Lire un fichier CSV et signaler les lignes coupées
*2026-11-11 · [PR #16](https://github.com/X-stratum/rd-manuf/pull/16)*

**En clair.** Le projet sait maintenant ouvrir un fichier CSV envoyé par l'usine et le transmettre à la réception. Le caractère invisible qu'Excel ajoute en tête de fichier ne fait plus refuser une livraison à tort, le séparateur (virgule ou point-virgule) est réglable, et une ligne coupée en route ou trop longue est signalée au lieu de passer inaperçue.
**Décisions.** Le lecteur lit et signale, il ne juge pas : les lignes mal formées sont signalées par leur numéro mais pas encore traitées par la réception (ticket suivant). Le format Parquet aura son propre lecteur, indépendant de celui-ci.

## #15 — Journal des changements
*2026-11-11 · [PR #15](https://github.com/X-stratum/rd-manuf/pull/15)*

**En clair.** Un fichier unique raconte le projet PR par PR, pour qu'un lecteur qui découvre le dépôt comprenne ce qui a été fait et pourquoi sans ouvrir chaque PR.
**Décisions.** Tenu à la main, une entrée par PR, dans la même PR. Les messages de commit de `main` ne sont pas réécrits.

## #14 — Modèle de PR : « En clair » et message de squash
*2026-11-11 · [PR #14](https://github.com/X-stratum/rd-manuf/pull/14)*

**En clair.** Les descriptions de PR servent désormais à la fois aux relecteurs techniques et aux lecteurs du métier : un court paragraphe sans jargon ouvre chaque PR, et un texte prêt à copier est prévu pour le message qui reste dans l'historique.
**Décisions.** Les preuves et les cases à cocher restent dans la PR ; seul le message de squash entre dans l'historique. Un titre ne dépasse pas 72 caractères, GitHub tronquant au-delà.

## #13 — Alerter quand une livraison est refusée pour sa structure
*2026-11-11 · [PR #13](https://github.com/X-stratum/rd-manuf/pull/13)*

**En clair.** Quand un fournisseur envoie un fichier dont les colonnes ne correspondent pas au contrat, l'émetteur est maintenant prévenu qu'il doit le renvoyer, avec l'équipe data et le métier en copie. Avant, le fichier était refusé sans que personne ne le sache.
**Décisions.** L'émetteur agit (lui seul peut renvoyer un bon fichier), l'équipe data est en copie (un refus peut venir d'une nouvelle version du fichier). Le nombre de lignes d'une alerte de structure est celui des lignes reçues et non intégrées (0 pour un fichier vide).

## #12 — Décider du statut d'une livraison, structure d'abord
*2026-11-10 · [PR #12](https://github.com/X-stratum/rd-manuf/pull/12)*

**En clair.** Chaque livraison reçue reçoit une décision unique, la structure étant vérifiée en premier : si les colonnes sont mauvaises, la livraison est refusée sans lire ses lignes ; sinon elle est acceptée, acceptée avec rejets ou bloquée, comme avant.
**Décisions.** Le validateur de lignes n'est même pas appelé pour une livraison refusée. Pour une livraison refusée, aucune ligne n'est comptée comme valide ou rejetée : aucune n'a été jugée.

## #11 — Refuser une livraison dont l'en-tête ne respecte pas le contrat
*2026-11-10 · [PR #11](https://github.com/X-stratum/rd-manuf/pull/11)*

**En clair.** Un fichier dont les colonnes ne correspondent pas au contrat (colonne manquante, en trop, en double, ou fichier vide) est détecté avant de lire ses lignes, avec la liste complète de ce qui ne va pas.
**Décisions.** Une structure invalide refuse la livraison entière. Un problème par code, avec toutes les colonnes concernées. Les noms de colonnes sont comparés à l'identique (la casse et les espaces comptent).

## #10 — Documenter la règle « une ligne valide » des tests
*2026-11-10 · [PR #10](https://github.com/X-stratum/rd-manuf/pull/10)*

**En clair.** Une consigne d'écriture de tests est écrite à l'endroit où elle sera lue : une livraison de test construite avec le seuil neutralisé doit garder au moins une ligne valide.
**Décisions.** Une règle d'écriture de tests se documente à la source (la fixture), pas dans un test particulier.

## #9 — Tests d'alertes indépendants du seuil de rejet
*2026-11-10 · [PR #9](https://github.com/X-stratum/rd-manuf/pull/9)*

**En clair.** Les tests des alertes ne dépendent plus d'un détail du seuil de rejet : ils vérifient uniquement qui est prévenu.
**Décisions.** Un test dépend du sujet qu'il vérifie, et d'aucun autre détail.

## #8 — Simulateur de livraisons avec anomalies par code
*2026-11-10 · [PR #8](https://github.com/X-stratum/rd-manuf/pull/8)*

**En clair.** Un simulateur fabrique de fausses livraisons conformes au contrat, avec des anomalies demandées par type d'erreur. Un même nombre de départ (la « graine ») donne toujours la même livraison : on peut tester et rejouer un incident sans attendre l'usine.
**Décisions.** Les tests utilisent le validateur existant comme juge : le simulateur ne se juge pas lui-même. Les anomalies occupent les dernières lignes.

## #7 — Tests : centraliser les valeurs de fabrication et figer le contrat réel
*2026-11-10 · [PR #7](https://github.com/X-stratum/rd-manuf/pull/7)*

**En clair.** Les tests sont réorganisés pour qu'un changement du contrat n'oblige plus à les réécrire un par un, sans changer ce qu'ils vérifient. Un test « alarme » signale tout changement du contrat réel.
**Décisions.** Les tables qui vérifient le contrat gardent leurs valeurs en clair ; toutes les autres valeurs sont dérivées du contrat.

## #6 — Codes d'erreur stables et routage par rôles
*2026-11-09 · [PR #6](https://github.com/X-stratum/rd-manuf/pull/6)*

**En clair.** Quand une ligne est rejetée, le système sait dire précisément pourquoi, avec un code précis et durable (7 codes possibles), et décider qui doit agir (l'émetteur du fichier ou le métier) et qui doit seulement être informé. Rien n'est encore envoyé. Les règles « qui prévient qui » sont dans un fichier lisible par le métier, avec des rôles et jamais des noms de personnes.
**Décisions.** Un rejeu technique ne produit aucune alerte. Celui qui agit n'est pas aussi listé parmi les informés. Une livraison bloquée produit d'abord une alerte `LIVRAISON_BLOQUEE`, puis le détail par code. Les codes sont un contrat avec les futurs consommateurs : les renommer casse les alertes.

## #5 — Valider une livraison : doublons, seuil de rejet, statut
*2026-11-08 · [PR #5](https://github.com/X-stratum/rd-manuf/pull/5)*

**En clair.** Une livraison est jugée dans son ensemble avant d'entrer dans les données exploitables (Silver) : acceptée, acceptée avec quelques lignes mises en quarantaine, ou bloquée si trop de lignes sont rejetées (seuil de 2 % fixé par le contrat). Une ligne reçue deux fois à l'identique est ignorée, et deux lignes qui se contredisent ne sont pas toutes deux gardées.
**Décisions.** Bloquée = rien n'est intégré, même les lignes saines (elles restent rejouables). Un doublon exact est ignoré sans alerte. Le seuil exact (2 %) est toléré. Le taux est calculé sur les lignes reçues.

## #4 — Contrat de la livraison de production et contrôle d'une ligne
*2026-11-08 · [PR #4](https://github.com/X-stratum/rd-manuf/pull/4)*

**En clair.** Le projet sait ce qu'est une livraison de production conforme. Un « contrat de données » (un fichier YAML) décrit chaque colonne attendue, ses types, ses bornes et ses règles, et le programme peut vérifier une ligne contre ce contrat et dire ce qui ne va pas.
**Décisions.** Aucune consignée à l'époque ; les règles sont décrites dans le contrat lui-même.

## #3 — Nettoyage après revue
*2026-11-08 · [PR #3](https://github.com/X-stratum/rd-manuf/pull/3)*

**En clair.** Petit nettoyage demandé par la revue : une ligne de code commenté est retirée et la version de Python requise est alignée sur celle de la CI.

## #2 — Garde-fou d'environnement et première CI
*2026-11-08 · [PR #2](https://github.com/X-stratum/rd-manuf/pull/2)*

**En clair.** Le projet ne peut plus travailler par erreur sur le mauvais environnement : un nom de catalogue (la « base » d'un environnement) inconnu ou mal orthographié est refusé, et l'environnement par défaut est le bac à sable, jamais la production. Cette PR met aussi en place la première vérification automatique (CI) à chaque demande de modification.
**Décisions.** Valeur par défaut volontairement sûre. Un nom inconnu est refusé, jamais traité « par défaut » comme un environnement sûr.

## #1 — Modèle de description de PR
*2026-11-07 · [PR #1](https://github.com/X-stratum/rd-manuf/pull/1)*

**En clair.** Toutes les demandes de modification du projet suivent le même plan de description : un relecteur sait toujours où trouver le pourquoi, ce qui change, l'impact sur les données et les risques.
