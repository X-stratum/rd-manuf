"""Tests de la CONVERSION des valeurs que le service Databricks rend, et de l'hygiène du module de l'exécuteur.

Le service d'exécution de requêtes rend chaque valeur en TEXTE (ou `None`), avec le type de sa colonne. `convertir_valeur` en refait les
types Python des schémas. Elle est stricte : un texte qu'elle ne reconnaît pas LÈVE, jamais de valeur devinée (un instant sans fuseau ne
devient pas « de l'UTC », un décimal infini n'entre pas). Les formats ici sont ceux que le code écrit ET ceux que la documentation annonce ;
la forme exacte des instants et des listes ne se prouvera que sur un vrai workspace (voir le journal des changements).

Aucune erreur ne contient la valeur reçue : ce peut être une donnée d'usine.
"""
import importlib
import os
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from rd_manuf.executeur_databricks import convertir_valeur

MODULE = Path(__file__).resolve().parents[2] / "src" / "rd_manuf" / "executeur_databricks.py"
TYPES_GERES = [
    pytest.param("STRING", "STRING", id="chaine"),
    pytest.param("LONG", "BIGINT", id="entier"),
    pytest.param("DOUBLE", "DOUBLE", id="decimal"),
    pytest.param("DATE", "DATE", id="date"),
    pytest.param("TIMESTAMP", "TIMESTAMP", id="instant"),
    pytest.param("ARRAY", "ARRAY<STRING>", id="liste"),
    pytest.param("MAP", "MAP<STRING, STRING>", id="table"),
    pytest.param("STRUCT", "STRUCT<a: STRING>", id="structure"),
]
VALEUR_SECRETE = "VALEUR_SECRETE"


def test_en_ci_le_sdk_est_installe():
    """Hors CI, les tests de l'exécuteur sont ignorés sans le SDK ; en CI ce serait un trou silencieux, donc il doit être là."""
    if os.environ.get("CI") != "true":
        pytest.skip("hors CI : le SDK est facultatif pour développer")
    importlib.import_module("databricks.sdk")


# ==============================================================================
# 1. 🔢 LES VALEURS : chaque type, ses formes valides et ses refus
# ==============================================================================
@pytest.mark.parametrize(("type_name", "type_text"), TYPES_GERES)
def test_une_valeur_absente_reste_absente_pour_chaque_type(type_name, type_text):
    assert convertir_valeur(None, type_name, type_text) is None


def test_une_chaine_reste_telle_quelle_meme_vide_ou_accentuee():
    assert convertir_valeur("", "STRING", "STRING") == ""
    assert convertir_valeur("éà 日本語 \U0001f600 'x' \"y\"", "STRING", "STRING") == "éà 日本語 \U0001f600 'x' \"y\""
    assert convertir_valeur("null", "STRING", "STRING") == "null"        # le texte « null » n'est pas l'absence


@pytest.mark.parametrize(("texte", "attendu"), [("42", 42), ("-7", -7), ("0", 0), ("1099511627776", 2**40)])
def test_un_entier_rendu_en_texte_redevient_un_entier(texte, attendu):
    valeur = convertir_valeur(texte, "LONG", "BIGINT")

    assert valeur == attendu and type(valeur) is int


@pytest.mark.parametrize("texte", ["4.2", "abc", "", "1_000", " 1", "1 ", "1e3", "--1", "+5", "٣", "0x10"])
def test_un_texte_qui_n_est_pas_un_entier_est_refuse(texte):
    with pytest.raises(ValueError):
        convertir_valeur(texte, "LONG", "BIGINT")


@pytest.mark.parametrize(
    ("texte", "attendu"), [("1.5", 1.5), ("3", 3.0), ("-0.25", -0.25), ("1e-3", 0.001), ("1.0E10", 1e10), ("0.0", 0.0)]
)
def test_un_decimal_rendu_en_texte_redevient_un_float(texte, attendu):
    valeur = convertir_valeur(texte, "DOUBLE", "DOUBLE")

    assert valeur == attendu and type(valeur) is float


@pytest.mark.parametrize(
    "texte", ["NaN", "nan", "Infinity", "-Infinity", "inf", "abc", "", "1,5", "1_0", " 1.5", "1.5 ", "٣.٥", "1e999", "-1e999"]
)
def test_un_texte_qui_n_est_pas_un_decimal_fini_est_refuse(texte):
    with pytest.raises(ValueError):
        convertir_valeur(texte, "DOUBLE", "DOUBLE")


def test_une_date_rendue_en_texte_redevient_une_date_et_non_un_instant():
    valeur = convertir_valeur("2026-11-14", "DATE", "DATE")

    assert valeur == date(2026, 11, 14) and not isinstance(valeur, datetime)


@pytest.mark.parametrize(
    "texte", ["20261114", "2026-W46-5", "2026-11-14T00:00:00Z", "14/11/2026", "", "2026-13-45", "2026-11-14 ", "٢٠٢٦-١١-١٤"]
)
def test_un_texte_qui_n_est_pas_une_date_est_refuse(texte):
    with pytest.raises(ValueError):
        convertir_valeur(texte, "DATE", "DATE")


@pytest.mark.parametrize(
    ("texte", "attendu"),
    [
        ("2026-11-14T10:00:00.000Z", datetime(2026, 11, 14, 10, 0, tzinfo=UTC)),
        ("2026-11-14T10:00:00Z", datetime(2026, 11, 14, 10, 0, tzinfo=UTC)),
        ("2026-11-14T10:00:00.123456Z", datetime(2026, 11, 14, 10, 0, 0, 123456, tzinfo=UTC)),
        ("2026-11-14T10:00:00+00:00", datetime(2026, 11, 14, 10, 0, tzinfo=UTC)),
        ("2026-11-14 10:00:00+00:00", datetime(2026, 11, 14, 10, 0, tzinfo=UTC)),
        ("2026-11-14T10:00:00+02:00", datetime(2026, 11, 14, 8, 0, tzinfo=UTC)),
        ("2026-11-14T10:00:00-05:30", datetime(2026, 11, 14, 15, 30, tzinfo=UTC)),
        ("0001-01-01T00:00:00.000000Z", datetime(1, 1, 1, tzinfo=UTC)),
    ],
)
def test_un_instant_rendu_en_texte_redevient_un_instant_en_utc(texte, attendu):
    valeur = convertir_valeur(texte, "TIMESTAMP", "TIMESTAMP")

    assert valeur == attendu and valeur.utcoffset() == timedelta(0)


@pytest.mark.parametrize(
    "texte",
    [
        "2026-11-14T10:00:00", "2026-11-14T10:00:00.000", "2026-11-14", "", "hier", "2026-11-14T25:00:00Z", "2026-11-14T10:00:00 UTC",
        "10:00:00Z",
    ],
)
def test_un_instant_sans_fuseau_ou_illisible_est_refuse_au_lieu_d_etre_pris_pour_de_l_utc(texte):
    with pytest.raises(ValueError):
        convertir_valeur(texte, "TIMESTAMP", "TIMESTAMP")


@pytest.mark.parametrize("texte", ["0001-01-01T00:00:00+02:00", "9999-12-31T23:59:59-02:00"])
def test_un_instant_qui_sort_des_bornes_une_fois_en_utc_est_refuse_par_une_value_error(texte):
    """`astimezone` lève `OverflowError` : la conversion ne laisse sortir que des `ValueError`."""
    with pytest.raises(ValueError):
        convertir_valeur(texte, "TIMESTAMP", "TIMESTAMP")


@pytest.mark.parametrize(
    ("texte", "attendu"),
    [
        ('["a","b"]', ["a", "b"]),
        ("[]", []),
        ('["a",null]', ["a", None]),
        ('["é","日本語"]', ["é", "日本語"]),
        ('[{"code":"X","colonnes":["c"],"message":"m"}]', [{"code": "X", "colonnes": ["c"], "message": "m"}]),
    ],
)
def test_une_liste_rendue_en_json_redevient_une_liste(texte, attendu):
    valeur = convertir_valeur(texte, "ARRAY", "ARRAY<STRING>")

    assert valeur == attendu and type(valeur) is list


@pytest.mark.parametrize(("type_name", "type_text"), [("MAP", "MAP<STRING, STRING>"), ("STRUCT", "STRUCT<k: STRING>")])
def test_une_table_ou_une_structure_rendue_en_json_redevient_un_dictionnaire(type_name, type_text):
    assert convertir_valeur('{"k":"v","n":null}', type_name, type_text) == {"k": "v", "n": None}
    assert convertir_valeur("{}", type_name, type_text) == {}


@pytest.mark.parametrize(
    ("type_name", "texte"),
    [
        ("ARRAY", '{"a":"b"}'), ("ARRAY", "[a, b]"), ("ARRAY", ""), ("ARRAY", "null"), ("ARRAY", '"texte"'), ("ARRAY", "[1, 2"),
        ("ARRAY", "[NaN]"), ("ARRAY", "[Infinity]"), ("MAP", '{"a":-Infinity}'),
        ("MAP", '["a"]'), ("MAP", "{a -> b}"), ("MAP", ""), ("MAP", "null"), ("MAP", '{"a":'),
        ("STRUCT", '["a"]'), ("STRUCT", "{a=b}"), ("STRUCT", ""), ("STRUCT", "42"),
    ],
)
def test_un_texte_qui_n_est_pas_du_json_de_la_bonne_forme_est_refuse(type_name, texte):
    with pytest.raises(ValueError):
        convertir_valeur(texte, type_name, f"{type_name}<STRING>")


@pytest.mark.parametrize("type_name", ["ARRAY", "MAP", "STRUCT"])
def test_un_json_trop_imbrique_est_refuse_par_une_value_error_et_non_par_un_depassement_de_pile(type_name):
    """La conversion rend une valeur ou lève `ValueError`, jamais autre chose : un texte pathologique ne sort pas en `RecursionError`."""
    ouvrant, fermant = ("[", "]") if type_name == "ARRAY" else ('{"a":', "}")

    with pytest.raises(ValueError):
        convertir_valeur(ouvrant * 100_000 + fermant * 100_000, type_name, type_name)


@pytest.mark.parametrize("type_name", ["BINARY", "BOOLEAN", "DECIMAL", "INTERVAL", "USER_DEFINED_TYPE", "NULL"])
def test_un_type_que_le_code_ne_produit_pas_est_refuse_au_lieu_d_etre_laisse_en_texte(type_name):
    with pytest.raises(ValueError, match=type_name):
        convertir_valeur("x", type_name, f"{type_name}(1)")


@pytest.mark.parametrize(
    ("type_name", "type_text"),
    [
        ("LONG", "BIGINT"), ("DOUBLE", "DOUBLE"), ("DATE", "DATE"), ("TIMESTAMP", "TIMESTAMP"), ("ARRAY", "ARRAY<STRING>"),
        ("MAP", "MAP<STRING, STRING>"), ("STRUCT", "STRUCT<a: STRING>"), ("BINARY", "BINARY"),
    ],
)
def test_une_erreur_de_conversion_ne_contient_pas_la_valeur_recue(type_name, type_text):
    with pytest.raises(ValueError) as erreur:
        convertir_valeur(VALEUR_SECRETE, type_name, type_text)

    assert VALEUR_SECRETE not in str(erreur.value) and VALEUR_SECRETE not in repr(erreur.value)


# ==============================================================================
# 2. 🧼 L'HYGIÈNE DU MODULE : il ne lit rien lui-même, le SDK reste facultatif à l'import
# ==============================================================================
def _source():
    return MODULE.read_text(encoding="utf-8")


def test_le_module_ne_lit_aucun_fichier_ni_variable_d_environnement():
    """Le profil, l'adresse du workspace et l'entrepôt viennent de l'appelant : jamais d'une lecture cachée ici (le dépôt est public)."""
    source = _source()

    for interdit in ("os.environ", "getenv", "databrickscfg", "open(", "read_text", "WorkspaceClient("):
        assert interdit not in source, f"« {interdit} » : le module ne doit rien lire lui-même"


def test_le_sdk_n_est_pas_importe_en_tete_du_module():
    """Importer `rd_manuf.executeur_databricks` doit marcher sans le SDK : il n'est demandé qu'au moment d'envoyer une requête."""
    assert re.search(r"^(import|from)\s+databricks", _source(), re.M) is None
