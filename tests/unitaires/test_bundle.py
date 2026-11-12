"""Le bundle Databricks (`databricks.yml`) doit rester d'accord avec le garde-fou d'environnement (`config.py`).

Le principe du projet : le même code tourne partout, l'environnement se choisit par le catalogue. `config.py` est le SEUL endroit
qui connaît les noms de catalogues ; le bundle ne fait que les relier aux cibles de déploiement. Ces tests empêchent une cible
d'être ajoutée ou modifiée sans que le garde-fou la reconnaisse, et empêchent qu'une adresse de workspace (donnée personnelle,
dépôt public) ou un secret soit écrit dans le fichier.
"""
from pathlib import Path

import pytest
import yaml

from rd_manuf.config import CATALOGUE_PAR_DEFAUT, CATALOGUE_PAR_ENVIRONNEMENT, ENV_PLAYGROUND, environnement_du_catalogue

BUNDLE = Path(__file__).resolve().parents[2] / "databricks.yml"


@pytest.fixture(scope="module")
def bundle():
    return yaml.safe_load(BUNDLE.read_text(encoding="utf-8"))


def test_les_cibles_du_bundle_sont_exactement_les_environnements_du_garde_fou(bundle):
    assert set(bundle["targets"]) == set(CATALOGUE_PAR_ENVIRONNEMENT)


@pytest.mark.parametrize("environnement", sorted(CATALOGUE_PAR_ENVIRONNEMENT))
def test_chaque_cible_vise_le_catalogue_que_le_garde_fou_lui_attribue(bundle, environnement):
    catalogue = bundle["targets"][environnement]["variables"]["catalogue"]
    assert catalogue == CATALOGUE_PAR_ENVIRONNEMENT[environnement]
    assert environnement_du_catalogue(catalogue) == environnement          # et le garde-fou reconnaît bien ce catalogue


def test_la_cible_par_defaut_est_le_playground_et_la_seule(bundle):
    par_defaut = [nom for nom, cible in bundle["targets"].items() if cible.get("default")]
    assert par_defaut == [ENV_PLAYGROUND]


def test_la_valeur_par_defaut_de_la_variable_est_celle_du_garde_fou(bundle):
    """Un déploiement lancé sans préciser de cible ne doit jamais toucher la prod."""
    assert bundle["variables"]["catalogue"]["default"] == CATALOGUE_PAR_DEFAUT


CLES_INTERDITES = {"host", "profile", "token", "client_secret", "password", "azure_client_secret"}
MARQUEURS_D_ADRESSE = ("://", "azuredatabricks", "databricks.com")


def _parcourir(noeud):
    """Rend toutes les clés et toutes les valeurs texte d'un document YAML, à tous les niveaux."""
    if isinstance(noeud, dict):
        for cle, valeur in noeud.items():
            yield "cle", str(cle)
            yield from _parcourir(valeur)
    elif isinstance(noeud, list):
        for element in noeud:
            yield from _parcourir(element)
    else:
        yield "valeur", str(noeud)


def test_le_fichier_ne_contient_ni_adresse_de_workspace_ni_secret(bundle):
    """Dépôt public : l'adresse du workspace vient du profil local de la CLI (`-p`), jamais du fichier.

    On contrôle les CLÉS et les VALEURS, pas les commentaires : un commentaire peut parler de secrets pour dire qu'il n'y en a pas.
    """
    for genre, texte in _parcourir(bundle):
        if genre == "cle":
            assert texte.lower() not in CLES_INTERDITES, f"clé interdite : {texte}"
        else:
            assert not any(marqueur in texte.lower() for marqueur in MARQUEURS_D_ADRESSE), f"adresse dans une valeur : {texte}"
