# DATA ENGINEER - Workflow Demonstration -

# use case : R&D et Manufacturing

Pipeline de données **R&D Manufacturing** sur Databricks (architecture médaillon bronze → silver → gold),
construit pas à pas avec un processus d'équipe : branches, Pull Requests, CI GitHub Actions et revue de code.

## En clair

Des usines envoient des fichiers de données de production, appelés « livraisons ». Ce projet vérifie chaque livraison avant de s'en servir :
le fichier est-il lisible, a-t-il les bonnes colonnes, chaque valeur respecte-t-elle les règles prévues ?
Une ligne incorrecte est écartée au lieu d'être utilisée, une livraison trop abîmée est refusée,
et le projet détermine qui doit agir (l'usine émettrice, le métier ou l'équipe data) et qui doit seulement être informé.
Les règles sont écrites dans un « contrat de données », un fichier lisible par le métier.

<p align="center">
  <a href="docs/plan-global.svg">
    <img src="docs/plan-global.svg" alt="Plan global du projet : sept étapes, de la réception de la livraison à son exploitation, avec les acteurs, les règles écrites et les outils, et l'avancement de chaque bloc." width="100%">
  </a>
</p>

<p align="center"><sub>Cliquer sur l'image pour l'ouvrir en grand.</sub></p>

## Plan global

🚧 Projet en construction. 🟢 fait (logique écrite et testée) · 🟠 prochain · ⚪ à venir. Rien n'a encore tourné sur un workspace : le code d'écriture existe et est testé, mais n'a jamais été lancé. Cliquez sur une étape pour la déplier : les blocs terminés renvoient vers le code.

<details>
<summary><b>01 · Recevoir</b> : lire le fichier de l'usine</summary>

- 🟢 **Aiguillage** ([`lecture.py`](src/rd_manuf/lecture.py), [`reception.py`](src/rd_manuf/reception.py)) : le nom du fichier choisit le lecteur (CSV ou Parquet) ; une fonction enchaîne tout.
- 🟢 **Lecture** ([`lecture.py`](src/rd_manuf/lecture.py)) : lit le CSV et le Parquet ; un fichier abîmé, coupé ou mal encodé est refusé proprement.
- 🟢 **Nom du fichier** ([`nom_fichier.py`](src/rd_manuf/nom_fichier.py)) : pays, usine (`AA`), ligne de production (`001`) et instant UTC, contrôlés avant toute lecture ; un nom invalide est refusé.

</details>

<details>
<summary><b>02 · Contrôler</b> : la structure, puis chaque ligne</summary>

- 🟢 **Structure** ([`structure.py`](src/rd_manuf/structure.py)) : colonnes manquantes, inconnues ou en double, fichier vide.
- 🟢 **Jugement des lignes** ([`contrat.py`](src/rd_manuf/contrat.py)) : chaque ligne contre le contrat (une date n'a pas d'heure) ; trop de rejets bloquent la livraison.

</details>

<details>
<summary><b>03 · Décider</b> : une décision unique par livraison</summary>

- 🟢 **Décision** ([`reception.py`](src/rd_manuf/reception.py)) : acceptée, acceptée avec rejets, bloquée ou refusée.
- 🟢 **Registre** ([`registre.py`](src/rd_manuf/registre.py)) : une ligne par livraison (décision, origine, arrivée) ; un rejeu identique est ignoré, un contenu différent refusé.
- 🟢 **Enchaînement** ([`pipeline.py`](src/rd_manuf/pipeline.py)) : du fichier reçu au registre, au bronze, au silver et aux alertes, en un appel ; l'état reste chez l'appelant.

</details>

<details>
<summary><b>04 · Stocker</b> : bronze, silver, gold</summary>

- 🟢 **Bronze** ([`bronze.py`](src/rd_manuf/bronze.py)) : les lignes reçues, en texte, avec leur traçabilité ; les lignes coupées d'un CSV sont gardées à part.
- 🟢 **Silver : une livraison** ([`silver.py`](src/rd_manuf/silver.py)) : les lignes typées ; les rejetées vont en quarantaine ; il décide comme la réception.
- 🟢 **Silver : doublons** ([`silver.py`](src/rd_manuf/silver.py)) : un lot (pays, usine, numéro) déjà intégré par une livraison plus ancienne : même contenu ignoré, sinon quarantaine.
- ⚪ **Gold** : des indicateurs prêts à l'emploi pour le métier.

</details>

<details>
<summary><b>05 · Prévenir</b> : qui agit, qui est informé</summary>

- 🟢 **Routage d'alertes** ([`alertes.py`](src/rd_manuf/alertes.py), [`catalogue.yaml`](contracts/alertes/catalogue.yaml)) : qui agit et qui est informé (des rôles), y compris pour un lot déjà intégré autrement.
- ⚪ **Annuaire** : les contacts, gardés hors du dépôt public.
- ⚪ **Envoi** : prévenir réellement les bonnes personnes (rien n'est encore envoyé).

</details>

<details>
<summary><b>06 · Livrer</b> : du bac à sable à la production</summary>

- 🟢 **Écrivain** ([`ecriture.py`](src/rd_manuf/ecriture.py), [`executeur_databricks.py`](src/rd_manuf/executeur_databricks.py)) : écrit et relit les cinq tables en SQL ; testé sans workspace.
- 🟠 **Première exécution** ([`sondage.py`](src/rd_manuf/sondage.py)) : en lecture seule sur le playground : essais de conversion, présence de nos tables. Pas encore lancé.
- ⚪ **Déploiement dev** ([`databricks.yml`](databricks.yml)) : trois cibles reliées à leurs catalogues, validées par Databricks ; rien n'est déployé.
- ⚪ **Mise en production** : version 0.1.0, puis reprise sur incident.

</details>

<details>
<summary><b>07 · Exploiter</b> : faire tourner et surveiller le pipeline</summary>

- ⚪ **Orchestration** : les étapes enchaînées en jobs Databricks, un passage par livraison.
- ⚪ **Ingestion incrémentale** : ne traiter que les nouveaux fichiers du dossier d'arrivée.
- ⚪ **Surveillance** : fraîcheur des livraisons, volumes, taux de rejet par usine, échecs.
- ⚪ **Retraitement** : reprendre une livraison corrigée ou rejugée sous un autre contrat, avec un signal de reclassification.
- ⚪ **Droits et secrets** : qui accède à quoi (catalogues), et où vivent les jetons.
- ⚪ **Évolution du contrat** : changer de version sans casser l'historique ni les lignes déjà intégrées.
- ⚪ **Tests de volume** : mesurer le pipeline sur de vraies tailles de livraison, pour choisir entre Python et Spark.

</details>

<details>
<summary><b>Règles écrites et outils</b> : ce qui garantit la qualité</summary>

- 🟢 **Contrat de données** ([`livraison_production.yaml`](contracts/data/livraison_production.yaml)) : colonnes, types, limites et règles, lisibles par le métier ; version 2.0.0.
- 🟢 **Codes d'erreur** : stables, jamais renommés, car les alertes s'appuient dessus.
- 🟢 **Environnements** ([`config.py`](src/rd_manuf/config.py)) : un catalogue par cible (playground, dev, prod) ; un catalogue inconnu est refusé.
- 🟢 **Simulateur** ([`simulateur.py`](src/rd_manuf/simulateur.py)) : fabrique des livraisons de test (CSV ou Parquet, noms, numéros de lot) avec des anomalies et des fichiers abîmés demandés, pour rejouer un incident.
- 🟢 **Journée de test** ([`journee.py`](src/rd_manuf/journee.py)) : joue des livraisons de trois pays sur l'enchaînement, puis les rejoue le lendemain, et vérifie que l'état reste cohérent.
- 🟢 **Tests, CI et revue** ([`tests/unitaires/`](tests/unitaires/), [`.github/`](.github/)) : tests avant le code, CI à chaque PR, environnement conda en une commande.
- 🟢 **Journal des changements** ([`CHANGELOG.md`](CHANGELOG.md)) : le projet raconté pas à pas, en langage clair.

</details>

## Environnements (catalogues Unity Catalog)

| Environnement | Catalogue | Rôle |
|---|---|---|
| playground | `rd_manuf_playground` | Tests destructifs et scénarios libres |
| dev | `rd_manuf_dev` | Intégration sur les livraisons rejouées de la prod |
| prod | `rd_manuf_prod` | Production |

## Principe

Le dépôt contient le **code** ; les catalogues contiennent les **données**. Le même code tourne partout :
l'environnement se choisit par le paramètre `catalogue`, jamais par une copie de dossier.

## Organisation du dépôt

- [`contracts/data/`](contracts/data/) : le contrat de données de la livraison de production (colonnes, types, limites, règles).
- [`contracts/alertes/`](contracts/alertes/) : pour chaque code d'erreur, le rôle qui agit et ceux qui sont informés.
- [`src/rd_manuf/`](src/rd_manuf/) : le code.
  - `contrat.py` : chargement du contrat, contrôle des lignes et des livraisons, codes d'erreur ;
  - `structure.py` : contrôle de l'en-tête d'une livraison ;
  - `reception.py` : la décision unique sur une livraison, et la réception d'un fichier (nom, lecture, décision) ;
  - `nom_fichier.py` : la convention de nom des fichiers livrés ;
  - `registre.py` : la ligne de registre de chaque livraison, et la détection des rejeux ;
  - `bronze.py` : les lignes bronze d'une livraison, copiées en texte avec leur traçabilité, et ses lignes coupées conservées à part ;
  - `pipeline.py` : l'enchaînement d'une livraison, du fichier reçu au registre, au bronze, au silver et aux alertes, sans rien écrire ;
  - `silver.py` : les lignes valides typées et la quarantaine d'une livraison, rejugées depuis le bronze comme à la réception ;
  - `tables.py` : les schémas des cinq tables (registre, bronze, lignes coupées, silver, quarantaine) et leur SQL de création, sans rien écrire ;
  - `ecriture.py` : l'écrivain, sans workspace : les conversions lignes ↔ tables, un magasin en mémoire ou en SQL, et l'état adossé à un magasin ;
  - `executeur_databricks.py` : l'exécuteur réel : envoie les requêtes de l'écrivain à un entrepôt SQL Databricks et rend des lignes typées ;
  - `sondage.py` : la première exécution sur un workspace, en lecture seule : des essais de conversion et la présence de nos tables ;
  - `alertes.py` : le routage des alertes selon le catalogue ;
  - `lecture.py` : lecture des fichiers CSV et Parquet, et choix du lecteur selon le format ;
  - `simulateur.py` : fabrication de livraisons de test (lignes, numéros de lot, noms de fichiers, CSV et Parquet, fichiers abîmés) ;
  - `journee.py` : une journée multi-usines jouée sur l'enchaînement, avec l'état cumulé et les invariants qui doivent rester vrais ;
  - `config.py` : garde-fou sur le choix de l'environnement.
- [`tests/unitaires/`](tests/unitaires/) : les tests.
- [`docs/`](docs/) : l'image du plan global et le programme qui la fabrique.
- [`environment.yml`](environment.yml) : l'environnement conda de travail.
- [`databricks.yml`](databricks.yml) : le bundle Databricks, qui relie chaque cible de déploiement à son catalogue.
- [`.github/`](.github/) : la CI et le modèle de description des Pull Requests.

## Installer et lancer les tests

Il faut Python 3.12 ou plus récent. Le plus simple est de passer par conda, qui crée un environnement isolé. Les commandes se lancent **depuis la racine du dépôt**, car l'installation désigne le dossier courant :

```bash
conda env create -f environment.yml   # crée l'environnement « rd-manuf » : Python 3.12, le projet et ses outils de test
conda activate rd-manuf               # l'active (à refaire dans chaque nouveau terminal)
python -m pytest                      # lance les tests
ruff check .                          # vérifie le style du code
```

Après un changement de dépendances dans `pyproject.toml`, on met l'environnement à jour avec `conda env update -f environment.yml --prune`.
Sans conda, `pip install -e ".[dev]"` installe la même chose dans l'environnement Python de son choix.

La liste des dépendances reste dans `pyproject.toml` : `environment.yml` ne fournit que Python et lance la même installation que la CI.
Un test vérifie que la version de Python y est la même que dans le projet et dans la CI.
La CI lance ces vérifications (style, puis tests) à chaque Pull Request, sur un environnement neuf.

## Databricks : le bundle

Le fichier [`databricks.yml`](databricks.yml) décrit **où** le projet se déploie : trois cibles, `playground` (par défaut), `dev` et `prod`, chacune reliée à son catalogue de données. C'est la mise en œuvre du principe du dépôt : le même code partout, l'environnement choisi par le catalogue. Les noms de catalogues ne sont connus que de `src/rd_manuf/config.py`, et un test garde les deux d'accord.

Le fichier ne contient **ni adresse de workspace ni secret** : le dépôt est public, la CLI utilise le profil local de la personne qui lance la commande.
Rien n'est encore déployé : on valide seulement le bundle. Les commandes se lancent **depuis la racine du dépôt**, là où se trouve `databricks.yml`.

```bash
databricks auth login --host ADRESSE_DU_WORKSPACE   # une fois : ouvre un navigateur et enregistre un profil local
databricks bundle validate                          # vérifie le bundle, cible par défaut (playground), sans rien déployer
databricks bundle validate -t dev                   # idem pour une autre cible (dev, prod)
```

Remplacer `ADRESSE_DU_WORKSPACE` par l'adresse du workspace (de la forme `https://…`, sans rien après le nom de domaine). Il faut la CLI Databricks en version 0.218 ou plus récente.

## Databricks : la première exécution (lecture seule)

Le programme [`sondage.py`](src/rd_manuf/sondage.py) ne crée ni n'écrit rien : il n'envoie que des requêtes de lecture (`SELECT`). Il vérifie que les valeurs rendues par un entrepôt SQL ont bien les types attendus par le code, puis dit si nos trois schémas et nos cinq tables existent déjà dans le catalogue playground. « Absent » veut dire absent pour votre profil, qui ne voit que ce sur quoi il a des droits, et une vue de même nom compte comme présente.
Il faut le SDK Databricks (`pip install -e ".[databricks]"`), un profil de connexion et l'identifiant d'un entrepôt SQL : le dépôt est public, ni l'un ni l'autre n'y est écrit, ils se donnent à chaque lancement. Depuis la racine du dépôt :

```bash
python -m rd_manuf.sondage --profil NOM_DU_PROFIL --entrepot IDENTIFIANT_DE_L_ENTREPOT
```

Le code de sortie est 0 si tout est conforme, 1 si un essai ou la vérification de présence a échoué, 2 si le lancement est refusé (tout catalogue autre que le playground est refusé avant toute connexion) ou si la connexion échoue. Le résultat s'affiche dans le terminal seulement : le relire avant de le coller dans une PR, il peut citer des noms propres à votre espace de travail.

## Travailler sur le projet

Le dépôt suit un processus d'équipe, même quand on y travaille seul :

1. une branche par ticket, créée depuis `main` à jour ;
2. le test d'abord, que l'on voit échouer avant d'écrire le code ;
3. des messages de commit au format *Conventional Commits* (`feat(lecture): ...`) ;
4. une Pull Request décrite avec le [modèle](.github/pull_request_template.md), dont la CI doit être verte ;
5. une revue avant de fusionner ;
6. une fusion en « squash » : le message qui reste dans l'historique de `main` est un texte brut en quatre parties (EN CLAIR, POURQUOI, CE QUI CHANGE, DÉCISIONS), compréhensible sans ouvrir la Pull Request ;
7. une entrée dans le [journal des changements](CHANGELOG.md), ajoutée par chaque Pull Request.

### Mettre à jour le plan global

L'image du plan est générée. Pour changer l'avancement, on modifie les statuts dans [`docs/generer_plan_global.py`](docs/generer_plan_global.py),
on lance `python docs/generer_plan_global.py`, puis on commite l'image régénérée. Un test vérifie que l'image et son générateur ne divergent pas.
Les sections ci-dessus se mettent à jour à la main, dans la même Pull Request.
