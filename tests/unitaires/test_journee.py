"""Tests de la JOURNÉE MULTI-USINES : des livraisons de trois pays jouées dans l'ordre sur le vrai enchaînement, l'état s'accumulant.

Chaque issue d'une livraison est déjà testée seule dans `test_pipeline.py`, avec l'état passé à la main. Ici on vérifie ce qu'une livraison
seule ne montre pas : l'état cumulé d'une journée, les invariants sur toute la journée, et la journée REJOUÉE sur l'état déjà cumulé.

L'ATTENDU de chaque livraison est écrit dans `journee.py` d'après sa recette (« 3 lignes hors plage sur 100 »), jamais lu dans un résultat
du pipeline : un test compare le pipeline à cet attendu, la journée n'est donc pas son propre oracle.
"""
import copy
from dataclasses import replace
from datetime import UTC, date, timedelta
from pathlib import Path

import pytest

from rd_manuf.alertes import CODE_LIVRAISON_BLOQUEE, Alerte, charger_catalogue
from rd_manuf.contrat import CodeErreur, StatutLivraison
from rd_manuf.journee import Attendu, Etat, jouer, journee_multi_usines, verifier_invariants
from rd_manuf.nom_fichier import NomFichierInvalide, lire_nom_fichier
from rd_manuf.registre import STATUT_REJEU
from rd_manuf.structure import CodeStructure

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"
JOUR = date(2026, 11, 12)
UN_JOUR = timedelta(days=1)

# La COMPOSITION de la journée, dans l'ordre d'arrivée : ce que la journée doit contenir, écrit ici à part de `journee.py`.
ISSUES = [
    "fra-aa-001-saine",
    "fra-aa-002-parquet-saine",
    "deu-ba-001-avec-rejets",
    "deu-ba-002-parquet-saine",
    "usa-ca-001-bloquee",
    "usa-ca-001-renvoyee-corrigee",
    "fra-aa-001-rejeu",
    "fra-aa-001-conflit-de-livraison",
    "fra-aa-003-conflit-de-cle",
    "nom-invalide",
    "fra-aa-004-colonne-manquante",
    "fra-aa-005-fichier-vide",
    "deu-ba-003-fichier-illisible",
    "deu-ba-004-parquet-colonne-inconnue",
    "deu-ba-005-parquet-colonne-dupliquee",
    "usa-ca-002-lignes-coupees",
    "usa-cb-001-memes-numeros-autre-usine",
]
TOUTES_LES_ISSUES = pytest.mark.parametrize("etiquette", ISSUES)


@pytest.fixture(scope="module")
def catalogue():
    return charger_catalogue(CHEMIN_CATALOGUE)


@pytest.fixture(scope="module")
def journee(contrat):
    return journee_multi_usines(contrat)


@pytest.fixture(scope="module")
def jouee(contrat, catalogue, journee, tmp_path_factory):
    etat = Etat()
    resultats = jouer(contrat, catalogue, journee, tmp_path_factory.mktemp("journee"), etat)
    return etat, resultats


@pytest.fixture(scope="module")
def rejouee(contrat, catalogue, journee, tmp_path_factory):
    """La même journée, puis rejouée le lendemain sur l'état déjà cumulé."""
    dossier = tmp_path_factory.mktemp("rejeu")
    etat = Etat()
    premiere = jouer(contrat, catalogue, journee, dossier, etat)
    seconde = jouer(contrat, catalogue, journee, dossier, etat, decalage=UN_JOUR)
    return etat, premiere, seconde


def _par_etiquette(journee, resultats):
    return {livraison.etiquette: (livraison, resultat) for livraison, resultat in zip(journee, resultats, strict=True)}


# ==============================================================================
# 1. 🗓️ LA COMPOSITION : une issue par livraison, dans l'ordre d'arrivée
# ==============================================================================
def test_la_journee_contient_les_issues_prevues_dans_l_ordre(journee):
    assert [livraison.etiquette for livraison in journee] == ISSUES


def test_les_livraisons_arrivent_ce_jour_la_en_ordre_strictement_croissant(journee):
    arrivees = [livraison.arrivee for livraison in journee]

    assert arrivees == sorted(set(arrivees)) and len(arrivees) == len(ISSUES)
    assert {arrivee.date() for arrivee in arrivees} == {JOUR}
    assert all(arrivee.utcoffset() == timedelta(0) and arrivee.tzinfo is UTC for arrivee in arrivees)


def test_la_journee_couvre_tous_les_statuts_et_tous_les_motifs_de_structure(journee):
    statuts = {livraison.attendu.statut for livraison in journee}
    alertes = {code for livraison in journee for code in livraison.attendu.alertes}

    assert statuts == {statut.value for statut in StatutLivraison} | {STATUT_REJEU}
    assert alertes >= {code.value for code in CodeStructure}
    codes_de_lignes = {CodeErreur.HORS_PLAGE.value, CodeErreur.CLE_EN_CONFLIT.value, CodeErreur.LIGNE_MALFORMEE.value}
    assert alertes >= {CODE_LIVRAISON_BLOQUEE} | codes_de_lignes


def test_la_journee_a_trois_pays_et_plusieurs_usines_et_lignes(journee):
    noms = [livraison.nom.split("_") for livraison in journee if livraison.nom.startswith("livraison_production_")]

    assert {nom[2] for nom in noms} == {"FRA", "DEU", "USA"}
    assert {(nom[2], nom[3]) for nom in noms} >= {("FRA", "AA"), ("DEU", "BA"), ("USA", "CA")}
    assert {(nom[2], nom[3], nom[4]) for nom in noms if nom[2] == "FRA" and nom[3] == "AA"} >= {("FRA", "AA", "001"), ("FRA", "AA", "002")}


# ==============================================================================
# 2. ✅ CHAQUE LIVRAISON REÇOIT L'ISSUE PRÉVUE, l'état s'accumulant au fil de la journée
# ==============================================================================
@TOUTES_LES_ISSUES
def test_chaque_livraison_recoit_l_issue_prevue(journee, jouee, etiquette):
    par_etiquette = _par_etiquette(journee, jouee[1])
    assert etiquette in par_etiquette, f"la journée ne contient pas « {etiquette} »"
    livraison, resultat = par_etiquette[etiquette]

    assert Attendu.observe(resultat) == livraison.attendu


def test_le_registre_a_une_ligne_par_livraison_dans_l_ordre_d_arrivee(journee, jouee):
    etat, _ = jouee

    assert [ligne.livraison_id for ligne in etat.registre] == [livraison.nom for livraison in journee]
    assert [ligne.heure_arrivee for ligne in etat.registre] == [livraison.arrivee for livraison in journee]


def test_la_premiere_arrivee_fait_foi_pour_un_conflit_de_cle(journee, jouee):
    """FRA_AA_003 reprend les numéros de FRA_AA_001 avec un autre contenu : c'est elle, arrivée plus tard, qui part en quarantaine."""
    par_etiquette = _par_etiquette(journee, jouee[1])
    ancienne, tardive = par_etiquette["fra-aa-001-saine"][1], par_etiquette["fra-aa-003-conflit-de-cle"][1]

    assert ancienne.silver.quarantaine == [] and len(ancienne.silver.lignes) > 0
    assert tardive.silver.lignes == [] and len(tardive.silver.quarantaine) > 0
    assert ancienne.ligne_registre.heure_arrivee < tardive.ligne_registre.heure_arrivee
    for ligne in tardive.silver.quarantaine:
        messages = [violation.message for violation in ligne.violations]
        assert all(ancienne.ligne_registre.livraison_id in message for message in messages), "l'origine doit être nommée"


def test_les_memes_numeros_de_lot_dans_une_autre_usine_ne_se_gênent_pas(journee, jouee):
    par_etiquette = _par_etiquette(journee, jouee[1])
    francaise, allemande = par_etiquette["fra-aa-001-saine"][1], par_etiquette["deu-ba-001-avec-rejets"][1]
    numeros = lambda resultat: {ligne.valeurs["id_lot"] for ligne in resultat.silver.lignes}  # noqa: E731

    assert numeros(francaise) & numeros(allemande), "la recette doit réutiliser des numéros de lot, sinon ce test ne prouve rien"
    assert allemande.silver.lignes and not any(a.code == CodeErreur.CLE_EN_CONFLIT.value for a in allemande.alertes)


def test_les_memes_numeros_de_lot_dans_une_autre_usine_du_meme_pays_ne_se_gênent_pas(journee, jouee):
    par_etiquette = _par_etiquette(journee, jouee[1])
    premiere, seconde = par_etiquette["usa-ca-001-renvoyee-corrigee"][1], par_etiquette["usa-cb-001-memes-numeros-autre-usine"][1]
    numeros = lambda resultat: {ligne.valeurs["id_lot"] for ligne in resultat.silver.lignes}  # noqa: E731

    assert numeros(premiere) == numeros(seconde) and numeros(seconde), "les deux usines doivent réutiliser les mêmes numéros"
    assert {ligne.pays for ligne in premiere.silver.lignes} == {ligne.pays for ligne in seconde.silver.lignes}
    assert seconde.silver.quarantaine == [] and Attendu.observe(seconde).alertes == ()


def test_l_instant_ecrit_dans_un_nom_precede_l_arrivee_de_la_livraison(journee):
    for livraison in journee:
        try:
            nom = lire_nom_fichier(livraison.nom)
        except NomFichierInvalide:
            continue
        assert nom.horodatage <= livraison.arrivee, livraison.etiquette


# ==============================================================================
# 3. 📐 LES INVARIANTS DE LA JOURNÉE
# ==============================================================================
def test_la_journee_respecte_ses_invariants(contrat, catalogue, jouee):
    etat, resultats = jouee

    assert verifier_invariants(contrat, catalogue, etat, resultats) == []


def _sans_la_premiere_ligne_bronze(etat, resultats):
    etat.lignes_bronze.pop(0)


def _cle_silver_en_double(etat, resultats):
    premiere = etat.lignes_silver[0]
    etat.lignes_silver.append(replace(premiere, livraison_id="autre-livraison.csv"))


def _silver_d_une_livraison_bloquee(etat, resultats):
    bloquee = next(ligne for ligne in etat.registre if ligne.statut == StatutLivraison.BLOQUEE.value)
    etat.lignes_silver.append(replace(etat.lignes_silver[0], livraison_id=bloquee.livraison_id, heure_arrivee=bloquee.heure_arrivee))


def _registre_incomplet(etat, resultats):
    etat.registre.pop()


def _alerte_sur_un_rejeu(etat, resultats):
    rejeu = next(i for i, resultat in enumerate(resultats) if resultat.ligne_registre.statut == STATUT_REJEU)
    alerte = Alerte("x", CodeErreur.HORS_PLAGE.value, 1, "emetteur", (), ())
    resultats[rejeu] = replace(resultats[rejeu], alertes=[alerte])


def _alerte_sur_une_livraison_saine(etat, resultats):
    saine = next(i for i, resultat in enumerate(resultats) if resultat.ligne_registre.statut == "ACCEPTEE" and not resultat.alertes)
    alerte = Alerte("x", CodeErreur.HORS_PLAGE.value, 1, "emetteur", (), ())
    resultats[saine] = replace(resultats[saine], alertes=[alerte])


def _code_d_alerte_inconnu(etat, resultats):
    avec_alerte = next(i for i, resultat in enumerate(resultats) if resultat.alertes)
    alertes = [replace(alerte, code="CODE_INCONNU_DU_CATALOGUE") for alerte in resultats[avec_alerte].alertes]
    resultats[avec_alerte] = replace(resultats[avec_alerte], alertes=alertes)


@pytest.mark.parametrize(
    ("corruption", "mot_attendu"),
    [
        pytest.param(_sans_la_premiere_ligne_bronze, "conservation", id="une-ligne-bronze-perdue"),
        pytest.param(_cle_silver_en_double, "doublon", id="un-lot-deux-fois-au-silver"),
        pytest.param(_silver_d_une_livraison_bloquee, "silver", id="silver-d-une-livraison-bloquee"),
        pytest.param(_registre_incomplet, "registre", id="une-livraison-sans-ligne-de-registre"),
        pytest.param(_alerte_sur_un_rejeu, "rejeu", id="alerte-sur-un-rejeu"),
        pytest.param(_alerte_sur_une_livraison_saine, "saine", id="alerte-sur-une-livraison-saine"),
        pytest.param(_code_d_alerte_inconnu, "catalogue", id="code-d-alerte-absent-du-catalogue"),
    ],
)
def test_chaque_invariant_voit_sa_propre_violation(contrat, catalogue, jouee, corruption, mot_attendu):
    """Les invariants ne sont pas vides : chacun doit se déclencher quand on abîme l'état ou les résultats qu'il garde."""
    etat, resultats = copy.deepcopy(jouee[0]), list(jouee[1])

    corruption(etat, resultats)
    violations = verifier_invariants(contrat, catalogue, etat, resultats)

    assert any(mot_attendu in violation for violation in violations), violations


def _cle(ligne):
    return ligne.livraison_id, ligne.heure_arrivee


def _integree(etat):
    return next(ligne for ligne in etat.registre if ligne.statut == StatutLivraison.ACCEPTEE.value)


def _refusee(etat):
    return next(ligne for ligne in etat.registre if ligne.statut == StatutLivraison.REFUSEE.value)


def _lot_inedit(ligne_silver, cible):
    """Une ligne silver rattachée à `cible`, avec un numéro de lot que personne n'a : elle ne déclenche pas l'invariant des doublons."""
    return replace(ligne_silver, livraison_id=cible.livraison_id, heure_arrivee=cible.heure_arrivee,
                   valeurs={**ligne_silver.valeurs, "id_lot": "LOT-999999"})


def _une_ligne_de(lignes, cible):
    return next(ligne for ligne in lignes if _cle(ligne) == _cle(cible))


def _bronze_et_silver_en_moins(etat, resultats):
    cible = _integree(etat)
    etat.lignes_bronze.remove(_une_ligne_de(etat.lignes_bronze, cible))
    etat.lignes_silver.remove(_une_ligne_de(etat.lignes_silver, cible))


def _bronze_et_silver_en_trop(etat, resultats):
    cible = _integree(etat)
    etat.lignes_bronze.append(_une_ligne_de(etat.lignes_bronze, cible))
    etat.lignes_silver.append(_lot_inedit(_une_ligne_de(etat.lignes_silver, cible), cible))


def _silver_en_moins(etat, resultats):
    etat.lignes_silver.remove(_une_ligne_de(etat.lignes_silver, _integree(etat)))


def _silver_en_trop(etat, resultats):
    cible = _integree(etat)
    etat.lignes_silver.append(_lot_inedit(_une_ligne_de(etat.lignes_silver, cible), cible))


def _bronze_d_une_refusee(etat, resultats):
    cible = _refusee(etat)
    etat.lignes_bronze.append(replace(etat.lignes_bronze[0], livraison_id=cible.livraison_id, heure_arrivee=cible.heure_arrivee))


def _ligne_coupee_d_une_refusee(etat, resultats):
    cible = _refusee(etat)
    etat.lignes_coupees.append(replace(etat.lignes_coupees[0], livraison_id=cible.livraison_id, heure_arrivee=cible.heure_arrivee))


def _silver_d_une_refusee(etat, resultats):
    cible = _refusee(etat)
    etat.lignes_silver.append(_lot_inedit(etat.lignes_silver[0], cible))


def _quarantaine_d_une_refusee(etat, resultats):
    cible = _refusee(etat)
    etat.quarantaine.append(replace(etat.quarantaine[0], livraison_id=cible.livraison_id, heure_arrivee=cible.heure_arrivee))


@pytest.mark.parametrize(
    ("corruption", "mot_attendu"),
    [
        pytest.param(_bronze_et_silver_en_moins, "mais en garde", id="bronze-en-moins"),
        pytest.param(_bronze_et_silver_en_trop, "mais en garde", id="bronze-en-trop"),
        pytest.param(_silver_en_moins, "au bronze mais", id="silver-en-moins"),
        pytest.param(_silver_en_trop, "au bronze mais", id="silver-en-trop"),
        pytest.param(_bronze_d_une_refusee, "n'entre pas", id="bronze-d-une-livraison-refusee"),
        pytest.param(_ligne_coupee_d_une_refusee, "n'entre pas", id="ligne-coupee-d-une-livraison-refusee"),
        pytest.param(_silver_d_une_refusee, "n'est pas intégrée", id="silver-d-une-livraison-refusee"),
        pytest.param(_quarantaine_d_une_refusee, "n'est pas intégrée", id="quarantaine-d-une-livraison-refusee"),
    ],
)
def test_chaque_controle_de_conservation_se_declenche_seul(contrat, catalogue, jouee, corruption, mot_attendu):
    """Chaque corruption n'abîme QU'UN contrôle (l'autre côté est compensé) : si un contrôle disparaît, rien d'autre ne le remplace."""
    etat, resultats = copy.deepcopy(jouee[0]), list(jouee[1])

    corruption(etat, resultats)
    violations = verifier_invariants(contrat, catalogue, etat, resultats)

    assert violations and all(mot_attendu in violation for violation in violations), violations


def test_les_invariants_ne_modifient_ni_l_etat_ni_les_resultats(contrat, catalogue, jouee):
    etat, resultats = copy.deepcopy(jouee[0]), list(jouee[1])

    verifier_invariants(contrat, catalogue, etat, resultats)

    assert etat == jouee[0] and resultats == list(jouee[1])


# ==============================================================================
# 4. ♻️ LA JOURNÉE REJOUÉE le lendemain sur l'état cumulé : rien ne s'ajoute à tort
# ==============================================================================
@TOUTES_LES_ISSUES
def test_chaque_livraison_rejouee_recoit_l_issue_prevue_au_rejeu(journee, rejouee, etiquette):
    par_etiquette = _par_etiquette(journee, rejouee[2])
    assert etiquette in par_etiquette, f"la journée ne contient pas « {etiquette} »"
    livraison, resultat = par_etiquette[etiquette]

    assert Attendu.observe(resultat) == livraison.attendu_au_rejeu


def test_la_premiere_version_bloquee_rejouee_apres_sa_version_corrigee_est_un_conflit_de_livraison(journee, rejouee):
    """Le même nom est maintenant intégré avec un autre contenu : ce n'est pas un rejeu, c'est un conflit, et l'émetteur est prévenu."""
    resultat = _par_etiquette(journee, rejouee[2])["usa-ca-001-bloquee"][1]

    assert resultat.ligne_registre.statut == StatutLivraison.REFUSEE.value
    assert [alerte.code for alerte in resultat.alertes] == [CodeStructure.LIVRAISON_EN_CONFLIT.value]


def test_rejouer_la_journee_n_ajoute_rien_aux_couches_de_donnees(rejouee):
    """Silver, quarantaine et bronze ne bougent pas : seul le registre garde une trace de chaque livraison revenue."""
    etat, premiere, seconde = rejouee
    nb_silver = sum(len(resultat.silver.lignes) for resultat in premiere)
    nb_bronze = sum(len(resultat.lignes_bronze) for resultat in premiere)

    assert len(etat.lignes_silver) == nb_silver and len(etat.lignes_bronze) == nb_bronze
    assert len(etat.registre) == len(premiere) + len(seconde)
    assert not any(resultat.lignes_bronze or resultat.silver.lignes or resultat.lignes_coupees for resultat in seconde)


def test_la_journee_rejouee_respecte_encore_ses_invariants(contrat, catalogue, rejouee):
    etat, premiere, seconde = rejouee

    assert verifier_invariants(contrat, catalogue, etat, [*premiere, *seconde]) == []


def test_le_rejeu_arrive_le_lendemain(journee, rejouee):
    etat, _, seconde = rejouee

    assert [resultat.ligne_registre.heure_arrivee for resultat in seconde] == [livraison.arrivee + UN_JOUR for livraison in journee]
    assert len(etat.registre) == 2 * len(journee)


# ==============================================================================
# 5. 🧱 L'ÉTAT : ce qu'il garde, et ce qu'il ne partage pas
# ==============================================================================
def test_l_etat_garde_chaque_couche_de_chaque_resultat(jouee):
    etat, resultats = jouee

    assert etat.registre == [resultat.ligne_registre for resultat in resultats]
    assert etat.lignes_bronze == [ligne for resultat in resultats for ligne in resultat.lignes_bronze]
    assert etat.lignes_coupees == [ligne for resultat in resultats for ligne in resultat.lignes_coupees]
    assert etat.lignes_silver == [ligne for resultat in resultats for ligne in resultat.silver.lignes]
    assert etat.quarantaine == [ligne for resultat in resultats for ligne in resultat.silver.quarantaine]
    assert etat.alertes == [alerte for resultat in resultats for alerte in resultat.alertes]


def test_ajouter_un_resultat_a_l_etat_ne_le_partage_pas(jouee):
    resultat = next(resultat for resultat in jouee[1] if resultat.silver.lignes and resultat.alertes)
    avant = (list(resultat.silver.lignes), list(resultat.alertes), list(resultat.lignes_bronze))
    etat = Etat()

    etat.appliquer(resultat)
    for liste in (etat.lignes_silver, etat.alertes, etat.lignes_bronze, etat.registre):
        liste.clear()

    assert (resultat.silver.lignes, resultat.alertes, resultat.lignes_bronze) == avant, "vider l'état ne doit pas vider le résultat"
    assert all(avant), "le résultat choisi doit avoir de quoi être partagé, sinon ce test ne prouve rien"


def test_deux_etats_neufs_ne_partagent_aucune_liste():
    premier, second = Etat(), Etat()

    premier.registre.append("x")

    assert second.registre == []


def test_la_meme_journee_deux_fois_sur_un_etat_neuf_donne_le_meme_etat(contrat, catalogue, journee, tmp_path):
    etats = []
    for numero in range(2):
        etat = Etat()
        jouer(contrat, catalogue, journee, tmp_path / f"jour-{numero}", etat)
        etats.append(etat)

    assert etats[0] == etats[1]
    assert len(etats[0].registre) == len(ISSUES)


# ==============================================================================
# 6. 🎚️ LA JOURNÉE SUIT LE SEUIL DU CONTRAT : ses nombres de rejets en sont dérivés, pas recopiés
# ==============================================================================
@pytest.mark.parametrize("taux", [0.01, 0.015, 0.02, 0.05, 0.10, 0.29], ids=lambda taux: f"seuil-{taux}")
def test_la_journee_tient_pour_un_autre_seuil_de_rejet(contrat, catalogue, tmp_path, taux):
    contrat_variante = replace(contrat, taux_rejet_max=taux)
    livraisons = journee_multi_usines(contrat_variante)
    etat = Etat()

    resultats = jouer(contrat_variante, catalogue, livraisons, tmp_path, etat)

    assert [Attendu.observe(resultat) for resultat in resultats] == [livraison.attendu for livraison in livraisons]
    assert verifier_invariants(contrat_variante, catalogue, etat, resultats) == []
    # Le bord du seuil, lu dans les résultats et non dans le module : la livraison « avec rejets » est au plus haut nombre de rejets que le
    # seuil tolère, la livraison bloquée un seul rejet au-dessus.
    par_etiquette = _par_etiquette(livraisons, resultats)
    avec, bloquee = par_etiquette["deu-ba-001-avec-rejets"][1].reception, par_etiquette["usa-ca-001-bloquee"][1].reception
    assert avec.nb_rejetees / avec.nb_recues <= taux < (avec.nb_rejetees + 1) / avec.nb_recues
    assert bloquee.nb_rejetees == avec.nb_rejetees + 1


def test_un_seuil_qui_ne_laisse_passer_aucun_rejet_est_refuse_clairement(contrat_strict):
    with pytest.raises(ValueError, match="seuil"):
        journee_multi_usines(contrat_strict)
