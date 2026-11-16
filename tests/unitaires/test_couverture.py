"""La couverture de test doit être mesurée par la CI, exiger TOUT le code, et ne pas s'éroder sans bruit.

Un seuil que personne ne garde se baisse « juste pour cette fois », et une exclusion (`# pragma: no cover`, `omit`, `exclude_lines`)
fait passer une ligne non testée sans que personne la voie. Ces tests gardent ce qui fait tenir la mesure : la CI, la configuration, les
outils installés, le fichier ignoré par Git, et l'absence d'exclusion cachée dans le code.
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
WORKFLOW = RACINE / ".github" / "workflows" / "ci.yml"
GITIGNORE = RACINE / ".gitignore"
SRC = RACINE / "src" / "rd_manuf"
FICHIERS_DE_CONFIGURATION_CONCURRENTS = [".coveragerc", ".coveragerc.toml", "setup.cfg", "tox.ini"]     # lus par `coverage` avant ou à côté
REGLAGES_DE_MESURE = {"source", "branch"}                    # ce que `[tool.coverage.run]` peut contenir : un autre exclurait du code
REGLAGES_DU_RAPPORT = {"fail_under", "show_missing", "skip_covered"}      # idem pour `[tool.coverage.report]` (ni `omit`, ni `exclude_*`)


def _pyproject():
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def _job_des_tests():
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["unitaires"]


def _etapes_pytest():
    return [etape for etape in _job_des_tests()["steps"] if str(etape.get("run", "")).split()[:1] == ["pytest"]]


# ==============================================================================
# 🚦 LA CI MESURE LA COUVERTURE, dans la même exécution que les tests, et ne peut pas la laisser tomber en silence
# ==============================================================================
def test_la_ci_mesure_la_couverture_dans_l_execution_des_tests():
    """Une seule exécution de `pytest` : une seconde, séparée, pourrait mesurer autre chose que ce que les tests ont réellement joué."""
    etapes = _etapes_pytest()

    assert len(etapes) == 1
    assert "--cov" in etapes[0]["run"].split()


def test_la_ci_ne_change_pas_le_seuil_en_ligne_de_commande():
    """Le seuil vit dans `pyproject.toml`, où un test le garde : `--cov-fail-under=0` dans la CI le baisserait sans bruit."""
    (etape,) = _etapes_pytest()
    mots = etape["run"].split()

    assert not any(mot.startswith(("--cov-fail-under", "--no-cov", "--cov-config", "--cov-report=term:skip")) for mot in mots)


def test_l_echec_de_la_couverture_fait_echouer_la_ci_et_l_etape_ne_peut_pas_etre_sautee():
    job = _job_des_tests()
    (etape,) = _etapes_pytest()

    assert not etape.get("continue-on-error", False)
    assert "if" not in etape
    assert not job.get("continue-on-error", False)
    assert "if" not in job


# ==============================================================================
# 📦 L'OUTIL EST INSTALLÉ, LE FICHIER DE MESURE EST IGNORÉ
# ==============================================================================
def test_pytest_cov_est_installe_avec_les_outils_de_developpement():
    """La CI et `environment.yml` installent `.[dev]` : sans `pytest-cov`, `pytest --cov` y échouerait."""
    noms = {depend.split(">")[0].split("=")[0].split("[")[0] for depend in _pyproject()["project"]["optional-dependencies"]["dev"]}

    assert "pytest-cov" in noms


def test_le_fichier_de_mesure_de_la_couverture_est_ignore_par_git():
    """Il est créé à la racine à chaque `pytest --cov` et, contrairement au cache de pytest (qui écrit son propre `.gitignore`),
    ne s'ignore pas tout seul."""
    lignes = {ligne.strip() for ligne in GITIGNORE.read_text(encoding="utf-8").splitlines()}

    assert ".coverage" in lignes


# ==============================================================================
# 🔒 LA MESURE EXIGE TOUT LE CODE, SANS EXCLUSION
# ==============================================================================
def test_la_couverture_exige_tout_le_code_en_lignes_et_en_branches():
    configuration = _pyproject()["tool"].get("coverage", {})

    assert configuration.get("run", {}).get("branch") is True
    assert configuration.get("run", {}).get("source") == ["rd_manuf"]
    assert configuration.get("report", {}).get("fail_under") == 100


def test_aucun_autre_fichier_ne_configure_la_couverture():
    """`coverage` lit `.coveragerc` AVANT `pyproject.toml` : mesuré, un `[report] fail_under = 0` dans ce fichier annulait le seuil et la
    configuration de `pyproject.toml`, que ces tests lisent, restait intacte. Il n'y a qu'un endroit où la configuration vit."""
    presents = [nom for nom in FICHIERS_DE_CONFIGURATION_CONCURRENTS if (RACINE / nom).exists()]

    assert presents == []


def test_aucun_reglage_ne_retire_du_code_de_la_mesure():
    """Un `omit`, un `include` ou un `exclude_lines` ferait sortir du code de la mesure : à décider ici, en le voyant."""
    configuration = _pyproject()["tool"].get("coverage", {})

    assert configuration, "la configuration de la couverture est absente de pyproject.toml"
    assert set(configuration.get("run", {})) <= REGLAGES_DE_MESURE
    assert set(configuration.get("report", {})) <= REGLAGES_DU_RAPPORT
    assert set(configuration) <= {"run", "report"}


# ==============================================================================
# 🔇 AUCUN `# pragma: no cover` DANS LE CODE, SAUF DÉCISION ÉCRITE ICI
# ==============================================================================
# C'est le moyen le plus court de faire passer une ligne non testée : le seuil de 100 % ne vaut que si ce commentaire ne s'écrit pas
# sans que ce soit un choix vu. En cas de besoin réel : l'écrire ICI avec le chemin du module et le nombre, et pourquoi en commentaire.
PRAGMAS_DECIDES: dict[str, int] = {}
# Le motif par défaut de coverage.py (mesuré avec la version 7.17.3) : `nocover` et `no coverage` en font partie, `Pragma`, `no-cover`
# et `pragma = no cover` n'en font pas partie. Un garde qui ne reconnaît pas exactement ce que l'outil exclut se contourne.
PRAGMA_SANS_COUVERTURE = re.compile(r"#\s*(pragma|PRAGMA)[:\s]?\s*(no|NO)\s*(cover|COVER)")


def _pragmas_sans_couverture(source):
    """Les numéros de ligne des commentaires `# pragma: no cover`. Le code est lu par ses jetons : un texte ou une docstring qui en parle
    n'est pas un commentaire."""
    lignes = []
    for jeton in tokenize.generate_tokens(io.StringIO(source).readline):
        if jeton.type == tokenize.COMMENT and PRAGMA_SANS_COUVERTURE.search(jeton.string):
            lignes.append(jeton.start[0])
    return lignes


def _ecarts_avec_la_liste_decidee(trouves, decides):
    """Ce qui est trouvé et pas décidé, et ce qui est décidé et n'est plus (ou plus autant) dans le code."""
    ecarts = []
    for module, lignes in sorted(trouves.items()):
        if lignes and len(lignes) != decides.get(module, 0):
            ecarts.append(f"{module} : {len(lignes)} commentaire(s) aux lignes {lignes}, {decides.get(module, 0)} décidé(s)")
    for module, nombre in sorted(decides.items()):
        if nombre and not trouves.get(module):
            ecarts.append(f"{module} : {nombre} décidé(s) mais aucun dans le code (retirer la ligne de la liste)")
    return ecarts


def _ecarts_du_dossier(dossier, decides):
    """Le nombre de modules regardés, et les écarts. Les modules sont désignés par leur CHEMIN dans `dossier` : deux modules de même nom
    (l'`__init__.py` d'un sous-paquet) ne s'écrasent pas."""
    modules = sorted(dossier.rglob("*.py"))
    trouves = {module.relative_to(dossier).as_posix(): _pragmas_sans_couverture(module.read_text(encoding="utf-8")) for module in modules}
    return len(modules), _ecarts_avec_la_liste_decidee(trouves, decides)


def test_aucun_pragma_no_cover_dans_le_code_en_dehors_de_la_liste_decidee():
    nombre, ecarts = _ecarts_du_dossier(SRC, PRAGMAS_DECIDES)

    assert nombre > 10, "le garde ne regarde presque rien : le chemin du code a changé"
    assert ecarts == []


def test_le_garde_lit_les_fichiers_d_un_dossier_et_nomme_le_chemin_du_module_fautif(tmp_path):
    (tmp_path / "propre.py").write_text("x = 1  # noqa: E501\n", encoding="utf-8")
    (tmp_path / "sous").mkdir()
    (tmp_path / "sous" / "fautif.py").write_text("a = 1\nif a:  # pragma: no cover\n    pass\n", encoding="utf-8")

    nombre, ecarts = _ecarts_du_dossier(tmp_path, {})

    assert nombre == 2
    assert len(ecarts) == 1 and "sous/fautif.py" in ecarts[0] and "[2]" in ecarts[0] and "propre.py" not in ecarts[0]
    assert _ecarts_du_dossier(tmp_path, {"sous/fautif.py": 1})[1] == []


def test_deux_modules_de_meme_nom_ne_se_cachent_pas_l_un_l_autre(tmp_path):
    """Le cas d'un futur sous-paquet : deux `__init__.py`, le premier fautif, le second propre."""
    (tmp_path / "__init__.py").write_text("x = 1  # pragma: no cover\n", encoding="utf-8")
    (tmp_path / "sous").mkdir()
    (tmp_path / "sous" / "__init__.py").write_text("y = 2\n", encoding="utf-8")

    nombre, ecarts = _ecarts_du_dossier(tmp_path, {})

    assert nombre == 2
    assert len(ecarts) == 1 and ecarts[0].startswith("__init__.py :")


@pytest.mark.parametrize(
    "source",
    [
        "if x:  # pragma: no cover\n    pass\n",
        "if x:  # pragma:no cover\n    pass\n",
        "if x:  #   pragma:   no   cover\n    pass\n",
        "if x:  # noqa: E501  # pragma: no cover\n    pass\n",
        "def f():  # pragma: no cover\n    pass\n",
        "# pragma: no cover\nx = 1\n",
        "if x:  # pragma no cover\n    pass\n",
        "if x:  # pragma: nocover\n    pass\n",
        "if x:  # pragma: no coverage\n    pass\n",
        "if x:  # PRAGMA: NO COVER\n    pass\n",
    ],
    ids=["pragma", "sans espace", "espaces en trop", "après un autre commentaire", "sur une fonction", "ligne seule", "sans deux-points",
         "nocover", "no coverage", "majuscules"],
)
def test_le_garde_trouve_un_pragma_no_cover(source):
    assert _pragmas_sans_couverture(source) != []


@pytest.mark.parametrize(
    "source",
    [
        'x = "# pragma: no cover"\n',
        '"""Ne jamais écrire # pragma: no cover."""\nx = 1\n',
        "# aucun pragma ici, pas de no cover\nx = 1\n",
        "x = 1  # noqa: E501\n",
        "x = 1  # Pragma: no cover\n",                      # mesuré : coverage ne l'exclut pas (casse mixte)
        "x = 1  # pragma: No Cover\n",
        "x = 1  # pragma: no-cover\n",
        "x = 1  # pragma = no cover\n",
    ],
    ids=["texte", "docstring", "phrase de commentaire", "noqa", "casse mixte", "casse mixte 2", "avec un tiret", "avec un égal"],
)
def test_le_garde_ne_prend_pas_pour_un_pragma_ce_qui_n_en_est_pas_un(source):
    assert _pragmas_sans_couverture(source) == []


def test_le_garde_donne_la_ligne_de_chaque_pragma():
    source = "a = 1\nif a:  # pragma: no cover\n    pass\nb = 2  # pragma: no cover\n"

    assert _pragmas_sans_couverture(source) == [2, 4]


@pytest.mark.parametrize(
    ("trouves", "decides", "attendu"),
    [
        ({"a.py": []}, {}, 0),
        ({"a.py": [3]}, {}, 1),
        ({"a.py": [3]}, {"a.py": 1}, 0),
        ({"a.py": [3, 9]}, {"a.py": 1}, 1),
        ({"a.py": [3]}, {"a.py": 2}, 1),
        ({"a.py": []}, {"a.py": 1}, 1),
        ({"a.py": [3], "b.py": [4]}, {"a.py": 1}, 1),
    ],
    ids=["rien", "non décidé", "décidé", "plus que décidé", "moins que décidé", "décidé mais disparu", "un décidé, un non"],
)
def test_la_liste_decidee_est_comparee_dans_les_deux_sens(trouves, decides, attendu):
    assert len(_ecarts_avec_la_liste_decidee(trouves, decides)) == attendu
