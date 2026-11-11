"""Lecture d'un fichier livré : du CSV vers (colonnes reçues, lignes), prêts pour `receptionner_livraison`.

Ce module LIT fidèlement, il ne JUGE pas : il ne corrige rien et ne devine rien. Les lignes bien formées sont transmises, les lignes
malformées sont mises à part avec leur numéro ; la réception décide (refus, rejet avec un code précis).
Tout est du texte à ce stade (le contrat convertit ensuite).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

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
    lignes: list[dict[str, str]]       # uniquement les lignes bien formées
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
