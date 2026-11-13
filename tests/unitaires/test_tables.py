"""Les SCHÉMAS des tables (registre, bronze, lignes coupées, silver, quarantaine) : ce que l'on écrira, avant de l'écrire.

Le module `tables` ne touche ni Spark ni Databricks : il décrit les colonnes et fabrique le texte SQL de création. Ces tests gardent les
schémas d'accord avec les dataclasses (qui disent ce que le code produit) ET avec le contrat (qui dit les colonnes de données) : ajouter un
champ à une ligne ou une colonne au contrat sans mettre à jour les schémas fait échouer la CI : l'invariant du projet, appliqué aux tables.
Les types SQL, eux, sont écrits ici en clair : c'est la référence indépendante du module.
"""
import re
import types
from dataclasses import dataclass, fields, replace
from datetime import UTC, date, datetime
from typing import Optional, Union, get_args, get_origin, get_type_hints

import pytest

from rd_manuf.bronze import LigneBronze, LigneBronzeCoupee, construire_lignes_bronze, construire_lignes_coupees
from rd_manuf.config import CATALOGUE_PAR_DEFAUT, ErreurEnvironnement
from rd_manuf.contrat import Colonne, Violation
from rd_manuf.lecture import lire_selon_format
from rd_manuf.nom_fichier import lire_nom_fichier
from rd_manuf.registre import LigneRegistre, traiter_fichier
from rd_manuf.silver import LigneQuarantaine, LigneSilver, construire_silver
from rd_manuf.simulateur import ecrire_livraison_csv, generer_livraison
from rd_manuf.tables import (
    BRONZE_COUPEES,
    QUARANTAINE,
    REGISTRE,
    sql_creation,
    table_bronze,
    table_silver,
    toutes_les_tables,
)

TRACABILITE_BRONZE = ["livraison_id", "heure_arrivee", "numero_ligne"]
TRACABILITE_SILVER = ["livraison_id", "heure_arrivee", "pays", "usine", "numero_ligne", "version_contrat"]
ARRIVEE = datetime(2026, 11, 11, 14, 35, 0, tzinfo=UTC)
NOM = "livraison_production_FRA_AA_001_2026-11-11-14-30-05Z.csv"


def _colonnes(table):
    return [(colonne.nom, colonne.type_sql, colonne.obligatoire) for colonne in table.colonnes]


def _noms(table):
    return [colonne.nom for colonne in table.colonnes]


def _champs(classe, sauf=()):
    return [champ.name for champ in fields(classe) if champ.name not in sauf]


def _est_facultatif(classe, nom_du_champ):
    """Le champ est annoté `X | None` : la colonne doit accepter NULL, et inversement."""
    annotation = get_type_hints(classe)[nom_du_champ]
    return get_origin(annotation) in (Union, types.UnionType) and type(None) in get_args(annotation)


@dataclass
class _ChampsDeTest:
    ecriture_barre: str | None
    ecriture_optional: Optional[str]
    ecriture_union: Union[str, None]
    obligatoire: str


def test_est_facultatif_reconnait_les_trois_ecritures_d_une_valeur_facultative():
    assert [_est_facultatif(_ChampsDeTest, champ.name) for champ in fields(_ChampsDeTest)] == [True, True, True, False]


# ==============================================================================
# 1. 🗂️ LE REGISTRE : une colonne par champ de `LigneRegistre`
# ==============================================================================
def test_les_colonnes_du_registre_sont_les_champs_de_la_ligne_de_registre():
    assert _noms(REGISTRE) == _champs(LigneRegistre)


def test_les_types_du_registre():
    assert _colonnes(REGISTRE) == [
        ("livraison_id", "STRING", True),
        ("statut", "STRING", True),
        ("version_contrat", "STRING", True),
        ("nb_recues", "BIGINT", True),
        ("nb_valides", "BIGINT", True),
        ("nb_rejetees", "BIGINT", True),
        ("codes_refus", "ARRAY<STRING>", True),
        ("pays", "STRING", False),
        ("usine", "STRING", False),
        ("ligne", "STRING", False),
        ("horodatage_fichier", "TIMESTAMP", False),
        ("heure_arrivee", "TIMESTAMP", True),
        ("empreinte", "STRING", False),
        ("rejeu_de", "STRING", False),
    ]


@pytest.mark.parametrize("table, classe", [(REGISTRE, LigneRegistre), (BRONZE_COUPEES, LigneBronzeCoupee)])
def test_une_colonne_accepte_null_exactement_quand_le_champ_est_facultatif(table, classe):
    attendu = {nom: not _est_facultatif(classe, nom) for nom in _champs(classe)}

    assert {colonne.nom: colonne.obligatoire for colonne in table.colonnes} == attendu


# ==============================================================================
# 2. ✂️ LES LIGNES COUPÉES : une colonne par champ de `LigneBronzeCoupee`
# ==============================================================================
def test_les_colonnes_des_lignes_coupees_sont_les_champs_de_la_ligne_coupee():
    assert _noms(BRONZE_COUPEES) == _champs(LigneBronzeCoupee)
    assert _colonnes(BRONZE_COUPEES) == [
        ("livraison_id", "STRING", True),
        ("heure_arrivee", "TIMESTAMP", True),
        ("numero_fichier", "BIGINT", True),
        ("champs", "ARRAY<STRING>", True),
    ]


# ==============================================================================
# 3. 🥉 LE BRONZE : la traçabilité, puis TOUTES les colonnes du contrat en texte
# ==============================================================================
def test_le_bronze_a_la_tracabilite_puis_les_colonnes_du_contrat_toutes_en_texte(contrat):
    table = table_bronze(contrat)

    assert _noms(table) == _champs(LigneBronze, sauf=("valeurs",)) + [colonne.nom for colonne in contrat.colonnes]
    assert _colonnes(table)[:3] == [
        ("livraison_id", "STRING", True),
        ("heure_arrivee", "TIMESTAMP", True),
        ("numero_ligne", "BIGINT", True),
    ]
    assert {(colonne.type_sql, colonne.obligatoire) for colonne in table.colonnes[3:]} == {("STRING", False)}    # ni jugé, ni converti


# ==============================================================================
# 4. 🥈 LE SILVER : la traçabilité, puis les colonnes du contrat TYPÉES
# ==============================================================================
def test_le_silver_a_la_tracabilite_puis_les_colonnes_du_contrat(contrat):
    table = table_silver(contrat)

    assert _noms(table) == _champs(LigneSilver, sauf=("valeurs",)) + [colonne.nom for colonne in contrat.colonnes]
    assert _colonnes(table)[:6] == [
        ("livraison_id", "STRING", True),
        ("heure_arrivee", "TIMESTAMP", True),
        ("pays", "STRING", True),
        ("usine", "STRING", True),
        ("numero_ligne", "BIGINT", True),
        ("version_contrat", "STRING", True),
    ]


@pytest.mark.parametrize("type_du_contrat, type_sql", [("chaine", "STRING"), ("entier", "BIGINT"), ("decimal", "DOUBLE"), ("date", "DATE")])
def test_chaque_type_du_contrat_a_son_type_sql(contrat, type_du_contrat, type_sql):
    """Un `decimal` du contrat est un `float` en Python et le contrat ne dit ni précision ni échelle : DOUBLE, pas DECIMAL(p, s)."""
    colonnes = tuple(replace(colonne, type=type_du_contrat) for colonne in contrat.colonnes)

    table = table_silver(replace(contrat, colonnes=colonnes))

    assert {colonne.type_sql for colonne in table.colonnes[6:]} == {type_sql}


def test_une_colonne_obligatoire_du_contrat_est_not_null_en_silver_et_seulement_la(contrat):
    table = table_silver(contrat)
    obligatoires = {colonne.nom: colonne.obligatoire for colonne in contrat.colonnes}

    assert {colonne.nom: colonne.obligatoire for colonne in table.colonnes[6:]} == obligatoires
    assert not all(obligatoires.values()), "le contrat réel doit avoir une colonne facultative, sinon ce test ne prouve rien"


def test_les_colonnes_du_contrat_reel_en_bronze_et_en_silver(contrat):
    """L'ALARME du contrat réel : valeurs en clair, elles échouent quand le contrat change et se mettent à jour à la main."""
    assert [(c.nom, c.type_sql, c.obligatoire) for c in table_silver(contrat).colonnes[6:]] == [
        ("id_lot", "STRING", True),
        ("id_ligne", "STRING", True),
        ("date_production", "DATE", True),
        ("quantite_produite", "BIGINT", True),
        ("quantite_rebut", "BIGINT", True),
        ("temperature_c", "DOUBLE", False),
    ]
    assert [c.nom for c in table_bronze(contrat).colonnes[3:]] == [
        "id_lot", "id_ligne", "date_production", "quantite_produite", "quantite_rebut", "temperature_c",
    ]


@pytest.mark.parametrize("construire", [table_bronze, table_silver])
@pytest.mark.parametrize("nom_pris", sorted(set(TRACABILITE_BRONZE + TRACABILITE_SILVER)))
def test_une_colonne_du_contrat_qui_porte_le_nom_d_une_colonne_de_tracabilite_est_refusee(contrat, construire, nom_pris):
    """Sans cette garde, la table aurait deux colonnes du même nom, ou la donnée écraserait la traçabilité."""
    contrat_piege = replace(contrat, colonnes=contrat.colonnes + (Colonne(nom=nom_pris, type="chaine"),))

    with pytest.raises(ValueError, match=nom_pris):
        construire(contrat_piege)


@pytest.mark.parametrize("construire", [table_bronze, table_silver])
@pytest.mark.parametrize("nom_pris", ["Livraison_ID", "HEURE_ARRIVEE", "Numero_Ligne", "VERSION_CONTRAT", "PAYS", "Usine"])
def test_la_casse_ne_contourne_pas_un_nom_de_tracabilite(contrat, construire, nom_pris):
    """Delta ne distingue pas les majuscules des minuscules : `Livraison_ID` ferait deux colonnes `livraison_id`."""
    contrat_piege = replace(contrat, colonnes=contrat.colonnes + (Colonne(nom=nom_pris, type="chaine"),))

    with pytest.raises(ValueError, match=nom_pris):
        construire(contrat_piege)


@pytest.mark.parametrize("construire", [table_bronze, table_silver])
def test_deux_colonnes_du_contrat_qui_ne_different_que_par_la_casse_sont_refusees(contrat, construire):
    premiere = contrat.colonnes[0]
    doublon = replace(premiere, nom=premiere.nom.upper())

    with pytest.raises(ValueError, match=doublon.nom):
        construire(replace(contrat, colonnes=contrat.colonnes + (doublon,)))


@pytest.mark.parametrize("construire", [table_bronze, table_silver])
def test_le_doublon_de_casse_est_vu_quel_que_soit_l_ordre_des_colonnes(contrat, construire):
    """La majuscule est cette fois sur la PREMIÈRE colonne : la garde doit comparer les deux noms en minuscules, pas seulement le second."""
    colonnes = (Colonne(nom="Mesure_X", type="entier", obligatoire=False), Colonne(nom="mesure_x", type="entier", obligatoire=False))

    with pytest.raises(ValueError, match="mesure_x"):
        construire(replace(contrat, colonnes=contrat.colonnes + colonnes))


@pytest.mark.parametrize("construire", [table_bronze, table_silver])
@pytest.mark.parametrize("nom_invalide", ["a`b", "id lot", "1abc", "x;DROP TABLE y", "a-b", "a.b"])
def test_un_nom_de_colonne_qui_n_est_pas_un_identifiant_simple_est_refuse(contrat, construire, nom_invalide):
    """Un backtick sortirait du nom entre guillemets du SQL ; un espace, un point ou un tiret sont refusés par Delta."""
    contrat_piege = replace(contrat, colonnes=contrat.colonnes + (Colonne(nom=nom_invalide, type="chaine"),))

    with pytest.raises(ValueError, match=re.escape(nom_invalide)):
        construire(contrat_piege)


def test_un_nom_de_colonne_avec_des_accents_est_accepte(contrat):
    """La garde ne doit pas être plus stricte que nécessaire : un identifiant en français reste un identifiant."""
    colonne = Colonne(nom="température_moyenne", type="decimal", obligatoire=False)

    assert "température_moyenne" in _noms(table_silver(replace(contrat, colonnes=contrat.colonnes + (colonne,))))


@pytest.mark.parametrize("construire", [table_bronze, table_silver])
def test_un_type_inconnu_du_contrat_est_refuse_avec_son_nom(contrat, construire):
    contrat_piege = replace(contrat, colonnes=(Colonne(nom="est_ok", type="booleen"),))

    with pytest.raises(ValueError, match="booleen"):
        construire(contrat_piege)


# ==============================================================================
# 5. 🚧 LA QUARANTAINE : deux colonnes typées pour le contenu, l'autre est NULL
# ==============================================================================
def test_les_colonnes_de_la_quarantaine_sont_les_champs_de_la_ligne_avec_le_contenu_en_deux_colonnes():
    attendu = []
    for nom in _champs(LigneQuarantaine):
        attendu += ["contenu_texte", "champs_bruts"] if nom == "contenu" else [nom]

    assert _noms(QUARANTAINE) == attendu


def test_les_types_de_la_quarantaine():
    assert _colonnes(QUARANTAINE) == [
        ("livraison_id", "STRING", True),
        ("heure_arrivee", "TIMESTAMP", True),
        ("version_contrat", "STRING", True),
        ("numero_ligne", "BIGINT", False),
        ("numero_fichier", "BIGINT", False),
        ("contenu_texte", "MAP<STRING, STRING>", False),
        ("champs_bruts", "ARRAY<STRING>", False),
        ("violations", "ARRAY<STRUCT<code: STRING, colonnes: ARRAY<STRING>, message: STRING>>", True),
    ]


def test_la_structure_d_une_violation_a_les_champs_de_la_dataclass_violation():
    violations = next(colonne for colonne in QUARANTAINE.colonnes if colonne.nom == "violations")

    assert re.findall(r"(\w+): ", violations.type_sql) == _champs(Violation)


def test_les_colonnes_facultatives_de_la_quarantaine_suivent_la_dataclass():
    """`numero_ligne` et `numero_fichier` sont `int | None`. `contenu` n'est jamais None mais a DEUX formes : chacune a sa colonne, et
    l'autre est NULL, donc les deux colonnes de contenu acceptent NULL."""
    facultatifs = {nom for nom in _champs(LigneQuarantaine) if _est_facultatif(LigneQuarantaine, nom)}
    formes_du_contenu = [forme for forme in get_args(get_type_hints(LigneQuarantaine)["contenu"]) if forme is not type(None)]

    assert facultatifs == {"numero_ligne", "numero_fichier"}
    assert len(formes_du_contenu) == 2
    assert {colonne.nom for colonne in QUARANTAINE.colonnes if not colonne.obligatoire} == facultatifs | {"contenu_texte", "champs_bruts"}


# ==============================================================================
# 6. 📍 LES SCHÉMAS ET LES NOMS : ops, bronze, silver
# ==============================================================================
def test_chaque_table_est_rangee_dans_son_schema(contrat):
    assert [(table.schema, table.nom) for table in toutes_les_tables(contrat)] == [
        ("ops", "registre"),
        ("bronze", "lignes"),
        ("bronze", "lignes_coupees"),
        ("silver", "lignes"),
        ("silver", "quarantaine"),
    ]


def test_toutes_les_tables_sont_celles_de_ce_module(contrat):
    assert toutes_les_tables(contrat) == (REGISTRE, table_bronze(contrat), BRONZE_COUPEES, table_silver(contrat), QUARANTAINE)


# ==============================================================================
# 7. 🧾 LE TEXTE SQL DE CRÉATION
# ==============================================================================
def test_le_sql_de_creation_d_une_table():
    assert sql_creation(BRONZE_COUPEES, CATALOGUE_PAR_DEFAUT) == (
        f"CREATE TABLE IF NOT EXISTS `{CATALOGUE_PAR_DEFAUT}`.`bronze`.`lignes_coupees` (\n"
        "  `livraison_id` STRING NOT NULL,\n"
        "  `heure_arrivee` TIMESTAMP NOT NULL,\n"
        "  `numero_fichier` BIGINT NOT NULL,\n"
        "  `champs` ARRAY<STRING> NOT NULL\n"
        ") USING DELTA"
    )


def test_le_sql_omet_not_null_pour_une_colonne_facultative():
    ligne = next(ligne for ligne in sql_creation(REGISTRE, CATALOGUE_PAR_DEFAUT).splitlines() if "`pays`" in ligne)

    assert ligne == "  `pays` STRING,"


def test_le_sql_nomme_toutes_les_colonnes_de_toutes_les_tables(contrat):
    tables = toutes_les_tables(contrat)
    assert len(tables) == 5, "une boucle sur zéro table réussirait sans rien vérifier"
    for table in tables:
        sql = sql_creation(table, CATALOGUE_PAR_DEFAUT)

        assert all(f"`{colonne.nom}` {colonne.type_sql}" in sql for colonne in table.colonnes), table.nom
        assert sql.startswith(f"CREATE TABLE IF NOT EXISTS `{CATALOGUE_PAR_DEFAUT}`.`{table.schema}`.`{table.nom}` (")
        assert sql.endswith(") USING DELTA")


def test_le_sql_refuse_un_catalogue_inconnu():
    """Le même garde-fou que le reste du projet : un nom mal orthographié n'est jamais traité comme un environnement sûr."""
    with pytest.raises(ErreurEnvironnement, match="rd_manuf_oups"):
        sql_creation(REGISTRE, "rd_manuf_oups")


# ==============================================================================
# 8. 🔗 LE PARCOURS RÉEL : ce que le registre, le bronze et le silver produisent entre dans ces tables
# ==============================================================================
PYTHON_PAR_TYPE_SQL = {"STRING": str, "BIGINT": int, "DOUBLE": float, "DATE": date, "TIMESTAMP": datetime}


def _parcours(tmp_path, contrat):
    chemin = tmp_path / NOM
    ecrire_livraison_csv(contrat, generer_livraison(contrat, 20, 42), chemin)
    _, registre = traiter_fichier(contrat, chemin, (), ARRIVEE)
    fichier = lire_selon_format(chemin, lire_nom_fichier(chemin.name).format)
    bronze = construire_lignes_bronze(contrat, registre, fichier)
    silver = construire_silver(contrat, registre, bronze, construire_lignes_coupees(registre, fichier))
    return registre, bronze, silver.lignes


def _verifier_enregistrement(table, enregistrement):
    assert list(enregistrement) == _noms(table)
    for colonne in table.colonnes:
        valeur = enregistrement[colonne.nom]
        if valeur is None:
            assert not colonne.obligatoire, f"{table.nom}.{colonne.nom} est NOT NULL mais reçoit NULL"
        elif colonne.type_sql == "ARRAY<STRING>":
            assert isinstance(valeur, tuple | list) and all(type(element) is str for element in valeur), f"{table.nom}.{colonne.nom}"
        elif colonne.type_sql == "MAP<STRING, STRING>":
            assert isinstance(valeur, dict) and all(type(cle) is str and type(v) in (str, type(None)) for cle, v in valeur.items())
        elif colonne.type_sql.startswith("ARRAY<STRUCT"):
            assert isinstance(valeur, tuple | list), f"{table.nom}.{colonne.nom}"
            assert all(isinstance(element, Violation) for element in valeur), f"{table.nom}.{colonne.nom}"
        else:
            attendu = PYTHON_PAR_TYPE_SQL[colonne.type_sql]
            assert type(valeur) is attendu, f"{table.nom}.{colonne.nom} : {type(valeur).__name__} au lieu de {attendu.__name__}"


def test_les_lignes_reelles_du_registre_du_bronze_et_du_silver_entrent_dans_leurs_tables(tmp_path, contrat):
    registre, bronze, silver = _parcours(tmp_path, contrat)

    _verifier_enregistrement(REGISTRE, {nom: getattr(registre, nom) for nom in _champs(LigneRegistre)})
    for ligne in bronze:
        _verifier_enregistrement(table_bronze(contrat), {nom: getattr(ligne, nom) for nom in TRACABILITE_BRONZE} | ligne.valeurs)
    for ligne in silver:
        _verifier_enregistrement(table_silver(contrat), {nom: getattr(ligne, nom) for nom in TRACABILITE_SILVER} | ligne.valeurs)
    assert len(bronze) == len(silver) == 20


LIVRAISON_AVEC_REJETS = (
    "LOT-000001,L1,2026-11-01,1000,10,200.0\n"
    "LOT-000002,L1,2026-11-01\n"                           # ligne coupée
    "LOT-000003,L2,2026-11-02,500,5,999.0\n"               # température hors plage : rejetée
    "LOT-000004,L3,2026-11-03,800,8,150.5\n"
)


def test_les_lignes_reelles_de_la_quarantaine_et_les_lignes_coupees_entrent_dans_leurs_tables(tmp_path, contrat_sans_seuil):
    contrat = contrat_sans_seuil
    chemin = tmp_path / NOM
    chemin.write_text(",".join(colonne.nom for colonne in contrat.colonnes) + "\n" + LIVRAISON_AVEC_REJETS, encoding="utf-8")
    _, registre = traiter_fichier(contrat, chemin, (), ARRIVEE)
    fichier = lire_selon_format(chemin, lire_nom_fichier(chemin.name).format)
    coupees = construire_lignes_coupees(registre, fichier)
    quarantaine = construire_silver(contrat, registre, construire_lignes_bronze(contrat, registre, fichier), coupees).quarantaine

    for ligne in coupees:
        _verifier_enregistrement(BRONZE_COUPEES, {nom: getattr(ligne, nom) for nom in _champs(LigneBronzeCoupee)})
    for ligne in quarantaine:
        texte, brut = (ligne.contenu, None) if isinstance(ligne.contenu, dict) else (None, ligne.contenu)
        _verifier_enregistrement(QUARANTAINE, {
            "livraison_id": ligne.livraison_id,
            "heure_arrivee": ligne.heure_arrivee,
            "version_contrat": ligne.version_contrat,
            "numero_ligne": ligne.numero_ligne,
            "numero_fichier": ligne.numero_fichier,
            "contenu_texte": texte,
            "champs_bruts": brut,
            "violations": ligne.violations,
        })
    assert (len(coupees), len(quarantaine)) == (1, 2)
    assert sorted(ligne.numero_fichier is None for ligne in quarantaine) == [False, True]    # les DEUX formes du contenu sont passées
