"""Tests des codes d'erreur STABLES : un rejet doit pouvoir être compté, filtré et routé sans lire de texte libre."""
import pytest

from rd_manuf.contrat import CodeErreur, controler_ligne, diagnostiquer_ligne, valider_livraison

CODES_ATTENDUS = {
    "OBLIGATOIRE_MANQUANT",
    "TYPE_INVALIDE",
    "HORS_PLAGE",
    "MOTIF_NON_RESPECTE",
    "VALEUR_HORS_DOMAINE",
    "REGLE_ENTRE_COLONNES",
    "CLE_EN_CONFLIT",
}


# ==============================================================================
# 1. 🔒 LES CODES SONT UN CONTRAT : les renommer casse les consommateurs (alertes, tableaux de bord)
# ==============================================================================
def test_les_codes_d_erreur_sont_exactement_ceux_attendus():
    assert {code.value for code in CodeErreur} == CODES_ATTENDUS


def test_la_valeur_d_un_code_est_son_nom():
    assert all(code.name == code.value for code in CodeErreur)


# ==============================================================================
# 2. 🏷️ CHAQUE VIOLATION PORTE LE BON CODE ET LA COLONNE FAUTIVE
#    Ce test VÉRIFIE le contrat : ses valeurs restent en clair, toutes dans cette table (à relire si le contrat évolue).
# ==============================================================================
@pytest.mark.parametrize(
    ("colonne", "valeur", "code"),
    [
        ("id_lot", "LOT-12", CodeErreur.MOTIF_NON_RESPECTE),
        ("id_lot", "lot-000001", CodeErreur.MOTIF_NON_RESPECTE),
        ("id_ligne", "L9", CodeErreur.VALEUR_HORS_DOMAINE),
        ("date_production", "01/11/2026", CodeErreur.TYPE_INVALIDE),
        ("date_production", None, CodeErreur.OBLIGATOIRE_MANQUANT),
        ("quantite_produite", -1, CodeErreur.HORS_PLAGE),
        ("quantite_produite", 100001, CodeErreur.HORS_PLAGE),
        ("quantite_produite", "beaucoup", CodeErreur.TYPE_INVALIDE),
        ("quantite_produite", 10.5, CodeErreur.TYPE_INVALIDE),
        ("quantite_rebut", "NA", CodeErreur.OBLIGATOIRE_MANQUANT),
        ("temperature_c", 401, CodeErreur.HORS_PLAGE),
        ("temperature_c", -51, CodeErreur.HORS_PLAGE),
        ("temperature_c", "inf", CodeErreur.TYPE_INVALIDE),
    ],
)
def test_chaque_violation_porte_le_bon_code_et_la_colonne_fautive(contrat, ligne_conforme, colonne, valeur, code):
    ligne_conforme[colonne] = valeur
    concernees = [violation for violation in diagnostiquer_ligne(contrat, ligne_conforme) if violation.code == code]
    assert concernees, f"{colonne}={valeur!r} aurait dû produire {code.value}"
    assert any(colonne in violation.colonnes for violation in concernees)


def test_la_regle_entre_colonnes_porte_son_code_et_les_deux_colonnes(contrat, ligne_conforme):
    ligne_conforme["quantite_rebut"] = ligne_conforme["quantite_produite"] + 1
    violations = diagnostiquer_ligne(contrat, ligne_conforme)
    assert [violation.code for violation in violations] == [CodeErreur.REGLE_ENTRE_COLONNES]
    assert set(violations[0].colonnes) == {"quantite_rebut", "quantite_produite"}


def test_une_ligne_conforme_n_a_aucune_violation(contrat, ligne_conforme):
    assert diagnostiquer_ligne(contrat, ligne_conforme) == []


def test_les_messages_lisibles_restent_disponibles_et_identiques(contrat, ligne_conforme):
    """`controler_ligne` (texte pour un humain) et `diagnostiquer_ligne` (codes pour une machine) décrivent les mêmes violations."""
    ligne_conforme["id_ligne"] = "L9"
    ligne_conforme["quantite_produite"] = -5
    assert controler_ligne(contrat, ligne_conforme) == [violation.message for violation in diagnostiquer_ligne(contrat, ligne_conforme)]


# ==============================================================================
# 3. 📦 LES CODES TRAVERSENT LA VALIDATION D'UNE LIVRAISON
# ==============================================================================
def test_une_ligne_rejetee_garde_ses_violations_et_ses_messages(contrat_sans_seuil, fabrique):
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.hors_plage(1)])
    rejet = resultat.lignes_rejetees[0]
    assert [violation.code for violation in rejet.violations] == [CodeErreur.HORS_PLAGE]
    assert rejet.erreurs == tuple(violation.message for violation in rejet.violations)


def test_un_conflit_de_cle_porte_son_code_et_les_colonnes_de_la_cle(contrat_sans_seuil, fabrique):
    ligne = fabrique.valide()
    resultat = valider_livraison(contrat_sans_seuil, [ligne, fabrique.concurrente(ligne)])
    violation = resultat.lignes_rejetees[0].violations[0]
    assert violation.code == CodeErreur.CLE_EN_CONFLIT
    assert violation.colonnes == ("id_lot",)
