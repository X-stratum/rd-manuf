"""Configuration centrale : catalogues et GARDE-FOUS d'environnement.

Principe : le même code tourne partout ; l'environnement se choisit par le paramètre `catalogue`.
Ce module est le SEUL endroit qui connaît les noms de catalogues et les interdictions.
"""

# ==============================================================================
# 1. 🏷️ NOMS DE CATALOGUES (Unity Catalog)
# ==============================================================================
ENV_PLAYGROUND = "playground"
ENV_DEV = "dev"
ENV_PROD = "prod"

CATALOGUE_PAR_ENVIRONNEMENT = {
    ENV_PLAYGROUND: "rd_manuf_playground",
    ENV_DEV: "rd_manuf_dev",
    ENV_PROD: "rd_manuf_prod",
}

# Valeur par défaut volontairement SÛRE : un paramètre oublié ne touche jamais la prod.
CATALOGUE_PAR_DEFAUT = CATALOGUE_PAR_ENVIRONNEMENT[ENV_PLAYGROUND]


# ==============================================================================
# 2. 🔒 GARDE-FOUS
# ==============================================================================
class ErreurEnvironnement(RuntimeError):
    """Levée quand une action est demandée dans un environnement qui ne l'autorise pas."""


def environnement_du_catalogue(catalogue: str) -> str:
    """Retourne l'environnement (playground/dev/prod) d'un catalogue, ou refuse un nom inconnu.

    Refuser l'inconnu est essentiel : un catalogue mal orthographié (ou `space_1`) ne doit
    jamais être traité « par défaut » comme un environnement sûr.
    """
    for environnement, nom in CATALOGUE_PAR_ENVIRONNEMENT.items():
        if nom == catalogue:
            return environnement
    connus = ", ".join(sorted(CATALOGUE_PAR_ENVIRONNEMENT.values()))
    #return ENV_PLAYGROUND # commande pour tester que la fonction marche correctement / résultat rouge au test
    raise ErreurEnvironnement(f"Catalogue inconnu : « {catalogue} ». Catalogues autorisés : {connus}.")
