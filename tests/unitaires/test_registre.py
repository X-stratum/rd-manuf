"""Tests du REGISTRE des livraisons : une ligne par livraison, avec sa décision, d'où et de quand elle vient, et quand elle est arrivée.

L'heure d'arrivée est PASSÉE EN PARAMÈTRE (jamais lue sur l'horloge) : les tests sont reproductibles et la règle de plausibilité des dates
pourra s'appuyer dessus. Un fichier déjà intégré qui arrive de nouveau est un REJEU : il est ignoré, avec une trace dans le registre.
"""
import hashlib
from datetime import UTC, datetime, timedelta, timezone

import pytest

from rd_manuf.contrat import CodeErreur, StatutLivraison
from rd_manuf.reception import receptionner_fichier, receptionner_livraison
from rd_manuf.registre import STATUT_REJEU, LigneRegistre, construire_ligne_registre, traiter_fichier
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison
from rd_manuf.structure import CodeStructure

GRAINE = 42
NB_LIGNES = 20
ARRIVEE = datetime(2026, 11, 11, 14, 35, 0, tzinfo=UTC)
NOM = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"
INSTANT_DU_NOM = datetime(2026, 11, 11, 14, 30, 5, tzinfo=UTC)


def _ecrire(tmp_path, contrat, nom=NOM, **anomalies):
    chemin = tmp_path / nom
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE, **anomalies), chemin)
    return chemin


# ==============================================================================
# 1. 📒 UNE LIGNE PAR LIVRAISON : la décision, les compteurs, d'où et de quand
# ==============================================================================
def test_une_livraison_acceptee_donne_une_ligne_complete(tmp_path, contrat):
    reception = receptionner_fichier(contrat, _ecrire(tmp_path, contrat))

    assert construire_ligne_registre(reception, ARRIVEE) == LigneRegistre(
        livraison_id=NOM,
        statut="ACCEPTEE",
        version_contrat=contrat.version,
        nb_recues=NB_LIGNES,
        nb_valides=NB_LIGNES,
        nb_rejetees=0,
        codes_refus=(),
        pays="FRA",
        usine="AA",
        ligne="001",
        horodatage_fichier=INSTANT_DU_NOM,
        heure_arrivee=ARRIVEE,
        empreinte=None,
        rejeu_de=None,
    )


def test_un_nom_invalide_donne_une_ligne_sans_origine_et_avec_son_motif(tmp_path, contrat):
    reception = receptionner_fichier(contrat, tmp_path / "donnees.csv")

    ligne = construire_ligne_registre(reception, ARRIVEE)

    assert (ligne.statut, ligne.codes_refus) == ("REFUSEE", ("NOM_FICHIER_INVALIDE",))
    assert (ligne.pays, ligne.usine, ligne.ligne, ligne.horodatage_fichier) == (None, None, None, None)
    assert ligne.livraison_id == "donnees.csv"


def test_un_fichier_illisible_garde_son_origine_et_son_motif(tmp_path, contrat):
    chemin = tmp_path / NOM
    chemin.write_bytes("id_lot\né\n".encode("latin-1"))

    ligne = construire_ligne_registre(receptionner_fichier(contrat, chemin), ARRIVEE)

    assert (ligne.statut, ligne.codes_refus) == ("REFUSEE", ("FICHIER_ILLISIBLE",))
    assert (ligne.pays, ligne.usine, ligne.ligne, ligne.horodatage_fichier) == ("FRA", "AA", "001", INSTANT_DU_NOM)


def test_tous_les_motifs_de_refus_sont_gardes_dans_l_ordre(contrat, entete):
    """Une livraison refusée pour plusieurs raisons les garde toutes : `COLONNE_INCONNUE` avant `COLONNE_MANQUANTE`, ordre des codes."""
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    reception = receptionner_livraison(contrat, "LIV-001", ["colonne_en_trop"] + entete[1:], lignes)

    assert construire_ligne_registre(reception, ARRIVEE).codes_refus == ("COLONNE_INCONNUE", "COLONNE_MANQUANTE")


# ==============================================================================
# 2. 🕐 L'HEURE D'ARRIVÉE : toujours en UTC, jamais « naïve »
# ==============================================================================
def test_l_heure_d_arrivee_est_convertie_en_utc(tmp_path, contrat):
    reception = receptionner_fichier(contrat, _ecrire(tmp_path, contrat))
    heure_locale = datetime(2026, 11, 11, 15, 35, 0, tzinfo=timezone(timedelta(hours=1)))      # 15:35 à UTC+1, soit 14:35 UTC

    ligne = construire_ligne_registre(reception, heure_locale)

    assert (ligne.heure_arrivee, ligne.heure_arrivee.utcoffset()) == (ARRIVEE, timedelta(0))


def test_une_heure_d_arrivee_sans_fuseau_est_refusee(tmp_path, contrat):
    """Une heure « naïve » ne dit pas dans quel fuseau elle est : on refuse de deviner."""
    reception = receptionner_fichier(contrat, _ecrire(tmp_path, contrat))
    with pytest.raises(ValueError, match="fuseau"):
        construire_ligne_registre(reception, datetime(2026, 11, 11, 14, 35, 0))


# ==============================================================================
# 3. ♻️ UN REJEU : un fichier déjà intégré qui arrive de nouveau est ignoré, avec une trace
# ==============================================================================
def test_le_meme_fichier_deux_fois_est_un_rejeu_ignore_avec_une_trace(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)
    registre = []
    _, premiere = traiter_fichier(contrat, chemin, registre, ARRIVEE)
    registre.append(premiere)

    reception, seconde = traiter_fichier(contrat, chemin, registre, ARRIVEE + timedelta(minutes=5))

    assert premiere.statut == "ACCEPTEE"
    assert reception is None                                       # rien n'est relu ni rejugé : la première livraison reste valable
    assert (seconde.statut, seconde.rejeu_de) == (STATUT_REJEU, NOM)
    assert seconde.heure_arrivee == ARRIVEE + timedelta(minutes=5)
    assert (seconde.pays, seconde.usine, seconde.ligne, seconde.horodatage_fichier) == ("FRA", "AA", "001", INSTANT_DU_NOM)
    assert (seconde.nb_recues, seconde.nb_valides, seconde.nb_rejetees) == (0, 0, 0)


def test_une_livraison_acceptee_avec_rejets_est_aussi_integree(tmp_path, contrat_sans_seuil):
    chemin = _ecrire(tmp_path, contrat_sans_seuil, anomalies={CodeErreur.HORS_PLAGE: 2})
    registre = []
    _, premiere = traiter_fichier(contrat_sans_seuil, chemin, registre, ARRIVEE)
    registre.append(premiere)

    reception, seconde = traiter_fichier(contrat_sans_seuil, chemin, registre, ARRIVEE)

    assert premiere.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS.value
    assert (reception, seconde.statut) == (None, STATUT_REJEU)


def test_un_fichier_refuse_peut_etre_renvoye_sous_le_meme_nom(tmp_path, contrat):
    chemin = tmp_path / NOM
    chemin.write_bytes("id_lot\né\n".encode("latin-1"))
    registre = []
    _, premiere = traiter_fichier(contrat, chemin, registre, ARRIVEE)
    registre.append(premiere)

    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE), chemin)          # l'émetteur corrige et renvoie
    reception, seconde = traiter_fichier(contrat, chemin, registre, ARRIVEE + timedelta(hours=1))

    assert premiere.statut == "REFUSEE"
    assert (reception.statut, seconde.statut, seconde.rejeu_de) == (StatutLivraison.ACCEPTEE, "ACCEPTEE", None)


def test_une_livraison_bloquee_peut_etre_renvoyee_sous_le_meme_nom(tmp_path, contrat_strict):
    chemin = _ecrire(tmp_path, contrat_strict, anomalies={CodeErreur.HORS_PLAGE: 2})
    registre = []
    _, premiere = traiter_fichier(contrat_strict, chemin, registre, ARRIVEE)
    registre.append(premiere)

    reception, seconde = traiter_fichier(contrat_strict, chemin, registre, ARRIVEE + timedelta(hours=1))

    assert premiere.statut == "BLOQUEE"
    assert (reception is not None, seconde.rejeu_de) == (True, None)           # rejugée : bloquée n'est pas intégrée


@pytest.mark.parametrize(
    "autre_nom",
    [
        pytest.param("livraison_production_FRA_AA_001_2026-11-11-14-30-06Z.csv", id="autre-instant"),
        pytest.param("livraison_production_FRA_AA_002_2026-11-11-14-30-05Z.csv", id="autre-ligne-de-production"),
        pytest.param("livraison_production_FRA_AB_001_2026-11-11-14-30-05Z.csv", id="autre-usine"),
        pytest.param("livraison_production_DEU_AA_001_2026-11-11-14-30-05Z.csv", id="autre-pays"),
    ],
)
def test_une_autre_origine_ou_un_autre_instant_n_est_pas_un_rejeu(tmp_path, contrat, autre_nom):
    registre = []
    _, premiere = traiter_fichier(contrat, _ecrire(tmp_path, contrat), registre, ARRIVEE)
    registre.append(premiere)

    reception, seconde = traiter_fichier(contrat, _ecrire(tmp_path, contrat, nom=autre_nom), registre, ARRIVEE)

    assert (reception.statut, seconde.rejeu_de) == (StatutLivraison.ACCEPTEE, None)


# ==============================================================================
# 4. 🔍 LE CONTENU COMPTE : même nom ET même contenu = rejeu ; même nom mais contenu différent = CONFLIT, jamais ignoré en silence
# ==============================================================================
def _empreinte(chemin):
    return hashlib.sha256(chemin.read_bytes()).hexdigest()


def test_la_ligne_d_une_livraison_garde_l_empreinte_de_son_contenu(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)

    _, ligne = traiter_fichier(contrat, chemin, [], ARRIVEE)

    assert ligne.empreinte == _empreinte(chemin)


def test_le_meme_contenu_reecrit_reste_un_rejeu(tmp_path, contrat):
    """Ce qui compte est le contenu, pas le fichier physique : le même contenu renvoyé de nouveau est bien un rejeu."""
    chemin = _ecrire(tmp_path, contrat)
    registre = []
    _, premiere = traiter_fichier(contrat, chemin, registre, ARRIVEE)
    registre.append(premiere)
    _ecrire(tmp_path, contrat)                                      # même graine : mêmes octets

    reception, seconde = traiter_fichier(contrat, chemin, registre, ARRIVEE + timedelta(hours=1))

    assert (reception, seconde.statut) == (None, STATUT_REJEU)


def test_un_contenu_different_sous_le_meme_nom_est_un_conflit_refuse(tmp_path, contrat):
    """Le cas relevé à la revue : sans cette règle, des données corrigées seraient ignorées en silence."""
    chemin = _ecrire(tmp_path, contrat)
    registre = []
    _, premiere = traiter_fichier(contrat, chemin, registre, ARRIVEE)
    registre.append(premiere)
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES + 30, GRAINE + 1), chemin)      # autres données, même nom

    reception, seconde = traiter_fichier(contrat, chemin, registre, ARRIVEE + timedelta(hours=1))

    assert premiere.statut == "ACCEPTEE"
    assert reception.statut == StatutLivraison.REFUSEE
    assert [probleme.code for probleme in reception.problemes_structure] == [CodeStructure.LIVRAISON_EN_CONFLIT]
    assert NOM in reception.problemes_structure[0].message                         # le message désigne la livraison déjà intégrée
    assert (seconde.statut, seconde.codes_refus, seconde.rejeu_de) == ("REFUSEE", ("LIVRAISON_EN_CONFLIT",), None)
    assert seconde.empreinte == _empreinte(chemin) != premiere.empreinte
    assert (seconde.nb_recues, seconde.nb_valides) == (0, 0)                        # le second fichier n'est pas jugé


def test_apres_un_conflit_la_livraison_d_origine_reste_la_reference(tmp_path, contrat):
    chemin = _ecrire(tmp_path, contrat)
    registre = []
    _, premiere = traiter_fichier(contrat, chemin, registre, ARRIVEE)
    registre.append(premiere)
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES + 30, GRAINE + 1), chemin)
    _, conflit = traiter_fichier(contrat, chemin, registre, ARRIVEE + timedelta(hours=1))
    registre.append(conflit)
    ecrire_livraison_csv(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE), chemin)                # on remet le contenu d'origine

    reception, troisieme = traiter_fichier(contrat, chemin, registre, ARRIVEE + timedelta(hours=2))

    assert (reception, troisieme.statut, troisieme.rejeu_de) == (None, STATUT_REJEU, NOM)
