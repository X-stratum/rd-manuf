"""Lecture d'un fichier livré (CSV ou Parquet) : des colonnes reçues et des lignes, prêtes pour `receptionner_livraison`.

Ce module LIT fidèlement, il ne JUGE pas : il ne corrige rien et ne devine rien. Les lignes bien formées sont transmises, les lignes
malformées (CSV) sont mises à part avec leur numéro ; la réception décide (refus, rejet avec un code précis).
Un CSV ne donne que du texte, un Parquet des valeurs déjà typées : dans les deux cas, c'est le contrat qui juge ensuite.
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rd_manuf.contrat import LigneMalformee

# ==============================================================================
# ⚙️ RÉGLAGES PAR DÉFAUT (le contrat les précisera dans un bloc `fichier:` à un ticket ultérieur)
# ==============================================================================
SEPARATEUR_PAR_DEFAUT = ","
ENCODAGE_PAR_DEFAUT = "utf-8-sig"   # lit l'UTF-8 avec OU sans BOM : le BOM d'Excel est retiré, pas laissé dans le premier nom


class FichierIllisible(Exception):
    """Le fichier ne peut pas être lu du tout (mauvais encodage, champ démesuré, format inconnu) : la livraison doit être refusée.

    Ce n'est pas un bug du programme mais un problème du fichier reçu : il doit donc être traité (refus, alerte), pas laisser s'arrêter.
    Elle n'hérite PAS de ValueError : `ContratInvalide` (un contrat mal écrit, erreur de chez nous) en est une, et les deux ne doivent pas
    pouvoir être attrapées ensemble.
    """


@dataclass(frozen=True)
class FichierLu:
    colonnes: list[str]                # l'en-tête tel que reçu, doublons compris (pour détecter COLONNE_DUPLIQUEE)
    lignes: list[dict[str, Any]]       # uniquement les lignes bien formées ; du texte pour un CSV, des valeurs typées pour un Parquet
    lignes_malformees: tuple[LigneMalformee, ...]


def lire_csv(chemin: str | Path, separateur: str = SEPARATEUR_PAR_DEFAUT, encodage: str = ENCODAGE_PAR_DEFAUT) -> FichierLu:
    lignes: list[dict[str, str]] = []
    malformees: list[LigneMalformee] = []
    try:
        # newline="" : exigé par le module csv, qui gère lui-même les retours à la ligne (y compris ceux entre guillemets)
        with Path(chemin).open("r", encoding=encodage, newline="") as fichier:
            lecteur = csv.reader(fichier, delimiter=separateur)   # pas DictReader : il fusionnerait les colonnes en double
            colonnes = next((valeurs for valeurs in lecteur if valeurs), [])   # l'en-tête est la première ligne NON vide
            for valeurs in lecteur:
                if not valeurs:
                    continue                                      # ligne totalement vide : aucune donnée, ni reçue ni malformée
                if len(valeurs) != len(colonnes):
                    malformees.append(LigneMalformee(numero=lecteur.line_num, champs=tuple(valeurs)))   # mise à part, jamais devinée
                    continue
                lignes.append(dict(zip(colonnes, valeurs, strict=True)))
    except (UnicodeDecodeError, csv.Error) as erreur:
        raise FichierIllisible(f"Le fichier ne peut pas être lu comme un CSV en {encodage} : {erreur}") from erreur
    return FichierLu(colonnes=colonnes, lignes=lignes, lignes_malformees=tuple(malformees))


def lire_parquet(chemin: str | Path) -> FichierLu:
    """Lit un fichier Parquet : le fichier porte son schéma, donc ni BOM, ni séparateur, ni ligne malformée.

    Les valeurs arrivent déjà typées (entiers, dates, absences `None`) : le contrat les juge de la même façon que celles d'un CSV.
    Un fichier qui n'est pas un Parquet valide (tronqué, autre format) lève `FichierIllisible`.
    """
    # Importé ici : le lecteur CSV et les autres modules n'ont pas besoin de ce paquet lourd.
    import pyarrow as pa
    import pyarrow.parquet as pq

    try:
        table = pq.read_table(chemin)
    except pa.ArrowException as erreur:
        raise FichierIllisible(f"Le fichier ne peut pas être lu comme un Parquet : {erreur}") from erreur
    return FichierLu(colonnes=list(table.column_names), lignes=table.to_pylist(), lignes_malformees=())


# ==============================================================================
# 🔀 AIGUILLAGE : le format annoncé par le nom du fichier choisit le lecteur
# ==============================================================================
LECTEURS = {"csv": lire_csv, "parquet": lire_parquet}     # mêmes clés que `nom_fichier.FORMATS` (un test le garantit)


def lire_selon_format(chemin: str | Path, format: str) -> FichierLu:
    """Lit le fichier avec le lecteur de son format. Un format qu'aucun lecteur ne connaît rend le fichier illisible."""
    lecteur = LECTEURS.get(format)
    if lecteur is None:
        raise FichierIllisible(f"Aucun lecteur pour le format « {format} » (formats lisibles : {', '.join(LECTEURS)}).")
    return lecteur(chemin)
