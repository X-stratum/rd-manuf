"""Le GOLD : la production par jour, par pays, usine et ligne de fabrication, calculée depuis le silver.

Le calcul est une fonction pure : des lignes silver en entrée, des lignes gold en sortie, rien d'écrit. Les valeurs attendues des premiers
tests sont comptées ICI, à la main, jamais lues dans le résultat du code (la référence ne se juge pas elle-même) ; les règles sur la
journée multi-usines vérifient ce qui doit rester vrai quelles que soient les livraisons. Aucun moteur SQL ici : le SQL d'agrégation
viendra avec le déploiement, et se comparera à cette référence.

Les modules sont importés par leur nom (`gold.calculer_...`) pour qu'un module encore vide fasse échouer chaque test sur son assertion,
et non la collecte de tout le fichier.
"""
import subprocess
import sys
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from rd_manuf import gold, tables
from rd_manuf.alertes import charger_catalogue
from rd_manuf.config import CATALOGUE_PAR_DEFAUT
from rd_manuf.journee import Etat, jouer, journee_multi_usines
from rd_manuf.silver import LigneSilver

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"
JOUR = date(2026, 11, 12)
LENDEMAIN = date(2026, 11, 13)
ARRIVEE = datetime(2026, 11, 13, 6, 0, 0, tzinfo=UTC)
TOLERANCE = 1e-12
COLONNES_DU_GOLD = [
    "pays", "usine", "id_ligne", "date_production", "nb_lots", "quantite_produite", "quantite_rebut", "taux_rebut",
    "nb_mesures_temperature", "temperature_moyenne", "temperature_min", "temperature_max",
]
_compteur = iter(range(1, 10_000_000))


def _lot(produite, rebut, temperature=None, *, pays="FRA", usine="AA", ligne="L1", jour=JOUR):
    """Une ligne silver comme le pipeline la rend : les valeurs typées du contrat, la traçabilité autour."""
    numero = next(_compteur)
    return LigneSilver(
        livraison_id=f"livraison_production_{pays}_{usine}_001_2026-11-12-23-00-00Z.csv",
        heure_arrivee=ARRIVEE,
        pays=pays,
        usine=usine,
        numero_ligne=numero,
        version_contrat="2.0.0",
        valeurs={
            "id_lot": f"LOT-{numero:06d}", "id_ligne": ligne, "date_production": jour,
            "quantite_produite": produite, "quantite_rebut": rebut, "temperature_c": temperature,
        },
    )


def _une_ligne(lots):
    (ligne,) = gold.calculer_production_quotidienne(lots)
    return ligne


# ==============================================================================
# 1. 🧮 LES SOMMES ET LE TAUX : comptés à la main
# ==============================================================================
def test_deux_lots_du_meme_jour_et_de_la_meme_ligne_font_une_seule_ligne_aux_sommes_justes():
    ligne = _une_ligne([_lot(10, 1, 200.0), _lot(100_000, 99, 300.0)])

    assert (ligne.pays, ligne.usine, ligne.id_ligne, ligne.date_production) == ("FRA", "AA", "L1", JOUR)
    assert ligne.nb_lots == 2
    assert ligne.quantite_produite == 100_010
    assert ligne.quantite_rebut == 100


def test_le_taux_de_rebut_est_calcule_sur_les_sommes_pas_en_moyennant_les_taux_des_lots():
    """Un lot de 10 pièces à 10 % de rebut et un lot de 100 000 à 0,099 % : la moyenne des taux dirait 5 %, la somme dit 0,1 %."""
    ligne = _une_ligne([_lot(10, 1), _lot(100_000, 99)])

    assert ligne.taux_rebut == pytest.approx(100 / 100_010, abs=TOLERANCE)
    assert ligne.taux_rebut < 0.01


def test_un_seul_lot_donne_ses_propres_valeurs():
    ligne = _une_ligne([_lot(250, 5, 180.5)])

    assert (ligne.nb_lots, ligne.quantite_produite, ligne.quantite_rebut) == (1, 250, 5)
    assert ligne.taux_rebut == pytest.approx(0.02, abs=TOLERANCE)
    mesures = (ligne.nb_mesures_temperature, ligne.temperature_moyenne, ligne.temperature_min, ligne.temperature_max)
    assert mesures == (1, 180.5, 180.5, 180.5)


def test_une_production_nulle_donne_un_taux_vide_et_pas_une_division_par_zero():
    ligne = _une_ligne([_lot(0, 0), _lot(0, 0)])

    assert ligne.nb_lots == 2 and ligne.quantite_produite == 0 and ligne.quantite_rebut == 0
    assert ligne.taux_rebut is None


def test_un_lot_vide_avec_un_lot_normal_ne_change_pas_le_taux_de_la_ligne():
    ligne = _une_ligne([_lot(0, 0), _lot(200, 10)])

    assert ligne.nb_lots == 2 and ligne.taux_rebut == pytest.approx(0.05, abs=TOLERANCE)


def test_un_rebut_nul_donne_un_taux_de_zero_et_pas_un_taux_vide():
    ligne = _une_ligne([_lot(500, 0)])

    assert ligne.taux_rebut == 0.0


def test_les_quantites_sont_des_entiers_et_la_date_une_date():
    ligne = _une_ligne([_lot(10, 1), _lot(20, 2)])

    assert type(ligne.nb_lots) is int and type(ligne.quantite_produite) is int and type(ligne.quantite_rebut) is int
    assert type(ligne.date_production) is date and type(ligne.nb_mesures_temperature) is int


# ==============================================================================
# 2. 🌡️ LA TEMPÉRATURE : seulement les mesures présentes
# ==============================================================================
def test_la_temperature_est_calculee_sur_les_seules_mesures_presentes():
    ligne = _une_ligne([_lot(10, 0, None), _lot(10, 0, 100.0), _lot(10, 0, None), _lot(10, 0, 50.0)])

    assert ligne.nb_lots == 4 and ligne.nb_mesures_temperature == 2
    assert ligne.temperature_moyenne == pytest.approx(75.0, abs=TOLERANCE)
    assert (ligne.temperature_min, ligne.temperature_max) == (50.0, 100.0)


def test_sans_aucune_mesure_les_temperatures_sont_vides_et_le_compte_est_zero():
    ligne = _une_ligne([_lot(10, 0, None), _lot(20, 1, None)])

    assert ligne.nb_mesures_temperature == 0
    assert ligne.temperature_moyenne is None and ligne.temperature_min is None and ligne.temperature_max is None


def test_une_temperature_de_zero_est_une_mesure():
    ligne = _une_ligne([_lot(10, 0, 0.0), _lot(10, 0, None)])

    assert ligne.nb_mesures_temperature == 1
    assert (ligne.temperature_moyenne, ligne.temperature_min, ligne.temperature_max) == (0.0, 0.0, 0.0)


def test_des_temperatures_negatives_donnent_le_bon_minimum_et_la_bonne_moyenne():
    ligne = _une_ligne([_lot(10, 0, -10.0), _lot(10, 0, 5.0)])

    assert (ligne.temperature_min, ligne.temperature_max) == (-10.0, 5.0)
    assert ligne.temperature_moyenne == pytest.approx(-2.5, abs=TOLERANCE)


def test_la_moyenne_des_temperatures_ne_derive_pas():
    ligne = _une_ligne([_lot(1, 0, 0.1), _lot(1, 0, 0.2), _lot(1, 0, 0.3)])

    assert ligne.temperature_moyenne == pytest.approx(0.2, abs=TOLERANCE)


def test_la_temperature_est_brute_et_pas_arrondie():
    ligne = _une_ligne([_lot(1, 0, 100.0), _lot(1, 0, 100.0), _lot(1, 0, 101.0)])

    assert ligne.temperature_moyenne == pytest.approx(301 / 3, abs=TOLERANCE)
    assert ligne.temperature_moyenne != round(ligne.temperature_moyenne, 2)


# ==============================================================================
# 3. 🧱 LE GRAIN : pays, usine, ligne de fabrication, jour
# ==============================================================================
@pytest.mark.parametrize(
    "autre",
    [{"ligne": "L2"}, {"jour": LENDEMAIN}, {"usine": "AB"}, {"pays": "DEU"}],
    ids=["autre ligne", "autre jour", "autre usine", "autre pays"],
)
def test_un_lot_d_une_autre_ligne_d_un_autre_jour_d_une_autre_usine_ou_d_un_autre_pays_a_sa_propre_ligne(autre):
    lignes = gold.calculer_production_quotidienne([_lot(10, 1), _lot(20, 2, **autre)])

    assert len(lignes) == 2
    assert sorted(ligne.quantite_produite for ligne in lignes) == [10, 20]
    assert all(ligne.nb_lots == 1 for ligne in lignes)


def test_deux_pays_qui_numerotent_leurs_lots_pareil_ne_se_melangent_pas():
    france = _lot(100, 4, pays="FRA", usine="AA")
    allemagne = _lot(300, 6, pays="DEU", usine="AA")
    france.valeurs["id_lot"] = allemagne.valeurs["id_lot"] = "LOT-000042"

    lignes = gold.calculer_production_quotidienne([france, allemagne])

    assert {(ligne.pays, ligne.quantite_produite, ligne.nb_lots) for ligne in lignes} == {("FRA", 100, 1), ("DEU", 300, 1)}


def test_la_sortie_est_triee_par_pays_usine_ligne_puis_jour():
    lots = [
        _lot(1, 0, pays="USA", usine="CA", ligne="L1", jour=JOUR),
        _lot(1, 0, pays="FRA", usine="AA", ligne="L2", jour=JOUR),
        _lot(1, 0, pays="FRA", usine="AA", ligne="L1", jour=LENDEMAIN),
        _lot(1, 0, pays="FRA", usine="AA", ligne="L1", jour=JOUR),
        _lot(1, 0, pays="DEU", usine="BA", ligne="L3", jour=JOUR),
    ]

    cles = [(ligne.pays, ligne.usine, ligne.id_ligne, ligne.date_production) for ligne in gold.calculer_production_quotidienne(lots)]

    assert cles == [
        ("DEU", "BA", "L3", JOUR),
        ("FRA", "AA", "L1", JOUR),
        ("FRA", "AA", "L1", LENDEMAIN),
        ("FRA", "AA", "L2", JOUR),
        ("USA", "CA", "L1", JOUR),
    ]


def test_l_ordre_des_lignes_silver_ne_change_rien():
    lots = [_lot(10, 1, 100.0), _lot(20, 2, None, ligne="L2"), _lot(30, 3, 120.0), _lot(40, 4, None, jour=LENDEMAIN)]

    assert gold.calculer_production_quotidienne(lots) == gold.calculer_production_quotidienne(list(reversed(lots)))


def test_aucune_ligne_silver_donne_aucune_ligne_gold():
    assert gold.calculer_production_quotidienne([]) == []


def test_le_calcul_ne_modifie_pas_ses_entrees():
    lots = [_lot(10, 1, 100.0), _lot(20, 2)]
    avant = [(lot, dict(lot.valeurs)) for lot in lots]

    gold.calculer_production_quotidienne(lots)

    assert [(lot, lot.valeurs) for lot in lots] == avant


def test_la_ligne_gold_est_immuable():
    ligne = _une_ligne([_lot(10, 1)])

    with pytest.raises(AttributeError):
        ligne.nb_lots = 99


def test_les_champs_de_la_ligne_gold_sont_ceux_de_la_table_dans_le_meme_ordre():
    assert [champ for champ in gold.LigneProductionQuotidienne.__dataclass_fields__] == COLONNES_DU_GOLD


# ==============================================================================
# 4. 🚧 UNE LIGNE SILVER QUI N'A PAS CE QU'IL FAUT : une erreur claire, jamais un calcul faux
# ==============================================================================
@pytest.mark.parametrize("colonne", ["id_ligne", "date_production", "quantite_produite", "quantite_rebut"])
def test_une_valeur_obligatoire_absente_est_refusee_avec_le_nom_de_la_colonne(colonne):
    lot = _lot(10, 1)
    lot.valeurs[colonne] = None

    with pytest.raises(ValueError, match=colonne):
        gold.calculer_production_quotidienne([lot])


@pytest.mark.parametrize("colonne", ["id_ligne", "date_production", "quantite_produite", "quantite_rebut"])
def test_une_colonne_que_le_silver_ne_porte_plus_est_refusee_avec_son_nom(colonne):
    lot = _lot(10, 1)
    del lot.valeurs[colonne]

    with pytest.raises(ValueError, match=colonne):
        gold.calculer_production_quotidienne([lot])


def test_une_colonne_de_temperature_absente_du_silver_est_refusee_aussi():
    lot = _lot(10, 1)
    del lot.valeurs["temperature_c"]

    with pytest.raises(ValueError, match="temperature_c"):
        gold.calculer_production_quotidienne([lot])


def test_les_colonnes_lues_par_le_gold_existent_dans_le_contrat_avec_le_bon_type(contrat):
    """Le gold lit ces colonnes du silver par leur nom : si le contrat les renomme ou change leur type, la CI échoue ici."""
    par_nom = {colonne.nom: colonne for colonne in contrat.colonnes}

    assert set(gold.COLONNES_LUES) == {"id_ligne", "date_production", "quantite_produite", "quantite_rebut", "temperature_c"}
    attendu = {
        "id_ligne": "chaine", "date_production": "date", "quantite_produite": "entier", "quantite_rebut": "entier",
        "temperature_c": "decimal",
    }
    for nom, type_attendu in attendu.items():
        assert nom in par_nom and par_nom[nom].type == type_attendu, nom
    assert not par_nom["temperature_c"].obligatoire
    assert all(par_nom[nom].obligatoire for nom in ("id_ligne", "date_production", "quantite_produite", "quantite_rebut"))


# ==============================================================================
# 5. 🗄️ LA TABLE : ce qu'on écrira, avant de l'écrire
# ==============================================================================
def test_la_table_gold_a_son_schema_et_son_nom():
    assert tables.SCHEMA_GOLD == "gold"
    table = tables.PRODUCTION_QUOTIDIENNE
    assert (table.schema, table.nom) == ("gold", "production_quotidienne")


def test_les_colonnes_de_la_table_sont_les_champs_de_la_ligne_gold():
    assert [colonne.nom for colonne in tables.PRODUCTION_QUOTIDIENNE.colonnes] == COLONNES_DU_GOLD


def test_les_types_de_la_table_gold():
    types = {colonne.nom: colonne.type_sql for colonne in tables.PRODUCTION_QUOTIDIENNE.colonnes}

    assert types == {
        "pays": "STRING", "usine": "STRING", "id_ligne": "STRING", "date_production": "DATE",
        "nb_lots": "BIGINT", "quantite_produite": "BIGINT", "quantite_rebut": "BIGINT", "taux_rebut": "DOUBLE",
        "nb_mesures_temperature": "BIGINT", "temperature_moyenne": "DOUBLE", "temperature_min": "DOUBLE", "temperature_max": "DOUBLE",
    }


def test_une_colonne_de_la_table_gold_accepte_null_exactement_quand_le_champ_est_facultatif():
    indices = get_type_hints(gold.LigneProductionQuotidienne)
    facultatives = {nom for nom, indice in indices.items() if type(None) in get_args(indice)}

    assert facultatives == {"taux_rebut", "temperature_moyenne", "temperature_min", "temperature_max"}
    for colonne in tables.PRODUCTION_QUOTIDIENNE.colonnes:
        assert colonne.obligatoire == (colonne.nom not in facultatives), colonne.nom


def test_le_sql_de_creation_de_la_table_gold():
    sql = tables.sql_creation(tables.PRODUCTION_QUOTIDIENNE, CATALOGUE_PAR_DEFAUT)

    assert sql.startswith(f"CREATE TABLE IF NOT EXISTS `{CATALOGUE_PAR_DEFAUT}`.`gold`.`production_quotidienne` (")
    assert sql.endswith(") USING DELTA")
    for nom in COLONNES_DU_GOLD:
        assert f"`{nom}`" in sql
    assert "`taux_rebut` DOUBLE\n" in sql or "`taux_rebut` DOUBLE,\n" in sql          # facultative : pas de NOT NULL
    assert "`nb_lots` BIGINT NOT NULL" in sql


def test_la_table_gold_n_est_pas_encore_creee_avec_les_cinq_tables_du_silver(contrat):
    """Le gold se crée avec son propre écrivain, plus tard : le sondage et `creer_tables` ne connaissent que les cinq tables."""
    assert tables.PRODUCTION_QUOTIDIENNE not in tables.toutes_les_tables(contrat)


# ==============================================================================
# 6. 📅 LA JOURNÉE MULTI-USINES : ce qui doit rester vrai quelles que soient les livraisons
# ==============================================================================
@pytest.fixture(scope="module")
def etat_de_la_journee(contrat, tmp_path_factory):
    etat = Etat()
    jouer(contrat, charger_catalogue(CHEMIN_CATALOGUE), journee_multi_usines(contrat), tmp_path_factory.mktemp("jour-gold"), etat)
    return etat


def test_la_journee_a_de_quoi_etre_agregee(etat_de_la_journee):
    """Garde contre un test qui passerait à vide : plusieurs pays, usines et lignes de fabrication au silver."""
    lignes = etat_de_la_journee.lignes_silver

    assert len(lignes) > 30
    assert len({ligne.pays for ligne in lignes}) >= 3 and len({ligne.usine for ligne in lignes}) >= 3
    gold_du_jour = gold.calculer_production_quotidienne(lignes)
    assert len(gold_du_jour) >= 3
    assert any(ligne.nb_lots > 1 for ligne in gold_du_jour)               # et de vrais regroupements, pas seulement des lots isolés


def test_la_production_et_le_rebut_du_gold_sont_ceux_du_silver(etat_de_la_journee):
    silver = etat_de_la_journee.lignes_silver
    lignes = gold.calculer_production_quotidienne(silver)

    assert sum(ligne.quantite_produite for ligne in lignes) == sum(ligne.valeurs["quantite_produite"] for ligne in silver)
    assert sum(ligne.quantite_rebut for ligne in lignes) == sum(ligne.valeurs["quantite_rebut"] for ligne in silver)


def test_chaque_lot_du_silver_est_compte_une_fois_et_une_seule(etat_de_la_journee):
    silver = etat_de_la_journee.lignes_silver
    lignes = gold.calculer_production_quotidienne(silver)

    assert sum(ligne.nb_lots for ligne in lignes) == len(silver)
    assert sum(ligne.nb_mesures_temperature for ligne in lignes) == sum(ligne.valeurs["temperature_c"] is not None for ligne in silver)


def test_le_gold_de_la_journee_est_celui_d_un_regroupement_ecrit_a_part(etat_de_la_journee):
    """Le même regroupement, écrit autrement (un dictionnaire de listes) : chaque ligne du gold doit en être l'image."""
    groupes = defaultdict(list)
    for ligne in etat_de_la_journee.lignes_silver:
        groupes[(ligne.pays, ligne.usine, ligne.valeurs["id_ligne"], ligne.valeurs["date_production"])].append(ligne.valeurs)

    lignes = gold.calculer_production_quotidienne(etat_de_la_journee.lignes_silver)

    assert {(g.pays, g.usine, g.id_ligne, g.date_production) for g in lignes} == set(groupes)
    for ligne in lignes:
        valeurs = groupes[(ligne.pays, ligne.usine, ligne.id_ligne, ligne.date_production)]
        mesures = [v["temperature_c"] for v in valeurs if v["temperature_c"] is not None]
        produite = sum(v["quantite_produite"] for v in valeurs)
        assert ligne.nb_lots == len(valeurs)
        assert ligne.quantite_produite == produite
        assert ligne.quantite_rebut == sum(v["quantite_rebut"] for v in valeurs)
        assert ligne.nb_mesures_temperature == len(mesures)
        if produite:
            assert ligne.taux_rebut == pytest.approx(ligne.quantite_rebut / produite, abs=TOLERANCE)
        if mesures:
            assert (ligne.temperature_min, ligne.temperature_max) == (min(mesures), max(mesures))
            assert ligne.temperature_moyenne == pytest.approx(sum(mesures) / len(mesures), abs=1e-9)


def test_rejouer_la_journee_le_lendemain_ne_change_pas_le_gold(contrat, tmp_path):
    catalogue = charger_catalogue(CHEMIN_CATALOGUE)
    etat = Etat()
    journee = journee_multi_usines(contrat)
    jouer(contrat, catalogue, journee, tmp_path / "jour", etat)
    avant = gold.calculer_production_quotidienne(etat.lignes_silver)

    jouer(contrat, catalogue, journee, tmp_path / "lendemain", etat, decalage=timedelta(days=1))

    assert gold.calculer_production_quotidienne(etat.lignes_silver) == avant


def test_le_gold_de_la_journee_ne_depend_pas_de_l_ordre_du_silver(etat_de_la_journee):
    silver = etat_de_la_journee.lignes_silver

    assert gold.calculer_production_quotidienne(silver) == gold.calculer_production_quotidienne(list(reversed(silver)))


# ==============================================================================
# 7. 🔒 LE CALCUL NE TOUCHE À RIEN
# ==============================================================================
def test_le_calcul_n_ecrit_aucun_fichier(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    gold.calculer_production_quotidienne([_lot(10, 1, 100.0)])

    assert list(tmp_path.iterdir()) == []


def test_importer_le_module_n_importe_ni_spark_ni_le_sdk():
    code = (
        "import sys, rd_manuf.gold; "
        "sys.exit(1 if any(m.split('.')[0] in ('databricks', 'pyspark') for m in sys.modules) else 0)"
    )

    assert subprocess.run([sys.executable, "-c", code], check=False).returncode == 0
