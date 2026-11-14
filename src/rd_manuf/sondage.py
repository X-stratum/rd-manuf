"""LE SONDAGE : la première exécution sur un workspace, strictement en LECTURE SEULE.

Programme que la personne lance elle-même, depuis la racine du dépôt :

    python -m rd_manuf.sondage --profil <son-profil> --entrepot <identifiant-de-l-entrepôt>

Il ne crée ni n'écrit rien : il n'envoie que des `SELECT`. Il fait deux choses.

1. Des ESSAIS de conversion : des requêtes sur des valeurs écrites dans le texte même (un entier, un décimal, une date, des instants, une
   liste, une structure, un paramètre nommé, `from_json` en mode d'échec immédiat), dont le résultat est comparé, type compris, à la valeur
   Python attendue. Ils vérifient ce que `executeur_databricks` suppose sans l'avoir vu : la forme des instants et des listes rendus par le
   service, et le mode d'échec immédiat. Un écart est rapporté, il n'arrête pas les autres essais.
2. La PRÉSENCE de nos trois schémas et de nos cinq tables dans le catalogue playground, pour voir s'il existe déjà des tables de mêmes
   noms AVANT d'en créer. Seuls ces noms sont nommés : rien d'autre du catalogue n'est listé. LIMITE : `information_schema` ne montre
   que ce sur quoi le profil a des droits, et compte les vues avec les tables : « absent » veut dire absent POUR CE PROFIL, et un nom
   pris par une vue est « présent ».

Le profil et l'identifiant de l'entrepôt viennent de la ligne de commande : ils ne sont jamais écrits dans un fichier du dépôt (public) ni
lus ailleurs que par le SDK. Le lanceur refuse tout catalogue autre que le playground, avant toute connexion. Ce qu'il affiche va
au terminal de la personne seulement ; il n'écrit aucun fichier.

Codes de sortie : 0 tout est conforme ; 1 un essai ou la vérification de présence a échoué ;
2 le lancement est refusé (catalogue, contrat, ou connexion : la toute première requête échoue).
"""
from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from rd_manuf.config import CATALOGUE_PAR_DEFAUT, environnement_du_catalogue
from rd_manuf.contrat import Contrat, ContratInvalide, charger_contrat
from rd_manuf.ecriture import Executeur
from rd_manuf.executeur_databricks import ErreurService, ExecuteurDatabricks
from rd_manuf.tables import SCHEMA_BRONZE, SCHEMA_OPS, SCHEMA_SILVER, toutes_les_tables

CONTRAT_PAR_DEFAUT = "contracts/data/livraison_production.yaml"      # relatif à la racine du dépôt, d'où le programme se lance
LONGUEUR_MAX_DU_DETAIL = 200
SQL_CONNEXION = "SELECT 1 AS v"                       # la première requête : si elle échoue, rien d'autre ne marchera
SCHEMAS = (SCHEMA_OPS, SCHEMA_BRONZE, SCHEMA_SILVER)
# La lecture JSON demandée dans le mode et la forme de `MagasinSql`.
SQL_LECTURE_JSON = "SELECT from_json(:lignes, 'ARRAY<STRUCT<a: BIGINT>>', map('mode', 'FAILFAST')) AS v"


# ==============================================================================
# 🧪 LES ESSAIS
# ==============================================================================
@dataclass(frozen=True)
class Essai:
    """Une requête de lecture seule, et ce qu'elle doit rendre : une liste de lignes, ou `ErreurService` si le service doit la refuser.

    Un refus ne vaut que s'il est du BON refus : `motif_du_refus` est le mot que le message doit contenir (casse ignorée). Sans cela, une
    requête mal écrite, annulée ou envoyée à un entrepôt introuvable passerait pour le refus attendu, et l'essai ne prouverait rien.
    """
    nom: str
    sql: str
    parametres: dict[str, str]
    attendu: Any
    motif_du_refus: str = ""


@dataclass(frozen=True)
class ResultatEssai:
    nom: str
    reussi: bool
    detail: str


MIDI_UTC = datetime(2026, 11, 14, 12, 0, tzinfo=UTC)
STRUCTURE = {"code": "E1", "colonnes": ["x"]}


def _une_valeur(valeur: Any) -> list[dict[str, Any]]:
    return [{"v": valeur}]


ESSAIS: tuple[Essai, ...] = (
    Essai("entier", "SELECT CAST(42 AS BIGINT) AS v", {}, _une_valeur(42)),
    Essai("decimal", "SELECT CAST(1.5 AS DOUBLE) AS v", {}, _une_valeur(1.5)),
    Essai("date", "SELECT DATE'2026-11-14' AS v", {}, _une_valeur(date(2026, 11, 14))),
    Essai("instant", "SELECT TIMESTAMP'2026-11-14 12:00:00+00:00' AS v", {}, _une_valeur(MIDI_UTC)),
    Essai("instant_avec_decalage", "SELECT TIMESTAMP'2026-11-14 14:00:00+02:00' AS v", {}, _une_valeur(MIDI_UTC)),
    Essai("vide", "SELECT CAST(NULL AS STRING) AS v", {}, _une_valeur(None)),
    Essai("liste", "SELECT array('a', 'b') AS v", {}, _une_valeur(["a", "b"])),
    Essai("structure", "SELECT named_struct('code', 'E1', 'colonnes', array('x')) AS v", {}, _une_valeur(STRUCTURE)),
    Essai("parametre", "SELECT :texte AS v", {"texte": "é'\"\\\n"}, _une_valeur("é'\"\\\n")),
    Essai("from_json_valide", SQL_LECTURE_JSON, {"lignes": '[{"a": 1}]'}, _une_valeur([{"a": 1}])),
    Essai("from_json_illisible", SQL_LECTURE_JSON, {"lignes": '[{"a": "x"}]'}, ErreurService, "malformed"),
)


def _borne(texte: str) -> str:
    texte = " ".join(texte.split())
    return texte if len(texte) <= LONGUEUR_MAX_DU_DETAIL else texte[:LONGUEUR_MAX_DU_DETAIL] + "…"


def _identique(obtenu: Any, attendu: Any) -> bool:
    """Égalité STRICTE : même type à chaque niveau (`True` n'est pas `1`, `1` n'est pas `1.0`), mêmes clés, même longueur."""
    if type(obtenu) is not type(attendu):
        return False
    if isinstance(attendu, list):
        return len(obtenu) == len(attendu) and all(_identique(o, a) for o, a in zip(obtenu, attendu, strict=True))
    if isinstance(attendu, dict):
        return obtenu.keys() == attendu.keys() and all(_identique(obtenu[cle], attendu[cle]) for cle in attendu)
    return bool(obtenu == attendu)


def _jouer(essai: Essai, executeur: Executeur) -> ResultatEssai:
    try:
        lignes = executeur(essai.sql, essai.parametres)
    except Exception as erreur:                       # un essai est une SONDE : toute panne est un résultat à rapporter, pas à propager
        if essai.attendu is ErreurService and isinstance(erreur, ErreurService):
            message = _borne(str(erreur))
            if essai.motif_du_refus.lower() in str(erreur).lower():
                return ResultatEssai(essai.nom, True, f"le service a refusé comme prévu : {message}")
            return ResultatEssai(
                essai.nom, False, f"refus d'une autre nature que celle attendue (« {essai.motif_du_refus} » absent du message) : {message}"
            )
        return ResultatEssai(essai.nom, False, _borne(f"{type(erreur).__name__} : {erreur}"))
    if essai.attendu is ErreurService:
        return ResultatEssai(essai.nom, False, f"aurait dû échouer, a rendu {_borne(repr(lignes))}")
    if _identique(lignes, essai.attendu):
        return ResultatEssai(essai.nom, True, "")
    return ResultatEssai(essai.nom, False, _borne(f"attendu {essai.attendu!r}, obtenu {lignes!r}"))


def jouer_les_essais(executeur: Executeur) -> list[ResultatEssai]:
    """Joue tous les essais dans l'ordre, un par requête ; une panne ou un écart n'arrête pas les suivants."""
    return [_jouer(essai, executeur) for essai in ESSAIS]


# ==============================================================================
# 🔎 LA PRÉSENCE
# ==============================================================================
@dataclass(frozen=True)
class Presence:
    """Pour chacun de nos schémas (`ops`) et de nos tables (`ops.registre`) : existe-t-il déjà dans le catalogue ?"""
    schemas: dict[str, bool]
    tables: dict[str, bool]


def _liste_sql(noms: Sequence[str]) -> str:
    return ", ".join(f"'{nom}'" for nom in noms)


def verifier_la_presence(contrat: Contrat, catalogue: str, executeur: Executeur) -> Presence:
    """Deux `SELECT` sur le catalogue d'information, limités à nos trois schémas ; le catalogue inconnu est refusé avant tout envoi."""
    environnement_du_catalogue(catalogue)
    base = f"`{catalogue}`.information_schema"
    schemas_vus = {ligne["schema_name"] for ligne in executeur(
        f"SELECT schema_name FROM {base}.schemata WHERE schema_name IN ({_liste_sql(SCHEMAS)})", {})}
    tables_vues = {(ligne["table_schema"], ligne["table_name"]) for ligne in executeur(
        f"SELECT table_schema, table_name FROM {base}.tables WHERE table_schema IN ({_liste_sql(SCHEMAS)})", {})}
    return Presence(
        schemas={schema: schema in schemas_vus for schema in SCHEMAS},
        tables={f"{table.schema}.{table.nom}": (table.schema, table.nom) in tables_vues for table in toutes_les_tables(contrat)},
    )


# ==============================================================================
# 🚀 LE LANCEUR
# ==============================================================================
def fabriquer_executeur_reel(profil: str, entrepot: str) -> Executeur:
    """L'exécuteur réel : le client du SDK est créé ICI, avec le profil de la personne (le SDK n'est demandé qu'à ce moment)."""
    try:
        from databricks.sdk import WorkspaceClient
    except ImportError as erreur:
        raise RuntimeError(
            "Le SDK Databricks n'est pas installé : `pip install -e \".[databricks]\"` depuis la racine du dépôt."
        ) from erreur
    return ExecuteurDatabricks(WorkspaceClient(profile=profil), entrepot)


def _lecteur_d_arguments() -> argparse.ArgumentParser:
    lecteur = argparse.ArgumentParser(
        prog="python -m rd_manuf.sondage",
        description="Première exécution sur un workspace, en lecture seule : essais de conversion et présence de nos tables.",
    )
    lecteur.add_argument("--profil", required=True, help="le profil de connexion Databricks de la personne qui lance")
    lecteur.add_argument("--entrepot", required=True, help="l'identifiant de l'entrepôt SQL à utiliser")
    lecteur.add_argument("--catalogue", default=CATALOGUE_PAR_DEFAUT, help=f"refusé s'il n'est pas le playground ({CATALOGUE_PAR_DEFAUT})")
    lecteur.add_argument("--contrat", default=CONTRAT_PAR_DEFAUT, help="le contrat de données, relatif au dossier courant")
    return lecteur


def principal(
    argv: Sequence[str] | None = None,
    *,
    fabriquer: Callable[[str, str], Executeur] = fabriquer_executeur_reel,
    sortie: Callable[[str], Any] = print,
) -> int:
    arguments = _lecteur_d_arguments().parse_args(argv)
    if arguments.catalogue != CATALOGUE_PAR_DEFAUT:
        sortie(f"Le sondage ne s'exécute que sur le catalogue playground ({CATALOGUE_PAR_DEFAUT}), pas sur « {arguments.catalogue} ».")
        return 2
    try:
        contrat = charger_contrat(Path(arguments.contrat))
    except (OSError, ContratInvalide) as erreur:
        sortie(f"Contrat illisible : {_borne(str(erreur))}. Lancer depuis la racine du dépôt, ou donner le chemin avec --contrat.")
        return 2
    try:
        executeur = fabriquer(arguments.profil, arguments.entrepot)
        executeur(SQL_CONNEXION, {})              # un jeton expiré ou un mauvais entrepôt échoue ICI, une seule fois, pas sur chaque essai
    except Exception as erreur:
        sortie(f"Connexion impossible : {_borne(f'{type(erreur).__name__} : {erreur}')}")
        return 2

    sortie(f"Sondage en lecture seule du catalogue {arguments.catalogue}.")
    resultats = jouer_les_essais(executeur)
    for resultat in resultats:
        sortie(f"{'OK' if resultat.reussi else 'ÉCART':<7} {resultat.nom}" + (f" : {resultat.detail}" if resultat.detail else ""))
    conformes = sum(resultat.reussi for resultat in resultats)
    sortie(f"Essais : {conformes} sur {len(resultats)} conformes.")

    code = 0 if conformes == len(resultats) else 1
    try:
        presence = verifier_la_presence(contrat, arguments.catalogue, executeur)
    except Exception as erreur:
        sortie(f"ÉCHEC de la vérification de présence : {_borne(f'{type(erreur).__name__} : {erreur}')}")
        code = 1
    else:
        for schema, present in presence.schemas.items():
            sortie(f"schéma {schema:<7}: {'présent' if present else 'absent'}")
        for table, presente in presence.tables.items():
            sortie(f"table {table:<22}: {'présente (table ou vue)' if presente else 'absente'}")
        sortie("« absent » veut dire absent pour ce profil, qui ne voit que ce sur quoi il a des droits.")
    sortie("Rien n'a été créé ni écrit : lecture seule.")
    return code


if __name__ == "__main__":
    raise SystemExit(principal())
