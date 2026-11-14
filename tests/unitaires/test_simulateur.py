"""Tests du simulateur de livraisons : il fabrique des livraisons conformes au contrat, avec des anomalies demandées par code.

Le VALIDATEUR existant sert d'oracle : on ne recopie aucune règle du contrat dans ces tests. Si le simulateur dit « 3 lignes HORS_PLAGE »,
`valider_livraison` doit en trouver exactement 3, et rien d'autre.
"""
import csv
from collections import Counter
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from rd_manuf import simulateur as module_simulateur
from rd_manuf.contrat import CodeErreur, Colonne, StatutLivraison, valider_livraison
from rd_manuf.lecture import lire_parquet
from rd_manuf.nom_fichier import EXEMPLE, FORMATS, PREFIXE, NomFichier, NomFichierInvalide, lire_nom_fichier
from rd_manuf.reception import receptionner_fichier
from rd_manuf.simulateur import (
    VALEUR_TEXTE_INVALIDE,
    ecrire_livraison_csv,
    ecrire_livraison_parquet,
    generer_livraison,
    nom_de_fichier,
)
from rd_manuf.structure import CodeStructure

GRAINE = 42
NB_LIGNES = 20
NB_GRAINES_VERIFIEES = 10
NB_ANOMALIES = 3
# LIGNE_MALFORMEE est exclu : elle vient de la FORME du fichier (nombre de champs), pas d'une valeur de ligne.
# Le simulateur fabrique des lignes (dictionnaires) et le validateur de lignes ne la voit jamais :
# elle est produite par la lecture du fichier (voir test_reception.py).
CODES_PAR_VALEUR = [code for code in CodeErreur if code != CodeErreur.LIGNE_MALFORMEE]
TOUS_LES_CODES = pytest.mark.parametrize("code", CODES_PAR_VALEUR, ids=lambda code: code.value)


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


def test_demander_une_ligne_malformee_au_simulateur_est_une_erreur_claire(contrat):
    """Un code qui vient de la forme du fichier ne se fabrique pas par une valeur : mieux vaut un refus explicite qu'un KeyError."""
    with pytest.raises(ValueError, match="LIGNE_MALFORMEE"):
        generer_livraison(contrat, NB_LIGNES, GRAINE, anomalies={CodeErreur.LIGNE_MALFORMEE: 1})


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


# ==============================================================================
# 5. 🔢 LA NUMÉROTATION DES LOTS : `premier_lot` décale les numéros de lot, rien d'autre
# ==============================================================================
# Chaque usine numérote ses lots de son côté : deux livraisons d'une même usine doivent pouvoir recevoir des plages de numéros disjointes.
# Le format `LOT-` + 6 chiffres est celui du contrat (le seul que le simulateur connaisse) : un 7e chiffre serait refusé par le validateur.
DERNIER_NUMERO_DE_LOT = 999_999


def _numeros(lignes):
    return [int(ligne["id_lot"].removeprefix("LOT-")) for ligne in lignes]


def _sans_numero(lignes):
    return [{**ligne, "id_lot": None} for ligne in lignes]


def test_les_lots_sont_numerotes_a_partir_du_premier_lot_demande(contrat):
    assert _numeros(generer_livraison(contrat, 5, GRAINE, premier_lot=100)) == [100, 101, 102, 103, 104]


def test_sans_premier_lot_la_numerotation_commence_a_un(contrat):
    assert _numeros(generer_livraison(contrat, 3, GRAINE)) == [1, 2, 3]
    assert generer_livraison(contrat, NB_LIGNES, GRAINE) == generer_livraison(contrat, NB_LIGNES, GRAINE, premier_lot=1)


def test_le_premier_lot_ne_change_que_les_numeros_de_lot(contrat):
    decalee = generer_livraison(contrat, 50, GRAINE, premier_lot=500)

    assert _sans_numero(decalee) == _sans_numero(generer_livraison(contrat, 50, GRAINE))
    assert _numeros(decalee) != _numeros(generer_livraison(contrat, 50, GRAINE))


def test_deux_plages_de_numeros_disjointes_ne_partagent_aucune_cle(contrat):
    premiere = generer_livraison(contrat, NB_LIGNES, GRAINE, premier_lot=1)
    seconde = generer_livraison(contrat, NB_LIGNES, GRAINE + 1, premier_lot=NB_LIGNES + 1)

    assert not {ligne["id_lot"] for ligne in premiere} & {ligne["id_lot"] for ligne in seconde}


@TOUS_LES_CODES
def test_chaque_anomalie_reste_exacte_avec_un_autre_premier_lot(contrat_sans_seuil, code):
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, anomalies={code: NB_ANOMALIES}, premier_lot=1000)
    resultat = valider_livraison(contrat_sans_seuil, lignes)

    assert len(resultat.lignes_rejetees) == NB_ANOMALIES
    assert set(_violations_par_code(resultat)) == {code}


def test_les_bornes_de_la_numerotation_sont_acceptees(contrat):
    derniere = generer_livraison(contrat, 4, GRAINE, premier_lot=DERNIER_NUMERO_DE_LOT - 3)

    assert _numeros(derniere)[-1] == DERNIER_NUMERO_DE_LOT
    assert valider_livraison(contrat, derniere).statut == StatutLivraison.ACCEPTEE
    assert generer_livraison(contrat, 0, GRAINE, premier_lot=1) == []
    assert generer_livraison(contrat, 0, GRAINE, premier_lot=DERNIER_NUMERO_DE_LOT + 10) == []        # sans ligne, aucun numéro à écrire


@pytest.mark.parametrize(
    ("premier_lot", "nb_lignes"),
    [
        pytest.param(0, 5, id="zero"),
        pytest.param(-3, 5, id="negatif"),
        pytest.param(DERNIER_NUMERO_DE_LOT - 2, 4, id="le-dernier-lot-depasse-6-chiffres"),
        pytest.param(DERNIER_NUMERO_DE_LOT + 1, 1, id="le-premier-lot-depasse-6-chiffres"),
    ],
)
def test_une_numerotation_impossible_est_refusee(contrat, premier_lot, nb_lignes):
    with pytest.raises(ValueError, match="premier_lot"):
        generer_livraison(contrat, nb_lignes, GRAINE, premier_lot=premier_lot)


def test_le_numero_qui_suit_le_dernier_serait_refuse_par_le_contrat(contrat):
    """C'est la raison de la borne : un lot à 7 chiffres ne respecterait pas le motif du contrat."""
    ligne = {**generer_livraison(contrat, 1, GRAINE)[0], "id_lot": f"LOT-{DERNIER_NUMERO_DE_LOT + 1}"}

    assert set(_violations_par_code(valider_livraison(contrat, [ligne]))) == {CodeErreur.MOTIF_NON_RESPECTE}


# ==============================================================================
# 6. 🏷️ LE NOM DE FICHIER : fabriqué avec la convention de `nom_fichier.py`, que `lire_nom_fichier` valide
# ==============================================================================
INSTANT = datetime(2026, 11, 11, 14, 30, 5, tzinfo=UTC)


@pytest.mark.parametrize("format", FORMATS)
def test_un_nom_fabrique_est_relu_tel_qu_il_a_ete_demande(format):
    nom = nom_de_fichier("DEU", "BA", "002", INSTANT, format)

    assert lire_nom_fichier(nom) == NomFichier(pays="DEU", usine="BA", ligne="002", horodatage=INSTANT, format=format)


def test_le_nom_de_l_exemple_de_la_convention_est_celui_que_le_simulateur_fabrique():
    assert nom_de_fichier("FRA", "AA", "001", INSTANT, "csv") == EXEMPLE
    assert EXEMPLE.startswith(PREFIXE)


def test_un_instant_dans_un_autre_fuseau_est_ecrit_en_utc():
    a_paris = datetime(2026, 11, 11, 15, 30, 5, tzinfo=timezone(timedelta(hours=1)))

    assert nom_de_fichier("FRA", "AA", "001", a_paris, "csv") == EXEMPLE


def test_un_instant_sans_fuseau_est_refuse():
    with pytest.raises(ValueError, match="fuseau"):
        nom_de_fichier("FRA", "AA", "001", datetime(2026, 11, 11, 14, 30, 5), "csv")


def test_un_instant_avec_des_fractions_de_seconde_est_refuse():
    """Le nom s'arrête à la seconde : le relire donnerait un autre instant que celui demandé."""
    with pytest.raises(ValueError, match="seconde"):
        nom_de_fichier("FRA", "AA", "001", INSTANT.replace(microsecond=500_000), "csv")


@pytest.mark.parametrize(
    ("pays", "usine", "ligne", "format"),
    [
        pytest.param("fr", "AA", "001", "csv", id="pays-invalide"),
        pytest.param("FRA", "A", "001", "csv", id="usine-invalide"),
        pytest.param("FRA", "AA", "1", "csv", id="ligne-invalide"),
        pytest.param("FRA", "AA", "001", "txt", id="format-inconnu"),
    ],
)
def test_une_piece_qui_ne_suit_pas_la_convention_est_refusee_par_la_convention(pays, usine, ligne, format):
    with pytest.raises(NomFichierInvalide):
        nom_de_fichier(pays, usine, ligne, INSTANT, format)


# ==============================================================================
# 7. 🧱 FICHIER PARQUET : des colonnes TYPÉES d'après le contrat, que la réception juge comme un CSV
# ==============================================================================
NOM_CSV = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"
NOM_PARQUET = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.parquet"
# ALARME du contrat réel : valeurs en clair, elles échouent quand le contrat change et se mettent à jour à la main.
TYPES_DU_CONTRAT_REEL = [
    ("id_lot", pa.string()),
    ("id_ligne", pa.string()),
    ("date_production", pa.date32()),
    ("quantite_produite", pa.int64()),
    ("quantite_rebut", pa.int64()),
    ("temperature_c", pa.float64()),
]
# Une colonne Parquet est typée : une valeur qui n'a pas le type de sa colonne ne peut pas y être écrite. Ce défaut, `TYPE_INVALIDE`,
# n'existe donc qu'en CSV (voir plus bas) ; les autres se retrouvent à l'identique dans les deux formats.
CODES_ECRITS_EN_PARQUET = [code for code in CODES_PAR_VALEUR if code != CodeErreur.TYPE_INVALIDE]


def _ecrire_parquet(contrat, lignes, chemin):
    ecrire_livraison_parquet(contrat, lignes, chemin)
    return chemin


def _typee(ligne):
    return {**ligne, "date_production": date.fromisoformat(ligne["date_production"])}


def test_les_colonnes_du_parquet_ont_les_types_du_contrat_dans_son_ordre(tmp_path, contrat):
    chemin = _ecrire_parquet(contrat, generer_livraison(contrat, NB_LIGNES, GRAINE), tmp_path / NOM_PARQUET)

    assert [(champ.name, champ.type) for champ in pq.read_schema(chemin)] == TYPES_DU_CONTRAT_REEL
    assert [nom for nom, _ in TYPES_DU_CONTRAT_REEL] == [colonne.nom for colonne in contrat.colonnes]


@pytest.mark.parametrize(
    ("type_du_contrat", "valeur", "type_parquet"),
    [
        pytest.param("chaine", "a", pa.string(), id="chaine"),
        pytest.param("entier", 1, pa.int64(), id="entier"),
        pytest.param("decimal", 1.5, pa.float64(), id="decimal"),
        pytest.param("date", "2026-01-01", pa.date32(), id="date"),
    ],
)
def test_chaque_type_du_contrat_a_son_type_parquet(tmp_path, contrat, type_du_contrat, valeur, type_parquet):
    un_seul_type = replace(contrat, colonnes=(Colonne(nom="x", type=type_du_contrat),))

    chemin = _ecrire_parquet(un_seul_type, [{"x": valeur}], tmp_path / NOM_PARQUET)

    assert pq.read_schema(chemin).field("x").type == type_parquet


def test_les_valeurs_relues_sont_typees_et_une_absence_est_un_vrai_null(tmp_path, contrat):
    lignes = generer_livraison(contrat, 100, GRAINE)
    assert any(ligne["temperature_c"] is None for ligne in lignes), "la livraison doit contenir une absence, sinon ce test ne prouve rien"

    relues = lire_parquet(_ecrire_parquet(contrat, lignes, tmp_path / NOM_PARQUET)).lignes

    assert relues == [_typee(ligne) for ligne in lignes]
    assert isinstance(relues[0]["date_production"], date) and not isinstance(relues[0]["date_production"], datetime)


def test_une_livraison_saine_en_parquet_est_acceptee_par_la_reception(tmp_path, contrat):
    chemin = _ecrire_parquet(contrat, generer_livraison(contrat, 50, GRAINE), tmp_path / NOM_PARQUET)

    reception = receptionner_fichier(contrat, chemin)

    assert reception.statut == StatutLivraison.ACCEPTEE
    assert (reception.nb_recues, reception.nb_valides, reception.nb_rejetees) == (50, 50, 0)


def test_une_livraison_sans_ligne_est_un_parquet_vide_refuse_par_la_reception(tmp_path, contrat):
    chemin = _ecrire_parquet(contrat, [], tmp_path / NOM_PARQUET)

    reception = receptionner_fichier(contrat, chemin)

    assert lire_parquet(chemin).colonnes == [colonne.nom for colonne in contrat.colonnes]        # le schéma est écrit même sans ligne
    assert [probleme.code for probleme in reception.problemes_structure] == [CodeStructure.FICHIER_VIDE]


def _decision(reception):
    rejets = Counter(violation.code for rejet in reception.resultat.lignes_rejetees for violation in rejet.violations)
    return (reception.statut, reception.nb_recues, reception.nb_valides, reception.nb_rejetees, rejets)


@pytest.mark.parametrize("code", CODES_ECRITS_EN_PARQUET, ids=lambda code: code.value)
def test_le_csv_et_le_parquet_d_une_meme_livraison_recoivent_la_meme_decision(tmp_path, contrat_sans_seuil, code):
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, anomalies={code: NB_ANOMALIES})
    chemin_csv = tmp_path / NOM_CSV
    ecrire_livraison_csv(contrat_sans_seuil, lignes, chemin_csv)
    chemin_parquet = _ecrire_parquet(contrat_sans_seuil, lignes, tmp_path / NOM_PARQUET)

    en_csv = receptionner_fichier(contrat_sans_seuil, chemin_csv)
    en_parquet = receptionner_fichier(contrat_sans_seuil, chemin_parquet)

    assert en_csv.nb_rejetees == NB_ANOMALIES and set(_decision(en_csv)[4]) == {code}        # l'anomalie est bien là, dans les deux
    assert _decision(en_parquet) == _decision(en_csv)


def test_un_texte_dans_une_colonne_numerique_ne_peut_pas_etre_ecrit_en_parquet(tmp_path, contrat_sans_seuil):
    lignes = generer_livraison(contrat_sans_seuil, NB_LIGNES, GRAINE, anomalies={CodeErreur.TYPE_INVALIDE: 1})
    chemin = tmp_path / NOM_PARQUET

    with pytest.raises(ValueError) as erreur:
        ecrire_livraison_parquet(contrat_sans_seuil, lignes, chemin)

    assert "quantite_produite" in str(erreur.value) and VALEUR_TEXTE_INVALIDE in str(erreur.value)
    assert not chemin.exists()                                                    # aucun fichier à moitié écrit


def _ligne_avec(contrat, colonne, valeur):
    return {**generer_livraison(contrat, 1, GRAINE)[0], colonne: valeur}


@pytest.mark.parametrize(
    ("colonne", "valeur"),
    [
        pytest.param("quantite_produite", 1.5, id="decimal-dans-une-colonne-entiere"),
        pytest.param("quantite_produite", True, id="booleen-dans-une-colonne-entiere"),
        pytest.param("temperature_c", True, id="booleen-dans-une-colonne-decimale"),
        pytest.param("temperature_c", float("inf"), id="infini-dans-une-colonne-decimale"),
        pytest.param("date_production", datetime(2026, 1, 1, 12, 0, tzinfo=UTC), id="date-avec-heure-dans-une-colonne-date"),
    ],
)
def test_une_valeur_que_le_contrat_refuse_comme_type_invalide_ne_s_ecrit_pas_en_parquet(tmp_path, contrat_sans_seuil, colonne, valeur):
    """Parquet convertirait ces valeurs sans rien dire (1.5 en 1, une heure perdue) : le fichier ne dirait plus ce que la ligne disait."""
    ligne = _ligne_avec(contrat_sans_seuil, colonne, valeur)
    chemin = tmp_path / NOM_PARQUET

    assert set(_violations_par_code(valider_livraison(contrat_sans_seuil, [ligne]))) == {CodeErreur.TYPE_INVALIDE}    # le contrat la refuse
    with pytest.raises(ValueError) as erreur:
        ecrire_livraison_parquet(contrat_sans_seuil, [ligne], chemin)

    assert colonne in str(erreur.value)
    assert not chemin.exists()


@pytest.mark.parametrize(
    ("colonne", "valeur"),
    [
        pytest.param("quantite_produite", "5", id="texte-numerique-dans-une-colonne-entiere"),
        pytest.param("quantite_produite", 2**70, id="entier-trop-grand-pour-64-bits"),
        pytest.param("id_lot", 42, id="nombre-dans-une-colonne-texte"),
    ],
)
def test_une_valeur_qu_une_colonne_parquet_ne_peut_pas_contenir_est_refusee_avec_sa_colonne(tmp_path, contrat, colonne, valeur):
    lignes = generer_livraison(contrat, 3, GRAINE)
    lignes[2][colonne] = valeur                      # la fautive est la dernière : la première valeur de la colonne est saine
    chemin = tmp_path / NOM_PARQUET

    with pytest.raises(ValueError) as erreur:
        ecrire_livraison_parquet(contrat, lignes, chemin)

    assert colonne in str(erreur.value) and f"« {valeur} »" in str(erreur.value)
    assert not chemin.exists()


def test_si_aucune_valeur_fautive_n_est_retrouvee_l_erreur_reste_une_value_error(tmp_path, contrat, monkeypatch):
    """Garde défensive : Parquet refuse la colonne, aucune valeur seule ne semble fautive ; pas de StopIteration qui fuit."""
    monkeypatch.setattr(module_simulateur, "_est_ecrivable", lambda valeur, type_arrow: True)

    with pytest.raises(ValueError, match="quantite_produite"):
        ecrire_livraison_parquet(contrat, [_ligne_avec(contrat, "quantite_produite", "5")], tmp_path / NOM_PARQUET)


def test_une_date_qui_n_en_est_pas_une_ne_peut_pas_etre_ecrite_en_parquet(tmp_path, contrat):
    lignes = [{**generer_livraison(contrat, 1, GRAINE)[0], "date_production": "pas une date"}]

    with pytest.raises(ValueError, match="date_production"):
        ecrire_livraison_parquet(contrat, lignes, tmp_path / NOM_PARQUET)


def test_l_ecriture_ne_modifie_pas_les_lignes_recues(tmp_path, contrat):
    lignes = generer_livraison(contrat, NB_LIGNES, GRAINE)
    copie = [dict(ligne) for ligne in lignes]

    _ecrire_parquet(contrat, lignes, tmp_path / NOM_PARQUET)

    assert lignes == copie                                                         # les dates restent du texte ISO, comme pour le CSV
