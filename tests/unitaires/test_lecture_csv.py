"""Tests du LECTEUR CSV : transformer un fichier envoyé par l'usine en (colonnes reçues, lignes), prêts pour la réception.

Le lecteur ne juge rien : il ne dit pas si le fichier est bon, il le LIT fidèlement. Le jugement reste à la réception.
Les fichiers de test sont fabriqués par le simulateur (conforme au contrat) puis abîmés à la main, un défaut à la fois.
"""
import codecs
import csv

import pytest

from rd_manuf.contrat import CodeErreur, StatutLivraison
from rd_manuf.lecture import FichierIllisible, lire_csv
from rd_manuf.reception import receptionner_livraison
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison

GRAINE = 42
NB_LIGNES = 5
NUMERO_DERNIERE_LIGNE = NB_LIGNES + 1   # numérotée comme dans Excel : l'en-tête est la ligne 1


def _ecrire_fichier_sain(chemin, contrat):
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE), chemin)


# ==============================================================================
# 1. 🫥 LE BOM : un caractère invisible qu'Excel ajoute en tête de fichier
# ==============================================================================
def test_un_bom_en_tete_n_est_pas_dans_le_nom_de_la_premiere_colonne(tmp_path, contrat, entete):
    """Sans traitement, la 1re colonne s'appellerait « \\ufeffid_lot » : refusée à la réception alors qu'elle est juste."""
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    chemin.write_bytes(codecs.BOM_UTF8 + chemin.read_bytes())

    assert lire_csv(chemin).colonnes == entete


# ==============================================================================
# 2. ✂️ LIGNE TROP COURTE : le signe d'un fichier tronqué, signalé et non deviné
# ==============================================================================
def test_un_fichier_sain_n_a_aucune_ligne_malformee(tmp_path, contrat):
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)

    assert lire_csv(chemin).lignes_malformees == ()


def test_une_derniere_ligne_coupee_est_signalee_et_mise_a_part(tmp_path, contrat, entete):
    """Sans ce signalement, la valeur manquante serait lue comme une absence : une ligne coupée passerait pour valide."""
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    lignes_du_fichier = chemin.read_text(encoding="utf-8").splitlines()
    lignes_du_fichier[-1] = lignes_du_fichier[-1].rsplit(",", 1)[0]      # la dernière valeur de la dernière ligne n'est jamais arrivée
    chemin.write_text("\n".join(lignes_du_fichier) + "\n", encoding="utf-8")

    fichier = lire_csv(chemin)

    (malformee,) = fichier.lignes_malformees
    assert malformee.numero == NUMERO_DERNIERE_LIGNE
    assert len(malformee.champs) == len(entete) - 1     # les valeurs brutes sont conservées, pour le diagnostic
    assert len(fichier.lignes) == NB_LIGNES - 1             # la ligne malformée n'est plus mêlée aux lignes saines


# ==============================================================================
# 3. 📏 LIGNE TROP LONGUE : une virgule non protégée décale tout, rien ne doit disparaître en silence
# ==============================================================================
def test_une_derniere_ligne_trop_longue_est_signalee_et_mise_a_part(tmp_path, contrat, entete):
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    lignes_du_fichier = chemin.read_text(encoding="utf-8").splitlines()
    lignes_du_fichier[-1] += ",valeur_en_trop"
    chemin.write_text("\n".join(lignes_du_fichier) + "\n", encoding="utf-8")

    fichier = lire_csv(chemin)

    (malformee,) = fichier.lignes_malformees
    assert malformee.numero == NUMERO_DERNIERE_LIGNE
    assert len(malformee.champs) == len(entete) + 1     # les valeurs brutes sont conservées, pour le diagnostic
    assert len(fichier.lignes) == NB_LIGNES - 1             # la ligne malformée n'est plus mêlée aux lignes saines


# ==============================================================================
# 4. 🔣 SÉPARATEUR : le CSV d'un Excel français sépare par « ; », celui d'un script par « , »
# ==============================================================================
def test_le_separateur_demande_est_celui_utilise_pour_decouper(tmp_path, contrat, entete):
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    # Remplacer toutes les virgules est sûr ici : le simulateur n'écrit aucune virgule à l'intérieur d'une valeur.
    texte_avec_virgules = chemin.read_text(encoding="utf-8")
    chemin.write_text(texte_avec_virgules.replace(",", ";"), encoding="utf-8")

    fichier = lire_csv(chemin, separateur=";")

    assert fichier.colonnes == entete
    assert len(fichier.lignes) == NB_LIGNES
    assert fichier.lignes_malformees == ()


# ==============================================================================
# 5. 📭 FICHIER VIDE, EN-TÊTE SEUL, COLONNE EN DOUBLE : le lecteur transmet tel quel, la réception jugera
# ==============================================================================
def test_un_fichier_vide_donne_ni_colonne_ni_ligne(tmp_path):
    chemin = tmp_path / "livraison.csv"
    chemin.write_bytes(b"")

    fichier = lire_csv(chemin)

    assert (fichier.colonnes, fichier.lignes, fichier.lignes_malformees) == ([], [], ())


def test_un_en_tete_seul_donne_les_colonnes_et_aucune_ligne(tmp_path, entete):
    chemin = tmp_path / "livraison.csv"
    chemin.write_text(",".join(entete) + "\n", encoding="utf-8")

    fichier = lire_csv(chemin)

    assert (fichier.colonnes, fichier.lignes) == (entete, [])


def test_une_colonne_en_double_reste_visible_dans_l_en_tete(tmp_path, entete):
    """Un lecteur qui fusionnerait les doublons ferait disparaître le problème avant que la réception puisse le voir."""
    chemin = tmp_path / "livraison.csv"
    chemin.write_text(",".join(entete + [entete[0]]) + "\n", encoding="utf-8")

    assert lire_csv(chemin).colonnes == entete + [entete[0]]


# ==============================================================================
# 6. 🔗 DE BOUT EN BOUT : un fichier réel, avec BOM, lu puis réceptionné
# ==============================================================================
def test_un_fichier_sain_avec_bom_est_accepte_par_la_reception(tmp_path, contrat):
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    chemin.write_bytes(codecs.BOM_UTF8 + chemin.read_bytes())

    fichier = lire_csv(chemin)
    reception = receptionner_livraison(contrat, "livraison-test", fichier.colonnes, fichier.lignes)

    assert reception.statut == StatutLivraison.ACCEPTEE


def test_un_fichier_tronque_n_est_plus_accepte_et_sa_ligne_coupee_est_rejetee(tmp_path, contrat):
    """Le cas qui motive le lecteur : sans lignes malformées, une ligne coupée avant sa dernière colonne facultative passait pour valide."""
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    lignes_du_fichier = chemin.read_text(encoding="utf-8").splitlines()
    lignes_du_fichier[-1] = lignes_du_fichier[-1].rsplit(",", 1)[0]
    chemin.write_text("\n".join(lignes_du_fichier) + "\n", encoding="utf-8")

    fichier = lire_csv(chemin)
    reception = receptionner_livraison(contrat, "livraison-test", fichier.colonnes, fichier.lignes, fichier.lignes_malformees)

    assert reception.statut != StatutLivraison.ACCEPTEE
    assert (reception.nb_recues, reception.nb_rejetees) == (NB_LIGNES, 1)
    (rejet,) = reception.resultat.lignes_rejetees
    assert [violation.code for violation in rejet.violations] == [CodeErreur.LIGNE_MALFORMEE]


# ==============================================================================
# 7. 🈳 LIGNE VIDE : elle ne porte aucune donnée, ce n'est ni une ligne reçue ni une ligne malformée
# ==============================================================================
def test_une_ligne_vide_en_fin_de_fichier_n_est_pas_une_ligne_de_donnees(tmp_path, contrat):
    """Les exports Windows se terminent souvent par une ligne vide : elle ne doit pas gonfler le nombre de lignes reçues."""
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    chemin.write_text(chemin.read_text(encoding="utf-8") + "\n", encoding="utf-8")

    fichier = lire_csv(chemin)

    assert len(fichier.lignes) == NB_LIGNES
    assert fichier.lignes_malformees == ()


def test_une_ligne_vide_au_milieu_du_fichier_est_ignoree(tmp_path, contrat):
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    lignes_du_fichier = chemin.read_text(encoding="utf-8").splitlines()
    lignes_du_fichier.insert(2, "")                                       # entre la 1re et la 2e ligne de données
    chemin.write_text("\n".join(lignes_du_fichier) + "\n", encoding="utf-8")

    fichier = lire_csv(chemin)

    assert len(fichier.lignes) == NB_LIGNES
    assert fichier.lignes_malformees == ()


def test_une_ligne_vide_avant_l_en_tete_ne_devient_pas_l_en_tete(tmp_path, contrat, entete):
    """Sinon le fichier serait lu sans aucune colonne, et la réception annoncerait « fichier vide » alors qu'il contient des données."""
    chemin = tmp_path / "livraison.csv"
    _ecrire_fichier_sain(chemin, contrat)
    chemin.write_text("\n" + chemin.read_text(encoding="utf-8"), encoding="utf-8")

    fichier = lire_csv(chemin)

    assert fichier.colonnes == entete
    assert len(fichier.lignes) == NB_LIGNES
    assert fichier.lignes_malformees == ()


# ==============================================================================
# 8. 🚫 FICHIER ILLISIBLE : une erreur CLAIRE, pas une erreur technique qui arrête tout le programme
# ==============================================================================
@pytest.mark.parametrize(
    "contenu",
    [
        pytest.param("id_lot,id_ligne\né\n".encode("latin-1"), id="latin-1-au-lieu-d-utf-8"),
        pytest.param("id_lot,id_ligne\n".encode("utf-16"), id="utf-16-comme-l-exporte-parfois-excel"),
        pytest.param(b"id_lot\n" + b"x" * (csv.field_size_limit() + 1), id="champ-plus-long-que-la-limite-du-lecteur"),
    ],
)
def test_un_fichier_illisible_leve_une_erreur_claire(tmp_path, contenu):
    chemin = tmp_path / "livraison.csv"
    chemin.write_bytes(contenu)

    with pytest.raises(FichierIllisible):
        lire_csv(chemin)
