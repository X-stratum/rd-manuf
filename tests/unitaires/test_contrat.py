"""Tests du contrat (cycle A) : une règle = un test qui prouve qu'elle rejette ce qu'elle doit."""
from pathlib import Path

import pytest
import yaml

from rd_manuf.contrat import ContratInvalide, charger_contrat, controler_ligne, est_absente

CHEMIN_CONTRAT_REEL = Path(__file__).resolve().parents[2] / "contracts" / "data" / "livraison_production.yaml"


# ==============================================================================
# 1. 🔎 CONTRÔLE D'UNE LIGNE
# ==============================================================================
def test_une_ligne_conforme_ne_produit_aucune_erreur(contrat, ligne_conforme):
    assert controler_ligne(contrat, ligne_conforme) == []


def test_la_temperature_est_optionnelle(contrat, ligne_conforme):
    ligne_conforme["temperature_c"] = None
    assert controler_ligne(contrat, ligne_conforme) == []


@pytest.mark.parametrize(
    ("colonne", "valeur", "fragment_erreur"),
    [
        ("id_lot", "LOT-12", "motif"),
        ("id_lot", "lot-000001", "motif"),
        ("id_ligne", "L9", "hors de"),
        ("date_production", "01/11/2026", "type date invalide"),
        ("date_production", None, "obligatoire"),
        ("quantite_produite", -1, "minimum"),
        ("quantite_produite", 100001, "maximum"),
        ("quantite_produite", "beaucoup", "type entier invalide"),
        ("quantite_produite", 10.5, "type entier invalide"),
        ("quantite_rebut", "", "obligatoire"),
        ("temperature_c", 401, "maximum"),
        ("temperature_c", -51, "minimum"),
    ],
)
def test_chaque_regle_de_colonne_rejette_sa_violation(contrat, ligne_conforme, colonne, valeur, fragment_erreur):
    ligne_conforme[colonne] = valeur
    erreurs = controler_ligne(contrat, ligne_conforme)
    assert erreurs, f"{colonne}={valeur!r} aurait dû être rejeté"
    assert any(fragment_erreur in erreur and colonne in erreur for erreur in erreurs), erreurs


@pytest.mark.parametrize(
    "modifications",
    [
        {"quantite_produite": 0, "quantite_rebut": 0},   # minimum exact (rebut aligné pour ne pas violer la règle inter-colonnes)
        {"quantite_produite": 100000},   # maximum exact (le rebut de la fixture, 10, reste valide)
        {"quantite_rebut": 0},   # minimum exact
        {"temperature_c": -50},   # minimum exact
        {"temperature_c": 400},   # maximum exact
    ],
)
def test_les_bornes_du_contrat_sont_incluses(contrat, ligne_conforme, modifications):
    """Frontière : une valeur EXACTEMENT égale à minimum/maximum est conforme (comparaison stricte, pas >=)."""
    ligne_conforme.update(modifications)
    assert controler_ligne(contrat, ligne_conforme) == []


MARQUEURS_D_ABSENCE = [
    "nan", "NaN", "NAN", "Nan",
    "nans", "Nans",
    "null", "Null", "NULL",
    "na", "NA", "Na",
    "", " ", "   ",
    None, float("nan"),   # None et un vrai NaN (pandas / Spark), distincts du texte « nan »
]


@pytest.mark.parametrize("valeur", MARQUEURS_D_ABSENCE)
def test_un_marqueur_d_absence_est_accepte_sur_une_colonne_optionnelle(contrat, ligne_conforme, valeur):
    ligne_conforme["temperature_c"] = valeur
    assert controler_ligne(contrat, ligne_conforme) == []


@pytest.mark.parametrize("valeur", MARQUEURS_D_ABSENCE)
def test_un_marqueur_d_absence_est_refuse_sur_une_colonne_obligatoire(contrat, ligne_conforme, valeur):
    ligne_conforme["quantite_produite"] = valeur
    erreurs = controler_ligne(contrat, ligne_conforme)
    assert any("quantite_produite" in erreur and "obligatoire" in erreur for erreur in erreurs), erreurs


@pytest.mark.parametrize("valeur", ["inf", "-inf", "Infinity", float("inf"), float("-inf")])
def test_une_valeur_infinie_n_est_pas_une_absence_c_est_une_erreur(contrat, ligne_conforme, valeur):
    ligne_conforme["temperature_c"] = valeur
    erreurs = controler_ligne(contrat, ligne_conforme)
    assert any("temperature_c" in erreur and "invalide" in erreur for erreur in erreurs), erreurs


@pytest.mark.parametrize("valeur", ["nano", "nul", "n/a", "NaNa", "0"])
def test_un_texte_proche_d_un_marqueur_n_est_pas_une_absence(valeur):
    """Correspondance EXACTE (après strip et minuscules) : « nano » ou « 0 » ne sont pas des marqueurs."""
    assert not est_absente(valeur)


def test_le_rebut_ne_peut_pas_depasser_la_production(contrat, ligne_conforme):
    ligne_conforme["quantite_rebut"] = ligne_conforme["quantite_produite"] + 1
    assert any("rebut_inferieur_ou_egal_production" in erreur for erreur in controler_ligne(contrat, ligne_conforme))


def test_rebut_egal_a_la_production_est_accepte(contrat, ligne_conforme):
    ligne_conforme["quantite_rebut"] = ligne_conforme["quantite_produite"]   # cas limite : tout est rebut
    assert controler_ligne(contrat, ligne_conforme) == []


def test_plusieurs_violations_sont_toutes_remontees(contrat, ligne_conforme):
    ligne_conforme["id_ligne"] = "L9"
    ligne_conforme["quantite_produite"] = -5
    assert len(controler_ligne(contrat, ligne_conforme)) >= 2


# ==============================================================================
# 2. 🧯 CONTRAT MAL FORMÉ : doit échouer AVANT de toucher la moindre donnée
# ==============================================================================
def _contrat_modifie(tmp_path, modification):
    """Copie le vrai contrat, applique `modification` au dictionnaire, écrit le résultat et retourne son chemin."""
    brut = yaml.safe_load(CHEMIN_CONTRAT_REEL.read_text(encoding="utf-8"))
    modification(brut)
    chemin = tmp_path / "contrat.yaml"
    chemin.write_text(yaml.safe_dump(brut, allow_unicode=True), encoding="utf-8")
    return chemin


def test_le_vrai_contrat_est_valide():
    assert charger_contrat(CHEMIN_CONTRAT_REEL).nom == "livraison_production"


@pytest.mark.parametrize(
    ("description", "modification"),
    [
        ("type inconnu", lambda brut: brut["colonnes"][0].update(type="texte")),
        ("clé primaire absente des colonnes", lambda brut: brut.update(cle_primaire=["inexistante"])),
        ("règle sur colonne inconnue", lambda brut: brut["regles_inter_colonnes"][0].update(gauche="fantome")),
        ("règle de type inconnu", lambda brut: brut["regles_inter_colonnes"][0].update(type="egal")),
        ("taux de rejet > 1", lambda brut: brut["seuils_qualite"].update(taux_rejet_max=2)),
        ("section obligatoire retirée", lambda brut: brut.pop("seuils_qualite")),
    ],
)
def test_un_contrat_mal_forme_est_refuse(tmp_path, description, modification):
    with pytest.raises(ContratInvalide):
        charger_contrat(_contrat_modifie(tmp_path, modification))
