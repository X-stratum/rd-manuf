"""L'ÉCRIVAIN : ranger dans des tables ce que le pipeline produit, sans jamais se répéter. Ce module ne parle à aucun workspace.

Trois couches, de la plus pure à la plus proche de Databricks :
1. les CONVERSIONS : une ligne du pipeline (registre, bronze, ligne coupée, silver, quarantaine) devient un ENREGISTREMENT, un
   dictionnaire aux colonnes et aux types du schéma de sa table (`tables.py`), et revient identique ;
2. le MAGASIN : deux opérations, « insérer ce qui manque » sur une clé, et « lire dans l'ordre ». `MagasinEnMemoire` en est la
   référence ; `MagasinSql` écrit des `MERGE` insert-seuls par un « exécuteur » (une fonction `(sql, paramètres)`) que l'on branche
   sur Databricks ;
3. l'ÉTAT adossé à un magasin, `EtatMagasin`, qui expose les mêmes noms que `journee.Etat` : `jouer` fonctionne sans changement.

PROTOCOLE D'ÉCRITURE d'une livraison : les couches de données d'abord, le REGISTRE en dernier. Entre deux tables il n'y a pas
d'atomicité ; le registre est ce qui décide d'un rejeu, donc ce qui dit « cette livraison est intégrée ». S'il manque, la livraison
n'est pas intégrée : on la reprend avec la même heure d'arrivée, et comme chaque écriture n'insère que ce qui manque sur la clé de
traçabilité, rien n'est dupliqué. Tant qu'une livraison n'a pas sa décision, ses lignes de données ne sont pas lues comme intégrées, et
aucune livraison classée après elle n'est écrite (`DecisionManquante`) : l'ordre du silver fait foi, la reprise passe avant la suite.

Les valeurs viennent de fichiers qu'une usine envoie : aucune n'entre jamais dans le texte d'une requête. Elles voyagent en un
paramètre JSON, typé par le schéma de la table. Seuls des identifiants (catalogue, schéma, table, colonnes, tous connus du code ou
validés) entrent dans le SQL.

Ce que ce module ne prouve pas, faute de workspace : la sémantique réelle de Delta (`MERGE`, types, tableaux, fuseau d'une colonne
TIMESTAMP), l'acceptation du paramètre JSON par le service, la taille maximale d'une requête (`TAILLE_LOT` est un choix prudent,
pas une mesure).
"""
from __future__ import annotations

import copy
import json
import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, Protocol

from rd_manuf.alertes import Alerte
from rd_manuf.bronze import STATUTS_BRONZE, LigneBronze, LigneBronzeCoupee
from rd_manuf.config import environnement_du_catalogue
from rd_manuf.contrat import CodeErreur, Contrat, Violation
from rd_manuf.pipeline import ResultatLivraison
from rd_manuf.registre import STATUTS_INTEGRES, LigneRegistre
from rd_manuf.silver import LigneQuarantaine, LigneSilver
from rd_manuf.tables import (
    BRONZE_COUPEES,
    IDENTIFIANT_SIMPLE,
    QUARANTAINE,
    REGISTRE,
    SCHEMA_BRONZE,
    SCHEMA_OPS,
    SCHEMA_SILVER,
    Table,
    sql_creation,
    table_bronze,
    table_silver,
    toutes_les_tables,
)

TAILLE_LOT = 1000       # enregistrements par requête : un choix prudent, la limite réelle se mesurera sur un workspace
Enregistrement = dict[str, Any]
Executeur = Callable[[str, dict[str, str]], list[dict[str, Any]]]
"""`executeur(sql, paramètres)` : lance la requête et rend ses lignes (vide pour une écriture). Les valeurs rendues doivent déjà avoir les
types Python des schémas (entier, décimal, date, instant UTC, liste, dictionnaire) : c'est à l'exécuteur réel de convertir ce que le service
rend en texte."""


# ==============================================================================
# 🧬 LES CONVERSIONS
# ==============================================================================
def _utc(instant: datetime | None) -> datetime | None:
    """Un instant s'écrit toujours en UTC : sans fuseau il ne se compare pas, on le refuse plutôt que de le deviner."""
    if instant is None:
        return None
    if instant.utcoffset() is None:
        raise ValueError(f"Un instant sans fuseau ne peut pas être écrit (UTC attendu) : {instant.isoformat()}.")
    return instant.astimezone(UTC)


def _dans_l_ordre(table: Table, valeurs: dict[str, Any]) -> Enregistrement:
    """Les colonnes de la table, dans son ordre : une valeur de trop ou en moins est une erreur du code, jamais ignorée."""
    noms = [colonne.nom for colonne in table.colonnes]
    if set(valeurs) != set(noms):
        raise ValueError(f"Colonnes de {table.schema}.{table.nom} : attendu {noms}, reçu {sorted(valeurs)}.")
    return {nom: valeurs[nom] for nom in noms}


def _registre_vers(ligne: LigneRegistre) -> Enregistrement:
    return _dans_l_ordre(REGISTRE, {
        "livraison_id": ligne.livraison_id, "statut": ligne.statut, "version_contrat": ligne.version_contrat,
        "nb_recues": ligne.nb_recues, "nb_valides": ligne.nb_valides, "nb_rejetees": ligne.nb_rejetees,
        "codes_refus": list(ligne.codes_refus), "pays": ligne.pays, "usine": ligne.usine, "ligne": ligne.ligne,
        "horodatage_fichier": _utc(ligne.horodatage_fichier), "heure_arrivee": _utc(ligne.heure_arrivee),
        "empreinte": ligne.empreinte, "rejeu_de": ligne.rejeu_de,
    })


def _registre_depuis(e: Enregistrement) -> LigneRegistre:
    return LigneRegistre(
        livraison_id=e["livraison_id"], statut=e["statut"], version_contrat=e["version_contrat"], nb_recues=e["nb_recues"],
        nb_valides=e["nb_valides"], nb_rejetees=e["nb_rejetees"], codes_refus=tuple(e["codes_refus"]), pays=e["pays"], usine=e["usine"],
        ligne=e["ligne"], horodatage_fichier=e["horodatage_fichier"], heure_arrivee=e["heure_arrivee"], empreinte=e["empreinte"],
        rejeu_de=e["rejeu_de"],
    )


def _coupee_vers(ligne: LigneBronzeCoupee) -> Enregistrement:
    return _dans_l_ordre(BRONZE_COUPEES, {
        "livraison_id": ligne.livraison_id, "heure_arrivee": _utc(ligne.heure_arrivee), "numero_fichier": ligne.numero_fichier,
        "champs": list(ligne.champs),
    })


def _coupee_depuis(e: Enregistrement) -> LigneBronzeCoupee:
    return LigneBronzeCoupee(livraison_id=e["livraison_id"], heure_arrivee=e["heure_arrivee"], numero_fichier=e["numero_fichier"],
                             champs=tuple(e["champs"]))


def _quarantaine_vers(ligne: LigneQuarantaine) -> Enregistrement:
    bien_formee = isinstance(ligne.contenu, dict)
    return _dans_l_ordre(QUARANTAINE, {
        "livraison_id": ligne.livraison_id, "heure_arrivee": _utc(ligne.heure_arrivee), "version_contrat": ligne.version_contrat,
        "numero_ligne": ligne.numero_ligne, "numero_fichier": ligne.numero_fichier,
        "contenu_texte": dict(ligne.contenu) if bien_formee else None,
        "champs_bruts": None if bien_formee else list(ligne.contenu),
        "violations": [{"code": v.code.value, "colonnes": list(v.colonnes), "message": v.message} for v in ligne.violations],
    })


def _quarantaine_depuis(e: Enregistrement) -> LigneQuarantaine:
    contenu = dict(e["contenu_texte"]) if e["contenu_texte"] is not None else tuple(e["champs_bruts"])
    violations = tuple(Violation(CodeErreur(v["code"]), tuple(v["colonnes"]), v["message"]) for v in e["violations"])
    return LigneQuarantaine(livraison_id=e["livraison_id"], heure_arrivee=e["heure_arrivee"], version_contrat=e["version_contrat"],
                            numero_ligne=e["numero_ligne"], numero_fichier=e["numero_fichier"], contenu=contenu, violations=violations)


def _bronze_vers(table: Table) -> Callable[[LigneBronze], Enregistrement]:
    def vers(ligne: LigneBronze) -> Enregistrement:
        return _dans_l_ordre(table, {"livraison_id": ligne.livraison_id, "heure_arrivee": _utc(ligne.heure_arrivee),
                                     "numero_ligne": ligne.numero_ligne, **ligne.valeurs})
    return vers


def _bronze_depuis(contrat: Contrat) -> Callable[[Enregistrement], LigneBronze]:
    def depuis(e: Enregistrement) -> LigneBronze:
        return LigneBronze(livraison_id=e["livraison_id"], heure_arrivee=e["heure_arrivee"], numero_ligne=e["numero_ligne"],
                           valeurs={colonne.nom: e[colonne.nom] for colonne in contrat.colonnes})
    return depuis


def _silver_vers(table: Table) -> Callable[[LigneSilver], Enregistrement]:
    def vers(ligne: LigneSilver) -> Enregistrement:
        return _dans_l_ordre(table, {
            "livraison_id": ligne.livraison_id, "heure_arrivee": _utc(ligne.heure_arrivee), "pays": ligne.pays, "usine": ligne.usine,
            "numero_ligne": ligne.numero_ligne, "version_contrat": ligne.version_contrat, **ligne.valeurs,
        })
    return vers


def _silver_depuis(contrat: Contrat) -> Callable[[Enregistrement], LigneSilver]:
    def depuis(e: Enregistrement) -> LigneSilver:
        return LigneSilver(livraison_id=e["livraison_id"], heure_arrivee=e["heure_arrivee"], pays=e["pays"], usine=e["usine"],
                           numero_ligne=e["numero_ligne"], version_contrat=e["version_contrat"],
                           valeurs={colonne.nom: e[colonne.nom] for colonne in contrat.colonnes})
    return depuis


@dataclass(frozen=True)
class Couche:
    """Une table, la clé qui identifie un enregistrement, l'ordre de lecture, et les deux sens de la conversion."""

    table: Table
    cles: tuple[str, ...]                                   # la clé de traçabilité : ce sur quoi on décide qu'un enregistrement existe déjà
    ordre: tuple[str, ...]                                  # l'ordre de lecture : l'arrivée d'abord, puis la clé
    vers_enregistrement: Callable[[Any], Enregistrement]
    depuis_enregistrement: Callable[[Enregistrement], Any]


def _couche(table: Table, cles: tuple[str, ...], vers: Callable, depuis: Callable) -> Couche:
    return Couche(table, cles, ("heure_arrivee", "livraison_id", *cles[2:]), vers, depuis)


def couches(contrat: Contrat) -> dict[str, Couche]:
    """Les cinq couches, dans l'ordre des tables. Les clés sont écrites ICI, une seule fois : l'idempotence en dépend.

    Les parties de clé de la quarantaine (`numero_ligne`, `numero_fichier`) sont nulles selon la forme de la ligne : elles se comparent par
    l'égalité tolérante au NULL.
    """
    bronze, silver = table_bronze(contrat), table_silver(contrat)
    return {
        "registre": _couche(REGISTRE, ("livraison_id", "heure_arrivee"), _registre_vers, _registre_depuis),
        "bronze": _couche(bronze, ("livraison_id", "heure_arrivee", "numero_ligne"), _bronze_vers(bronze), _bronze_depuis(contrat)),
        "coupees": _couche(BRONZE_COUPEES, ("livraison_id", "heure_arrivee", "numero_fichier"), _coupee_vers, _coupee_depuis),
        "silver": _couche(silver, ("livraison_id", "heure_arrivee", "numero_ligne"), _silver_vers(silver), _silver_depuis(contrat)),
        "quarantaine": _couche(QUARANTAINE, ("livraison_id", "heure_arrivee", "numero_ligne", "numero_fichier"), _quarantaine_vers,
                               _quarantaine_depuis),
    }


# ==============================================================================
# 🗄️ LE MAGASIN
# ==============================================================================
class Magasin(Protocol):
    """Ce que l'état demande à un lieu de stockage : insérer ce qui manque, lire dans l'ordre. Rien d'autre, jamais de mise à jour."""

    def inserer_absentes(self, table: Table, cles: tuple[str, ...], enregistrements: Sequence[Enregistrement]) -> None: ...

    def lire(self, table: Table, ordre: tuple[str, ...]) -> list[Enregistrement]: ...


def _cle_de_tri(enregistrement: Enregistrement, ordre: tuple[str, ...]) -> tuple:
    """Les valeurs absentes d'abord, comme Spark pour un tri croissant."""
    return tuple((enregistrement[nom] is not None, enregistrement[nom] if enregistrement[nom] is not None else 0) for nom in ordre)


class MagasinEnMemoire:
    """La référence : des listes d'enregistrements par table. Insérer ce qui manque : l'ancien est gardé, rien n'est partagé."""

    def __init__(self) -> None:
        self._tables: dict[tuple[str, str], list[Enregistrement]] = {}

    def inserer_absentes(self, table: Table, cles: tuple[str, ...], enregistrements: Sequence[Enregistrement]) -> None:
        stockes = self._tables.setdefault((table.schema, table.nom), [])
        presentes = {tuple(e[cle] for cle in cles) for e in stockes}
        for enregistrement in enregistrements:
            cle = tuple(enregistrement[nom] for nom in cles)          # `None == None` : une clé à partie absente se retrouve
            if cle not in presentes:
                presentes.add(cle)
                stockes.append(copy.deepcopy(enregistrement))

    def lire(self, table: Table, ordre: tuple[str, ...]) -> list[Enregistrement]:
        return copy.deepcopy(sorted(self._tables.get((table.schema, table.nom), []), key=lambda e: _cle_de_tri(e, ordre)))


# ==============================================================================
# 📚 L'ÉTAT ADOSSÉ À UN MAGASIN
# ==============================================================================
# Une couche de données n'est lue que pour les livraisons dont le REGISTRE dit qu'elles y entrent : le bronze (et les lignes coupées) pour
# une livraison acceptée ou bloquée, le silver et la quarantaine pour une livraison intégrée. Sans décision, ou avec une autre, les lignes
# écrites ne comptent pas : une panne entre les données et le registre ne doit pas faire croire à une livraison qui n'est pas intégrée.
STATUTS_QUI_FONT_ENTRER = {"bronze": STATUTS_BRONZE, "coupees": STATUTS_BRONZE, "silver": STATUTS_INTEGRES, "quarantaine": STATUTS_INTEGRES}


class DecisionManquante(RuntimeError):
    """Une livraison arrivée plus tôt a des lignes de données sans registre : elle doit être reprise avant d'en écrire une autre.

    Écrire la suivante d'abord la jugerait sans connaître la première : elles pourraient entrer toutes deux au silver, un même lot deux
    fois, sans alerte. Rien n'est écrit quand cette erreur est levée.
    """


class EtatMagasin:
    """Les mêmes noms que `journee.Etat` (`registre`, `lignes_bronze`, ...) et `appliquer`, mais lus dans un magasin à chaque accès.

    `appliquer` écrit les couches de données d'abord et le registre EN DERNIER (voir le protocole, en tête de module). Tant que le registre
    manque, la livraison n'est pas intégrée : ses lignes de données ne sont PAS rendues par `lignes_bronze`, `lignes_coupees`,
    `lignes_silver` et `quarantaine` (sinon la livraison suivante les prendrait pour du déjà intégré), mais `lignes_sans_decision` les
    garde à portée pour les diagnostiquer. La reprise de la livraison, à la même heure d'arrivée, les rend lisibles en écrivant le registre.
    Les alertes n'ont pas de table (elles seront envoyées, pas stockées) : elles restent une liste en mémoire.
    Les listes rendues sont neuves.
    """

    def __init__(self, contrat: Contrat, magasin: Magasin) -> None:
        self._couches = couches(contrat)
        self._magasin = magasin
        self.alertes: list[Alerte] = []

    def _enregistrements(self, nom: str) -> list[Enregistrement]:
        couche = self._couches[nom]
        return self._magasin.lire(couche.table, couche.ordre)

    def _decisions(self, nom: str) -> set[tuple[str, datetime]]:
        """Les livraisons (nom, heure d'arrivée) dont le registre dit que la couche `nom` leur appartient."""
        statuts = STATUTS_QUI_FONT_ENTRER[nom]
        return {(e["livraison_id"], e["heure_arrivee"]) for e in self._enregistrements("registre") if e["statut"] in statuts}

    def _lire_donnees(self, nom: str, avec_decision: bool) -> list:
        decisions = self._decisions(nom)
        couche = self._couches[nom]
        return [
            couche.depuis_enregistrement(e) for e in self._enregistrements(nom)
            if ((e["livraison_id"], e["heure_arrivee"]) in decisions) == avec_decision
        ]

    @property
    def registre(self) -> list[LigneRegistre]:
        couche = self._couches["registre"]
        return [couche.depuis_enregistrement(e) for e in self._enregistrements("registre")]

    @property
    def lignes_bronze(self) -> list[LigneBronze]:
        return self._lire_donnees("bronze", True)

    @property
    def lignes_coupees(self) -> list[LigneBronzeCoupee]:
        return self._lire_donnees("coupees", True)

    @property
    def lignes_silver(self) -> list[LigneSilver]:
        return self._lire_donnees("silver", True)

    @property
    def quarantaine(self) -> list[LigneQuarantaine]:
        return self._lire_donnees("quarantaine", True)

    def lignes_sans_decision(self) -> dict[str, list]:
        """Les lignes de données écrites dont le registre ne dit pas (encore) qu'elles comptent, par couche : à diagnostiquer."""
        return {nom: self._lire_donnees(nom, False) for nom in STATUTS_QUI_FONT_ENTRER}

    def _ecrire(self, nom: str, objets: Sequence[Any]) -> None:
        couche = self._couches[nom]
        self._magasin.inserer_absentes(couche.table, couche.cles, [couche.vers_enregistrement(objet) for objet in objets])

    def _livraisons_a_reprendre(self, ligne: LigneRegistre) -> list[str]:
        """Les livraisons classées AVANT celle-ci, dont des lignes de données n'ont pas de décision.

        Le classement est celui du silver : `(heure d'arrivée, nom)`. À heure égale le nom départage, l'heure l'emporte sur le nom. La
        reprise d'une livraison garde son heure et son nom : ses propres lignes sans décision ne sont pas classées avant elle.
        """
        arrivee = _utc(ligne.heure_arrivee)                    # la même erreur qu'à l'écriture, avec ou sans lignes en attente
        en_attente = {(o.livraison_id, o.heure_arrivee) for lignes in self.lignes_sans_decision().values() for o in lignes}
        plus_tot = [(nom, heure) for nom, heure in en_attente if (heure, nom) < (arrivee, ligne.livraison_id)]
        return sorted(f"{nom} (arrivée {heure:%Y-%m-%d %H:%M:%S} UTC)" for nom, heure in plus_tot)

    def appliquer(self, resultat: ResultatLivraison) -> None:
        """Ajoute ce qu'une livraison a produit. Reprise possible : rejouée avec la même heure d'arrivée, elle ne duplique rien.

        Refuse, sans rien écrire, d'écrire une livraison tant qu'une livraison classée avant elle (heure d'arrivée, puis nom, comme au
        silver) a des lignes de données sans registre (`DecisionManquante`) : il faut reprendre celle-là d'abord, puis rejuger celle-ci.
        Une livraison classée APRÈS, sans registre, ne gêne pas.
        """
        a_reprendre = self._livraisons_a_reprendre(resultat.ligne_registre)
        if a_reprendre:
            raise DecisionManquante(
                f"{resultat.ligne_registre.livraison_id} ne peut pas être écrite : reprendre d'abord {', '.join(a_reprendre)}, "
                f"dont des lignes de données n'ont pas de ligne de registre."
            )
        for nom, objets in (("bronze", resultat.lignes_bronze), ("coupees", resultat.lignes_coupees),
                            ("silver", resultat.silver.lignes), ("quarantaine", resultat.silver.quarantaine)):
            if objets:
                self._ecrire(nom, objets)
        self._ecrire("registre", [resultat.ligne_registre])       # la décision, en dernier : sans elle, la livraison n'est pas intégrée
        self.alertes.extend(resultat.alertes)


# ==============================================================================
# 🔤 LE JSON : ce qui voyage dans le paramètre
# ==============================================================================
def _vers_json(valeur: Any) -> Any:
    if valeur is None or isinstance(valeur, (str, bool, int)):
        return valeur
    if isinstance(valeur, float):
        if not math.isfinite(valeur):
            raise ValueError(f"Un décimal infini ou indéfini ne s'écrit pas en JSON : {valeur}.")
        return valeur
    if isinstance(valeur, datetime):                          # avant `date` : un instant est aussi une date en Python
        return _utc(valeur).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(valeur, date):
        return valeur.isoformat()
    if isinstance(valeur, (list, tuple)):
        return [_vers_json(element) for element in valeur]
    if isinstance(valeur, dict):
        return {str(cle): _vers_json(element) for cle, element in valeur.items()}
    raise TypeError(f"Ce type ne s'écrit pas en JSON : {type(valeur).__name__}.")


def en_json(enregistrements: Sequence[Enregistrement]) -> str:
    """Les enregistrements en texte JSON : dates et instants en texte ISO (UTC avec microsecondes), décimaux exacts, aucun NaN."""
    return json.dumps([_vers_json(e) for e in enregistrements], ensure_ascii=False, allow_nan=False, separators=(",", ":"))


# ==============================================================================
# 🧾 LE MAGASIN SQL
# ==============================================================================
def _identifiant(nom: str) -> str:
    """Un identifiant entre accents graves, seulement s'il est simple : ce qui est connu du code se vérifie quand même."""
    if not IDENTIFIANT_SIMPLE.fullmatch(nom):
        raise ValueError(f"« {nom} » n'est pas un identifiant simple (lettres, chiffres et _) : il n'entre pas dans une requête.")
    return f"`{nom}`"


def _nom_complet(catalogue: str, table: Table) -> str:
    return f"`{catalogue}`.{_identifiant(table.schema)}.{_identifiant(table.nom)}"


class MagasinSql:
    """Écrit et lit des tables par des requêtes SQL envoyées à un exécuteur. Les valeurs ne sont jamais dans le texte : un paramètre JSON.

    Insérer ce qui manque est un `MERGE` insert-seul sur la clé, avec l'égalité tolérante au NULL (`<=>`) : `NULL = NULL` n'est pas vrai en
    SQL, et une clé à partie absente (la quarantaine) serait réinsérée à chaque reprise. Les enregistrements partent par lots.
    """

    def __init__(self, executeur: Executeur, catalogue: str, taille_lot: int = TAILLE_LOT) -> None:
        environnement_du_catalogue(catalogue)                 # un catalogue inconnu est refusé, jamais traité comme sûr
        if taille_lot < 1:
            raise ValueError(f"La taille d'un lot doit être positive : {taille_lot}.")
        self._executeur = executeur
        self._catalogue = catalogue
        self._taille_lot = taille_lot

    def inserer_absentes(self, table: Table, cles: tuple[str, ...], enregistrements: Sequence[Enregistrement]) -> None:
        nom = _nom_complet(self._catalogue, table)
        colonnes = {colonne.nom: colonne.type_sql for colonne in table.colonnes}
        for colonne in colonnes:
            _identifiant(colonne)
        absentes = [cle for cle in cles if cle not in colonnes]
        if absentes or not cles:
            raise ValueError(
                f"La clé de {table.schema}.{table.nom}, {list(cles)}, doit être faite de colonnes de la table (inconnues : {absentes})."
            )
        for enregistrement in enregistrements:
            if set(enregistrement) != set(colonnes):
                manquantes, en_trop = sorted(set(colonnes) - set(enregistrement)), sorted(set(enregistrement) - set(colonnes))
                raise ValueError(
                    f"Les colonnes de {table.schema}.{table.nom} ne sont pas les siennes : manquantes {manquantes}, en trop {en_trop}."
                )
        if not enregistrements:
            return
        schema = ", ".join(f"`{colonne}` {type_sql}" for colonne, type_sql in colonnes.items())
        condition = " AND ".join(f"cible.`{cle}` <=> source.`{cle}`" for cle in cles)
        sql = (
            f"MERGE INTO {nom} AS cible\n"
            f"USING (SELECT lignes.* FROM (SELECT explode(from_json(:lignes, 'ARRAY<STRUCT<{schema}>>')) AS lignes)) AS source\n"
            f"ON {condition}\n"
            f"WHEN NOT MATCHED THEN INSERT *"
        )
        for debut in range(0, len(enregistrements), self._taille_lot):
            self._executeur(sql, {"lignes": en_json(enregistrements[debut:debut + self._taille_lot])})

    def lire(self, table: Table, ordre: tuple[str, ...]) -> list[Enregistrement]:
        tri = ", ".join(_identifiant(colonne) for colonne in ordre)
        return self._executeur(f"SELECT * FROM {_nom_complet(self._catalogue, table)} ORDER BY {tri}", {})


def creer_tables(contrat: Contrat, catalogue: str, executeur: Executeur) -> None:
    """Crée les trois schémas puis les cinq tables si elles n'existent pas, dans le catalogue demandé (refusé s'il est inconnu)."""
    environnement_du_catalogue(catalogue)
    for schema in (SCHEMA_OPS, SCHEMA_BRONZE, SCHEMA_SILVER):
        executeur(f"CREATE SCHEMA IF NOT EXISTS `{catalogue}`.`{schema}`", {})
    for table in toutes_les_tables(contrat):
        executeur(sql_creation(table, catalogue), {})
