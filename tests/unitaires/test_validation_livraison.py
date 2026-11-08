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
# 🧰 FABRIQUE DE LIVRAISONS : lignes toutes distinctes (clé = id_lot), valides ou non
# ==============================================================================
def _livraison(modele, nb_valides, nb_invalides=0):
    """`nb_valides` lignes conformes puis `nb_invalides` lignes violant UNE règle (quantité négative). Clés toutes distinctes."""
    lignes = []
    for numero in range(1, nb_valides + nb_invalides + 1):
        ligne = dict(modele, id_lot=f"LOT-{numero:06d}")
        if numero > nb_valides:
            ligne["quantite_produite"] = -1
            ligne["quantite_rebut"] = 0
        lignes.append(ligne)
    return lignes


# ==============================================================================
# 1. 📦 CAS DE BASE
# ==============================================================================
def test_une_livraison_vide_est_acceptee(contrat):
    """Un export sans ligne (en-tête seul) est une livraison valide, pas une erreur."""
    resultat = valider_livraison(contrat, [])
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.nb_recues == 0
    assert resultat.taux_rejet == 0.0


def test_une_livraison_conforme_est_acceptee_et_garde_l_ordre(contrat, ligne_conforme):
    lignes = _livraison(ligne_conforme, nb_valides=5)
    resultat = valider_livraison(contrat, lignes)
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.lignes_valides == lignes
    assert resultat.lignes_rejetees == []


def test_le_resultat_porte_la_version_du_contrat_appliquee(contrat, ligne_conforme):
    """Traçabilité : on doit pouvoir dire avec QUELLE version du contrat une livraison a été jugée."""
    assert valider_livraison(contrat, _livraison(ligne_conforme, 1)).version_contrat == contrat.version


# ==============================================================================
# 2. 🚦 SEUIL DE REJET : la frontière est le cas qui compte
# ==============================================================================
@pytest.mark.parametrize(
    ("nb_invalides", "statut_attendu"),
    [
        pytest.param(0, StatutLivraison.ACCEPTEE, id="seuil_2pct-rejets_0pct-aucun-rejet"),
        pytest.param(1, StatutLivraison.ACCEPTEE_AVEC_REJETS, id="seuil_2pct-rejets_1pct-sous-le-seuil"),
        pytest.param(2, StatutLivraison.ACCEPTEE_AVEC_REJETS, id="seuil_2pct-rejets_2pct-pile-sur-le-seuil-tolere"),
        pytest.param(3, StatutLivraison.BLOQUEE, id="seuil_2pct-rejets_3pct-au-dessus-bloque"),
    ],
)
def test_le_seuil_du_contrat_decide_du_statut(contrat, ligne_conforme, nb_invalides, statut_attendu):
    resultat = valider_livraison(contrat, _livraison(ligne_conforme, 100 - nb_invalides, nb_invalides))
    assert resultat.statut == statut_attendu


def test_avec_un_seuil_a_zero_une_seule_ligne_rejetee_bloque(contrat, ligne_conforme):
    contrat_strict = replace(contrat, taux_rejet_max=0.0)
    assert valider_livraison(contrat_strict, _livraison(ligne_conforme, 100)).statut == StatutLivraison.ACCEPTEE
    assert valider_livraison(contrat_strict, _livraison(ligne_conforme, 99, 1)).statut == StatutLivraison.BLOQUEE


@pytest.mark.parametrize("seuil", [0.07, 0.29, 0.58])
def test_la_frontiere_ne_depend_pas_des_arrondis_des_flottants(contrat, ligne_conforme, seuil):
    """0.29 * 100 vaut 28.999999999999996 en flottant : comparer `rejets > seuil * total` bloquerait à tort 29 / 100."""
    nb_au_seuil = round(seuil * 100)
    contrat_seuil = replace(contrat, taux_rejet_max=seuil)
    au_seuil = valider_livraison(contrat_seuil, _livraison(ligne_conforme, 100 - nb_au_seuil, nb_au_seuil))
    au_dessus = valider_livraison(contrat_seuil, _livraison(ligne_conforme, 99 - nb_au_seuil, nb_au_seuil + 1))
    assert au_seuil.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    assert au_dessus.statut == StatutLivraison.BLOQUEE


def test_le_taux_de_rejet_est_rejetees_sur_recues(contrat, ligne_conforme):
    resultat = valider_livraison(contrat, _livraison(ligne_conforme, 3, 1))
    assert resultat.nb_recues == 4
    assert resultat.taux_rejet == 0.25


# ==============================================================================
# 3. 🛑 UNE LIVRAISON BLOQUÉE N'INTÈGRE RIEN, MAIS RESTE DIAGNOSTIQUABLE
# ==============================================================================
def test_une_livraison_bloquee_n_integre_aucune_ligne(contrat, ligne_conforme):
    resultat = valider_livraison(contrat, _livraison(ligne_conforme, 90, 10))
    assert resultat.statut == StatutLivraison.BLOQUEE
    assert resultat.lignes_valides == []            # tout ou rien : les 90 lignes saines n'entrent pas non plus
    assert len(resultat.lignes_rejetees) == 10      # mais on sait exactement ce qui a posé problème


def test_une_livraison_acceptee_avec_rejets_integre_les_valides_et_garde_les_rejets(contrat, ligne_conforme):
    resultat = valider_livraison(contrat, _livraison(ligne_conforme, 99, 1))
    assert resultat.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    assert len(resultat.lignes_valides) == 99
    assert len(resultat.lignes_rejetees) == 1
    assert resultat.lignes_rejetees[0].erreurs, "une ligne rejetée doit toujours dire pourquoi"


# ==============================================================================
# 4. 🔁 DOUBLONS DE CLÉ PRIMAIRE (id_lot)
# ==============================================================================
def test_un_doublon_exact_est_reduit_a_une_ligne_sans_rejet(contrat, ligne_conforme):
    """Rejeu technique (reprise réseau) : même ligne deux fois. Aucune information perdue, donc ni rejet ni alerte."""
    resultat = valider_livraison(contrat, [ligne_conforme, dict(ligne_conforme)])
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.lignes_valides == [ligne_conforme]
    assert resultat.lignes_rejetees == []
    assert resultat.nb_doublons_exacts == 1


def test_un_doublon_exact_compte_dans_les_lignes_recues_mais_pas_dans_les_rejets(contrat, ligne_conforme):
    lignes = _livraison(ligne_conforme, 98, 1) + [dict(_livraison(ligne_conforme, 1)[0])]   # 1 rejet + 1 rejeu de LOT-000001
    resultat = valider_livraison(contrat, lignes)
    assert resultat.nb_recues == 100
    assert resultat.nb_doublons_exacts == 1
    assert resultat.taux_rejet == 0.01              # 1 rejet / 100 reçues : le rejeu ne gonfle pas le taux


def test_meme_cle_et_contenu_different_met_la_seconde_ligne_en_quarantaine(contrat, ligne_conforme):
    """On ne sait pas laquelle est juste : on garde la première (déjà arrivée) et la seconde est rejetée, jamais fusionnée."""
    concurrente = dict(ligne_conforme, quantite_produite=2000)
    contrat_tolerant = replace(contrat, taux_rejet_max=1.0)   # isole la règle de doublon du seuil de rejet
    resultat = valider_livraison(contrat_tolerant, [ligne_conforme, concurrente])
    assert resultat.lignes_valides == [ligne_conforme]
    assert [rejet.ligne for rejet in resultat.lignes_rejetees] == [concurrente]
    assert any("conflit" in erreur and "LOT-000001" in erreur for erreur in resultat.lignes_rejetees[0].erreurs)


def test_une_ligne_invalide_ne_reserve_pas_sa_cle(contrat, ligne_conforme):
    """Une ligne rejetée n'est pas « arrivée » : si une version valide de la même clé suit, elle doit être acceptée."""
    invalide = dict(ligne_conforme, quantite_produite=-1, quantite_rebut=0)
    contrat_tolerant = replace(contrat, taux_rejet_max=1.0)
    resultat = valider_livraison(contrat_tolerant, [invalide, ligne_conforme])
    assert resultat.lignes_valides == [ligne_conforme]
    assert [rejet.ligne for rejet in resultat.lignes_rejetees] == [invalide]


def test_la_meme_ligne_invalide_deux_fois_est_rejetee_deux_fois(contrat, ligne_conforme):
    invalide = dict(ligne_conforme, quantite_produite=-1, quantite_rebut=0)
    contrat_tolerant = replace(contrat, taux_rejet_max=1.0)
    resultat = valider_livraison(contrat_tolerant, [invalide, dict(invalide)])
    assert len(resultat.lignes_rejetees) == 2
    assert resultat.nb_doublons_exacts == 0         # un doublon exact ne concerne que les lignes VALIDES
