# Journal des changements

Ce fichier raconte le projet **PR par PR, du plus récent au plus ancien**, en langage clair : ce qui a changé, pourquoi, et les choix à ne pas perdre.
Pour le détail technique (fichiers, tests, preuves), ouvrir la PR indiquée sous chaque entrée.

**Le tenir à jour.** Chaque PR ajoute sa propre entrée en tête de liste, dans la même PR. L'entrée reprend le « En clair » et les « Décisions à retenir » de la description de la PR.
Les dates sont en UTC, comme les heures du projet.
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
- **Usine et ligne de production** : dans le nom d'un fichier, l'usine est le site (deux lettres, `AA`) et la ligne de production est l'une des lignes de fabrication de ce site (trois chiffres, `001`).
- **Lot** : une quantité fabriquée d'un coup, repérée par un numéro (`id_lot`) que choisit l'usine. Deux usines peuvent donc utiliser le même numéro : un lot s'identifie par son pays, son usine et son numéro.
- **Schéma d'une table** : la liste de ses colonnes, avec le type de chaque valeur (texte, entier, date…) et le droit ou non d'être vide.
- **PR** (*pull request*) : une demande de modification, relue avant d'être intégrée au projet.
- **CI** : la vérification automatique lancée à chaque PR (style du code, puis tests).
- **Sabotage** : une modification volontaire du code pour vérifier que les tests détectent bien l'erreur.

---

## #42 — Écrivain : ranger les résultats dans des tables, sans encore parler à Databricks
*2026-11-14 · [PR #42](https://github.com/X-stratum/rd-manuf/pull/42)*

**En clair.** Le pipeline sait maintenant préparer l'écriture de tout ce qu'il produit (le registre, le bronze, les lignes coupées, le silver, la quarantaine) dans des tables, et relire ces tables pour retrouver son état. Rien n'est encore envoyé à Databricks : tout se vérifie sur l'ordinateur, avec un magasin en mémoire qui sert de référence, et des requêtes SQL générées et inspectées sans être lancées. La journée multi-usines de la #41 tourne sans changement sur cet état adossé à un magasin, et donne exactement le même résultat que sur des listes ; rejouée, elle n'ajoute que des lignes de registre ; une panne injectée à n'importe quel moment d'une écriture se rattrape en reprenant la livraison.
**Décisions.** Les couches de données s'écrivent d'abord et le registre en dernier : le registre est ce qui décide d'un rejeu, donc ce qui dit qu'une livraison est intégrée, et entre deux tables il n'y a pas d'atomicité. Chaque écriture n'insère que ce qui manque sur la clé de traçabilité de la table, et la livraison reprise avec la même heure d'arrivée ne duplique rien. Tant qu'une livraison n'a pas sa décision, ses lignes de données ne sont pas lues comme intégrées (sinon la livraison suivante les prendrait pour du déjà intégré et irait en quarantaine à tort), et on refuse d'écrire une livraison classée après elle avant que la première soit reprise, avec le classement du silver (l'heure d'arrivée, puis le nom à heure égale ; un test compare les deux) : sinon les deux entreraient au silver, chaque lot deux fois, sans aucune alerte, ce que la revue a fait mesurer. Une panne avant toute écriture de données ne laisse aucune trace : reprendre la livraison avant d'écrire les suivantes reste à la charge de ce qui orchestrera les passages. La quarantaine a une clé dont deux parties sont tantôt absentes (ligne bien formée ou coupée) : elles se comparent par l'égalité tolérante à l'absence, sinon ces lignes seraient réinsérées à chaque reprise. Aucune valeur venue d'un fichier n'entre jamais dans le texte d'une requête : elles voyagent dans un seul paramètre JSON, typé par le schéma de la table ; seuls des noms de tables et de colonnes, vérifiés, sont écrits dans le SQL. Les alertes n'ont pas de table (elles seront envoyées) : elles restent en mémoire. Ce qui n'est pas prouvé, faute de workspace : le comportement réel de Delta (le `MERGE`, les types, les tableaux, le fuseau d'une colonne d'instant), l'acceptation du paramètre JSON par le service, la taille maximale d'une requête (mille enregistrements par lot est un choix prudent, pas une mesure) et la conversion des valeurs rendues par le service, que le futur exécuteur devra faire. Cela se prouvera en jouant la journée sur le playground depuis l'ordinateur, dans la PR suivante ; le bloc « Écrire en tables » du plan reste donc à faire.

## #41 — Journée de test : des livraisons de trois pays jouées de bout en bout
*2026-11-14 · [PR #41](https://github.com/X-stratum/rd-manuf/pull/41)*

**En clair.** Une journée type est maintenant écrite une fois pour toutes : des livraisons de la France, de l'Allemagne et des États-Unis, une par situation (saine, avec rejets, bloquée puis renvoyée corrigée, refusée de plusieurs façons, rejouée, en conflit, avec une ligne coupée), jouées dans l'ordre d'arrivée sur le vrai enchaînement. Le résultat de chaque livraison s'ajoute à un état commun (registre, bronze, silver, quarantaine, alertes). On vérifie chaque livraison contre ce qu'on en attendait, puis des règles qui doivent rester vraies toute la journée : aucun lot deux fois au silver, rien de perdu entre les couches, aucune alerte pour une livraison saine ou rejouée. Enfin la journée est rejouée le lendemain sur le même état : les livraisons déjà intégrées sont ignorées, les refus se répètent, et la première version bloquée d'une livraison renvoyée corrigée devient un conflit, pas un rejeu. Chaque situation était déjà testée seule ; ce qui est nouveau, c'est la journée entière et l'état cumulé, que l'écrivain Spark remplacera par des tables.
**Décisions.** Ce qu'on attend de chaque livraison est écrit d'après sa recette (« 2 lignes hors plage sur 100 »), jamais lu dans un résultat du pipeline, sinon la journée serait son propre juge ; les nombres de rejets viennent du seuil du contrat, et un test rejoue la journée avec d'autres seuils, au bord exact de ce que le seuil tolère. Le module vit dans le code et non dans les tests, comme le simulateur : l'écrivain Spark et les tests de volume pourront jouer la même journée, et seul l'état sera remplacé. Les règles de la journée ne dépendent pas des livraisons choisies : un script hors tests les a aussi vérifiées sur des sous-ensembles et des ordres tirés au hasard, puis rejoués. Une autre usine du même pays reprend les numéros de lot d'une première sans conflit : un lot s'identifie par son pays et son usine. Rejouée après sa version corrigée, la première version bloquée est refusée comme conflit de livraison, parce que le même nom est intégré avec un autre contenu ; c'est le bon comportement et il est écrit comme tel. Les règles de conservation valent pour une journée sans doublon exact. Pas encore fait : écrire en tables, mesurer sur de vraies tailles, jouer plusieurs jours de suite, et les trois points notés à la #40 (une ligne coupée efface l'anomalie qu'elle remplace, les valeurs d'une colonne retirée sont vérifiées en Parquet et pas en CSV, un champ hors contrat est ignoré en Parquet).

## #40 — Simulateur : fabriquer des fichiers abîmés (colonnes, fichier vide, ligne coupée)
*2026-11-14 · [PR #40](https://github.com/X-stratum/rd-manuf/pull/40)*

**En clair.** Le simulateur sait maintenant abîmer un fichier comme une usine pourrait le faire : retirer des colonnes, en ajouter qui n'existent pas dans le contrat, en écrire certaines deux fois, livrer un fichier sans un octet, un fichier illisible (un CSV qui n'est pas de l'UTF-8, un Parquet tronqué) ou des lignes coupées en route. On demande ces défauts à l'écriture, en disant combien (`{COLONNE_MANQUANTE: 2, LIGNE_COUPEE: 3}`), comme on demandait déjà les anomalies de valeur. Chaque défaut est jugé par la réception et par l'enchaînement complet, qui doivent répondre exactement le code attendu : refus avec le bon motif et la bonne alerte, ou ligne coupée gardée à part au bronze et en quarantaine au silver. Une colonne en double dans un Parquet était refusée comme « fichier illisible » ; elle est maintenant signalée comme en CSV, avec le nom de la colonne à corriger.
**Décisions.** Les défauts se demandent à l'écriture du fichier, pas dans `generer_livraison`, parce qu'ils viennent de la forme du fichier et non de la valeur d'une ligne ; un défaut qui n'existe pas dans un format est refusé avec une explication, sans écrire de fichier (une ligne coupée en Parquet, un Parquet sans octet : il est illisible, pas vide ; pour zéro ligne, on écrit une livraison vide). Le fichier ne dépend que de la demande : les colonnes retirées sont les dernières du contrat, les doublons les premières colonnes du fichier, les lignes coupées les dernières lignes. Comme les lignes coupées prennent la place des dernières lignes, couper une ligne qui portait une anomalie efface cette anomalie : seule la coupure est jugée, et un test le fixe. Un nombre à zéro veut dire « non demandé ». L'écriture CSV passe de `DictWriter` à `csv.writer`, qui accepte deux colonnes de même nom ; sans défaut demandé, les octets du fichier sont identiques à ceux d'avant, et les deux anciens comportements (un champ inconnu refusé, une clé absente écrite vide) sont désormais testés. Le lecteur Parquet lit avec `ParquetFile` au lieu de `read_table` pour rapporter les noms de colonnes tels que reçus ; un chemin de dossier lève maintenant une erreur système au lieu d'être lu comme un jeu de données, ce que la réception n'envoie jamais. Pas encore fait : le nom de fichier invalide et le conflit de livraison (ils dépendent du nom et de deux livraisons, ce sera le scénario de la journée multi-usines), et les fichiers de test écrits à la main ne sont pas migrés vers le simulateur.

## #39 — Simulateur : numéroter les lots, nommer les fichiers, écrire du Parquet
*2026-11-14 · [PR #39](https://github.com/X-stratum/rd-manuf/pull/39)*

**En clair.** Le simulateur de livraisons fabrique maintenant ce qui entoure les lignes. Il numérote les lots à partir d'un numéro choisi, pour que deux livraisons d'une même usine n'aient pas les mêmes numéros. Il fabrique le nom d'un fichier, avec la convention du projet. Et il écrit une livraison en Parquet, avec des colonnes typées d'après le contrat, comme l'enverrait une usine qui exporte autre chose qu'un CSV. Avant, chaque test écrivait son nom à la main et fabriquait son Parquet à sa façon.
**Décisions.** Le nom est fabriqué avec les constantes de `nom_fichier.py` puis relu par `lire_nom_fichier`, qui refuse une pièce hors convention : la forme du nom n'est écrite qu'à un seul endroit. Une heure sans fuseau est refusée, comme dans le registre, et une fraction de seconde aussi, parce que le nom s'arrête à la seconde. Les numéros de lot vont de 1 à 999 999, les 6 chiffres du contrat : au-delà, le contrat refuserait le lot, et le simulateur le dit avant plutôt que de fabriquer un lot invalide. Une colonne Parquet est typée : un texte dans une colonne numérique ne peut pas s'y écrire, donc l'anomalie « type invalide » n'existe qu'en CSV, et le simulateur la refuse en Parquet en nommant la colonne et la valeur, sans écrire de fichier. Il refuse de même ce que Parquet convertirait sans rien dire (un décimal non entier dans une colonne entière, une date avec heure), en demandant au contrat plutôt qu'en recopiant ses règles : un fichier ne doit pas dire autre chose que la ligne qu'on lui a donnée. Les six autres anomalies reçoivent la même décision dans les deux formats, ce qu'un test vérifie pour chacune. Pas encore fait : les défauts de structure (colonne manquante, fichier vide, ligne coupée), ce sera la PR suivante, puis le scénario de la journée multi-usines.

## #38 — Alertes : un conflit de clé avec une livraison plus ancienne prévient l'émetteur
*2026-11-14 · [PR #38](https://github.com/X-stratum/rd-manuf/pull/38)*

**En clair.** Quand une ligne d'une livraison porte le numéro d'un lot déjà intégré par une livraison plus ancienne de la même usine, avec un contenu différent, elle part en quarantaine depuis la #32. Mais personne n'était prévenu : le silver n'appelait pas les alertes, et la réception ne connaît que la livraison en cours. L'émetteur reçoit maintenant une alerte, comme pour les autres rejets, avec le nombre de lignes concernées et la colonne à regarder.
**Décisions.** Le code reste `CLE_EN_CONFLIT`, sa règle est déjà dans le catalogue (l'émetteur agit, le métier est informé) : aucune entrée à ajouter. Il n'y a qu'une alerte par livraison et par code : si la livraison a aussi un conflit de clé à l'intérieur d'elle-même, que la réception alerte déjà, les deux sortes de lignes s'additionnent dans la même alerte au lieu d'en produire deux. Seul le silver sait combien de lignes viennent d'une autre livraison (sa quarantaine contient aussi des rejets déjà alertés), donc il le compte lui-même et le dit dans son résultat. Le statut de la livraison ne change pas : elle reste acceptée, avec une alerte. Pas encore fait : l'alerte ne nomme pas la livraison d'origine, qui reste lisible dans le message de la quarantaine.

## #37 — Enchaînement : une livraison de bout en bout en un seul appel
*2026-11-14 · [PR #37](https://github.com/X-stratum/rd-manuf/pull/37)*

**En clair.** Le projet sait maintenant faire entrer une livraison dans toutes les couches en un seul appel : le registre décide, puis, si elle entre, le bronze range ce qui est arrivé, le silver rejuge les lignes, et les alertes disent qui doit agir. Chaque étape existait déjà et se testait seule, mais leur ordre n'était écrit que dans des tests. Rien n'est écrit dans une table, et le plan global compte un bloc de plus (21 sur 34).
**Décisions.** L'état reste chez l'appelant : le registre déjà écrit, les lignes silver déjà intégrées et l'heure d'arrivée sont passés en paramètres, jamais lus, jamais modifiés, et le résultat dit seulement ce qu'il y a à ajouter. Aujourd'hui l'appelant est un test ; demain ce sera l'écrivain Spark, qui remplacera les listes par des tables. L'enchaînement ne décide rien lui-même : c'est le registre qui dit si la livraison entre, et le silver garde sa garantie de décider comme la réception. Le fichier est ouvert trois fois au plus : son empreinte, la réception, puis le bronze ; un nom invalide n'en ouvre aucune, un rejeu une seule, un refus de structure deux. On optimisera plus tard. Chaque résultat vide est fabriqué neuf, parce que ses listes sont modifiables : un objet partagé aurait gardé les lignes qu'un appelant y aurait rangées. Pas encore fait : un conflit de clé entre deux livraisons part en quarantaine sans alerte, ce sera la PR suivante.

## #36 — Silver : un lot s'identifie par son pays, son usine et son numéro
*2026-11-13 · [PR #36](https://github.com/X-stratum/rd-manuf/pull/36)*

**En clair.** Le silver ne confond plus les lots de deux usines. Deux usines qui numérotent chacune leurs lots depuis `LOT-000001` se heurtaient : la seconde livraison était acceptée à la réception, mais toutes ses lignes partaient en quarantaine comme « en conflit » ; et si le contenu était le même, la ligne de la seconde usine était ignorée sans aucune trace. Un lot s'identifie maintenant par son pays, son usine et son numéro, et chaque ligne du silver porte son pays et son usine.
**Décisions.** La ligne de production ne fait pas partie de l'identité d'un lot : une usine numérote les lots de toutes ses lignes, et deux de ses lignes qui réutiliseraient un numéro avec un contenu différent restent en conflit. Si ce choix est faux, l'erreur est visible (une quarantaine), alors que le choix inverse laisserait passer de vrais doublons sans signal ; l'identité est écrite à un seul endroit, facile à changer. Le contrat n'est pas modifié, et la décision de la réception non plus (statut et seuil de rejet inchangés) : seule la comparaison des lots entre livraisons change. `pays` et `usine` sont des noms réservés, comme `livraison_id` : une colonne du contrat qui porterait ces noms est refusée, en bronze comme en silver. Une livraison dont le registre n'a ni pays ni usine est refusée par le silver avec une erreur claire, plutôt qu'écrite avec des valeurs vides. Pas encore décidé : le lien entre la ligne de production du nom de fichier et la colonne `id_ligne` des données.

## #35 — Nom de fichier : séparer l'usine et sa ligne de production
*2026-11-13 · [PR #35](https://github.com/X-stratum/rd-manuf/pull/35)*

**En clair.** Dans le nom d'un fichier, `AA_001` n'est pas un code d'usine en un seul morceau : `AA` est l'usine (deux lettres) et `001` est une ligne de production de cette usine (trois chiffres). Le projet lit maintenant les deux séparément, et le registre des livraisons garde une colonne `ligne` à côté de `usine`. Un nom de fichier valide reste valide : `livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv` s'écrit et se lit comme avant.
**Décisions.** Le sens du code était décidé à la #22 (« usine et ligne de production »), mais le code le gardait en un seul morceau, ce qui laissait lire `AA_001` comme « le numéro de l'usine ». Séparer les deux est le préalable pour distinguer plusieurs usines d'un même pays, puis plusieurs lignes d'une même usine. Un fichier déjà intégré qui revient est un rejeu si le pays, l'usine, la ligne et l'instant sont les mêmes : sans la ligne, deux lignes de production d'une même usine qui envoient à la même seconde seraient prises l'une pour le rejeu de l'autre. La colonne `ligne` est du texte et non un nombre, pour garder les zéros de `001`. Le message de refus d'un nom nomme maintenant `{USINE}` et `{LIGNE}` et donne leurs largeurs, parce que la forme abstraite `LL_NNN` ne disait pas que c'étaient deux informations. Ces largeurs sont répétées à la main dans le message, comme l'exemple répète la forme : un test échoue si elles ne sont plus celles des motifs, pour que le message ne mente jamais à l'émetteur. Pas encore décidé : le lien entre la ligne du nom et la colonne `id_ligne` des données (L1, L2, L3). Cette PR ne change pas non plus la comparaison des lots entre livraisons : elle se fait encore toutes usines confondues, ce sera la PR suivante.

## #34 — Tables : les schémas du registre, du bronze et du silver
*2026-11-13 · [PR #34](https://github.com/X-stratum/rd-manuf/pull/34)*

**En clair.** Le projet sait maintenant décrire les cinq tables où seront écrites les données, et fabriquer le texte SQL qui les crée : le registre des livraisons, le bronze, les lignes coupées, le silver et la quarantaine. Rien n'est encore créé ni écrit dans Databricks : ce sont des descriptions, vérifiées par des tests.
**Décisions.** Les tables sont rangées par couche : le registre dans `ops`, le bronze et les lignes coupées dans `bronze`, le silver et la quarantaine dans `silver`. Les colonnes de données se déduisent du contrat, pas d'une liste écrite à part : le bronze les garde toutes en texte, sans obligation, parce qu'il ne juge rien ; le silver les type (texte, entier, date) et rend NOT NULL celles que le contrat déclare obligatoires. Un décimal devient un `DOUBLE`, pas un `DECIMAL(p, s)` : le code manipule un nombre à virgule flottante et le contrat ne dit ni précision ni échelle, donc choisir une précision serait inventer une règle. Le contenu d'une ligne en quarantaine a deux formes (les valeurs en texte, ou les champs bruts d'une ligne coupée) : chacune a sa colonne typée et l'autre reste vide, plutôt qu'un JSON à relire. Une colonne du contrat qui porterait un nom de traçabilité (`livraison_id`, `heure_arrivee`, `numero_ligne`, `version_contrat`) est refusée, comme un type que les tables ne savent pas ranger et un nom qui n'est pas un identifiant simple (lettres, chiffres et `_`, sans commencer par un chiffre) ; les noms se comparent sans tenir compte de la casse, comme le fait Delta. Le SQL de création passe par le garde-fou de catalogue : un nom inconnu est refusé. Chaque schéma est gardé en test d'accord avec sa dataclass et avec le contrat : ajouter un champ à une ligne, ou une colonne au contrat, sans mettre à jour le schéma fait échouer la CI. Un test fait aussi passer de vraies lignes du registre, du bronze, du silver et de la quarantaine par ces schémas.

## #33 — Plan global : une septième étape « Exploiter »
*2026-11-13 · [PR #33](https://github.com/X-stratum/rd-manuf/pull/33)*

**En clair.** Le plan global (image et README) montre maintenant tout ce qu'il faudra pour faire tourner et surveiller le pipeline, pas seulement le construire : une septième étape « Exploiter » avec sept blocs, l'orchestration, l'ingestion incrémentale, la surveillance, le retraitement, les droits et secrets, l'évolution du contrat et les tests de volume. Ils sont tous à faire (⚪) : le plan passe de 20 blocs terminés sur 26 à 20 sur 33. Rien d'autre ne change dans le code du projet.
**Décisions.** Ces blocs étaient absents du plan, alors qu'un pipeline qui n'est ni lancé, ni surveillé, ni rejouable n'est pas terminé : les voir en blanc empêche de croire le projet plus avancé qu'il ne l'est. Le plan n'est plus dessiné à la main : la largeur de l'image se déduit du nombre d'étapes, et la rangée des outils prend la même largeur que la chaîne, sans valeur recopiée. Les colonnes passent de 186 à 204 px pour laisser de la place aux titres. Quatre gardes en test vérifient la géométrie (les colonnes tiennent dans leur couloir, la dernière carte dans sa colonne, les titres et les descriptions dans leur carte, la rangée des outils sur la largeur de la chaîne). Trois gardes de plus vérifient que le nombre d'étapes annoncé dans le pied de l'image, dans sa description et dans le texte alternatif du README suit le nombre de colonnes : la première version de la PR annonçait encore six étapes à côté de la septième colonne, sans qu'aucun test ne le voie. La garde des textes est une estimation (nombre de caractères × largeur moyenne), pas une mesure : elle a signalé que « Silver : une livraison » dépassait peut-être déjà de sa carte depuis la #31, ce qu'aucun rendu n'a confirmé.

## #32 — Silver : une clé déjà intégrée par une livraison plus ancienne
*2026-11-13 · [PR #32](https://github.com/X-stratum/rd-manuf/pull/32)*

**En clair.** Le silver sait maintenant traiter une clé qui existe déjà : quand une livraison contient une ligne dont la clé a été intégrée par une livraison arrivée avant, la ligne est ignorée si le contenu est identique, et mise en quarantaine s'il diffère. La première arrivée fait foi, car on ne sait pas laquelle des deux versions est la bonne. Rien n'est encore écrit dans une table.
**Décisions.** Le statut de la livraison et son seuil de rejet ne changent pas : la réception ne connaît que la livraison en cours, et le silver ne doit jamais décider autrement qu'elle (sinon 20 conflits sur 20 auraient bloqué à tort une livraison que la réception avait acceptée). Le code d'erreur reste `CLE_EN_CONFLIT`, comme pour deux lignes d'une même livraison, avec un message qui nomme la livraison d'origine : aucune entrée de plus au catalogue d'alertes. « Première » veut dire arrivée avant, d'après l'heure d'arrivée du registre : un retraitement ne voit que les livraisons arrivées avant lui. Deux livraisons arrivées à la même heure sont départagées par leur nom (sinon chacune ignorerait l'autre et la clé serait intégrée deux fois), et la livraison courante est exclue par son nom, même retraitée avec une autre heure. Une ligne rejetée ne réserve pas sa clé, comme au sein d'une livraison. Le silver reçoit les lignes déjà intégrées en paramètre : il reste une fonction pure, et c'est l'écriture en tables, la brique suivante, qui les lira dans la table.

## #31 — Silver : les lignes valides typées et la quarantaine
*2026-11-13 · [PR #31](https://github.com/X-stratum/rd-manuf/pull/31)*

**En clair.** Le projet sait maintenant construire le silver d'une livraison : les lignes valides, avec leurs valeurs typées (nombres, dates), et la quarantaine des lignes rejetées, avec ce qu'il faut corriger. Il relit le bronze, rejuge la livraison avec le contrat, et refuse de rendre quoi que ce soit s'il ne décide pas comme la réception. Une livraison bloquée, refusée ou rejouée ne rend rien. Rien n'est encore écrit dans une table.
**Décisions.** Le silver reprend la fonction de jugement de la réception : une seule source de vérité pour les règles. Avec la même version de contrat que le registre, un statut différent est une erreur claire : c'est ce qui aurait intégré 97 lignes d'une livraison bloquée si les lignes coupées manquaient (exemple vérifié : 3 % de rejets à la réception, 1 % sans elles). Avec une autre version, c'est un retraitement autorisé, et chaque ligne porte la version qui l'a jugée. Une livraison bloquée ne rend ni lignes ni quarantaine : elle est bloquée en entier, le diagnostic est au registre, et la quarantaine ne sert qu'aux livraisons acceptées avec rejets, lignes coupées comprises. Un doublon exact est ignoré (il reste au bronze). La conversion de valeurs du contrat devient publique, avec sa précondition écrite : la ligne doit avoir été jugée d'abord. La brique suivante : une même clé reçue dans deux livraisons.

## #30 — Plan global : remis à jour, avec la suite et des gardes
*2026-11-13 · [PR #30](https://github.com/X-stratum/rd-manuf/pull/30)*

**En clair.** Le plan global (image et README) montre maintenant l'ordre réel de la suite : le silver d'une livraison est la prochaine brique, puis les doublons entre livraisons, puis l'écriture en tables au premier déploiement. Registre et bronze y sont verts : leur logique est écrite et testée, seul le stockage manque. Les puces sont plus courtes et couvrent aussi les environnements et le contrat 2.0.0. Et la CI vérifie désormais que le README et le plan ne divergent plus.
**Décisions.** Le vert veut dire « logique écrite et testée », pas « écrit dans une table » : sinon un bloc terminé côté code paraissait inachevé pendant des semaines. L'écriture en tables est un bloc à part, placé au déploiement, et le silver est le seul bloc orange. Une puce ne dépasse pas 220 caractères : le détail est dans le code, les tests et ce journal. Quatre gardes en test (statuts identiques, chaque module listé, liens valides, longueur des puces) : un plan oublié fait échouer la CI au lieu de dépendre de la vigilance de chacun. La hauteur de l'image se règle sur la colonne la plus fournie, ce qui permet d'ajouter des blocs sans rien décaler à la main.

## #29 — Date sans heure : une colonne date ne contient qu'une date
*2026-11-13 · [PR #29](https://github.com/X-stratum/rd-manuf/pull/29)*

**En clair.** Une colonne de type date n'accepte plus qu'une date : une date avec une heure est maintenant refusée, quel que soit le format du fichier. Avant, un fichier Parquet aux dates horodatées était accepté alors que le même contenu en CSV était refusé. Le contrat de données passe en version 2.0.0.
**Décisions.** Une règle unique pour les deux formats : sans elle, un silver qui relit le texte du bronze aurait décidé autrement que la réception (essai vérifié : 10 lignes acceptées à la réception, toutes rejetées vues du texte du bronze). On refuse plutôt que de garder la date et de couper l'heure : l'heure ne se perd pas en silence, et le CSV n'accepte pas une forme qu'il refusait. Une date-heure à minuit est refusée aussi : c'est encore une date-heure. La version est majeure, comme l'écrit le contrat pour une règle durcie : des fichiers jadis acceptés peuvent être bloqués, et la version doit continuer à dire avec quelle règle une livraison a été jugée. Rien n'est en production et aucun consommateur gold n'existe : personne à prévenir.

## #28 — Bronze : conserver aussi les lignes coupées
*2026-11-12 · [PR #28](https://github.com/X-stratum/rd-manuf/pull/28)*

**En clair.** Le bronze conserve maintenant aussi les lignes coupées d'un fichier CSV, à part des autres : leurs valeurs telles que reçues et leur numéro de ligne dans le fichier. Rien de ce qui est arrivé n'est donc perdu à cette étape, et le silver pourra juger une livraison exactement comme la réception.
**Décisions.** Cela remplace une décision de la #26, qui laissait ces lignes hors du bronze : on s'est aperçu qu'un silver relisant le bronze sans elles pouvait décider autrement que la réception. Exemple vérifié sur le vrai contrat : 100 lignes dont 1 invalide et 2 coupées donnent « bloquée » à la réception (3 % de rejets pour un seuil de 2 %), mais « acceptée avec rejets » (1 %) si les lignes coupées manquent, et 97 lignes auraient été intégrées alors que le registre dit « bloquée ». Les lignes coupées restent rangées à part, car on ne sait pas à quelle colonne va chaque valeur. Elles suivent la même règle d'entrée que les autres lignes (livraison acceptée, acceptée avec rejets ou bloquée ; ni refus ni rejeu), et c'est toujours le registre qui décide. Leur numéro est celui que relève le lecteur dans le fichier (l'en-tête est la ligne 1 ; pour une ligne coupée sur plusieurs lignes par un retour à la ligne entre guillemets, c'est la dernière), alors que celui des lignes bien formées est un rang : les deux ne se comparent pas. Le bronze vérifie aussi que le fichier lu compte autant de lignes que le registre en a reçu : sans cette garde, un silver pourrait rejuger une livraison incomplète.

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
