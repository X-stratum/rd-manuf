"""Tests du LECTEUR PARQUET : transformer un fichier Parquet envoyé par l'usine en (colonnes reçues, lignes), comme le lecteur CSV.

Différence avec le CSV : le fichier porte son propre schéma, donc ni BOM, ni séparateur, ni ligne dont le nombre de champs serait faux.
Les valeurs arrivent déjà typées (entiers, dates, absences). Les fichiers de test sont fabriqués par le simulateur, conforme au contrat.
"""
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rd_manuf.contrat import StatutLivraison
from rd_manuf.lecture import FichierIllisible, lire_parquet
from rd_manuf.reception import receptionner_livraison
from rd_manuf.simulateur import generer_livraison, types_arrow_du_contrat

GRAINE = 42
NB_LIGNES = 5
TYPES_ARROW = types_arrow_du_contrat()


def _ecrire_parquet_sain(chemin, contrat):
    pq.write_table(pa.Table.from_pylist(generer_livraison(contrat, NB_LIGNES, GRAINE)), chemin)


# ==============================================================================
# 1. ✅ UN FICHIER SAIN : mêmes colonnes et mêmes lignes que le simulateur les a fabriquées
# ==============================================================================
def test_un_fichier_parquet_sain_donne_ses_colonnes_et_ses_lignes(tmp_path, contrat, entete):
    chemin = tmp_path / "livraison.parquet"
    _ecrire_parquet_sain(chemin, contrat)

    fichier = lire_parquet(chemin)

    assert fichier.colonnes == entete
    assert fichier.lignes == generer_livraison(contrat, NB_LIGNES, GRAINE)
    assert fichier.lignes_malformees == ()


# ==============================================================================
# 2. 🚫 FICHIER ILLISIBLE : une erreur CLAIRE, comme pour un CSV mal encodé
# ==============================================================================
def test_un_fichier_qui_n_est_pas_un_parquet_est_illisible(tmp_path):
    chemin = tmp_path / "livraison.parquet"
    chemin.write_text("id_lot,id_ligne\nLOT-000001,L1\n", encoding="utf-8")      # un CSV renvoyé sous le mauvais nom

    with pytest.raises(FichierIllisible):
        lire_parquet(chemin)


def test_un_parquet_tronque_est_illisible(tmp_path, contrat):
    """Un transfert interrompu : le pied du fichier Parquet, qui contient son schéma, est perdu."""
    chemin = tmp_path / "livraison.parquet"
    _ecrire_parquet_sain(chemin, contrat)
    octets = chemin.read_bytes()
    chemin.write_bytes(octets[: len(octets) // 2])

    with pytest.raises(FichierIllisible):
        lire_parquet(chemin)


def test_un_parquet_a_colonne_en_double_est_lu_avec_ses_deux_noms(tmp_path):
    """Le fichier est valide (Parquet accepte deux colonnes de même nom) : le LECTEUR ne juge pas, il rapporte les noms tels que reçus,
    pour que la réception dise `COLONNE_DUPLIQUEE`, comme pour un CSV. Le refuser comme illisible cacherait la colonne à corriger."""
    chemin = tmp_path / "livraison.parquet"
    pq.write_table(pa.Table.from_arrays([pa.array(["a"]), pa.array(["b"]), pa.array([1])], names=["id_lot", "id_lot", "autre"]), chemin)

    fichier = lire_parquet(chemin)

    assert fichier.colonnes == ["id_lot", "id_lot", "autre"]
    assert len(fichier.lignes) == 1


# ==============================================================================
# 3. 🔗 DE BOUT EN BOUT : un vrai Parquet typé (dates, entiers, décimaux, absence) est jugé comme un CSV
# ==============================================================================
def _ecrire_parquet_type(chemin, contrat, colonne_absente):
    """Chaque colonne a le type que le contrat décrit ; l'absence de `colonne_absente` est un vrai `null`, pas un texte vide."""
    def typee(colonne, valeur):
        return date.fromisoformat(valeur) if colonne.type == "date" else valeur      # le simulateur écrit les dates en texte ISO

    schema = pa.schema([(colonne.nom, TYPES_ARROW[colonne.type]) for colonne in contrat.colonnes])
    lignes = [
        {colonne.nom: typee(colonne, ligne[colonne.nom]) for colonne in contrat.colonnes}
        for ligne in generer_livraison(contrat, NB_LIGNES, GRAINE)
    ]
    lignes[0][colonne_absente] = None
    pq.write_table(pa.Table.from_pylist(lignes, schema=schema), chemin)


def test_un_parquet_type_avec_une_absence_est_accepte_par_la_reception(tmp_path, contrat):
    facultative = next(colonne.nom for colonne in contrat.colonnes if not colonne.obligatoire)
    chemin = tmp_path / "livraison.parquet"
    _ecrire_parquet_type(chemin, contrat, facultative)

    fichier = lire_parquet(chemin)
    reception = receptionner_livraison(contrat, "livraison-test", fichier.colonnes, fichier.lignes, fichier.lignes_malformees)

    assert fichier.lignes[0][facultative] is None                  # une absence arrive comme un vrai None
    assert reception.statut == StatutLivraison.ACCEPTEE
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (NB_LIGNES, NB_LIGNES, 0)
