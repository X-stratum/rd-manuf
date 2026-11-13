"""Tests de la CONVENTION DE NOM des fichiers livrés : livraison_production_{PAYS}_{USINE}_{LIGNE}_{AAAA-MM-JJ-HH-MM-SS}Z.{csv|parquet}.

Le nom dit d'où (pays, usine, ligne de production) et de quand (instant en UTC, marqué par le Z) vient le fichier.
Il est contrôlé AVANT toute lecture : un nom douteux ne permet ni de savoir qui prévenir, ni de savoir quelle livraison remplace laquelle.
"""
from datetime import UTC, datetime

import pytest

from rd_manuf.nom_fichier import NomFichier, NomFichierInvalide, lire_nom_fichier

PAYS = "FRA"
USINE = "AA"
LIGNE = "001"
DATE = "2026-11-11-14-30-05"


def _nom(pays=PAYS, usine=USINE, ligne=LIGNE, date=DATE, fin="Z.csv", debut="livraison_production"):
    return f"{debut}_{pays}_{usine}_{ligne}_{date}{fin}"


# ==============================================================================
# 1. ✅ UN NOM CONFORME : on en tire pays, usine, ligne de production, instant UTC et format
# ==============================================================================
def test_un_nom_conforme_donne_ses_cinq_informations():
    attendu = NomFichier(pays=PAYS, usine=USINE, ligne=LIGNE, horodatage=datetime(2026, 11, 11, 14, 30, 5, tzinfo=UTC), format="csv")
    assert lire_nom_fichier(_nom()) == attendu


@pytest.mark.parametrize("extension", ["csv", "parquet"])
def test_les_deux_formats_sont_reconnus(extension):
    assert lire_nom_fichier(_nom(fin=f"Z.{extension}")).format == extension


def test_l_horodatage_est_un_instant_en_utc():
    """Un instant « naïf » (sans fuseau) se comparerait mal à l'heure d'arrivée : celui-ci est explicitement en UTC."""
    assert lire_nom_fichier(_nom()).horodatage.utcoffset().total_seconds() == 0


def test_l_usine_et_la_ligne_de_production_sont_deux_informations_separees():
    """`AA_001` n'est pas « un code usine » : AA est l'usine, 001 sa ligne de production. Deux lignes d'une usine partagent l'usine."""
    premiere = lire_nom_fichier(_nom(ligne="001"))
    seconde = lire_nom_fichier(_nom(ligne="002"))
    assert (premiere.usine, premiere.ligne) == ("AA", "001")
    assert (seconde.usine, seconde.ligne) == ("AA", "002")


def test_les_underscores_du_nom_ne_perturbent_pas_le_decoupage():
    """Les séparateurs sont des « _ » comme celui entre l'usine et la ligne : seules les largeurs fixes les distinguent."""
    nom = lire_nom_fichier("livraison_production_DEU_ZZ_999_2026-12-31-23-59-59Z.parquet")
    assert (nom.pays, nom.usine, nom.ligne, nom.format) == ("DEU", "ZZ", "999", "parquet")
    assert nom.horodatage == datetime(2026, 12, 31, 23, 59, 59, tzinfo=UTC)


# ==============================================================================
# 2. 🚫 UN NOM NON CONFORME EST REFUSÉ : un défaut à la fois, pour savoir lequel est détecté
# ==============================================================================
@pytest.mark.parametrize(
    "nom",
    [
        pytest.param(_nom(fin=".csv"), id="sans-le-Z-d-utc"),
        pytest.param(_nom(date="2026-11-11-14-30-05+01"), id="avec-un-decalage-au-lieu-du-Z"),
        pytest.param(_nom(pays="fra"), id="pays-en-minuscules"),
        pytest.param(_nom(pays="FR"), id="pays-sur-2-lettres"),
        pytest.param(_nom(pays="FRAN"), id="pays-sur-4-lettres"),
        pytest.param(_nom(usine="A"), id="usine-sur-1-lettre"),
        pytest.param(_nom(usine="AAA"), id="usine-sur-3-lettres"),
        pytest.param(_nom(usine="aa"), id="usine-en-minuscules"),
        pytest.param(_nom(usine="A1"), id="usine-avec-un-chiffre"),
        pytest.param(_nom(ligne="01"), id="ligne-sur-2-chiffres"),
        pytest.param(_nom(ligne="0001"), id="ligne-sur-4-chiffres"),
        pytest.param(_nom(ligne="ABC"), id="ligne-en-lettres"),
        pytest.param(_nom(ligne="٠٠١"), id="chiffres-non-latins-dans-la-ligne"),
        pytest.param(_nom().replace("AA_001", "AA001"), id="usine-et-ligne-collees-ancien-format"),
        pytest.param(_nom().replace("AA_001", "AA-001"), id="usine-et-ligne-separees-par-un-tiret"),
        pytest.param(_nom(ligne="_001"), id="usine-et-ligne-separees-par-deux-underscores"),
        pytest.param(_nom().replace("AA_001_", ""), id="sans-usine-ni-ligne"),
        pytest.param(_nom().replace("_001", ""), id="sans-ligne"),
        pytest.param(_nom(debut="livraison_stock"), id="mauvais-prefixe"),
        pytest.param(_nom(fin="Z.txt"), id="extension-inconnue"),
        pytest.param(_nom(fin="Z.CSV"), id="extension-en-majuscules"),
        pytest.param(_nom(date="2026-11-11-14-30"), id="sans-les-secondes"),
        pytest.param(_nom(date="2026-11-11-14:30:05"), id="deux-points-au-lieu-de-tirets"),
        pytest.param(_nom(date="2026-02-31-14-30-05"), id="31-fevrier"),
        pytest.param(_nom(date="2026-13-11-14-30-05"), id="mois-13"),
        pytest.param(_nom(date="2026-11-11-25-30-05"), id="heure-25"),
        pytest.param(_nom() + "\n", id="retour-a-la-ligne-final"),
        pytest.param(_nom(date="٢٠٢٦-11-11-14-30-05"), id="chiffres-non-latins-dans-la-date"),
        pytest.param("", id="nom-vide"),
        pytest.param("livraison.csv", id="nom-trop-court"),
    ],
)
def test_un_nom_non_conforme_est_refuse(nom):
    with pytest.raises(NomFichierInvalide):
        lire_nom_fichier(nom)


def test_le_message_cite_le_nom_recu_et_la_forme_attendue():
    """Le refus est lu par un humain (l'émetteur, l'équipe data) : il doit dire ce qui a été reçu et ce qui était attendu."""
    with pytest.raises(NomFichierInvalide) as erreur:
        lire_nom_fichier("n_importe_quoi.csv")
    message = str(erreur.value)
    assert "n_importe_quoi.csv" in message
    assert "livraison_production_" in message


def test_le_message_donne_un_exemple_de_nom_conforme_et_cet_exemple_est_accepte():
    """La forme abstraite est difficile à lire avec ses « _ ». Un exemple concret aide, à condition qu'il soit juste."""
    with pytest.raises(NomFichierInvalide) as erreur:
        lire_nom_fichier("n_importe_quoi.csv")
    exemple = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"
    assert exemple in str(erreur.value)
    nom = lire_nom_fichier(exemple)
    assert (nom.usine, nom.ligne) == ("AA", "001")


def test_la_forme_attendue_nomme_l_usine_et_la_ligne_de_production():
    """`{USINE}_{LIGNE}` dit que ce sont deux informations ; leurs largeurs sont écrites à côté, car les mots seuls ne les disent pas."""
    with pytest.raises(NomFichierInvalide) as erreur:
        lire_nom_fichier("n_importe_quoi.csv")
    message = str(erreur.value)
    assert "{PAYS}_{USINE}_{LIGNE}_{AAAA-MM-JJ-HH-MM-SS}Z" in message
    assert "USINE : 2 lettres majuscules" in message
    assert "LIGNE : 3 chiffres" in message


def test_nom_fichier_invalide_n_est_pas_une_value_error():
    """Même précaution que pour FichierIllisible : un `except ValueError` ne doit pas attraper aussi un contrat cassé."""
    assert not issubclass(NomFichierInvalide, ValueError)
