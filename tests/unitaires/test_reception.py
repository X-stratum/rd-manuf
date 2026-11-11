"""Tests de la RÉCEPTION d'une livraison : structure d'abord, lignes ensuite, une seule décision à la fin.

Ordre de la décision A : si la structure est invalide, la livraison est REFUSEE sans qu'aucune ligne soit lue ; sinon le statut est celui
du contrat (ACCEPTEE, ACCEPTEE_AVEC_REJETS, BLOQUEE). Le simulateur fabrique les livraisons, aucune valeur du contrat n'est recopiée ici.
"""
from rd_manuf.contrat import CodeErreur, StatutLivraison
from rd_manuf.lecture import LigneMalformee
from rd_manuf.reception import receptionner_livraison
from rd_manuf.simulateur import generer_livraison
from rd_manuf.structure import CodeStructure

LIVRAISON_ID = "LIV-001"
GRAINE = 42
NB_LIGNES = 20
NB_ANOMALIES = 2
STATUTS_ATTENDUS = {"ACCEPTEE", "ACCEPTEE_AVEC_REJETS", "BLOQUEE", "REFUSEE"}
NUMERO_LIGNE_COUPEE = 7
NB_CHAMPS_RECUS_PAR_LA_LIGNE_COUPEE = 2


# ==============================================================================
# 1. 🔒 LES STATUTS SONT UN CONTRAT : le registre et les alertes s'appuient dessus
# ==============================================================================
def test_les_statuts_de_livraison_sont_exactement_ceux_attendus():
    assert {statut.value for statut in StatutLivraison} == STATUTS_ATTENDUS


# ==============================================================================
# 2. ✅ STRUCTURE VALIDE : le statut est celui du contrat, avec ses compteurs
# ==============================================================================
def test_une_livraison_conforme_est_acceptee(contrat, entete):
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    reception = receptionner_livraison(contrat, LIVRAISON_ID, entete, lignes)
    assert reception.statut == StatutLivraison.ACCEPTEE
    assert (reception.livraison_id, reception.version_contrat) == (LIVRAISON_ID, contrat.version)
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (NB_LIGNES, NB_LIGNES, 0)
    assert reception.problemes_structure == ()


def test_des_rejets_sous_le_seuil_donnent_acceptee_avec_rejets(contrat_sans_seuil, entete):
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, anomalies={CodeErreur.HORS_PLAGE: NB_ANOMALIES})
    reception = receptionner_livraison(contrat_sans_seuil, LIVRAISON_ID, entete, lignes)
    assert reception.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (NB_LIGNES, NB_LIGNES - NB_ANOMALIES, NB_ANOMALIES)


def test_une_livraison_au_dessus_du_seuil_est_bloquee_et_garde_son_diagnostic(contrat_strict, entete):
    lignes = generer_livraison(contrat_strict, NB_LIGNES, GRAINE, anomalies={CodeErreur.HORS_PLAGE: NB_ANOMALIES})
    reception = receptionner_livraison(contrat_strict, LIVRAISON_ID, entete, lignes)
    assert reception.statut == StatutLivraison.BLOQUEE
    assert (reception.nb_valides, reception.nb_rejetees) == (0, NB_ANOMALIES)     # tout ou rien : rien n'est intégré
    assert len(reception.resultat.lignes_rejetees) == NB_ANOMALIES                # mais le détail reste disponible pour les alertes


# ==============================================================================
# 3. 🚫 STRUCTURE INVALIDE : REFUSEE, aucune ligne n'est lue
# ==============================================================================
def test_une_colonne_manquante_refuse_la_livraison_meme_si_les_lignes_sont_valides(contrat, entete):
    """Les lignes contiennent toutes leurs valeurs ; seul l'en-tête reçu est incomplet : la structure décide en premier."""
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    reception = receptionner_livraison(contrat, LIVRAISON_ID, entete[1:], lignes)
    assert reception.statut == StatutLivraison.REFUSEE
    assert reception.version_contrat == contrat.version     # savoir avec quelle version un refus a été prononcé
    assert [probleme.code for probleme in reception.problemes_structure] == [CodeStructure.COLONNE_MANQUANTE]
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (NB_LIGNES, 0, 0)
    assert reception.resultat is None


def test_un_fichier_sans_ligne_est_refuse(contrat, entete):
    reception = receptionner_livraison(contrat, LIVRAISON_ID, entete, [])
    assert reception.statut == StatutLivraison.REFUSEE
    assert [probleme.code for probleme in reception.problemes_structure] == [CodeStructure.FICHIER_VIDE]


def test_les_lignes_ne_sont_pas_validees_quand_la_structure_est_refusee(contrat, entete, monkeypatch):
    """« Aucune ligne n'est lue » : le validateur ne doit même pas être appelé."""
    def ne_doit_pas_etre_appele(*args, **kwargs):
        raise AssertionError("valider_livraison appelé alors que la structure est refusée")

    monkeypatch.setattr("rd_manuf.reception.valider_livraison", ne_doit_pas_etre_appele)
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    assert receptionner_livraison(contrat, LIVRAISON_ID, entete[1:], lignes).statut == StatutLivraison.REFUSEE


# ==============================================================================
# 5. ✂️ LIGNES MALFORMÉES : mises à part par le lecteur, REJETÉES ici avec leur code, et comptées comme toute ligne rejetée
# ==============================================================================
def _ligne_coupee(entete):
    """Une ligne qui n'a reçu que quelques champs : le lecteur ne sait pas à quelles colonnes les associer."""
    return LigneMalformee(numero=NUMERO_LIGNE_COUPEE, champs=tuple(entete[:NB_CHAMPS_RECUS_PAR_LA_LIGNE_COUPEE]))


def test_une_ligne_malformee_est_rejetee_avec_son_code(contrat_sans_seuil, entete):
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE)
    reception = receptionner_livraison(contrat_sans_seuil, LIVRAISON_ID, entete, lignes, [_ligne_coupee(entete)])
    assert reception.statut == StatutLivraison.ACCEPTEE_AVEC_REJETS
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (NB_LIGNES + 1, NB_LIGNES, 1)
    (rejet,) = reception.resultat.lignes_rejetees
    assert [violation.code for violation in rejet.violations] == [CodeErreur.LIGNE_MALFORMEE]


def test_une_ligne_malformee_compte_dans_le_taux_et_peut_bloquer_la_livraison(contrat_strict, entete):
    """Avec un seuil à zéro, la moindre ligne rejetée bloque : une ligne coupée ne doit pas passer sous le radar."""
    lignes = generer_livraison(contrat_strict, NB_LIGNES, GRAINE)
    reception = receptionner_livraison(contrat_strict, LIVRAISON_ID, entete, lignes, [_ligne_coupee(entete)])
    assert reception.statut == StatutLivraison.BLOQUEE
    assert (reception.nb_valides, reception.nb_rejetees) == (0, 1)


def test_une_livraison_refusee_compte_aussi_ses_lignes_malformees_parmi_les_recues(contrat, entete):
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    reception = receptionner_livraison(contrat, LIVRAISON_ID, entete[1:], lignes, [_ligne_coupee(entete)])
    assert reception.statut == StatutLivraison.REFUSEE
    assert reception.nb_recues == NB_LIGNES + 1


def test_un_fichier_dont_toutes_les_lignes_sont_malformees_n_est_pas_un_fichier_vide(contrat_sans_seuil, entete):
    reception = receptionner_livraison(contrat_sans_seuil, LIVRAISON_ID, entete, [], [_ligne_coupee(entete)])
    assert reception.problemes_structure == ()
    assert reception.nb_recues == 1
