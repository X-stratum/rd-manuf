"""Tests du BRONZE : la copie fidèle des lignes reçues, en texte, avec de quoi remonter jusqu'à la livraison.

Bronze ne juge pas : une ligne invalide y entre comme les autres (c'est le silver qui la mettra en quarantaine). Il garde toutes les lignes
bien formées des livraisons ACCEPTEE, ACCEPTEE_AVEC_REJETS et BLOQUEE (une livraison bloquée pourra être retraitée après correction).
Une livraison REFUSEE n'y entre pas, un rejeu non plus : le registre en garde la trace et le fichier reste à la source.
Ces tests ne touchent aucune table : le module construit des lignes, l'écriture viendra avec le déploiement.
"""
from datetime import UTC, date, datetime

import pytest

from rd_manuf.bronze import STATUTS_BRONZE, LigneBronze, construire_lignes_bronze
from rd_manuf.contrat import CodeErreur, StatutLivraison, est_absente
from rd_manuf.lecture import FichierLu, lire_csv
from rd_manuf.registre import LigneRegistre, traiter_fichier
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison

GRAINE = 42
NB_LIGNES = 20
ARRIVEE = datetime(2026, 11, 11, 14, 35, 0, tzinfo=UTC)
NOM = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"


def _ecrire(tmp_path, contrat, nom=NOM, anomalies=None, graine=GRAINE, nb_lignes=NB_LIGNES):
    chemin = tmp_path / nom
    ecrire_livraison_csv(contrat, generer_livraison(contrat, nb_lignes, graine, anomalies), chemin)
    return chemin


def _bronze_du_fichier(contrat, chemin, registre=(), arrivee=ARRIVEE):
    """Le parcours complet sur un vrai fichier CSV : le registre décide, puis bronze construit ses lignes."""
    _, ligne_registre = traiter_fichier(contrat, chemin, registre, arrivee)
    return ligne_registre, construire_lignes_bronze(contrat, ligne_registre, lire_csv(chemin))


def _registre_accepte():
    """Une ligne de registre ACCEPTEE minimale, pour les tests qui construisent leur fichier sans passer par le disque."""
    return LigneRegistre(
        livraison_id=NOM, statut="ACCEPTEE", version_contrat="1.0.0", nb_recues=1, nb_valides=1, nb_rejetees=0, codes_refus=(),
        pays="FRA", usine="AA_001", horodatage_fichier=datetime(2026, 11, 11, 14, 30, 5, tzinfo=UTC), heure_arrivee=ARRIVEE,
        empreinte=None, rejeu_de=None,
    )


# ==============================================================================
# 1. 📥 UNE LIGNE BRONZE PAR LIGNE LUE, AVEC SA TRAÇABILITÉ
# ==============================================================================
def test_chaque_ligne_lue_devient_une_ligne_bronze_dans_l_ordre(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)

    ligne_registre, lignes = _bronze_du_fichier(contrat, chemin)

    assert ligne_registre.statut == "ACCEPTEE"
    assert [ligne.numero_ligne for ligne in lignes] == list(range(1, NB_LIGNES + 1))
    assert {(ligne.livraison_id, ligne.heure_arrivee) for ligne in lignes} == {(NOM, ARRIVEE)}


def test_une_ligne_bronze_garde_les_valeurs_du_fichier_telles_quelles(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)
    lues = lire_csv(chemin).lignes

    _, lignes = _bronze_du_fichier(contrat, chemin)

    assert [ligne.valeurs for ligne in lignes] == lues       # y compris une température absente : un champ vide, bronze ne corrige rien


def test_les_valeurs_suivent_l_ordre_des_colonnes_du_contrat(contrat, entete, ligne_conforme):
    inverse = list(reversed(entete))
    fichier = FichierLu(colonnes=inverse, lignes=[{nom: ligne_conforme[nom] for nom in inverse}], lignes_malformees=())
    ligne_registre = _registre_accepte()

    [ligne] = construire_lignes_bronze(contrat, ligne_registre, fichier)

    assert list(ligne.valeurs) == entete


# ==============================================================================
# 2. 🔤 TOUT EST DU TEXTE : un Parquet arrive typé, bronze le copie en texte
# ==============================================================================
def test_les_valeurs_typees_d_un_parquet_deviennent_du_texte_et_l_absence_reste_absente(contrat, entete, ligne_conforme):
    typee = dict(ligne_conforme, date_production=date(2026, 11, 1), quantite_produite=1000, temperature_c=None)
    fichier = FichierLu(colonnes=entete, lignes=[typee], lignes_malformees=())

    [ligne] = construire_lignes_bronze(contrat, _registre_accepte(), fichier)

    assert ligne.valeurs["date_production"] == "2026-11-01"
    assert ligne.valeurs["quantite_produite"] == "1000"
    assert ligne.valeurs["temperature_c"] is None            # une absence n'est pas le texte « None »
    assert all(valeur is None or isinstance(valeur, str) for valeur in ligne.valeurs.values())


# ==============================================================================
# 3. ⚖️ BRONZE NE JUGE PAS : les lignes invalides y entrent, la livraison bloquée aussi
# ==============================================================================
def test_les_lignes_rejetees_par_le_contrat_sont_quand_meme_gardees(tmp_path, contrat_sans_seuil):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 2})

    ligne_registre, lignes = _bronze_du_fichier(contrat_sans_seuil, chemin)

    assert (ligne_registre.statut, ligne_registre.nb_rejetees) == ("ACCEPTEE_AVEC_REJETS", 2)
    assert len(lignes) == NB_LIGNES                           # les deux lignes hors plage sont là, copiées sans être jugées


def test_une_livraison_bloquee_est_gardee(tmp_path, contrat_strict):
    chemin = _ecrire(tmp_path, contrat_strict, anomalies={CodeErreur.HORS_PLAGE: 2})

    ligne_registre, lignes = _bronze_du_fichier(contrat_strict, chemin)

    assert ligne_registre.statut == "BLOQUEE"
    assert len(lignes) == NB_LIGNES


def test_une_ligne_malformee_n_entre_pas_au_bronze(tmp_path, contrat, entete):
    """Une ligne coupée ne dit pas à quelle colonne va chaque valeur : on ne peut pas la ranger. Le contrat la rejette."""
    chemin = tmp_path / NOM
    chemin.write_text(",".join(entete) + "\nLOT-000001,L1,2026-11-01,1000,10,200.0\nLOT-000002,L1,2026-11-01\n", encoding="utf-8")

    ligne_registre, lignes = _bronze_du_fichier(contrat, chemin)

    assert ligne_registre.nb_recues == 2                      # elle est bien arrivée...
    assert [ligne.numero_ligne for ligne in lignes] == [1]    # ...mais seule la ligne bien formée est rangée


# ==============================================================================
# 4. 🚫 CE QUI N'ENTRE PAS : refus, conflit, rejeu
# ==============================================================================
def test_une_livraison_refusee_pour_sa_structure_n_entre_pas(tmp_path, contrat, entete):
    chemin = tmp_path / NOM
    chemin.write_text(",".join(entete[:-1]) + "\nLOT-000001,L1,2026-11-01,1000,10\n", encoding="utf-8")     # il manque une colonne

    ligne_registre, lignes = _bronze_du_fichier(contrat, chemin)

    assert ligne_registre.statut == "REFUSEE"
    assert lignes == []


def test_un_nom_invalide_n_entre_pas(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat, nom="donnees.csv")

    ligne_registre, lignes = _bronze_du_fichier(contrat, chemin)

    assert (ligne_registre.statut, lignes) == ("REFUSEE", [])


def test_un_rejeu_n_entre_pas_deux_fois(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)
    premiere, premier_bronze = _bronze_du_fichier(contrat, chemin)

    rejeu, bronze_du_rejeu = _bronze_du_fichier(contrat, chemin, registre=[premiere])

    assert len(premier_bronze) == NB_LIGNES
    assert (rejeu.statut, bronze_du_rejeu) == ("REJEU_IGNORE", [])


def test_un_conflit_n_entre_pas(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)
    premiere, _ = _bronze_du_fichier(contrat, chemin)
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES + 5, GRAINE + 1), chemin)      # autres données, même nom

    conflit, lignes = _bronze_du_fichier(contrat, chemin, registre=[premiere])

    assert (conflit.statut, lignes) == ("REFUSEE", [])


# ==============================================================================
# 5. 🧱 LA FORME DES LIGNES
# ==============================================================================
def test_une_ligne_bronze_ne_se_modifie_pas(contrat, entete, ligne_conforme):
    fichier = FichierLu(colonnes=entete, lignes=[ligne_conforme], lignes_malformees=())
    [ligne] = construire_lignes_bronze(contrat, _registre_accepte(), fichier)

    assert isinstance(ligne, LigneBronze)
    with pytest.raises(AttributeError):
        ligne.numero_ligne = 99


# ==============================================================================
# 6. 🔎 POINTS RELEVÉS À LA REVUE
# ==============================================================================
def test_un_fichier_qui_ne_correspond_pas_au_contrat_donne_une_erreur_claire(contrat, entete, ligne_conforme):
    """Une ligne de registre ACCEPTEE avec un fichier auquel il manque une colonne : un appel incohérent, pas un `KeyError` obscur."""
    fichier = FichierLu(colonnes=entete[:-1], lignes=[{nom: ligne_conforme[nom] for nom in entete[:-1]}], lignes_malformees=())

    with pytest.raises(ValueError, match="colonnes"):
        construire_lignes_bronze(contrat, _registre_accepte(), fichier)


def test_le_rang_compte_les_lignes_bien_formees_et_pas_les_lignes_du_fichier(tmp_path, contrat, entete):
    """Limite assumée : une ligne coupée décale le rang d'après (la 4e ligne du fichier est le rang 2). Ce n'est pas un numéro de ligne."""
    chemin = tmp_path / NOM
    chemin.write_text(
        ",".join(entete) + "\nLOT-000001,L1,2026-11-01,1000,10,200.0\nLOT-000002,L1,2026-11-01\nLOT-000003,L1,2026-11-01,1000,10,200.0\n",
        encoding="utf-8",
    )

    _, lignes = _bronze_du_fichier(contrat, chemin)

    assert [(ligne.numero_ligne, ligne.valeurs["id_lot"]) for ligne in lignes] == [(1, "LOT-000001"), (2, "LOT-000003")]


def test_un_renvoi_corrige_d_une_livraison_bloquee_entre_sous_une_autre_heure_d_arrivee(tmp_path, contrat_strict):
    """Une livraison bloquée n'est pas « intégrée » : la renvoyer corrigée sous le même nom n'est pas un conflit. Les deux versions sont au
    bronze, distinguées par l'heure d'arrivée ; c'est le statut du registre qui dira laquelle le silver doit lire."""
    chemin = _ecrire(tmp_path, contrat_strict, anomalies={CodeErreur.HORS_PLAGE: 2})
    bloquee, bronze_bloque = _bronze_du_fichier(contrat_strict, chemin)
    ecrire_livraison_csv(contrat_strict, generer_livraison(contrat_strict, NB_LIGNES, GRAINE), chemin)          # corrigé, même nom
    plus_tard = datetime(2026, 11, 12, 9, 0, 0, tzinfo=UTC)

    renvoi, bronze_renvoye = _bronze_du_fichier(contrat_strict, chemin, registre=[bloquee], arrivee=plus_tard)

    assert (bloquee.statut, renvoi.statut) == ("BLOQUEE", "ACCEPTEE")
    assert {ligne.heure_arrivee for ligne in bronze_bloque} == {ARRIVEE}
    assert {ligne.heure_arrivee for ligne in bronze_renvoye} == {plus_tard}


def test_un_nouveau_statut_doit_etre_classe_explicitement():
    """Chaque statut de livraison entre au bronze OU est refusé, jamais « oublié » : un statut ajouté plus tard fait échouer ce test."""
    assert {statut.value for statut in StatutLivraison} == STATUTS_BRONZE | {StatutLivraison.REFUSEE.value}


def test_un_nan_parquet_est_copie_en_texte_et_reste_lu_comme_absent(contrat, entete, ligne_conforme):
    fichier = FichierLu(colonnes=entete, lignes=[dict(ligne_conforme, temperature_c=float("nan"))], lignes_malformees=())

    [ligne] = construire_lignes_bronze(contrat, _registre_accepte(), fichier)

    assert ligne.valeurs["temperature_c"] == "nan"
    assert est_absente(ligne.valeurs["temperature_c"])        # le contrat lit « nan » comme une absence : rien n'est perdu au silver
