"""Le RETRAITEMENT : rejuger une livraison déjà reçue sous une AUTRE version du contrat, et dire ce que ce nouveau jugement change.

Le silver sait déjà rejuger le bronze d'une livraison avec un autre contrat (`construire_silver`), la réception sait déjà juger des lignes
lues (`receptionner_livraison`) et le registre sait déjà fabriquer sa ligne (`construire_ligne_registre`) : le retraitement ne refait
rien de cela, il les ENCHAÎNE sur ce que le bronze a gardé, et ajoute deux choses qui n'existaient nulle part : choisir les livraisons à
rejuger (`a_retraiter`) et signaler un RECLASSEMENT (`Reclassement`) quand le statut ou les comptes changent.

Ces tests ne touchent aucune table : les fonctions sont pures. Le remplacement de l'ancien jugement par le nouveau dans l'état (la règle de
la ligne « en vigueur ») appartient à l'évolution du contrat : ici on rend ce qu'un retraitement ajouterait, sans rien écrire.

Le module est importé par son nom (`retraitement.retraiter`) pour qu'un module encore vide fasse échouer chaque test sur son assertion, et
non la collecte de tout le fichier.
"""
from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta

import pytest

from rd_manuf import retraitement
from rd_manuf.alertes import charger_catalogue
from rd_manuf.bronze import LigneBronzeCoupee, construire_lignes_bronze, construire_lignes_coupees
from rd_manuf.contrat import CodeErreur, StatutLivraison
from rd_manuf.journee import Etat, jouer, journee_multi_usines
from rd_manuf.lecture import lire_selon_format
from rd_manuf.nom_fichier import lire_nom_fichier
from rd_manuf.registre import STATUT_REJEU, LigneRegistre, traiter_fichier
from rd_manuf.silver import construire_silver
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison

GRAINE = 42
ARRIVEE = datetime(2026, 11, 11, 14, 35, tzinfo=UTC)
RETRAITE_LE = datetime(2026, 11, 20, 9, 0, tzinfo=UTC)
NOM = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"
ACCEPTEE = StatutLivraison.ACCEPTEE.value
AVEC_REJETS = StatutLivraison.ACCEPTEE_AVEC_REJETS.value
BLOQUEE = StatutLivraison.BLOQUEE.value
REFUSEE = StatutLivraison.REFUSEE.value


def _v3(contrat):
    """Le même contrat sous une autre version : la condition d'un retraitement."""
    return replace(contrat, version="3.0.0")


def _livraison(contrat, tmp_path, anomalies=None, nb_lignes=20, nom=NOM, arrivee=ARRIVEE):
    """Une vraie livraison, passée par la réception et le bronze : sa ligne de registre, ses lignes bronze et ses lignes coupées."""
    chemin = tmp_path / nom
    ecrire_livraison_csv(contrat, generer_livraison(contrat, nb_lignes, GRAINE, anomalies), chemin)
    _, ligne = traiter_fichier(contrat, chemin, (), arrivee)
    fichier = lire_selon_format(chemin, lire_nom_fichier(nom).format)
    return ligne, construire_lignes_bronze(contrat, ligne, fichier), construire_lignes_coupees(ligne, fichier)


def _retraiter(contrat, ligne, bronze, coupees, deja=(), heure=RETRAITE_LE):
    return retraitement.retraiter(contrat, ligne, bronze, coupees, deja, heure)


def _silver_a_l_arrivee(contrat, ligne, bronze, coupees):
    """Les lignes silver qu'une livraison a intégrées à son arrivée : les « déjà intégrées » de la suivante."""
    return construire_silver(contrat, ligne, bronze, coupees).lignes


# ==============================================================================
# 1. 🎯 QUELLES LIVRAISONS RETRAITER : la décision en vigueur de chaque livraison, jugée sous une autre version
# ==============================================================================
def _decision(livraison_id="a.csv", statut=ACCEPTEE, version="2.0.0", heures=0, empreinte="x"):
    """Une ligne de registre : seuls le nom, le statut, la version et l'arrivée comptent pour choisir."""
    return LigneRegistre(
        livraison_id=livraison_id, statut=statut, version_contrat=version, nb_recues=10, nb_valides=10, nb_rejetees=0, codes_refus=(),
        pays="FRA", usine="AA", ligne="001", horodatage_fichier=None, heure_arrivee=ARRIVEE + timedelta(hours=heures), empreinte=empreinte,
        rejeu_de=None,
    )


def test_le_code_du_signal_est_celui_annonce():
    assert retraitement.LIVRAISON_RECLASSEE == "LIVRAISON_RECLASSEE"


def test_un_registre_vide_n_a_rien_a_retraiter(contrat):
    assert retraitement.a_retraiter([], _v3(contrat)) == []


def test_une_livraison_jugee_sous_une_autre_version_est_a_retraiter(contrat):
    ligne = _decision(version="2.0.0")

    assert retraitement.a_retraiter([ligne], _v3(contrat)) == [ligne]


def test_une_livraison_deja_jugee_sous_cette_version_n_est_plus_a_retraiter(contrat):
    assert retraitement.a_retraiter([_decision(version="3.0.0")], _v3(contrat)) == []


@pytest.mark.parametrize(
    ("statut", "attendu"), [(ACCEPTEE, True), (AVEC_REJETS, True), (BLOQUEE, True), (REFUSEE, False), (STATUT_REJEU, False)],
    ids=["acceptée", "avec rejets", "bloquée", "refusée", "rejouée"],
)
def test_seule_une_livraison_dont_les_lignes_sont_au_bronze_se_retraite(contrat, statut, attendu):
    """Une refusée n'a pas de lignes, une rejouée n'a rien jugé : il n'y a rien à rejuger."""
    ligne = _decision(statut=statut)

    assert (retraitement.a_retraiter([ligne], _v3(contrat)) == [ligne]) is attendu


def test_c_est_la_derniere_decision_d_une_livraison_qui_est_en_vigueur(contrat):
    """Bloquée sous l'ancienne version puis renvoyée corrigée et jugée sous la nouvelle : elle est à jour, la première ne compte plus."""
    bloquee, corrigee = _decision(statut=BLOQUEE, version="2.0.0", heures=0), _decision(statut=ACCEPTEE, version="3.0.0", heures=1)

    assert retraitement.a_retraiter([bloquee, corrigee], _v3(contrat)) == []
    assert retraitement.a_retraiter([corrigee, bloquee], _v3(contrat)) == []                   # l'ordre du registre ne change rien


def test_une_decision_plus_recente_sous_l_ancienne_version_est_celle_qui_se_retraite(contrat):
    ancienne, recente = _decision(version="3.0.0", heures=0), _decision(statut=BLOQUEE, version="2.0.0", heures=1)

    assert retraitement.a_retraiter([ancienne, recente], _v3(contrat)) == [recente]


def test_un_rejeu_ou_un_refus_plus_recent_ne_remplace_pas_la_decision(contrat):
    decision = _decision(version="2.0.0", heures=0)
    registre = [decision, _decision(statut=STATUT_REJEU, version="9.9.9", heures=1), _decision(statut=REFUSEE, version="9.9.9", heures=2)]

    assert retraitement.a_retraiter(registre, _v3(contrat)) == [decision]


def test_une_livraison_seulement_refusee_n_est_jamais_a_retraiter(contrat):
    assert retraitement.a_retraiter([_decision(statut=REFUSEE, version="1.0.0")], _v3(contrat)) == []


def test_chaque_livraison_est_jugee_seule_et_rendue_dans_l_ordre_d_arrivee_puis_du_nom(contrat):
    tardive, premiere, meme_heure = _decision("c.csv", heures=5), _decision("b.csv", heures=1), _decision("a.csv", heures=1)

    assert retraitement.a_retraiter([tardive, premiere, meme_heure], _v3(contrat)) == [meme_heure, premiere, tardive]


def test_la_selection_ne_modifie_pas_ses_entrees_et_accepte_n_importe_quel_iterable(contrat):
    registre = [_decision("b.csv"), _decision("a.csv", version="3.0.0")]
    copie = list(registre)

    selection = retraitement.a_retraiter((ligne for ligne in registre), _v3(contrat))

    assert registre == copie and [ligne.livraison_id for ligne in selection] == ["b.csv"]


# ==============================================================================
# 2. 🔁 LE RETRAITEMENT D'UNE LIVRAISON : ce qu'il ajoute, et ce qu'il signale
# ==============================================================================
def test_une_livraison_acceptee_avec_rejets_devient_bloquee_sous_un_seuil_plus_strict(tmp_path, contrat, contrat_strict):
    """100 lignes dont 2 rejetées : 2 % passe le seuil de 2 % du contrat d'origine, pas un seuil à zéro."""
    ligne, bronze, coupees = _livraison(contrat, tmp_path, anomalies={CodeErreur.HORS_PLAGE: 2}, nb_lignes=100)

    resultat = _retraiter(_v3(contrat_strict), ligne, bronze, coupees)

    assert ligne.statut == AVEC_REJETS and resultat.ligne_registre.statut == BLOQUEE
    assert (resultat.silver.lignes, resultat.silver.quarantaine) == ([], [])                  # une livraison bloquée ne rend rien
    signal = resultat.reclassement
    assert (signal.code, signal.livraison_id) == ("LIVRAISON_RECLASSEE", NOM)
    assert (signal.statut_avant, signal.statut_apres) == (AVEC_REJETS, BLOQUEE)
    assert (signal.version_avant, signal.version_apres) == ("2.0.0", "3.0.0")
    assert (signal.nb_valides_avant, signal.nb_valides_apres, signal.nb_rejetees_avant, signal.nb_rejetees_apres) == (98, 0, 2, 2)


def test_une_livraison_bloquee_devient_acceptee_avec_rejets_sous_un_seuil_plus_large(tmp_path, contrat, contrat_sans_seuil):
    """20 lignes dont 1 rejetée : 5 % bloque sous le seuil de 2 % ; le seuil neutralisé la laisse entrer, avec sa quarantaine."""
    ligne, bronze, coupees = _livraison(contrat, tmp_path, anomalies={CodeErreur.HORS_PLAGE: 1})

    resultat = _retraiter(_v3(contrat_sans_seuil), ligne, bronze, coupees)

    assert ligne.statut == BLOQUEE and resultat.ligne_registre.statut == AVEC_REJETS
    assert (len(resultat.silver.lignes), len(resultat.silver.quarantaine)) == (19, 1)
    assert {ligne.version_contrat for ligne in resultat.silver.lignes} == {"3.0.0"}
    signal = resultat.reclassement
    assert (signal.statut_avant, signal.statut_apres) == (BLOQUEE, AVEC_REJETS)
    assert (signal.nb_valides_avant, signal.nb_valides_apres, signal.nb_rejetees_avant, signal.nb_rejetees_apres) == (0, 19, 1, 1)


def test_la_nouvelle_ligne_de_registre_garde_l_identite_de_la_livraison_et_prend_l_heure_et_la_version_du_retraitement(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    nouvelle = _retraiter(_v3(contrat), ligne, bronze, coupees).ligne_registre

    assert (nouvelle.livraison_id, nouvelle.heure_arrivee, nouvelle.version_contrat) == (NOM, RETRAITE_LE, "3.0.0")
    origine = (ligne.pays, ligne.usine, ligne.ligne, ligne.horodatage_fichier)
    assert (nouvelle.pays, nouvelle.usine, nouvelle.ligne, nouvelle.horodatage_fichier) == origine
    assert (nouvelle.empreinte, nouvelle.nb_recues) == (ligne.empreinte, ligne.nb_recues)
    assert (nouvelle.statut, nouvelle.rejeu_de) == (ACCEPTEE, None)


def test_une_nouvelle_version_sans_effet_ne_signale_rien_mais_garde_la_trace_du_jugement(tmp_path, contrat):
    """Même statut, mêmes comptes : aucun reclassement. La livraison est pourtant rejugée, et son silver porte la nouvelle version."""
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    resultat = _retraiter(_v3(contrat), ligne, bronze, coupees)

    assert resultat.reclassement is None
    assert resultat.ligne_registre.version_contrat == "3.0.0" and len(resultat.silver.lignes) == 20
    assert {ligne.version_contrat for ligne in resultat.silver.lignes} == {"3.0.0"}
    assert {ligne.heure_arrivee for ligne in resultat.silver.lignes} == {RETRAITE_LE}


def test_un_changement_de_comptes_sans_changement_de_statut_est_un_reclassement(tmp_path, contrat):
    """Deux rejets (une température hors plage, une date absente) : un contrat qui relève la plage de température n'en laisse qu'un.
    Acceptée avec rejets avant comme après, mais une ligne a changé de sort : c'est à signaler."""
    anomalies = {CodeErreur.HORS_PLAGE: 1, CodeErreur.OBLIGATOIRE_MANQUANT: 1}
    ligne, bronze, coupees = _livraison(contrat, tmp_path, anomalies=anomalies, nb_lignes=100)
    plage_relevee = replace(contrat, version="3.0.0", colonnes=tuple(
        replace(colonne, maximum=500) if colonne.nom == "temperature_c" else colonne for colonne in contrat.colonnes))

    resultat = _retraiter(plage_relevee, ligne, bronze, coupees)

    assert (ligne.statut, resultat.ligne_registre.statut) == (AVEC_REJETS, AVEC_REJETS)
    signal = resultat.reclassement
    assert (signal.nb_valides_avant, signal.nb_valides_apres, signal.nb_rejetees_avant, signal.nb_rejetees_apres) == (98, 99, 2, 1)
    assert (len(resultat.silver.lignes), len(resultat.silver.quarantaine)) == (99, 1)


def test_un_contrat_dont_la_structure_ne_correspond_plus_au_bronze_refuse_la_livraison(tmp_path, contrat):
    """La structure est rejugée aussi, depuis les colonnes que le bronze a gardées : une colonne exigée et absente refuse la livraison."""
    ligne, bronze, coupees = _livraison(contrat, tmp_path)
    sans_temperature = [replace(b, valeurs={cle: valeur for cle, valeur in b.valeurs.items() if cle != "temperature_c"}) for b in bronze]

    resultat = _retraiter(_v3(contrat), ligne, sans_temperature, coupees)

    assert resultat.ligne_registre.statut == REFUSEE and (resultat.silver.lignes, resultat.silver.quarantaine) == ([], [])
    assert (resultat.reclassement.statut_avant, resultat.reclassement.statut_apres) == (ACCEPTEE, REFUSEE)
    assert (resultat.ligne_registre.nb_valides, resultat.ligne_registre.nb_rejetees) == (0, 0)


def test_une_livraison_sans_ligne_bien_formee_garde_les_colonnes_du_contrat_au_lieu_d_etre_refusee_a_tort(contrat, contrat_sans_seuil):
    """Sans ligne au bronze, l'en-tête n'est pas gardé : prendre « aucune colonne » refuserait à tort la livraison pour structure."""
    ligne = replace(_decision(NOM, statut=BLOQUEE, version="2.0.0"), nb_recues=2, nb_valides=0, nb_rejetees=2)
    coupees = [LigneBronzeCoupee(livraison_id=NOM, heure_arrivee=ligne.heure_arrivee, numero_fichier=n, champs=("LOT-1", "L1"))
               for n in (2, 3)]

    resultat = _retraiter(_v3(contrat_sans_seuil), ligne, [], coupees)

    assert resultat.ligne_registre.statut != REFUSEE and resultat.ligne_registre.nb_recues == 2


# ==============================================================================
# 3. ⚖️ LES DOUBLONS ENTRE LIVRAISONS : le silver décide, à l'heure du retraitement
# ==============================================================================
def test_une_livraison_retraitee_ne_double_pas_des_lots_qu_une_autre_a_integres_entre_temps(tmp_path, contrat, contrat_sans_seuil):
    """Bloquée à l'origine, elle n'avait rien au silver ; une autre livraison de mêmes clés et de même contenu est entrée depuis. Rejugée
    maintenant, elle ne doit pas intégrer une seconde fois les mêmes lots : le gold les compterait deux fois."""
    bloquee, bronze, coupees = _livraison(contrat, tmp_path, anomalies={CodeErreur.HORS_PLAGE: 1})
    autre, bronze_autre, coupees_autre = _livraison(
        contrat_sans_seuil, tmp_path, nom="livraison_production_FRA_AA_001_2026-11-12-09-00-00Z.csv", arrivee=ARRIVEE + timedelta(days=1))
    deja = _silver_a_l_arrivee(contrat_sans_seuil, autre, bronze_autre, coupees_autre)

    resultat = _retraiter(_v3(contrat_sans_seuil), bloquee, bronze, coupees, deja=deja)

    assert bloquee.statut == BLOQUEE and resultat.ligne_registre.statut == AVEC_REJETS
    assert resultat.silver.lignes == []                                  # les 19 lignes valides sont déjà là, avec le même contenu


def test_une_livraison_retraitee_ne_se_compare_pas_a_ses_propres_lignes(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)
    avant = _silver_a_l_arrivee(contrat, ligne, bronze, coupees)

    resultat = _retraiter(_v3(contrat), ligne, bronze, coupees, deja=avant)

    assert len(avant) == 20 and len(resultat.silver.lignes) == 20 and resultat.silver.quarantaine == []


# ==============================================================================
# 4. 🛡️ LES REFUS : une erreur de l'appelant se dit tout de suite, sans rien rendre
# ==============================================================================
def test_une_livraison_deja_jugee_sous_cette_version_ne_se_retraite_pas(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    with pytest.raises(ValueError, match="déjà jugée sous la version 2.0.0"):
        _retraiter(contrat, ligne, bronze, coupees)


@pytest.mark.parametrize("statut", [REFUSEE, STATUT_REJEU], ids=["refusée", "rejouée"])
def test_une_livraison_dont_les_lignes_ne_sont_pas_au_bronze_ne_se_retraite_pas(tmp_path, contrat, statut):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    with pytest.raises(ValueError, match="bronze"):
        _retraiter(_v3(contrat), replace(ligne, statut=statut), bronze, coupees)


def test_une_heure_de_retraitement_sans_fuseau_est_refusee(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    with pytest.raises(ValueError, match="fuseau"):
        _retraiter(_v3(contrat), ligne, bronze, coupees, heure=datetime(2026, 11, 20, 9, 0))


@pytest.mark.parametrize("decalage", [timedelta(0), timedelta(hours=-1)], ids=["la même heure", "avant l'arrivée"])
def test_le_retraitement_doit_etre_posterieur_a_l_arrivee_sinon_il_porterait_la_cle_de_l_ancien_jugement(tmp_path, contrat, decalage):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    with pytest.raises(ValueError, match="postérieure"):
        _retraiter(_v3(contrat), ligne, bronze, coupees, heure=ligne.heure_arrivee + decalage)


def test_des_lignes_bronze_d_une_autre_livraison_sont_refusees(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)
    d_ailleurs = [replace(b, livraison_id="autre.csv") for b in bronze]

    with pytest.raises(ValueError, match="autre.csv"):
        _retraiter(_v3(contrat), ligne, d_ailleurs, coupees)


def test_des_lignes_bronze_d_une_autre_arrivee_sont_refusees(tmp_path, contrat):
    """Bloquée puis renvoyée corrigée sous le même nom : les lignes de la première ne sont pas celles de la seconde."""
    ligne, bronze, coupees = _livraison(contrat, tmp_path)
    d_une_autre_arrivee = [replace(b, heure_arrivee=b.heure_arrivee + timedelta(days=1)) for b in bronze]

    with pytest.raises(ValueError, match="arrivée"):
        _retraiter(_v3(contrat), ligne, d_une_autre_arrivee, coupees)


def test_des_lignes_coupees_d_une_autre_livraison_sont_refusees(tmp_path, contrat):
    ligne, bronze, _ = _livraison(contrat, tmp_path)
    coupee = LigneBronzeCoupee(livraison_id="autre.csv", heure_arrivee=ligne.heure_arrivee, numero_fichier=3, champs=("LOT-1",))

    with pytest.raises(ValueError, match="autre.csv"):
        _retraiter(_v3(contrat), ligne, bronze, [coupee])


def test_un_bronze_incomplet_ne_se_retraite_pas(tmp_path, contrat):
    """Rejuger moins de lignes que le registre n'en a reçu donnerait un autre jugement sur des données perdues : on le refuse."""
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    with pytest.raises(ValueError, match="incomplète"):
        _retraiter(_v3(contrat), ligne, bronze[:-1], coupees)


# ==============================================================================
# 5. 🔗 PLUSIEURS RETRAITEMENTS DE SUITE : le bronze garde l'arrivée d'origine, la ligne en vigueur a l'heure du dernier retraitement
# ==============================================================================
V4 = "4.0.0"


def _retraitee_une_fois(contrat, tmp_path):
    """Une livraison jugée sous 2.0.0 puis retraitée sous 3.0.0 : son ancienne ligne, sa ligne en vigueur, son bronze (resté d'origine)."""
    ligne, bronze, coupees = _livraison(contrat, tmp_path)
    return ligne, bronze, coupees, _retraiter(_v3(contrat), ligne, bronze, coupees).ligne_registre


def test_une_livraison_deja_retraitee_se_retraite_encore_quand_on_dit_de_quelle_arrivee_sont_ses_lignes_bronze(tmp_path, contrat):
    ligne, bronze, coupees, nouvelle = _retraitee_une_fois(contrat, tmp_path)
    registre, quatrieme = [ligne, nouvelle], replace(contrat, version=V4)
    [en_vigueur] = retraitement.a_retraiter(registre, quatrieme)
    arrivee = retraitement.retrouver_arrivee_du_bronze(registre, en_vigueur)

    plus_tard = RETRAITE_LE + timedelta(days=1)
    resultat = retraitement.retraiter(quatrieme, en_vigueur, bronze, coupees, (), plus_tard, arrivee_du_bronze=arrivee)

    assert en_vigueur == nouvelle and arrivee == ligne.heure_arrivee
    assert (resultat.ligne_registre.version_contrat, resultat.ligne_registre.heure_arrivee) == (V4, RETRAITE_LE + timedelta(days=1))
    assert resultat.reclassement is None and len(resultat.silver.lignes) == 20


def test_sans_l_arrivee_du_bronze_une_livraison_deja_retraitee_reste_refusee(tmp_path, contrat):
    """Par défaut les lignes bronze doivent avoir l'arrivée de la ligne : le garde ne s'est pas relâché."""
    _, bronze, coupees, nouvelle = _retraitee_une_fois(contrat, tmp_path)

    with pytest.raises(ValueError, match="arrivée"):
        _retraiter(replace(contrat, version=V4), nouvelle, bronze, coupees, heure=RETRAITE_LE + timedelta(days=1))


def test_une_arrivee_du_bronze_qui_n_est_pas_celle_des_lignes_est_refusee(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    with pytest.raises(ValueError, match="arrivée"):
        fausse = ligne.heure_arrivee + timedelta(days=1)
        retraitement.retraiter(_v3(contrat), ligne, bronze, coupees, (), RETRAITE_LE, arrivee_du_bronze=fausse)


def test_l_arrivee_d_origine_donnee_explicitement_est_acceptee_comme_par_defaut(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)

    resultat = retraitement.retraiter(_v3(contrat), ligne, bronze, coupees, (), RETRAITE_LE, arrivee_du_bronze=ligne.heure_arrivee)

    assert resultat == _retraiter(_v3(contrat), ligne, bronze, coupees)


def test_une_livraison_renvoyee_corrigee_ne_prend_pas_les_lignes_bronze_de_la_premiere(tmp_path, contrat):
    """Bloquée puis renvoyée corrigée sous le même nom : le contenu change, donc l'arrivée des lignes ; les anciennes restent refusées."""
    premiere, bronze, coupees = _livraison(contrat, tmp_path, anomalies={CodeErreur.HORS_PLAGE: 1})
    seconde, _, _ = _livraison(contrat, tmp_path, arrivee=ARRIVEE + timedelta(days=1))
    registre = [premiere, seconde]
    arrivee = retraitement.retrouver_arrivee_du_bronze(registre, seconde)

    assert premiere.empreinte != seconde.empreinte and arrivee == seconde.heure_arrivee
    with pytest.raises(ValueError, match="arrivée"):
        retraitement.retraiter(_v3(contrat), seconde, bronze, coupees, (), RETRAITE_LE, arrivee_du_bronze=arrivee)


def test_l_arrivee_du_bronze_d_une_livraison_jamais_retraitee_est_la_sienne():
    ligne = _decision(heures=3)

    assert retraitement.retrouver_arrivee_du_bronze([ligne], ligne) == ligne.heure_arrivee


def test_l_arrivee_du_bronze_d_une_chaine_de_retraitements_est_celle_de_la_premiere_ligne_de_meme_contenu():
    origine, v3, v4 = _decision(heures=0), _decision(version="3.0.0", heures=5), _decision(version="4.0.0", heures=9)
    registre = [v4, origine, v3]                                                   # dans le désordre

    assert [retraitement.retrouver_arrivee_du_bronze(registre, ligne) for ligne in (origine, v3, v4)] == [origine.heure_arrivee] * 3


def test_un_renvoi_corrige_a_ses_propres_lignes_bronze_donc_sa_propre_arrivee():
    """Même nom, autre contenu (autre empreinte) : ses lignes bronze ne sont pas celles de la première version, ni après un retraitement."""
    premiere, corrigee = _decision(statut=BLOQUEE, heures=0, empreinte="E1"), _decision(heures=3, empreinte="E2")
    corrigee_retraitee = _decision(version="3.0.0", heures=8, empreinte="E2")
    registre = [premiere, corrigee, corrigee_retraitee]

    assert retraitement.retrouver_arrivee_du_bronze(registre, corrigee_retraitee) == corrigee.heure_arrivee
    assert retraitement.retrouver_arrivee_du_bronze(registre, premiere) == premiere.heure_arrivee


def test_les_lignes_sans_bronze_et_les_autres_livraisons_ne_donnent_pas_l_arrivee_du_bronze():
    ligne = _decision(heures=4)
    registre = [_decision(statut=REFUSEE, heures=-2), _decision(statut=STATUT_REJEU, heures=-1), _decision("b.csv", heures=-3), ligne]

    assert retraitement.retrouver_arrivee_du_bronze(registre, ligne) == ligne.heure_arrivee


def test_l_arrivee_du_bronze_compte_la_ligne_meme_si_le_registre_donne_ne_la_contient_pas():
    ancienne, nouvelle = _decision(heures=0), _decision(version="3.0.0", heures=5)

    assert retraitement.retrouver_arrivee_du_bronze([], nouvelle) == nouvelle.heure_arrivee
    assert retraitement.retrouver_arrivee_du_bronze([ancienne], nouvelle) == ancienne.heure_arrivee


def test_le_resultat_est_immuable(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)
    resultat = _retraiter(_v3(contrat), ligne, bronze, coupees)

    with pytest.raises(FrozenInstanceError):
        resultat.reclassement = None


def test_le_retraitement_ne_modifie_pas_ses_entrees(tmp_path, contrat):
    ligne, bronze, coupees = _livraison(contrat, tmp_path)
    copies = (ligne, list(bronze), list(coupees))

    _retraiter(_v3(contrat), ligne, bronze, coupees)

    assert (ligne, bronze, coupees) == copies


# ==============================================================================
# 5. 🏭 LA JOURNÉE DE TEST : sous les mêmes règles, retraiter ne change rien à aucune livraison (attendus recomptés à part)
# ==============================================================================
@pytest.fixture(scope="module")
def etat_de_la_journee(contrat, tmp_path_factory):
    etat = Etat()
    catalogue = charger_catalogue("contracts/alertes/catalogue.yaml")
    jouer(contrat, catalogue, journee_multi_usines(contrat), tmp_path_factory.mktemp("journee"), etat)
    return etat


def _decisions_en_vigueur(registre):
    """Recomptées à part : pour chaque nom, sa dernière ligne dont les lignes entrent au bronze."""
    par_nom = {}
    for ligne in sorted(registre, key=lambda ligne: ligne.heure_arrivee):
        if ligne.statut in {ACCEPTEE, AVEC_REJETS, BLOQUEE}:
            par_nom[ligne.livraison_id] = ligne
    return par_nom


def test_la_selection_de_la_journee_est_celle_des_decisions_en_vigueur(contrat, etat_de_la_journee):
    attendues = _decisions_en_vigueur(etat_de_la_journee.registre)

    selection = retraitement.a_retraiter(etat_de_la_journee.registre, _v3(contrat))

    assert len(attendues) >= 8 and {ligne.livraison_id: ligne for ligne in selection} == attendues


def _silver(lignes):
    return sorted((s.numero_ligne, tuple(sorted(s.valeurs.items()))) for s in lignes)


def _quarantaine(lignes):
    return sorted(str((q.numero_ligne, q.numero_fichier, [v.code.value for v in q.violations])) for q in lignes)


def _bronze_de(etat, ligne):
    cle = (ligne.livraison_id, ligne.heure_arrivee)
    return ([b for b in etat.lignes_bronze if (b.livraison_id, b.heure_arrivee) == cle],
            [c for c in etat.lignes_coupees if (c.livraison_id, c.heure_arrivee) == cle])


def test_retraiter_deux_fois_toute_la_journee_de_suite_ne_reclasse_aucune_livraison(contrat, etat_de_la_journee):
    """Sous 3.0.0 puis sous 4.0.0, chaque livraison est retraitée depuis son bronze d'origine, dont l'arrivée se retrouve au registre."""
    etat, registre, retraitees = etat_de_la_journee, list(etat_de_la_journee.registre), 0
    for ligne in retraitement.a_retraiter(registre, _v3(contrat)):
        bronze, coupees = _bronze_de(etat, ligne)
        premier = _retraiter(_v3(contrat), ligne, bronze, coupees, deja=etat.lignes_silver)
        registre_apres = [*registre, premier.ligne_registre]
        choisies = retraitement.a_retraiter(registre_apres, replace(contrat, version=V4))
        en_vigueur = next(choisie for choisie in choisies if choisie.livraison_id == ligne.livraison_id)
        arrivee = retraitement.retrouver_arrivee_du_bronze(registre_apres, en_vigueur)

        second = retraitement.retraiter(replace(contrat, version=V4), en_vigueur, bronze, coupees, etat.lignes_silver,
                                        RETRAITE_LE + timedelta(days=1), arrivee_du_bronze=arrivee)

        assert en_vigueur == premier.ligne_registre and arrivee == ligne.heure_arrivee, ligne.livraison_id
        assert premier.reclassement is None and second.reclassement is None, ligne.livraison_id
        assert second.ligne_registre.version_contrat == V4, ligne.livraison_id
        retraitees += 1
    assert retraitees >= 8


def test_retraiter_toute_la_journee_sous_les_memes_regles_ne_reclasse_aucune_livraison(contrat, etat_de_la_journee):
    """Même contrat, autre version : chaque livraison retrouve le même statut, les mêmes comptes, le même silver et la même quarantaine."""
    etat = etat_de_la_journee
    retraitees = 0
    for ligne in retraitement.a_retraiter(etat.registre, _v3(contrat)):
        cle = (ligne.livraison_id, ligne.heure_arrivee)
        bronze = [b for b in etat.lignes_bronze if (b.livraison_id, b.heure_arrivee) == cle]
        coupees = [c for c in etat.lignes_coupees if (c.livraison_id, c.heure_arrivee) == cle]

        resultat = _retraiter(_v3(contrat), ligne, bronze, coupees, deja=etat.lignes_silver, heure=RETRAITE_LE)

        silver_avant = _silver(s for s in etat.lignes_silver if (s.livraison_id, s.heure_arrivee) == cle)
        quarantaine_avant = _quarantaine(q for q in etat.quarantaine if (q.livraison_id, q.heure_arrivee) == cle)
        assert resultat.reclassement is None, ligne.livraison_id
        assert (resultat.ligne_registre.statut, resultat.ligne_registre.nb_valides, resultat.ligne_registre.nb_rejetees) == (
            ligne.statut, ligne.nb_valides, ligne.nb_rejetees), ligne.livraison_id
        assert _silver(resultat.silver.lignes) == silver_avant, ligne.livraison_id
        assert _quarantaine(resultat.silver.quarantaine) == quarantaine_avant, ligne.livraison_id
        retraitees += 1
    assert retraitees >= 8
