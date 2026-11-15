"""Tests de la lecture YAML qui refuse une clé répétée (PyYAML garde la DERNIÈRE valeur sans un mot)."""
from pathlib import Path

import pytest
import yaml

from rd_manuf.lecture_yaml import lire_yaml_sans_cle_repetee

RACINE = Path(__file__).resolve().parents[2]
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
