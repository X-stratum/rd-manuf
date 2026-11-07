# rd-manuf

Pipeline de données **R&D Manufacturing** sur Databricks (architecture médaillon bronze → silver → gold),
construit pas à pas avec un processus d'équipe : branches, Pull Requests, CI GitHub Actions et revue de code.

## État

🚧 Projet en construction. Ce dépôt ne contient pour l'instant que son squelette.

## Environnements (catalogues Unity Catalog)

| Environnement | Catalogue | Rôle |
|---|---|---|
| playground | `rd_manuf_playground` | Tests destructifs et scénarios libres |
| dev | `rd_manuf_dev` | Intégration sur les livraisons rejouées de la prod |
| prod | `rd_manuf_prod` | Production |

## Principe

Le dépôt contient le **code** ; les catalogues contiennent les **données**. Le même code tourne partout :
l'environnement se choisit par le paramètre `catalogue`, jamais par une copie de dossier.
