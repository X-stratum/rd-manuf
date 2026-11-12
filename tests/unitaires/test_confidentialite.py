"""Ce qui ne doit JAMAIS partir sur GitHub (le dépôt est public) doit rester dans le `.gitignore`.

La CLI Databricks crée un dossier `.databricks/` dans le dépôt (état local du bundle, qui peut contenir des informations sur le workspace) ;
les profils de connexion et les jetons vivent hors du dépôt, mais un fichier de secrets pourrait y être créé par erreur. Le `.gitignore` est
la barrière : ces tests empêchent qu'une ligne en soit retirée sans que personne ne le voie.
"""
from pathlib import Path

import pytest

GITIGNORE = Path(__file__).resolve().parents[2] / ".gitignore"

MOTIFS_PROTEGES = [
    pytest.param(".databricks/", id="etat-local-du-bundle-databricks"),
    pytest.param(".bundle/", id="dossier-de-travail-des-bundles"),
    pytest.param(".databrickscfg", id="profils-de-connexion-databricks"),
    pytest.param(".env", id="variables-d-environnement-locales"),
    pytest.param("*.token", id="fichiers-de-jetons"),
]


def _lignes_actives():
    """Les motifs réellement actifs : sans les commentaires ni les lignes vides."""
    lignes = (ligne.strip() for ligne in GITIGNORE.read_text(encoding="utf-8").splitlines())
    return {ligne for ligne in lignes if ligne and not ligne.startswith("#")}


@pytest.mark.parametrize("motif", MOTIFS_PROTEGES)
def test_ce_qui_ne_doit_pas_partir_sur_github_est_ignore(motif):
    assert motif in _lignes_actives(), f"« {motif} » doit rester dans .gitignore (dépôt public)"
