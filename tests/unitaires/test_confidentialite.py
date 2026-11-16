"""Ce qui ne doit JAMAIS partir sur GitHub (le dépôt est public) doit rester dans le `.gitignore`.

La CLI Databricks crée un dossier `.databricks/` dans le dépôt (état local du bundle, qui peut contenir des informations sur le workspace) ;
les profils de connexion et les jetons vivent hors du dépôt, mais un fichier de secrets pourrait y être créé par erreur. Le `.gitignore` est
la barrière : ces tests empêchent qu'une ligne en soit retirée, ou annulée par une ligne négative (`!motif`), sans que personne ne le voie.
"""
import re
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


@pytest.mark.parametrize("motif", MOTIFS_PROTEGES)
def test_aucune_ligne_n_annule_la_protection(motif):
    """Une ligne négative (`!.databricks/`) rétablirait le suivi du dossier alors que le motif est toujours présent plus haut."""
    base = motif.rstrip("/")
    formes_negatives = {f"!{motif}", f"!{base}", f"!{base}/**", f"!{base}/*"}
    assert not (formes_negatives & _lignes_actives()), f"une ligne négative annule la protection de « {motif} »"


# ==============================================================================
# Le CODE ne doit contenir ni adresse de workspace ni jeton (le dépôt est public)
# ==============================================================================
SOURCES = Path(__file__).resolve().parents[2] / "src"
MARQUEURS_D_ADRESSE = ("://", "azuredatabricks", "databricks.com", "cloud.databricks", "databricks.net")
JETON_PERSONNEL = re.compile(r"\bdapi[0-9a-f]{16,}\b")      # la forme d'un jeton d'accès personnel Databricks


def _fichiers_du_code(dossier=SOURCES):
    return sorted(dossier.rglob("*.py"))


def _fichiers_qui(dossier, condition):
    """Les fichiers du code qui remplissent `condition`, par leur chemin dans `dossier` (deux `__init__.py` ne se confondent pas)."""
    fichiers = _fichiers_du_code(dossier)
    return [fichier.relative_to(dossier).as_posix() for fichier in fichiers if condition(fichier.read_text(encoding="utf-8"))]


def test_il_y_a_du_code_a_garder():
    assert _fichiers_du_code(), "aucun fichier trouvé dans src : la garde ne garderait rien"


@pytest.mark.parametrize("marqueur", MARQUEURS_D_ADRESSE)
def test_aucun_fichier_du_code_ne_contient_d_adresse_de_workspace(marqueur):
    """L'adresse du workspace, le profil et l'entrepôt viennent de la personne qui lance, jamais d'un fichier suivi."""
    fautifs = _fichiers_qui(SOURCES, lambda texte: marqueur in texte)

    assert not fautifs, f"« {marqueur} » dans {fautifs} : aucune adresse ne doit être écrite dans le code"


def test_aucun_fichier_du_code_ne_contient_de_jeton_d_acces():
    fautifs = _fichiers_qui(SOURCES, lambda texte: JETON_PERSONNEL.search(texte))

    assert not fautifs, f"un jeton d'accès semble écrit dans {fautifs}"


def test_deux_fichiers_de_meme_nom_sont_distingues_dans_le_message(tmp_path):
    """Le message d'un garde doit dire LEQUEL des deux `__init__.py` est fautif."""
    (tmp_path / "__init__.py").write_text("url = 'https://exemple.databricks.com'\n", encoding="utf-8")
    (tmp_path / "sous").mkdir()
    (tmp_path / "sous" / "__init__.py").write_text("url = 'https://exemple.databricks.com'\n", encoding="utf-8")

    assert _fichiers_qui(tmp_path, lambda texte: "databricks.com" in texte) == ["__init__.py", "sous/__init__.py"]
