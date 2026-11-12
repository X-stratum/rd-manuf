"""Tests du contrôle de STRUCTURE : une livraison est-elle lisible avant même de regarder ses lignes ?

Décision : une structure invalide REFUSE la livraison entière (aucune ligne n'est lue). Ce module ne fait que DIRE ce qui ne va pas ;
le statut REFUSEE et le registre viennent au ticket suivant.
Les noms de colonnes viennent du contrat : si le contrat change, ces tests suivent.
"""
import pytest

from rd_manuf.structure import CodeStructure, controler_structure

NB_LIGNES = 5
COLONNE_EN_TROP = "colonne_en_trop"
NB_MAX_COLONNES_EN_TROP = 4
CODES_ATTENDUS = {
    "FICHIER_VIDE", "FICHIER_ILLISIBLE", "NOM_FICHIER_INVALIDE", "COLONNE_MANQUANTE", "COLONNE_INCONNUE", "COLONNE_DUPLIQUEE",
}


def _codes(problemes):
    return [probleme.code for probleme in problemes]


# ==============================================================================
# 1. 🔒 LES CODES SONT UN CONTRAT : les renommer casse les consommateurs (alertes, registre)
# ==============================================================================
def test_les_codes_de_structure_sont_exactement_ceux_attendus():
    assert {code.value for code in CodeStructure} == CODES_ATTENDUS


def test_la_valeur_d_un_code_de_structure_est_son_nom():
    assert all(code.name == code.value for code in CodeStructure)


# ==============================================================================
# 2. ✅ UNE STRUCTURE CONFORME
# ==============================================================================
def test_une_structure_conforme_n_a_aucun_probleme(contrat, entete):
    assert controler_structure(contrat, entete, NB_LIGNES) == []


def test_l_ordre_des_colonnes_n_a_pas_d_importance(contrat, entete):
    """Le CSV est lu par NOM de colonne : un émetteur qui réordonne ses colonnes ne casse rien."""
    assert controler_structure(contrat, list(reversed(entete)), NB_LIGNES) == []


# ==============================================================================
# 3. 📭 FICHIER VIDE
# ==============================================================================
def test_un_en_tete_sans_aucune_ligne_est_un_fichier_vide(contrat, entete):
    assert _codes(controler_structure(contrat, entete, 0)) == [CodeStructure.FICHIER_VIDE]


def test_sans_en_tete_ni_ligne_seul_fichier_vide_est_signale(contrat):
    """Pas d'en-tête : lister toutes les colonnes « manquantes » noierait le vrai problème."""
    assert _codes(controler_structure(contrat, [], 0)) == [CodeStructure.FICHIER_VIDE]


# ==============================================================================
# 4. 🕳️ COLONNE MANQUANTE : une par colonne du contrat
# ==============================================================================
def test_chaque_colonne_du_contrat_est_exigee(contrat, entete):
    for manquante in entete:
        recues = [nom for nom in entete if nom != manquante]
        problemes = controler_structure(contrat, recues, NB_LIGNES)
        assert [(p.code, p.colonnes) for p in problemes] == [(CodeStructure.COLONNE_MANQUANTE, (manquante,))], manquante


# ==============================================================================
# 5. ➕ COLONNE INCONNUE ET ➰ COLONNE DUPLIQUÉE
# ==============================================================================
def test_une_colonne_en_trop_est_signalee_avec_son_nom(contrat, entete):
    problemes = controler_structure(contrat, entete + [COLONNE_EN_TROP], NB_LIGNES)
    assert [(p.code, p.colonnes) for p in problemes] == [(CodeStructure.COLONNE_INCONNUE, (COLONNE_EN_TROP,))]


def test_une_colonne_dupliquee_est_signalee_une_seule_fois(contrat, entete):
    """Un nom répété est ambigu (quelle valeur lire ?) : il est signalé comme tel, pas comme manquant ni inconnu."""
    doublon = entete[0]
    problemes = controler_structure(contrat, entete + [doublon, doublon], NB_LIGNES)
    assert [(p.code, p.colonnes) for p in problemes] == [(CodeStructure.COLONNE_DUPLIQUEE, (doublon,))]


# ==============================================================================
# 5 bis. 🧺 PLUSIEURS COLONNES FAUTIVES DU MÊME CODE : UN SEUL problème qui les regroupe (une alerte par code, pas par colonne)
# ==============================================================================
def test_plusieurs_colonnes_manquantes_font_un_seul_probleme_dans_l_ordre_du_contrat(contrat, entete):
    """De 2 à N-1 colonnes retirées (retirer TOUTES les colonnes revient à un fichier sans en-tête, cas traité plus haut)."""
    for nb_manquantes in range(2, len(entete)):
        recues = list(reversed(entete[nb_manquantes:]))           # reçues à l'envers : l'ordre rendu est celui du contrat, pas de l'arrivée
        attendu = [(CodeStructure.COLONNE_MANQUANTE, tuple(entete[:nb_manquantes]))]
        assert [(p.code, p.colonnes) for p in controler_structure(contrat, recues, NB_LIGNES)] == attendu, nb_manquantes


def test_plusieurs_colonnes_en_trop_font_un_seul_probleme_dans_l_ordre_d_arrivee(contrat, entete):
    for nb_en_trop in range(2, NB_MAX_COLONNES_EN_TROP + 1):
        en_trop = [f"{COLONNE_EN_TROP}_{numero}" for numero in range(nb_en_trop)]
        problemes = controler_structure(contrat, entete + en_trop, NB_LIGNES)
        assert [(p.code, p.colonnes) for p in problemes] == [(CodeStructure.COLONNE_INCONNUE, tuple(en_trop))], nb_en_trop


def test_plusieurs_colonnes_dupliquees_font_un_seul_probleme(contrat, entete):
    for nb_doublons in range(2, len(entete) + 1):
        doublons = entete[:nb_doublons]
        problemes = controler_structure(contrat, entete + doublons, NB_LIGNES)
        assert [(p.code, p.colonnes) for p in problemes] == [(CodeStructure.COLONNE_DUPLIQUEE, tuple(doublons))], nb_doublons


# ==============================================================================
# 6. 🔠 LES NOMS SONT COMPARÉS À L'IDENTIQUE (casse, espaces) : pas de lecture « au mieux »
# ==============================================================================
@pytest.mark.parametrize(
    "transformer",
    [
        pytest.param(str.upper, id="majuscules"),
        pytest.param(lambda nom: f" {nom}", id="espace-devant"),
        pytest.param(lambda nom: f"{nom} ", id="espace-derriere"),
    ],
)
def test_un_nom_de_colonne_presque_juste_est_refuse(contrat, entete, transformer):
    recues = [transformer(entete[0])] + entete[1:]
    assert set(_codes(controler_structure(contrat, recues, NB_LIGNES))) == {CodeStructure.COLONNE_MANQUANTE, CodeStructure.COLONNE_INCONNUE}


# ==============================================================================
# 7. 🧮 PLUSIEURS PROBLÈMES : tous signalés d'un coup, dans un ordre fixe
# ==============================================================================
def test_tous_les_problemes_sont_signales_ensemble_tries_par_code(contrat, entete):
    """Une colonne renommée donne deux problèmes. Les montrer ensemble évite à l'émetteur de corriger puis de renvoyer cinq fois."""
    recues = [COLONNE_EN_TROP] + entete[1:]
    problemes = controler_structure(contrat, recues, NB_LIGNES)
    assert [(p.code, p.colonnes) for p in problemes] == [
        (CodeStructure.COLONNE_INCONNUE, (COLONNE_EN_TROP,)),
        (CodeStructure.COLONNE_MANQUANTE, (entete[0],)),
    ]


def test_un_fichier_vide_garde_aussi_ses_autres_problemes_quand_il_a_un_en_tete(contrat, entete):
    problemes = controler_structure(contrat, entete[1:], 0)
    assert set(_codes(problemes)) == {CodeStructure.FICHIER_VIDE, CodeStructure.COLONNE_MANQUANTE}


def test_un_changement_de_version_melange_colonnes_retirees_et_ajoutees(contrat, entete):
    """Cas réaliste : l'émetteur retire deux colonnes et en ajoute deux autres, l'une en tête, l'autre au milieu de l'en-tête.

    Les noms sont choisis pour que l'ordre du contrat (ou d'arrivée) soit DIFFÉRENT de l'ordre alphabétique : un tri parasite se verrait.
    """
    retirees = (entete[0], entete[2])
    nouvelles = (f"{COLONNE_EN_TROP}_b", f"{COLONNE_EN_TROP}_a")
    assert list(retirees) != sorted(retirees), "le contrat a changé : choisir deux colonnes retirées dont l'ordre n'est pas alphabétique"

    restantes = [nom for nom in entete if nom not in retirees]
    milieu = len(restantes) // 2
    recues = [nouvelles[0], *restantes[:milieu], nouvelles[1], *restantes[milieu:]]

    problemes = controler_structure(contrat, recues, NB_LIGNES)
    assert [(p.code, p.colonnes) for p in problemes] == [
        (CodeStructure.COLONNE_INCONNUE, nouvelles),
        (CodeStructure.COLONNE_MANQUANTE, retirees),
    ]


# ==============================================================================
# 8. 💬 CHAQUE PROBLÈME SE LIT PAR UN HUMAIN
# ==============================================================================
def test_le_message_nomme_les_colonnes_concernees(contrat, entete):
    probleme = controler_structure(contrat, entete[1:], NB_LIGNES)[0]
    assert entete[0] in probleme.message
