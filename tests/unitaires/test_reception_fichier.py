"""Tests de l'ENCHAÎNEMENT complet sur un fichier : nom, puis lecture selon le format, puis réception.

Un nom invalide est refusé AVANT toute lecture (les fichiers de ces tests n'existent même pas) ; un fichier illisible est refusé
proprement ; sinon c'est la réception habituelle qui décide. L'identifiant de la livraison est le nom du fichier.
"""
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rd_manuf.contrat import StatutLivraison
from rd_manuf.reception import receptionner_fichier
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison
from rd_manuf.structure import CodeStructure

GRAINE = 42
NB_LIGNES = 20
NOM_CSV = "livraison_production_FRA_AB123_2026-11-11-14-30-05Z.csv"
NOM_PARQUET = "livraison_production_FRA_AB123_2026-11-11-14-30-05Z.parquet"


def _codes(reception):
    return [probleme.code for probleme in reception.problemes_structure]


# ==============================================================================
# 1. ✅ UN FICHIER CONFORME, DANS CHAQUE FORMAT, EST RÉCEPTIONNÉ
# ==============================================================================
def test_un_csv_conforme_est_accepte_et_garde_son_nom_comme_identifiant(tmp_path, contrat):
    chemin = tmp_path / NOM_CSV
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE), chemin)

    reception = receptionner_fichier(contrat, chemin)

    assert reception.statut == StatutLivraison.ACCEPTEE
    assert reception.livraison_id == NOM_CSV
    assert (reception.nb_recues, reception.nb_valides) == (NB_LIGNES, NB_LIGNES)


def test_un_parquet_conforme_est_accepte(tmp_path, contrat):
    chemin = tmp_path / NOM_PARQUET
    pq.write_table(pa.Table.from_pylist(generer_livraison(contrat, NB_LIGNES, GRAINE)), chemin)

    reception = receptionner_fichier(contrat, chemin)

    assert reception.statut == StatutLivraison.ACCEPTEE
    assert reception.nb_valides == NB_LIGNES


# ==============================================================================
# 2. 🚫 NOM INVALIDE : refusé avant toute lecture
# ==============================================================================
@pytest.mark.parametrize("nom", ["donnees.csv", "livraison_production_fra_AB123_2026-11-11-14-30-05Z.csv"])
def test_un_nom_invalide_est_refuse_sans_lire_le_fichier(tmp_path, contrat, nom):
    reception = receptionner_fichier(contrat, tmp_path / nom)      # le fichier n'existe pas : le refus vient du nom seul

    assert reception.statut == StatutLivraison.REFUSEE
    assert _codes(reception) == [CodeStructure.NOM_FICHIER_INVALIDE]
    assert reception.livraison_id == nom
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (0, 0, 0)
    assert nom in reception.problemes_structure[0].message


# ==============================================================================
# 3. 🚫 FICHIER ILLISIBLE : refusé proprement, quel que soit le format
# ==============================================================================
def test_un_csv_mal_encode_est_refuse_comme_illisible(tmp_path, contrat):
    chemin = tmp_path / NOM_CSV
    chemin.write_bytes("id_lot,id_ligne\né\n".encode("latin-1"))

    reception = receptionner_fichier(contrat, chemin)

    assert reception.statut == StatutLivraison.REFUSEE
    assert _codes(reception) == [CodeStructure.FICHIER_ILLISIBLE]
    assert reception.livraison_id == NOM_CSV


def test_un_parquet_tronque_est_refuse_comme_illisible(tmp_path, contrat):
    chemin = tmp_path / NOM_PARQUET
    pq.write_table(pa.Table.from_pylist(generer_livraison(contrat, NB_LIGNES, GRAINE)), chemin)
    octets = chemin.read_bytes()
    chemin.write_bytes(octets[: len(octets) // 2])

    reception = receptionner_fichier(contrat, chemin)

    assert reception.statut == StatutLivraison.REFUSEE
    assert _codes(reception) == [CodeStructure.FICHIER_ILLISIBLE]
