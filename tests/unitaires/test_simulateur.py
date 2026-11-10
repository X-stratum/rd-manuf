"""Tests du simulateur de livraisons : il fabrique des livraisons conformes au contrat, avec des anomalies demandées par code.

Le VALIDATEUR existant sert d'oracle : on ne recopie aucune règle du contrat dans ces tests. Si le simulateur dit « 3 lignes HORS_PLAGE »,
`valider_livraison` doit en trouver exactement 3, et rien d'autre.
"""
import csv
from collections import Counter
from dataclasses import replace

import pytest

from rd_manuf.contrat import CodeErreur, StatutLivraison, valider_livraison
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison

GRAINE = 42
NB_LIGNES = 20
NB_GRAINES_VERIFIEES = 10
NB_ANOMALIES = 3
TOUS_LES_CODES = pytest.mark.parametrize("code", list(CodeErreur), ids=lambda code: code.value)


def _violations_par_code(resultat):
    return Counter(violation.code for rejet in resultat.lignes_rejetees for violation in rejet.violations)


# ==============================================================================
# 1. ✅ UNE LIVRAISON SAINE
# ==============================================================================
def test_une_livraison_saine_est_acceptee_sans_rejet_ni_doublon(contrat):
    resultat = valider_livraison(contrat, generer_livraison(contrat, 200, GRAINE))
    assert resultat.statut == StatutLivraison.ACCEPTEE
    assert resultat.nb_recues == 200
    assert resultat.nb_doublons_exacts == 0


def test_zero_ligne_donne_une_livraison_vide(contrat):
    assert generer_livraison(contrat, 0, GRAINE) == []


def test_le_rebut_ne_depasse_jamais_la_production_meme_quand_les_bornes_sont_serrees(contrat):
    """Bornes serrées : la frontière rebut == production est tirée très souvent, un défaut ne passe plus par chance."""
    colonnes = tuple(replace(c, maximum=1) if c.nom == "quantite_produite" else c for c in contrat.colonnes)
    serre = replace(contrat, colonnes=colonnes)
    for graine in range(NB_GRAINES_VERIFIEES):
        lignes = generer_livraison(serre, NB_LIGNES, graine)
        assert all(ligne["quantite_rebut"] <= ligne["quantite_produite"] for ligne in lignes), graine

# ==============================================================================
# 2. 🎲 REPRODUCTIBLE : même graine, même livraison (indispensable pour rejouer un incident)
# ==============================================================================
def test_la_meme_graine_donne_la_meme_livraison(contrat):
    assert generer_livraison(contrat, 50, GRAINE) == generer_livraison(contrat, 50, GRAINE)


def test_une_autre_graine_donne_une_autre_livraison(contrat):
    assert generer_livraison(contrat, 50, GRAINE) != generer_livraison(contrat, 50, GRAINE + 1)


# ==============================================================================
# 3. 💥 ANOMALIES DEMANDÉES PAR CODE : exactement celles-là, ni plus ni moins
# ==============================================================================
@TOUS_LES_CODES
def test_chaque_anomalie_demandee_produit_exactement_ce_code(contrat_sans_seuil, code):
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, anomalies={code: NB_ANOMALIES})
    resultat = valider_livraison(contrat_sans_seuil, lignes)
    assert len(lignes) == NB_LIGNES                        # les anomalies REMPLACENT des lignes saines, elles ne s'ajoutent pas
    assert len(resultat.lignes_rejetees) == NB_ANOMALIES
    assert set(_violations_par_code(resultat)) == {code}


def test_plusieurs_anomalies_de_codes_differents_se_combinent(contrat_sans_seuil):
    anomalies = {CodeErreur.HORS_PLAGE: 2, CodeErreur.VALEUR_HORS_DOMAINE: 1}
    resultat = valider_livraison(contrat_sans_seuil, generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, anomalies=anomalies))
    assert _violations_par_code(resultat) == Counter({CodeErreur.HORS_PLAGE: 2, CodeErreur.VALEUR_HORS_DOMAINE: 1})


def test_sans_anomalie_demandee_la_livraison_est_saine(contrat):
    assert generer_livraison(contrat, NB_LIGNES, GRAINE, anomalies={}) == generer_livraison(contrat, NB_LIGNES, GRAINE)


@pytest.mark.parametrize(
    ("nb_lignes", "anomalies"),
    [
        pytest.param(3, {CodeErreur.HORS_PLAGE: 5}, id="plus-d-anomalies-que-de-lignes"),
        pytest.param(4, {CodeErreur.CLE_EN_CONFLIT: 3}, id="conflits-sans-assez-de-lignes-saines-a-contredire"),
        pytest.param(5, {CodeErreur.HORS_PLAGE: -2}, id="nombre-d-anomalies-negatif"),
        pytest.param(5, {"HORS_PLAGE": 1}, id="code-qui-n-est-pas-un-CodeErreur"),
    ],
)
def test_une_demande_impossible_est_refusee(contrat, nb_lignes, anomalies):
    with pytest.raises(ValueError):
        generer_livraison(contrat, nb_lignes, GRAINE, anomalies=anomalies)


# ==============================================================================
# 4. 📄 FICHIER CSV : ce que le bronze lira (tout y est du texte)
# ==============================================================================
def _relire(chemin):
    with chemin.open(encoding="utf-8", newline="") as fichier:
        return list(csv.DictReader(fichier))


def test_le_csv_a_les_colonnes_du_contrat_dans_l_ordre(tmp_path, contrat):
    chemin = tmp_path / "livraison.csv"
    ecrire_livraison_csv(contrat, generer_livraison(contrat, 5, GRAINE), chemin)
    with chemin.open(encoding="utf-8", newline="") as fichier:
        assert next(csv.reader(fichier)) == [colonne.nom for colonne in contrat.colonnes]


def test_une_livraison_saine_relue_depuis_le_csv_est_acceptee(tmp_path, contrat):
    chemin = tmp_path / "livraison.csv"
    ecrire_livraison_csv(contrat, generer_livraison(contrat, 50, GRAINE), chemin)
    relues = _relire(chemin)
    assert len(relues) == 50
    assert valider_livraison(contrat, relues).statut == StatutLivraison.ACCEPTEE


@TOUS_LES_CODES
def test_une_anomalie_reste_detectable_apres_le_passage_par_le_csv(tmp_path, contrat_sans_seuil, code):
    """Le fichier ne contient que du texte : l'anomalie doit survivre à l'écriture puis à la relecture."""
    chemin = tmp_path / "livraison.csv"
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, anomalies={code: NB_ANOMALIES})
    ecrire_livraison_csv(contrat_sans_seuil, lignes, chemin)
    resultat = valider_livraison(contrat_sans_seuil, _relire(chemin))
    assert len(resultat.lignes_rejetees) == NB_ANOMALIES
    assert set(_violations_par_code(resultat)) == {code}
