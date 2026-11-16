"""Tests de la lecture YAML qui refuse une clé répétée (PyYAML garde la DERNIÈRE valeur sans un mot)."""
import ast
from pathlib import Path

import pytest
import yaml

from rd_manuf.lecture_yaml import lire_yaml_sans_cle_repetee

RACINE = Path(__file__).resolve().parents[2]
SRC = RACINE / "src" / "rd_manuf"
FICHIERS_DU_DEPOT = [
    RACINE / "contracts" / "data" / "livraison_production.yaml",
    RACINE / "contracts" / "alertes" / "catalogue.yaml",
    RACINE / "contracts" / "surveillance" / "sources_attendues.yaml",
]


def _ecrire(tmp_path, texte):
    chemin = tmp_path / "fichier.yaml"
    chemin.write_text(texte, encoding="utf-8")
    return chemin


@pytest.mark.parametrize(
    ("texte", "cle"),
    [
        ("a: 1\nb: 2\na: 3\n", "a"),
        ("a: 1\nb: 2\na: 1\n", "a"),                                     # même valeur : une copie-colle, refusée aussi
        ("haut:\n  a: 1\n  a: 2\n", "a"),                                # imbriquée
        ("liste:\n  - x: 1\n    y: 2\n  - x: 1\n    x: 2\n", "x"),       # dans le second élément d'une liste
        ("a: 1\n'a': 2\n", "a"),                                         # même clé écrite avec des guillemets
        ("1: un\n01: deux\n", "1"),                                      # deux écritures d'un même entier
    ],
    ids=["premier niveau", "même valeur", "imbriquée", "dans une liste", "avec guillemets", "même entier"],
)
def test_une_cle_repetee_est_refusee_avec_son_nom_et_sa_ligne(tmp_path, texte, cle):
    with pytest.raises(yaml.YAMLError, match=f"clé « {cle} » est répétée") as erreur:
        lire_yaml_sans_cle_repetee(_ecrire(tmp_path, texte))

    assert "line " in str(erreur.value)                                 # où chercher dans le fichier, pas seulement le nom


def test_la_ligne_donnee_est_celle_de_la_seconde_occurrence(tmp_path):
    with pytest.raises(yaml.YAMLError) as erreur:
        lire_yaml_sans_cle_repetee(_ecrire(tmp_path, "a: 1\nb: 2\nc: 3\na: 4\n"))

    assert "line 4," in str(erreur.value)


def test_la_meme_cle_dans_deux_mappings_differents_n_est_pas_une_repetition(tmp_path):
    texte = "premier:\n  a: 1\nsecond:\n  a: 2\nliste:\n  - a: 1\n  - a: 2\n"

    assert lire_yaml_sans_cle_repetee(_ecrire(tmp_path, texte)) == {
        "premier": {"a": 1}, "second": {"a": 2}, "liste": [{"a": 1}, {"a": 2}],
    }


def test_une_fusion_yaml_n_est_pas_prise_pour_une_cle_repetee(tmp_path):
    """`<<: *base` reprend les champs d'un mapping puis en remplace certains : c'est du YAML légitime."""
    texte = "base: &base\n  a: 1\n  b: 2\nautre:\n  <<: *base\n  b: 3\n"

    assert lire_yaml_sans_cle_repetee(_ecrire(tmp_path, texte))["autre"] == {"a": 1, "b": 3}


def test_une_cle_qui_n_est_pas_un_texte_simple_est_refusee_sans_planter(tmp_path):
    """Une clé liste est impossible à comparer comme les autres : le refus reste une erreur YAML, pas un TypeError."""
    with pytest.raises(yaml.YAMLError):
        lire_yaml_sans_cle_repetee(_ecrire(tmp_path, "? [a, b]\n: 1\n"))


def test_une_construction_python_est_refusee_comme_avec_safe_load(tmp_path):
    """Le lecteur strict reste un lecteur SÛR : `!!python/...` demanderait d'exécuter du code (ici un appel sans effet, `os.getcwd`)."""
    with pytest.raises(yaml.YAMLError):
        lire_yaml_sans_cle_repetee(_ecrire(tmp_path, "a: !!python/object/apply:os.getcwd []\n"))


def test_un_yaml_illisible_reste_une_erreur_yaml(tmp_path):
    with pytest.raises(yaml.YAMLError):
        lire_yaml_sans_cle_repetee(_ecrire(tmp_path, "a: [pas fermé\n  b: : :"))


def test_un_fichier_vide_donne_rien(tmp_path):
    assert lire_yaml_sans_cle_repetee(_ecrire(tmp_path, "")) is None


@pytest.mark.parametrize("chemin", FICHIERS_DU_DEPOT, ids=lambda chemin: chemin.name)
def test_les_fichiers_du_depot_sont_lus_comme_avant(chemin):
    """Le refus des clés répétées ne doit rien changer pour un fichier sans répétition."""
    assert lire_yaml_sans_cle_repetee(chemin) == yaml.safe_load(chemin.read_text(encoding="utf-8"))


# ==============================================================================
# 🛡️ GARDE : aucun module ne lit du YAML en dehors de `lecture_yaml.py`
# ==============================================================================
LECTURES_YAML = {
    "safe_load", "safe_load_all", "load", "load_all", "full_load", "full_load_all", "unsafe_load", "unsafe_load_all",
}


def _lectures_yaml(source):
    """Les numéros de ligne où le code lit du YAML par la bibliothèque, au lieu de passer par le lecteur qui refuse les clés répétées.

    Le code est lu par son arbre : un commentaire ou un texte qui cite `yaml.safe_load` n'est pas un appel."""
    arbre = ast.parse(source)
    noms = {"yaml"}                                                      # les noms du module, `import yaml as y` compris
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.Import):
            noms |= {nom.asname for nom in noeud.names if nom.name == "yaml" and nom.asname}
    lignes = []
    for noeud in ast.walk(arbre):
        if isinstance(noeud, ast.ImportFrom) and noeud.module == "yaml":
            if any(nom.name in LECTURES_YAML | {"*"} for nom in noeud.names):
                lignes.append(noeud.lineno)
        elif isinstance(noeud, ast.Attribute) and noeud.attr in LECTURES_YAML:
            if isinstance(noeud.value, ast.Name) and noeud.value.id in noms:
                lignes.append(noeud.lineno)
    return sorted(lignes)


def _lectures_hors_du_lecteur(dossier):
    """Le nombre de modules regardés, et les lectures YAML (`chemin:ligne`) de tous ceux qui ne sont pas le lecteur lui-même.

    Les modules sont désignés par leur CHEMIN dans `dossier` : le lecteur est `lecture_yaml.py` à la racine, pas tout module de ce nom."""
    chemins = {module: module.relative_to(dossier).as_posix() for module in sorted(dossier.rglob("*.py"))}
    modules = [module for module, chemin in chemins.items() if chemin != "lecture_yaml.py"]
    fautifs = [f"{chemins[module]}:{ligne}" for module in modules for ligne in _lectures_yaml(module.read_text(encoding="utf-8"))]
    return len(modules), fautifs


def test_aucun_module_du_code_ne_lit_du_yaml_en_dehors_de_lecture_yaml():
    """Un nouveau fichier YAML lu par `yaml.safe_load` retrouverait le défaut de cette PR : la dernière clé répétée gagnerait en silence."""
    nombre, fautifs = _lectures_hors_du_lecteur(SRC)

    assert nombre > 10, "le garde ne regarde presque rien : le chemin du code a changé"
    assert fautifs == [], f"lecture YAML hors de `lecture_yaml.py` (utiliser `lire_yaml_sans_cle_repetee`) : {fautifs}"


LECTURE = "import yaml\nbrut = yaml.safe_load(texte)\n"


def test_seul_le_lecteur_a_la_racine_du_code_est_exempte(tmp_path):
    """L'exemption est celle du CHEMIN `lecture_yaml.py`, pas du nom : un `sous/lecture_yaml.py` serait un autre module, à garder."""
    (tmp_path / "lecture_yaml.py").write_text(LECTURE, encoding="utf-8")
    (tmp_path / "autre.py").write_text("x = 1\n", encoding="utf-8")
    assert _lectures_hors_du_lecteur(tmp_path) == (1, [])

    (tmp_path / "sous").mkdir()
    (tmp_path / "sous" / "lecture_yaml.py").write_text(LECTURE, encoding="utf-8")

    nombre, fautifs = _lectures_hors_du_lecteur(tmp_path)

    assert nombre == 2
    assert fautifs == ["sous/lecture_yaml.py:2"]


def test_deux_modules_de_meme_nom_sont_distingues_dans_le_message(tmp_path):
    (tmp_path / "__init__.py").write_text(LECTURE, encoding="utf-8")
    (tmp_path / "sous").mkdir()
    (tmp_path / "sous" / "__init__.py").write_text(LECTURE, encoding="utf-8")

    assert _lectures_hors_du_lecteur(tmp_path)[1] == ["__init__.py:2", "sous/__init__.py:2"]


@pytest.mark.parametrize(
    "source",
    [
        "import yaml\nbrut = yaml.safe_load(texte)\n",
        "import yaml\nbrut = yaml.load(texte, Loader=yaml.SafeLoader)\n",
        "import yaml\nbrut = yaml.full_load(texte)\n",
        "import yaml\nbrut = yaml.unsafe_load(texte)\n",
        "import yaml\nbrut = list(yaml.safe_load_all(texte))\n",
        "import yaml as y\nbrut = y.safe_load(texte)\n",
        "from yaml import safe_load\n",
        "from yaml import load as lire\n",
        "from yaml import *\n",
        "import yaml\n\n\ndef f(texte):\n    return yaml.safe_load(texte)\n",
    ],
    ids=[
        "safe_load", "load", "full_load", "unsafe_load", "safe_load_all", "alias", "import nommé", "import renommé", "import étoile",
        "dans une fonction",
    ],
)
def test_le_garde_trouve_une_lecture_yaml(source):
    assert _lectures_yaml(source) != []


@pytest.mark.parametrize(
    "source",
    [
        "import yaml\ntry:\n    pass\nexcept yaml.YAMLError:\n    pass\n",
        "import yaml\nclass Lecteur(yaml.SafeLoader):\n    pass\n",
        "import yaml\nsortie = yaml.safe_dump({})\n",
        "# yaml.safe_load(texte) est interdit ici\nx = 1\n",
        '"""Ne pas appeler yaml.safe_load."""\nx = 1\n',
        "x = \"yaml.safe_load(texte)\"\n",
        "from yaml import YAMLError\n",
        "import json\nbrut = json.load(fichier)\n",
        "import yamlish\nbrut = yamlish.load(texte)\n",
    ],
    ids=[
        "exception", "classe de lecteur", "écriture", "commentaire", "docstring", "texte", "import d'une exception", "json",
        "autre module",
    ],
)
def test_le_garde_ne_prend_pas_pour_une_lecture_ce_qui_n_en_est_pas_une(source):
    assert _lectures_yaml(source) == []


def test_le_garde_donne_la_ligne_de_chaque_lecture():
    source = "import yaml\n\na = yaml.safe_load(x)\n\nb = yaml.load(y, Loader=L)\n"

    assert _lectures_yaml(source) == [3, 5]


def test_le_garde_donne_la_ligne_d_un_import_de_lecture():
    source = "import yaml\n\nfrom yaml import safe_load\n"

    assert _lectures_yaml(source) == [3]
