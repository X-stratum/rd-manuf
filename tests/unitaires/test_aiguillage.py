"""Tests de l'AIGUILLAGE : le format annoncé par le nom du fichier choisit le lecteur (CSV ou Parquet)."""
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rd_manuf.lecture import LECTEURS, FichierIllisible, lire_selon_format
from rd_manuf.nom_fichier import FORMATS
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison

GRAINE = 42
NB_LIGNES = 5


def test_chaque_format_que_le_nom_autorise_a_un_lecteur():
    """Sinon un nom valide pourrait annoncer un format que personne ne sait lire."""
    assert set(LECTEURS) == set(FORMATS)


def test_un_csv_est_lu_par_le_lecteur_csv(tmp_path, contrat, entete):
    chemin = tmp_path / "livraison.csv"
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE), chemin)
    assert lire_selon_format(chemin, "csv").colonnes == entete


def test_un_parquet_est_lu_par_le_lecteur_parquet(tmp_path, contrat, entete):
    chemin = tmp_path / "livraison.parquet"
    pq.write_table(pa.Table.from_pylist(generer_livraison(contrat, NB_LIGNES, GRAINE)), chemin)
    assert lire_selon_format(chemin, "parquet").colonnes == entete


def test_le_format_demande_decide_du_lecteur(tmp_path, contrat):
    """Un Parquet lu comme un CSV n'est pas lisible : c'est bien le format annoncé qui choisit, pas le contenu."""
    chemin = tmp_path / "livraison.parquet"
    pq.write_table(pa.Table.from_pylist(generer_livraison(contrat, NB_LIGNES, GRAINE)), chemin)
    with pytest.raises(FichierIllisible):
        lire_selon_format(chemin, "csv")


def test_un_format_inconnu_est_illisible(tmp_path):
    with pytest.raises(FichierIllisible, match="xml"):
        lire_selon_format(tmp_path / "livraison.xml", "xml")
