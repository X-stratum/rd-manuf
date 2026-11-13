"""Les schémas des tables : ce que l'on écrira dans Databricks, décrit sans rien écrire.

Cinq tables, rangées par couche : `ops.registre`, `bronze.lignes`, `bronze.lignes_coupees`, `silver.lignes` et `silver.quarantaine`.
Ce module ne touche ni Spark ni Databricks : il décrit les colonnes et fabrique le texte SQL de création. L'écriture viendra ensuite.

Les colonnes de données (bronze, silver) se déduisent du CONTRAT, pas d'une liste écrite ici : le bronze les garde toutes en texte, le
silver les type. Les autres colonnes suivent les dataclasses des lignes ; un test garde chaque schéma d'accord avec la sienne, donc ajouter
un champ à une ligne sans mettre à jour le schéma fait échouer la CI.

Types : `chaine` en STRING, `entier` en BIGINT, `decimal` en DOUBLE (le code rend un `float` et le contrat ne dit ni précision ni
échelle : un DECIMAL(p, s) imposerait une précision que personne n'a décidée), `date` en DATE. Une colonne obligatoire du contrat est
NOT NULL en silver : une ligne valide ne peut pas être absente. Le bronze ne juge rien, toutes ses colonnes de données acceptent NULL.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from rd_manuf.config import environnement_du_catalogue
from rd_manuf.contrat import TYPE_CHAINE, TYPE_DATE, TYPE_DECIMAL, TYPE_ENTIER, Colonne, Contrat

SCHEMA_OPS = "ops"
SCHEMA_BRONZE = "bronze"
SCHEMA_SILVER = "silver"

TYPE_SQL_DU_CONTRAT = {TYPE_CHAINE: "STRING", TYPE_ENTIER: "BIGINT", TYPE_DECIMAL: "DOUBLE", TYPE_DATE: "DATE"}
TYPE_VIOLATIONS = "ARRAY<STRUCT<code: STRING, colonnes: ARRAY<STRING>, message: STRING>>"

# Noms réservés à la traçabilité, dans TOUTES les tables de données : une colonne du contrat qui les porterait créerait deux colonnes du
# même nom, ou écraserait la traçabilité. Le refus est le même en bronze et en silver, pour qu'une colonne ait le même nom partout.
COLONNES_RESERVEES = ("livraison_id", "heure_arrivee", "numero_ligne", "version_contrat")

# Un nom de colonne devient un identifiant SQL entre backticks : lettres (accents permis), chiffres et `_`, sans commencer par un chiffre.
# Un backtick sortirait des guillemets ; un espace, un point, un tiret ou un point-virgule sont refusés par Delta à la création.
IDENTIFIANT_SIMPLE = re.compile(r"[^\W\d]\w*")


@dataclass(frozen=True)
class ColonneTable:
    nom: str
    type_sql: str
    obligatoire: bool                          # False : la colonne accepte NULL


@dataclass(frozen=True)
class Table:
    schema: str
    nom: str
    colonnes: tuple[ColonneTable, ...]


def _colonne(nom: str, type_sql: str, obligatoire: bool = True) -> ColonneTable:
    return ColonneTable(nom=nom, type_sql=type_sql, obligatoire=obligatoire)


REGISTRE = Table(SCHEMA_OPS, "registre", (
    _colonne("livraison_id", "STRING"),
    _colonne("statut", "STRING"),
    _colonne("version_contrat", "STRING"),
    _colonne("nb_recues", "BIGINT"),
    _colonne("nb_valides", "BIGINT"),
    _colonne("nb_rejetees", "BIGINT"),
    _colonne("codes_refus", "ARRAY<STRING>"),
    _colonne("pays", "STRING", obligatoire=False),
    _colonne("usine", "STRING", obligatoire=False),
    _colonne("horodatage_fichier", "TIMESTAMP", obligatoire=False),
    _colonne("heure_arrivee", "TIMESTAMP"),
    _colonne("empreinte", "STRING", obligatoire=False),
    _colonne("rejeu_de", "STRING", obligatoire=False),
))

BRONZE_COUPEES = Table(SCHEMA_BRONZE, "lignes_coupees", (
    _colonne("livraison_id", "STRING"),
    _colonne("heure_arrivee", "TIMESTAMP"),
    _colonne("numero_fichier", "BIGINT"),
    _colonne("champs", "ARRAY<STRING>"),
))

# Le contenu d'une ligne rejetée a deux formes : les valeurs en texte (ligne bien formée) ou les champs bruts (ligne coupée). Chacune a
# sa colonne typée, l'autre est NULL : aucun JSON à relire, et le schéma dit ce que la ligne contient.
QUARANTAINE = Table(SCHEMA_SILVER, "quarantaine", (
    _colonne("livraison_id", "STRING"),
    _colonne("heure_arrivee", "TIMESTAMP"),
    _colonne("version_contrat", "STRING"),
    _colonne("numero_ligne", "BIGINT", obligatoire=False),
    _colonne("numero_fichier", "BIGINT", obligatoire=False),
    _colonne("contenu_texte", "MAP<STRING, STRING>", obligatoire=False),
    _colonne("champs_bruts", "ARRAY<STRING>", obligatoire=False),
    _colonne("violations", TYPE_VIOLATIONS),
))


def _type_sql(colonne: Colonne) -> str:
    if colonne.type not in TYPE_SQL_DU_CONTRAT:
        raise ValueError(f"Type de colonne inconnu du contrat : « {colonne.type} » (colonne {colonne.nom}) : aucun type SQL ne correspond.")
    return TYPE_SQL_DU_CONTRAT[colonne.type]


def _verifier_les_colonnes_du_contrat(contrat: Contrat) -> None:
    """Un contrat que les tables ne savent pas ranger est refusé, quelle que soit la table demandée (bronze et silver vont ensemble).

    Les noms se comparent sans tenir compte de la casse, comme le fait Delta : `Livraison_ID` serait le même nom que `livraison_id`.
    """
    vus: set[str] = set()
    for colonne in contrat.colonnes:
        nom = colonne.nom
        if not IDENTIFIANT_SIMPLE.fullmatch(nom):
            raise ValueError(
                f"La colonne du contrat « {nom} » n'est pas un identifiant simple (lettres, chiffres et _, sans commencer par un "
                "chiffre) : elle ne peut pas devenir une colonne de table."
            )
        if nom.lower() in COLONNES_RESERVEES:
            raise ValueError(
                f"La colonne du contrat « {nom} » porte le nom d'une colonne de traçabilité : {list(COLONNES_RESERVEES)}."
            )
        if nom.lower() in vus:
            raise ValueError(f"La colonne du contrat « {nom} » ne diffère d'une autre que par la casse : Delta ne les distingue pas.")
        vus.add(nom.lower())
        _type_sql(colonne)


def table_bronze(contrat: Contrat) -> Table:
    """La traçabilité, puis TOUTES les colonnes du contrat en texte, sans NOT NULL : le bronze ne juge ni ne convertit rien."""
    _verifier_les_colonnes_du_contrat(contrat)
    return Table(SCHEMA_BRONZE, "lignes", (
        _colonne("livraison_id", "STRING"),
        _colonne("heure_arrivee", "TIMESTAMP"),
        _colonne("numero_ligne", "BIGINT"),
        *(_colonne(colonne.nom, "STRING", obligatoire=False) for colonne in contrat.colonnes),
    ))


def table_silver(contrat: Contrat) -> Table:
    """La traçabilité, puis les colonnes du contrat TYPÉES ; une colonne obligatoire du contrat est NOT NULL."""
    _verifier_les_colonnes_du_contrat(contrat)
    return Table(SCHEMA_SILVER, "lignes", (
        _colonne("livraison_id", "STRING"),
        _colonne("heure_arrivee", "TIMESTAMP"),
        _colonne("numero_ligne", "BIGINT"),
        _colonne("version_contrat", "STRING"),
        *(_colonne(colonne.nom, _type_sql(colonne), obligatoire=colonne.obligatoire) for colonne in contrat.colonnes),
    ))


def toutes_les_tables(contrat: Contrat) -> tuple[Table, ...]:
    return (REGISTRE, table_bronze(contrat), BRONZE_COUPEES, table_silver(contrat), QUARANTAINE)


def sql_creation(table: Table, catalogue: str) -> str:
    """Le texte SQL qui crée la table si elle n'existe pas, dans le catalogue demandé.

    Le catalogue passe par le garde-fou d'environnement : un nom inconnu est refusé, jamais traité comme un environnement sûr.
    """
    environnement_du_catalogue(catalogue)
    colonnes = ",\n".join(f"  `{colonne.nom}` {colonne.type_sql}{' NOT NULL' if colonne.obligatoire else ''}" for colonne in table.colonnes)
    return f"CREATE TABLE IF NOT EXISTS `{catalogue}`.`{table.schema}`.`{table.nom}` (\n{colonnes}\n) USING DELTA"
