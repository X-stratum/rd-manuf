"""Lecture d'un fichier livré : du CSV vers (colonnes reçues, lignes), prêts pour `receptionner_livraison`.

Ce module LIT fidèlement, il ne JUGE pas : un fichier mal formé n'est pas corrigé ici, il est transmis tel quel à la réception,
qui le refuse ou rejette ses lignes avec un code précis. Tout est du texte à ce stade (le contrat convertit ensuite).
"""
from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

# ==============================================================================
# ⚙️ RÉGLAGES PAR DÉFAUT (le contrat les précisera dans un bloc `fichier:` à un ticket ultérieur)
# ==============================================================================
SEPARATEUR_PAR_DEFAUT = ","
ENCODAGE_PAR_DEFAUT = "utf-8-sig"   # lit l'UTF-8 avec OU sans BOM : le BOM d'Excel est retiré, pas laissé dans le premier nom


@dataclass(frozen=True)
class FichierLu:
    colonnes: list[str]                # l'en-tête tel que reçu, doublons compris (pour détecter COLONNE_DUPLIQUEE)
    lignes: list[dict[str, str]]
    lignes_malformees: tuple[int, ...]   # numéros (en-tête = ligne 1) des lignes au nombre de champs différent de l'en-tête


def lire_csv(chemin: str | Path, separateur: str = SEPARATEUR_PAR_DEFAUT, encodage: str = ENCODAGE_PAR_DEFAUT) -> FichierLu:
    lignes: list[dict[str, str]] = []
    malformees: list[int] = []
    # newline="" : exigé par le module csv, qui gère lui-même les retours à la ligne (y compris ceux entre guillemets)
    with Path(chemin).open("r", encoding=encodage, newline="") as fichier:
        lecteur = csv.reader(fichier, delimiter=separateur)   # pas DictReader : il fusionnerait les colonnes en double
        colonnes = next(lecteur, [])
        for valeurs in lecteur:
            if not valeurs:
                continue                                      # ligne totalement vide : aucune donnée, ni ligne reçue ni ligne malformée
            if len(valeurs) != len(colonnes):
                malformees.append(lecteur.line_num)           # numéro de ligne du fichier, tel que l'émetteur le verrait dans Excel
            # strict=False voulu : une ligne plus courte ou plus longue est signalée ci-dessus, puis transmise ; la réception décidera
            lignes.append(dict(zip(colonnes, valeurs, strict=False)))
    return FichierLu(colonnes=colonnes, lignes=lignes, lignes_malformees=tuple(malformees))
