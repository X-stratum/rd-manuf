"""Tests de la CONVENTION DE NOM des fichiers livrés : livraison_production_{PAYS}_{LL_NNN}_{AAAA-MM-JJ-HH-MM-SS}Z.{csv|parquet}.

Le nom dit d'où (pays, usine) et de quand (instant en UTC, marqué par le Z) vient le fichier. Il est contrôlé AVANT toute lecture :
un nom douteux ne permet ni de savoir qui prévenir, ni de savoir quelle livraison remplace laquelle.
"""
from datetime import UTC, datetime

import pytest

from rd_manuf.nom_fichier import NomFichier, NomFichierInvalide, lire_nom_fichier

PAYS = "FRA"
USINE = "AA_001"
DATE = "2026-11-11-14-30-05"


def _nom(pays=PAYS, usine=USINE, date=DATE, fin="Z.csv", debut="livraison_production"):
    return f"{debut}_{pays}_{usine}_{date}{fin}"


# ==============================================================================
# 1. ✅ UN NOM CONFORME : on en tire pays, usine, instant UTC et format
# ==============================================================================
def test_un_nom_conforme_donne_ses_quatre_informations():
    attendu = NomFichier(pays=PAYS, usine=USINE, horodatage=datetime(2026, 11, 11, 14, 30, 5, tzinfo=UTC), format="csv")
    assert lire_nom_fichier(_nom()) == attendu


@pytest.mark.parametrize("extension", ["csv", "parquet"])
def test_les_deux_formats_sont_reconnus(extension):
    assert lire_nom_fichier(_nom(fin=f"Z.{extension}")).format == extension


def test_l_horodatage_est_un_instant_en_utc():
    """Un instant « naïf » (sans fuseau) se comparerait mal à l'heure d'arrivée : celui-ci est explicitement en UTC."""
    assert lire_nom_fichier(_nom()).horodatage.utcoffset().total_seconds() == 0


def test_l_underscore_du_code_usine_ne_perturbe_pas_le_decoupage():
    """Le code usine contient un « _ » comme les séparateurs : seules les largeurs fixes les distinguent."""
    nom = lire_nom_fichier("livraison_production_DEU_ZZ_999_2026-12-31-23-59-59Z.parquet")
    assert (nom.pays, nom.usine, nom.format) == ("DEU", "ZZ_999", "parquet")
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
        pytest.param(_nom(usine="A_001"), id="usine-sur-1-lettre"),
        pytest.param(_nom(usine="AAA_001"), id="usine-sur-3-lettres"),
        pytest.param(_nom(usine="aa_001"), id="usine-en-minuscules"),
        pytest.param(_nom(usine="AA_01"), id="usine-sur-2-chiffres"),
        pytest.param(_nom(usine="AA_0001"), id="usine-sur-4-chiffres"),
        pytest.param(_nom(usine="AA001"), id="usine-sans-underscore-ancien-format"),
        pytest.param(_nom(usine="AA-001"), id="usine-avec-un-tiret"),
        pytest.param(_nom(usine="AA__001"), id="usine-avec-deux-underscores"),
        pytest.param(_nom(usine="AA_٠٠١"), id="chiffres-non-latins-dans-l-usine"),
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
    """Le code usine contient un « _ » : la forme abstraite est difficile à lire. Un exemple concret aide, à condition qu'il soit juste."""
    with pytest.raises(NomFichierInvalide) as erreur:
        lire_nom_fichier("n_importe_quoi.csv")
    exemple = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"
    assert exemple in str(erreur.value)
    assert lire_nom_fichier(exemple).usine == "AA_001"


def test_la_forme_attendue_montre_que_le_code_usine_contient_un_underscore():
    """`LL_NNN` (L : lettre, N : chiffre) dit que le code contient un « _ », et ne se confond pas avec `AAAA` (année)."""
    with pytest.raises(NomFichierInvalide) as erreur:
        lire_nom_fichier("n_importe_quoi.csv")
    assert "{PAYS}_{LL_NNN}_{AAAA-MM-JJ-HH-MM-SS}Z" in str(erreur.value)


def test_nom_fichier_invalide_n_est_pas_une_value_error():
    """Même précaution que pour FichierIllisible : un `except ValueError` ne doit pas attraper aussi un contrat cassé."""
    assert not issubclass(NomFichierInvalide, ValueError)
