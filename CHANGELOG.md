# Journal des changements

Ce fichier raconte le projet **PR par PR, du plus récent au plus ancien**, en langage clair : ce qui a changé, pourquoi, et les choix à ne pas perdre.
Pour le détail technique (fichiers, tests, preuves), ouvrir la PR indiquée sous chaque entrée.

**Le tenir à jour.** Chaque PR ajoute sa propre entrée en tête de liste, dans la même PR. L'entrée reprend le « En clair » et les « Décisions à retenir » de la description de la PR.
Les messages de commit déjà présents dans `main` ne sont pas réécrits (l'historique d'un dépôt public ne se réécrit pas) : ce fichier en est la version lisible.

## Petit lexique

- **CSV** : un fichier texte où chaque ligne est une ligne de données, les valeurs étant séparées par un symbole (virgule ou point-virgule). C'est ce qu'exporte Excel.
- **Ligne malformée** : une ligne dont le nombre de valeurs ne correspond pas à l'en-tête, le plus souvent une ligne coupée en route. On ne peut pas savoir à quelle colonne appartient chaque valeur.
- **Parquet** : un format de fichier de données qui contient lui-même ses colonnes et leurs types (nombres, dates), donc plus fiable qu'un CSV, qui n'est que du texte.
- **Environnement conda** : un espace de travail isolé (Python et bibliothèques) que l'on crée en une commande, pour que chacun travaille avec les mêmes versions.
- **Bundle Databricks** : un fichier qui décrit où le projet se déploie sur Databricks, et quel catalogue de données chaque cible utilise.
- **Livraison** : un fichier de données envoyé par l'usine. Celui qui l'envoie est l'**émetteur** ; ceux qui utilisent les données sont le **métier** ; ceux qui construisent le pipeline sont l'**équipe data**.
- **Contrat de données** : la fiche qui décrit ce qu'une livraison doit contenir (colonnes, types de valeurs, limites, règles).
- **Rejet** : une ligne qui ne respecte pas le contrat est mise de côté pour être corrigée, au lieu d'être utilisée.
- **Seuil de rejet** : la part maximale de lignes rejetées (2 %) au-delà de laquelle toute la livraison est bloquée.
- **Bronze / Silver** : deux étapes de traitement. Le bronze garde les données telles que reçues ; le silver contient les données contrôlées, prêtes à servir.
- **PR** (*pull request*) : une demande de modification, relue avant d'être intégrée au projet.
- **CI** : la vérification automatique lancée à chaque PR (style du code, puis tests).
- **Sabotage** : une modification volontaire du code pour vérifier que les tests détectent bien l'erreur.

---

## #27 — Code usine : le format AA_001
*2026-11-12 · [PR #27](https://github.com/X-stratum/rd-manuf/pull/27)*

**En clair.** Le code de l'usine, dans le nom d'un fichier, s'écrit maintenant sur le modèle `AA_001` : deux lettres, un tiret bas (_), trois chiffres. Un fichier dont le nom utilise l'ancienne forme, comme `AB123`, est refusé, et le message de refus donne un exemple de nom conforme. Cela remplace la forme `AB123` décidée à la #22 (2 lettres et 3 chiffres).
**Décisions.** Chaque partie du code a une largeur fixe : le tiret bas qu'il contient ne gêne donc pas la lecture du nom, le pays, le code usine et la date retombent chacun à leur place. L'ancienne forme est refusée plutôt qu'acceptée en parallèle : un seul format, simple, et rien n'est encore en production, donc aucune livraison existante n'est touchée. Le format est écrit dans une seule constante du code ; l'exemple du message de refus la répète, et un test échoue si les deux divergent. Le message de refus donne un exemple de nom, parce que la forme abstraite se lit moins bien avec un tiret bas dans le code.

## #26 — Bronze : copier fidèlement les lignes reçues
*2026-11-12 · [PR #26](https://github.com/X-stratum/rd-manuf/pull/26)*

**En clair.** Le projet sait maintenant fabriquer les lignes du bronze, la copie fidèle de ce que l'usine a envoyé : toutes les valeurs en texte, sans rien corriger ni juger, avec le nom de la livraison, son heure d'arrivée et le rang de chaque ligne parmi les lignes bien formées du fichier. Ces lignes ne sont pas encore écrites dans une table : cela viendra avec le déploiement.
**Décisions.** Le bronze garde les lignes des livraisons acceptées, acceptées avec rejets et bloquées : une livraison bloquée pourra être retraitée après correction, il faut donc la conserver. Une livraison refusée n'y entre pas (le registre en garde la trace, le fichier reste à la source), ni un rejeu (l'ingérer deux fois doublerait les données). Une ligne coupée d'un CSV n'y entre pas non plus : on ne sait pas à quelle colonne appartient chaque valeur, le contrat la rejette. C'est le registre qui décide si une livraison entre, le bronze ne refait pas ce calcul. Une valeur absente d'un Parquet reste absente ; un « nan » est copié en texte, et le contrat le lit comme une absence. Une livraison bloquée puis renvoyée corrigée sous le même nom n'est pas un conflit : les deux versions sont au bronze, sous deux heures d'arrivée, et c'est le statut du registre qui dira laquelle lire. Le rang d'une ligne n'est pas son numéro dans le fichier : une ligne coupée décale le rang des suivantes. Une complétion (lignes corrigées) arrive comme une nouvelle livraison, sous un nouveau nom, pas comme un rejeu : le retraitement des lignes en quarantaine et les doublons de clé entre livraisons seront traités avec le silver.

## #25 — Bundle Databricks : trois cibles, trois catalogues
*2026-11-12 · [PR #25](https://github.com/X-stratum/rd-manuf/pull/25)*

**En clair.** Le projet décrit maintenant, dans un fichier, où il se déploiera sur Databricks : trois cibles (bac à sable, développement, production), chacune reliée à son catalogue de données. Databricks lui-même a vérifié ce fichier, mais rien n'est encore déployé. Un test garantit qu'il reste d'accord avec le garde-fou qui interdit de confondre les environnements, et qu'aucune adresse ni aucun secret n'y est écrit, le dépôt étant public.
**Décisions.** La cible par défaut est le bac à sable : un déploiement lancé sans précision ne touche jamais la production. L'adresse du workspace n'est jamais dans le fichier : la commande utilise le profil local de la personne qui la lance. Une seule variable, le catalogue, distingue les trois cibles. Aucune tâche ni table n'est encore décrite : c'est le squelette sur lequel le reste se branchera. Un second test garde la liste des fichiers exclus de Git : l'état local de la CLI Databricks, les profils de connexion et les jetons ne peuvent pas en être retirés sans que le test échoue.

## #24 — Environnement conda et nouveau titre du README
*2026-11-12 · [PR #24](https://github.com/X-stratum/rd-manuf/pull/24)*

**En clair.** Le projet peut maintenant recréer son environnement de travail en une seule commande, au lieu d'installer les outils à la main, et il s'est vérifié : un environnement neuf créé depuis ce fichier fait passer tous les tests. Le README porte aussi son nouveau titre, qui présente le dépôt comme une démonstration de méthode de travail appliquée à un cas concret, la R&D et la fabrication.
**Décisions.** On utilise conda pour le poste de travail, tandis que la liste des bibliothèques reste écrite à un seul endroit. Un test garde la version de Python identique dans le projet, la CI et l'environnement. Pas de conteneur pour l'instant : la CI, qui installe tout sur une machine vierge à chaque modification, joue déjà ce rôle. Le verrouillage des versions viendra avec le déploiement.

## #23 — Registre des livraisons et rejeux
*2026-11-12 · [PR #23](https://github.com/X-stratum/rd-manuf/pull/23)*

**En clair.** Chaque livraison reçue laisse maintenant une trace : une ligne de registre dit ce qui a été décidé, d'où et de quand vient le fichier, et quand il est arrivé. Un fichier déjà intégré qui arrive une seconde fois avec le même contenu est ignoré, avec une trace, au lieu d'être traité deux fois. S'il arrive avec un contenu différent sous le même nom, il est refusé et l'émetteur est prévenu : des données corrigées ne sont jamais ignorées en silence. Un fichier refusé ou bloqué, lui, peut être renvoyé sous le même nom une fois corrigé.
**Décisions.** Un rejeu à contenu identique est ignoré, comme les lignes en double ; à contenu différent, c'est un conflit, refusé comme pour une clé de ligne en conflit. Seul un fichier déjà intégré peut provoquer l'un ou l'autre. L'heure d'arrivée est toujours en UTC et donnée par l'appelant, jamais lue sur l'horloge, pour que les tests soient reproductibles. Les lignes ne sont pas encore écrites dans une table.

## #22 — Nom des fichiers, aiguillage par format et enchaînement complet
*2026-11-12 · [PR #22](https://github.com/X-stratum/rd-manuf/pull/22)*

**En clair.** Le projet sait maintenant traiter un fichier de bout en bout : il vérifie d'abord que son nom dit d'où et de quand il vient (pays, usine et instant en UTC), choisit le bon lecteur selon son format (CSV ou Parquet), puis décide. Un nom qui ne respecte pas la convention est refusé avant même d'ouvrir le fichier, avec une alerte à l'émetteur.
**Décisions.** L'heure est toujours en UTC, marquée par un Z, pour qu'un changement d'heure ne donne jamais le même nom à deux fichiers. Le code usine fait 2 lettres et 3 chiffres (usine et ligne de production), écrit à un seul endroit pour pouvoir changer. L'identifiant d'une livraison est le nom de son fichier. Le plan global est mis à jour : l'étape « Recevoir » est terminée.

## #21 — Plan global illustré dans le README
*2026-11-12 · [PR #21](https://github.com/X-stratum/rd-manuf/pull/21)*

**En clair.** La page d'accueil du projet montre maintenant, en une image, où en est le projet : les six étapes de la livraison à la mise en production, ce qui est fait, ce qui vient ensuite et ce qui reste à construire. Chaque étape se déplie dans le README, avec des liens vers le code des blocs terminés.
**Décisions.** L'image est fabriquée par un petit programme, et un test garantit qu'elle ne s'écarte pas du plan : changer un statut se fait en une ligne. GitHub ne permet pas de zones cliquables dans une image : des sections dépliables les remplacent.

## #20 — Lire un fichier au format Parquet
*2026-11-12 · [PR #20](https://github.com/X-stratum/rd-manuf/pull/20)*

**En clair.** Le projet sait maintenant lire un second format de fichier, le Parquet, en plus du CSV. Un fichier Parquet contient lui-même la description de ses colonnes et de leurs types : il n'y a ni caractère invisible, ni séparateur, ni ligne coupée à gérer. Un Parquet abîmé, ou un autre format renvoyé sous ce nom, est refusé avec une alerte comme un CSV illisible.
**Décisions.** Chaque format a son propre lecteur, qui rend la même chose (colonnes et lignes) : la réception ne sait pas de quel format vient le fichier. La bibliothèque de lecture (`pyarrow`) n'est chargée que quand on lit un Parquet.

## #19 — Refuser proprement un fichier illisible
*2026-11-12 · [PR #19](https://github.com/X-stratum/rd-manuf/pull/19)*

**En clair.** Un fichier qu'on ne peut pas lire du tout (accents mal encodés, export « Unicode » d'Excel, fichier abîmé) est maintenant refusé avec une alerte à l'émetteur, au lieu de faire s'arrêter le programme sur une erreur technique. Un fichier qui commence par une ligne vide n'est plus pris pour un fichier vide.
**Décisions.** Un fichier illisible est un problème du fichier, pas du programme : il est traité comme un refus de structure (code `FICHIER_ILLISIBLE`). L'émetteur agit, l'équipe data est informée, et le motif technique est conservé dans le message. L'en-tête est la première ligne non vide.

## #18 — Rejeter les lignes mal formées d'un fichier
*2026-11-12 · [PR #18](https://github.com/X-stratum/rd-manuf/pull/18)*

**En clair.** Une ligne coupée en route, ou dont le nombre de valeurs est faux, n'est plus acceptée par erreur : elle est écartée comme toute ligne incorrecte, comptée dans le taux de rejet, et l'émetteur est prévenu de la renvoyer. Avant, une ligne coupée juste avant sa dernière valeur facultative pouvait passer pour valide.
**Décisions.** Le lecteur met ces lignes à part sans jamais deviner à quelles colonnes appartiennent leurs valeurs. Un seul rejet par ligne, pas un par valeur manquante. L'émetteur agit, l'équipe data est informée (un transfert interrompu peut venir de chez nous). Les codes d'erreur étant un contrat avec les alertes, ce huitième code est ajouté volontairement, en même temps que sa règle d'alerte.

## #17 — Mettre en forme le message de squash
*2026-11-11 · [PR #17](https://github.com/X-stratum/rd-manuf/pull/17)*

**En clair.** Le texte qui reste dans l'historique du projet à chaque modification a maintenant une forme fixe et soignée (titres en majuscules, lignes courtes, listes à tirets), lisible partout, car GitHub n'affiche pas la mise en forme habituelle dans l'historique. Le modèle de description rappelle aussi qu'il faut remplacer ce que GitHub propose de lui-même, qui est toute la description.
**Décisions.** Texte brut, sans mise en forme Markdown (elle n'est pas affichée). Les messages déjà dans `main`, dont celui de la PR #16 qui contient toute sa description, ne sont pas réécrits.

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
