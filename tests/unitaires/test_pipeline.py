"""Tests de l'ENCHAÎNEMENT d'une livraison : du fichier reçu au registre, au bronze, au silver et aux alertes, en un seul appel.

Chaque issue d'une livraison (acceptée, avec rejets, bloquée, refusée, rejouée, en conflit) est jouée sur un vrai fichier. L'appelant garde
l'ÉTAT (le registre, le silver déjà intégré) : la fonction ne l'écrit nulle part et ne le modifie pas. Ces tests ne touchent aucune table.
"""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rd_manuf.alertes import charger_catalogue, router_alertes_reception
from rd_manuf.contrat import CodeErreur, StatutLivraison
from rd_manuf.pipeline import traiter_livraison
from rd_manuf.registre import STATUT_REJEU, traiter_fichier
from rd_manuf.silver import ResultatSilver
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"
GRAINE = 42
NB_LIGNES = 20
ARRIVEE = datetime(2026, 11, 11, 14, 35, 0, tzinfo=UTC)
NOM_CSV = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"
NOM_PARQUET = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.parquet"
NOM_SUIVANT = "livraison_production_FRA_AA_001_2026-11-12-09-00-00Z.csv"
NOM_AUTRE_USINE = "livraison_production_FRA_AB_001_2026-11-12-09-00-00Z.csv"
RIEN = ResultatSilver(lignes=[], quarantaine=[])


@pytest.fixture(scope="module")
def catalogue():
    return charger_catalogue(CHEMIN_CATALOGUE)


def _csv(tmp_path, contrat, nom=NOM_CSV, anomalies=None, graine=GRAINE, modifiees=0):
    """Un CSV du simulateur ; les `modifiees` premières lignes ont un contenu différent (même clé, autre quantité)."""
    lignes = generer_livraison(contrat, NB_LIGNES, graine, anomalies)
    for ligne in lignes[:modifiees]:
        ligne["quantite_produite"] += 1
    chemin = tmp_path / nom
    ecrire_livraison_csv(contrat, lignes, chemin)
    return chemin


def _parquet(tmp_path, contrat, nom=NOM_PARQUET, anomalies=None):
    chemin = tmp_path / nom
    pq.write_table(pa.Table.from_pylist(generer_livraison(contrat, NB_LIGNES, GRAINE, anomalies)), chemin)
    return chemin


def _traiter(contrat, catalogue, chemin, registre=(), deja_integrees=(), arrivee=ARRIVEE):
    return traiter_livraison(contrat, catalogue, chemin, registre, deja_integrees, arrivee)


def _codes(resultat):
    return [alerte.code for alerte in resultat.alertes]


# ==============================================================================
# 1. ✅ UNE LIVRAISON ACCEPTÉE : toutes les couches, aucune alerte
# ==============================================================================
def test_une_livraison_acceptee_remplit_le_registre_le_bronze_et_le_silver(tmp_path, contrat, catalogue):
    chemin = _csv(tmp_path, contrat)

    resultat = _traiter(contrat, catalogue, chemin)

    assert resultat.ligne_registre.statut == "ACCEPTEE"
    assert resultat.ligne_registre == traiter_fichier(contrat, chemin, [], ARRIVEE)[1]       # le registre vient de `traiter_fichier`
    assert resultat.reception.statut == StatutLivraison.ACCEPTEE
    assert len(resultat.lignes_bronze) == len(resultat.silver.lignes) == NB_LIGNES
    assert (resultat.lignes_coupees, resultat.silver.quarantaine, resultat.alertes) == ([], [], [])
    assert {(ligne.livraison_id, ligne.heure_arrivee) for ligne in resultat.lignes_bronze} == {(NOM_CSV, ARRIVEE)}


@pytest.mark.parametrize("ecrire", [_csv, _parquet], ids=["csv", "parquet"])
def test_une_livraison_acceptee_avec_rejets_garde_tout_au_bronze_et_alerte_l_emetteur(tmp_path, contrat_sans_seuil, catalogue, ecrire):
    chemin = ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 2})

    resultat = _traiter(contrat_sans_seuil, catalogue, chemin)

    assert resultat.ligne_registre.statut == "ACCEPTEE_AVEC_REJETS"
    assert len(resultat.lignes_bronze) == NB_LIGNES                       # le bronze garde TOUT ce qui est arrivé, rejets compris
    assert (len(resultat.silver.lignes), len(resultat.silver.quarantaine)) == (NB_LIGNES - 2, 2)
    assert _codes(resultat) == ["HORS_PLAGE"]
    assert (resultat.alertes[0].nb_lignes, resultat.alertes[0].agit) == (2, "emetteur")
    assert resultat.alertes == router_alertes_reception(catalogue, resultat.reception)


def test_le_csv_et_le_parquet_du_meme_contenu_recoivent_la_meme_decision(tmp_path, contrat_sans_seuil, catalogue):
    anomalies = {CodeErreur.HORS_PLAGE: 2, CodeErreur.VALEUR_HORS_DOMAINE: 1}

    en_csv = _traiter(contrat_sans_seuil, catalogue, _csv(tmp_path, contrat_sans_seuil, anomalies=anomalies))
    en_parquet = _traiter(contrat_sans_seuil, catalogue, _parquet(tmp_path, contrat_sans_seuil, anomalies=anomalies))

    def resume(resultat):
        return (resultat.ligne_registre.statut, len(resultat.lignes_bronze), len(resultat.silver.lignes),
                len(resultat.silver.quarantaine), _codes(resultat), [alerte.nb_lignes for alerte in resultat.alertes])

    assert resume(en_csv) == resume(en_parquet)
    assert en_csv.ligne_registre.statut == "ACCEPTEE_AVEC_REJETS"


def test_les_lignes_coupees_d_un_csv_sont_gardees_a_part_et_alertees(tmp_path, contrat_sans_seuil, catalogue, entete):
    chemin = tmp_path / NOM_CSV
    chemin.write_text("\n".join([
        ",".join(entete),
        "LOT-000001,L1,2026-11-01,1000,10,200.0",
        "LOT-000002,L1,2026-11-01",                        # coupée
        "LOT-000003,L2,2026-11-02,500,5,",
        "LOT-000004,L3",                                   # coupée
        "LOT-000005,L1,2026-11-03,800,8,150.5",
    ]) + "\n", encoding="utf-8")

    resultat = _traiter(contrat_sans_seuil, catalogue, chemin)

    assert resultat.ligne_registre.statut == "ACCEPTEE_AVEC_REJETS"
    assert (len(resultat.lignes_bronze), len(resultat.lignes_coupees)) == (3, 2)
    assert (len(resultat.silver.lignes), len(resultat.silver.quarantaine)) == (3, 2)
    assert _codes(resultat) == ["LIGNE_MALFORMEE"] and resultat.alertes[0].nb_lignes == 2


# ==============================================================================
# 2. 🛑 UNE LIVRAISON BLOQUÉE : le bronze garde tout, le silver ne reçoit rien, l'émetteur est alerté
# ==============================================================================
def test_une_livraison_bloquee_garde_son_bronze_et_ne_rend_rien_au_silver(tmp_path, contrat_strict, catalogue):
    chemin = _csv(tmp_path, contrat_strict, anomalies={CodeErreur.HORS_PLAGE: 2})

    resultat = _traiter(contrat_strict, catalogue, chemin)

    assert resultat.ligne_registre.statut == "BLOQUEE"
    assert len(resultat.lignes_bronze) == NB_LIGNES
    assert resultat.silver == RIEN
    assert _codes(resultat) == ["LIVRAISON_BLOQUEE", "HORS_PLAGE"]
    assert resultat.alertes == router_alertes_reception(catalogue, resultat.reception)


def test_une_livraison_bloquee_peut_etre_renvoyee_corrigee_sous_le_meme_nom(tmp_path, contrat_strict, catalogue):
    premiere = _traiter(contrat_strict, catalogue, _csv(tmp_path, contrat_strict, anomalies={CodeErreur.HORS_PLAGE: 2}))

    corrigee = _csv(tmp_path, contrat_strict)                                       # même nom, contenu corrigé
    seconde = _traiter(contrat_strict, catalogue, corrigee, registre=[premiere.ligne_registre], arrivee=ARRIVEE + timedelta(hours=1))

    assert (premiere.ligne_registre.statut, seconde.ligne_registre.statut) == ("BLOQUEE", "ACCEPTEE")
    assert (len(seconde.silver.lignes), seconde.alertes) == (NB_LIGNES, [])


# ==============================================================================
# 3. ⛔ UNE LIVRAISON REFUSÉE : rien n'entre, personne n'est lu en trop, l'alerte dit qui doit agir
# ==============================================================================
def _verifier_refus(resultat, code, agit="emetteur"):
    assert resultat.ligne_registre.statut == "REFUSEE"
    assert resultat.reception.statut == StatutLivraison.REFUSEE
    assert resultat.ligne_registre.codes_refus == (code,)
    assert (resultat.lignes_bronze, resultat.lignes_coupees, resultat.silver) == ([], [], RIEN)
    assert _codes(resultat) == [code]
    assert resultat.alertes[0].agit == agit


def test_un_nom_invalide_est_refuse_sans_lire_le_fichier(tmp_path, contrat, catalogue):
    """Le fichier n'existe même pas : le nom suffit à refuser, rien n'est lu."""
    resultat = _traiter(contrat, catalogue, tmp_path / "donnees.csv")

    _verifier_refus(resultat, "NOM_FICHIER_INVALIDE")
    assert (resultat.ligne_registre.pays, resultat.ligne_registre.ligne) == (None, None)


def test_une_colonne_manquante_est_refusee_avec_la_colonne_dans_l_alerte(tmp_path, contrat, catalogue):
    chemin = tmp_path / NOM_CSV
    sans_rebut = ["id_lot,id_ligne,date_production,quantite_produite,temperature_c", "LOT-000001,L1,2026-11-01,1000,200.0"]
    chemin.write_text("\n".join(sans_rebut) + "\n", encoding="utf-8")

    resultat = _traiter(contrat, catalogue, chemin)

    _verifier_refus(resultat, "COLONNE_MANQUANTE")
    assert resultat.alertes[0].colonnes == ("quantite_rebut",)
    assert resultat.ligne_registre.nb_recues == 1


def test_un_fichier_illisible_est_refuse_sans_etre_relu_pour_le_bronze(tmp_path, contrat, catalogue):
    chemin = tmp_path / NOM_CSV
    chemin.write_bytes("id_lot\né\n".encode("latin-1"))

    resultat = _traiter(contrat, catalogue, chemin)

    _verifier_refus(resultat, "FICHIER_ILLISIBLE")
    assert (resultat.ligne_registre.pays, resultat.ligne_registre.usine, resultat.ligne_registre.ligne) == ("FRA", "AA", "001")


def test_un_conflit_de_livraison_est_refuse_et_l_emetteur_est_alerte(tmp_path, contrat, catalogue):
    premiere = _traiter(contrat, catalogue, _csv(tmp_path, contrat))
    autre_contenu = _csv(tmp_path, contrat, graine=GRAINE + 1)                      # même nom, contenu différent

    seconde = _traiter(contrat, catalogue, autre_contenu, registre=[premiere.ligne_registre], arrivee=ARRIVEE + timedelta(hours=1))

    _verifier_refus(seconde, "LIVRAISON_EN_CONFLIT")
    assert premiere.ligne_registre.statut == "ACCEPTEE"


# ==============================================================================
# 4. ♻️ UN REJEU : un fichier déjà intégré qui revient n'est ni relu ni rejugé, et ne déclenche aucune alerte
# ==============================================================================
def test_un_rejeu_laisse_une_trace_et_ne_fait_rien_d_autre(tmp_path, contrat, catalogue):
    chemin = _csv(tmp_path, contrat)
    premiere = _traiter(contrat, catalogue, chemin)

    rejeu = _traiter(contrat, catalogue, chemin, registre=[premiere.ligne_registre], arrivee=ARRIVEE + timedelta(minutes=5))

    assert (rejeu.ligne_registre.statut, rejeu.ligne_registre.rejeu_de) == (STATUT_REJEU, NOM_CSV)
    assert rejeu.reception is None                                                    # rien n'est relu ni rejugé
    assert (rejeu.lignes_bronze, rejeu.lignes_coupees, rejeu.silver, rejeu.alertes) == ([], [], RIEN, [])


# ==============================================================================
# 5. 🏭 LE SILVER VOIT LES LIVRAISONS DÉJÀ INTÉGRÉES : un lot se compare à ceux de son usine seulement
# ==============================================================================
def test_une_cle_deja_integree_par_la_meme_usine_va_en_quarantaine(tmp_path, contrat, catalogue):
    deja = _traiter(contrat, catalogue, _csv(tmp_path, contrat)).silver.lignes

    seconde = _traiter(contrat, catalogue, _csv(tmp_path, contrat, nom=NOM_SUIVANT, modifiees=NB_LIGNES),
                       deja_integrees=deja, arrivee=ARRIVEE + timedelta(days=1))

    assert seconde.ligne_registre.statut == "ACCEPTEE"                                # le statut décidé à la réception ne change pas
    assert (len(seconde.silver.lignes), len(seconde.silver.quarantaine)) == (0, NB_LIGNES)
    assert {v.code for q in seconde.silver.quarantaine for v in q.violations} == {CodeErreur.CLE_EN_CONFLIT}


def test_les_memes_numeros_de_lot_dans_une_autre_usine_entrent_au_silver(tmp_path, contrat, catalogue):
    deja = _traiter(contrat, catalogue, _csv(tmp_path, contrat)).silver.lignes

    seconde = _traiter(contrat, catalogue, _csv(tmp_path, contrat, nom=NOM_AUTRE_USINE, modifiees=NB_LIGNES),
                       deja_integrees=deja, arrivee=ARRIVEE + timedelta(days=1))

    assert (len(seconde.silver.lignes), seconde.silver.quarantaine) == (NB_LIGNES, [])
    assert {(ligne.pays, ligne.usine) for ligne in seconde.silver.lignes} == {("FRA", "AB")}


# ==============================================================================
# 6. 🧱 L'ÉTAT RESTE CHEZ L'APPELANT : la fonction ne l'écrit pas, ne le modifie pas, et ne cache pas ses erreurs
# ==============================================================================
def test_la_fonction_ne_modifie_ni_le_registre_ni_les_lignes_deja_integrees(tmp_path, contrat, catalogue):
    premiere = _traiter(contrat, catalogue, _csv(tmp_path, contrat))
    registre, deja = [premiere.ligne_registre], list(premiere.silver.lignes)

    seconde = _traiter(contrat, catalogue, _csv(tmp_path, contrat, nom=NOM_AUTRE_USINE), registre=registre, deja_integrees=deja,
                       arrivee=ARRIVEE + timedelta(days=1))

    assert len(seconde.silver.lignes) == NB_LIGNES                  # la livraison ajoute bien des lignes : ce n'est pas un cas vide
    assert registre == [premiere.ligne_registre] and deja == premiere.silver.lignes


def test_le_fichier_est_ouvert_autant_de_fois_que_son_issue_l_exige(tmp_path, contrat, catalogue, monkeypatch):
    """Nom invalide : 0 (rien à lire). Rejeu : 1 (l'empreinte). Refus de structure : 2 (empreinte, réception). Acceptée : 3 (et le bronze).

    Ces nombres sont écrits dans le docstring du module : si une optimisation les réduit, ce test et ce texte se mettent à jour ensemble.
    """
    ouvertures = []
    ouvrir = Path.open

    def espion(self, *args, **kwargs):
        if self.parent == tmp_path:
            ouvertures.append(self.name)
        return ouvrir(self, *args, **kwargs)

    chemin = _csv(tmp_path, contrat)
    sans_rebut = tmp_path / "livraison_production_FRA_AA_002_2026-11-11-14-30-05Z.csv"
    sans_rebut.write_text("id_lot\nLOT-000001\n", encoding="utf-8")
    monkeypatch.setattr(Path, "open", espion)

    def lectures(fichier, registre=()):
        ouvertures.clear()
        resultat = _traiter(contrat, catalogue, fichier, registre=registre, arrivee=ARRIVEE + timedelta(minutes=5))
        return resultat, len(ouvertures)

    invalide, n_invalide = lectures(tmp_path / "donnees.csv")
    accepte, n_accepte = lectures(chemin)
    rejeu, n_rejeu = lectures(chemin, registre=[accepte.ligne_registre])
    refuse, n_refuse = lectures(sans_rebut)

    assert [r.ligne_registre.statut for r in (invalide, accepte, rejeu, refuse)] == ["REFUSEE", "ACCEPTEE", STATUT_REJEU, "REFUSEE"]
    assert (n_invalide, n_rejeu, n_refuse, n_accepte) == (0, 1, 2, 3)


def test_les_resultats_vides_ne_partagent_pas_leurs_listes(tmp_path, contrat, catalogue):
    """Un appelant qui range une ligne dans le silver d'un refus ne doit pas la retrouver dans celui d'une autre livraison."""
    chemin = _csv(tmp_path, contrat)
    rejeu_source = _traiter(contrat, catalogue, chemin)
    refus = _traiter(contrat, catalogue, tmp_path / "donnees.csv")
    rejeu = _traiter(contrat, catalogue, chemin, registre=[rejeu_source.ligne_registre])

    refus.silver.lignes.append("pollution")
    refus.silver.quarantaine.append("pollution")

    assert rejeu.silver == RIEN
    assert _traiter(contrat, catalogue, tmp_path / "autres.csv").silver == RIEN


def test_une_heure_d_arrivee_sans_fuseau_est_refusee(tmp_path, contrat, catalogue):
    with pytest.raises(ValueError, match="fuseau"):
        _traiter(contrat, catalogue, _csv(tmp_path, contrat), arrivee=datetime(2026, 11, 11, 14, 35))
