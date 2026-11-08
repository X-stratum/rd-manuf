"""Chargement d'un contrat de données (YAML) et contrôle d'une ligne contre ce contrat.

Cycle A : `charger_contrat` (auto-contrôle du contrat) et `controler_ligne` (types, obligatoire, plages,
motif, règles entre colonnes). Le contrôle d'une livraison entière arrive au cycle B.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

# ==============================================================================
# 1. 📚 VOCABULAIRE DU CONTRAT
# ==============================================================================
TYPE_CHAINE = "chaine"
TYPE_ENTIER = "entier"
TYPE_DECIMAL = "decimal"
TYPE_DATE = "date"
TYPES_SUPPORTES = (TYPE_CHAINE, TYPE_ENTIER, TYPE_DECIMAL, TYPE_DATE)

REGLE_INFERIEUR_OU_EGAL = "inferieur_ou_egal"
REGLES_INTER_SUPPORTEES = (REGLE_INFERIEUR_OU_EGAL,)

# Textes qui, dans un export de capteur ou de CSV, signifient « pas de valeur » (comparés en minuscules, sans espaces).
# La chaîne vide en fait partie : « " " » et « "" » se réduisent à "" après strip.
MARQUEURS_ABSENCE = frozenset({"", "nan", "nans", "null", "na"})

CLES_OBLIGATOIRES_CONTRAT = ("nom", "version", "cle_primaire", "colonnes", "seuils_qualite")


class ContratInvalide(ValueError):
    """Le fichier YAML du contrat est mal formé (erreur de conception, pas de données)."""


# ==============================================================================
# 2. 🧱 STRUCTURES
# ==============================================================================
@dataclass(frozen=True)
class Colonne:
    nom: str
    type: str
    obligatoire: bool = True
    minimum: float | None = None
    maximum: float | None = None
    motif: str | None = None
    valeurs_autorisees: tuple[str, ...] | None = None


@dataclass(frozen=True)
class RegleInter:
    nom: str
    type: str
    gauche: str
    droite: str


@dataclass(frozen=True)
class Contrat:
    nom: str
    version: str
    cle_primaire: tuple[str, ...]
    colonnes: tuple[Colonne, ...]
    regles_inter_colonnes: tuple[RegleInter, ...]
    taux_rejet_max: float
    brut: dict[str, Any] = field(repr=False, default_factory=dict)  # YAML d'origine


# ==============================================================================
# 3. 📥 CHARGEMENT ET AUTO-CONTRÔLE DU CONTRAT
# ==============================================================================
def charger_contrat(chemin: str | Path) -> Contrat:
    """Lit le YAML et vérifie qu'il est cohérent AVANT de s'en servir."""
    brut = yaml.safe_load(Path(chemin).read_text(encoding="utf-8"))
    if not isinstance(brut, dict):
        raise ContratInvalide(f"{chemin} : le contrat doit être un dictionnaire YAML.")

    manquantes = [cle for cle in CLES_OBLIGATOIRES_CONTRAT if cle not in brut]
    if manquantes:
        raise ContratInvalide(f"{chemin} : clés manquantes {manquantes}.")

    colonnes = []
    for definition in brut["colonnes"]:
        if definition.get("type") not in TYPES_SUPPORTES:
            raise ContratInvalide(f"Colonne « {definition.get('nom')} » : type inconnu « {definition.get('type')} ».")
        colonnes.append(
            Colonne(
                nom=definition["nom"],
                type=definition["type"],
                obligatoire=definition.get("obligatoire", True),
                minimum=definition.get("minimum"),
                maximum=definition.get("maximum"),
                motif=definition.get("motif"),
                valeurs_autorisees=tuple(definition["valeurs_autorisees"]) if "valeurs_autorisees" in definition else None,
            )
        )

    noms = {colonne.nom for colonne in colonnes}
    for cle in brut["cle_primaire"]:
        if cle not in noms:
            raise ContratInvalide(f"Clé primaire « {cle} » absente des colonnes.")

    regles = []
    for definition in brut.get("regles_inter_colonnes", []):
        if definition["type"] not in REGLES_INTER_SUPPORTEES:
            raise ContratInvalide(f"Règle « {definition['nom']} » : type inconnu « {definition['type']} ».")
        for cle in ("gauche", "droite"):
            if definition[cle] not in noms:
                raise ContratInvalide(f"Règle « {definition['nom']} » : colonne « {definition[cle]} » inconnue.")
        regles.append(RegleInter(definition["nom"], definition["type"], definition["gauche"], definition["droite"]))

    taux = brut["seuils_qualite"]["taux_rejet_max"]
    if not 0 <= taux <= 1:
        raise ContratInvalide(f"taux_rejet_max doit être entre 0 et 1 (reçu {taux}).")

    return Contrat(
        nom=brut["nom"],
        version=str(brut["version"]),
        cle_primaire=tuple(brut["cle_primaire"]),
        colonnes=tuple(colonnes),
        regles_inter_colonnes=tuple(regles),
        taux_rejet_max=float(taux),
        brut=brut,
    )


# ==============================================================================
# 4. 🔎 CONTRÔLE D'UNE LIGNE
# ==============================================================================
def est_absente(valeur: Any) -> bool:
    """Une valeur est absente si elle est None, un NaN réel, une chaîne vide/blanche ou un marqueur d'absence.

    La comparaison aux marqueurs est EXACTE (après strip et minuscules) : « nano » ou « 0 » restent des valeurs.
    """
    if valeur is None:
        return True
    if isinstance(valeur, float):
        return math.isnan(valeur)
    return isinstance(valeur, str) and valeur.strip().lower() in MARQUEURS_ABSENCE


def convertir_valeur(type_colonne: str, valeur: Any) -> Any:
    """Convertit vers le type du contrat. Lève ValueError si impossible."""
    if type_colonne == TYPE_CHAINE:
        return str(valeur).strip()
    if type_colonne == TYPE_ENTIER:
        if isinstance(valeur, bool) or (isinstance(valeur, float) and not valeur.is_integer()):
            raise ValueError("entier attendu")
        return int(valeur)
    if type_colonne == TYPE_DECIMAL:
        if isinstance(valeur, bool):
            raise ValueError("décimal attendu")
        nombre = float(valeur)
        if not math.isfinite(nombre):   # inf / -inf : de vrais nombres aberrants, jamais une absence
            raise ValueError("décimal fini attendu")
        return nombre
    if type_colonne == TYPE_DATE:
        return valeur if isinstance(valeur, date) else date.fromisoformat(str(valeur).strip())
    raise ValueError(f"type inconnu {type_colonne}")


def controler_ligne(contrat: Contrat, ligne: dict[str, Any]) -> list[str]:
    """Retourne la liste des violations de la ligne (liste vide = ligne conforme)."""
    erreurs: list[str] = []
    valeurs: dict[str, Any] = {}

    for colonne in contrat.colonnes:
        brute = ligne.get(colonne.nom)
        if est_absente(brute):
            if colonne.obligatoire:
                erreurs.append(f"{colonne.nom} : valeur obligatoire manquante")
            continue
        try:
            valeur = convertir_valeur(colonne.type, brute)
        except (ValueError, TypeError):
            erreurs.append(f"{colonne.nom} : type {colonne.type} invalide (« {brute} »)")
            continue
        valeurs[colonne.nom] = valeur

        if colonne.minimum is not None and valeur < colonne.minimum:
            erreurs.append(f"{colonne.nom} : {valeur} < minimum {colonne.minimum}")
        if colonne.maximum is not None and valeur > colonne.maximum:
            erreurs.append(f"{colonne.nom} : {valeur} > maximum {colonne.maximum}")
        if colonne.motif and not re.search(colonne.motif, str(valeur)):
            erreurs.append(f"{colonne.nom} : « {valeur} » ne respecte pas le motif {colonne.motif}")
        if colonne.valeurs_autorisees and str(valeur) not in colonne.valeurs_autorisees:
            erreurs.append(f"{colonne.nom} : « {valeur} » hors de {list(colonne.valeurs_autorisees)}")

    for regle in contrat.regles_inter_colonnes:
        if regle.gauche in valeurs and regle.droite in valeurs and not valeurs[regle.gauche] <= valeurs[regle.droite]:
            erreurs.append(f"règle {regle.nom} : {regle.gauche}={valeurs[regle.gauche]} > {regle.droite}={valeurs[regle.droite]}")

    return erreurs
