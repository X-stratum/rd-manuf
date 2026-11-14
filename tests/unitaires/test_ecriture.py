"""Tests de l'ÉCRIVAIN, en local : transformer ce que le pipeline produit en enregistrements de tables, et l'écrire sans jamais se répéter.

Trois couches, de la plus pure à la plus proche de Databricks : les CONVERSIONS (une ligne du pipeline devient un enregistrement aux types
des schémas, et revient identique), le MAGASIN (insérer ce qui manque sur une clé, lire dans l'ordre ; en mémoire pour référence, en SQL
pour Databricks) et l'ÉTAT adossé à un magasin, qui expose les mêmes noms que `journee.Etat` : `jouer` fonctionne donc sans changement.

Ces tests prouvent la génération du SQL et le protocole d'écriture. La sémantique réelle de Delta (`MERGE`, types, tableaux, fuseau d'une
colonne TIMESTAMP) ne se prouve que sur un vrai workspace : voir le journal des changements.
"""
import copy
import json
import re
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from rd_manuf.alertes import charger_catalogue
from rd_manuf.config import ErreurEnvironnement
from rd_manuf.ecriture import EtatMagasin, MagasinEnMemoire, MagasinSql, couches, creer_tables, en_json
from rd_manuf.journee import Attendu, Etat, jouer, journee_multi_usines, verifier_invariants
from rd_manuf.tables import (
    BRONZE_COUPEES,
    QUARANTAINE,
    REGISTRE,
    SCHEMA_BRONZE,
    SCHEMA_OPS,
    SCHEMA_SILVER,
    TYPE_VIOLATIONS,
    ColonneTable,
    sql_creation,
    table_bronze,
    table_silver,
    toutes_les_tables,
)

CHEMIN_CATALOGUE = Path(__file__).resolve().parents[2] / "contracts" / "alertes" / "catalogue.yaml"
CATALOGUE_SQL = "rd_manuf_playground"
COUCHES = ["registre", "bronze", "coupees", "silver", "quarantaine"]
TOUTES_LES_COUCHES = pytest.mark.parametrize("nom", COUCHES)
# Les clés, écrites ICI à part du module : l'idempotence de l'écriture repose sur elles, un changement doit se voir.
CLES_ATTENDUES = {
    "registre": ("livraison_id", "heure_arrivee"),
    "bronze": ("livraison_id", "heure_arrivee", "numero_ligne"),
    "coupees": ("livraison_id", "heure_arrivee", "numero_fichier"),
    "silver": ("livraison_id", "heure_arrivee", "numero_ligne"),
    "quarantaine": ("livraison_id", "heure_arrivee", "numero_ligne", "numero_fichier"),
}
HOSTILES = [
    "x'); DROP TABLE registre; --",
    "guillemet ' simple et \" double",
    "barre \\ inverse et \\' mêlés",
    "ligne\nsuivante\r\navec retours",
    "accents éèà € 日本語 \U0001f600",
    "séparateurs , ; | et `accent grave`",
]


@pytest.fixture(scope="module")
def catalogue():
    return charger_catalogue(CHEMIN_CATALOGUE)


@pytest.fixture(scope="module")
def journee(contrat):
    return journee_multi_usines(contrat)


@pytest.fixture(scope="module")
def jouee(contrat, catalogue, journee, tmp_path_factory):
    etat = Etat()
    resultats = jouer(contrat, catalogue, journee, tmp_path_factory.mktemp("jour"), etat)
    return etat, resultats


@pytest.fixture(scope="module")
def jouee_en_magasin(contrat, catalogue, journee, tmp_path_factory):
    etat = EtatMagasin(contrat, MagasinEnMemoire())
    resultats = jouer(contrat, catalogue, journee, tmp_path_factory.mktemp("jour-magasin"), etat)
    return etat, resultats


def _objets(nom, resultats):
    """Les objets d'une couche produits par une liste de résultats."""
    if nom == "registre":
        return [resultat.ligne_registre for resultat in resultats]
    extraire = {
        "bronze": lambda r: r.lignes_bronze,
        "coupees": lambda r: r.lignes_coupees,
        "silver": lambda r: r.silver.lignes,
        "quarantaine": lambda r: r.silver.quarantaine,
    }[nom]
    return [objet for resultat in resultats for objet in extraire(resultat)]


def _nom_d_attribut(nom):
    return {"registre": "registre", "bronze": "lignes_bronze", "coupees": "lignes_coupees", "silver": "lignes_silver",
            "quarantaine": "quarantaine"}[nom]


def _tri(valeur):
    """None d'abord, comme Spark pour un tri croissant : une clé à partie absente se range avant les autres."""
    return (valeur is not None, valeur if valeur is not None else 0)


def _trier(objets, couche):
    return sorted(objets, key=lambda objet: tuple(_tri(couche.vers_enregistrement(objet)[nom]) for nom in couche.ordre))


# ==============================================================================
# 1. 🧬 LES CONVERSIONS : une ligne du pipeline, un enregistrement de table, et le chemin inverse
# ==============================================================================
def _conforme(valeur, type_sql):
    """Une valeur a-t-elle le type Python qui correspond au type SQL de sa colonne ?"""
    if type_sql == "STRING":
        return isinstance(valeur, str)
    if type_sql == "BIGINT":
        return isinstance(valeur, int) and not isinstance(valeur, bool)
    if type_sql == "DOUBLE":
        return isinstance(valeur, float)
    if type_sql == "DATE":
        return isinstance(valeur, date) and not isinstance(valeur, datetime)
    if type_sql == "TIMESTAMP":
        return isinstance(valeur, datetime) and valeur.utcoffset() == timedelta(0)
    if type_sql == "ARRAY<STRING>":
        return isinstance(valeur, list) and all(isinstance(element, str) for element in valeur)
    if type_sql == "MAP<STRING, STRING>":
        return isinstance(valeur, dict) and all(isinstance(cle, str) and (v is None or isinstance(v, str)) for cle, v in valeur.items())
    if type_sql == TYPE_VIOLATIONS:
        return isinstance(valeur, list) and all(
            set(element) == {"code", "colonnes", "message"} and isinstance(element["code"], str)
            and _conforme(element["colonnes"], "ARRAY<STRING>") and isinstance(element["message"], str) for element in valeur
        )
    raise AssertionError(f"type SQL non prévu par le test : {type_sql}")


def test_il_y_a_une_couche_par_table_avec_la_table_de_ses_schemas(contrat):
    toutes = couches(contrat)

    assert list(toutes) == COUCHES
    assert [toutes[nom].table for nom in COUCHES] == [REGISTRE, table_bronze(contrat), BRONZE_COUPEES, table_silver(contrat), QUARANTAINE]
    assert [couche.table for couche in toutes.values()] == list(toutes_les_tables(contrat))


@TOUTES_LES_COUCHES
def test_les_cles_de_chaque_couche_sont_celles_de_la_specification(contrat, nom):
    assert nom in couches(contrat)
    couche = couches(contrat)[nom]

    assert couche.cles == CLES_ATTENDUES[nom]
    assert couche.ordre == ("heure_arrivee", "livraison_id", *couche.cles[2:]), "tri de lecture : l'arrivée d'abord, puis la clé"


@TOUTES_LES_COUCHES
def test_les_colonnes_de_cle_existent_et_ne_sont_nulles_que_dans_la_quarantaine(contrat, nom):
    assert nom in couches(contrat)
    couche = couches(contrat)[nom]
    colonnes = {colonne.nom: colonne for colonne in couche.table.colonnes}

    assert set(couche.cles) <= set(colonnes)
    peuvent_etre_nulles = {cle for cle in couche.cles if not colonnes[cle].obligatoire}
    assert peuvent_etre_nulles == ({"numero_ligne", "numero_fichier"} if nom == "quarantaine" else set())


@TOUTES_LES_COUCHES
def test_un_enregistrement_a_les_colonnes_et_les_types_de_sa_table(contrat, jouee, nom):
    assert nom in couches(contrat)
    couche = couches(contrat)[nom]
    objets = _objets(nom, jouee[1])
    assert objets, "la journée doit fournir de quoi tester cette couche"

    for objet in objets:
        enregistrement = couche.vers_enregistrement(objet)
        assert list(enregistrement) == [colonne.nom for colonne in couche.table.colonnes], "mêmes colonnes, dans l'ordre de la table"
        for colonne in couche.table.colonnes:
            valeur = enregistrement[colonne.nom]
            if valeur is None:
                assert not colonne.obligatoire, f"{colonne.nom} est obligatoire : {objet}"
            else:
                assert _conforme(valeur, colonne.type_sql), f"{colonne.nom} ({colonne.type_sql}) : {valeur!r}"


@TOUTES_LES_COUCHES
def test_un_enregistrement_relu_donne_l_objet_d_origine(contrat, jouee, nom):
    assert nom in couches(contrat)
    couche = couches(contrat)[nom]

    for objet in _objets(nom, jouee[1]):
        assert couche.depuis_enregistrement(couche.vers_enregistrement(objet)) == objet


@TOUTES_LES_COUCHES
def test_les_cles_sont_uniques_dans_la_journee(contrat, jouee, nom):
    """L'écriture est un « insérer ce qui manque » sur la clé : deux objets distincts à la même clé s'écraseraient."""
    assert nom in couches(contrat)
    couche = couches(contrat)[nom]
    cles = [tuple(couche.vers_enregistrement(objet)[cle] for cle in couche.cles) for objet in _objets(nom, jouee[1])]

    assert len(cles) == len(set(cles)) > 0


def test_la_quarantaine_a_deux_formes_dont_une_seule_colonne_est_remplie(contrat, jouee):
    couche = couches(contrat)["quarantaine"]
    enregistrements = [couche.vers_enregistrement(objet) for objet in _objets("quarantaine", jouee[1])]
    bien_formees = [e for e in enregistrements if e["contenu_texte"] is not None]
    coupees = [e for e in enregistrements if e["champs_bruts"] is not None]

    assert bien_formees and coupees, "la journée doit contenir les deux formes, sinon ce test ne prouve rien"
    assert len(bien_formees) + len(coupees) == len(enregistrements)
    assert all(e["champs_bruts"] is None and e["numero_ligne"] is not None and e["numero_fichier"] is None for e in bien_formees)
    assert all(e["contenu_texte"] is None and e["numero_ligne"] is None and e["numero_fichier"] is not None for e in coupees)


def test_les_valeurs_inhabituelles_font_l_aller_retour(contrat, jouee):
    toutes = couches(contrat)
    silver = next(ligne for ligne in _objets("silver", jouee[1]) if ligne.valeurs["temperature_c"] is not None)
    quarantaine = next(ligne for ligne in _objets("quarantaine", jouee[1]) if isinstance(ligne.contenu, dict))
    registre = next(ligne for ligne in _objets("registre", jouee[1]) if ligne.statut == "REFUSEE")
    instant = datetime(2026, 11, 12, 9, 0, 0, 123456, tzinfo=UTC)
    cas = {
        "silver": replace(silver, livraison_id=HOSTILES[0], heure_arrivee=instant,
                          valeurs={**silver.valeurs, "id_lot": HOSTILES[3], "temperature_c": 0.1 + 0.2, "date_production": date(1, 1, 1)}),
        "quarantaine": replace(quarantaine, livraison_id=HOSTILES[4], heure_arrivee=instant,
                               contenu={**quarantaine.contenu, "id_lot": HOSTILES[1], "id_ligne": None}),
        "registre": replace(registre, livraison_id=HOSTILES[2], heure_arrivee=instant, horodatage_fichier=instant - timedelta(days=1),
                            codes_refus=(), empreinte=None),
    }

    for nom, objet in cas.items():
        enregistrement = toutes[nom].vers_enregistrement(objet)
        assert toutes[nom].depuis_enregistrement(enregistrement) == objet
        assert enregistrement["heure_arrivee"].microsecond == 123456


def test_un_instant_d_un_autre_fuseau_est_ecrit_en_utc_et_relu_au_meme_instant(contrat, jouee):
    couche = couches(contrat)["registre"]
    registre = jouee[1][0].ligne_registre
    a_paris = datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=1)))

    enregistrement = couche.vers_enregistrement(replace(registre, heure_arrivee=a_paris))

    assert enregistrement["heure_arrivee"].utcoffset() == timedelta(0)
    assert couche.depuis_enregistrement(enregistrement).heure_arrivee == a_paris


@TOUTES_LES_COUCHES
def test_chaque_couche_ecrit_l_heure_d_arrivee_en_utc_et_refuse_une_heure_sans_fuseau(contrat, jouee, nom):
    assert nom in couches(contrat)
    couche = couches(contrat)[nom]
    objet = _objets(nom, jouee[1])[0]
    a_paris = datetime(2026, 11, 12, 10, 0, tzinfo=timezone(timedelta(hours=1)))

    enregistrement = couche.vers_enregistrement(replace(objet, heure_arrivee=a_paris))

    assert enregistrement["heure_arrivee"].utcoffset() == timedelta(0) and enregistrement["heure_arrivee"] == a_paris
    with pytest.raises(ValueError, match="fuseau"):
        couche.vers_enregistrement(replace(objet, heure_arrivee=datetime(2026, 11, 12, 9, 0)))


def test_l_horodatage_du_fichier_est_ecrit_en_utc_et_refuse_sans_fuseau(contrat, jouee):
    couche = couches(contrat)["registre"]
    registre = next(ligne for ligne in _objets("registre", jouee[1]) if ligne.horodatage_fichier is not None)
    a_paris = datetime(2026, 11, 12, 8, 0, tzinfo=timezone(timedelta(hours=1)))

    enregistrement = couche.vers_enregistrement(replace(registre, horodatage_fichier=a_paris))

    assert enregistrement["horodatage_fichier"].utcoffset() == timedelta(0) and enregistrement["horodatage_fichier"] == a_paris
    with pytest.raises(ValueError, match="fuseau"):
        couche.vers_enregistrement(replace(registre, horodatage_fichier=datetime(2026, 11, 12, 8, 0)))


@pytest.mark.parametrize("nom", ["bronze", "silver"])
def test_des_valeurs_qui_ne_sont_pas_les_colonnes_du_contrat_sont_refusees(contrat, jouee, nom):
    """Une colonne de trop ou en moins est une erreur du code, jamais ignorée : elle ferait écrire une ligne qui n'est pas la bonne."""
    couche = couches(contrat)[nom]
    ligne = _objets(nom, jouee[1])[0]
    en_trop = replace(ligne, valeurs={**ligne.valeurs, "colonne_en_trop": "x"})
    en_moins = replace(ligne, valeurs={cle: valeur for cle, valeur in ligne.valeurs.items() if cle != "id_lot"})

    for fautive in (en_trop, en_moins):
        with pytest.raises(ValueError, match="Colonnes"):
            couche.vers_enregistrement(fautive)


@pytest.mark.parametrize("nom", ["bronze", "silver"])
def test_les_colonnes_d_un_enregistrement_suivent_la_table_meme_si_les_valeurs_sont_rangees_autrement(contrat, jouee, nom):
    couche = couches(contrat)[nom]
    ligne = _objets(nom, jouee[1])[0]
    a_l_envers = replace(ligne, valeurs=dict(reversed(list(ligne.valeurs.items()))))

    enregistrement = couche.vers_enregistrement(a_l_envers)

    assert list(enregistrement) == [colonne.nom for colonne in couche.table.colonnes]
    assert couche.depuis_enregistrement(enregistrement) == a_l_envers


def test_un_instant_sans_fuseau_est_refuse(contrat, jouee):
    couche = couches(contrat)["registre"]

    with pytest.raises(ValueError, match="fuseau"):
        couche.vers_enregistrement(replace(jouee[1][0].ligne_registre, heure_arrivee=datetime(2026, 11, 12, 9, 0)))


def test_modifier_un_enregistrement_ne_modifie_pas_l_objet(contrat, jouee):
    couche = couches(contrat)["registre"]
    refusee = next(ligne for ligne in _objets("registre", jouee[1]) if ligne.codes_refus)
    avant = copy.deepcopy(refusee)

    enregistrement = couche.vers_enregistrement(refusee)
    enregistrement["codes_refus"].append("AUTRE")
    enregistrement["statut"] = "MODIFIE"

    assert refusee == avant


# ==============================================================================
# 2. 🗄️ LE MAGASIN EN MÉMOIRE : insérer ce qui manque sur une clé, lire dans l'ordre
# ==============================================================================
def _registre(livraison, heure, statut="ACCEPTEE"):
    return {"livraison_id": livraison, "statut": statut, "version_contrat": "2.0.0", "nb_recues": 1, "nb_valides": 1, "nb_rejetees": 0,
            "codes_refus": [], "pays": None, "usine": None, "ligne": None, "horodatage_fichier": None, "heure_arrivee": heure,
            "empreinte": None, "rejeu_de": None}


def _quarantaine(livraison, heure, numero_ligne, numero_fichier):
    return {"livraison_id": livraison, "heure_arrivee": heure, "version_contrat": "2.0.0", "numero_ligne": numero_ligne,
            "numero_fichier": numero_fichier, "contenu_texte": None, "champs_bruts": None, "violations": []}


HEURE = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)
CLES_REGISTRE = ("livraison_id", "heure_arrivee")
CLES_QUARANTAINE = ("livraison_id", "heure_arrivee", "numero_ligne", "numero_fichier")


def test_un_magasin_neuf_est_vide_pour_toutes_les_tables():
    magasin = MagasinEnMemoire()

    assert magasin.lire(REGISTRE, ("heure_arrivee",)) == [] and magasin.lire(QUARANTAINE, ("heure_arrivee",)) == []


def test_les_enregistrements_absents_sont_inseres_et_relus():
    magasin = MagasinEnMemoire()
    lignes = [_registre("a.csv", HEURE), _registre("b.csv", HEURE)]

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, lignes)

    assert magasin.lire(REGISTRE, ("heure_arrivee", "livraison_id")) == lignes


def test_un_enregistrement_deja_present_est_ignore_et_l_ancien_est_garde():
    magasin = MagasinEnMemoire()
    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [_registre("a.csv", HEURE, "ACCEPTEE")])

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [_registre("a.csv", HEURE, "BLOQUEE"), _registre("b.csv", HEURE)])

    lus = magasin.lire(REGISTRE, ("livraison_id",))
    assert [(ligne["livraison_id"], ligne["statut"]) for ligne in lus] == [("a.csv", "ACCEPTEE"), ("b.csv", "ACCEPTEE")]


def test_deux_enregistrements_de_la_meme_ecriture_a_la_meme_cle_n_en_font_qu_un():
    magasin = MagasinEnMemoire()

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [_registre("a.csv", HEURE, "ACCEPTEE"), _registre("a.csv", HEURE, "BLOQUEE")])

    assert [ligne["statut"] for ligne in magasin.lire(REGISTRE, ("livraison_id",))] == ["ACCEPTEE"]


def test_une_cle_a_partie_absente_est_comparee_comme_les_autres():
    """`NULL = NULL` n'est pas vrai en SQL : sans l'égalité tolérante au NULL (`<=>`), ces lignes seraient réinsérées à chaque reprise."""
    magasin = MagasinEnMemoire()
    ligne = _quarantaine("a.csv", HEURE, None, 7)

    magasin.inserer_absentes(QUARANTAINE, CLES_QUARANTAINE, [ligne])
    magasin.inserer_absentes(QUARANTAINE, CLES_QUARANTAINE, [_quarantaine("a.csv", HEURE, None, 7)])
    magasin.inserer_absentes(QUARANTAINE, CLES_QUARANTAINE, [_quarantaine("a.csv", HEURE, 7, None)])

    assert len(magasin.lire(QUARANTAINE, ("heure_arrivee",))) == 2


def test_les_tables_ne_partagent_pas_leurs_enregistrements():
    magasin = MagasinEnMemoire()

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [_registre("a.csv", HEURE)])
    magasin.inserer_absentes(BRONZE_COUPEES, ("livraison_id", "heure_arrivee", "numero_fichier"),
                             [{"livraison_id": "a.csv", "heure_arrivee": HEURE, "numero_fichier": 1, "champs": ["x"]}])

    assert len(magasin.lire(REGISTRE, ("heure_arrivee",))) == 1 and len(magasin.lire(BRONZE_COUPEES, ("heure_arrivee",))) == 1


def test_la_lecture_est_triee_avec_les_valeurs_absentes_d_abord():
    magasin = MagasinEnMemoire()
    desordre = [_quarantaine("b.csv", HEURE, 2, None), _quarantaine("a.csv", HEURE, 5, None), _quarantaine("a.csv", HEURE, None, 3),
                _quarantaine("a.csv", HEURE + timedelta(hours=1), 1, None)]
    magasin.inserer_absentes(QUARANTAINE, CLES_QUARANTAINE, desordre)

    lus = magasin.lire(QUARANTAINE, ("heure_arrivee", "livraison_id", "numero_ligne", "numero_fichier"))

    assert [(ligne["livraison_id"], ligne["heure_arrivee"].hour, ligne["numero_ligne"], ligne["numero_fichier"]) for ligne in lus] == [
        ("a.csv", 9, None, 3), ("a.csv", 9, 5, None), ("b.csv", 9, 2, None), ("a.csv", 10, 1, None)]


def test_le_magasin_ne_partage_rien_avec_ce_qu_on_lui_donne_ni_ce_qu_il_rend():
    magasin = MagasinEnMemoire()
    ligne = _registre("a.csv", HEURE)
    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [ligne])

    ligne["statut"] = "MODIFIE"
    ligne["codes_refus"].append("X")
    magasin.lire(REGISTRE, ("heure_arrivee",))[0]["statut"] = "MODIFIE AUSSI"
    magasin.lire(REGISTRE, ("heure_arrivee",)).clear()

    relu = magasin.lire(REGISTRE, ("heure_arrivee",))
    assert [(r["statut"], r["codes_refus"]) for r in relu] == [("ACCEPTEE", [])]


# ==============================================================================
# 3. 📚 L'ÉTAT ADOSSÉ À UN MAGASIN : les mêmes noms que `Etat`, la même journée
# ==============================================================================
@TOUTES_LES_COUCHES
def test_la_journee_jouee_dans_un_magasin_donne_le_meme_etat_que_la_journee_en_memoire(contrat, jouee, jouee_en_magasin, nom):
    couche = couches(contrat)[nom] if nom in couches(contrat) else None
    assert couche is not None
    attribut = _nom_d_attribut(nom)

    en_memoire = getattr(jouee[0], attribut)
    dans_le_magasin = getattr(jouee_en_magasin[0], attribut)

    assert len(en_memoire) > 0
    assert _trier(dans_le_magasin, couche) == _trier(en_memoire, couche)


@TOUTES_LES_COUCHES
def test_la_lecture_du_magasin_est_dans_l_ordre_de_la_table(contrat, jouee_en_magasin, nom):
    assert nom in couches(contrat)
    couche = couches(contrat)[nom]
    lus = getattr(jouee_en_magasin[0], _nom_d_attribut(nom))

    assert lus == _trier(lus, couche)


def test_les_alertes_de_la_journee_sont_gardees_en_memoire_comme_dans_etat(jouee, jouee_en_magasin):
    assert jouee_en_magasin[0].alertes == jouee[0].alertes and len(jouee[0].alertes) > 0


def test_les_resultats_de_la_journee_sont_ceux_de_la_journee_en_memoire(journee, jouee, jouee_en_magasin):
    en_magasin, en_memoire = ([Attendu.observe(r) for r in resultats] for resultats in (jouee_en_magasin[1], jouee[1]))
    assert en_magasin == en_memoire == [livraison.attendu for livraison in journee]


def test_les_invariants_de_la_journee_tiennent_dans_un_magasin(contrat, catalogue, jouee_en_magasin):
    etat, resultats = jouee_en_magasin

    assert len(etat.registre) == len(resultats) > 0
    assert verifier_invariants(contrat, catalogue, etat, resultats) == []


def test_la_journee_rejouee_dans_un_magasin_n_ajoute_que_des_lignes_de_registre(contrat, catalogue, journee, tmp_path):
    etat = EtatMagasin(contrat, MagasinEnMemoire())
    jouer(contrat, catalogue, journee, tmp_path, etat)
    avant = {nom: len(getattr(etat, _nom_d_attribut(nom))) for nom in COUCHES}

    rejeu = jouer(contrat, catalogue, journee, tmp_path, etat, decalage=timedelta(days=1))

    apres = {nom: len(getattr(etat, _nom_d_attribut(nom))) for nom in COUCHES}
    assert [Attendu.observe(r) for r in rejeu] == [livraison.attendu_au_rejeu for livraison in journee]
    assert apres == {**avant, "registre": 2 * avant["registre"]}


class _Espion(MagasinEnMemoire):
    """Un magasin en mémoire qui note dans quel ordre on lui écrit."""

    def __init__(self):
        super().__init__()
        self.ecritures = []

    def inserer_absentes(self, table, cles, enregistrements):
        self.ecritures.append((f"{table.schema}.{table.nom}", len(enregistrements)))
        super().inserer_absentes(table, cles, enregistrements)


def test_le_registre_est_ecrit_en_dernier_apres_les_couches_de_donnees(contrat, jouee):
    complete = next(r for r in jouee[1] if r.lignes_bronze and r.lignes_coupees and r.silver.lignes and r.silver.quarantaine)
    espion = _Espion()

    EtatMagasin(contrat, espion).appliquer(complete)

    ecrits = [nom for nom, nombre in espion.ecritures if nombre]
    assert ecrits == ["bronze.lignes", "bronze.lignes_coupees", "silver.lignes", "silver.quarantaine", "ops.registre"]
    assert [table for table, _ in espion.ecritures].count("ops.registre") == 1


def test_une_livraison_refusee_n_ecrit_que_son_registre(contrat, jouee):
    refusee = next(r for r in jouee[1] if r.ligne_registre.statut == "REFUSEE")
    espion = _Espion()

    EtatMagasin(contrat, espion).appliquer(refusee)

    assert [nom for nom, nombre in espion.ecritures if nombre] == ["ops.registre"]


def test_appliquer_deux_fois_le_meme_resultat_ne_duplique_rien(contrat, jouee):
    etat = EtatMagasin(contrat, MagasinEnMemoire())
    complete = next(r for r in jouee[1] if r.lignes_bronze and r.lignes_coupees and r.silver.lignes and r.silver.quarantaine)

    etat.appliquer(complete)
    une_fois = {nom: getattr(etat, _nom_d_attribut(nom)) for nom in COUCHES}
    etat.appliquer(complete)

    assert {nom: getattr(etat, _nom_d_attribut(nom)) for nom in COUCHES} == une_fois


class _Capricieux(MagasinEnMemoire):
    """Tombe en panne à la troisième écriture du registre, une seule fois : les données de la livraison sont déjà là, pas la décision."""

    def __init__(self):
        super().__init__()
        self.ecritures_du_registre = 0

    def inserer_absentes(self, table, cles, enregistrements):
        if table.nom == "registre" and enregistrements:
            self.ecritures_du_registre += 1
            if self.ecritures_du_registre == 3:
                raise RuntimeError("panne avant le registre")
        super().inserer_absentes(table, cles, enregistrements)


def test_une_panne_avant_le_registre_se_rattrape_en_reprenant_la_livraison_a_la_meme_heure(contrat, catalogue, journee, tmp_path):
    """Le registre est écrit en dernier : tant qu'il manque, la livraison n'est pas intégrée et on la reprend, sans rien dupliquer."""
    propre = EtatMagasin(contrat, MagasinEnMemoire())
    jouer(contrat, catalogue, journee, tmp_path / "propre", propre)
    flou = EtatMagasin(contrat, _Capricieux())

    with pytest.raises(RuntimeError, match="panne"):
        jouer(contrat, catalogue, journee, tmp_path / "flou", flou)
    deja = len(flou.registre)
    assert deja == 2, "la panne doit tomber à la troisième livraison, après deux décisions écrites"
    jouer(contrat, catalogue, journee[deja:], tmp_path / "flou", flou)        # on reprend là où le registre s'est arrêté

    for nom in COUCHES:
        couche = couches(contrat)[nom]
        assert _trier(getattr(flou, _nom_d_attribut(nom)), couche) == _trier(getattr(propre, _nom_d_attribut(nom)), couche), nom
    assert len(flou.registre) == len(propre.registre) == len(journee)


def test_l_etat_lit_le_magasin_a_chaque_acces_et_rend_des_listes_independantes(contrat, jouee):
    magasin = MagasinEnMemoire()
    etat = EtatMagasin(contrat, magasin)

    assert etat.registre == []
    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [couches(contrat)["registre"].vers_enregistrement(jouee[1][0].ligne_registre)])
    lue = etat.registre
    lue.clear()

    assert len(etat.registre) == 1 and len(magasin.lire(REGISTRE, ("heure_arrivee",))) == 1


def test_un_etat_neuf_n_a_ni_alertes_ni_lignes(contrat):
    etat = EtatMagasin(contrat, MagasinEnMemoire())

    assert [getattr(etat, _nom_d_attribut(nom)) for nom in COUCHES] == [[]] * len(COUCHES) and etat.alertes == []


# ==============================================================================
# 4. 🧾 LE MAGASIN SQL : des requêtes générées, les valeurs seulement en paramètre
# ==============================================================================
class _Enregistreur:
    """Un exécuteur qui note ce qu'on lui demande, au lieu de parler à Databricks."""

    def __init__(self, reponse=None):
        self.appels = []
        self.reponse = reponse or []

    def __call__(self, sql, parametres):
        self.appels.append((sql, dict(parametres)))
        return copy.deepcopy(self.reponse)


def _sql(executeur=None, **options):
    executeur = executeur or _Enregistreur()
    return executeur, MagasinSql(executeur, CATALOGUE_SQL, **options)


def test_une_insertion_est_un_merge_insert_seul_sur_les_cles_avec_egalite_tolerante_au_null():
    executeur, magasin = _sql()

    magasin.inserer_absentes(QUARANTAINE, CLES_QUARANTAINE, [_quarantaine("a.csv", HEURE, None, 7)])

    (sql, parametres), = executeur.appels
    assert sql.startswith(f"MERGE INTO `{CATALOGUE_SQL}`.`{SCHEMA_SILVER}`.`quarantaine`")
    for cle in CLES_QUARANTAINE:
        assert f"cible.`{cle}` <=> source.`{cle}`" in sql, cle
    assert sql.count(" AND ") == len(CLES_QUARANTAINE) - 1
    assert "WHEN NOT MATCHED THEN INSERT *" in sql
    assert "WHEN MATCHED" not in sql and "UPDATE" not in sql and "DELETE" not in sql
    assert ":lignes" in sql and set(parametres) == {"lignes"}


def test_le_schema_lu_dans_le_json_donne_chaque_colonne_avec_son_type():
    executeur, magasin = _sql()

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [_registre("a.csv", HEURE)])

    sql = executeur.appels[0][0]
    for colonne in REGISTRE.colonnes:
        assert f"`{colonne.nom}` {colonne.type_sql}" in sql, colonne.nom


def test_aucune_valeur_n_entre_dans_le_texte_de_la_requete_et_toutes_sont_dans_le_parametre():
    executeur, magasin = _sql()
    lignes = [{**_registre(valeur, HEURE), "rejeu_de": valeur, "codes_refus": [valeur]} for valeur in HOSTILES]

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, lignes)

    sql, parametres = executeur.appels[0]
    assert not any(valeur in sql for valeur in HOSTILES) and "DROP" not in sql
    relues = json.loads(parametres["lignes"])
    assert [ligne["livraison_id"] for ligne in relues] == HOSTILES
    assert [ligne["codes_refus"] for ligne in relues] == [[valeur] for valeur in HOSTILES]


def test_les_enregistrements_sont_ecrits_par_lots_dans_l_ordre():
    executeur, magasin = _sql(taille_lot=2)
    lignes = [_registre(f"{i}.csv", HEURE) for i in range(5)]

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, lignes)

    lots = [[ligne["livraison_id"] for ligne in json.loads(parametres["lignes"])] for _, parametres in executeur.appels]
    assert lots == [["0.csv", "1.csv"], ["2.csv", "3.csv"], ["4.csv"]]


def test_rien_a_ecrire_n_envoie_aucune_requete():
    executeur, magasin = _sql()

    magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [])

    assert executeur.appels == []


@pytest.mark.parametrize("taille", [0, -1])
def test_une_taille_de_lot_qui_n_est_pas_positive_est_refusee(taille):
    with pytest.raises(ValueError, match="lot"):
        MagasinSql(_Enregistreur(), CATALOGUE_SQL, taille_lot=taille)


def test_un_catalogue_inconnu_est_refuse_avant_toute_requete():
    executeur = _Enregistreur()

    with pytest.raises(ErreurEnvironnement):
        MagasinSql(executeur, "rd_manuf_inconnu")

    assert executeur.appels == []


@pytest.mark.parametrize(
    "ligne",
    [
        pytest.param({k: v for k, v in _registre("a.csv", HEURE).items() if k != "statut"}, id="colonne-manquante"),
        pytest.param({**_registre("a.csv", HEURE), "colonne_en_trop": 1}, id="colonne-en-trop"),
    ],
)
def test_un_enregistrement_qui_n_a_pas_les_colonnes_de_la_table_est_refuse(ligne):
    executeur, magasin = _sql()

    with pytest.raises(ValueError, match="colonne"):
        magasin.inserer_absentes(REGISTRE, CLES_REGISTRE, [ligne])

    assert executeur.appels == []


def test_une_cle_qui_n_est_pas_une_colonne_de_la_table_est_refusee():
    executeur, magasin = _sql()

    with pytest.raises(ValueError, match="clé"):
        magasin.inserer_absentes(REGISTRE, ("livraison_id", "colonne_inconnue"), [_registre("a.csv", HEURE)])

    assert executeur.appels == []


def test_un_identifiant_qui_n_est_pas_simple_est_refuse_meme_s_il_vient_du_code():
    executeur, magasin = _sql()
    piege = replace(REGISTRE, nom="registre`; DROP TABLE x; --")

    with pytest.raises(ValueError, match="identifiant"):
        magasin.inserer_absentes(piege, CLES_REGISTRE, [_registre("a.csv", HEURE)])
    with pytest.raises(ValueError, match="identifiant"):
        magasin.lire(piege, ("heure_arrivee",))

    assert executeur.appels == []


def test_une_colonne_dont_le_nom_n_est_pas_un_identifiant_simple_est_refusee():
    executeur, magasin = _sql()
    piege = replace(REGISTRE, colonnes=(ColonneTable("livraison_id", "STRING", True), ColonneTable("x`; DROP TABLE y; --", "STRING", True)))

    with pytest.raises(ValueError, match="identifiant"):
        magasin.inserer_absentes(piege, ("livraison_id",), [{"livraison_id": "a", "x`; DROP TABLE y; --": "b"}])

    assert executeur.appels == []


def test_une_cle_vide_est_refusee():
    executeur, magasin = _sql()

    with pytest.raises(ValueError, match="clé"):
        magasin.inserer_absentes(REGISTRE, (), [_registre("a.csv", HEURE)])

    assert executeur.appels == []


def test_la_lecture_est_un_select_trie_sans_parametre_et_rend_ce_que_l_executeur_rend():
    reponse = [_registre("a.csv", HEURE)]
    executeur, magasin = _sql(_Enregistreur(reponse))

    lus = magasin.lire(REGISTRE, ("heure_arrivee", "livraison_id"))

    assert executeur.appels == [(f"SELECT * FROM `{CATALOGUE_SQL}`.`{SCHEMA_OPS}`.`registre` ORDER BY `heure_arrivee`, `livraison_id`", {})]
    assert lus == reponse


def test_creer_les_tables_cree_les_schemas_puis_les_cinq_tables(contrat):
    executeur = _Enregistreur()

    creer_tables(contrat, CATALOGUE_SQL, executeur)

    requetes = [sql for sql, _ in executeur.appels]
    schemas = (SCHEMA_OPS, SCHEMA_BRONZE, SCHEMA_SILVER)
    assert requetes[:3] == [f"CREATE SCHEMA IF NOT EXISTS `{CATALOGUE_SQL}`.`{schema}`" for schema in schemas]
    assert requetes[3:] == [sql_creation(table, CATALOGUE_SQL) for table in toutes_les_tables(contrat)]
    assert all(parametres == {} for _, parametres in executeur.appels)


def test_creer_les_tables_dans_un_catalogue_inconnu_est_refuse_avant_toute_requete(contrat):
    executeur = _Enregistreur()

    with pytest.raises(ErreurEnvironnement):
        creer_tables(contrat, "rd_manuf_inconnu", executeur)

    assert executeur.appels == []


# ==============================================================================
# 5. 🔤 LE JSON : ce qui voyage dans le paramètre, avec ses types écrits en texte
# ==============================================================================
def test_le_json_ecrit_les_dates_et_les_instants_en_texte_iso():
    instant = datetime(2026, 11, 12, 9, 5, 7, 123456, tzinfo=UTC)

    relu = json.loads(en_json([{"jour": date(2026, 11, 12), "instant": instant, "absent": None, "liste": ["a"], "table": {"k": None}}]))

    assert relu == [{"jour": "2026-11-12", "instant": "2026-11-12T09:05:07.123456Z", "absent": None, "liste": ["a"], "table": {"k": None}}]


def test_le_json_garde_les_decimaux_exacts_et_les_entiers_entiers():
    relu = json.loads(en_json([{"d": 0.1 + 0.2, "n": 2**40, "z": 0.0}]))[0]

    assert relu["d"] == 0.1 + 0.2 and relu["n"] == 2**40 and isinstance(relu["n"], int) and relu["z"] == 0.0


def test_le_json_refuse_un_instant_sans_fuseau_et_un_decimal_infini_ou_indefini():
    with pytest.raises(ValueError, match="fuseau"):
        en_json([{"instant": datetime(2026, 11, 12, 9, 0)}])
    for valeur in (float("nan"), float("inf")):
        with pytest.raises(ValueError, match="décimal"):
            en_json([{"d": valeur}])


def test_le_json_convertit_aussi_ce_qui_est_dans_une_liste_ou_une_table():
    instant = datetime(2026, 11, 12, 9, 0, tzinfo=UTC)

    relu = json.loads(en_json([{"liste": [date(2026, 11, 12), instant], "table": {"k": [instant], "j": date(2026, 11, 13)}}]))[0]

    assert relu == {"liste": ["2026-11-12", "2026-11-12T09:00:00.000000Z"],
                    "table": {"k": ["2026-11-12T09:00:00.000000Z"], "j": "2026-11-13"}}


def test_le_json_refuse_un_type_qu_il_ne_connait_pas():
    with pytest.raises(TypeError, match="type"):
        en_json([{"x": {1, 2}}])


def test_le_json_garde_les_valeurs_hostiles_intactes():
    relu = json.loads(en_json([{"v": valeur} for valeur in HOSTILES]))

    assert [ligne["v"] for ligne in relu] == HOSTILES


def test_le_json_d_une_liste_vide_est_une_liste_vide():
    assert json.loads(en_json([])) == []


def test_le_parametre_d_une_journee_entiere_est_un_json_valide_pour_chaque_couche(contrat, jouee):
    for nom in COUCHES:
        couche = couches(contrat)[nom]
        enregistrements = [couche.vers_enregistrement(objet) for objet in _objets(nom, jouee[1])]

        relu = json.loads(en_json(enregistrements))

        assert len(relu) == len(enregistrements) > 0 and all(set(ligne) == {c.nom for c in couche.table.colonnes} for ligne in relu)
        assert not re.search(r"NaN|Infinity", en_json(enregistrements))
