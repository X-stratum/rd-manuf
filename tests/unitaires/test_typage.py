"""Le contrôle des types (mypy) doit être lancé par la CI, installé avec les outils de développement, et rester strict.

Un contrôle qui ne tourne pas en CI se périme sans bruit : le code dérive, les erreurs s'accumulent, et le jour où quelqu'un le relance
il y en a des dizaines. Ces tests gardent les quatre endroits qui le font tenir : la CI, `pyproject.toml` (la configuration et les
outils installés) et la version de Python, qui doit être celle que la CI teste.
"""
import io
import re
import tokenize
import tomllib
from pathlib import Path

import pytest
import yaml

RACINE = Path(__file__).resolve().parents[2]
PYPROJECT = RACINE / "pyproject.toml"
SRC = RACINE / "src" / "rd_manuf"
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


# ==============================================================================
# 🔇 AUCUN COMMENTAIRE NE FAIT TAIRE MYPY EN SILENCE
# ==============================================================================
# `# type: ignore` fait taire une erreur sur une ligne, `# mypy: ignore-errors` (ou `disable-error-code`) tout un module : la
# configuration est gardée plus haut, ces commentaires la contourneraient sans qu'aucun test le voie. Il n'y en a aucun aujourd'hui.
# En cas de besoin réel : l'écrire ICI, avec le nom du module, le nombre, et pourquoi (en commentaire), pour que ce soit un choix vu.
IGNORES_DECIDES: dict[str, int] = {}
ASSOUPLISSEMENT_EN_LIGNE = re.compile(r"#\s*(type:\s*ignore\b|mypy:)")


def _assouplissements_en_ligne(source):
    """Les numéros de ligne des commentaires qui font taire mypy. Le code est lu par ses jetons : un texte ou une docstring qui cite
    `# type: ignore` n'est pas un commentaire, et les commentaires ne sont pas dans l'arbre syntaxique."""
    lignes = []
    for jeton in tokenize.generate_tokens(io.StringIO(source).readline):
        if jeton.type == tokenize.COMMENT and ASSOUPLISSEMENT_EN_LIGNE.search(jeton.string):
            lignes.append(jeton.start[0])
    return lignes


def _ecarts_avec_la_liste_decidee(trouves, decides):
    """Ce qui est trouvé dans le code et n'est pas décidé, et ce qui est décidé et n'est plus (ou plus autant) dans le code."""
    ecarts = []
    for module, lignes in sorted(trouves.items()):
        if lignes and len(lignes) != decides.get(module, 0):
            ecarts.append(f"{module} : {len(lignes)} commentaire(s) aux lignes {lignes}, {decides.get(module, 0)} décidé(s)")
    for module, nombre in sorted(decides.items()):
        if nombre and not trouves.get(module):
            ecarts.append(f"{module} : {nombre} décidé(s) mais aucun dans le code (retirer la ligne de la liste)")
    return ecarts


def _ecarts_du_dossier(dossier, decides):
    """Le nombre de modules regardés, et les écarts entre ce que les modules de `dossier` contiennent et ce qui est décidé."""
    modules = sorted(dossier.rglob("*.py"))
    trouves = {module.name: _assouplissements_en_ligne(module.read_text(encoding="utf-8")) for module in modules}
    return len(modules), _ecarts_avec_la_liste_decidee(trouves, decides)


def test_aucun_commentaire_ne_fait_taire_mypy_en_dehors_de_la_liste_decidee():
    nombre, ecarts = _ecarts_du_dossier(SRC, IGNORES_DECIDES)

    assert nombre > 10, "le garde ne regarde presque rien : le chemin du code a changé"
    assert ecarts == []


def test_le_garde_lit_les_fichiers_d_un_dossier_et_nomme_le_module_fautif(tmp_path):
    (tmp_path / "propre.py").write_text("x = 1  # noqa: E501\n", encoding="utf-8")
    (tmp_path / "sous_dossier").mkdir()
    (tmp_path / "sous_dossier" / "fautif.py").write_text("a = 1\nb: int = 'x'  # type: ignore[assignment]\n", encoding="utf-8")

    nombre, ecarts = _ecarts_du_dossier(tmp_path, {})

    assert nombre == 2                                                      # le sous-dossier est parcouru aussi
    assert len(ecarts) == 1 and "fautif.py" in ecarts[0] and "[2]" in ecarts[0] and "propre.py" not in ecarts[0]
    assert _ecarts_du_dossier(tmp_path, {"fautif.py": 1})[1] == []          # une fois décidé, au nombre près, il n'y a plus d'écart


@pytest.mark.parametrize(
    "source",
    [
        "x: int = 'a'  # type: ignore\n",
        "x: int = 'a'  # type: ignore[assignment]\n",
        "x: int = 'a'  # type:ignore\n",
        "x: int = 'a'  #   type:   ignore[assignment, misc]\n",
        "x: int = 'a'  # noqa: E501  # type: ignore[assignment]\n",
        "# type: ignore\nx = 1\n",
        "# mypy: ignore-errors\nx = 1\n",
        "# mypy: disable-error-code=\"arg-type\"\nx = 1\n",
        "def f() -> int:\n    return 'a'  # type: ignore[return-value]\n",
    ],
    ids=["ignore", "avec code", "sans espace", "espaces en trop", "après un autre commentaire", "ligne seule", "mypy ignore-errors",
         "mypy disable-error-code", "dans une fonction"],
)
def test_le_garde_trouve_un_commentaire_qui_fait_taire_mypy(source):
    assert _assouplissements_en_ligne(source) != []


@pytest.mark.parametrize(
    "source",
    [
        'x = "# type: ignore"\n',
        "x = '# mypy: ignore-errors'\n",
        '"""Ne jamais écrire # type: ignore ici."""\nx = 1\n',
        'def f() -> str:\n    """Pas de # mypy: ignore-errors."""\n    return "a"\n',
        "# aucun type: ignore ici, ni mypy ou ignore\nx = 1\n",
        "# on n'écrit pas type ignore\nx = 1\n",
        "x = 1  # noqa: E501\n",
        "x = 1  # un type d'ignorance\n",
        "x: int = 'a'  # type: ignorer\n",               # mesuré : mypy ne les prend pas pour un ignore, l'erreur reste signalée
        "x: int = 'a'  # type: ignored\n",
        "x: int = 'a'  # type: ignore_all\n",
    ],
    ids=["texte", "texte mypy", "docstring de module", "docstring de fonction", "phrase de commentaire", "phrase sans deux-points",
         "noqa", "autre commentaire", "ignorer", "ignored", "ignore_all"],
)
def test_le_garde_ne_prend_pas_pour_un_ignore_ce_qui_n_en_est_pas_un(source):
    assert _assouplissements_en_ligne(source) == []


def test_le_garde_donne_la_ligne_de_chaque_commentaire():
    source = "a = 1\nb: int = 'x'  # type: ignore\nc = 2\n# mypy: ignore-errors\n"

    assert _assouplissements_en_ligne(source) == [2, 4]


@pytest.mark.parametrize(
    ("trouves", "decides", "attendu"),
    [
        ({"a.py": []}, {}, 0),                                   # rien trouvé, rien décidé
        ({"a.py": [3]}, {}, 1),                                  # trouvé, pas décidé
        ({"a.py": [3]}, {"a.py": 1}, 0),                         # trouvé et décidé, au nombre près
        ({"a.py": [3, 9]}, {"a.py": 1}, 1),                      # plus que décidé
        ({"a.py": [3]}, {"a.py": 2}, 1),                         # moins que décidé : la décision est périmée
        ({"a.py": []}, {"a.py": 1}, 1),                          # décidé, mais il n'y en a plus
        ({"a.py": [3], "b.py": [4]}, {"a.py": 1}, 1),            # un module décidé, un autre non
    ],
    ids=["rien", "non décidé", "décidé", "plus que décidé", "moins que décidé", "décidé mais disparu", "un décidé, un non"],
)
def test_la_liste_decidee_est_comparee_dans_les_deux_sens(trouves, decides, attendu):
    assert len(_ecarts_avec_la_liste_decidee(trouves, decides)) == attendu
