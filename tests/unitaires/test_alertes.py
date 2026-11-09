"""Tests du catalogue d'alertes et du routage : qui doit AGIR, qui doit être INFORMÉ (jamais des personnes : des RÔLES).

Les adresses des personnes ne sont PAS dans Git (dépôt public) : elles vivront dans un annuaire hors du dépôt.
"""
from pathlib import Path

import pytest
import yaml

from rd_manuf.alertes import CODE_LIVRAISON_BLOQUEE, Alerte, CatalogueInvalide, charger_catalogue, router_alertes
from rd_manuf.contrat import CodeErreur, StatutLivraison, valider_livraison

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"


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
    ],
)
def test_le_routage_suit_la_decision_prise(catalogue, code, agit):
    assert catalogue.regle(code).agit == agit


def test_le_metier_est_toujours_informe(catalogue):
    assert "metier" in catalogue.toujours_informes


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
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.deux_colonnes_hors_plage(1)])
    alertes = router_alertes(catalogue, resultat, livraison_id="LIV-001")
    assert len(alertes) == 1
    assert alertes[0].nb_lignes == 1                                         # une LIGNE, pas deux violations
    assert alertes[0].colonnes == ("quantite_produite", "temperature_c")     # mais les deux colonnes à corriger sont listées


def test_deux_codes_differents_font_deux_alertes_triees_par_code(contrat_sans_seuil, catalogue, fabrique):
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.hors_domaine(2), fabrique.hors_plage(1)])
    alertes = router_alertes(catalogue, resultat, livraison_id="LIV-001")
    assert [alerte.code for alerte in alertes] == ["HORS_PLAGE", "VALEUR_HORS_DOMAINE"]


def test_celui_qui_agit_n_est_pas_aussi_liste_comme_informe(contrat_sans_seuil, catalogue, fabrique):
    """VALEUR_HORS_DOMAINE : le métier agit ET est « toujours informé » : il ne doit apparaître qu'une fois (dans `agit`)."""
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.hors_domaine(1)])
    alerte = router_alertes(catalogue, resultat, livraison_id="LIV-001")[0]
    assert alerte.agit == "metier"
    assert alerte.informes == ()


def test_une_ligne_a_deux_codes_differents_alimente_chaque_alerte(contrat_sans_seuil, catalogue, fabrique):
    """Garde-fou : une ligne qui cumule deux codes ne doit en perdre aucun, et chaque alerte ne liste que SA colonne."""
    resultat = valider_livraison(contrat_sans_seuil, [fabrique.deux_codes_differents(1)])
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
