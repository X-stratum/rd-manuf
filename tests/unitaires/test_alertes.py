"""Tests du catalogue d'alertes et du routage : qui doit AGIR, qui doit être INFORMÉ (jamais des personnes : des RÔLES).

Les adresses des personnes ne sont PAS dans Git (dépôt public) : elles vivront dans un annuaire hors du dépôt.
"""
from pathlib import Path

import pytest
import yaml

from rd_manuf.alertes import (
    CODE_LIVRAISON_BLOQUEE,
    Alerte,
    CatalogueInvalide,
    charger_catalogue,
    router_alertes,
    router_alertes_reception,
)
from rd_manuf.contrat import CodeErreur, LigneMalformee, StatutLivraison, valider_livraison
from rd_manuf.reception import receptionner_livraison
from rd_manuf.simulateur import generer_livraison
from rd_manuf.structure import CodeStructure

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"
GRAINE = 42
NB_LIGNES = 20
NB_ANOMALIES = 2


@pytest.fixture(scope="session")
def catalogue():
    return charger_catalogue(CHEMIN_CATALOGUE)


def _catalogue_modifie(tmp_path, modification):
    """Copie le vrai catalogue, applique `modification` au dictionnaire, écrit le résultat et retourne son chemin."""
    brut = yaml.safe_load(CHEMIN_CATALOGUE.read_text(encoding="utf-8"))
    modification(brut)
    chemin = tmp_path / "catalogue.yaml"
    chemin.write_text(yaml.safe_dump(brut, allow_unicode=True), encoding="utf-8")
    return chemin


# ==============================================================================
# 1. 📒 LE CATALOGUE : un code d'erreur = une règle de routage
# ==============================================================================
def test_chaque_code_d_erreur_a_une_regle_de_routage(catalogue):
    for code in CodeErreur:
        assert catalogue.regle(code).agit in catalogue.roles, code


@pytest.mark.parametrize(
    ("code", "agit"),
    [
        (CodeErreur.OBLIGATOIRE_MANQUANT, "emetteur"),
        (CodeErreur.TYPE_INVALIDE, "emetteur"),
        (CodeErreur.HORS_PLAGE, "emetteur"),
        (CodeErreur.MOTIF_NON_RESPECTE, "emetteur"),
        (CodeErreur.VALEUR_HORS_DOMAINE, "metier"),     # seul le métier sait si une nouvelle valeur est légitime
        (CodeErreur.REGLE_ENTRE_COLONNES, "emetteur"),
        (CodeErreur.CLE_EN_CONFLIT, "emetteur"),        # lui seul sait quelle ligne est la bonne
        (CodeErreur.LIGNE_MALFORMEE, "emetteur"),       # lui seul peut renvoyer une ligne entière
    ],
)
def test_le_routage_suit_la_decision_prise(catalogue, code, agit):
    assert catalogue.regle(code).agit == agit


def test_le_metier_est_toujours_informe(catalogue):
    assert "metier" in catalogue.toujours_informes


def test_chaque_code_de_structure_a_une_regle_de_routage(catalogue):
    for code in CodeStructure:
        assert catalogue.regle_structure(code).agit in catalogue.roles, code


@pytest.mark.parametrize("code", list(CodeStructure), ids=lambda code: code.value)
def test_un_refus_de_structure_est_a_corriger_par_l_emetteur_avec_l_equipe_data_en_copie(catalogue, code):
    """Lui seul peut renvoyer un fichier à la bonne structure ; l'équipe data suit : le refus peut venir d'une nouvelle version."""
    regle = catalogue.regle_structure(code)
    assert regle.agit == "emetteur"
    assert "equipe_data" in regle.informe


# ==============================================================================
# 2. 🧯 CATALOGUE MAL FORMÉ : refusé au chargement, avec un message qui dit quoi corriger
# ==============================================================================
@pytest.mark.parametrize(
    ("description", "modification"),
    [
        ("code d'erreur sans règle", lambda brut: brut["codes"].pop("HORS_PLAGE")),
        ("règle pour un code qui n'existe pas", lambda brut: brut["codes"].update(CODE_FANTOME={"agit": "emetteur"})),
        ("rôle inconnu dans agit", lambda brut: brut["codes"]["HORS_PLAGE"].update(agit="fantome")),
        ("agit absent", lambda brut: brut["codes"]["HORS_PLAGE"].pop("agit")),
        ("rôle inconnu dans informe", lambda brut: brut["codes"]["HORS_PLAGE"].update(informe=["fantome"])),
        ("rôle inconnu dans toujours_informes", lambda brut: brut.update(toujours_informes=["fantome"])),
        ("livraison_bloquee absente", lambda brut: brut.pop("livraison_bloquee")),
        ("roles n'est pas une liste", lambda brut: brut.update(roles="metier")),
        ("codes n'est pas un dictionnaire", lambda brut: brut.update(codes=[])),
        ("agit est une liste", lambda brut: brut["codes"]["HORS_PLAGE"].update(agit=["emetteur"])),
        ("informe contient une liste", lambda brut: brut["codes"]["HORS_PLAGE"].update(informe=[["metier"]])),
        ("toujours_informes contient un dictionnaire", lambda brut: brut.update(toujours_informes=[{"a": 1}])),
        ("code de structure sans règle", lambda brut: brut["structure"].pop("FICHIER_VIDE")),
        ("règle pour un code de structure qui n'existe pas", lambda brut: brut["structure"].update(CODE_FANTOME={"agit": "emetteur"})),
        ("rôle inconnu dans agit d'un code de structure", lambda brut: brut["structure"]["FICHIER_VIDE"].update(agit="fantome")),
        ("section structure absente", lambda brut: brut.pop("structure")),
        ("structure n'est pas un dictionnaire", lambda brut: brut.update(structure=[])),
    ],
)
def test_un_catalogue_mal_forme_est_refuse(tmp_path, description, modification):
    with pytest.raises(CatalogueInvalide):
        charger_catalogue(_catalogue_modifie(tmp_path, modification))


def test_un_yaml_illisible_est_refuse(tmp_path):
    chemin = tmp_path / "casse.yaml"
    chemin.write_text("roles: [pas fermé\n  codes: : :", encoding="utf-8")
    with pytest.raises(CatalogueInvalide):
        charger_catalogue(chemin)


@pytest.mark.parametrize(
    "contenu",
    [
        pytest.param("", id="fichier-vide"),
        pytest.param("- emetteur\n- metier\n", id="liste"),
        pytest.param("juste du texte", id="texte-seul"),
    ],
)
def test_un_yaml_lisible_mais_qui_n_est_pas_un_dictionnaire_est_refuse(tmp_path, contenu):
    """Un fichier vide donne `None` : il doit produire CatalogueInvalide, pas un TypeError incompréhensible."""
    chemin = tmp_path / "pas_un_dictionnaire.yaml"
    chemin.write_text(contenu, encoding="utf-8")
    with pytest.raises(CatalogueInvalide):
        charger_catalogue(chemin)


def test_les_cles_inconnues_sont_ignorees_pour_permettre_des_extensions_futures(tmp_path):
    """Ex. : ajouter plus tard `escalade_vers` ne doit casser aucun chargeur existant."""

    def ajouter(brut):
        brut["futur"] = 1
        brut["codes"]["HORS_PLAGE"]["escalade_vers"] = "metier"

    assert charger_catalogue(_catalogue_modifie(tmp_path, ajouter)).regle(CodeErreur.HORS_PLAGE).agit == "emetteur"


# ==============================================================================
# 3. 🚦 ROUTAGE : une alerte par (livraison, code), jamais une par ligne
# ==============================================================================
def test_une_livraison_acceptee_ne_produit_aucune_alerte(contrat, catalogue, fabrique):
    resultat = valider_livraison(contrat, fabrique.valides(10))
    assert router_alertes(catalogue, resultat, livraison_id="LIV-001") == []


def test_un_rejeu_technique_ne_produit_aucune_alerte(contrat, catalogue, fabrique):
    """Décision : même ligne deux fois dans la même livraison = reprise réseau, ni rejet ni alerte."""
    ligne = fabrique.valide()
    resultat = valider_livraison(contrat, [ligne, dict(ligne)])
    assert resultat.nb_doublons_exacts == 1
    assert router_alertes(catalogue, resultat, livraison_id="LIV-001") == []


def test_un_rejet_isole_alerte_l_emetteur_et_informe_le_metier(contrat_sans_seuil, catalogue, fabrique):
    resultat = valider_livraison(contrat_sans_seuil, fabrique.livraison(nb_valides=3, nb_hors_plage=1))   # le seuil est hors sujet ici
    assert router_alertes(catalogue, resultat, livraison_id="LIV-001") == [
        Alerte(livraison_id="LIV-001", code="HORS_PLAGE", nb_lignes=1, agit="emetteur", informes=("metier",), colonnes=("temperature_c",))
    ]


def test_plusieurs_lignes_de_meme_code_font_une_seule_alerte_avec_un_compteur(contrat_sans_seuil, catalogue, fabrique):
    nb_rejets = 5
    resultat = valider_livraison(contrat_sans_seuil, fabrique.livraison(nb_valides=3, nb_hors_plage=nb_rejets))
    alertes = router_alertes(catalogue, resultat, livraison_id="LIV-001")
    assert len(alertes) == 1
    assert alertes[0].nb_lignes == nb_rejets


def test_une_ligne_a_deux_violations_du_meme_code_compte_pour_une_ligne(contrat_sans_seuil, catalogue, fabrique):
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.valide(1), fabrique.deux_colonnes_hors_plage(2)])
    alertes = router_alertes(catalogue, resultat, livraison_id="LIV-001")
    assert len(alertes) == 1
    assert alertes[0].nb_lignes == 1                                         # une LIGNE, pas deux violations
    assert alertes[0].colonnes == ("quantite_produite", "temperature_c")     # mais les deux colonnes à corriger sont listées


def test_deux_codes_differents_font_deux_alertes_triees_par_code(contrat_sans_seuil, catalogue, fabrique):
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.valide(1), fabrique.hors_domaine(3), fabrique.hors_plage(2)])
    alertes = router_alertes(catalogue, resultat, livraison_id="LIV-001")
    assert [alerte.code for alerte in alertes] == ["HORS_PLAGE", "VALEUR_HORS_DOMAINE"]


def test_celui_qui_agit_n_est_pas_aussi_liste_comme_informe(contrat_sans_seuil, catalogue, fabrique):
    """VALEUR_HORS_DOMAINE : le métier agit ET est « toujours informé » : il ne doit apparaître qu'une fois (dans `agit`)."""
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.valide(1), fabrique.hors_domaine(2)])
    alerte = router_alertes(catalogue, resultat, livraison_id="LIV-001")[0]
    assert alerte.agit == "metier"
    assert alerte.informes == ()


def test_une_ligne_a_deux_codes_differents_alimente_chaque_alerte(contrat_sans_seuil, catalogue, fabrique):
    """Garde-fou : une ligne qui cumule deux codes ne doit en perdre aucun, et chaque alerte ne liste que SA colonne."""
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.valide(1), fabrique.deux_codes_differents(2)])
    alertes = router_alertes(catalogue, resultat, livraison_id="LIV-001")
    assert [(alerte.code, alerte.nb_lignes, alerte.colonnes) for alerte in alertes] == [
        ("HORS_PLAGE", 1, ("temperature_c",)),
        ("VALEUR_HORS_DOMAINE", 1, ("id_ligne",)),
    ]


def test_un_conflit_de_cle_alerte_l_emetteur(contrat_sans_seuil, catalogue, fabrique):
    ligne = fabrique.valide()
    resultat = valider_livraison(contrat_sans_seuil, [ligne, fabrique.concurrente(ligne)])
    alerte = router_alertes(catalogue, resultat, livraison_id="LIV-001")[0]
    assert (alerte.code, alerte.agit, alerte.colonnes) == ("CLE_EN_CONFLIT", "emetteur", ("id_lot",))


def test_une_ligne_malformee_alerte_l_emetteur_et_informe_l_equipe_data_et_le_metier(contrat_sans_seuil, catalogue, fabrique):
    """Aucune colonne précise à corriger : c'est la ligne entière qui est à renvoyer."""
    malformee = LigneMalformee(numero=3, champs=("LOT-000002",))
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.valide(1)], [malformee])
    (alerte,) = router_alertes(catalogue, resultat, livraison_id="LIV-001")
    assert (alerte.code, alerte.agit, alerte.nb_lignes, alerte.colonnes) == ("LIGNE_MALFORMEE", "emetteur", 1, ())
    assert alerte.informes == ("equipe_data", "metier")


# ==============================================================================
# 4. 🛑 LIVRAISON BLOQUÉE : le métier qualifie l'écart, l'émetteur reste informé de ce qu'il doit corriger
# ==============================================================================
def test_une_livraison_bloquee_alerte_le_metier_en_premier_puis_detaille_par_code(contrat_strict, catalogue, fabrique):
    nb_rejets = 2
    resultat = valider_livraison(contrat_strict, fabrique.livraison(nb_valides=3, nb_hors_plage=nb_rejets))   # seuil à 0 : bloquée
    assert resultat.statut == StatutLivraison.BLOQUEE
    alertes = router_alertes(catalogue, resultat, livraison_id="LIV-001")

    bloquee = alertes[0]
    assert bloquee.code == CODE_LIVRAISON_BLOQUEE
    assert bloquee.agit == "metier"
    assert bloquee.informes == ("emetteur", "equipe_data")
    assert bloquee.nb_lignes == nb_rejets

    assert [alerte.code for alerte in alertes[1:]] == ["HORS_PLAGE"]       # l'émetteur sait aussi QUOI corriger
    assert alertes[1].agit == "emetteur"


# ==============================================================================
# 5. 👥 INTÉGRER UN NOUVEL INTERLOCUTEUR : un rôle = une ligne dans le catalogue, aucune ligne de code
# ==============================================================================
def test_un_nouveau_role_informe_en_permanence_se_declare_sans_toucher_au_code(tmp_path, contrat_sans_seuil, fabrique):
    def ajouter_qualite(brut):
        brut["roles"].append("qualite")
        brut["toujours_informes"] = ["metier", "qualite"]

    catalogue_etendu = charger_catalogue(_catalogue_modifie(tmp_path, ajouter_qualite))
    resultat = valider_livraison(contrat_sans_seuil, fabrique.livraison(nb_valides=3, nb_hors_plage=1))
    alerte = router_alertes(catalogue_etendu, resultat, livraison_id="LIV-001")[0]
    assert alerte.informes == ("metier", "qualite")


# ==============================================================================
# 6. 🚫 LIVRAISON REFUSÉE : une alerte par code de structure (sinon un mauvais en-tête serait refusé EN SILENCE)
#    `nb_lignes` = lignes reçues et NON intégrées à cause du refus (aucune n'a été jugée) ; un fichier vide donne 0.
# ==============================================================================
def _reception(contrat, colonnes_recues, nb_lignes=NB_LIGNES, **options):
    return receptionner_livraison(contrat, "LIV-001", colonnes_recues, generer_livraison(contrat, nb_lignes, GRAINE, **options))


def test_une_colonne_manquante_alerte_l_emetteur_et_informe_l_equipe_data_et_le_metier(contrat, catalogue, entete):
    reception = _reception(contrat, entete[1:])
    assert router_alertes_reception(catalogue, reception) == [
        Alerte(
            livraison_id="LIV-001",
            code="COLONNE_MANQUANTE",
            nb_lignes=NB_LIGNES,
            agit="emetteur",
            informes=("equipe_data", "metier"),
            colonnes=(entete[0],),
        )
    ]


def test_un_fichier_sans_ligne_alerte_avec_zero_ligne_et_sans_colonne(contrat, catalogue, entete):
    reception = _reception(contrat, entete, nb_lignes=0)
    assert router_alertes_reception(catalogue, reception) == [
        Alerte("LIV-001", "FICHIER_VIDE", 0, "emetteur", ("equipe_data", "metier"), ())
    ]


def test_plusieurs_problemes_de_structure_font_une_alerte_par_code_sans_livraison_bloquee(contrat, catalogue, entete):
    reception = _reception(contrat, entete[1:] + ["colonne_en_trop"])
    codes = [alerte.code for alerte in router_alertes_reception(catalogue, reception)]
    assert codes == ["COLONNE_INCONNUE", "COLONNE_MANQUANTE"]      # triées ; LIVRAISON_BLOQUEE est réservée au dépassement du seuil


# ==============================================================================
# 7. ➡️ LIVRAISON NON REFUSÉE : le point d'entrée unique ne change rien au routage existant
# ==============================================================================
def test_une_livraison_acceptee_ne_produit_aucune_alerte_a_la_reception(contrat, catalogue, entete):
    reception = _reception(contrat, entete)
    assert reception.statut == StatutLivraison.ACCEPTEE
    assert router_alertes_reception(catalogue, reception) == []


def test_une_livraison_bloquee_est_routee_comme_avant(contrat_strict, catalogue, entete):
    reception = _reception(contrat_strict, entete, anomalies={CodeErreur.HORS_PLAGE: NB_ANOMALIES})
    assert reception.statut == StatutLivraison.BLOQUEE
    alertes = router_alertes_reception(catalogue, reception)
    assert alertes == router_alertes(catalogue, reception.resultat, "LIV-001")
    assert alertes[0].code == CODE_LIVRAISON_BLOQUEE                        # la comparaison ne doit pas se faire sur une liste vide


def test_une_livraison_acceptee_avec_rejets_est_routee_comme_avant(contrat_sans_seuil, catalogue, entete):
    """Le troisième statut « non refusé » : des rejets sous le seuil produisent leurs alertes, pas la branche des refus de structure."""
    reception = _reception(contrat_sans_seuil, entete, anomalies={CodeErreur.HORS_PLAGE: NB_ANOMALIES})
    assert reception.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    alertes = router_alertes_reception(catalogue, reception)
    assert alertes == router_alertes(catalogue, reception.resultat, "LIV-001")
    assert [alerte.code for alerte in alertes] == ["HORS_PLAGE"]
