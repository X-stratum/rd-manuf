"""Tests de la validation d'une LIVRAISON (cycle B) : doublons, seuil de rejet, statut final.

Trois statuts possibles :
    ACCEPTEE               aucune ligne rejetée
    ACCEPTEE_AVEC_REJETS   des rejets, mais le taux reste dans le seuil du contrat (rejets mis en quarantaine)
    BLOQUEE                taux de rejet au-dessus du seuil : RIEN n'entre en silver (tout ou rien)
"""
from dataclasses import replace

import pytest

from rd_manuf.contrat import StatutLivraison, valider_livraison


# ==============================================================================
# 1. 📦 CAS DE BASE
# ==============================================================================
def test_une_livraison_vide_est_acceptee(contrat):
    """Un export sans ligne (en-tête seul) est une livraison valide, pas une erreur."""
    resultat = valider_livraison(contrat, [])
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.nb_recues == 0
    assert resultat.taux_rejet == 0.0


def test_une_livraison_conforme_est_acceptee_et_garde_l_ordre(contrat, fabrique):
    lignes = fabrique.valides(5)
    resultat = valider_livraison(contrat, lignes)
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.lignes_valides == lignes
    assert resultat.lignes_rejetees == []


def test_le_resultat_porte_la_version_du_contrat_appliquee(contrat, fabrique):
    """Traçabilité : on doit pouvoir dire avec QUELLE version du contrat une livraison a été jugée."""
    assert valider_livraison(contrat, fabrique.valides(1)).version_contrat == contrat.version


# ==============================================================================
# 2. 🚦 SEUIL DE REJET : la frontière est le cas qui compte
# ==============================================================================
# Cette table VÉRIFIE le seuil réel du contrat (2 %) : ses chiffres restent en clair.
@pytest.mark.parametrize(
    ("nb_invalides", "statut_attendu"),
    [
        pytest.param(0, StatutLivraison.ACCEPTEE, id="seuil_2pct-rejets_0pct-aucun-rejet"),
        pytest.param(1, StatutLivraison.ACCEPTEE_AVEC_REJETS, id="seuil_2pct-rejets_1pct-sous-le-seuil"),
        pytest.param(2, StatutLivraison.ACCEPTEE_AVEC_REJETS, id="seuil_2pct-rejets_2pct-pile-sur-le-seuil-tolere"),
        pytest.param(3, StatutLivraison.BLOQUEE, id="seuil_2pct-rejets_3pct-au-dessus-bloque"),
    ],
)
def test_le_seuil_du_contrat_decide_du_statut(contrat, fabrique, nb_invalides, statut_attendu):
    resultat = valider_livraison(contrat, fabrique.livraison(100 - nb_invalides, nb_invalides))
    assert resultat.statut == statut_attendu


def test_avec_un_seuil_a_zero_une_seule_ligne_rejetee_bloque(contrat_strict, fabrique):
    assert valider_livraison(contrat_strict, fabrique.livraison(100)).statut == StatutLivraison.ACCEPTEE
    assert valider_livraison(contrat_strict, fabrique.livraison(99, 1)).statut == StatutLivraison.BLOQUEE


@pytest.mark.parametrize("seuil", [0.07, 0.29, 0.58])
def test_la_frontiere_ne_depend_pas_des_arrondis_des_flottants(contrat, fabrique, seuil):
    """0.29 * 100 vaut 28.999999999999996 en flottant : comparer `rejets > seuil * total` bloquerait à tort 29 / 100."""
    nb_au_seuil = round(seuil * 100)
    contrat_seuil = replace(contrat, taux_rejet_max=seuil)
    au_seuil = valider_livraison(contrat_seuil, fabrique.livraison(100 - nb_au_seuil, nb_au_seuil))
    au_dessus = valider_livraison(contrat_seuil, fabrique.livraison(99 - nb_au_seuil, nb_au_seuil + 1))
    assert au_seuil.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    assert au_dessus.statut == StatutLivraison.BLOQUEE


def test_le_taux_de_rejet_est_rejetees_sur_recues(contrat_sans_seuil, fabrique):
    resultat = valider_livraison(contrat_sans_seuil, fabrique.livraison(3, 1))
    assert resultat.nb_recues == 4
    assert resultat.taux_rejet == 0.25


# ==============================================================================
# 3. 🛑 UNE LIVRAISON BLOQUÉE N'INTÈGRE RIEN, MAIS RESTE DIAGNOSTIQUABLE
# ==============================================================================
def test_une_livraison_bloquee_n_integre_aucune_ligne(contrat_strict, fabrique):
    resultat = valider_livraison(contrat_strict, fabrique.livraison(90, 10))
    assert resultat.statut == StatutLivraison.BLOQUEE
    assert resultat.lignes_valides == []            # tout ou rien : les 90 lignes saines n'entrent pas non plus
    assert len(resultat.lignes_rejetees) == 10      # mais on sait exactement ce qui a posé problème


def test_une_livraison_acceptee_avec_rejets_integre_les_valides_et_garde_les_rejets(contrat_sans_seuil, fabrique):
    resultat = valider_livraison(contrat_sans_seuil, fabrique.livraison(99, 1))
    assert resultat.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    assert len(resultat.lignes_valides) == 99
    assert len(resultat.lignes_rejetees) == 1
    assert resultat.lignes_rejetees[0].erreurs, "une ligne rejetée doit toujours dire pourquoi"


# ==============================================================================
# 4. 🔁 DOUBLONS DE CLÉ PRIMAIRE (id_lot)
# ==============================================================================
def test_un_doublon_exact_est_reduit_a_une_ligne_sans_rejet(contrat, fabrique):
    """Rejeu technique (reprise réseau) : même ligne deux fois. Aucune information perdue, donc ni rejet ni alerte."""
    ligne = fabrique.valide()
    resultat = valider_livraison(contrat, [ligne, dict(ligne)])
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.lignes_valides == [ligne]
    assert resultat.lignes_rejetees == []
    assert resultat.nb_doublons_exacts == 1


def test_un_doublon_exact_compte_dans_les_lignes_recues_mais_pas_dans_les_rejets(contrat, fabrique):
    lignes = fabrique.livraison(98, 1) + [dict(fabrique.valide(1))]   # 1 rejet + 1 rejeu de la première ligne
    resultat = valider_livraison(contrat, lignes)
    assert resultat.nb_recues == 100
    assert resultat.nb_doublons_exacts == 1
    assert resultat.taux_rejet == 0.01              # 1 rejet / 100 reçues : le rejeu ne gonfle pas le taux


def test_meme_cle_et_contenu_different_met_la_seconde_ligne_en_quarantaine(contrat_sans_seuil, fabrique):
    """On ne sait pas laquelle est juste : on garde la première (déjà arrivée) et la seconde est rejetée, jamais fusionnée."""
    ligne = fabrique.valide()
    concurrente = fabrique.concurrente(ligne)
    resultat = valider_livraison(contrat_sans_seuil, [ligne, concurrente])
    assert resultat.lignes_valides == [ligne]
    assert [rejet.ligne for rejet in resultat.lignes_rejetees] == [concurrente]
    assert any("conflit" in erreur and "LOT-000001" in erreur for erreur in resultat.lignes_rejetees[0].erreurs)


def test_une_ligne_invalide_ne_reserve_pas_sa_cle(contrat_sans_seuil, fabrique):
    """Une ligne rejetée n'est pas « arrivée » : si une version valide de la même clé suit, elle doit être acceptée."""
    invalide, valide = fabrique.hors_plage(1), fabrique.valide(1)   # même clé primaire
    resultat = valider_livraison(contrat_sans_seuil, [invalide, valide])
    assert resultat.lignes_valides == [valide]
    assert [rejet.ligne for rejet in resultat.lignes_rejetees] == [invalide]


def test_la_meme_ligne_invalide_deux_fois_est_rejetee_deux_fois(contrat_sans_seuil, fabrique):
    invalide = fabrique.hors_plage(1)
    resultat = valider_livraison(contrat_sans_seuil, [invalide, dict(invalide)])
    assert len(resultat.lignes_rejetees) == 2
    assert resultat.nb_doublons_exacts == 0         # un doublon exact ne concerne que les lignes VALIDES


# ==============================================================================
# 5. 🧬 « IDENTIQUE » SE JUGE APRÈS NORMALISATION PAR LE CONTRAT, PAS SUR LA FORME BRUTE
# ==============================================================================
@pytest.mark.parametrize("absence", [None, "", "NA", "null", float("nan")], ids=["None", "vide", "NA", "null", "NaN-reel"])
def test_toutes_les_formes_d_absence_d_une_colonne_optionnelle_sont_equivalentes(contrat, fabrique, absence):
    """Deux NaN créés séparément ne sont jamais égaux en Python (nan != nan) : un rejeu ne doit pas devenir un « conflit »."""
    premiere = dict(fabrique.valide(), temperature_c=None)
    rejeu = dict(premiere, temperature_c=absence)
    resultat = valider_livraison(contrat, [premiere, rejeu])
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.nb_doublons_exacts == 1


def test_deux_nan_reels_crees_separement_forment_un_doublon_exact(contrat, fabrique):
    premiere = dict(fabrique.valide(), temperature_c=float("nan"))
    rejeu = dict(fabrique.valide(), temperature_c=float("nan"))
    resultat = valider_livraison(contrat, [premiere, rejeu])
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.nb_doublons_exacts == 1


@pytest.mark.parametrize(
    ("colonne", "autre_ecriture"),
    [
        ("quantite_produite", lambda valeur: str(valeur)),            # 1000 -> "1000"
        ("id_ligne", lambda valeur: f" {valeur} "),                   # "L1" -> " L1 "
        ("temperature_c", lambda valeur: str(int(valeur))),           # 200.0 -> "200"
    ],
    ids=["entier-en-texte", "espaces-autour", "decimal-en-texte"],
)
def test_une_autre_ecriture_de_la_meme_valeur_est_un_doublon_exact(contrat, fabrique, colonne, autre_ecriture):
    """L'autre écriture est calculée depuis la valeur de la ligne modèle : aucune valeur du contrat n'est recopiée ici."""
    ligne = fabrique.valide()
    rejeu = dict(ligne, **{colonne: autre_ecriture(ligne[colonne])})
    resultat = valider_livraison(contrat, [ligne, rejeu])
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.nb_doublons_exacts == 1
