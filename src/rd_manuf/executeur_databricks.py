"""L'EXÉCUTEUR DATABRICKS : envoie les requêtes de l'écrivain à un entrepôt SQL et rend des lignes aux types Python des schémas.

C'est le seul point de contact entre `ecriture.MagasinSql` et un workspace : `ExecuteurDatabricks(client, entrepot)` est un `Executeur`
(`executeur(sql, paramètres) -> lignes`). Il reçoit le CLIENT et l'identifiant de l'entrepôt de l'appelant et ne lit RIEN lui-même : ni
variable d'environnement, ni profil, ni fichier. L'adresse du workspace et le profil de connexion ne sont donc jamais dans le dépôt
(public) ; c'est la personne qui lance qui les fournit. Le SDK n'est demandé qu'au moment d'envoyer une requête : ce module s'importe
sans lui.

Ce qui part : le texte de la requête et des paramètres NOMMÉS (`:lignes`), tous en texte, jamais de valeur dans le texte. Ce qui revient :
le service rend chaque valeur en TEXTE (ou `None`) avec le type de sa colonne ; `convertir_valeur` en refait les types Python. Elle est
stricte : un texte qu'elle ne reconnaît pas lève (pour une liste, une table ou une structure, la forme extérieure seulement : le contenu des
éléments n'est pas vérifié), et aucune erreur ne contient la valeur reçue (ce peut être une donnée d'usine).

Limites connues, à confirmer sur un vrai workspace : la forme exacte des instants et des listes rendus par le service (la conversion
refuse ce qu'elle ne reconnaît pas plutôt que de deviner), la durée d'une requête sur la Free Edition, et le message d'un échec du service,
repris tel quel : avec le mode d'échec immédiat de `from_json`, il peut citer l'enregistrement fautif.
"""
import json
import math
import re
import time
from collections.abc import Callable
from contextlib import suppress
from datetime import UTC, date, datetime
from typing import Any

ATTENTE_SYNCHRONE = "50s"       # le maximum que l'API accepte pour une réponse immédiate ; au-delà on interroge
ETATS_EN_COURS = ("PENDING", "RUNNING")
ETAT_REUSSI = "SUCCEEDED"


class ErreurService(RuntimeError):
    """Le service a refusé, annulé ou n'a pas terminé la requête, ou rend un résultat qu'on ne peut pas lire en entier."""


# ==============================================================================
# 🔢 LA CONVERSION : le texte rendu par le service, redevenu une valeur Python
# ==============================================================================
_ENTIER = re.compile(r"-?[0-9]+")
_DECIMAL = re.compile(r"-?[0-9]+(\.[0-9]+)?([eE][+-]?[0-9]+)?")
_DATE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_INSTANT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}[T ][0-9]{2}:[0-9]{2}:[0-9]{2}(\.[0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})")


def _entier(texte: str) -> int:
    if not _ENTIER.fullmatch(texte):
        raise ValueError("pas un entier")
    return int(texte)


def _decimal(texte: str) -> float:
    if not _DECIMAL.fullmatch(texte):
        raise ValueError("pas un décimal")
    valeur = float(texte)
    if not math.isfinite(valeur):
        raise ValueError("décimal infini")
    return valeur


def _jour(texte: str) -> date:
    if not _DATE.fullmatch(texte):
        raise ValueError("pas une date")
    return date.fromisoformat(texte)


def _instant(texte: str) -> datetime:
    """Un instant AVEC fuseau, rendu en UTC. Sans fuseau on ne sait pas de quelle heure il s'agit : refusé, jamais pris pour de l'UTC."""
    if not _INSTANT.fullmatch(texte):
        raise ValueError("pas un instant avec fuseau")
    return datetime.fromisoformat(texte).astimezone(UTC)


def _json_de_forme(forme: type) -> Callable[[str], Any]:
    def convertir(texte: str) -> Any:
        valeur = json.loads(texte, parse_constant=_refuser_constante)
        if not isinstance(valeur, forme):
            raise ValueError("pas du JSON de la bonne forme")
        return valeur
    return convertir


def _refuser_constante(_nom: str) -> Any:
    raise ValueError("constante JSON refusée")


_CONVERTISSEURS: dict[str, Callable[[str], Any]] = {
    "STRING": str,
    "LONG": _entier,
    "DOUBLE": _decimal,
    "DATE": _jour,
    "TIMESTAMP": _instant,
    "ARRAY": _json_de_forme(list),
    "MAP": _json_de_forme(dict),
    "STRUCT": _json_de_forme(dict),
}


def convertir_valeur(texte: str | None, type_name: str, type_text: str) -> Any:
    """Le texte rendu par le service pour une colonne de ce type, en valeur Python ; `None` reste `None`.

    Seuls les types que le code produit sont reconnus (chaîne, entier, décimal, date, instant, liste, table, structure) : un autre est
    refusé plutôt que laissé en texte, ce qui donnerait une valeur du mauvais type sans erreur. Une erreur nomme le type, jamais la valeur.
    """
    convertisseur = _CONVERTISSEURS.get(type_name)
    if convertisseur is None:
        raise ValueError(f"Le type {type_name} ({type_text}) n'est pas un type que ce code produit : il n'est pas converti.")
    if texte is None:
        return None
    try:
        return convertisseur(texte)
    except (ValueError, OverflowError, RecursionError):        # un texte pathologique sort en ValueError, jamais en dépassement de pile
        raise ValueError(f"Une valeur de type {type_name} ({type_text}) est illisible.") from None


# ==============================================================================
# 📡 L'EXÉCUTEUR
# ==============================================================================
def _outils_du_sdk() -> tuple[Any, Any, Any, Any]:
    try:
        from databricks.sdk.service.sql import Disposition, ExecuteStatementRequestOnWaitTimeout, Format, StatementParameterListItem
    except ImportError as erreur:
        raise RuntimeError("Le SDK Databricks n'est pas installé : `pip install \"rd-manuf[databricks]\"`.") from erreur
    return StatementParameterListItem, ExecuteStatementRequestOnWaitTimeout, Disposition, Format


def _nom_de_l_etat(reponse: Any) -> str:
    etat = reponse.status.state
    return str(getattr(etat, "value", etat))


class ExecuteurDatabricks:
    """Lance des requêtes sur un entrepôt SQL par l'API d'exécution de requêtes, et rend les lignes aux types Python des colonnes.

    `client` est un client du SDK (ses requêtes passent par `client.statement_execution`), `entrepot` l'identifiant de l'entrepôt SQL : tous
    deux viennent de l'appelant. `dormir` et `horloge` sont injectables pour que les tests n'attendent pas pour de vrai. Une requête qui ne
    finit pas dans `attente_max` secondes APRÈS SON ENVOI (l'attente synchrone de l'envoi comprise) est annulée puis refusée.
    """

    def __init__(
        self,
        client: Any,
        entrepot: str,
        *,
        attente_max: float = 600.0,
        pause: float = 2.0,
        dormir: Callable[[float], None] = time.sleep,
        horloge: Callable[[], float] = time.monotonic,
    ) -> None:
        self._client = client
        self._entrepot = entrepot
        self._attente_max = attente_max
        self._pause = pause
        self._dormir = dormir
        self._horloge = horloge

    def __call__(self, sql: str, parametres: dict[str, str]) -> list[dict[str, Any]]:
        for nom, valeur in parametres.items():
            if not isinstance(valeur, str):
                raise TypeError(f"Le paramètre « {nom} » doit être du texte, pas {type(valeur).__name__}.")
        parametre, attente, disposition, format_ = _outils_du_sdk()
        service = self._client.statement_execution
        debut = self._horloge()                                # le délai compte depuis l'ENVOI, attente synchrone comprise
        reponse = service.execute_statement(
            statement=sql,
            warehouse_id=self._entrepot,
            parameters=[parametre(name=nom, value=valeur, type="STRING") for nom, valeur in parametres.items()] or None,
            wait_timeout=ATTENTE_SYNCHRONE,
            on_wait_timeout=attente.CONTINUE,
            disposition=disposition.INLINE,
            format=format_.JSON_ARRAY,
        )
        reponse = self._attendre(service, reponse, debut)
        etat = _nom_de_l_etat(reponse)
        if etat != ETAT_REUSSI:
            erreur = reponse.status.error
            message = getattr(erreur, "message", None)
            raise ErreurService(f"La requête a fini à l'état {etat}" + (f" : {message}" if message else " (sans message du service)") + ".")
        return self._lignes(service, reponse)

    def _attendre(self, service: Any, reponse: Any, debut: float) -> Any:
        while _nom_de_l_etat(reponse) in ETATS_EN_COURS:
            if self._horloge() - debut >= self._attente_max:
                with suppress(Exception):                      # l'annulation est un effort : son échec ne doit pas masquer le délai
                    service.cancel_execution(reponse.statement_id)
                raise ErreurService(f"La requête n'a pas fini dans le délai de {self._attente_max:g} s : elle a été annulée.")
            self._dormir(self._pause)
            reponse = service.get_statement(reponse.statement_id)
        return reponse

    def _lignes(self, service: Any, reponse: Any) -> list[dict[str, Any]]:
        manifest = reponse.manifest
        if manifest is None:
            return []
        if manifest.truncated:
            raise ErreurService("Le service a tronqué le résultat : il est refusé plutôt que rendu incomplet.")
        colonnes = manifest.schema.columns if manifest.schema is not None and manifest.schema.columns else []
        valeurs = list(reponse.result.data_array or []) if reponse.result is not None else []
        suivant = reponse.result.next_chunk_index if reponse.result is not None else None
        while suivant is not None:
            morceau = service.get_statement_result_chunk_n(reponse.statement_id, suivant)
            valeurs.extend(morceau.data_array or [])
            suivant = morceau.next_chunk_index
        return [self._ligne(colonnes, ligne) for ligne in valeurs]

    @staticmethod
    def _ligne(colonnes: list[Any], valeurs: list[str | None]) -> dict[str, Any]:
        if len(valeurs) != len(colonnes):
            raise ValueError(f"Une ligne rendue a {len(valeurs)} valeurs pour {len(colonnes)} colonnes.")
        ligne: dict[str, Any] = {}
        for colonne, texte in zip(colonnes, valeurs, strict=True):
            type_name = str(getattr(colonne.type_name, "value", colonne.type_name))
            try:
                ligne[colonne.name] = convertir_valeur(texte, type_name, colonne.type_text)
            except ValueError as erreur:
                raise ValueError(f"La colonne « {colonne.name} » : {erreur}") from None
        return ligne
