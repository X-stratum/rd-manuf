"""Le contrôle des types (mypy) doit être lancé par la CI, installé avec les outils de développement, et rester strict.

Un contrôle qui ne tourne pas en CI se périme sans bruit : le code dérive, les erreurs s'accumulent, et le jour où quelqu'un le relance
il y en a des dizaines. Ces tests gardent les quatre endroits qui le font tenir : la CI, `pyproject.toml` (la configuration et les
outils installés) et la version de Python, qui doit être celle que la CI teste.
"""
import tomllib
from pathlib import Path

import yaml

RACINE = Path(__file__).resolve().parents[2]
PYPROJECT = RACINE / "pyproject.toml"
WORKFLOW = RACINE / ".github" / "workflows" / "ci.yml"
OPTIONS_PERMISES = {"python_version", "files", "strict", "overrides"}      # tout autre réglage assouplirait le contrôle
MODULES_SANS_TYPES = {"pyarrow", "pyarrow.*"}                              # la seule bibliothèque qui ne publie pas ses types


def _pyproject():
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _workflow():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _commandes_du_job_statique():
    """Les commandes `run` du job « statique », dans l'ordre où la CI les lance."""
    return [etape["run"] for etape in _workflow()["jobs"]["statique"]["steps"] if "run" in etape]


# ==============================================================================
# 🚦 LA CI LANCE MYPY, après ruff (du moins cher au plus cher)
# ==============================================================================
def test_la_ci_lance_mypy_dans_le_job_statique():
    assert "mypy" in _commandes_du_job_statique()


def test_la_ci_lance_mypy_sans_chemin_pour_que_la_configuration_du_projet_decide_de_ce_qu_il_regarde():
    """`mypy src/un_seul_fichier.py` ne contrôlerait presque rien sans que personne le voie : le périmètre est `files` dans pyproject."""
    assert [commande for commande in _commandes_du_job_statique() if commande.split()[0] == "mypy"] == ["mypy"]


def test_l_echec_de_mypy_fait_echouer_la_ci_et_l_etape_ne_peut_pas_etre_sautee():
    """`continue-on-error` ou `if: false` laisseraient la ligne `mypy` dans la CI tout en la rendant sans effet : la CI resterait verte."""
    job = _workflow()["jobs"]["statique"]
    etape = next((etape for etape in job["steps"] if etape.get("run") == "mypy"), None)

    assert etape is not None
    assert not etape.get("continue-on-error", False)
    assert "if" not in etape
    assert not job.get("continue-on-error", False)
    assert "if" not in job


def test_la_ci_lance_mypy_apres_ruff():
    commandes = _commandes_du_job_statique()
    premiere_de_ruff = next(i for i, commande in enumerate(commandes) if commande.startswith("ruff"))

    assert "mypy" in commandes and commandes.index("mypy") > premiere_de_ruff


# ==============================================================================
# 📦 MYPY ET LES TYPES DE SES DÉPENDANCES SONT INSTALLÉS AVEC LES OUTILS DE DÉVELOPPEMENT
# ==============================================================================
def test_mypy_et_les_types_de_pyyaml_sont_installes_avec_les_outils_de_developpement():
    """La CI et `environment.yml` installent `.[dev]` : sans ces deux paquets, mypy y échouerait (ou ne verrait pas les types de PyYAML)."""
    noms = {depend.split(">")[0].split("=")[0].split("[")[0] for depend in _pyproject()["project"]["optional-dependencies"]["dev"]}

    assert {"mypy", "types-PyYAML"} <= noms


# ==============================================================================
# 🔒 LA CONFIGURATION RESTE STRICTE
# ==============================================================================
def test_mypy_est_strict_sur_le_code_et_seulement_sur_lui():
    reglages = _pyproject()["tool"]["mypy"]

    assert reglages["strict"] is True
    assert reglages["files"] == ["src"]


def test_aucun_reglage_n_assouplit_mypy_en_dehors_des_options_attendues():
    """Un `ignore_missing_imports` global ou un `disable_error_code` ferait taire des erreurs réelles : à décider ici, en le voyant."""
    assert set(_pyproject()["tool"]["mypy"]) <= OPTIONS_PERMISES


def test_la_seule_exception_aux_types_manquants_est_pyarrow():
    exceptions = _pyproject()["tool"]["mypy"]["overrides"]

    assert [set(exception["module"]) for exception in exceptions] == [MODULES_SANS_TYPES]
    assert all(set(exception) == {"module", "ignore_missing_imports"} for exception in exceptions)       # et rien d'autre dans l'exception


def test_la_version_de_python_de_mypy_est_celle_testee_par_la_ci():
    assert _pyproject()["tool"]["mypy"]["python_version"] == str(_workflow()["env"]["VERSION_PYTHON"])
