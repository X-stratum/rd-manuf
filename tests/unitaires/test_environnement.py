"""Le fichier `environment.yml` (environnement conda de travail) doit rester d'accord avec le projet et avec la CI.

Trois endroits parlent de la version de Python : `pyproject.toml` (le minimum exigé), la CI (la version testée) et `environment.yml`
(la version installée sur le poste). S'ils divergent, on développe sur une version que la CI ne teste pas, ou l'inverse.
"""
import tomllib
from pathlib import Path

import yaml

RACINE = Path(__file__).resolve().parents[2]
ENVIRONNEMENT = RACINE / "environment.yml"
PYPROJECT = RACINE / "pyproject.toml"
WORKFLOW = RACINE / ".github" / "workflows" / "ci.yml"
README = RACINE / "README.md"


def _environnement():
    return yaml.safe_load(ENVIRONNEMENT.read_text(encoding="utf-8"))


def _version_python_de_l_environnement():
    spec = next(d for d in _environnement()["dependencies"] if isinstance(d, str) and d.startswith("python="))
    return spec.removeprefix("python=")


def test_l_environnement_s_appelle_comme_le_projet():
    assert _environnement()["name"] == tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["name"]


def test_la_version_de_python_de_l_environnement_respecte_le_minimum_du_projet():
    minimum = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"]["requires-python"].removeprefix(">=")
    installee = _version_python_de_l_environnement()
    assert tuple(map(int, installee.split("."))) >= tuple(map(int, minimum.split(".")))


def test_la_version_de_python_de_l_environnement_est_celle_testee_par_la_ci():
    version_ci = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["env"]["VERSION_PYTHON"]
    assert _version_python_de_l_environnement() == str(version_ci)


def test_l_environnement_installe_le_projet_avec_ses_outils_de_developpement():
    """Même commande que la CI : `pip install -e ".[dev]"`, pour avoir exactement les mêmes dépendances."""
    pip = next(d["pip"] for d in _environnement()["dependencies"] if isinstance(d, dict))
    assert '-e ".[dev]"' in pip or "-e .[dev]" in pip


def test_le_readme_donne_les_commandes_de_controle_que_la_ci_lance():
    """Le README dit comment refaire en local les contrôles de la CI : `ruff check .` contre `ruff check src tests` ferait réussir en local
    ce que la CI ne regarde pas, ou l'inverse. Chaque commande `ruff` ou `mypy` du job « statique » doit y être écrite à l'identique."""
    etapes = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["statique"]["steps"]
    commandes = [etape["run"] for etape in etapes if etape.get("run", "").startswith(("ruff", "mypy"))]
    lignes_du_readme = {ligne.split("#")[0].strip() for ligne in README.read_text(encoding="utf-8").splitlines()}

    assert len(commandes) == 2
    assert all(commande in lignes_du_readme for commande in commandes)
