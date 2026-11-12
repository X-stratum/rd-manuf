"""Convention de nom des fichiers livrés : `livraison_production_{PAYS}_{USINE}_{AAAA-MM-JJ-HH-MM-SS}Z.{csv|parquet}`.

Le nom dit d'où (pays, usine) et de quand (un instant, TOUJOURS en UTC, ce que marque le Z) vient le fichier. L'UTC évite l'ambiguïté du
changement d'heure : à 02:30 heure locale, deux fichiers pourraient porter le même nom. Le nom est contrôlé AVANT toute lecture.

Pour changer la convention (par exemple la forme du code usine), modifier les trois constantes de la section « LA CONVENTION ».
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import UTC, datetime

# ==============================================================================
# 📐 LA CONVENTION : c'est ici, et seulement ici, qu'on la change
# ==============================================================================
PREFIXE = "livraison_production"
MOTIF_PAYS = "[A-Z]{3}"                 # code pays ISO 3166-1 alpha-3, en majuscules (FRA, DEU...)
MOTIF_USINE = "[A-Z]{2}[0-9]{3}"        # 2 lettres pour l'usine, 3 chiffres pour la ligne ou le sous-ensemble (AB123)
FORMAT_DATE = "%Y-%m-%d-%H-%M-%S"       # les « : » sont interdits dans un nom de fichier Windows, d'où les tirets
FORMATS = ("csv", "parquet")

_MOTIF_DATE = "[0-9]{4}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}-[0-9]{2}"      # [0-9], pas \d : \d accepte aussi les chiffres non latins
_NOM = re.compile(rf"{PREFIXE}_(?P<pays>{MOTIF_PAYS})_(?P<usine>{MOTIF_USINE})_(?P<date>{_MOTIF_DATE})Z\.(?P<format>{'|'.join(FORMATS)})")
FORME_ATTENDUE = f"{PREFIXE}_{{PAYS}}_{{USINE}}_{{AAAA-MM-JJ-HH-MM-SS}}Z.{{{'|'.join(FORMATS)}}}"


class NomFichierInvalide(Exception):
    """Le nom ne suit pas la convention : la livraison doit être refusée, avant toute lecture.

    Elle n'hérite PAS de ValueError, comme `FichierIllisible` : un contrat cassé (erreur de chez nous) ne doit pas pouvoir être attrapé
    avec un nom de fichier mal formé (erreur de l'émetteur).
    """


@dataclass(frozen=True)
class NomFichier:
    pays: str
    usine: str
    horodatage: datetime        # un instant en UTC, avec son fuseau (jamais « naïf »)
    format: str                 # « csv » ou « parquet » : choisit le lecteur


def lire_nom_fichier(nom: str) -> NomFichier:
    """Extrait pays, usine, instant UTC et format d'un nom conforme ; lève `NomFichierInvalide` sinon."""
    trouve = _NOM.fullmatch(nom)          # toute la chaîne : `$` tolérerait un retour à la ligne final
    if trouve is None:
        raise NomFichierInvalide(f"Nom de fichier non conforme : « {nom} ». Forme attendue : {FORME_ATTENDUE}.")
    try:
        horodatage = datetime.strptime(trouve["date"], FORMAT_DATE).replace(tzinfo=UTC)
    except ValueError as erreur:       # une date qui n'existe pas : 31 février, mois 13, heure 25
        raise NomFichierInvalide(f"Nom de fichier avec une date impossible : « {nom} » ({erreur}).") from erreur
    return NomFichier(pays=trouve["pays"], usine=trouve["usine"], horodatage=horodatage, format=trouve["format"])
